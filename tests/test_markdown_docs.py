import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
INSTALL = REPO / "INSTALL.md"
SKILL = REPO / "src" / "hermes_headroom_plugin" / "skills" / "headroom-token-cost-evaluation" / "SKILL.md"
RUNTIME_MANAGER = REPO / "docs" / "runtime-manager.md"
PORTS_AND_SERVICES = REPO / "docs" / "ports-and-services.md"
AGENT_INSTALL = REPO / "docs" / "AGENT-INSTALL.md"
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

    def test_ports_and_services_separate_defaults_overrides_and_legacy(self):
        text = PORTS_AND_SERVICES.read_text(encoding="utf-8")
        required = [
            "The Hermes plugin does not open a listener",
            "http://127.0.0.1:8787",
            "headroom-hermes-plugin.service",
            "com.headroom.hermes-plugin",
            "headroom-hermes-plugin-startup",
            "headroom-hermes-plugin-health",
            "127.0.0.1:28787",
            "Retired integration-specific default",
            "hermes-context-reduction.service",
            "Instance-specific override only",
            "hermes config get context_reduction.proxy_url",
        ]
        for needle in required:
            self.assertIn(needle, text)

    def test_operator_test_runner_and_certification_docs_are_current(self):
        docs = [
            README,
            INSTALL,
            REPO / "AGENTS.md",
            REPO / "SECURITY.md",
            REPO / "docs" / "portable-core.md",
            SKILL,
        ]
        canonical = "python scripts/run-isolated-unit-tests.py"
        stale = "uv run --isolated --no-project --with pytest --with PyYAML -- python scripts/run-isolated-unit-tests.py"
        for path in docs:
            text = path.read_text(encoding="utf-8")
            self.assertIn(canonical, text, str(path))
            self.assertNotIn(stale, text, str(path))

        readme = README.read_text(encoding="utf-8")
        agent_install = AGENT_INSTALL.read_text(encoding="utf-8")
        self.assertIn("| Ubuntu | 3.14 | ✅ | ✅ |", readme)
        self.assertIn("| Windows native | 3.14 | ✅ | ✅ |", readme)
        self.assertIn("3.11/3.14", agent_install)
        self.assertNotIn("blocking Issue #24 candidate lane", readme)
        self.assertNotIn("blocking Issue #24 candidate lane", agent_install)

    def test_install_guide_is_not_overlong(self):
        lines = INSTALL.read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(lines), 260)


if __name__ == "__main__":
    unittest.main()
