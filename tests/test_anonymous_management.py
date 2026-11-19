from contextlib import closing, contextmanager
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import urlsplit

from printbench.management import MAX_BODY
from printbench.store import Store
from test_management import frame
from test_store import snapshot

OPT_IN = 'PRINTBENCH_MANAGEMENT_ANONYMOUS'
PROJECT = '/manage/v1/projects/sample'
READ = '/api/projects/sample'
# Observe real contention in the child, then delegate to the unmodified blocking lock.
OBSERVE_LOCK = '''
import fcntl, runpy
flock = fcntl.flock
def observe(fd, operation):
    try:
        flock(fd, operation | fcntl.LOCK_NB)
    except BlockingIOError:
        print('waiting for writer lock', flush=True)
        flock(fd, operation)
fcntl.flock = observe
runpy.run_module('printbench', run_name='__main__', alter_sys=True)
'''


class AnonymousManagementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'library')
        self.spec = json.loads(Path('examples/sample.json').read_bytes())
        self.body = frame(self.spec)
        self.headers = {'Content-Type': 'application/octet-stream', 'If-None-Match': '*'}
        self.serve = ['--data', str(self.store.root), 'serve', '--port', '0']
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('PRINTBENCH_')}

    def line(self, proc):
        self.assertTrue(select.select([proc.stdout], [], [], 5)[0], 'Waiting for server output')
        return proc.stdout.readline()

    @contextmanager
    def server(self, *, enabled=True, host=None, hook=None):
        env = self.env | ({OPT_IN: '1'} if enabled else {})
        command = ['-c', hook] if hook else ['-m', 'printbench']
        if host:
            env['PRINTBENCH_HOST'] = host
        with subprocess.Popen([sys.executable, *command, *self.serve], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
            try:
                port = urlsplit(json.loads(self.line(proc))['url']).port
                yield proc, port
                if proc.poll() is None:
                    proc.send_signal(signal.SIGINT)
                out, err = proc.communicate(timeout=5)
                self.assertEqual((proc.returncode, out, err), (0, b'', b''))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)

    def request(self, port, method, path=PROJECT, headers=None, body=None):
        with closing(HTTPConnection('127.0.0.1', port, timeout=3)) as conn:
            conn.request(method, path, body, headers or {})
            with conn.getresponse() as res:
                return res.status, res.headers, res.read()

    def test_invalid_or_conflicting_opt_ins_fail_before_serving(self):
        for config in (*({OPT_IN: value} for value in ('', '0', 'true', 'yes', ' 1')),
                       *({OPT_IN: '1', key: value} for key, value in (
                           ('PRINTBENCH_MANAGEMENT_TOKEN_FILE', '/unused'),
                           ('PRINTBENCH_MANAGEMENT_PROOF_FILE', '/unused'),
                           ('PRINTBENCH_MANAGEMENT_SHUTDOWN_PROBE', '1')))):
            with self.subTest(config=config):
                try:
                    result = subprocess.run([sys.executable, '-m', 'printbench', *self.serve], env=self.env | config,
                                            capture_output=True, timeout=3)
                except subprocess.TimeoutExpired:
                    self.fail('Invalid configuration did not exit')
                self.assertEqual((result.returncode, result.stdout), (1, b''))
                self.assertIn('anonymous', json.loads(result.stderr)['error'].lower())
        self.assertFalse(self.store.root.exists())

    def test_browser_cross_origin_shapes_cannot_write_and_host_remains_exact(self):
        with self.server(host='bench.example') as (_, port):
            host = {'Host': 'bench.example'}
            origin = {'Origin': 'https://other.example'}
            for method, path, headers, code in (
                ('PUT', PROJECT, self.headers | {'X-Forwarded-Host': 'bench.example'}, 403),
                ('PUT', PROJECT, self.headers | {'Host': 'bench.example:80'}, 403),
                ('OPTIONS', PROJECT, host | origin | {'Access-Control-Request-Method': 'PUT',
                 'Access-Control-Request-Headers': 'content-type,if-none-match'}, 405),
                ('OPTIONS', PROJECT + '/archive', host | origin | {'Access-Control-Request-Method': 'POST',
                 'Access-Control-Request-Headers': 'if-match'}, 405),
                ('POST', PROJECT + '/archive', host | origin | {'Content-Type': 'text/plain'}, 428),
                ('PUT', PROJECT, host | self.headers | {'Content-Length': str(MAX_BODY + 1)}, 413),
                ('PUT', '/api/projects', host, 405),
            ):
                with self.subTest(method=method, path=path, code=code):
                    status, reply, _ = self.request(port, method, path, headers)
                    self.assertEqual(status, code)
                    self.assertIsNone(reply.get('Access-Control-Allow-Origin'))
            with closing(HTTPConnection('127.0.0.1', port, timeout=3)) as conn:
                conn.putrequest('GET', PROJECT + '/state', skip_host=True)
                conn.putheader('Host', 'bench.example')
                conn.putheader('Host', 'bench.example')
                conn.endheaders()
                self.assertEqual(conn.getresponse().status, 403)
            self.assertFalse(self.store.root.exists())
            self.assertEqual(self.request(port, 'PUT', headers=host | self.headers, body=self.body)[0], 201)

    def test_archives_stay_hidden_after_rollback_to_default_reader(self):
        with self.server() as (_, port):
            self.assertEqual(self.request(port, 'PUT', headers=self.headers, body=self.body)[0], 201)
            visible = frame(dict(self.spec, id='visible'))
            self.assertEqual(self.request(port, 'PUT', '/manage/v1/projects/visible', self.headers, visible)[0], 201)
            tag = self.request(port, 'GET', PROJECT + '/state')[1]['ETag']
            file = READ + '/revisions/r1/files/sample.txt'
            etag = self.request(port, 'GET', file)[1]['ETag']
            self.assertEqual(self.request(port, 'POST', PROJECT + '/archive', {'If-Match': tag})[0], 200)
        archived = snapshot(self.store.root)
        with self.server(enabled=False) as (_, port):
            self.assertEqual(self.request(port, 'PUT', headers=self.headers, body=self.body)[0], 405)
            self.assertEqual(self.request(port, 'GET', PROJECT + '/state')[0], 404)
            status, _, data = self.request(port, 'GET', '/api/projects')
            self.assertEqual((status, [p['id'] for p in json.loads(data)]), (200, ['visible']))
            self.assertEqual(self.request(port, 'GET', '/api/projects/visible/revisions/r1/files/sample.txt')[2], b'model')
            for path in (READ, READ + '/history', READ + '/revisions/r1', file):
                for method in ('GET', 'HEAD'):
                    self.assertEqual(self.request(port, method, path, {'If-None-Match': etag})[0], 404)
        self.assertEqual(snapshot(self.store.root), archived)

    def test_sigterm_closes_listener_and_waits_for_admitted_write(self):
        with self.server(hook=OBSERVE_LOCK) as (proc, port), \
                closing(HTTPConnection('127.0.0.1', port, timeout=5)) as conn:
            with self.store._writer(create=True):
                conn.request('PUT', PROJECT, self.body, self.headers)
                self.assertEqual(self.line(proc), b'waiting for writer lock\n')
                proc.terminate()
                deadline = time.monotonic() + 3
                while True:
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=1):
                            pass
                    except ConnectionRefusedError:
                        break
                    self.assertLess(time.monotonic(), deadline, 'Listener still accepting after SIGTERM')
                    time.sleep(0.01)
                with self.assertRaises(subprocess.TimeoutExpired, msg='Process exited with a write still waiting'):
                    proc.wait(timeout=0.2)
            with conn.getresponse() as res:
                self.assertEqual((res.status, json.load(res)['revision']), (201, 'r1'))
            self.assertEqual(proc.wait(timeout=5), 0)
        self.assertEqual(self.store.history('sample')['revisions'], ['r1'])
        self.assertEqual(self.store.artifact('sample', 'r1', 'sample.txt')[0], b'model')
