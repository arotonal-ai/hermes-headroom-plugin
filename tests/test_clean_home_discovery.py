import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class CleanHomeDiscoveryTest(unittest.TestCase):
    def test_directory_plugin_discovery_and_loads_in_temp_home(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / 'hermes-home'
            plugins = home / 'plugins'
            plugins.mkdir(parents=True)
            target = plugins / 'headroom_retrieve'
            try:
                target.symlink_to(repo, target_is_directory=True)
            except OSError:
                shutil.copytree(repo, target, ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))
            (home / 'config.yaml').write_text('plugins:\n  enabled:\n    - headroom_retrieve\n', encoding='utf-8')
            code = textwrap.dedent("""
                import json
                from hermes_cli.plugins import PluginManager
                pm = PluginManager()
                pm.discover_and_load(force=True)
                loaded = pm._plugins.get('headroom_retrieve')
                print(json.dumps({
                    'seen': loaded is not None,
                    'enabled': bool(getattr(loaded, 'enabled', False)) if loaded else False,
                    'error': getattr(loaded, 'error', None) if loaded else None,
                    'tools': getattr(loaded, 'tools_registered', []) if loaded else [],
                    'commands': getattr(loaded, 'commands_registered', []) if loaded else [],
                }, sort_keys=True))
            """)
            env = os.environ.copy()
            env['HERMES_HOME'] = str(home)
            env['PYTHONPATH'] = str(repo / 'src') + os.pathsep + env.get('PYTHONPATH', '')
            proc = subprocess.run([sys.executable, '-c', code], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertTrue(data['seen'], data)
            self.assertTrue(data['enabled'], data)
            self.assertIsNone(data['error'], data)
            self.assertIn('headroom_retrieve', data['tools'])
            self.assertIn('headroom', data['commands'])


if __name__ == '__main__':
    unittest.main()
