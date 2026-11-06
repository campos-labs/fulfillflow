-- Separate regression target for the preserved v1.0 migrations and frozen dataset.
CREATE ROLE fulfillflow_legacy LOGIN PASSWORD 'v11-isolated-legacy-test';
CREATE DATABASE fulfillflow_legacy OWNER fulfillflow_legacy;
REVOKE ALL ON DATABASE fulfillflow_legacy FROM PUBLIC;
