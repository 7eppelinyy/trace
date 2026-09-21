ALTER TABLE forecast_snapshot ADD COLUMN time_basis TEXT NOT NULL DEFAULT 'analysis_recorded';
ALTER TABLE forecast_check ADD COLUMN time_basis TEXT NOT NULL DEFAULT 'analysis_recorded';
-- Historical v1 records were made by mixed/backfilled paths. Preserve originals,
-- but do not assert provenance that cannot be established retrospectively.
UPDATE forecast_snapshot SET time_basis='legacy_unverified';
UPDATE forecast_check SET time_basis='legacy_unverified';
