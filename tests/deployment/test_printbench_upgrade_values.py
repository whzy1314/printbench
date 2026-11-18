"""Exercise Helm's real upgrade value merge with synthetic release history."""
import base64
import gzip
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from threading import Thread
import unittest

import yaml

from test_printbench_chart import CHART, IMAGE, render

WRITER = {'tokenSecret': 'bench-token'}
ENABLED = {**WRITER, 'shutdownProbe': True}
NULL_OVERRIDES = (
    ({**WRITER, 'shutdownProbe': None}, 'management.shutdownProbe must be a boolean'),
    ({**ENABLED, 'tokenSecret': None}, 'management requires a valid tokenSecret'),
)


class UpgradeValuesTests(unittest.TestCase):
    def setUp(self):
        helm = shutil.which('helm')
        if helm is None:
            self.fail('Helm is required for upgrade tests')
        self.helm = helm
        prior = render(management=ENABLED)
        self.assertEqual(prior.returncode, 0, prior.stderr)
        self.prior = list(yaml.safe_load_all(prior.stdout))
        release = {
            'name': 'bench', 'namespace': 'lab', 'version': 1, 'info': {'status': 'deployed'},
            'config': {'image': IMAGE, 'management': ENABLED}, 'manifest': prior.stdout,
            'chart': {'metadata': yaml.safe_load((CHART / 'Chart.yaml').read_text()),
                      'values': yaml.safe_load((CHART / 'values.yaml').read_text())},
        }
        # Helm stores base64(gzip(release JSON)); Kubernetes base64-encodes Secret data again.
        encoded = base64.b64encode(base64.b64encode(gzip.compress(json.dumps(release).encode())))
        secret = {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'helm.sh/release.v1',
                  'metadata': {'name': 'sh.helm.release.v1.bench.v1', 'namespace': 'lab',
                               'labels': {'owner': 'helm', 'name': 'bench', 'status': 'deployed', 'version': '1'}},
                  'data': {'release': encoded.decode()}}
        responses = {
            '/version': {'gitVersion': 'v1.32.0'},
            '/api': {'kind': 'APIVersions', 'versions': ['v1']},
            '/apis': {'kind': 'APIGroupList', 'groups': [{'name': 'apps',
                      'versions': [{'groupVersion': 'apps/v1', 'version': 'v1'}]}]},
            '/api/v1/namespaces/lab/secrets': {'apiVersion': 'v1', 'kind': 'SecretList', 'items': [secret]},
        }
        for route, version, name, kind in (
            ('/api/v1', 'v1', 'persistentvolumeclaims', 'PersistentVolumeClaim'),
            ('/apis/apps/v1', 'apps/v1', 'deployments', 'Deployment'),
        ):
            responses[route] = {'kind': 'APIResourceList', 'groupVersion': version,
                                'resources': [{'name': name, 'namespaced': True, 'kind': kind}]}

        class ReleaseAPI(BaseHTTPRequestHandler):
            def do_GET(self):
                response = responses.get(self.path.split('?', 1)[0])
                if response is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())

            def log_message(self, format, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), ReleaseAPI)
        self.addCleanup(server.server_close)
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        home = tempfile.TemporaryDirectory(prefix='printbench-helm-')
        self.addCleanup(home.cleanup)
        kubeconfig = Path(home.name) / 'kubeconfig'
        kubeconfig.write_text('apiVersion: v1\nkind: Config\n')
        self.env = {'HOME': home.name, 'HELM_DRIVER': 'secret'}
        self.command = [self.helm, 'upgrade', 'bench', str(CHART), '-n', 'lab', '-f', '-',
                        '--dry-run=client', '--disable-openapi-validation', '-o', 'json',
                        '--kubeconfig', str(kubeconfig), '--kube-apiserver', f'http://127.0.0.1:{server.server_port}']

    def upgrade(self, mode, **values):
        return subprocess.run([*self.command, mode], input=yaml.safe_dump({'image': IMAGE, **values}),
                              env=self.env, capture_output=True, text=True, timeout=15)

    def manifests(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return list(yaml.safe_load_all(json.loads(result.stdout)['manifest']))

    def test_reset_rejects_explicit_null(self):
        for values, diagnostic in NULL_OVERRIDES:
            with self.subTest(values=values):
                result = self.upgrade('--reset-values', management=values)
                self.assertEqual(result.returncode, 1, result.stderr or 'null override did not fail')
                self.assertFalse(result.stdout, 'unexpected release output for a rejected upgrade')
                self.assertIn(diagnostic, result.stderr)

    def test_reset_omit_or_false_matches_fresh_render(self):
        for management in (None, WRITER, {**WRITER, 'shutdownProbe': False}):
            with self.subTest(management=management):
                writer = management is not None
                values = {'management': management} if writer else {}
                docs = self.manifests(self.upgrade('--reset-values', **values))
                deployment, = (doc for doc in docs if doc['kind'] == 'Deployment')
                pod = deployment['spec']['template']['spec']
                self.assertEqual(pod['containers'][0].get('env', []), [
                    {'name': 'PRINTBENCH_MANAGEMENT_TOKEN_FILE', 'value': '/run/printbench/token/token'},
                ] if writer else [])
                self.assertEqual([v['secret']['secretName'] for v in pod['volumes'] if 'secret' in v],
                                 ['bench-token'] if writer else [])
                fresh = render(management=management) if writer else render()
                self.assertEqual(fresh.returncode, 0, fresh.stderr)
                self.assertEqual(docs, list(yaml.safe_load_all(fresh.stdout)))

    def test_helm_3_18_3_unsupported_reuse_retains_prior_writer(self):
        version = subprocess.check_output([self.helm, 'version', '--template', '{{.Version}}'],
                                          env=self.env, text=True, timeout=15)
        if version != 'v3.18.3':
            self.skipTest('characterizes unsupported merge behavior in Helm 3.18.3')
        for values, _ in NULL_OVERRIDES:
            with self.subTest(values=values):
                self.assertEqual(self.manifests(self.upgrade('--reuse-values', management=values)), self.prior)
        self.assertEqual(self.manifests(self.upgrade('--reset-then-reuse-values', management=WRITER)), self.prior)
