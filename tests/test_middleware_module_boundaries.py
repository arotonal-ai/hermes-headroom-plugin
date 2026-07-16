import ast
import unittest
from pathlib import Path

import hermes_headroom_plugin.middleware as facade
from hermes_headroom_plugin import middleware_request, middleware_tool, observability, reduction
from hermes_headroom_plugin.provider_headroom import HeadroomReductionProvider


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "hermes_headroom_plugin"
TEST_ROOT = Path(__file__).resolve().parent


class MiddlewareModuleBoundaryTest(unittest.TestCase):
    @staticmethod
    def _local_imports(module_name: str) -> set[str]:
        path = PACKAGE_ROOT / f"{module_name}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                imports.add(node.module.split(".", 1)[0])
        return imports

    def test_legacy_facade_reexports_real_entrypoints(self):
        self.assertIs(facade.on_tool_execution, middleware_tool.on_tool_execution)
        self.assertIs(facade.on_llm_request, middleware_request.on_llm_request)
        self.assertIs(facade.compress_tool_result_for_context, reduction.compress_tool_result_for_context)
        self.assertIs(facade.remember_platform_context, observability.remember_platform_context)

    def test_legacy_private_helpers_remain_importable_during_migration(self):
        legacy_names = (
            "_EVENT_WRITE_LOCK",
            "_PLATFORM_CONTEXT_MAX",
            "_args_contain_sensitive_value",
            "_args_preview",
            "_below_min_aggregate_enabled",
            "_below_min_aggregate_key",
            "_dedupe",
            "_detect_data_class",
            "_event_dedupe_key",
            "_event_log_contains_dedupe_key",
            "_event_log_max_bytes",
            "_extract_labeled_values",
            "_extract_matching_lines",
            "_extract_urls",
            "_falsey",
            "_infer_lane",
            "_normalize_data_class",
            "_normalize_platform",
            "_prune_below_min_buffers",
            "_resolve_event_platform",
            "_rotate_event_log_if_needed",
            "_scan_text",
            "_truthy",
            "_utc_stamp",
        )
        self.assertEqual([name for name in legacy_names if not hasattr(facade, name)], [])

    def test_implementation_modules_do_not_import_compatibility_facade(self):
        for name in (
            "config.py",
            "policy.py",
            "observability.py",
            "reduction.py",
            "provider_headroom.py",
            "middleware_tool.py",
            "middleware_request.py",
        ):
            tree = ast.parse((PACKAGE_ROOT / name).read_text(encoding="utf-8"), filename=name)
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    imports.append(node.module or "")
                elif isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
            self.assertFalse(
                any(value == "middleware" or value.endswith(".middleware") for value in imports),
                f"{name} imports the compatibility facade",
            )

    def test_reduction_orchestrates_through_provider_adapter(self):
        self.assertIs(reduction.HeadroomReductionProvider, HeadroomReductionProvider)
        source = (PACKAGE_ROOT / "reduction.py").read_text(encoding="utf-8")
        self.assertNotIn("from .proxy import compress_messages", source)
        self.assertNotIn("from .proxy import readyz", source)

    def test_dependency_direction_has_no_internal_cycle(self):
        module_names = {path.stem for path in PACKAGE_ROOT.glob("*.py")}
        graph = {name: self._local_imports(name) & module_names for name in module_names}
        visiting = set()
        visited = set()

        def visit(name: str, trail: tuple[str, ...] = ()) -> None:
            if name in visiting:
                self.fail(f"internal import cycle: {' -> '.join((*trail, name))}")
            if name in visited:
                return
            visiting.add(name)
            for dependency in sorted(graph[name]):
                visit(dependency, (*trail, name))
            visiting.remove(name)
            visited.add(name)

        for module_name in sorted(module_names):
            visit(module_name)

    def test_policy_and_observability_do_not_depend_on_transport(self):
        self.assertNotIn("proxy", self._local_imports("policy"))
        self.assertNotIn("proxy", self._local_imports("observability"))
        self.assertNotIn("provider_headroom", self._local_imports("policy"))
        self.assertNotIn("provider_headroom", self._local_imports("observability"))

    def test_core_runtime_settings_do_not_bypass_effective_config(self):
        allowed = {"config.py", "wrappers.py"}
        offenders = []
        for path in PACKAGE_ROOT.glob("*.py"):
            if path.name in allowed:
                continue
            source = path.read_text(encoding="utf-8")
            if 'os.environ.get("HEADROOM_' in source or "os.environ.get('HEADROOM_" in source:
                offenders.append(path.name)
            if '__import__("os").environ' in source or "__import__('os').environ" in source:
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_tests_do_not_patch_dependencies_through_legacy_facade(self):
        forbidden = (
            "hermes_headroom_plugin.middleware.readyz",
            "hermes_headroom_plugin.middleware.compress_messages",
            "hermes_headroom_plugin.middleware.hermes_home",
            "hermes_headroom_plugin.middleware.load_context_reduction_config",
            "hermes_headroom_plugin.middleware.llm_request_compression_enabled",
        )
        offenders = []
        for path in TEST_ROOT.glob("test_*.py"):
            if path.resolve() == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8")
            for target in forbidden:
                if target in text:
                    offenders.append(f"{path.name}: {target}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
