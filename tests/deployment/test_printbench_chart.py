"""Offline tests for the PrintBench Helm chart."""
from pathlib import Path
import subprocess
import unittest

import yaml

CHART = Path(__file__).resolve().parents[2] / "charts/printbench"
IMAGE = "registry.example:5000/tools/printbench:Test@sha256:" + "1" * 64  # Synthetic, never pulled.
MAX_NAME = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 61])


def render(*, image: object = IMAGE, release="bench", namespace="lab", **values):
    return subprocess.run(
        ["helm", "template", release, str(CHART), "--namespace", namespace, "-f", "-"],
        input=yaml.safe_dump({"image": image, **values}),
        capture_output=True, text=True, timeout=15,
    )


class PullSecretTests(unittest.TestCase):
    def test_rejects_non_string_yaml_values(self):
        for value in ({"name": "synthetic"}, ["synthetic"], 123, True, None):
            with self.subTest(value=value):
                result = render(imagePullSecret=value)
                self.assertEqual(result.returncode, 1, result.stdout or result.stderr)
                self.assertIn("pull Secret", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_rejects_invalid_names_without_trimming(self):
        for value in (" ", " valid-name ", "valid-name\n", "Not A Secret Name",
                      "a_b", "-abc", "abc-", "abc.-def", "a..b", "a.", MAX_NAME + "e"):
            with self.subTest(value=value):
                result = render(imagePullSecret=value)
                self.assertEqual(result.returncode, 1, result.stdout or result.stderr)
                self.assertIn("pull Secret", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_preserves_valid_names_as_strings(self):
        for value in ("pull-1", "registry.pull-1", "true", "123", MAX_NAME):
            with self.subTest(value=value):
                result = render(imagePullSecret=value)
                self.assertEqual(result.returncode, 0, result.stderr)
                deployment, = (doc for doc in yaml.safe_load_all(result.stdout)
                               if doc and doc["kind"] == "Deployment")
                self.assertEqual(deployment["spec"]["template"]["spec"]["imagePullSecrets"],
                                 [{"name": value}])


class ChartTests(unittest.TestCase):
    def manifests(self, **values):
        result = render(**values)
        self.assertEqual(result.returncode, 0, result.stderr)
        return {doc["kind"]: doc for doc in yaml.safe_load_all(result.stdout)}

    def test_omits_pull_secrets_when_unset_or_empty(self):
        for values in ({}, {"imagePullSecret": ""}):
            with self.subTest(values=values):
                deployment = self.manifests(**values)["Deployment"]
                self.assertNotIn("imagePullSecrets", deployment["spec"]["template"]["spec"])

    def test_generic_release_uses_default_storage_without_owning_namespace(self):
        docs = self.manifests()
        self.assertEqual(set(docs), {"Deployment", "PersistentVolumeClaim"})
        claim = docs["PersistentVolumeClaim"]
        self.assertNotIn("storageClassName", claim["spec"])
        self.assertEqual(claim["metadata"]["namespace"], "lab")
        self.assertEqual(claim["metadata"]["name"], "bench-library")
        pod = docs["Deployment"]["spec"]["template"]["spec"]
        self.assertEqual(pod["containers"][0]["image"], IMAGE)
        self.assertNotIn("env", pod["containers"][0])
        self.assertNotIn("ports", pod["containers"][0])
        self.assertEqual(pod["volumes"][0]["persistentVolumeClaim"]["claimName"], "bench-library")

    def test_ingress_routes_one_host_to_its_release_without_changing_storage_or_probes(self):
        default = self.manifests()
        docs = self.manifests(ingressHost="bench.example")
        self.assertEqual(set(docs), set(default) | {"Service", "Ingress"})
        route = docs.pop("Ingress")["spec"]
        self.assertEqual(route, {"ingressClassName": "traefik", "rules": [{
            "host": "bench.example", "http": {"paths": [{"path": "/", "pathType": "Prefix",
                "backend": {"service": {"name": "bench", "port": {"name": "http"}}}}]},
        }]})
        svc = docs.pop("Service")
        self.assertEqual((svc["metadata"]["namespace"], svc["metadata"]["name"]), ("lab", "bench"))
        selector = docs["Deployment"]["spec"]["selector"]["matchLabels"]
        self.assertEqual(svc["spec"], {"type": "ClusterIP", "selector": selector,
                                     "ports": [{"name": "http", "port": 80, "targetPort": "http"}]})
        container = docs["Deployment"]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container.pop("env"), [{"name": "PRINTBENCH_HOST", "value": "bench.example"}])
        self.assertEqual(container.pop("ports"), [{"name": "http", "containerPort": 8765}])
        self.assertEqual(docs, default)

    def test_ingress_rejects_invalid_host_values(self):
        for host in (None, True, [], {}, "", "*.example", "BENCH.example", "bench.example.",
                     "bench.example:80", "127.0.0.1", "bench", "bench.-example",
                     "a" * 64 + ".example", "bench.example\n", MAX_NAME + "e"):
            with self.subTest(host=host):
                result = render(ingressHost=host)
                self.assertEqual(result.returncode, 1, result.stdout or result.stderr)
                self.assertIn("ingressHost", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_management_projects_only_token_on_the_existing_workload(self):
        plain = render(ingressHost="bench.example")
        opted = render(ingressHost="bench.example", management={
            "tokenSecret": "bench-token"})
        self.assertEqual(opted.returncode, 0, opted.stderr)
        docs = {(d['kind'], d['metadata']['name']):d for d in yaml.safe_load_all(opted.stdout)}
        deployment = docs[('Deployment', 'bench')]
        pod = deployment['spec']['template']['spec']
        container = pod['containers'][0]
        env = {row['name']:row['value'] for row in container['env'] if row['name'] != 'PRINTBENCH_HOST'}
        self.assertEqual(env, {'PRINTBENCH_MANAGEMENT_TOKEN_FILE': '/run/printbench/token/token'})
        container['env'] = [row for row in container['env'] if row['name'] == 'PRINTBENCH_HOST']
        self.assertEqual(container['volumeMounts'].pop(), {'name': 'management-token',
                         'mountPath': '/run/printbench/token', 'readOnly': True})
        self.assertEqual(pod['volumes'].pop(), {'name': 'management-token', 'secret': {
            'secretName': 'bench-token', 'defaultMode': 0o440, 'items': [{'key': 'token', 'path': 'token'}]}})
        self.assertEqual(docs, {(d['kind'], d['metadata']['name']):d for d in yaml.safe_load_all(plain.stdout)})

    def test_shutdown_probe_adds_only_the_writer_env_flag(self):
        writer = {'tokenSecret': 'bench-token'}
        for values in ({}, {'ingressHost': 'bench.example'}):
            with self.subTest(values=values):
                base = self.manifests(management=writer, **values)
                docs = self.manifests(management={**writer, 'shutdownProbe': True}, **values)
                container = docs['Deployment']['spec']['template']['spec']['containers'][0]
                self.assertEqual(container['env'].pop(), {
                    'name': 'PRINTBENCH_MANAGEMENT_SHUTDOWN_PROBE', 'value': '1'})
                self.assertEqual(docs, base)

    def test_anonymous_is_disabled_by_default_and_adds_only_an_env_flag(self):
        for values in ({}, {'ingressHost': 'bench.example'}):
            with self.subTest(values=values):
                base = self.manifests(**values)
                self.assertEqual(self.manifests(management={'anonymous': False}, **values), base)
                docs = self.manifests(management={'anonymous': True}, **values)
                container = docs['Deployment']['spec']['template']['spec']['containers'][0]
                self.assertEqual(container['env'].pop(), {
                    'name': 'PRINTBENCH_MANAGEMENT_ANONYMOUS', 'value': '1'})
                if not container['env']:
                    del container['env']
                self.assertEqual(docs, base)

    def test_anonymous_rejects_non_booleans_and_mixed_settings(self):
        for config in (
            *({'anonymous': value} for value in (None, '', 'true', 'false', 0, 1, [], {})),
            *({'anonymous': value, **other} for value in (True, False) for other in (
                {'tokenSecret': 'writer'}, {'proofSecret': 'proof'}, {'shutdownProbe': True},
                {'shutdownProbe': False}, {'unknown': False})),
        ):
            with self.subTest(config=config):
                result = render(management=config)
                self.assertEqual(result.returncode, 1, result.stdout or result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertIn('management', result.stderr)

    def test_explicitly_disabled_shutdown_probe_preserves_writer_render(self):
        writer = {'tokenSecret': 'bench-token'}
        base = render(management=writer)
        disabled = render(management={**writer, 'shutdownProbe': False})
        self.assertEqual(base.returncode, 0, base.stderr)
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        self.assertEqual(disabled.stdout, base.stdout)

    def test_shutdown_probe_rejects_malformed_or_incompatible_management(self):
        for override in (
            *({'tokenSecret': 'bench-token', 'shutdownProbe': value}
              for value in (None, '', 'true', 'false', 0, 1, [], {})),
            *({'tokenSecret': value, 'shutdownProbe': True}
              for value in (None, '', 'A/B', 'bench-token\n', 123, MAX_NAME + 'e')),
            {'shutdownProbe': True},
            {'shutdownProbe': False},
            {'shutdownProbe': True, 'proofSecret': 'bench-proof'},
            {'shutdownProbe': False, 'proofSecret': 'bench-proof'},
            {'shutdownProbe': True, 'proofSecret': 'bench-proof', 'tokenSecret': 'bench-token'},
            {'shutdownProbe': True, 'tokenSecret': 'bench-token', 'unknown': False},
        ):
            with self.subTest(override=override):
                result = render(management=override)
                self.assertEqual(result.returncode, 1, result.stdout or result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertIn('management', result.stderr)

    def test_proof_projects_only_the_sentinel_on_the_existing_workload(self):
        base = self.manifests(ingressHost='bench.example')
        docs = self.manifests(ingressHost='bench.example', management={'proofSecret': 'bench-proof'})
        pod = docs['Deployment']['spec']['template']['spec']
        container = pod['containers'][0]
        self.assertEqual(container['env'].pop(), {'name': 'PRINTBENCH_MANAGEMENT_PROOF_FILE',
                         'value': '/run/printbench/token/sentinel'})
        self.assertEqual(container['volumeMounts'].pop(), {'name': 'management-token',
                         'mountPath': '/run/printbench/token', 'readOnly': True})
        self.assertEqual(pod['volumes'].pop(), {'name': 'management-token', 'secret': {
            'secretName': 'bench-proof', 'defaultMode': 0o440,
            'items': [{'key': 'sentinel', 'path': 'sentinel'}]}})
        self.assertEqual(docs, base)

    def test_proof_rejects_mixed_unknown_or_malformed_opt_in(self):
        for override in ({'proofSecret': 'probe', 'tokenSecret': 'writer'},
                         {'proofSecret': 'probe', 'tokenSecret': None},
                         {'proofSecret': 'probe', 'enabled': False}, {'proofsecret': 'probe'},
                         *({'proofSecret': value} for value in
                           (None, False, [], {}, 123, '', 'Probe', 'a/b', 'probe\n', MAX_NAME + 'e'))):
            with self.subTest(override=override):
                result = render(management=override, ingressHost='bench.example')
                self.assertEqual(result.returncode, 1, result.stdout or result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertIn('management', result.stderr)

    def test_management_refuses_missing_or_malformed_values(self):
        for override in (None, False, {}, {'host': 'bench.example'}, {'tokenSecret': ''},
                         {'tokenSecret': 'A/B'}, {'tokenSecret': None}, {'tokenSecret': 123},
                         {'host': 'bench.example', 'tokenSecret': 'a', 'tlsSecret': 'b'}):
            with self.subTest(override=override):
                result = render(management=override)
                self.assertEqual(result.returncode, 1, result.stdout or result.stderr)
                self.assertIn('management', result.stderr)

    def test_yaml_like_names_stay_strings(self):
        docs = self.manifests(release="true", namespace="123", createNamespace=True)
        self.assertEqual(docs["Namespace"]["metadata"]["name"], "123")
        deployment = docs["Deployment"]
        self.assertEqual(deployment["metadata"]["name"], "true")
        self.assertEqual(deployment["spec"]["selector"]["matchLabels"], {"app.kubernetes.io/name": "true"})

    def test_reuses_named_storage_class(self):
        docs = self.manifests(storageClass={"name": "existing"})
        self.assertNotIn("StorageClass", docs)
        self.assertEqual(docs["PersistentVolumeClaim"]["spec"]["storageClassName"], "existing")

    def test_explicit_namespace_and_storage_class(self):
        docs = self.manifests(createNamespace=True, storageClass={
            "name": "test-retain", "provisioner": "example.invalid/csi", "parameters": {"tier": "fast"},
        })
        self.assertEqual(docs["Namespace"]["metadata"]["name"], "lab")
        storage = docs["StorageClass"]
        self.assertEqual(storage["metadata"]["name"], "test-retain")
        self.assertEqual(storage["provisioner"], "example.invalid/csi")
        self.assertEqual(storage["parameters"], {"tier": "fast"})
        self.assertEqual(storage["reclaimPolicy"], "Retain")
        self.assertEqual(docs["PersistentVolumeClaim"]["spec"]["storageClassName"], "test-retain")

    def test_rejects_provisioner_without_storage_class_name(self):
        result = render(storageClass={"provisioner": "example.invalid/csi"})
        self.assertEqual(result.returncode, 1, result.stdout or result.stderr)
        self.assertIn("storageClass.name", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_rejects_missing_mutable_or_invalid_image_references(self):
        for value in ("", "repo/image:tag", "repo/image@sha256:123", f" {IMAGE}",
                      IMAGE.replace("tools/printbench", "tools//printbench"), {"ref": IMAGE}, None):
            with self.subTest(value=value):
                result = render(image=value)
                self.assertEqual(result.returncode, 1, result.stdout or result.stderr)
                self.assertIn("immutable", result.stderr)
                self.assertEqual(result.stdout, "")
