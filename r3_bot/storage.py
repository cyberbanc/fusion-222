import contextlib
import json
import os
import sqlite3
import threading
import time
import uuid

TABLE = 'fusion_r3_v1_records'
KINDS = {'meta','account','decision','position','trial','snapshot','event','funding','equity','study','candle1','candle_tf','signal','source'}


class Store:
    def __init__(self, url=None):
        self.url = url or os.getenv('DATABASE_URL', '')
        self.lock = threading.RLock()
        self.pg = self.url.startswith(('postgres://','postgresql://'))
        if self.pg:
            import psycopg
            self.conn = psycopg.connect(self.url, autocommit=True, connect_timeout=15)
        else:
            path = self.url.removeprefix('sqlite:///') or ':memory:'
            self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        with self.lock:
            self.conn.execute(f'CREATE TABLE IF NOT EXISTS {TABLE} (kind TEXT NOT NULL, key TEXT NOT NULL, account TEXT NOT NULL DEFAULT \'\', symbol TEXT NOT NULL DEFAULT \'\', status TEXT NOT NULL DEFAULT \'\', ts BIGINT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(kind,key))')
            self.conn.execute(f'CREATE INDEX IF NOT EXISTS fusion_r3_kind_status_idx ON {TABLE}(kind,status,ts)')
            self.conn.execute(f'CREATE INDEX IF NOT EXISTS fusion_r3_account_time_idx ON {TABLE}(kind,account,ts,key)')
            self.conn.execute(f'CREATE INDEX IF NOT EXISTS fusion_r3_symbol_time_idx ON {TABLE}(kind,symbol,ts,key)')
        self.leader = False

    def sql(self, text):
        return text.replace('?', '%s') if self.pg else text

    def acquire_worker(self):
        with self.lock:
            if self.pg:
                self.leader = bool(self.conn.execute('SELECT pg_try_advisory_lock(222330015)').fetchone()[0])
            else:
                self.leader = True
        return self.leader

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            self.conn.execute('BEGIN')
            try:
                yield
                self.conn.execute('COMMIT')
            except BaseException:
                self.conn.execute('ROLLBACK')
                raise

    def get(self, kind, key, default=None):
        with self.lock:
            row = self.conn.execute(self.sql(f'SELECT data FROM {TABLE} WHERE kind=? AND key=?'), (kind,key)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, kind, key, data):
        assert kind in KINDS
        ts = int(data.get('created_ts', data.get('entry_ts', data.get('ts', time.time()))))
        values=(kind,key,data.get('account_id',''),data.get('symbol',''),data.get('status',''),ts,
            json.dumps(data, ensure_ascii=False, separators=(',',':'), allow_nan=False))
        with self.lock:
            self.conn.execute(self.sql(f'INSERT INTO {TABLE}(kind,key,account,symbol,status,ts,data) VALUES (?,?,?,?,?,?,?) ON CONFLICT(kind,key) DO UPDATE SET account=excluded.account,symbol=excluded.symbol,status=excluded.status,ts=excluded.ts,data=excluded.data'),values)

    def rows(self, kind, account=None, symbol=None, status=None, limit=500, before=None):
        clauses=['kind=?']; values=[kind]
        for field,value in [('account',account),('symbol',symbol),('status',status)]:
            if value is not None:
                clauses.append(field+'=?'); values.append(value)
        if before is not None:
            clauses.append('ts<?'); values.append(before)
        query=f'SELECT data FROM {TABLE} WHERE '+ ' AND '.join(clauses)+' ORDER BY ts DESC,key DESC'
        if limit is not None:
            query+=' LIMIT ?'; values.append(limit)
        with self.lock:
            rows=self.conn.execute(self.sql(query),values).fetchall()
        return [json.loads(row[0]) for row in rows]

    def count(self, kind, account=None, status=None):
        clauses=['kind=?']; values=[kind]
        if account is not None:clauses.append('account=?');values.append(account)
        if status is not None:clauses.append('status=?');values.append(status)
        with self.lock:
            return self.conn.execute(self.sql(f'SELECT COUNT(*) FROM {TABLE} WHERE '+ ' AND '.join(clauses)),values).fetchone()[0]

    def export_batches(self, kind, size=250):
        # Keyset over the primary key: no UI limit and no giant fetchall.
        cursor=''
        while True:
            with self.lock:
                rows=self.conn.execute(self.sql(f'SELECT key,data FROM {TABLE} WHERE kind=? AND key>? ORDER BY key LIMIT ?'),(kind,cursor,size)).fetchall()
            if not rows:break
            cursor=rows[-1][0]
            yield [json.loads(row[1]) for row in rows]

    def event(self, level, message, **data):
        now=int(time.time())
        self.put('event',str(uuid.uuid4()),{'created_ts':now,'level':level,'message':message,**data})

    def ping(self):
        with self.lock:return self.conn.execute('SELECT 1').fetchone()[0]==1

    @contextlib.contextmanager
    def export_snapshot(self):
        reader=object.__new__(Store)
        reader.lock=threading.RLock();reader.pg=self.pg;reader.url=self.url;reader.leader=False
        if self.pg:
            import psycopg
            reader.conn=psycopg.connect(self.url,autocommit=True,connect_timeout=15)
            reader.conn.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        else:
            reader.conn=sqlite3.connect(':memory:',check_same_thread=False,isolation_level=None)
            with self.lock:self.conn.backup(reader.conn)
        try:yield reader
        finally:
            if self.pg:reader.conn.execute('ROLLBACK')
            reader.conn.close()
