import hashlib
from http.client import HTTPConnection
import json
import threading
import unittest

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
        self.server = make_server(self.store,0)
        thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.assertEqual(self.server.server_address[0],'127.0.0.1')

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
        self.assertEqual(self.request('/api/projects','POST')[0],405)

    def test_static_ui_and_tamper_refusal(self):
        for path,expected in [('/',b'PrintBench'),('/app.js',b'fetch'),('/viewer.mjs',b'parseSTL'),('/style.css',b'@media')]:
            status,headers,data = self.request(path)
            self.assertEqual(status,200)
            self.assertIn(expected,data)
        (self.store.root/'tile/revisions/r1/tile.stl').write_bytes(b'broken')
        self.assertEqual(self.request('/api/projects/tile/revisions/r1/files/tile.stl')[0],409)


if __name__ == '__main__':
    unittest.main()
