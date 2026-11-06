"""Замер на синтетической выгрузке 20 тыс. покупателей и 50 тыс. сделок.

python tests/bench_sync.py <database_url> first|unchanged|changed
С переменной BENCH_TRACE печатается ещё пик Python-кучи.
"""
import os
import random
import sys
import time
import tracemalloc
from pathlib import Path

from flask import Flask
from sqlalchemy import event, func, select, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
from amocrm import Customer  # noqa: E402
from models import AmoCustomer, AmoLead, db  # noqa: E402

CUSTOMERS = 20_000
LEADS = 50_000
PAGE = 250
TRACE = 'BENCH_TRACE' in os.environ  # tracemalloc в несколько раз замедляет прогон
FILLER = 'x' * 2500  # api_value около 3 КБ


def custom_fields(login):
    return [{'field_id': 1, 'field_name': 'Логин', 'values': [{'value': login}]},
            {'field_id': 2, 'field_name': 'Заметка', 'values': [{'value': FILLER}]}]


def fake_get(scenario):
    rng = random.Random(1)
    changed = 0.05 if scenario == 'changed' else 0
    absent = 0.01 if scenario == 'changed' else 0

    def get(entity_cls, **_):
        total = CUSTOMERS if entity_cls is Customer else LEADS
        for start in range(1, total + 1, PAGE):
            page = []  # как страница клиента: 250 объектов живут одновременно
            for i in range(start, min(start + PAGE, total + 1)):
                if rng.random() < absent:
                    continue
                name = f'{entity_cls.__name__} {i}' + (' новое' if rng.random() < changed else '')
                page.append(entity_cls.from_api({
                    'id': i,
                    'name': name,
                    'status_id': 1,
                    'custom_fields_values': custom_fields(f'user{i % CUSTOMERS}'),
                    '_embedded': {'contacts': [{'id': i}], 'companies': [{'id': i}]},
                }))
            yield from page

    return get


def peak_rss_mb():
    try:
        import resource
    except ImportError:  # Windows
        import psutil
        return psutil.Process().memory_info().peak_wset / 2 ** 20
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2 ** 10


def run(url, scenario):
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = url
    db.init_app(app)
    with app.app_context():
        if scenario == 'first':
            db.session.execute(text("DROP TABLE IF EXISTS amo_lead, amo_customer, abstract_customer CASCADE"))
            db.session.commit()
            db.create_all()

        counters = {'statements': 0, 'commits': 0}

        def on_statement(*_):
            counters['statements'] += 1

        def on_commit(*_):
            counters['commits'] += 1

        event.listen(db.engine, 'before_cursor_execute', on_statement)
        event.listen(db.engine, 'commit', on_commit)
        main.amocrm_get = fake_get(scenario)
        print(f'RSS до прогона {peak_rss_mb():.0f} МБ')

        if TRACE:
            tracemalloc.start()
        started = time.perf_counter()
        main.collect_leads_customers(is_full=True)
        elapsed = time.perf_counter() - started
        print(f'{scenario}: {elapsed:.1f} с, запросов {counters["statements"]}, commit {counters["commits"]}')
        if TRACE:
            print(f'пик Python-кучи {tracemalloc.get_traced_memory()[1] / 2 ** 20:.0f} МБ')
            tracemalloc.stop()
        print(f'пик RSS {peak_rss_mb():.0f} МБ')
        print('строк:', db.session.scalar(select(func.count()).select_from(AmoCustomer)),
              db.session.scalar(select(func.count()).select_from(AmoLead)))
        for query in ("SELECT * FROM amo_lead WHERE login = 'user5'",
                      "SELECT * FROM amo_customer WHERE login = 'user5'"):
            plan = db.session.execute(text('EXPLAIN (ANALYZE, COSTS OFF) ' + query)).scalars().all()
            print(query, '->', plan[0].strip(), '|', plan[-1].strip())
        plan = db.session.execute(text(
            "EXPLAIN ANALYZE SELECT crm_id FROM amo_lead WHERE name ILIKE '%ad 4999%'")).scalars().all()
        print('ILIKE без индекса (без миграции 002):', plan[0].strip()[:40], '|', plan[-1].strip())


if __name__ == '__main__':
    run(sys.argv[1], sys.argv[2])
