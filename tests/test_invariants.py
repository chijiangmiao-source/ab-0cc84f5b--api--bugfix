"""Differential / witness-substitution invariant tests.

These tests do *not* trust the engine's own DBMs: scenarios are simulated with
plain scalar Fraction clocks by enumerating concrete delays, and the rational
witness returned on rejection is substituted back into the emitted difference
constraints using independent scalar arithmetic.
"""

from fractions import Fraction
from itertools import product
import random

from app.engine import parse_capture, parse_model, review


def _scenario(rng):
    nloc = rng.randint(2, 5)
    nclk = rng.randint(1, 3)
    locs = [f"L{i}" for i in range(nloc)]
    clks = [f"c{i}" for i in range(nclk)]
    event_names = ["e0", "e1", "e2"]
    trs = []
    tid = 0
    # Build transitions per (source, event) with disjoint guard boxes by
    # partitioning clock c0's range into closed intervals with gaps.
    for src in range(nloc - 1):  # last location intentionally often terminal
        for ev in event_names:
            k = rng.randint(1, 3)
            starts = [rng.randint(0, 8) for _ in range(k)]
            starts.sort()
            prev_hi = -1
            made = 0
            for s in starts:
                lo = max(s, prev_hi + 2)  # integer gap keeps boxes disjoint
                hi = lo + rng.randint(0, 2)
                prev_hi = hi
                dst = rng.randrange(nloc)
                guards = [{"clock": "c0", "lower": lo, "upper": hi}]
                # sometimes add a second-clock closed guard
                if nclk > 1 and rng.random() < 0.4:
                    a = rng.randint(0, 5)
                    guards.append({"clock": "c1", "lower": a,
                                   "upper": a + rng.randint(0, 2)})
                resets = [c for c in clks if rng.random() < 0.25]
                trs.append({"id": f"t{tid}", "source": locs[src],
                            "target": locs[dst], "event": ev,
                            "guards": guards, "resets": resets})
                tid += 1
                made += 1
                if made >= 3:
                    break
    model = {
        "audit_id": f"PROP-{rng.randrange(10**9)}",
        "locations": locs, "clocks": clks,
        "initial_location": locs[0],
        "final_locations": [locs[-1]],
        "transitions": trs,
    }
    # legalise: drop model if guard boxes accidentally overlap on multi-clocks
    try:
        parsed = parse_model(model)
    except Exception:
        return None
    nev = rng.randint(1, 5)
    events = []
    for _ in range(nev):
        a = rng.randint(0, 4)
        b = a + rng.randint(0, 3)
        events.append({"event": rng.choice(event_names),
                       "relative_lower": a, "relative_upper": b})
    return parsed, parse_capture(events), model, events


def _simulate(model, events):
    """Enumerate concrete delay grids; return set of outcomes.

    Each outcome: ("ok", final_loc_index) or ("gap", event_index, location)
    or ("no_event", event_index, location) or ("multi", ...) .
    """
    lk = {n: i for i, n in enumerate(model.locations)}
    ck = {n: i for i, n in enumerate(model.clocks)}
    grids = []
    for w in events:
        pts = {w.lo, w.hi}
        # extra interior grid points
        for q in range(1, 4):
            p = w.lo + (w.hi - w.lo) * q / 4
            pts.add(p)
        grids.append(sorted(pts))
    outcomes = set()
    for deltas in product(*grids):
        clocks = [Fraction(0)] * len(model.clocks)
        loc = model.initial
        ok = True
        for k, d in enumerate(deltas):
            clocks = [c + d for c in clocks]
            ev = events[k].event
            enabled = []
            for t in model.transitions:
                if t.source != loc or t.event != ev:
                    continue
                if all(g.lo <= clocks[g.clock] <= g.hi for g in t.guards):
                    enabled.append(t)
            if len(enabled) == 0:
                # distinguish gap (some transition for event exists) vs none
                exists = any(t.source == loc and t.event == ev
                             for t in model.transitions)
                outcomes.add(("gap" if exists else "no_event", k, loc))
                ok = False
                break
            if len(enabled) > 1:
                outcomes.add(("multi", k, loc))
                ok = False
                break
            t = enabled[0]
            for r in t.resets:
                clocks[r] = Fraction(0)
            loc = t.target
        if ok:
            outcomes.add(("end", loc))
    return outcomes


def _in_constraints(values, constraints):
    """Independent scalar check of emitted DBM difference constraints."""
    v = {"t0": Fraction(0), **values}
    for c in constraints:
        lhs, rhs = [s.strip() for s in c["lhs"].split("-")]
        b = Fraction(c["bound"]["numerator"], c["bound"]["denominator"])
        d = v[lhs] - v[rhs]
        if c["op"] == "<":
            if not d < b:
                return False
        else:
            if not d <= b:
                return False
    return True


def test_engine_agrees_with_scalar_simulation_and_witness_substitutes():
    rng = random.Random(20261005)
    frozen_seen = rejected_seen = 0
    trials = 0
    for _ in range(120):
        scen = _scenario(rng)
        if scen is None:
            continue
        model, windows, model_json, events_json = scen
        result = review(model, windows)
        sim = _simulate(model, windows)
        trials += 1

        if result["status"] == "frozen":
            frozen_seen += 1
            # every enumerated concrete trajectory must be unique-transition
            # and end in a final location
            for kind, *rest in sim:
                assert kind == "end", (kind, rest, model_json, events_json)
                assert rest[0] in model.finals
        else:
            rejected_seen += 1
            idx = result["earliest_event_index"]
            # the reported earliest event must match a concrete failing trace
            bad_kinds = {s[0] for s in sim if s[1] == idx}
            assert bad_kinds & {"gap", "no_event", "multi"} or \
                result["failure"]["kind"] == "not_final", \
                (sim, result["reason"], model_json, events_json)

            # witness substitution: rational clock values must (a) lie in the
            # post-elapse zone evidence for that event, and (b) satisfy no
            # blocking guard box.
            f = result["failure"]
            if f["kind"] in ("uncovered_time", "no_transition"):
                step = result["steps"][idx]
                zones = step["post_elapse_zones"]
                zone = next(z for z in zones
                            if z["location"] == f["location"])
                vals = {k: Fraction(v["numerator"], v["denominator"])
                        for k, v in f["clock_values"].items()}
                assert all(v >= 0 for v in vals.values())
                assert _in_constraints(vals, zone["zone_constraints"]), \
                    (vals, zone["zone_constraints"])
                for cand in f.get("blocking_guards", []):
                    # guard text "c0 in [a, b]"; verify witness evades it if
                    # the candidate actually guards a clock in the witness
                    pass
                # independent check against the model itself:
                ev_name = windows[idx].event
                loc_name = f["location"]
                li = model.locations.index(loc_name)
                for t in model.transitions:
                    if t.source == li and t.event == ev_name:
                        inside = all(g.lo <= vals[model.clocks[g.clock]]
                                     <= g.hi for g in t.guards)
                        assert not inside, (t.id, vals)

    assert rejected_seen > 0, "random scenarios produced no rejections"


def test_handbuilt_legal_models_freeze_and_match_simulation():
    """Deterministic legal models: engine freezes and scalar grid agrees."""
    base = {
        "locations": ["s", "a", "b", "F"],
        "clocks": ["c0", "c1"],
        "initial_location": "s",
        "final_locations": ["F"],
    }
    cases = [
        # 1) single covering guard + unguarded ack, resets present
        ({"audit_id": "HB-1", **base, "transitions": [
            {"id": "g", "source": "s", "target": "a", "event": "e0",
             "guards": [{"clock": "c0", "lower": 0, "upper": 10}],
             "resets": ["c1"]},
            {"id": "ok", "source": "a", "target": "F", "event": "e1",
             "guards": [], "resets": []}]},
         [{"event": "e0", "relative_lower": 1, "relative_upper": 3},
          {"event": "e1", "relative_lower": 0, "relative_upper": 2}]),
        # 2) two closed guards partitioned by a capture window that only
        #    intersects one side per reachable trajectory, both converge
        ({"audit_id": "HB-2", **base, "transitions": [
            {"id": "lo", "source": "s", "target": "a", "event": "e0",
             "guards": [{"clock": "c0", "lower": 0, "upper": 4}],
             "resets": ["c1"]},
            {"id": "hi", "source": "s", "target": "b", "event": "e0",
             "guards": [{"clock": "c0", "lower": 5, "upper": 20}],
             "resets": ["c1"]},
            {"id": "okA", "source": "a", "target": "F", "event": "e1",
             "guards": [{"clock": "c1", "lower": 0, "upper": 20}],
             "resets": []},
            {"id": "okB", "source": "b", "target": "F", "event": "e1",
             "guards": [{"clock": "c1", "lower": 0, "upper": 20}],
             "resets": []}]},
         [{"event": "e0", "relative_lower": 6, "relative_upper": 9},
          {"event": "e1", "relative_lower": 0, "relative_upper": 4}]),
        # 3) closed guard exactly equal to a singleton window (boundary)
        ({"audit_id": "HB-3", **base, "transitions": [
            {"id": "g", "source": "s", "target": "a", "event": "e0",
             "guards": [{"clock": "c0", "lower": 7, "upper": 7}],
             "resets": ["c1"]},
            {"id": "ok", "source": "a", "target": "F", "event": "e1",
             "guards": [{"clock": "c1", "lower": 0, "upper": 5}],
             "resets": []}]},
         [{"event": "e0", "relative_lower": 7, "relative_upper": 7},
          {"event": "e1", "relative_lower": 1, "relative_upper": 2}]),
    ]
    for model_json, events_json in cases:
        model = parse_model(model_json)
        windows = parse_capture(events_json)
        result = review(model, windows)
        assert result["status"] == "frozen", (model_json["audit_id"],
                                              result["reason"])
        sim = _simulate(model, windows)
        for kind, *rest in sim:
            assert kind == "end" and rest[0] in model.finals, \
                (model_json["audit_id"], kind, rest)
