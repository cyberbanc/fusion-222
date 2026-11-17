import ast, collections, contextlib, csv, datetime as dt, hashlib, http.client, io, json, os, tempfile, threading, time, unittest, zipfile
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT=Path(__file__).resolve().parents[1]
CONFIG={'accounts':['SOL_15_FIXED'],'version':'TEST','study_note':'same','bank_note':'same','learner_rules':{'note':'same'}}
KINDS=['account','decision','position','trial','equity','source']

def load(name):
    tree=ast.parse((ROOT/'trend_bot/server.py').read_text())
    keep=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in ['json_bytes','history_archive','Handler']]
    env=dict(globals())
    exec(compile(ast.Module(body=keep,type_ignores=[]),name,'exec'),env)
    return env

class Store:
    def __init__(self,gate=None,fail=False):self.gate=gate;self.fail=fail;self.snapshots=0;self.events=[]
    @contextlib.contextmanager
    def export_snapshot(self):
        self.snapshots+=1
        if self.gate and not self.gate.wait(3):raise RuntimeError('test gate expired')
        yield self
    def export_batches(self,kind):
        if self.fail and kind=='source':raise RuntimeError('test interrupted database read')
        yield [{'kind':kind,'ts':123,'created_ts':123,'account_id':'SOL_15_FIXED','nested':{'same':[1,2,3]}}]
    def event(self,*args,**kwargs):self.events.append((args,kwargs))

class Unseekable:
    def __init__(self):self.data=bytearray()
    def write(self,b):self.data.extend(b);return len(b)
    def flush(self):pass

class StreamingTests(unittest.TestCase):
    def test_all_members_and_proofs_match_buffered_export(self):
        import unittest.mock as mock
        before,after=load('server.before.py'),load('server.after.py')
        a,b=Store(),Store(); sink=Unseekable()
        with mock.patch.object(time,'time',return_value=1794888299):
            buffered,counts=before['history_archive'](a)
            _,stream_counts=after['history_archive'](b,sink)
        with zipfile.ZipFile(buffered) as old,zipfile.ZipFile(io.BytesIO(sink.data)) as new:
            self.assertIsNone(new.testzip());self.assertEqual(old.namelist(),new.namelist())
            for name in old.namelist():self.assertEqual(old.read(name),new.read(name),name)
        self.assertEqual(counts,stream_counts);self.assertEqual((a.snapshots,b.snapshots),(1,1))
        buffered.close()
    def serve(self,store):
        handler=load('server.after.py')['Handler'];handler.store=store
        server=ThreadingHTTPServer(('127.0.0.1',0),handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        return server.server_address[1]
    def test_headers_arrive_before_export_finishes(self):
        gate=threading.Event();store=Store(gate);port=self.serve(store)
        c=http.client.HTTPConnection('127.0.0.1',port,timeout=1)
        c.request('GET','/exports/history.zip');response=c.getresponse()
        self.assertEqual(response.status,200);self.assertIsNone(response.getheader('Content-Length'))
        self.assertFalse(gate.is_set());gate.set()
        body=response.read();c.close()
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertIsNone(archive.testzip());self.assertIn('manifest.json',archive.namelist())
    def test_failed_stream_cannot_pass_manifest_verification(self):
        store=Store(fail=True);port=self.serve(store)
        c=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
        c.request('GET','/exports/history.zip');response=c.getresponse();body=response.read();c.close()
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            with self.assertRaises(KeyError):archive.read('manifest.json')
        self.assertTrue(store.events);self.assertNotIn(b'HTTP/1.0 500',body)

if __name__=='__main__':unittest.main()

