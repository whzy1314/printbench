import hashlib
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from printbench.server import make_server
import test_store


class HTTPTests(unittest.TestCase):
    def request(self, path, method='GET', headers=None):
        connection = HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        self.addCleanup(connection.close)
        connection.request(method,path,headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()

    def setUp(self):
        test_store.StoreTests.setUp(self)
        self.published = self.store.publish(self.spec, self.source)
        self.start(make_server(self.store,0))
        self.assertEqual(self.server.server_address[0],'127.0.0.1')

    def start(self, server):
        self.server = server
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def test_api_and_hash_verified_download(self):
        status,headers,data = self.request('/api/projects')
        self.assertEqual(status,200)
        self.assertEqual(json.loads(data),[self.published])
        status,headers,data = self.request('/api/projects/tile/revisions/r1/files/tile.stl')
        self.assertEqual(status,200)
        self.assertEqual(hashlib.sha256(data).hexdigest(),self.published['artifacts'][0]['sha256'])
        self.assertIn('attachment;',headers['Content-Disposition'])
        self.assertEqual(headers['X-Content-Type-Options'],'nosniff')
        self.assertIn("default-src 'self'",headers['Content-Security-Policy'])
        self.assertEqual(self.request('/api/projects/tile/revisions/r1/files/tile.stl','HEAD')[2],b'')
        self.assertEqual(json.loads(self.request('/api/projects/tile/history')[2])['current'],'r1')

    def test_traversal_rebinding_and_unpublished_paths_refused(self):
        for path in ['/../README.md','/%2e%2e/README.md','/api/projects/..',
                     '/api/projects/tile/revisions/r1/files/%2e%2e%2fproject.json',
                     '/api/projects/tile/revisions/unknown','/api/projects/tile/revisions/r1/files/manifest.json',
                     '/api/projects/tile%2frevisions','/api/projects/tile/revisions/r1/files/tile.stl/extra']:
            with self.subTest(path=path):
                self.assertNotEqual(self.request(path)[0],200)
        self.assertEqual(self.request('/',headers={'Host':'attacker.example'})[0],403)
        self.assertEqual(self.request('/',headers={'Host':'bench.example'})[0],403)
        self.assertEqual(self.request('/api/projects','POST')[0],405)

    def test_opt_in_serves_only_canonical_host_and_keeps_loopback_probes(self):
        host = 'bench.example'
        # Exercise the real HTTP handler without opening a listener on the test host's LAN.
        with patch('printbench.server.ThreadingHTTPServer',
                   side_effect=lambda address, handler: ThreadingHTTPServer(('127.0.0.1', address[1]), handler)) as server:
            self.start(make_server(self.store, 0, host=host))
            self.assertEqual(server.call_args.args[0], ('0.0.0.0', 0))

        for method in ('GET', 'HEAD'):
            self.assertEqual(self.request('/api/projects', method, {'Host': host})[0], 200)
            self.assertEqual(self.request('/api/projects', method)[0], 200)
        for supplied in ('wrong.example', 'BENCH.example', host + '.', host + ':80', '127.0.0.1'):
            with self.subTest(host=supplied):
                self.assertEqual(self.request('/', headers={'Host': supplied, 'X-Forwarded-Host': host})[0], 403)
        peer = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5,
                              source_address=('127.0.0.2', 0))
        self.addCleanup(peer.close)
        for method, headers, status in [('GET', {}, 403), ('GET', {'Host': host}, 200), *(
                (method, {'Host': host}, 405) for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'))]:
            with self.subTest(peer_method=method, headers=headers):
                peer.request(method, '/api/projects', headers=headers)
                response = peer.getresponse()
                self.assertEqual(response.status, status)
                response.read()
        peer.putrequest('GET', '/', skip_host=True)
        peer.putheader('Host', host)
        peer.putheader('Host', 'wrong.example')
        peer.endheaders()
        self.assertEqual(peer.getresponse().status, 403)

    def test_cli_rejects_invalid_hosts(self):
        cmd = [sys.executable, '-m', 'printbench', '--data', str(self.store.root), 'serve', '--port', '0']
        for host in ('', 'BENCH.example', 'bench.example.', 'bench.example:80', '127.0.0.1',
                     'bench', 'bench.-example', 'a' * 64 + '.example', 'bench.example\n'):
            with self.subTest(host=host):
                result = subprocess.run(cmd + ['--host', host], capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, '')
                self.assertIn('canonical', json.loads(result.stderr)['error'])
        result = subprocess.run(cmd, env={**os.environ, 'PRINTBENCH_HOST': ''},
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn('canonical', json.loads(result.stderr)['error'])

    def test_static_ui_and_tamper_refusal(self):
        for path,expected in [('/',b'PrintBench'),('/app.js',b'fetch'),('/viewer.mjs',b'parseSTL'),('/style.css',b'@media')]:
            status,headers,data = self.request(path)
            self.assertEqual(status,200)
            self.assertIn(expected,data)
        (self.store.root/'tile/revisions/r1/tile.stl').write_bytes(b'broken')
        self.assertEqual(self.request('/api/projects/tile/revisions/r1/files/tile.stl')[0],409)


if __name__ == '__main__':
    unittest.main()
