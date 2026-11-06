import hashlib
import json
import logging
from collections import defaultdict
from typing import Dict, List, NamedTuple, Optional, Set, Type, Union

from sqlalchemy import bindparam, func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from amocrm import Customer, Lead
from models import AbstractCustomer, AmoCustomer, AmoLead

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
RECORD_ID_BLOCK = 1000

DELETE_MARK = 'удалить'
# В C-локали БД lower() и ILIKE не знают кириллицу, поэтому регистр перечислен явно.
DELETE_MARK_REGEX = ''.join(f'[{char}{char.upper()}]' for char in DELETE_MARK)

MERGED_JSON_ATTRS = ('amo_contacts', 'amo_companies', 'send_methods')

# Вложенная сущность остаётся, только если на неё ссылается покупатель или сделка этой же записи.
FILTER_NESTED_SQL = """
WITH linked AS (
    SELECT abstract_customer_id, unnest({ids}) AS crm_id FROM amo_customer
    UNION
    SELECT abstract_customer_id, unnest({ids}) FROM amo_lead
), checked AS (
    SELECT a.id, e.item, e.ord,
           l.crm_id IS NOT NULL
           AND coalesce(e.item -> 'general' ->> 'name', '') !~ :mark AS keep
    FROM abstract_customer a
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(a.{attr}) = 'array' THEN a.{attr} ELSE '[]'::jsonb END
    ) WITH ORDINALITY AS e(item, ord)
    LEFT JOIN linked l
        ON l.abstract_customer_id = a.id AND l.crm_id::text = e.item -> 'general' ->> 'id'
), filtered AS (
    SELECT id, coalesce(jsonb_agg(item ORDER BY ord) FILTER (WHERE keep), '[]'::jsonb) AS items
    FROM checked
    GROUP BY id
    HAVING NOT bool_and(keep)
)
UPDATE abstract_customer a
SET {attr} = f.items, updated_at = now()
FROM filtered f
WHERE a.id = f.id
"""


def dict_compare_merge(l_dict1, l_dict2):
    d = defaultdict(dict)
    for i in l_dict1 + l_dict2:
        d[i['general']['id']].update(i)

    return list(d.values())


def send_methods_merge(methods1: List[Dict], methods2: List[Dict]) -> List[Dict]:
    """У способов связи нет id, одинаковым считается тот же тип и значение."""
    merged = {(m.get('type'), m.get('value')): m for m in methods2}
    merged.update({(m.get('type'), m.get('value')): m for m in methods1})
    return list(merged.values())


def get_customer_json(customer: Customer) -> dict:
    return {
        'general': {
            'id': customer.id,
            'login': customer.get_field('Логин'),
            'billing_type': customer.get_field('Вид оплаты'),
            'is_robot': bool(customer.get_field('Робот')),
            'is_nds': bool(customer.get_field('Оплата с НДС')),
            'system': customer.get_field('Система'),
            'host': customer.get_field('Хост'),
            'name': customer.name,
        },
        'api_value': customer.as_dict()
    }


def get_lead_json(lead: Lead) -> dict:
    return {
        'general': {
            'id': lead.id,
            'login': lead.get_field('Логин'),
            'name': lead.name,
        },
        'api_value': lead.as_dict()
    }


def get_login(entity: Union[Customer, Lead]) -> Optional[str]:
    login = entity.get_field('Логин')
    return str(login) if login else None


def has_delete_mark(entity: Union[Customer, Lead]) -> bool:
    return DELETE_MARK in (entity.name or '').lower()


class KnownEntity(NamedTuple):
    record_id: int
    login: Optional[str]
    payload_hash: Optional[bytes]


class EntityIndex:
    """Лёгкая проекция таблицы: без JSON, только то, что нужно для поиска и сравнения."""

    def __init__(self):
        self.by_id: Dict[int, KnownEntity] = {}
        # login -> {record_id: сколько сущностей записи имеют этот логин}
        self.by_login: Dict[str, Dict[int, int]] = {}
        self.seen: Set[int] = set()
        self.marked: Set[int] = set()

    def put(self, crm_id: int, entity: KnownEntity):
        previous = self.by_id.get(crm_id)
        if previous is not None and previous.login:
            records = self.by_login[previous.login]
            records[previous.record_id] -= 1
            if not records[previous.record_id]:
                del records[previous.record_id]
            if not records:
                del self.by_login[previous.login]
        self.by_id[crm_id] = entity
        if entity.login:
            records = self.by_login.setdefault(entity.login, {})
            records[entity.record_id] = records.get(entity.record_id, 0) + 1

    def outdated_ids(self) -> List[int]:
        """Только то, что было известно на старте: чужие вставки во время прогона не трогаем."""
        return list((self.by_id.keys() - self.seen) | self.marked)


class CustomerSync:
    """Решения принимаются в памяти по индексам, в Postgres уходят только изменения пачками.

    Индексы обновляются сразу, так что следующая сущность видит результат предыдущей,
    даже если та ещё не записана. Каждая пачка — одна транзакция: прерванный прогон
    оставляет согласованные данные и безопасно повторяется.
    """

    def __init__(self, session, batch_size: int = BATCH_SIZE):
        self.session = session
        self.batch_size = batch_size
        self.customers = self._load_index(AmoCustomer)
        self.leads = self._load_index(AmoLead)
        self.session.commit()

        self.merged_into: Dict[int, int] = {}
        self.merged_since_flush: List[int] = []
        self.new_records: Set[int] = set()
        self.free_record_ids: List[int] = []
        self.pending_customers: Dict[int, Dict] = {}
        self.pending_leads: Dict[int, Dict] = {}

    def _load_index(self, model: Type) -> EntityIndex:
        index = EntityIndex()
        rows = self.session.execute(
            select(model.crm_id, model.abstract_customer_id, model.login, model.payload_hash))
        for crm_id, record_id, login, payload_hash in rows:
            index.put(crm_id, KnownEntity(record_id, login, payload_hash))
        return index

    def save_customer(self, customer: Customer):
        self._mark_seen(self.customers, customer)
        login = get_login(customer)
        if not login:
            return
        payload = get_customer_json(customer)

        record_id = self._record_by_id(self.customers, customer.id)
        if record_id is None:
            record_id = self._record_by_login(self.leads, login)  # почти всегда логины совпадают
        if record_id is None:
            record_id = self._new_record_id()
        self._stage(self.customers, self.pending_customers, customer, payload, record_id, login)

    def save_lead(self, lead: Lead):
        self._mark_seen(self.leads, lead)
        login = get_login(lead)
        payload = get_lead_json(lead)

        by_id = self._record_by_id(self.leads, lead.id)
        by_login = self._record_by_login(self.customers, login) if login else None
        record_id = by_login if by_id is None else by_id
        if record_id is None:
            record_id = self._new_record_id()
        if by_id is not None and by_login is not None and by_id != by_login:
            self.merged_into[by_login] = by_id
            self.merged_since_flush.append(by_login)
        self._stage(self.leads, self.pending_leads, lead, payload, record_id, login)

    @staticmethod
    def _mark_seen(index: EntityIndex, entity: Union[Customer, Lead]):
        index.seen.add(entity.id)
        if has_delete_mark(entity):
            index.marked.add(entity.id)

    def _resolve(self, record_id: int) -> int:
        while record_id in self.merged_into:
            record_id = self.merged_into[record_id]
        return record_id

    def _record_by_id(self, index: EntityIndex, crm_id: int) -> Optional[int]:
        known = index.by_id.get(crm_id)
        return None if known is None else self._resolve(known.record_id)

    def _record_by_login(self, index: EntityIndex, login: str) -> Optional[int]:
        records = {self._resolve(record_id) for record_id in index.by_login.get(login, ())}
        if len(records) > 1:
            logger.warning(f"Login {login} has more than one abccustomer")
            return None
        return records.pop() if records else None

    def _new_record_id(self) -> int:
        """id из последовательности берётся заранее: запись должна быть видна до вставки."""
        if not self.free_record_ids:
            self.free_record_ids = list(self.session.scalars(
                text("SELECT nextval(pg_get_serial_sequence('abstract_customer', 'id')) "
                     "FROM generate_series(1, :count)"),
                {'count': RECORD_ID_BLOCK}))
            self.session.commit()
        record_id = self.free_record_ids.pop()
        self.new_records.add(record_id)
        return record_id

    def _stage(self, index: EntityIndex, pending: Dict[int, Dict], entity: Union[Customer, Lead],
               payload: Dict, record_id: int, login: Optional[str]):
        contact_ids = sorted({contact['id'] for contact in entity.contacts})
        company_ids = sorted({company['id'] for company in entity.companies})
        serialized = json.dumps([payload, contact_ids, company_ids], sort_keys=True, ensure_ascii=False)
        payload_hash = hashlib.blake2b(serialized.encode(), digest_size=16).digest()

        known = index.by_id.get(entity.id)
        if known is not None and known.payload_hash == payload_hash \
                and self._resolve(known.record_id) == record_id:
            return
        index.put(entity.id, KnownEntity(record_id, login, payload_hash))
        pending[entity.id] = {
            'crm_id': entity.id,
            'abstract_customer_id': record_id,
            'data': payload,
            'contact_ids': contact_ids,
            'company_ids': company_ids,
            'payload_hash': payload_hash,
        }

    def flush_if_full(self):
        if len(self.pending_customers) + len(self.pending_leads) >= self.batch_size:
            self.flush()

    def flush(self):
        try:
            self._insert_new_records()
            self._upsert(AmoCustomer, self.pending_customers)
            self._upsert(AmoLead, self.pending_leads)
            self._apply_merges()
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        self.new_records.clear()
        self.merged_since_flush.clear()
        self.pending_customers.clear()
        self.pending_leads.clear()

    def _insert_new_records(self):
        record_ids = self.new_records - self.merged_into.keys()
        if record_ids:
            self.session.execute(insert(AbstractCustomer).values([{'id': i} for i in record_ids]))

    def _upsert(self, model: Type, pending: Dict[int, Dict]):
        if not pending:
            return
        for row in pending.values():
            row['abstract_customer_id'] = self._resolve(row['abstract_customer_id'])
        stmt = insert(model).values(list(pending.values()))
        self.session.execute(stmt.on_conflict_do_update(
            index_elements=[model.crm_id],
            set_={
                'abstract_customer_id': stmt.excluded.abstract_customer_id,
                'data': stmt.excluded.data,
                'contact_ids': stmt.excluded.contact_ids,
                'company_ids': stmt.excluded.company_ids,
                'payload_hash': stmt.excluded.payload_hash,
                'updated_at': func.now(),
            }))

    def _apply_merges(self):
        if not self.merged_since_flush:
            return
        losers = list(self.merged_since_flush)
        winners = [self._resolve(loser) for loser in losers]
        self._merge_json_fields(losers, winners)
        for table in ('amo_customer', 'amo_lead'):
            self.session.execute(
                text(f"""
                    UPDATE {table} t
                    SET abstract_customer_id = m.winner, updated_at = now()
                    FROM unnest(CAST(:losers AS bigint[]), CAST(:winners AS bigint[])) AS m(loser, winner)
                    WHERE t.abstract_customer_id = m.loser
                """),
                {'losers': losers, 'winners': winners})
        self.session.execute(text("DELETE FROM abstract_customer WHERE id = ANY(CAST(:ids AS bigint[]))"),
                             {'ids': losers})

    def _merge_json_fields(self, losers: List[int], winners: List[int]):
        records = {
            row.id: dict(row._mapping)
            for row in self.session.execute(
                select(AbstractCustomer.id, *[getattr(AbstractCustomer, a) for a in MERGED_JSON_ATTRS])
                .where(AbstractCustomer.id.in_(losers + winners)))
        }
        merged = {}
        for loser, winner in zip(losers, winners):
            if loser not in records:  # запись создана и слита в одной пачке, JSON у неё ещё нет
                continue
            source, target = records[loser], records[winner]
            target['amo_contacts'] = dict_compare_merge(target['amo_contacts'] or [],
                                                        source['amo_contacts'] or [])
            target['amo_companies'] = dict_compare_merge(target['amo_companies'] or [],
                                                         source['amo_companies'] or [])
            target['send_methods'] = send_methods_merge(target['send_methods'] or [],
                                                        source['send_methods'] or [])
            merged[winner] = target
        if not merged:
            return
        table = AbstractCustomer.__table__
        self.session.execute(
            update(table)
            .where(table.c.id == bindparam('record_id'))
            .values({attr: bindparam(attr) for attr in MERGED_JSON_ATTRS}),
            [{'record_id': record_id, **{a: record[a] for a in MERGED_JSON_ATTRS}}
             for record_id, record in merged.items()])

    def has_empty_export(self) -> bool:
        """Пустой ответ при непустой таблице — сбой выгрузки, а не удаление всех клиентов из CRM."""
        return any(index.by_id and not index.seen for index in (self.customers, self.leads))

    def cleanup(self):
        """Удаляет то, чего нет в выгрузке. Одна транзакция, Python не читает JSON из таблицы."""
        try:
            for model, index in ((AmoCustomer, self.customers), (AmoLead, self.leads)):
                self.session.execute(
                    text(f"DELETE FROM {model.__tablename__} WHERE crm_id = ANY(CAST(:ids AS bigint[]))"),
                    {'ids': index.outdated_ids()})
            for attr, ids_column in (('amo_contacts', 'contact_ids'), ('amo_companies', 'company_ids')):
                self.session.execute(text(FILTER_NESTED_SQL.format(attr=attr, ids=ids_column)),
                                     {'mark': DELETE_MARK_REGEX})
            self.session.execute(text("""
                DELETE FROM abstract_customer a
                WHERE NOT EXISTS (SELECT 1 FROM amo_customer c WHERE c.abstract_customer_id = a.id)
                  AND NOT EXISTS (SELECT 1 FROM amo_lead l WHERE l.abstract_customer_id = a.id)
            """))
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
