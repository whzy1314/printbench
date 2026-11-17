"""Offline tests for the PrintBench Helm chart."""
from pathlib import Path
import subprocess
import unittest

import yaml

CHART = Path(__file__).resolve().parents[2] / "charts/printbench"
IMAGE = "registry.example:5000/tools/printbench:Test@sha256:" + "1" * 64  # Synthetic, never pulled.
MAX_NAME = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 61])


def render(secret: object = "registry-pull", *, image: object = IMAGE,
           release="bench", namespace="lab", **values):
    return subprocess.run(
        ["helm", "template", release, str(CHART), "--namespace", namespace, "-f", "-"],
        input=yaml.safe_dump({"image": image, "imagePullSecret": secret, **values}),
        capture_output=True, text=True, timeout=15,
    )


class PullSecretTests(unittest.TestCase):
    def test_rejects_non_string_yaml_values(self):
        for value in ({"name": "synthetic"}, ["synthetic"], 123, True, None):
            with self.subTest(value=value):
                result = render(value)
                self.assertEqual(result.returncode, 1, result.stdout or result.stderr)
                self.assertIn("pull Secret", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_rejects_invalid_names_without_trimming(self):
        for value in ("", " ", " valid-name ", "valid-name\n", "Not A Secret Name",
                      "a_b", "-abc", "abc-", "abc.-def", "a..b", "a.", MAX_NAME + "e"):
            with self.subTest(value=value):
                result = render(value)
                self.assertEqual(result.returncode, 1, result.stdout or result.stderr)
                self.assertIn("pull Secret", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_preserves_valid_names_as_strings(self):
        for value in ("pull-1", "registry.pull-1", "true", "123", MAX_NAME):
            with self.subTest(value=value):
                result = render(value)
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

    def test_generic_release_uses_default_storage_without_owning_namespace(self):
        docs = self.manifests()
        self.assertEqual(set(docs), {"Deployment", "PersistentVolumeClaim"})
        claim = docs["PersistentVolumeClaim"]
        self.assertNotIn("storageClassName", claim["spec"])
        self.assertEqual(claim["metadata"]["namespace"], "lab")
        self.assertEqual(claim["metadata"]["name"], "bench-library")
        pod = docs["Deployment"]["spec"]["template"]["spec"]
        self.assertEqual(pod["containers"][0]["image"], IMAGE)
        self.assertEqual(pod["volumes"][0]["persistentVolumeClaim"]["claimName"], "bench-library")

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
