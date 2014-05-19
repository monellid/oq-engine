# -*- coding: utf-8 -*-

# Copyright (c) 2012-2014, GEM Foundation.
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake.  If not, see <http://www.gnu.org/licenses/>.

"""
Hazard getters for Risk calculators.

A HazardGetter is responsible fo getting hazard outputs needed by a risk
calculation.
"""
import itertools
import operator

import numpy

from openquake.hazardlib.imt import from_string
from openquake.risklib import scientific

from openquake.engine import logs
from openquake.engine.db import models

BYTES_PER_FLOAT = numpy.zeros(1, dtype=float).nbytes


class AssetSiteAssociationError(Exception):
    pass


def make_epsilons(asset_count, num_ruptures, seed, correlation,
                  epsilons_management):
    """
    :param int asset_count: the number of assets
    :param int num_ruptures: the number of ruptures
    :param int seed: a random seed
    :param float correlation: the correlation coefficient
    :param str epsilons_management: specify how to compute the epsilon matrix

    If `epsilons_management` is 'full', generate the full epsilon matrix;
    if it is 'fast' generate a vector of epsilons.
    """
    if epsilons_management == 'full':
        # generate the full epsilon matrix
        zeros = numpy.zeros((asset_count, num_ruptures))
        return scientific.make_epsilons(zeros, seed, correlation)
    elif epsilons_management == 'fast':
        # use a single epsilon for all realizations
        zeros = numpy.zeros((asset_count, 1))
        return scientific.make_epsilons(zeros, seed, correlation).reshape(-1)
    else:
        raise RuntimeError('Wrong parameter epsilons_management '
                           'in openquake.cfg: %r' % epsilons_management)


class HazardGetter(object):
    """
    A Hazard Getter is used to query for the closest hazard data for
    each given asset. A Hazard Getter must be pickable such that it
    should be possible to use different strategies (e.g. distributed
    or not, using postgis or not).
    A Hazard Getter should be instantiated by a GetterBuilder and
    not directly.

    :attr hazard_output:
        A :class:`openquake.engine.db.models.Output` instance

    :attr assets:
        The assets for which we want to extract the hazard

   :attr site_ids:
        The ids of the sites associated to the hazards
    """
    def __init__(self, hazard_output, assets, site_ids):
        self.hazard_output = hazard_output
        self.assets = assets
        self.site_ids = site_ids
        self.epsilons = None

    def __repr__(self):
        shape = getattr(self.epsilons, 'shape', None)
        eps = ', %s epsilons' % str(shape) if shape else ''
        return "<%s %d assets%s>" % (
            self.__class__.__name__, len(self.assets), eps)

    def get_data(self):
        """
        Subclasses must implement this.
        """
        raise NotImplementedError

    @property
    def hid(self):
        """Return the id of the given hazard output"""
        return self.hazard_output.id

    @property
    def weight(self):
        """Return the weight of the realization of the hazard output"""
        h = self.hazard_output.output_container
        if hasattr(h, 'lt_realization') and h.lt_realization:
            return h.lt_realization.weight


class HazardCurveGetter(HazardGetter):
    """
    Simple HazardCurve Getter that performs a spatial query for each
    asset.
    """ + HazardGetter.__doc__

    def get_data(self, imt):
        """
        Extracts the hazard curves for the given `imt` from the hazard output.

        :param str imt: Intensity Measure Type
        :returns: a list of N curves, each one being a list of pairs (iml, poe)
        """
        imt_type, sa_period, sa_damping = from_string(imt)

        oc = self.hazard_output.output_container
        if oc.output.output_type == 'hazard_curve':
            imls = oc.imls
        elif oc.output.output_type == 'hazard_curve_multi':
            oc = models.HazardCurve.objects.get(
                output__oq_job=oc.output.oq_job,
                output__output_type='hazard_curve',
                statistics=oc.statistics,
                lt_realization=oc.lt_realization,
                imt=imt_type,
                sa_period=sa_period,
                sa_damping=sa_damping)
            imls = oc.imls

        cursor = models.getcursor('job_init')
        query = """\
        SELECT hzrdr.hazard_curve_data.poes
        FROM hzrdr.hazard_curve_data
        WHERE hazard_curve_id = %s AND location = %s
        """
        all_curves = []
        for site_id in self.site_ids:
            location = models.HazardSite.objects.get(pk=site_id).location
            cursor.execute(query, (oc.id, 'SRID=4326; ' + location.wkt))
            poes = cursor.fetchall()[0][0]
            all_curves.append(zip(imls, poes))
        return all_curves


class GroundMotionValuesGetter(HazardGetter):
    """
    Hazard getter for loading ground motion values.
    """ + HazardGetter.__doc__
    rupture_ids = None  # set by the GetterBuilder
    epsilons = None  # set by the GetterBuilder

    def _get_gmv_dict(self, imt_type, sa_period, sa_damping):
        """
        :returns: a dictionary {rupture_id: gmv} for the given site and IMT
        """
        gmf_id = self.hazard_output.output_container.id
        if sa_period:
            imt_query = 'imt=%s and sa_period=%s and sa_damping=%s'
        else:
            imt_query = 'imt=%s and sa_period is %s and sa_damping is %s'
        gmv_dict = {}
        cursor = models.getcursor('job_init')
        cursor.execute('select site_id, rupture_ids, gmvs from '
                       'hzrdr.gmf_data where gmf_id=%s and site_id in %s '
                       'and {} order by site_id, task_no'.format(imt_query),
                       (gmf_id, tuple(self.site_ids),
                        imt_type, sa_period, sa_damping))
        for sid, group in itertools.groupby(cursor, operator.itemgetter(0)):
            gmvs = []
            ruptures = []
            for site_id, rupture_ids, gmvs in group:
                gmvs.extend(gmvs)
                ruptures.extend(rupture_ids)
            gmv_dict[sid] = dict(itertools.izip(ruptures, gmvs))
        return gmv_dict

    def get_data(self, imt):
        """
        Extracts the GMFs for the given `imt` from the hazard output.

        :param str imt: Intensity Measure Type
        :returns: a list of N arrays with R elements each.
        """
        imt_type, sa_period, sa_damping = from_string(imt)
        gmv_dict = self._get_gmv_dict(imt_type, sa_period, sa_damping)
        all_gmvs = []
        for site_id in self.site_ids:
            gmv = gmv_dict.get(site_id, {})
            if not gmv:
                logs.LOG.info('No data for site_id=%d, imt=%s', site_id, imt)
            array = numpy.array([gmv.get(r, 0.) for r in self.rupture_ids])
            all_gmvs.append(array)
        return all_gmvs


class ScenarioGetter(HazardGetter):
    """
    Hazard getter for loading ground motion values.
    """ + HazardGetter.__doc__

    rupture_ids = []  # there are no ruptures on the db
    epsilons = None  # set by the GetterBuilder
    num_samples = None  # set by the GetterBuilder

    def get_data(self, imt):
        """
        Extracts the GMFs for the given `imt` from the hazard output.

        :param str imt: Intensity Measure Type
        :returns: a list of N arrays with R elements each.
        """
        imt_type, sa_period, sa_damping = from_string(imt)
        gmf_id = self.hazard_output.output_container.id
        if sa_period:
            imt_query = 'imt=%s and sa_period=%s and sa_damping=%s'
        else:
            imt_query = 'imt=%s and sa_period is %s and sa_damping is %s'
        cursor = models.getcursor('job_init')
        templ = ('select site_id, gmvs from '
                 'hzrdr.gmf_data where gmf_id=%s and site_id in %s '
                 'and {} order by site_id, task_no'.format(imt_query))
        args = (gmf_id, tuple(set(self.site_ids)),
                imt_type, sa_period, sa_damping)
        cursor.execute(templ, args)

        gmvs_dict = {}  # site_id -> gmvs array
        for sid, group in itertools.groupby(cursor, operator.itemgetter(0)):
            gmvs = []
            for site_id, gmvs_ in group:
                gmvs.extend(gmvs_)
            gmvs_dict[sid] = numpy.array(gmvs, dtype=float)
        # TODO: add the test for the case where there is no data
        # for the given site and vector of zeros is returned
        return [gmvs_dict[sid] if sid in gmvs_dict
                else numpy.zeros(self.num_samples, dtype=float)
                for sid in self.site_ids]


class GetterBuilder(object):
    """
    A facility to build hazard getters. When instantiated, populates
    the lists .asset_ids and .site_ids with the associations between
    the assets in the current exposure model and the sites in the
    previous hazard calculation.

    :param str taxonomy: the taxonomy we are interested in
    :param rc: a :class:`openquake.engine.db.models.RiskCalculation` instance

    Warning: instantiating a GetterBuilder performs a potentially
    expensive geospatial query.
    """
    def __init__(self, taxonomy, rc, epsilons_management='full'):
        self.taxonomy = taxonomy
        self.rc = rc
        self.epsilons_management = epsilons_management
        self.hc = rc.get_hazard_calculation()
        max_dist = rc.best_maximum_distance * 1000  # km to meters
        cursor = models.getcursor('job_init')
        cursor.execute("""
SELECT DISTINCT ON (exp.id) exp.id AS asset_id, hsite.id AS site_id
FROM riski.exposure_data AS exp
JOIN hzrdi.hazard_site AS hsite
ON ST_DWithin(exp.site, hsite.location, %s)
WHERE hsite.hazard_calculation_id = %s
AND exposure_model_id = %s AND taxonomy=%s
AND ST_COVERS(ST_GeographyFromText(%s), exp.site)
ORDER BY exp.id, ST_Distance(exp.site, hsite.location, false)
""", (max_dist, self.hc.id, rc.exposure_model.id, taxonomy,
            rc.region_constraint.wkt))
        self.asset_ids, self.site_ids = zip(*cursor.fetchall())
        self.rupture_ids = {}
        self.epsilons = {}
        self.epsilons_shape = {}

    def calc_nbytes(self, hazard_outputs):
        """
        :param hazard_outputs: the outputs of a hazard calculation
        :returns: the number of bytes to be allocated (can be
                  much less if there is no correlation and the 'fast'
                  epsilons management is enabled).

        If the hazard_outputs come from an event based or scenario computation,
        populate the .epsilons_shape dictionary.
        """
        num_assets = len(self.asset_ids)
        ses_collections = []
        if self.hc.calculation_mode == 'event_based':
            lt_model_ids = set(ho.output_container.lt_realization.lt_model.id
                               for ho in hazard_outputs)
            for lt_model_id in lt_model_ids:
                ses_collections.append(
                    models.SESCollection.objects.get(lt_model=lt_model_id))
        elif self.hc.calculation_mode == 'scenario':
            ses_collections.append(
                models.SESCollection.objects.get(
                    output__oq_job=hazard_outputs[0].oq_job,
                    output__output_type='ses'))

        for ses_coll in ses_collections:
            if self.epsilons_management == 'full':
                samples = ses_coll.get_ruptures().count()
            else:
                samples = 1
            self.epsilons_shape[ses_coll.id] = (num_assets, samples)

        if self.epsilons_management == 'fast' and self.epsilons_shape:
            # size of the correlation matrix
            return num_assets * num_assets * BYTES_PER_FLOAT
        nbytes = 0
        for (n, r) in self.epsilons_shape.values():
            # the max(n, r) is taken because if n > r then the limiting
            # factor is the size of the correlation matrix, i.e. n
            nbytes += max(n, r) * n * BYTES_PER_FLOAT
        return nbytes

    def init_epsilons(self, hazard_outputs):
        """
        :param hazard_outputs: the outputs of a hazard calculation

        If the hazard_outputs come from an event based or scenario computation,
        populate the .epsilons and the .rupture_ids dictionaries.
        """
        ses_collections = []
        if not self.epsilons_shape:
            self.calc_nbytes(hazard_outputs)
        if self.hc.calculation_mode == 'event_based':
            lt_model_ids = set(ho.output_container.lt_realization.lt_model.id
                               for ho in hazard_outputs)
            for lt_model_id in lt_model_ids:
                ses_collections.append(models.SESCollection.objects.get(
                    lt_model=lt_model_id))
        elif self.hc.calculation_mode == 'scenario':
            ses_collections.append(
                models.SESCollection.objects.get(
                    output__oq_job=hazard_outputs[0].oq_job,
                    output__output_type='ses'))

        for ses_coll in ses_collections:
            scid = ses_coll.id
            self.rupture_ids[scid] = ses_coll.get_ruptures(
                ).values_list('id', flat=True)
            self.epsilons[scid] = make_epsilons(
                len(self.asset_ids), len(self.rupture_ids[scid]),
                self.rc.master_seed, self.rc.asset_correlation,
                self.epsilons_management)

    def _indices_asset_site(self, asset_block):
        """
        Filter the given assets by the asset_ids known to the builder
        and determine their indices.

        :param asset_block: a block of assets of the right taxonomy
        :returns: three lists of the same lenght indices, assets, site_ids
        """
        indices = []
        assets = []
        site_ids = []
        for asset in asset_block:
            assert asset.taxonomy == self.taxonomy, (
                asset.taxonomy, self.taxonomy)
            try:
                idx = self.asset_ids.index(asset.id)
            except ValueError:  # asset.id not in list
                logs.LOG.info(
                    "No hazard has been found for "
                    "the asset %s within %s km", asset,
                    self.rc.best_maximum_distance)
            else:
                site_ids.append(self.site_ids[idx])
                assets.append(asset)
                indices.append(idx)
        return indices, assets, site_ids

    def make_getters(self, gettercls, hazard_outputs, asset_block):
        """
        Build the appropriate hazard getters from the given hazard
        outputs. The assets which have no corresponding hazard site
        within the maximum distance are discarded. A RuntimeError is
        raised if all assets are discarded. From outputs coming from
        an event based or a scenario calculation the right epsilons
        corresponding to the assets are stored in the getters.

        :param gettercls: the HazardGetter subclass to use
        :param hazard_outputs: the outputs of a hazard calculation
        :param asset_block: a block of assets

        :returns: a list of HazardGetter instances
        """
        indices, assets, site_ids = self._indices_asset_site(asset_block)
        if not indices:
            raise AssetSiteAssociationError(
                'Could not associated any asset in %s to '
                'hazard sites within the distance of %s km'
                % (asset_block, self.rc.best_maximum_distance))
        if not self.epsilons:
            self.init_epsilons(hazard_outputs)
        getters = []
        for ho in hazard_outputs:
            getter = gettercls(ho, assets, site_ids)
            if self.hc.calculation_mode == 'event_based':
                ses_coll_id = models.SESCollection.objects.get(
                    lt_model=ho.output_container.lt_realization.lt_model).id
                getter.rupture_ids = self.rupture_ids[ses_coll_id]
                getter.epsilons = self.epsilons[ses_coll_id][indices]
            elif self.hc.calculation_mode == 'scenario':
                [(ses_coll_id, rupture_ids)] = self.rupture_ids.items()
                getter.rupture_ids = rupture_ids
                getter.epsilons = self.epsilons[ses_coll_id][indices]
            getters.append(getter)
        return getters
