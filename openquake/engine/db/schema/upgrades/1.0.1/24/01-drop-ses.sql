DROP TABLE hzrdr.ses; -- obsolete

ALTER TABLE hzrdr.ses_collection ALTER COLUMN lt_model_id DROP NOT NULL;

ALTER TABLE uiapi.output
SET output_type='gmf' WHERE output_type='gmf_scenario'; 
