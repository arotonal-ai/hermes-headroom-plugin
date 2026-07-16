"""Tool schemas shown to the model."""

HEADROOM_RETRIEVE_SCHEMA = {
    "name": "headroom_retrieve",
    "description": (
        "Retrieve original uncompressed content behind a Headroom compression marker. "
        "Markers look like '[... hash=abc123]' or '<<ccr:abc123>>'. "
        "They are not file paths. Use this only when a Headroom marker is present. "
        "The complete exact retained payload is returned for the supplied hash."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "hash": {"type": "string", "description": "CCR hash from a Headroom marker."},
        },
        "required": ["hash"],
        "additionalProperties": False,
    },
}
