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

    @patch("hermes_headroom_plugin.tools.retrieve")
    def test_legacy_query_is_never_forwarded_to_provider(self, call):
        call.return_value = {"success": True, "content": "exact"}
        result = json.loads(handle_headroom_retrieve({"hash": "abc123", "query": "legacy focus"}))
        self.assertTrue(result["success"])
        call.assert_called_once_with("abc123")


if __name__ == "__main__":
    unittest.main()
