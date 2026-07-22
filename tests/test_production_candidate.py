import copy
import json
from pathlib import Path

from agent.context_engine import ContextEngine
from hermes_headroom_plugin.config import EffectiveConfig, resolve_effective_config
from hermes_headroom_plugin.context_engine import HeadroomCompositeEngine
from hermes_headroom_plugin.lifecycle import transform_history
from hermes_headroom_plugin.middleware_request import on_llm_request
from hermes_headroom_plugin.request_shaper import RESIDENT_TOOLS, shape_request
from hermes_headroom_plugin.waterfall import classify_request, render_waterfall


def history(outputs, name="search_files", arguments=None):
    rows = [{"role": "system", "content": "synthetic authority"}]
    for i, text in enumerate(outputs):
        call_args = arguments[i] if isinstance(arguments, list) else arguments
        rows += [{"role": "assistant", "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(call_args or {})}}]},
                 {"role": "tool", "tool_call_id": f"c{i}", "content": text}]
    rows.append({"role": "user", "content": "current"})
    return rows


def durable(tool, text, digest, **kwargs):
    return f"summary for {tool} hash=durablemarker123456"


def enabled(**kwargs):
    return EffectiveConfig(lifecycle_enabled=True, lifecycle_materiality_chars=kwargs.get("materiality", 8000),
        lifecycle_hot_tool_results=kwargs.get("hot", 0), lifecycle_warm_tool_results=kwargs.get("warm", 8),
        lifecycle_aggregate_budget_chars=kwargs.get("budget", 16000))


def test_lifecycle_config_is_typed_bounded_and_defaults_inert():
    default = resolve_effective_config(raw_config={})
    assert default.lifecycle_enabled is False
    cfg = resolve_effective_config(raw_config={"lifecycle": {"enabled": True, "hot_tool_results": -3,
        "warm_tool_results": 999999, "aggregate_budget_chars": 1}})
    assert cfg.lifecycle_enabled is True and cfg.lifecycle_hot_tool_results == 0
    assert cfg.lifecycle_warm_tool_results == 10000 and cfg.lifecycle_aggregate_budget_chars == 2000


def test_waterfall_accounts_maps_responses_calls_and_is_content_free():
    req = {"input": [{"type": "function_call", "call_id": "x", "name": "search_files", "arguments": "SECRET"},
                     {"type": "function_call_output", "call_id": "x", "output": "PRIVATE"}],
           "tools": [{"type": "function", "name": "search_files", "parameters": {"type": "object"}}]}
    before = copy.deepcopy(req); report = classify_request(req, "codex_responses", {"session_id": "s", "cache_read_tokens": 2})
    rendered = render_waterfall(report)
    assert report["items"]["accounted"] == report["items"]["total"] == 2
    assert report["by_tool"]["search_files"] == 2 and req == before
    assert "SECRET" not in rendered and "PRIVATE" not in rendered and report["usage"]["cache_read_tokens"] == 2


def test_shadow_event_temp_home_metadata_only_and_request_identical(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    req = {"input": [{"role": "user", "content": "prompt-secret-xy"},
                     {"type": "function_call", "call_id": "id1", "name": "innocent", "arguments": "arg-secret-xy"},
                     {"type": "function_call_output", "call_id": "id1", "output": "output-secret-xy"}],
           "tools": [{"type": "function", "name": "innocent", "description": "schema-secret-xy"}]}
    before = copy.deepcopy(req)
    assert on_llm_request(request=req, api_mode="codex_responses", session_id="s", provider="openai") is None
    assert req == before
    path = tmp_path / "control-plane/headroom/events/headroom-events.jsonl"
    raw = path.read_text(); event = json.loads(raw)
    assert event["schema"] == "headroom.waterfall.v1" and event["items"]["accounted"] == 3
    assert not any(secret in raw for secret in ("prompt-secret", "arg-secret", "output-secret", "schema-secret"))


def test_warm_real_marker_anchors_pairing_and_stability():
    text = "path: synthetic/a.py\nstatus: failed\nrollback available\n--- a/a\n+++ b/a\n@@ line\n" + "ordinary body\n" * 900
    messages = history([text])
    a, info = transform_history(messages, hot_tool_results=0, compressor=durable)
    b, _ = transform_history(messages, hot_tool_results=0, compressor=durable)
    assert info["changed"] == 1 and a == b and a[2]["tool_call_id"] == "c0"
    for anchor in ("path: synthetic/a.py", "status: failed", "rollback", "source_sha256=", "headroom_retrieve"):
        assert anchor in a[2]["content"]


def test_lifecycle_propagates_paired_tool_arguments_to_compressor():
    text = "path: synthetic/a.py\nstatus: ok\n" + "ordinary body\n" * 900
    messages = history(
        [text],
        name="read_file",
        arguments={"path": "/tmp/synthetic-a.py", "offset": 11, "limit": 900},
    )
    seen = []

    def compressor(tool, body, digest, **kwargs):
        seen.append((tool, body, digest, kwargs))
        return "summary hash=pairedargs123456"

    out, info = transform_history(messages, hot_tool_results=0, compressor=compressor)
    assert info["changed"] == 1 and out != messages
    assert seen[0][3]["tool_args"] == {"path": "/tmp/synthetic-a.py", "offset": 11, "limit": 900}


def test_protected_head_tail_stay_exact_and_uncompressed_cold_gets_real_handle():
    first = "first protected\n" + "a" * 9000
    middle = "path: synthetic/cold.log\nstatus: failed\n" + "b" * 9000
    newest = "newest hot\n" + "c" * 9000
    messages = history([first, middle, newest])
    calls = []
    def compressor(tool, text, digest, **kwargs):
        calls.append(text)
        return "bounded summary hash=coldmarker123456"
    out, info = transform_history(messages, protect_first_n=2, protect_last_n=2,
        hot_tool_results=0, warm_tool_results=0, compressor=compressor)
    assert out[2]["content"] == first
    assert out[6]["content"] == newest
    assert len(calls) == 1 and calls[0] == middle and info["changed"] == 1
    assert "Headroom cold tool result" in out[4]["content"]
    assert "path: synthetic/cold.log" in out[4]["content"]
    assert "headroom_retrieve(hash='coldmarker123456')" in out[4]["content"]


def test_secret_mutation_and_headroom_retrieve_stay_exact():
    secret = history(["Authorization: Bearer synthetic-secret\n" + "x" * 9000], "read_file")
    out, info = transform_history(secret, hot_tool_results=0, compressor=durable)
    assert out == secret and info["blocked"] == 1
    for tool in ("patch", "write_file", "headroom_retrieve"):
        messages = history(["receipt sha256: deadbeef\n" + "x" * 9000], tool)
        assert transform_history(messages, hot_tool_results=0, compressor=durable)[0] == messages


def test_below_min_group_calls_once_transforms_and_preserves_pairing():
    calls = []
    def compressor(tool, text, digest, **kwargs):
        calls.append((tool, kwargs)); return "group summary hash=aggregate12345678"
    messages = history(["small output\n" * 350, "small output\n" * 350])
    out, info = transform_history(messages, hot_tool_results=0, aggregate_budget_chars=8000, compressor=compressor)
    assert len(calls) == 1 and calls[0][1]["aggregate"] is True and info["changed"] == 2
    assert [out[2]["tool_call_id"], out[4]["tool_call_id"]] == ["c0", "c1"]
    assert all("headroom_retrieve(hash='aggregate12345678')" in out[i]["content"] for i in (2, 4))


def test_below_min_group_does_not_merge_different_argument_windows():
    messages = history(
        ["small output\n" * 350, "small output\n" * 350],
        name="read_file",
        arguments=[{"path": "/tmp/a", "offset": 1}, {"path": "/tmp/a", "offset": 351}],
    )
    calls = []
    out, info = transform_history(
        messages,
        hot_tool_results=0,
        aggregate_budget_chars=8000,
        compressor=lambda *args, **kwargs: calls.append((args, kwargs)) or "summary hash=shouldnotrun123",
    )
    assert out == messages and info["changed"] == 0 and calls == []


def test_sub_budget_and_failed_marker_groups_stay_exact():
    messages = history(["small\n" * 100, "small\n" * 100])
    assert transform_history(messages, hot_tool_results=0, aggregate_budget_chars=8000, compressor=durable)[0] == messages
    larger = history(["small\n" * 800, "small\n" * 800])
    assert transform_history(larger, hot_tool_results=0, aggregate_budget_chars=8000,
        compressor=lambda *a, **k: "summary without marker")[0] == larger


class Builtin:
    def __init__(self): self.received = None
    def compress(self, messages, **kwargs): self.received = messages; return [{"role": "system", "content": "builtin"}]
    def update_from_response(self, usage): pass
    def on_session_start(self, *a, **k): pass
    def on_session_end(self, *a, **k): pass
    def on_session_reset(self): pass
    def update_model(self, *a, **k): pass


def test_engine_inert_fallback_deepcopy_and_no_schema():
    builtin = Builtin(); original = history(["x" * 10000])
    engine = HeadroomCompositeEngine(builtin=builtin, effective_config=EffectiveConfig())
    assert isinstance(engine, ContextEngine) and copy.deepcopy(engine).name == engine.name and engine.get_tool_schemas() == []
    assert engine.compress(original) == [{"role": "system", "content": "builtin"}] and builtin.received == original


def test_engine_deepcopy_rebinds_default_compressor_to_clone():
    engine = HeadroomCompositeEngine(effective_config=enabled())
    clone = copy.deepcopy(engine)
    assert clone._uses_default_compressor is True
    assert getattr(clone._lifecycle_compressor, "__self__", None) is clone


def test_engine_accepts_only_material_valid_lifecycle_and_adapts_surface():
    seen = []
    def injected(tool, text, digest, **kwargs): seen.append((tool, digest, kwargs)); return "tiny hash=engine123456789"
    original = history(["z" * 20000])
    engine = HeadroomCompositeEngine(protect_first_n=0, protect_last_n=0,
        effective_config=enabled(materiality=8000), lifecycle_compressor=injected)
    out = engine.compress(original)
    assert out != original and seen and len(seen[0][1]) == 64


def test_engine_no_useful_falls_back_on_original_and_backoff():
    builtin = Builtin(); calls = []; original = history(["x" * 10000])
    engine = HeadroomCompositeEngine(builtin=builtin, protect_first_n=0, protect_last_n=0,
        effective_config=enabled(),
        lifecycle_compressor=lambda *a, **k: calls.append(1) or None)
    engine.compress(original); engine.compress(original)
    assert builtin.received == original and len(calls) == 1


def test_request_shaper_responses_cow_residents_protocol_and_owner_guard():
    tools = [{"type": "function", "name": name, "parameters": {"type": "object"}} for name in sorted(RESIDENT_TOOLS)]
    tools.append({"type": "function", "name": "optional_lookup", "vendor": {"keep": True}})
    req = {"input": [{"role": "user", "content": "exact"}], "tools": tools, "stream": True, "vendor": {"opaque": 1}}
    assert shape_request(req, "codex_responses", owner="provider") is None
    before = copy.deepcopy(req); shaped = shape_request(req, "codex_responses", owner="provider", compatibility_test=True)
    assert shaped is not None
    assert req == before and shaped is not req and shaped["vendor"] == req["vendor"]
    assert shaped["tools"][0] == {"type": "tool_search"}
    values = {x["name"]: x["defer_loading"] for x in shaped["tools"] if x.get("type") == "function"}
    assert all(values[name] is False for name in RESIDENT_TOOLS) and values["optional_lookup"] is True
    assert shape_request(shaped, "codex_responses", owner="provider", compatibility_test=True) is shaped
    assert sum(x.get("type") == "tool_search" for x in shaped["tools"] if isinstance(x, dict)) == 1
    assert shape_request(req, "chat_completions", owner="provider", compatibility_test=True) is None
    assert shape_request(req, "anthropic_messages", owner="provider", compatibility_test=True) is None
    assert shape_request(req, "codex_responses", owner="provider", native_tool_search=True, compatibility_test=True) is None
