"""Tool schemas shown to the model."""

HEADROOM_RETRIEVE_SCHEMA = {
    "name": "headroom_retrieve",
    "description": (
        "Retrieve original uncompressed content behind a Headroom compression marker. "
        "Markers look like '[... hash=abc123]' or '<<ccr:abc123>>'. "
        "They are not file paths. Use this only when a Headroom marker is present; "
        "pass an optional query to focus very large retrieved content."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "hash": {"type": "string", "description": "CCR hash from a Headroom marker."},
            "query": {"type": "string", "description": "Optional focused retrieval query."},
        },
        "required": ["hash"],
    },
}
