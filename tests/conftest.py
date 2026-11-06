import os
import sys
from pathlib import Path

import pytest
from flask import Flask
from sqlalchemy import event, text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
from amocrm import Customer, Lead  # noqa: E402
from models import db  # noqa: E402

DATABASE_URL = os.environ.get('TEST_DATABASE_URL')


def drop_everything():
    db.session.rollback()
    db.session.execute(text("DROP TABLE IF EXISTS amo_lead, amo_customer, abstract_customer CASCADE"))
    db.session.commit()


@pytest.fixture
def app():
    if not DATABASE_URL:
        pytest.skip('TEST_DATABASE_URL не задан')
    flask_app = Flask(__name__)
    flask_app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
    db.init_app(flask_app)
    with flask_app.app_context():
        drop_everything()
        db.create_all()
        yield flask_app
        drop_everything()


@pytest.fixture
def statements(app):
    """Счётчик запросов к Postgres (executemany считается одним запросом)."""
    executed = []

    def remember(conn, cursor, statement, parameters, context, executemany):
        executed.append(statement)

    event.listen(db.engine, 'before_cursor_execute', remember)
    yield executed
    event.remove(db.engine, 'before_cursor_execute', remember)


class Crm:
    """Подменяет amocrm_get. Значение может быть исключением — так обрывается выгрузка."""

    def __init__(self):
        self.customers = []
        self.leads = []

    def __call__(self, entity_cls, **_):
        for item in (self.customers if entity_cls is Customer else self.leads):
            if isinstance(item, Exception):
                raise item
            yield item


@pytest.fixture
def crm(app, monkeypatch):
    fake = Crm()
    monkeypatch.setattr(main, 'amocrm_get', fake)
    return fake


def fields(**values):
    names = {'login': 'Логин', 'robot': 'Робот', 'nds': 'Оплата с НДС'}
    return [{'field_id': i, 'field_name': names[key], 'values': [{'value': value}]}
            for i, (key, value) in enumerate(values.items(), 1)]


def customer(id, login='', name='покупатель', contacts=(), companies=(), status_id=1, **extra):
    custom = fields(login=login, **extra) if login else None
    return Customer.from_api({
        'id': id, 'name': name, 'status_id': status_id, 'custom_fields_values': custom,
        '_embedded': {'contacts': [{'id': i} for i in contacts],
                      'companies': [{'id': i} for i in companies]},
    })


def lead(id, login='', name='сделка', contacts=(), companies=(), status_id=1):
    return Lead.from_api({
        'id': id, 'name': name, 'status_id': status_id,
        'custom_fields_values': fields(login=login) if login else None,
        '_embedded': {'contacts': [{'id': i} for i in contacts],
                      'companies': [{'id': i} for i in companies]},
    })
