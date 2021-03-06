# Copyright (c) 2010-2014, GEM Foundation.
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
Core calculator functionality for computing stochastic event sets and ground
motion fields using the 'event-based' method.

Stochastic events sets (which can be thought of as collections of ruptures) are
computed iven a set of seismic sources and investigation time span (in years).

For more information on computing stochastic event sets, see
:mod:`openquake.hazardlib.calc.stochastic`.

One can optionally compute a ground motion field (GMF) given a rupture, a site
collection (which is a collection of geographical points with associated soil
parameters), and a ground shaking intensity model (GSIM).

For more information on computing ground motion fields, see
:mod:`openquake.hazardlib.calc.gmf`.
"""

import time
import random
import operator
import itertools
import collections

import numpy.random

from django.db import transaction
from openquake.hazardlib.calc import gmf, filters
from openquake.hazardlib.imt import from_string
from openquake.hazardlib.site import FilteredSiteCollection

from openquake.commonlib import logictree

from openquake.engine import writer
from openquake.engine.calculators.hazard import general
from openquake.engine.db import models
from openquake.engine.utils import tasks
from openquake.engine.performance import EnginePerformanceMonitor, LightMonitor

# NB: beware of large caches
inserter = writer.CacheInserter(models.GmfData, 1000)
source_inserter = writer.CacheInserter(models.SourceInfo, 100)


# NB (MS): the approach used here will not work for non-poissonian models
def gmvs_to_haz_curve(gmvs, imls, invest_time, duration):
    """
    Given a set of ground motion values (``gmvs``) and intensity measure levels
    (``imls``), compute hazard curve probabilities of exceedance.

    :param gmvs:
        A list of ground motion values, as floats.
    :param imls:
        A list of intensity measure levels, as floats.
    :param float invest_time:
        Investigation time, in years. It is with this time span that we compute
        probabilities of exceedance.

        Another way to put it is the following. When computing a hazard curve,
        we want to answer the question: What is the probability of ground
        motion meeting or exceeding the specified levels (``imls``) in a given
        time span (``invest_time``).
    :param float duration:
        Time window during which GMFs occur. Another was to say it is, the
        period of time over which we simulate ground motion occurrences.

        NOTE: Duration is computed as the calculation investigation time
        multiplied by the number of stochastic event sets.

    :returns:
        Numpy array of PoEs (probabilities of exceedance).
    """
    # convert to numpy array and redimension so that it can be broadcast with
    # the gmvs for computing PoE values; there is a gmv for each rupture
    # here is an example: imls = [0.03, 0.04, 0.05], gmvs=[0.04750576]
    # => num_exceeding = [1, 1, 0] coming from 0.04750576 > [0.03, 0.04, 0.05]
    imls = numpy.array(imls).reshape((len(imls), 1))
    num_exceeding = numpy.sum(numpy.array(gmvs) >= imls, axis=1)
    poes = 1 - numpy.exp(- (invest_time / duration) * num_exceeding)
    return poes


@tasks.oqtask
def compute_ruptures(
        job_id, sitecol, src_seeds, trt_model_id):
    """
    Celery task for the stochastic event set calculator.

    Samples logic trees and calls the stochastic event set calculator.

    Once stochastic event sets are calculated, results will be saved to the
    database. See :class:`openquake.engine.db.models.SESCollection`.

    Optionally (specified in the job configuration using the
    `ground_motion_fields` parameter), GMFs can be computed from each rupture
    in each stochastic event set. GMFs are also saved to the database.

    :param int job_id:
        ID of the currently running job.
    :param sitecol:
        a :class:`openquake.hazardlib.site.SiteCollection` instance
    :param src_seeds:
        List of pairs (source, seed)
    """
    # NB: all realizations in gsims correspond to the same source model
    trt_model = models.TrtModel.objects.get(pk=trt_model_id)
    ses_coll = models.SESCollection.objects.get(lt_model=trt_model.lt_model)

    hc = models.HazardCalculation.objects.get(oqjob=job_id)
    all_ses = range(1, hc.ses_per_logic_tree_path + 1)
    tot_ruptures = 0

    filter_sites_mon = LightMonitor(
        'filtering sites', job_id, compute_ruptures)
    generate_ruptures_mon = LightMonitor(
        'generating ruptures', job_id, compute_ruptures)
    filter_ruptures_mon = LightMonitor(
        'filtering ruptures', job_id, compute_ruptures)
    save_ruptures_mon = LightMonitor(
        'saving ruptures', job_id, compute_ruptures)

    # Compute and save stochastic event sets
    rnd = random.Random()
    for src, seed in src_seeds:
        t0 = time.time()
        rnd.seed(seed)

        with filter_sites_mon:  # filtering sources
            s_sites = src.filter_sites_by_distance_to_source(
                hc.maximum_distance, sitecol
            ) if hc.maximum_distance else sitecol
            if s_sites is None:
                continue

        # the dictionary `ses_num_occ` contains [(ses, num_occurrences)]
        # for each occurring rupture for each ses in the ses collection
        ses_num_occ = collections.defaultdict(list)
        with generate_ruptures_mon:  # generating ruptures for the given source
            for rup_no, rup in enumerate(src.iter_ruptures(), 1):
                rup.rup_no = rup_no
                for ses_idx in all_ses:
                    numpy.random.seed(rnd.randint(0, models.MAX_SINT_32))
                    num_occurrences = rup.sample_number_of_occurrences()
                    if num_occurrences:
                        ses_num_occ[rup].append((ses_idx, num_occurrences))

        # NB: the number of occurrences is very low, << 1, so it is
        # more efficient to filter only the ruptures that occur, i.e.
        # to call sample_number_of_occurrences() *before* the filtering
        for rup in sorted(ses_num_occ, key=operator.attrgetter('rup_no')):
            with filter_ruptures_mon:  # filtering ruptures
                r_sites = filters.filter_sites_by_distance_to_rupture(
                    rup, hc.maximum_distance, s_sites
                    ) if hc.maximum_distance else s_sites
                if r_sites is None:
                    # ignore ruptures which are far away
                    del ses_num_occ[rup]  # save memory
                    continue

            # saving ses_ruptures
            with save_ruptures_mon:
                # using a django transaction make the saving faster
                with transaction.commit_on_success(using='job_init'):
                    indices = r_sites.indices if len(r_sites) < len(sitecol) \
                        else None  # None means that nothing was filtered
                    prob_rup = models.ProbabilisticRupture.create(
                        rup, ses_coll, trt_model, indices)
                    for ses_idx, num_occurrences in ses_num_occ[rup]:
                        for occ_no in range(1, num_occurrences + 1):
                            rup_seed = rnd.randint(0, models.MAX_SINT_32)
                            models.SESRupture.create(
                                prob_rup, ses_idx, src.source_id,
                                rup.rup_no, occ_no, rup_seed)

        if ses_num_occ:
            num_ruptures = len(ses_num_occ)
            occ_ruptures = sum(num for rup in ses_num_occ
                               for ses, num in ses_num_occ[rup])
            tot_ruptures += occ_ruptures
        else:
            num_ruptures = rup_no
            occ_ruptures = 0

        # save SourceInfo
        source_inserter.add(
            models.SourceInfo(trt_model_id=trt_model_id,
                              source_id=src.source_id,
                              source_class=src.__class__.__name__,
                              num_sites=len(s_sites),
                              num_ruptures=rup_no,
                              occ_ruptures=occ_ruptures,
                              uniq_ruptures=num_ruptures,
                              calc_time=time.time() - t0))

    filter_sites_mon.flush()
    generate_ruptures_mon.flush()
    filter_ruptures_mon.flush()
    save_ruptures_mon.flush()
    source_inserter.flush()

    return tot_ruptures, trt_model_id


@tasks.oqtask
def compute_gmfs_and_curves(job_id, ses_ruptures, sitecol):
    """
    :param int job_id:
        ID of the currently running job
    :param ses_ruptures:
        a list of blocks of SESRuptures with homogeneous TrtModel
    :param sitecol:
        a SiteCollection instance
    """
    job = models.OqJob.objects.get(pk=job_id)
    hc = job.hazard_calculation
    imts = map(from_string, hc.intensity_measure_types)

    result = {}  # trt_model_id -> (curves_by_gsim, [])
    # NB: by construction each block is a non-empty list with
    # ruptures of homogeneous trt_model
    trt_model = ses_ruptures[0].rupture.trt_model
    rlzs_by_gsim = trt_model.get_rlzs_by_gsim()
    gsims = [logictree.GSIM[gsim]() for gsim in rlzs_by_gsim]
    calc = GmfCalculator(
        sorted(imts), sorted(gsims), trt_model.id,
        hc.truncation_level, models.get_correl_model(job))

    with EnginePerformanceMonitor(
            'computing gmfs', job_id, compute_gmfs_and_curves):
        for rupture, group in itertools.groupby(
                ses_ruptures, operator.attrgetter('rupture')):
            r_sites = sitecol if rupture.site_indices is None \
                else FilteredSiteCollection(rupture.site_indices, sitecol)
            calc.calc_gmfs(
                r_sites, rupture, [(r.id, r.seed) for r in group])

    if hc.hazard_curves_from_gmfs:
        with EnginePerformanceMonitor(
                'hazard curves from gmfs',
                job_id, compute_gmfs_and_curves):
            result[trt_model.id] = (calc.to_haz_curves(
                sitecol.sids, hc.intensity_measure_types_and_levels,
                hc.investigation_time, hc.ses_per_logic_tree_path), [])
    else:
        result[trt_model.id] = ([], [])

    if hc.ground_motion_fields:
        with EnginePerformanceMonitor(
                'saving gmfs', job_id, compute_gmfs_and_curves):
            calc.save_gmfs(rlzs_by_gsim)

    return result


class GmfCalculator(object):
    """
    A class to store ruptures and then compute and save ground motion fields.
    """
    def __init__(self, sorted_imts, sorted_gsims, trt_model_id,
                 truncation_level=None, correl_model=None):
        """
        :param sorted_imts:
            a sorted list of hazardlib intensity measure types
        :param sorted_gsims:
            a sorted list of hazardlib GSIM instances
        :param int trt_model_id:
            the ID of a TRTModel instance
        :param int truncation_level:
            the truncation level, or None
        :param str correl_model:
            the correlation model, or None
        """
        self.sorted_imts = sorted_imts
        self.sorted_gsims = sorted_gsims
        self.trt_model_id = trt_model_id
        self.truncation_level = truncation_level
        self.correl_model = correl_model
        # NB: I tried to use a single dictionary
        # {site_id: [(gmv, rupt_id),...]} but it took a lot more memory (MS)
        self.gmvs_per_site = collections.defaultdict(list)
        self.ruptures_per_site = collections.defaultdict(list)

    def calc_gmfs(self, r_sites, rupture, rupid_seed_pairs):
        """
        Compute the GMF generated by the given rupture on the given
        sites and collect the values in the dictionaries
        .gmvs_per_site and .ruptures_per_site.

        :param r_sites:
            a SiteCollection instance with the sites affected by the rupture
        :param rupture:
            a ProbabilisticRupture instance
        :param rupid_seed_pairs:
            a list of pairs (ses_rupture_id, ses_rupture_seed)
        """
        computer = gmf.GmfComputer(
            rupture, r_sites, self.sorted_imts, self.sorted_gsims,
            self.truncation_level, self.correl_model)
        for rupid, seed in rupid_seed_pairs:
            for (gsim_name, imt_str), gmvs in computer.compute(seed):
                for site_id, gmv in zip(r_sites.sids, gmvs):
                    self.gmvs_per_site[
                        gsim_name, imt_str, site_id].append(gmv)
                    self.ruptures_per_site[
                        gsim_name, imt_str, site_id].append(rupid)

    def save_gmfs(self, rlzs_by_gsim):
        """
        Helper method to save the computed GMF data to the database.
        """
        for gsim_name, imt_str, site_id in self.gmvs_per_site:
            for rlz in rlzs_by_gsim[gsim_name]:
                imt_name, sa_period, sa_damping = from_string(imt_str)
                inserter.add(models.GmfData(
                    gmf=models.Gmf.objects.get(lt_realization=rlz),
                    task_no=0,
                    imt=imt_name,
                    sa_period=sa_period,
                    sa_damping=sa_damping,
                    site_id=site_id,
                    gmvs=self.gmvs_per_site[gsim_name, imt_str, site_id],
                    rupture_ids=self.ruptures_per_site[
                        gsim_name, imt_str, site_id]
                ))
        inserter.flush()
        self.gmvs_per_site.clear()
        self.ruptures_per_site.clear()

    def to_haz_curves(self, sids, imtls, invest_time, num_ses):
        """
        Convert the gmf into hazard curves (by gsim and imt)

        :param sids: database ids of the given sites
        :param imtls: dictionary {IMT: intensity measure levels}
        :param invest_time: investigation time
        :param num_ses: number of Stochastic Event Sets
        """
        gmf = collections.defaultdict(dict)  # (gsim, imt) > {site_id: poes}
        sorted_imts = map(str, self.sorted_imts)
        zeros = {imt: numpy.zeros(len(imtls[imt])) for imt in sorted_imts}
        for (gsim, imt, site_id), gmvs in self.gmvs_per_site.iteritems():
            gmf[gsim, imt][site_id] = gmvs_to_haz_curve(
                gmvs, imtls[imt], invest_time, num_ses * invest_time)
        curves_by_gsim = []
        for gsim_obj in self.sorted_gsims:
            gsim = gsim_obj.__class__.__name__
            curves_by_imt = []
            for imt in sorted_imts:
                curves_by_imt.append(
                    numpy.array([gmf[gsim, imt].get(site_id, zeros[imt])
                                 for site_id in sids]))
            curves_by_gsim.append((gsim, curves_by_imt))
        return curves_by_gsim


class EventBasedHazardCalculator(general.BaseHazardCalculator):
    """
    Probabilistic Event-Based hazard calculator. Computes stochastic event sets
    and (optionally) ground motion fields.
    """
    core_calc_task = compute_ruptures

    def task_arg_gen(self, _block_size=None):
        """
        Loop through realizations and sources to generate a sequence of
        task arg tuples. Each tuple of args applies to a single task.
        Yielded results are tuples of the form job_id, sources, ses, seeds
        (seeds will be used to seed numpy for temporal occurence sampling).
        """
        hc = self.hc
        rnd = random.Random()
        rnd.seed(hc.random_seed)
        for job_id, sitecol, block, trt_model_id in \
                super(EventBasedHazardCalculator, self).task_arg_gen():
            ss = [(src, rnd.randint(0, models.MAX_SINT_32))
                  for src in block]  # source, seed pairs
            yield job_id, sitecol, ss, trt_model_id

    def task_completed(self, task_result):
        """
        :param task_result:
            a pair (num_ruptures, trt_model_id)

        If the parameter `ground_motion_fields` is set, compute and save
        the GMFs from the ruptures generated by the given task.
        """
        num_ruptures, trt_model_id = task_result
        if num_ruptures:
            self.num_ruptures[trt_model_id] += num_ruptures

    def post_execute(self):
        trt_models = models.TrtModel.objects.filter(
            lt_model__hazard_calculation=self.hc)
        # save the right number of occurring ruptures
        for trt_model in trt_models:
            trt_model.num_ruptures = self.num_ruptures.get(trt_model.id, 0)
            trt_model.save()
        if (not self.hc.ground_motion_fields and
                not self.hc.hazard_curves_from_gmfs):
            return  # do nothing

        # create a Gmf output for each realization
        self.initialize_realizations()
        if self.hc.ground_motion_fields:
            for rlz in self._get_realizations():
                output = models.Output.objects.create(
                    oq_job=self.job,
                    display_name='GMF rlz-%s' % rlz.id,
                    output_type='gmf')
                models.Gmf.objects.create(output=output, lt_realization=rlz)

        self.generate_gmfs_and_curves()

        # now save the curves, if any
        if self.curves:
            self.save_hazard_curves()

    @EnginePerformanceMonitor.monitor
    def generate_gmfs_and_curves(self):
        """
        Generate the GMFs and optionally the hazard curves too
        """
        sitecol = self.hc.site_collection
        sesruptures = []  # collect the ruptures in a fixed order
        for trt_model in models.TrtModel.objects.filter(
                lt_model__hazard_calculation=self.hc):
            sesruptures.extend(
                models.SESRupture.objects.filter(
                    rupture__trt_model=trt_model))
        self.curves = tasks.apply_reduce(
            compute_gmfs_and_curves,
            (self.job.id, sesruptures, sitecol),
            self.agg_curves, {}, key=lambda sr: sr.rupture.trt_model.id)

    def initialize_ses_db_records(self, lt_model):
        """
        Create :class:`~openquake.engine.db.models.Output`,
        :class:`~openquake.engine.db.models.SESCollection` and
        :class:`~openquake.engine.db.models.SES` "container" records for
        a single realization.

        Stochastic event set ruptures computed for this realization will be
        associated to these containers.

        NOTE: Many tasks can contribute ruptures to the same SES.
        """
        output = models.Output.objects.create(
            oq_job=self.job,
            display_name='SES Collection smlt-%d' % lt_model.ordinal,
            output_type='ses')

        ses_coll = models.SESCollection.objects.create(
            output=output, lt_model=lt_model, ordinal=lt_model.ordinal)

        return ses_coll

    def pre_execute(self):
        """
        Do pre-execution work. At the moment, this work entails:
        parsing and initializing sources, parsing and initializing the
        site model (if there is one), parsing vulnerability and
        exposure files, and generating logic tree realizations. (The
        latter piece basically defines the work to be done in the
        `execute` phase.)
        """
        weights = super(EventBasedHazardCalculator, self).pre_execute()
        for lt_model in models.LtSourceModel.objects.filter(
                hazard_calculation=self.hc):
            self.initialize_ses_db_records(lt_model)
        return weights
