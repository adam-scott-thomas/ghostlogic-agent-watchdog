"""Payload redaction. Compiled-once regex pass over the serialized payload.

Cheap (single regex pass per event), conservative (we'd rather drop a
payload than leak a secret), and configurable (operator can add patterns
to the existing default list)."""
from __future__ import annotations
import json
import re
from typing import Any


class Redactor:
    def __init__(self, patterns: tuple[str, ...]):
        # Compile each pattern separately so we can index them in the
        # replacement marker — useful for forensic review when an operator
        # asks "which rule fired?"
        self._patterns: list[re.Pattern[str]] = [
            re.compile(p) for p in patterns
        ]

    def redact_text(self, s: str) -> str:
        for i, pat in enumerate(self._patterns):
            s = pat.sub(f"[REDACTED:{i}]", s)
        return s

    def redact_payload(self, payload: Any) -> Any:
        """Serialize payload, redact the text, return parsed-or-text.

        We work on the JSON serialization so that the patterns match
        secrets embedded anywhere — string values, dict keys, nested
        arrays. Trade-off: returns either the re-parsed dict (if still
        valid JSON after redaction) or a `{"_redacted_text": ...}`
        wrapper if redaction broke the JSON shape (which would only
        happen if a secret pattern crossed a JSON delimiter)."""
        try:
            text = json.dumps(payload, separators=(",", ":"))
        except (TypeError, ValueError):
            return {"_unserializable": True}
        redacted = self.redact_text(text)
        if redacted == text:
            return payload
        try:
            return json.loads(redacted)
        except (json.JSONDecodeError, ValueError):
            return {"_redacted_text": redacted}
