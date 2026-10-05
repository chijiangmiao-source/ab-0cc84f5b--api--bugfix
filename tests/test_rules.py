"""Rule tests for the exact rational interlock review engine."""

from fractions import Fraction

import pytest

from app.engine import (MAX_CLOCKS, MAX_EVENTS, MAX_LOCATIONS,
                        MAX_TRANSITIONS, ModelError, parse_capture,
                        parse_model, review, uncovered_witness, Zone)


# ---- fixtures ---------------------------------------------------------------

def make_model(*, guards_arm=("0..4", "5..10"), finals=("done",)):
    lo1, hi1 = guards_arm[0].split("..")
    lo2, hi2 = guards_arm[1].split("..")
    return {
        "audit_id": "RULE-1",
        "locations": ["armed", "cooling", "open", "done"],
        "clocks": ["x", "y"],
        "initial_location": "armed",
        "final_locations": list(finals),
        "transitions": [
            {"id": "t_cooldown", "source": "armed", "target": "cooling",
             "event": "cmd",
             "guards": [{"clock": "x", "lower": lo1, "upper": hi1}],
             "resets": ["y"]},
            {"id": "t_open", "source": "armed", "target": "open",
             "event": "cmd",
             "guards": [{"clock": "x", "lower": lo2, "upper": hi2}],
             "resets": ["y"]},
            {"id": "t_ack_c", "source": "cooling", "target": "done",
             "event": "ack",
             "guards": [{"clock": "y", "lower": 1, "upper": 3}],
             "resets": []},
            {"id": "t_ack_o", "source": "open", "target": "done",
             "event": "ack",
             "guards": [{"clock": "y", "lower": 1, "upper": 3}],
             "resets": []},
        ],
    }


def run(payload_model, events):
    return review(parse_model(payload_model), parse_capture(events))


# ---- legal overlapping time windows ----------------------------------------

def test_legal_overlapping_windows_freezes():
    """Jitter windows overlap across the two branches' absolute schedules.

    cmd may arrive at any t in [5,10]; the two closed guards [0,4] and
    [5,10] are disjoint, yet every possible instant is covered *inside this
    window* (only t_open can fire).  ack then arrives 1..3 later and both
    branches converge to `done`.  Overlapping absolute-time windows across
    branches must still yield a unique transition per trajectory.
    """
    # Single covering guard at armed: use guards [0,10] only, two later
    # branches distinguished by a clock-reset split instead.
    model = {
        "audit_id": "OVERLAP-1",
        "locations": ["armed", "A", "B", "done"],
        "clocks": ["x", "y"],
        "initial_location": "armed",
        "final_locations": ["done"],
        "transitions": [
            # cmd t in [0,10] splits on x: early branch A, late branch B;
            # the closed intervals touch nowhere (gap (4,5)) but the capture
            # window below is entirely covered by one of them per trajectory.
            {"id": "early", "source": "armed", "target": "A", "event": "cmd",
             "guards": [{"clock": "x", "lower": 0, "upper": 4}],
             "resets": ["y"]},
            {"id": "late", "source": "armed", "target": "B", "event": "cmd",
             "guards": [{"clock": "x", "lower": 5, "upper": 20}],
             "resets": ["y"]},
            {"id": "ackA", "source": "A", "target": "done", "event": "ack",
             "guards": [{"clock": "y", "lower": 0, "upper": 20}],
             "resets": []},
            {"id": "ackB", "source": "B", "target": "done", "event": "ack",
             "guards": [{"clock": "y", "lower": 0, "upper": 20}],
             "resets": []},
        ],
    }
    # cmd in [6,9] (only `late`); ack jitter [0,4] overlaps the tail of the
    # cmd window in absolute time (ack absolute 6..13 spans values below and
    # above cmd's 9).  All trajectories must still freeze.
    events = [
        {"event": "cmd", "relative_lower": 6, "relative_upper": 9},
        {"event": "ack", "relative_lower": 0, "relative_upper": 4},
    ]
    r = run(model, events)
    assert r["status"] == "frozen", r["reason"]
    assert r["earliest_event_index"] is None
    final_locs = {s["location"] for s in r["final_states"]}
    assert final_locs == {"done"}
    # exactly one branch fired at event 0
    assert {b["transition"] for b in r["steps"][0]["branches"]} == {"late"}


def test_closed_window_midpoint_overlap_freezes():
    """Both disjoint guards used by one capture whose window is fully
    covered by their union: cmd in [2,7] = [2,4] ∪ (4,5 gap!) ... so instead
    use [2,4]∪[5,7] with no gap by guards [0,4] and [5,20]; window pieces
    [2,4] and [5,7] leave (4,5) outside the capture window — freeze."""
    model = {
        "audit_id": "OVERLAP-2",
        "locations": ["armed", "A", "B", "done"],
        "clocks": ["x"],
        "initial_location": "armed",
        "final_locations": ["done"],
        "transitions": [
            {"id": "early", "source": "armed", "target": "A", "event": "cmd",
             "guards": [{"clock": "x", "lower": 0, "upper": 4}],
             "resets": []},
            {"id": "late", "source": "armed", "target": "B", "event": "cmd",
             "guards": [{"clock": "x", "lower": 5, "upper": 20}],
             "resets": []},
            {"id": "ackA", "source": "A", "target": "done", "event": "ack",
             "guards": [], "resets": []},
            {"id": "ackB", "source": "B", "target": "done", "event": "ack",
             "guards": [], "resets": []},
        ],
    }
    # This window actually straddles the gap -> rejected, demonstrating the
    # exact boundary reasoning:
    r = run(model, [{"event": "cmd", "relative_lower": 2,
                     "relative_upper": 7},
                    {"event": "ack", "relative_lower": 0,
                     "relative_upper": 1}])
    assert r["status"] == "rejected"
    assert r["earliest_event_index"] == 0
    witness = r["failure"]["clock_values"]["x"]
    assert Fraction(witness["numerator"], witness["denominator"]) == \
        Fraction(9, 2)

    # Window pieces on both sides but never inside (4,5): split the capture
    # itself is one closed window so that cannot skip the gap; instead two
    # events each wholly on one side:
    model2 = dict(model)
    model2["transitions"] = [
        dict(t, event="cmd") if t["id"] in ("early", "late") else t
        for t in model["transitions"]
    ]
    r2 = run(model, [
        {"event": "cmd", "relative_lower": 5, "relative_upper": 7},
        {"event": "ack", "relative_lower": 0, "relative_upper": 1}])
    assert r2["status"] == "frozen"


# ---- cooling interval gap ---------------------------------------------------

def test_cooling_gap_rejected_with_exact_witness():
    r = run(make_model(), [
        {"event": "cmd", "relative_lower": 4, "relative_upper": 6},
        {"event": "ack", "relative_lower": 2, "relative_upper": 2}])
    assert r["status"] == "rejected"
    assert r["earliest_event_index"] == 0
    f = r["failure"]
    assert f["kind"] == "uncovered_time"
    assert f["location"] == "armed"
    x = Fraction(f["clock_values"]["x"]["numerator"],
                 f["clock_values"]["x"]["denominator"])
    # the witness is the midpoint of the uncovered closed-set gap (4,5)
    assert x == Fraction(9, 2)
    assert 4 < x < 5
    names = {g["transition"] for g in f["blocking_guards"]}
    assert names == {"t_cooldown", "t_open"}


def test_gap_at_second_event_reports_earliest_event_index():
    # event 0 is wholly inside one guard; event 1's window straddles a gap
    # for the reachable branch.
    events = [
        {"event": "cmd", "relative_lower": 0, "relative_upper": 4},
        {"event": "ack", "relative_lower": 4, "relative_upper": 5}]
    r = run(make_model(), events)
    assert r["status"] == "rejected"
    assert r["earliest_event_index"] == 1
    assert r["failure"]["location"] == "cooling"
    y = Fraction(r["failure"]["clock_values"]["y"]["numerator"],
                 r["failure"]["clock_values"]["y"]["denominator"])
    # ack guard is y in [1,3]; reachable window is y in [4,5] -> witness 4
    assert y == 4


def test_evidence_is_stable_across_runs():
    import json
    events = [{"event": "cmd", "relative_lower": 4,
               "relative_upper": 6}]
    a = json.dumps(run(make_model(), events), sort_keys=True)
    b = json.dumps(run(make_model(), events), sort_keys=True)
    assert a == b


# ---- closed intervals -------------------------------------------------------

def test_closed_boundaries_are_covered():
    """A capture window equal to a closed guard includes both endpoints."""
    r = run(make_model(), [
        {"event": "cmd", "relative_lower": 0, "relative_upper": 4},
        {"event": "ack", "relative_lower": 1, "relative_upper": 3}])
    assert r["status"] == "frozen"
    # the upper endpoint x=4 reached final via the cooling branch
    assert {b["transition"] for b in r["steps"][0]["branches"]} == \
        {"t_cooldown"}


def test_fractional_window_exact():
    r = run(make_model(), [
        {"event": "cmd", "relative_lower": "9/2",
         "relative_upper": "11/2"}])
    assert r["status"] == "rejected"
    x = Fraction(r["failure"]["clock_values"]["x"]["numerator"],
                 r["failure"]["clock_values"]["x"]["denominator"])
    assert x == Fraction(9, 2)


# ---- guard overlap is an illegal model --------------------------------------

def test_overlapping_guards_rejected_as_illegal_model():
    bad = make_model(guards_arm=("0..5", "5..10"))
    with pytest.raises(ModelError) as ei:
        parse_model(bad)
    assert "overlapping guards" in str(ei.value)
    d = ei.value.details
    assert d["code"] == "overlapping_guards"
    v = Fraction(d["clock_values"]["x"]["numerator"],
                 d["clock_values"]["x"]["denominator"])
    assert v == 5  # the shared boundary is the concrete overlap witness


def test_two_clock_box_overlap_detected():
    model = {
        "audit_id": "BOX", "locations": ["s", "d"],
        "clocks": ["x", "y"], "initial_location": "s",
        "final_locations": ["d"],
        "transitions": [
            {"id": "a", "source": "s", "target": "d", "event": "e",
             "guards": [{"clock": "x", "lower": 0, "upper": 2},
                        {"clock": "y", "lower": 0, "upper": 2}],
             "resets": []},
            {"id": "b", "source": "s", "target": "d", "event": "e",
             "guards": [{"clock": "x", "lower": 1, "upper": 3},
                        {"clock": "y", "lower": 1, "upper": 3}],
             "resets": []},
        ],
    }
    with pytest.raises(ModelError):
        parse_model(model)


def test_boxes_disjoint_on_one_clock_are_legal():
    # y intervals overlap but x intervals do not: boxes are disjoint.
    model = {
        "audit_id": "BOX2", "locations": ["s", "d"],
        "clocks": ["x", "y"], "initial_location": "s",
        "final_locations": ["d"],
        "transitions": [
            {"id": "a", "source": "s", "target": "d", "event": "e",
             "guards": [{"clock": "x", "lower": 0, "upper": 1},
                        {"clock": "y", "lower": 0, "upper": 9}],
             "resets": []},
            {"id": "b", "source": "s", "target": "d", "event": "e",
             "guards": [{"clock": "x", "lower": 2, "upper": 3},
                        {"clock": "y", "lower": 0, "upper": 9}],
             "resets": []},
        ],
    }
    parse_model(model)  # must not raise


# ---- resets -----------------------------------------------------------------

def test_reset_zeroes_clock_across_elapse():
    # after cmd, y was reset; even a long first delay makes y start at 0.
    r = run(make_model(), [
        {"event": "cmd", "relative_lower": 8, "relative_upper": 10},
        {"event": "ack", "relative_lower": 1, "relative_upper": 2}])
    assert r["status"] == "frozen", r["reason"]
    post = r["steps"][0]["branches"][0]["post_reset_zone"]
    # the reset zone pins y - t0 == 0
    texts = {(c["lhs"], c["bound"]["text"]) for c in post}
    assert ("y - t0", "0") in texts


# ---- final state ------------------------------------------------------------

def test_non_final_trajectory_rejected():
    # only cmd, which lands in cooling/open (neither is final)
    r = run(make_model(), [
        {"event": "cmd", "relative_lower": 5, "relative_upper": 8}])
    assert r["status"] == "rejected"
    assert r["failure"]["kind"] == "not_final"
    assert r["failure"]["location"] == "open"


# ---- generic model validation -----------------------------------------------

@pytest.mark.parametrize("mutate,needle", [
    (lambda m: m.update(locations=[]), "locations"),
    (lambda m: m.update(locations=[f"L{i}" for i in range(MAX_LOCATIONS + 1)]),
     "locations"),
    (lambda m: m.update(clocks=["a", "b", "c", "d", "e"]), "clocks"),
    (lambda m: m.update(transitions=[]), "transitions"),
    (lambda m: m.update(initial_location="nowhere"), "initial_location"),
    (lambda m: m.update(final_locations=[]), "final_locations"),
])
def test_model_limits_enforced(mutate, needle):
    m = make_model()
    mutate(m)
    with pytest.raises(ModelError) as ei:
        parse_model(m)
    assert needle in str(ei.value)


def test_capture_limits_and_order():
    m = parse_model(make_model())
    with pytest.raises(ModelError):
        parse_capture([])
    with pytest.raises(ModelError):
        parse_capture([{"event": "cmd", "relative_lower": 0,
                        "relative_upper": 0}] * (MAX_EVENTS + 1))
    with pytest.raises(ModelError):
        parse_capture([{"event": "cmd", "relative_lpper": 0,
                        "relative_upper": 0}])
    with pytest.raises(ModelError):
        parse_capture([{"event": "cmd", "relative_lower": 3,
                        "relative_upper": 2}])


# ---- DBM internals ----------------------------------------------------------

def test_zone_initial_and_elapse_window():
    z = Zone.initial(1)
    w = z.restrict_elapse_window(Fraction(2), Fraction(5))
    lo = -w.m[0][1].value
    hi = w.m[1][0].value
    assert (lo, hi) == (Fraction(2), Fraction(5))


def test_uncovered_witness_on_boxes():
    m = parse_model(make_model())
    armed = next(t for t in m.transitions if t.id == "t_open")
    cool = next(t for t in m.transitions if t.id == "t_cooldown")
    z = Zone.initial(2).restrict_elapse_window(
        Fraction(4), Fraction(6))
    w = uncovered_witness(z, [cool, armed])
    assert w is not None
    assert Fraction(4) < w[0] < Fraction(5)  # clock x index 0
