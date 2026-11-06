from enum import Enum

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Column, Computed, ForeignKey, func, MetaData
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import declared_attr

naming_convention = {
    "ix": 'ix_%(column_0_label)s',
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s"
}
db = SQLAlchemy(metadata=MetaData(naming_convention=naming_convention))


class AbcCustomerEntities(Enum):
    AmoCustomer = 'amo_customers'
    AmoLead = 'amo_leads'
    AmoCompany = 'amo_companies'
    AmoContact = 'amo_contacts'
    SendMethods = 'send_methods'


class AbstractCustomer(db.Model):
    """
    Запись объединяет покупателей и сделки одного клиента. Связь сделка-покупатель —
    общий abstract_customer_id в строках AmoCustomer и AmoLead.

    "default" "general" : {
        "id": int
    }

    "amo_customers" (свойство, строки AmoCustomer.data): [
        {
            "general": {
                "login": str,
                "billing_type": str,
                "is_robot": bool,
                "is_nds": bool,
                "system": str,
                "host": str,
                "name": str
            },
            "api_value": {}
        }
    ]

    "amo_leads" (свойство, строки AmoLead.data): [
        {
            "general": {
                "login": Optional[str],
                "name": str
            }
            "api_value": {}
        }
    ]

    "amo_companies": [
        {
            "general": {
                "id": int,
                "inn": Optional[str],
                "name": str
            }
            "api_value": {}
        }
    ]

    "amo_contacts": [
        {
            "general": {
                "id": int,
                "phone": Optional[str],
                "name": str
            }
            "api_value": {}
        }
    ]

    "send_methods": [
        {
            "type": str (mail,sms,wtsp),
            "value": str,
            "desc": str,
        }
    ]
    """
    __tablename__ = "abstract_customer"
    id = Column(db.BigInteger(), nullable=False, primary_key=True)
    amo_companies = Column(JSONB)
    amo_contacts = Column(JSONB)
    send_methods = Column(JSONB)
    created_at = Column(db.DateTime, server_default=func.now())
    updated_at = Column(db.DateTime, onupdate=func.now())

    customers = db.relationship('AmoCustomer', viewonly=True, order_by='AmoCustomer.crm_id')
    leads = db.relationship('AmoLead', viewonly=True, order_by='AmoLead.crm_id')

    @property
    def amo_customers(self):
        return [customer.data for customer in self.customers]

    @property
    def amo_leads(self):
        return [lead.data for lead in self.leads]


class AmoEntityMixin:
    """Строка покупателя или сделки: один элемент бывшего JSON-массива.

    login и name вычисляются из data, поэтому не могут разойтись с JSON.
    contact_ids и company_ids — вложенные сущности из выгрузки, по ним чистятся
    amo_contacts и amo_companies. payload_hash позволяет не писать неизменившееся.
    Индекс по name (pg_trgm) есть только в миграции 002: расширение нужно не везде.
    """
    crm_id = Column(db.BigInteger(), primary_key=True, autoincrement=False)
    data = Column(JSONB, nullable=False)
    login = Column(db.Text, Computed("data -> 'general' ->> 'login'", persisted=True), index=True)
    name = Column(db.Text, Computed("data -> 'general' ->> 'name'", persisted=True))
    contact_ids = Column(ARRAY(db.BigInteger()), nullable=False, server_default='{}')
    company_ids = Column(ARRAY(db.BigInteger()), nullable=False, server_default='{}')
    payload_hash = Column(db.LargeBinary)
    created_at = Column(db.DateTime, server_default=func.now())
    updated_at = Column(db.DateTime, onupdate=func.now())

    @declared_attr
    def abstract_customer_id(cls):
        return Column(db.BigInteger(),
                      ForeignKey('abstract_customer.id', ondelete='CASCADE'),
                      nullable=False, index=True)


class AmoCustomer(AmoEntityMixin, db.Model):
    __tablename__ = "amo_customer"


class AmoLead(AmoEntityMixin, db.Model):
    __tablename__ = "amo_lead"
