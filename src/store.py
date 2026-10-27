"""Local, minimal model metadata. No bodies, prompts or credentials."""
import collections
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Hong_Kong')


class Store:
    def __init__(self, root):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / 'records.sqlite3', timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS requests (key TEXT PRIMARY KEY, time REAL NOT NULL, model TEXT NOT NULL, flow TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS request_time ON requests(time);
          CREATE TABLE IF NOT EXISTS responses (id TEXT PRIMARY KEY, first REAL NOT NULL, last REAL NOT NULL, status TEXT NOT NULL, status_at REAL NOT NULL);
          CREATE INDEX IF NOT EXISTS response_time ON responses(first);
          CREATE TABLE IF NOT EXISTS models (response_id TEXT NOT NULL, model TEXT NOT NULL, PRIMARY KEY(response_id,model));
          CREATE TABLE IF NOT EXISTS response_flows (response_id TEXT NOT NULL, flow_id TEXT NOT NULL, PRIMARY KEY(response_id,flow_id));
          CREATE TABLE IF NOT EXISTS info (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')

    def close(self):
        self.db.close()

    def info(self, key, value=None):
        if value is not None:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO info VALUES (?,?)', (key, str(value)))
        row = self.db.execute('SELECT value FROM info WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def merge(self, ledger):
        with self.db:
            for key, r in ledger['requests'].items():
                self.db.execute('INSERT OR IGNORE INTO requests VALUES (?,?,?,?)',
                                (key, r['time'], r['model'], r['flow_id']))
            for rid, r in ledger['responses'].items():
                old = self.db.execute('SELECT status,status_at FROM responses WHERE id=?', (rid,)).fetchone()
                status, stamp = r.get('status', 'unknown'), r.get('status_at', r['last_seen'])
                terminal = ('completed', 'failed', 'incomplete', 'cancelled')
                if old and (old[1] > stamp or (old[1] == stamp and old[0] in terminal and status not in terminal)):
                    status, stamp = old
                self.db.execute('''INSERT INTO responses VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    first=MIN(first,excluded.first),last=MAX(last,excluded.last),status=excluded.status,status_at=excluded.status_at''',
                    (rid, r['first_seen'], r['last_seen'], status, stamp))
                for model in r['models']:
                    self.db.execute('INSERT OR IGNORE INTO models VALUES (?,?)', (rid, model))
                for flow_id in r.get('flow_ids', []):
                    self.db.execute('INSERT OR IGNORE INTO response_flows VALUES (?,?)', (rid, flow_id))

    def event(self, event, timestamp, flow_id, from_client, key):
        import model_report
        ledger = model_report.empty_ledger()
        model_report.consume_event(ledger, event, timestamp, flow_id, from_client, key, collections.Counter())
        if ledger['requests'] or ledger['responses']:
            self.merge(ledger)

    def report(self, day=None):
        day = dt.date.fromisoformat(day) if day else dt.datetime.now(ZONE).date()
        begin = dt.datetime.combine(day, dt.time.min, ZONE).timestamp()
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, ZONE).timestamp()
        stamp = lambda t: dt.datetime.fromtimestamp(t, ZONE).isoformat(timespec='milliseconds')
        requests = [dict(id=key, time=stamp(t), model=model, status='Sent', flow_id=flow)
                    for key, t, model, flow in self.db.execute('SELECT key,time,model,flow FROM requests WHERE time>=? AND time<? ORDER BY time DESC', (begin,end))]
        responses = []
        for rid, first, last, status in self.db.execute('SELECT id,first,last,status FROM responses WHERE first>=? AND first<? ORDER BY first DESC', (begin,end)):
            models = [r[0] for r in self.db.execute('SELECT model FROM models WHERE response_id=? ORDER BY model', (rid,))]
            responses.append(dict(id=rid, time=stamp(first), model='; '.join(models) or 'Model not reported', status=status, last=stamp(last)))
        latest = self.db.execute('SELECT MAX(time) FROM requests').fetchone()[0]
        return {'date':str(day), 'timezone':'Asia/Hong_Kong', 'requests':requests, 'responses':responses,
                'request_models':dict(collections.Counter(r['model'] for r in requests)),
                'response_models':dict(collections.Counter(r['model'] for r in responses)),
                'last_request':stamp(latest) if latest else None,
                'updated_at':dt.datetime.now(ZONE).isoformat(timespec='seconds')}
