"""Durable evidence store keyed by the stable audit identifier.

A submission's *semantic content* is hashed (SHA-256 over canonicalised JSON).
The API accepts two equivalent envelopes -- ``{"model": {...}, "events": [...]}``
and the flat form with the model fields at the top level -- and both normalise
to the same parsed model/capture, so they hash identically.  Rational spellings
(1, 0.5, "1/2") are normalised as well.  A same-id submission whose semantic
content differs is a conflict: the original verdict and evidence are retained
and the conflict is reported.  Submissions whose model cannot be parsed fall
back to hashing the raw envelope.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Any

_LOCK = threading.Lock()


class Store:
    def __init__(self, path: str):
        self.path = path
        self._data: dict[str, dict[str, Any]] = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self._data = json.load(fh)
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def _flush(self) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, self.path)

    @staticmethod
    def fingerprint(payload: Any) -> str:
        canonical = json.dumps(payload, ensure_ascii=False,
                               sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def content_fingerprint(cls, model: Any = None,
                            events: Any = None) -> str:
        """Hash the parsed semantic content, envelope-independent.

        The nested and flat request representations parse to the same
        ``(model, events)`` pair and therefore share one fingerprint; key order,
        whitespace and rational spellings are irrelevant.
        """
        return cls.fingerprint({"model": model, "events": events})

    def lookup(self, audit_id: str) -> dict[str, Any] | None:
        with _LOCK:
            rec = self._data.get(audit_id)
            return json.loads(json.dumps(rec)) if rec else None

    def submit(self, audit_id: str, payload: Any, result: dict[str, Any],
               model_valid: bool,
               content: Any | None = None) -> dict[str, Any]:
        """Persist or reconcile a submission.

        Fingerprinting uses the parsed semantic ``content`` when supplied
        (nested and flat envelopes of the same review then coincide); the raw
        ``payload`` is only hashed for submissions that could not be parsed.
        Returns the response body including replay/conflict annotations.
        """
        fp = (self.content_fingerprint(**content) if content is not None
              else self.fingerprint(payload))
        with _LOCK:
            prior = self._data.get(audit_id)
            if prior is not None:
                if prior["fingerprint"] == fp:
                    body = json.loads(json.dumps(prior["result"]))
                    body["replay"] = {
                        "semantically_equivalent_retransmission": True,
                        "original_fingerprint": prior["fingerprint"],
                        "original_received_at": prior["received_at"],
                        "replayed_verdict": True,
                    }
                    return body
                # same id, different content: keep original evidence
                return {
                    "status": "conflict",
                    "audit_id": audit_id,
                    "reason": "a submission with this stable audit identifier "
                              "but different content already exists; the "
                              "original evidence is retained",
                    "conflict": {
                        "original_fingerprint": prior["fingerprint"],
                        "incoming_fingerprint": fp,
                        "original_status": prior["result"].get("status"),
                        "original_received_at": prior["received_at"],
                        "original_evidence": prior["result"],
                    },
                }

            import datetime
            record = {
                "fingerprint": fp,
                "received_at": datetime.datetime.now(datetime.timezone.utc)
                .isoformat(timespec="seconds"),
                "model_valid": model_valid,
                "result": result,
            }
            self._data[audit_id] = record
            self._flush()
            body = json.loads(json.dumps(result))
            body["stored"] = {"fingerprint": fp,
                              "received_at": record["received_at"]}
            return body
