-- Enterprise cost logs (PostgreSQL 18 compatible)
-- Apply: psql -U postgres -d postgres -f database/schema.sql

CREATE TABLE IF NOT EXISTS cloud_costs (
    id          BIGSERIAL PRIMARY KEY,
    date        DATE NOT NULL,
    service_name TEXT NOT NULL,
    environment TEXT NOT NULL CHECK (environment IN ('dev', 'prod')),
    cost_usd    NUMERIC(12, 4) NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_cloud_costs_date ON cloud_costs (date DESC);
CREATE INDEX IF NOT EXISTS idx_cloud_costs_env ON cloud_costs (environment);
CREATE INDEX IF NOT EXISTS idx_cloud_costs_service ON cloud_costs (service_name);

-- Mock data for local testing
INSERT INTO cloud_costs (date, service_name, environment, cost_usd) VALUES
    ('2025-03-01', 'Compute Engine', 'prod', 1240.5500),
    ('2025-03-01', 'Cloud Storage', 'prod', 89.1200),
    ('2025-03-01', 'BigQuery', 'dev', 45.0000),
    ('2025-03-02', 'Compute Engine', 'prod', 1310.2000),
    ('2025-03-02', 'Cloud SQL', 'prod', 210.7500),
    ('2025-03-02', 'Artifact Registry', 'dev', 12.3400),
    ('2025-03-03', 'Compute Engine', 'prod', 1189.9000),
    ('2025-03-03', 'Networking', 'prod', 156.0000),
    ('2025-03-03', 'Vertex AI', 'dev', 78.6600),
    ('2025-03-04', 'BigQuery', 'prod', 402.1000),
    ('2025-03-04', 'Cloud Storage', 'prod', 91.0000),
    ('2025-03-04', 'Logging', 'dev', 5.2000);

-- If re-running: clear and reload (optional dev helper)
-- TRUNCATE cloud_costs RESTART IDENTITY CASCADE;
-- Then re-run INSERTs above.
