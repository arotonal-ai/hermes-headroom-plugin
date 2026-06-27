import unittest

from hermes_headroom_plugin.policy import classify_data, should_compress


class PolicyTest(unittest.TestCase):
    def test_blocked_wins(self):
        self.assertEqual(classify_data(tool="terminal", data_class="secret_or_sensitive"), "blocked")

    def test_exact_tools(self):
        self.assertEqual(classify_data(tool="read_file", data_class="raw_log"), "exact")

    def test_raw_log_compressible(self):
        self.assertTrue(should_compress(data_class="raw_log"))

    def test_final_exact(self):
        self.assertEqual(classify_data(data_class="final_packet"), "exact")


if __name__ == "__main__":
    unittest.main()
