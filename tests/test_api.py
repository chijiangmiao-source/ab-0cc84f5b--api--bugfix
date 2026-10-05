"""HTTP API tests: verdicts, replay, conflict, invalid model, health."""

import importlib
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("EVIDENCE_STORE", str(tmp_path / "evidence.json"))
    monkeypatch.setenv("HEALTH_ACK", "alive")
    from app import main
    importlib.reload(main)
    return TestClient(main.app)


MODEL = {
    "audit_id": "API-1",
    "locations": ["armed", "cooling", "open", "done"],
    "clocks": ["x", "y"],
    "initial_location": "armed",
    "final_locations": ["done"],
    "transitions": [
        {"id": "t_cooldown", "source": "armed", "target": "cooling",
         "event": "cmd",
         "guards": [{"clock": "x", "lower": 0, "upper": 4}],
         "resets": ["y"]},
        {"id": "t_open", "source": "armed", "target": "open", "event": "cmd",
         "guards": [{"clock": "x", "lower": 5, "upper": 10}],
         "resets": ["y"]},
        {"id": "t_ack_c", "source": "cooling", "target": "done",
         "event": "ack",
         "guards": [{"clock": "y", "lower": 1, "upper": 3}], "resets": []},
        {"id": "t_ack_o", "source": "open", "target": "done", "event": "ack",
         "guards": [{"clock": "y", "lower": 1, "upper": 3}], "resets": []},
    ],
}


def payload(events):
    return {"model": MODEL, "events": events}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "alive"


def test_gap_submission_rejected(client):
    r = client.post("/api/reviews", json=payload([
        {"event": "cmd", "relative_lower": 4, "relative_upper": 6},
        {"event": "ack", "relative_lower": 2, "relative_upper": 2}]))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rejected"
    assert body["earliest_event_index"] == 0
    assert body["stored"]["fingerprint"]
    assert len(body["steps"]) == 1


def test_freezing_submission(client):
    r = client.post("/api/reviews", json=payload([
        {"event": "cmd", "relative_lower": 5, "relative_upper": 10},
        {"event": "ack", "relative_lower": 1, "relative_upper": 3}]))
    assert r.status_code == 200
    assert r.json()["status"] == "frozen"


def test_semantic_equivalent_retransmission_replays(client):
    p = payload([{"event": "cmd", "relative_lower": 5,
                  "relative_upper": 10}])
    r1 = client.post("/api/reviews", json=p)
    b1 = r1.json()
    assert b1["status"] == "rejected"  # not final after one event
    # reorder JSON keys + whitespace -> semantically identical bytes content
    p2 = {"events": list(reversed(p["events"])),
          "model": dict(reversed(list(p["model"].items())))}
    # NOTE events order must stay identical; rebuild canonically instead
    p2 = {"model": {k: MODEL[k] for k in reversed(list(MODEL))},
          "events": p["events"]}
    r2 = client.post("/api/reviews", json=p2)
    b2 = r2.json()
    assert b2["status"] == "rejected"
    assert b2["replay"]["semantically_equivalent_retransmission"] is True
    assert b2["replay"]["original_fingerprint"] == b1["stored"]["fingerprint"]
    # same verdict/evidence content
    assert b2["reason"] == b1["reason"]
    assert b2["earliest_event_index"] == b1["earliest_event_index"]


# A minimal legal model: two locations, one clock, a single transition from
# the initial to the final location guarded by t in [0, 1]; a capture of one
# event whose relative window is [0, 1] freezes.
FLAT_MODEL = {
    "audit_id": "FLAT-1",
    "locations": ["init", "final"],
    "clocks": ["t"],
    "initial_location": "init",
    "final_locations": ["final"],
    "transitions": [
        {"id": "go", "source": "init", "target": "final", "event": "fire",
         "guards": [{"clock": "t", "lower": 0, "upper": 1}], "resets": []}],
}
FLAT_EVENTS = [{"event": "fire", "relative_lower": 0,
                "relative_upper": 1}]


def test_nested_then_flat_replays_first_verdict(client):
    """Nested then flat encoding of the same audit must replay, not conflict."""
    nested = {"model": FLAT_MODEL, "events": FLAT_EVENTS}
    r1 = client.post("/api/reviews", json=nested)
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert b1["status"] == "frozen"
    assert b1["stored"]["fingerprint"]

    # identical model fields and events, but the flat top-level representation
    flat = {**FLAT_MODEL, "events": FLAT_EVENTS}
    r2 = client.post("/api/reviews", json=flat)
    assert r2.status_code == 200, r2.text
    b2 = r2.json()
    assert b2["status"] == "frozen"
    assert b2["replay"]["semantically_equivalent_retransmission"] is True
    assert b2["replay"]["replayed_verdict"] is True
    assert b2["replay"]["original_fingerprint"] == b1["stored"]["fingerprint"]
    assert "conflict" not in b2


def test_flat_then_nested_replays_first_verdict(client):
    """The reverse order must be treated as the same audit retransmission."""
    r1 = client.post("/api/reviews",
                     json={**FLAT_MODEL, "events": FLAT_EVENTS})
    assert r1.status_code == 200
    fp = r1.json()["stored"]["fingerprint"]

    r2 = client.post("/api/reviews",
                     json={"model": FLAT_MODEL, "events": FLAT_EVENTS})
    assert r2.status_code == 200, r2.text
    b2 = r2.json()
    assert b2["replay"]["replayed_verdict"] is True
    assert b2["replay"]["original_fingerprint"] == fp


def test_nested_and_flat_share_the_canonical_fingerprint(client):
    from app.storage import Store
    nested = {"model": FLAT_MODEL, "events": FLAT_EVENTS}
    flat = {**FLAT_MODEL, "events": FLAT_EVENTS}
    from app.main import _split_payload
    _, _, cn = _split_payload(nested)
    _, _, cf = _split_payload(flat)
    assert Store.fingerprint(cn) == Store.fingerprint(cf)
    # sanity: the raw wrappers genuinely differ
    assert Store.fingerprint(nested) != Store.fingerprint(flat)


def test_same_id_flat_retransmission_with_different_events_conflicts(client):
    """Representation differences replay; genuinely different content does not."""
    r1 = client.post("/api/reviews",
                     json={"model": FLAT_MODEL, "events": FLAT_EVENTS})
    assert r1.status_code == 200
    fp = r1.json()["stored"]["fingerprint"]

    # same id and wrapper-independent model, but a different capture window
    other_events = [{"event": "fire", "relative_lower": 2,
                     "relative_upper": 3}]
    r2 = client.post("/api/reviews",
                     json={**FLAT_MODEL, "events": other_events})
    assert r2.status_code == 409
    b2 = r2.json()
    assert b2["status"] == "conflict"
    assert b2["conflict"]["original_fingerprint"] == fp
    assert b2["conflict"]["incoming_fingerprint"] != fp
    # original evidence retained under the stable audit id
    g = client.get("/api/reviews/FLAT-1").json()
    assert g["status"] == "frozen"


def test_same_id_different_content_conflicts_and_keeps_original(client):
    p = payload([{"event": "cmd", "relative_lower": 5,
                  "relative_upper": 10}])
    b1 = client.post("/api/reviews", json=p).json()
    p2 = payload([{"event": "cmd", "relative_lower": 0,
                   "relative_upper": 4}])
    p2["model"] = {**MODEL}
    r = client.post("/api/reviews", json=p2)
    assert r.status_code == 409
    b2 = r.json()
    assert b2["status"] == "conflict"
    assert b2["conflict"]["original_fingerprint"] == b1["stored"]["fingerprint"]
    assert b2["conflict"]["incoming_fingerprint"] != \
        b1["stored"]["fingerprint"]
    assert b2["conflict"]["original_evidence"]["status"] == b1["status"]
    # GET still returns the original retained evidence
    g = client.get("/api/reviews/API-1").json()
    assert g["status"] == b1["status"]


def test_invalid_overlap_model_reported(client):
    bad = {**MODEL, "transitions": [
        dict(t) for t in MODEL["transitions"]]}
    bad["transitions"][0]["guards"][0]["upper"] = 5  # [0,5] vs [5,10]
    r = client.post("/api/reviews", json={"model": bad, "events": [
        {"event": "cmd", "relative_lower": 5, "relative_upper": 5}]})
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "invalid_model"
    assert body["error"]["details"]["code"] == "overlapping_guards"


def test_bad_json_and_missing_fields(client):
    r = client.post("/api/reviews", content="not json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 400
    r = client.post("/api/reviews", json={"events": []})
    assert r.status_code == 422


def test_index_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "联锁捕获复核" in r.text
