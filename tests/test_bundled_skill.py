import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "src" / "hermes_headroom_plugin" / "skills" / "headroom-token-cost-evaluation" / "SKILL.md"


def parse_frontmatter(text):
    assert text.startswith("---\n")
    match = re.search(r"\n---\s*\n", text[3:])
    assert match, "missing closing frontmatter"
    frontmatter = yaml.safe_load(text[3 : match.start() + 3])
    body = text[match.end() + 3 :]
    return frontmatter, body


class BundledSkillTest(unittest.TestCase):
    def test_skill_frontmatter_is_valid_and_does_not_paint_version(self):
        text = SKILL.read_text(encoding="utf-8")
        frontmatter, body = parse_frontmatter(text)
        self.assertEqual(frontmatter["name"], "headroom-token-cost-evaluation")
        self.assertIn("installable Hermes Headroom plugin", frontmatter["description"])
        self.assertLessEqual(len(frontmatter["description"]), 1024)
        self.assertNotIn("version", frontmatter)
        self.assertIn("# Headroom plugin operations", body)

    def test_skill_contains_portable_operating_contract(self):
        text = SKILL.read_text(encoding="utf-8")
        required = [
            "skill_view(name=\"headroom_retrieve:headroom-token-cost-evaluation\")",
            "hermes plugins install arotonal-ai/hermes-headroom-plugin --enable",
            "INSTALL_PASS",
            "RUNTIME_PARTIAL",
            "RUNTIME_FULL",
            "python scripts/test-headroom-dependency-install.py",
            "scripts/test-clean-hermes-install.sh --local",
            "generate-weekly-savings-table.py",
            "docs/metrics/weekly-savings.md",
            "headroom_retrieve",
            "headroom-ai[proxy]",
            "Do not print or advertise a plugin/skill version",
            "global/default routing",
        ]
        for needle in required:
            self.assertIn(needle, text)

    def test_skill_avoids_owner_local_paths_and_unproven_wrapper_claims(self):
        text = SKILL.read_text(encoding="utf-8")
        forbidden = [
            "/home/openclaw",
            "/home/bb",
            "owner-capabilities",
            "control-plane/projects",
            "hr-nav --dry-run",
            "hr-debug --dry-run",
            "Stage 2 lane-default promotion is active owner-locally",
        ]
        for needle in forbidden:
            self.assertNotIn(needle, text)
        self.assertIn("wrapper behavior is still a later migration stage", text)
        self.assertIn("local overlays, not portable repo guarantees", text)

    @unittest.skipIf(importlib.util.find_spec("hermes_cli") is None, "Hermes CLI package not installed in this Python environment")
    def test_plugin_skill_loads_by_qualified_name_in_temp_home(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "hermes-home"
            plugins = home / "plugins"
            plugins.mkdir(parents=True)
            target = plugins / "headroom_retrieve"
            try:
                target.symlink_to(REPO, target_is_directory=True)
            except OSError:
                shutil.copytree(REPO, target, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
            (home / "config.yaml").write_text("plugins:\n  enabled:\n    - headroom_retrieve\n", encoding="utf-8")
            code = textwrap.dedent(
                r"""
                import json
                from tools.skills_tool import skill_view
                result = json.loads(skill_view('headroom_retrieve:headroom-token-cost-evaluation', preprocess=False))
                print(json.dumps({
                    'success': result.get('success'),
                    'name': result.get('name'),
                    'readiness_status': result.get('readiness_status'),
                    'has_install': 'hermes plugins install arotonal-ai/hermes-headroom-plugin --enable' in result.get('content', ''),
                    'has_runtime_full': 'RUNTIME_FULL' in result.get('content', ''),
                    'has_version_field': '\nversion:' in result.get('content', ''),
                }, sort_keys=True))
                """
            ).strip()
            env = os.environ.copy()
            env["HERMES_HOME"] = str(home)
            env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
            proc = subprocess.run([sys.executable, "-c", code], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertTrue(data["success"], data)
            self.assertEqual(data["name"], "headroom_retrieve:headroom-token-cost-evaluation")
            self.assertEqual(data["readiness_status"], "available")
            self.assertTrue(data["has_install"], data)
            self.assertTrue(data["has_runtime_full"], data)
            self.assertFalse(data["has_version_field"], data)


if __name__ == "__main__":
    unittest.main()
