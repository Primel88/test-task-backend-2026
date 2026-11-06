-- Для внешнего поиска по названию: LIKE, ILIKE и, начиная с PostgreSQL 14, равенство.
-- pg_trgm входит в contrib и отдельного сервиса не требует.
BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX ix_amo_customer_name_trgm ON amo_customer USING gin (name gin_trgm_ops);
CREATE INDEX ix_amo_lead_name_trgm ON amo_lead USING gin (name gin_trgm_ops);

COMMIT;
