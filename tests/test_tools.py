import json
import unittest

from hermes_headroom_plugin.tools import handle_headroom_retrieve


class ToolsTest(unittest.TestCase):
    def test_missing_hash(self):
        result = json.loads(handle_headroom_retrieve({"hash": ""}))
        self.assertFalse(result["success"])
        self.assertIn("hash", result["error"])


if __name__ == "__main__":
    unittest.main()
