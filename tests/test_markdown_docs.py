import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
INSTALL = REPO / "INSTALL.md"
SKILL = REPO / "src" / "hermes_headroom_plugin" / "skills" / "headroom-token-cost-evaluation" / "SKILL.md"


class MarkdownDocsTest(unittest.TestCase):
    def test_readme_mermaid_architecture_uses_github_safe_labels(self):
        text = README.read_text(encoding="utf-8")
        match = re.search(r"```mermaid\n(.*?)\n```", text, re.S)
        self.assertIsNotNone(match, "README must contain a Mermaid architecture diagram")
        assert match is not None
        diagram = match.group(1)
        self.assertIn('H["Hermes Agent"]', diagram)
        self.assertIn('C["/headroom status, smoke, audit"]', diagram)
        self.assertIn('R["global/default provider routing unchanged"]', diagram)
        self.assertNotIn('[/headroom status|smoke|audit]', diagram)
        self.assertNotIn('-. does not mutate .->', diagram)

    def test_docs_include_owner_instance_runtime_commands(self):
        combined = "\n".join(
            p.read_text(encoding="utf-8") for p in [README, INSTALL, SKILL]
        )
        required = [
            "hermes plugins install arotonal-ai/hermes-headroom-plugin --enable",
            "hermes gateway restart",
            "/headroom status",
            "python3 -m venv ~/.cache/hermes-headroom-venv",
            "py -m venv $env:USERPROFILE\\.cache\\hermes-headroom-venv",
            "headroom proxy --host 127.0.0.1 --port 28787",
            "/headroom smoke",
        ]
        for needle in required:
            self.assertIn(needle, combined)

    def test_install_guide_is_not_overlong(self):
        lines = INSTALL.read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(lines), 260)


if __name__ == "__main__":
    unittest.main()
