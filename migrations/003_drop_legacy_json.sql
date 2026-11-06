-- Выполнять отдельно, когда проверен первый прогон: до этого шага старые данные на месте.
ALTER TABLE abstract_customer
    DROP COLUMN amo_customers,
    DROP COLUMN amo_leads;
