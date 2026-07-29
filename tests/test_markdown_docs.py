import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
INSTALL = REPO / "INSTALL.md"
SKILL = REPO / "src" / "hermes_headroom_plugin" / "skills" / "headroom-token-cost-evaluation" / "SKILL.md"
RUNTIME_MANAGER = REPO / "docs" / "runtime-manager.md"
AFTER_INSTALL = REPO / "after-install.md"


class MarkdownDocsTest(unittest.TestCase):
    def test_readme_mermaid_architecture_uses_github_safe_labels(self):
        text = README.read_text(encoding="utf-8")
        match = re.search(r"```mermaid\n(.*?)\n```", text, re.S)
        self.assertIsNotNone(match, "README must contain a Mermaid architecture diagram")
        assert match is not None
        diagram = match.group(1)
        self.assertIn('H["Hermes Agent"]', diagram)
        self.assertIn('C["/headroom status, setup, smoke, audit"]', diagram)
        self.assertIn('R["global/default provider routing unchanged"]', diagram)
        self.assertNotIn('[/headroom status|smoke|audit]', diagram)
        self.assertNotIn('-. does not mutate .->', diagram)

    def test_docs_include_owner_instance_runtime_commands(self):
        combined = "\n".join(
            p.read_text(encoding="utf-8")
            for p in [README, INSTALL, SKILL, RUNTIME_MANAGER, AFTER_INSTALL]
        )
        required = [
            "hermes plugins install arotonal-ai/hermes-headroom-plugin --enable",
            "hermes gateway restart",
            "/headroom status",
            "scripts/headroom-runtime.py",
            "headroom-runtime setup",
            "headroom-runtime status --json",
            "headroom-runtime doctor --json",
            "headroom-runtime uninstall --json",
            "headroom-ai[proxy]==0.32.1",
            "manual provider selection",
            "/headroom smoke",
        ]
        for needle in required:
            self.assertIn(needle, combined)

    def test_context_loop_is_bounded_and_controllable(self):
        loop_doc = (REPO / "docs" / "context-economy-loop.md").read_text(encoding="utf-8")
        required = [
            "not an autonomous meta-agent",
            "not an autonomous meta-agent, background watcher",
            "HEADROOM_AUTO_COMPRESSION=0",
            "context_reduction.auto_compression: false",
            "disables middleware auto-compression only",
            "Efficiency test for a fresh Hermes instance",
            "measure exact context chars/tokens avoided or compressed minus loop overhead",
            "A FAIL means keep the runtime/plugin compression path but remove or reduce the reporting/learning layer",
            "headroom-adoption-benchmark --samples 3",
            "ADOPT_LOOP",
            "COMPRESSION_ONLY",
            "DISABLE_LOOP_REPORTING",
            "does not mutate Hermes config",
        ]
        for needle in required:
            self.assertIn(needle, loop_doc)

    def test_runtime_manager_documents_temporal_ccr_source_authority(self):
        text = RUNTIME_MANAGER.read_text(encoding="utf-8")
        required = [
            "CCR source authority is temporal",
            "memory backend",
            "1,800-second TTL",
            "does not survive a runtime restart",
            "Markers can outlive their exact source",
            "no plugin-local exact fallback",
            "does not authorize SQLite",
        ]
        for needle in required:
            self.assertIn(needle, text)

    def test_install_guide_is_not_overlong(self):
        lines = INSTALL.read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(lines), 260)


if __name__ == "__main__":
    unittest.main()
