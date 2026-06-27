"""Admission policy for exact, compressible, and blocked Headroom classes."""
from __future__ import annotations

EXACT_TOOLS = frozenset({"read_file", "search_files", "patch", "write_file", "headroom_retrieve"})
EXACT_CLASSES = frozenset({"final_packet", "patch_diff", "canonical_html_css", "manifest_hashes", "claim_ledger", "final_artifact"})
BLOCKED_CLASSES = frozenset({"secret_or_sensitive", "memory_profile_instruction", "protected_contamination", "system_developer_instructions"})
COMPRESSIBLE_CLASSES = frozenset({"raw_log", "worker_trace_raw", "browser_debug_trace", "ocr_raw_text", "research_corpus_raw", "qa_trace", "diagnostic_intermediate"})


def classify_data(tool: str = "", data_class: str = "", final: bool = False, sensitive: bool = False) -> str:
    """Return one of: blocked, exact, compressible, exact_bounded.

    The order matters: blocked > exact > compressible > exact_bounded.
    """
    tool = (tool or "").strip()
    data_class = (data_class or "").strip()
    if sensitive or data_class in BLOCKED_CLASSES:
        return "blocked"
    if final or tool in EXACT_TOOLS or data_class in EXACT_CLASSES:
        return "exact"
    if data_class in COMPRESSIBLE_CLASSES:
        return "compressible"
    return "exact_bounded"


def should_compress(tool: str = "", data_class: str = "", *, final: bool = False, sensitive: bool = False) -> bool:
    return classify_data(tool=tool, data_class=data_class, final=final, sensitive=sensitive) == "compressible"
