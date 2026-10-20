import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from flask import Flask, g
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from werkzeug.datastructures import MultiDict

from data_explorer_api import table_payload
from tenant_database import TenantSQLAlchemy, bind_tenant_session, remove_tenant_session, snapshot_history_index_hint, tenant_engine


class TenantIsolationTest(unittest.TestCase):
    def test_concurrent_requests_and_cleanup_never_cross_tenants(self):
        app = Flask(__name__)
        db = TenantSQLAlchemy()
        barrier = Barrier(2)
        with tempfile.TemporaryDirectory() as directory:
            uris = [f"sqlite:///{Path(directory) / (name + '.db')}" for name in ['first', 'second']]
            for index, uri in enumerate(uris):
                with tenant_engine(uri).begin() as conn:
                    conn.execute(text('CREATE TABLE marker(value INTEGER)'))
                    conn.execute(text('INSERT INTO marker VALUES(:value)'), {'value': index})

            def request(index):
                for _ in range(5):
                    with app.test_request_context('/'):
                        bind_tenant_session(uris[index])
                        barrier.wait(timeout=10)
                        self.assertEqual(db.session.execute(text('SELECT value FROM marker')).scalar_one(), index)
                        db.session.execute(text('INSERT INTO marker VALUES(99)'))
                        remove_tenant_session()  # rolls back this request's write
                        self.assertNotIn('tenant_session', g)
                        barrier.wait(timeout=10)
                return True

            with ThreadPoolExecutor(max_workers=2) as executor:
                self.assertEqual(list(executor.map(request, [0, 1])), [True, True])
            for uri in uris:
                self.assertIs(tenant_engine(uri), tenant_engine(uri))
                tenant_engine(uri).dispose()


class ExplorerPerformanceTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        self.session = Session(self.engine)
        self.session.execute(text('CREATE TABLE event(id INTEGER PRIMARY KEY, cmdr TEXT, event TEXT, tickid TEXT, raw_json TEXT)'))
        self.session.execute(text('INSERT INTO event VALUES(:id,:cmdr,:event,:tick,:raw)'), [
            {'id': index, 'cmdr': 'Ästra' if index % 2 else 'Other', 'event': 'FSDJump', 'tick': 'tick-1', 'raw': json.dumps({'station': '100%_literal'}) if index == 299 else '{}'}
            for index in range(300)
        ])

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_catalog_returns_distinct_values_without_event_payloads(self):
        result = table_payload(self.session, 'event', MultiDict({'options': '1'}))
        self.assertEqual(result['data'], [])
        self.assertEqual(result['pagination']['total'], 300)
        self.assertEqual(set(result['meta']['filter_options']['cmdrs']), {'Ästra', 'Other'})
        self.assertEqual(result['meta']['filter_options']['events'], ['FSDJump'])

    def test_unicode_search_is_paginated_and_counted_before_limit(self):
        result = table_payload(self.session, 'event', MultiDict({'search': 'ÄSTRA', 'page': '2', 'page_size': '25', 'sort': 'id'}))
        self.assertEqual(result['pagination']['total'], 150)
        self.assertEqual(len(result['data']), 25)
        self.assertEqual(result['data'][0]['id'], 51)

    def test_literal_json_search_finds_row_beyond_first_page(self):
        result = table_payload(self.session, 'event', MultiDict({'search': '100%_literal'}))
        self.assertEqual([row['id'] for row in result['data']], [299])
        self.assertEqual(table_payload(self.session, 'event', MultiDict({'search': "' OR 1=1 --"}))['data'], [])

    def test_filters_and_search_are_combined(self):
        result = table_payload(self.session, 'event', MultiDict({'search': 'ästra', 'filters': json.dumps([{'field': 'cmdr', 'value': 'Other'}])}))
        self.assertEqual(result['pagination']['total'], 0)

    def test_snapshot_index_hint_is_guarded(self):
        conn = self.session.connection()
        self.assertEqual(snapshot_history_index_hint(conn), '')
        conn.execute(text('CREATE TABLE system_tick_snapshot(system_name TEXT,ticktime TEXT)'))
        conn.execute(text('CREATE INDEX ix_snapshot_system_ticktime ON system_tick_snapshot(system_name,ticktime)'))
        self.assertEqual(snapshot_history_index_hint(conn), ' INDEXED BY ix_snapshot_system_ticktime')


if __name__ == '__main__':
    unittest.main()
