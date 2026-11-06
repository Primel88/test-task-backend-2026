import logging
from typing import Callable, Iterable

from abstract_customer import CustomerSync
from amocrm import Customer, COMPANIES, CONTACTS, Lead, amocrm_get
from models import db

logger = logging.getLogger(__name__)

SKIPPED_CUSTOMER_STATUS = 111349
SKIPPED_LEAD_STATUS = 143


def save_all(entities: Iterable, save: Callable, sync: CustomerSync) -> int:
    """Возвращает число сбоев. Ошибка выгрузки или записи в БД не ловится и прерывает прогон."""
    failed = 0
    for entity in entities:
        try:
            save(entity)
        except Exception as e:
            failed += 1
            logger.exception(f'{entity} {e}')
        sync.flush_if_full()
    sync.flush()
    return failed


def collect_leads_customers(is_full=True):
    """
    Сначала покупатели, потом сделки: сделка ищет запись по логину среди уже обновлённых покупателей.
    Чистка запускается только после полного прохода без единого сбоя.
    Правила поиска и слияния — в CustomerSync.
    """
    sync = CustomerSync(db.session)

    customers = amocrm_get(Customer, _with=f"{COMPANIES},{CONTACTS}")
    failed = save_all((c for c in customers if is_full or c.status_id != SKIPPED_CUSTOMER_STATUS),
                      sync.save_customer, sync)
    logger.info(f"Collected customers {is_full=}")

    leads = amocrm_get(Lead, _with=f"{CONTACTS}")
    failed += save_all((l for l in leads if is_full or l.status_id != SKIPPED_LEAD_STATUS),
                       sync.save_lead, sync)
    logger.info(f"Collected leads {is_full=}")

    if not is_full:
        return
    if failed:
        logger.error(f"Чистка пропущена: не обработано сущностей: {failed}")
        return
    if sync.has_empty_export():
        logger.error("Чистка пропущена: CRM вернула пустую выгрузку при непустой таблице")
        return
    logger.info("Чистка покупателей")
    sync.cleanup()
