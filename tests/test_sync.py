import logging
from pathlib import Path

import pytest
import requests
from sqlalchemy import select, text

import abstract_customer
from amocrm import AmoCrmError, Customer, Lead, amocrm_get
from conftest import customer, lead
from main import collect_leads_customers
from models import AbstractCustomer, AmoCustomer, AmoLead, db

MIGRATIONS = Path(__file__).resolve().parent.parent / 'migrations'


def record_of(model, crm_id):
    return db.session.scalar(select(model.abstract_customer_id).where(model.crm_id == crm_id))


def records_count():
    return db.session.query(AbstractCustomer).count()


def nested(entity_id, name='контакт'):
    return {'general': {'id': entity_id, 'name': name}, 'api_value': {}}


def set_json(record_id, **values):
    for attr, value in values.items():
        setattr(db.session.get(AbstractCustomer, record_id), attr, value)
    db.session.commit()


def get_json(record_id, attr):
    db.session.expire_all()
    return getattr(db.session.get(AbstractCustomer, record_id), attr)


def test_customer_without_login_is_skipped(crm):
    crm.customers = [customer(1, login='')]
    collect_leads_customers()
    assert db.session.query(AmoCustomer).count() == 0
    assert records_count() == 0


def test_customer_found_by_id_is_updated_in_place(crm):
    crm.customers = [customer(1, login='a', name='старое')]
    collect_leads_customers()
    record_id = record_of(AmoCustomer, 1)

    crm.customers = [customer(1, login='a', name='новое')]
    collect_leads_customers()
    assert record_of(AmoCustomer, 1) == record_id
    assert db.session.get(AmoCustomer, 1).name == 'новое'
    assert db.session.get(AmoCustomer, 1).data['general']['name'] == 'новое'


def test_customer_joins_record_of_saved_lead_with_same_login(crm):
    crm.leads = [lead(10, login='a')]
    collect_leads_customers(is_full=False)
    crm.leads = []
    crm.customers = [customer(1, login='a')]
    collect_leads_customers(is_full=False)
    assert record_of(AmoCustomer, 1) == record_of(AmoLead, 10)


def test_two_leads_in_one_record_are_not_ambiguous(crm):
    crm.customers = [customer(1, login='seed')]
    crm.leads = [lead(10, login='seed'), lead(11, login='seed')]
    collect_leads_customers()
    record_id = record_of(AmoCustomer, 1)
    db.session.execute(text("DELETE FROM amo_customer"))
    db.session.execute(text(
        "UPDATE amo_lead SET data = jsonb_set(data, '{general,login}', '\"a\"')"))
    db.session.commit()

    crm.customers = [customer(2, login='a')]
    crm.leads = []
    collect_leads_customers(is_full=False)
    assert record_of(AmoCustomer, 2) == record_id


def test_customer_ignores_login_found_in_several_records(crm, caplog):
    crm.leads = [lead(10, login='a'), lead(11, login='a')]
    collect_leads_customers(is_full=False)
    assert record_of(AmoLead, 10) != record_of(AmoLead, 11)

    crm.leads = []
    crm.customers = [customer(1, login='a')]
    with caplog.at_level(logging.WARNING):
        collect_leads_customers(is_full=False)
    assert record_of(AmoCustomer, 1) not in (record_of(AmoLead, 10), record_of(AmoLead, 11))
    assert 'more than one' in caplog.text


def test_lead_ignores_login_found_in_several_records(crm):
    crm.leads = [lead(10, login='a'), lead(11, login='a')]
    collect_leads_customers(is_full=False)
    crm.leads = []
    crm.customers = [customer(1, login='a'), customer(2, login='a')]
    collect_leads_customers(is_full=False)
    assert record_of(AmoCustomer, 1) != record_of(AmoCustomer, 2)

    crm.customers = []
    crm.leads = [lead(12, login='a')]
    collect_leads_customers(is_full=False)
    assert record_of(AmoLead, 12) not in (record_of(AmoCustomer, 1), record_of(AmoCustomer, 2))


def test_lead_joins_customer_created_in_same_run(crm):
    crm.customers = [customer(1, login='a')]
    crm.leads = [lead(10, login='a')]
    collect_leads_customers()
    assert record_of(AmoLead, 10) == record_of(AmoCustomer, 1)
    assert records_count() == 1


def test_lead_without_match_creates_new_record(crm):
    crm.customers = [customer(1, login='a')]
    crm.leads = [lead(10), lead(11, login='b')]
    collect_leads_customers()
    assert records_count() == 3


def test_lead_uses_login_changed_in_same_run(crm):
    crm.customers = [customer(1, login='old')]
    collect_leads_customers()

    crm.customers = [customer(1, login='new')]
    crm.leads = [lead(10, login='old'), lead(11, login='new')]
    collect_leads_customers()
    assert record_of(AmoLead, 10) != record_of(AmoCustomer, 1)
    assert record_of(AmoLead, 11) == record_of(AmoCustomer, 1)


def test_merge_keeps_record_found_by_lead(crm):
    crm.leads = [lead(10)]
    collect_leads_customers(is_full=False)
    lead_record = record_of(AmoLead, 10)
    crm.leads = []
    crm.customers = [customer(1, login='a', contacts=[100], companies=[200])]
    collect_leads_customers(is_full=False)
    customer_record = record_of(AmoCustomer, 1)
    assert lead_record != customer_record

    set_json(lead_record, amo_contacts=[nested(101)], send_methods=[{'type': 'sms', 'value': '1', 'desc': ''}])
    set_json(customer_record,
             amo_contacts=[nested(100)], amo_companies=[nested(200)],
             send_methods=[{'type': 'sms', 'value': '1', 'desc': ''}, {'type': 'mail', 'value': 'x', 'desc': ''}])

    crm.leads = [lead(10, login='a', name='обновлённая', contacts=[101])]
    collect_leads_customers()

    assert record_of(AmoCustomer, 1) == lead_record
    assert record_of(AmoLead, 10) == lead_record
    assert db.session.get(AbstractCustomer, customer_record) is None
    assert db.session.get(AmoLead, 10).name == 'обновлённая'
    assert {c['general']['id'] for c in get_json(lead_record, 'amo_contacts')} == {100, 101}
    assert [c['general']['id'] for c in get_json(lead_record, 'amo_companies')] == [200]
    assert sorted(m['type'] for m in get_json(lead_record, 'send_methods')) == ['mail', 'sms']


def test_lead_after_merge_sees_merged_record(crm):
    crm.leads = [lead(10), lead(11)]
    collect_leads_customers(is_full=False)
    crm.leads = []
    crm.customers = [customer(1, login='a'), customer(2, login='a2')]
    collect_leads_customers(is_full=False)

    # 11 забирает запись покупателя 2, затем 10 — запись с покупателем 1,
    # а затем цепочка: запись сделки 11 сливается в запись сделки 10.
    crm.customers = []
    crm.leads = [lead(11, login='a2'), lead(10, login='a'), lead(10, login='a2')]
    collect_leads_customers(is_full=False)
    record_id = record_of(AmoLead, 10)
    assert {record_of(AmoLead, 11), record_of(AmoCustomer, 1), record_of(AmoCustomer, 2)} == {record_id}
    assert records_count() == 1


def test_full_pass_removes_absent_entities(crm):
    crm.customers = [customer(1, login='a'), customer(2, login='b'), customer(3, login='c')]
    crm.leads = [lead(10, login='a'), lead(11, login='a'), lead(12, login='b')]
    collect_leads_customers()
    gone_record = record_of(AmoCustomer, 2)

    crm.customers = [customer(1, login='a'), customer(3, login='c')]
    crm.leads = [lead(10, login='a')]
    collect_leads_customers()
    assert db.session.get(AmoCustomer, 2) is None
    assert db.session.get(AmoLead, 11) is None
    assert db.session.get(AmoLead, 12) is None
    assert db.session.get(AbstractCustomer, gone_record) is None
    assert db.session.get(AmoLead, 10) is not None


@pytest.mark.parametrize('name', ['удалить', 'УДАЛИТЬ клиента', 'Не Удалить'])
def test_full_pass_removes_marked_entities_present_in_export(crm, name):
    crm.customers = [customer(1, login='a'), customer(2, login='b', name=name)]
    crm.leads = [lead(10, login='a'), lead(11, login='a', name=name)]
    collect_leads_customers()
    assert db.session.get(AmoCustomer, 2) is None
    assert db.session.get(AmoLead, 11) is None
    assert db.session.get(AmoCustomer, 1) is not None
    assert db.session.get(AmoLead, 10) is not None
    assert records_count() == 1


def test_nameless_lead_does_not_break_cleanup(crm):
    crm.customers = [customer(1, login='a', contacts=[100])]
    crm.leads = [lead(10, login='a', name=None)]
    collect_leads_customers()
    record_id = record_of(AmoCustomer, 1)
    set_json(record_id, amo_contacts=[nested(100), nested(101)])
    collect_leads_customers()
    assert db.session.get(AmoLead, 10) is not None
    assert [c['general']['id'] for c in get_json(record_id, 'amo_contacts')] == [100]


def test_nested_entities_are_filtered_per_entity(crm):
    crm.customers = [customer(1, login='a', contacts=[100, 101], companies=[200])]
    crm.leads = [lead(10, login='a', contacts=[102], companies=[201])]
    collect_leads_customers()
    record_id = record_of(AmoCustomer, 1)
    set_json(record_id,
             amo_contacts=[nested(100), nested(101, 'УДАЛИТЬ'), nested(102), nested(103), nested(None, None)],
             amo_companies=[nested(200), nested(201), nested(202)],
             send_methods=[{'type': 'sms', 'value': '1', 'desc': 'удалить'}])

    collect_leads_customers()
    assert [c['general']['id'] for c in get_json(record_id, 'amo_contacts')] == [100, 102]
    assert [c['general']['id'] for c in get_json(record_id, 'amo_companies')] == [200, 201]
    assert len(get_json(record_id, 'send_methods')) == 1


def test_contact_moved_to_other_record_leaves_old_one(crm):
    crm.customers = [customer(1, login='a', contacts=[100]), customer(2, login='b')]
    collect_leads_customers()
    first = record_of(AmoCustomer, 1)
    set_json(first, amo_contacts=[nested(100)])

    crm.customers = [customer(1, login='a'), customer(2, login='b', contacts=[100])]
    collect_leads_customers()
    assert get_json(first, 'amo_contacts') == []


def test_customer_without_login_in_export_is_not_deleted(crm):
    crm.customers = [customer(1, login='a', contacts=[100])]
    collect_leads_customers()
    record_id = record_of(AmoCustomer, 1)
    set_json(record_id, amo_contacts=[nested(100)])

    crm.customers = [customer(1, contacts=[100]), customer(2, login='b')]
    collect_leads_customers()
    assert db.session.get(AmoCustomer, 1).login == 'a'
    assert len(get_json(record_id, 'amo_contacts')) == 1


@pytest.mark.parametrize('broken', ['customers', 'leads'])
def test_interrupted_export_skips_cleanup(crm, broken):
    crm.customers = [customer(1, login='a'), customer(2, login='b')]
    crm.leads = [lead(10, login='a'), lead(11, login='b')]
    collect_leads_customers()

    crm.customers = [customer(1, login='a')]
    crm.leads = [lead(10, login='a')]
    getattr(crm, broken).append(AmoCrmError('сбой', 500))
    with pytest.raises(AmoCrmError):
        collect_leads_customers()
    assert db.session.get(AmoCustomer, 2) is not None
    assert db.session.get(AmoLead, 11) is not None


def test_failed_entity_skips_cleanup_but_others_are_saved(crm, monkeypatch):
    crm.customers = [customer(1, login='a'), customer(2, login='b')]
    crm.leads = [lead(10, login='a')]
    collect_leads_customers()

    real = abstract_customer.get_lead_json

    def failing(entity):
        if entity.id == 11:
            raise ValueError('битая сделка')
        return real(entity)

    monkeypatch.setattr(abstract_customer, 'get_lead_json', failing)
    crm.customers = [customer(1, login='a')]
    crm.leads = [lead(11, login='a'), lead(12, login='a')]
    collect_leads_customers()
    assert db.session.get(AmoCustomer, 2) is not None
    assert db.session.get(AmoLead, 10) is not None
    assert db.session.get(AmoLead, 12) is not None


def test_empty_export_does_not_wipe_table(crm):
    crm.customers = [customer(1, login='a')]
    crm.leads = [lead(10, login='a')]
    collect_leads_customers()

    crm.customers = []
    collect_leads_customers()
    assert db.session.get(AmoCustomer, 1) is not None


def test_partial_pass_filters_statuses_and_keeps_everything_else(crm):
    crm.customers = [customer(1, login='a'), customer(2, login='b')]
    crm.leads = [lead(10, login='a'), lead(11, login='b')]
    collect_leads_customers()

    crm.customers = [customer(1, login='a', name='новое', status_id=111349)]
    crm.leads = [lead(10, login='a', name='новое', status_id=143)]
    collect_leads_customers(is_full=False)
    assert db.session.get(AmoCustomer, 1).name != 'новое'
    assert db.session.get(AmoLead, 10).name != 'новое'
    assert db.session.get(AmoCustomer, 2) is not None
    assert db.session.get(AmoLead, 11) is not None


def test_partial_pass_does_not_delete_absent_entities(crm):
    crm.customers = [customer(1, login='a'), customer(2, login='b')]
    crm.leads = [lead(10, login='a'), lead(11, login='b')]
    collect_leads_customers()

    crm.customers = [customer(1, login='a')]
    crm.leads = [lead(10, login='a')]
    collect_leads_customers(is_full=False)
    assert db.session.get(AmoCustomer, 2) is not None
    assert db.session.get(AmoLead, 11) is not None


def test_custom_field_false_value_is_false(crm):
    crm.customers = [customer(1, login='a', robot=False, nds=True)]
    collect_leads_customers()
    general = db.session.get(AmoCustomer, 1).data['general']
    assert general['is_robot'] is False
    assert general['is_nds'] is True


def test_batches_do_not_change_result(crm, monkeypatch):
    monkeypatch.setattr(abstract_customer, 'BATCH_SIZE', 2)
    monkeypatch.setattr(abstract_customer.CustomerSync.__init__, '__defaults__', (2,))
    crm.customers = [customer(i, login=f'l{i}') for i in range(1, 8)]
    crm.leads = [lead(100 + i, login=f'l{i}') for i in range(1, 8)]
    collect_leads_customers()
    assert records_count() == 7
    for i in range(1, 8):
        assert record_of(AmoLead, 100 + i) == record_of(AmoCustomer, i)


def test_unchanged_night_writes_nothing(crm, statements):
    crm.customers = [customer(i, login=f'l{i}', contacts=[i]) for i in range(1, 51)]
    crm.leads = [lead(100 + i, login=f'l{i}') for i in range(1, 51)]
    collect_leads_customers()

    statements.clear()
    collect_leads_customers()
    writes = [s for s in statements if s.lstrip().startswith('INSERT')]
    assert writes == []
    assert len(statements) <= 7  # 2 чтения индекса и 5 запросов чистки


def test_queries_do_not_grow_per_entity(crm, statements):
    crm.customers = [customer(i, login=f'l{i}') for i in range(1, 1201)]
    crm.leads = [lead(10000 + i, login=f'l{i}') for i in range(1, 1201)]
    collect_leads_customers()
    assert len(statements) < 30


def test_exposes_same_json_format(crm):
    crm.customers = [customer(1, login='a')]
    crm.leads = [lead(10, login='a')]
    collect_leads_customers()
    record = db.session.get(AbstractCustomer, record_of(AmoCustomer, 1))
    assert record.amo_customers[0]['general']['login'] == 'a'
    assert record.amo_leads[0]['general']['id'] == 10


def test_migration_moves_json_to_rows(app, crm):
    db.drop_all()
    db.session.execute(text("""
        CREATE TABLE abstract_customer (
            id BIGSERIAL PRIMARY KEY, amo_customers JSONB, amo_leads JSONB, amo_companies JSONB,
            amo_contacts JSONB, send_methods JSONB, created_at TIMESTAMP DEFAULT now(), updated_at TIMESTAMP)
    """))
    db.session.execute(text("""
        INSERT INTO abstract_customer (amo_customers, amo_leads, amo_contacts) VALUES
        ('[{"general": {"id": 1, "login": "a", "name": "x"}, "api_value": {}}]',
         '[{"general": {"id": 10, "login": "a", "name": "s"}, "api_value": {}},
           {"general": {"id": 11, "login": "a", "name": "s"}, "api_value": {}}]',
         '[{"general": {"id": 100, "name": "c"}, "api_value": {}}]'),
        ('[{"general": {"id": 1, "login": "a", "name": "дубль"}, "api_value": {}}]', 'null', NULL),
        (NULL, NULL, NULL)
    """))
    db.session.commit()
    for script in ('001_split_customers_and_leads.sql', '003_drop_legacy_json.sql'):
        connection = db.engine.raw_connection()
        connection.cursor().execute((MIGRATIONS / script).read_text(encoding='utf-8'))
        connection.commit()
        connection.close()

    assert db.session.get(AmoCustomer, 1).name == 'x'
    assert db.session.query(AmoLead).filter_by(login='a').count() == 2

    crm.customers = [customer(1, login='a', contacts=[100])]
    crm.leads = [lead(10, login='a')]
    collect_leads_customers()
    db.session.expire_all()
    assert records_count() == 1
    assert db.session.get(AmoCustomer, 1).payload_hash is not None
    assert db.session.get(AmoLead, 11) is None
    assert len(get_json(record_of(AmoCustomer, 1), 'amo_contacts')) == 1


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        return self.payload


def page(items, next_url=None):
    links = {'next': {'href': next_url}} if next_url else {}
    return FakeResponse(200, {'_embedded': {'customers': items}, '_links': links})


def run_client(monkeypatch, responses):
    calls = []

    def get(url, **_):
        calls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(requests, 'get', get)
    monkeypatch.setattr('time.sleep', lambda _: None)
    return list(amocrm_get(Customer)), calls


def test_client_stops_when_full_page_has_no_next_link(monkeypatch):
    items = [{'id': i, 'custom_fields_values': None} for i in range(250)]
    loaded, calls = run_client(monkeypatch, [page(items)])
    assert len(loaded) == 250 and len(calls) == 1
    assert loaded[0].get_field('Логин') is None


def test_client_follows_next_link_after_short_page(monkeypatch):
    loaded, _ = run_client(monkeypatch, [page([{'id': 1}], 'next-url'), page([{'id': 2}])])
    assert [entity.id for entity in loaded] == [1, 2]


def test_client_raises_on_error_mid_export(monkeypatch):
    with pytest.raises(AmoCrmError):
        run_client(monkeypatch, [page([{'id': 1}], 'next-url'), FakeResponse(500, {})])


def test_lead_reads_embedded_contacts_and_companies():
    loaded = Lead.from_api({'id': 1, '_embedded': {'contacts': [{'id': 5}], 'companies': [{'id': 6}]}})
    assert loaded.contacts == [{'id': 5}]
    assert loaded.companies == [{'id': 6}]
