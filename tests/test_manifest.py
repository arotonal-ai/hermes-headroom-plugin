import unittest
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "plugin.yaml"


class PluginManifestTest(unittest.TestCase):
    def test_manifest_uses_canonical_hermes_fields(self):
        data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(data["manifest_version"], 1)
        self.assertEqual(data["name"], "headroom_retrieve")
        self.assertEqual(data["kind"], "standalone")
        self.assertEqual(data["provides_tools"], ["headroom_retrieve"])
        self.assertEqual(
            data["provides_hooks"],
            ["transform_terminal_output", "transform_llm_output", "pre_llm_call"],
        )

    def test_manifest_does_not_use_unsupported_metadata_fields_or_paint_plugin_version(self):
        data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
        for field in ["commands", "middleware", "provides_middleware"]:
            self.assertNotIn(field, data)
        self.assertNotIn("version", data)


if __name__ == "__main__":
    unittest.main()
