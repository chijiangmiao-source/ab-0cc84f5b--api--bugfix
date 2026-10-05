"""End-to-end HTTP smoke used by the verify container.

Covers, over the real network API:
  * health response,
  * a legal overlapping-jitter-window capture that freezes,
  * a cooling interval gap rejected at the earliest event with a rational
    clock witness and blocking guards,
  * an illegal model (closed guards touching/overlapping) -> 422,
  * a semantically equivalent retransmission -> same verdict replayed,
  * a same-id different-content submission -> 409 conflict, original kept.
Exits 0 on full success, 1 otherwise.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

MODEL = {
    "audit_id": "SMOKE-1",
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

failures: list[str] = []


def call(method: str, url: str, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        failures.append(name)


def main(base: str) -> int:
    print("== health ==")
    s, b = call("GET", f"{base}/health")
    check("health 200", s == 200 and b.get("status") == "ok", str(b))

    print("== legal overlapping jitter window freezes ==")
    freeze_events = [
        {"event": "cmd", "relative_lower": 5, "relative_upper": 10},
        {"event": "ack", "relative_lower": 1, "relative_upper": 3}]
    s, b = call("POST", f"{base}/api/reviews",
                {"model": MODEL, "events": freeze_events})
    check("freeze 200", s == 200, f"status={s}")
    check("frozen", b.get("status") == "frozen", b.get("reason", ""))
    check("two steps evidence", len(b.get("steps", [])) == 2)
    fp_freeze = b.get("stored", {}).get("fingerprint", "")

    print("== cooling interval gap rejected at earliest event ==")
    gap_events = [
        {"event": "cmd", "relative_lower": 4, "relative_upper": 6},
        {"event": "ack", "relative_lower": 2, "relative_upper": 2}]
    gap_model = json.loads(json.dumps(MODEL))
    gap_model["audit_id"] = "SMOKE-GAP"
    s, b = call("POST", f"{base}/api/reviews",
                {"model": gap_model, "events": gap_events})
    check("gap 200", s == 200)
    check("gap rejected", b.get("status") == "rejected", b.get("reason", ""))
    check("earliest event 0", b.get("earliest_event_index") == 0)
    x = b["failure"]["clock_values"]["x"]
    check("witness 9/2",
          f"{x['numerator']}/{x['denominator']}" == "9/2", x.get("text", ""))
    names = {g["transition"] for g in
             b["failure"].get("blocking_guards", [])}
    check("blocking guards listed",
          names == {"t_cooldown", "t_open"}, str(names))

    print("== illegal model: closed guards overlap at x=5 ==")
    bad = json.loads(json.dumps(gap_model))
    bad["audit_id"] = "SMOKE-BAD"
    bad["transitions"][0]["guards"][0]["upper"] = 5
    s, b = call("POST", f"{base}/api/reviews",
                {"model": bad,
                 "events": [{"event": "cmd", "relative_lower": 5,
                             "relative_upper": 5}]})
    check("illegal 422", s == 422, f"status={s}")
    check("invalid_model", b.get("status") == "invalid_model")
    check("overlap code",
          b.get("error", {}).get("details", {}).get("code")
          == "overlapping_guards")

    print("== semantic-equivalent retransmission replays verdict ==")
    body1 = {"model": gap_model, "events": gap_events}
    s1, b1 = call("POST", f"{base}/api/reviews", body1)
    # reorder object keys: semantically equivalent, canonical fingerprint
    body2 = {"events": gap_events,
             "model": {k: gap_model[k] for k in reversed(list(gap_model))}}
    s2, b2 = call("POST", f"{base}/api/reviews", body2)
    check("replay 200", s2 == 200)
    check("replay flag",
          bool(b2.get("replay", {}).get(
              "semantically_equivalent_retransmission")))
    check("same verdict", b2.get("status") == b1.get("status")
          and b2.get("reason") == b1.get("reason"))

    print("== same id, different content -> conflict, evidence retained ==")
    conflict_events = [
        {"event": "cmd", "relative_lower": 0, "relative_upper": 4},
        {"event": "ack", "relative_lower": 2, "relative_upper": 2}]
    s, b = call("POST", f"{base}/api/reviews",
                {"model": gap_model, "events": conflict_events})
    check("conflict 409", s == 409, f"status={s}")
    check("conflict status", b.get("status") == "conflict")
    check("original evidence retained",
          b.get("conflict", {}).get("original_evidence", {}).get("status")
          == "rejected")
    s, b = call("GET", f"{base}/api/reviews/SMOKE-GAP")
    check("GET keeps original", s == 200 and b.get("status") == "rejected")

    if failures:
        print(f"\nSMOKE FAILURES: {failures}")
        return 1
    print("\nSMOKE: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
    sys.exit(main(base.rstrip("/")))
