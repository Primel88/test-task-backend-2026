-- Покупатели и сделки переезжают из JSONB-массивов abstract_customer в отдельные строки.
-- payload_hash и contact_ids пусты: первый прогон после миграции перепишет все строки.
-- Дубль по id CRM не переживает первичный ключ: остаётся запись с меньшим id,
-- а записи без покупателей и сделок удалит ближайшая чистка.
BEGIN;

CREATE TABLE amo_customer (
    crm_id               BIGINT NOT NULL,
    data                 JSONB NOT NULL,
    login                TEXT GENERATED ALWAYS AS (data -> 'general' ->> 'login') STORED,
    name                 TEXT GENERATED ALWAYS AS (data -> 'general' ->> 'name') STORED,
    contact_ids          BIGINT[] NOT NULL DEFAULT '{}',
    company_ids          BIGINT[] NOT NULL DEFAULT '{}',
    payload_hash         BYTEA,
    created_at           TIMESTAMP DEFAULT now(),
    updated_at           TIMESTAMP,
    abstract_customer_id BIGINT NOT NULL
);
CREATE TABLE amo_lead (LIKE amo_customer INCLUDING DEFAULTS INCLUDING GENERATED);

INSERT INTO amo_customer (crm_id, abstract_customer_id, data)
SELECT DISTINCT ON (crm_id) crm_id, record_id, item
FROM (
    SELECT (e.item -> 'general' ->> 'id')::bigint AS crm_id, a.id AS record_id, e.item
    FROM abstract_customer a
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(a.amo_customers) = 'array' THEN a.amo_customers ELSE '[]'::jsonb END
    ) AS e(item)
) source
WHERE crm_id IS NOT NULL
ORDER BY crm_id, record_id;

INSERT INTO amo_lead (crm_id, abstract_customer_id, data)
SELECT DISTINCT ON (crm_id) crm_id, record_id, item
FROM (
    SELECT (e.item -> 'general' ->> 'id')::bigint AS crm_id, a.id AS record_id, e.item
    FROM abstract_customer a
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(a.amo_leads) = 'array' THEN a.amo_leads ELSE '[]'::jsonb END
    ) AS e(item)
) source
WHERE crm_id IS NOT NULL
ORDER BY crm_id, record_id;

ALTER TABLE amo_customer
    ADD CONSTRAINT pk_amo_customer PRIMARY KEY (crm_id),
    ADD CONSTRAINT fk_amo_customer_abstract_customer_id_abstract_customer
        FOREIGN KEY (abstract_customer_id) REFERENCES abstract_customer (id) ON DELETE CASCADE;
ALTER TABLE amo_lead
    ADD CONSTRAINT pk_amo_lead PRIMARY KEY (crm_id),
    ADD CONSTRAINT fk_amo_lead_abstract_customer_id_abstract_customer
        FOREIGN KEY (abstract_customer_id) REFERENCES abstract_customer (id) ON DELETE CASCADE;

CREATE INDEX ix_amo_customer_abstract_customer_id ON amo_customer (abstract_customer_id);
CREATE INDEX ix_amo_customer_login ON amo_customer (login);
CREATE INDEX ix_amo_lead_abstract_customer_id ON amo_lead (abstract_customer_id);
CREATE INDEX ix_amo_lead_login ON amo_lead (login);

COMMIT;
