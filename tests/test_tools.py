import json
import unittest
from unittest.mock import patch

from hermes_headroom_plugin.schemas import HEADROOM_RETRIEVE_SCHEMA
from hermes_headroom_plugin.tools import handle_headroom_retrieve


class ToolsTest(unittest.TestCase):
    def test_missing_hash(self):
        result = json.loads(handle_headroom_retrieve({"hash": ""}))
        self.assertFalse(result["success"])
        self.assertIn("hash", result["error"])

    def test_marker_is_normalized_and_handler_uses_hash_only(self):
        with patch("hermes_headroom_plugin.tools.retrieve", return_value={"success": True, "result": {"original_content": "exact"}}) as call:
            result = json.loads(handle_headroom_retrieve({"hash": "<<ccr:abc123,base64,1KB>>"}))
        self.assertTrue(result["success"])
        call.assert_called_once_with("abc123")

    def test_schema_has_only_required_hash(self):
        parameters = HEADROOM_RETRIEVE_SCHEMA["parameters"]
        self.assertEqual(parameters["required"], ["hash"])
        self.assertEqual(set(parameters["properties"]), {"hash"})
        self.assertFalse(parameters["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
