"""Exact rational reachability review for a timed-automaton style interlock.

The reviewer is given:
  * a stable audit identifier,
  * locations (at most 8), clocks (at most 4), transitions (at most 16),
    each transition carrying a closed time guard and a reset set,
  * an initial location and a set of final locations,
  * a capture of at most 32 events arriving in capture order, each carrying a
    closed relative-time interval (relative to the previous event; the first
    relative to time 0).

For every event the reviewer:
  1. advances time by every amount inside the event's closed interval,
  2. splits on every enabled transition guard,
  3. fires the unique enabled transition and applies its clock resets,
and finally requires every possible trajectory to rest in a final location.

All arithmetic is exact (fractions.Fraction).  Zones are difference bound
matrices (DBMs) that also carry strict bits, so the complement of closed
guard boxes can be explored exactly over open cells and boundary points.

A failing review reports the earliest offending event together with concrete
rational clock values and the blocking guard(s), so the evidence can be
substituted back and checked by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable

# ---- model limits -----------------------------------------------------------

MAX_LOCATIONS = 8
MAX_CLOCKS = 4
MAX_TRANSITIONS = 16
MAX_EVENTS = 32


class ModelError(ValueError):
    """The submitted model itself is malformed (independent of any capture)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


# ---- rational helpers -------------------------------------------------------

def rat(value: Any, what: str) -> Fraction:
    """Parse a JSON number/string into an exact non-negative Fraction."""
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        raise ModelError(f"{what} must be a number")
    if isinstance(value, int):
        f = Fraction(value)
    elif isinstance(value, float):
        # Decimal rendering keeps the float's exact decimal value.
        f = Fraction(repr(value))
    elif isinstance(value, str):
        try:
            f = Fraction(value)
        except (ValueError, ZeroDivisionError):
            raise ModelError(f"{what} is not a valid rational: {value!r}")
    else:
        raise ModelError(f"{what} must be a number")
    if f < 0:
        raise ModelError(f"{what} must be non-negative")
    return f


def frac_out(f: Fraction) -> dict[str, Any]:
    return {"numerator": f.numerator, "denominator": f.denominator,
            "decimal": f"{f.numerator / f.denominator:.12g}",
            "text": (str(f.numerator) if f.denominator == 1
                     else f"{f.numerator}/{f.denominator}")}


def rat_text(f: Fraction) -> str:
    return str(f.numerator) if f.denominator == 1 \
        else f"{f.numerator}/{f.denominator}"


# ---- difference bound matrices ----------------------------------------------
#
# Index 0 is the reference clock x_0 == 0.  Entry m[i][j] is a Bound on
# x_i - x_j.  Bound(None) means +infinity; strict means "<" rather than "<=".
# Tightness ordering (see Bound.key): smaller key = tighter; for equal values
# a strict bound is tighter than a non-strict one.


@dataclass(frozen=True)
class Bound:
    value: Fraction | None  # None means +infinity
    strict: bool = False

    def key(self) -> tuple[int, Fraction, int]:
        if self.value is None:
            return (2, Fraction(0), 1)
        return (0, self.value, 0 if self.strict else 1)


def tighter(a: Bound, b: Bound) -> Bound:
    return a if a.key() <= b.key() else b


def add_bounds(a: Bound, b: Bound) -> Bound:
    if a.value is None or b.value is None:
        return Bound(None)
    return Bound(a.value + b.value, a.strict or b.strict)


@dataclass
class Constraint:
    i: int
    j: int
    bound: Bound


@dataclass
class Zone:
    n: int                    # number of real clocks (indices 1..n)
    m: list[list[Bound]]     # (n+1) x (n+1) DBM

    # -- constructors --------------------------------------------------------

    @classmethod
    def unbounded(cls, n: int) -> "Zone":
        m = [[Bound(None) for _ in range(n + 1)] for _ in range(n + 1)]
        for i in range(n + 1):
            m[i][i] = Bound(Fraction(0))
        return cls(n, m)

    @classmethod
    def initial(cls, n: int) -> "Zone":
        z = cls.unbounded(n)
        for i in range(1, n + 1):
            z.m[i][0] = Bound(Fraction(0))
            z.m[0][i] = Bound(Fraction(0))
        z.canonicalize()
        return z

    def copy(self) -> "Zone":
        return Zone(self.n, [row[:] for row in self.m])

    # -- DBM operations ------------------------------------------------------

    def canonicalize(self) -> None:
        n = self.n
        m = self.m
        for k in range(n + 1):
            mk = m[k]
            for i in range(n + 1):
                mik = m[i][k]
                if mik.value is None or i == k:
                    continue
                mi = m[i]
                for j in range(n + 1):
                    cand = add_bounds(mik, mk[j])
                    if cand.key() < mi[j].key():
                        mi[j] = cand

    def is_satisfiable(self) -> bool:
        self.canonicalize()
        for i in range(self.n + 1):
            b = self.m[i][i]
            if b.value is not None and (b.value < 0 or
                                        (b.value == 0 and b.strict)):
                return False
        return True

    def intersect(self, other: "Zone") -> "Zone":
        z = self.copy()
        for i in range(self.n + 1):
            for j in range(self.n + 1):
                z.m[i][j] = tighter(z.m[i][j], other.m[i][j])
        return z

    def with_interval(self, clock: int, lo: Fraction, hi: Fraction,
                      strict: bool = False) -> "Zone":
        """Return self ∩ {lo <=/< x_clock <=/< hi} (clock index 0-based)."""
        z = self.copy()
        i = clock + 1
        z.m[i][0] = tighter(z.m[i][0], Bound(hi, strict))
        z.m[0][i] = tighter(z.m[0][i], Bound(-lo, strict))
        return z

    def with_equals(self, clock: int, v: Fraction) -> "Zone":
        z = self.copy()
        i = clock + 1
        z.m[i][0] = Bound(v)
        z.m[0][i] = Bound(-v)
        return z

    def time_elapse(self) -> "Zone":
        z = self.copy()
        for i in range(1, z.n + 1):
            z.m[i][0] = Bound(None)
        z.canonicalize()
        return z

    def restrict_elapse_window(self, lo: Fraction, hi: Fraction) -> "Zone":
        """Image of elapsing by exactly delta in closed [lo, hi].

        A one-shot helper clock t (index n+1) is pinned to 0 before elapse,
        elapses together with the real clocks, is constrained to [lo, hi]
        afterwards and is then projected out.
        """
        n = self.n
        z = Zone.unbounded(n + 1)
        for i in range(n + 1):
            for j in range(n + 1):
                z.m[i][j] = self.m[i][j]
        z.m[n + 1][0] = Bound(Fraction(0))
        z.m[0][n + 1] = Bound(Fraction(0))
        z.canonicalize()
        for i in range(1, n + 2):
            z.m[i][0] = Bound(None)
        z.canonicalize()
        z.m[n + 1][0] = Bound(hi)
        z.m[0][n + 1] = Bound(-lo)
        z.canonicalize()
        if not z.is_satisfiable():
            empty = self.unbounded(n)
            empty.m[0][0] = Bound(Fraction(-1))
            return empty
        out = Zone.unbounded(n)
        for i in range(n + 1):
            for j in range(n + 1):
                out.m[i][j] = z.m[i][j]
        out.canonicalize()
        return out

    def reset(self, clocks: Iterable[int]) -> "Zone":
        """Reset clocks given by their DBM indices (1-based)."""
        z = self.copy()
        for r in clocks:
            for j in range(self.n + 1):
                z.m[r][j] = Bound(None) if j != r else Bound(Fraction(0))
            for i in range(self.n + 1):
                if i != r:
                    z.m[i][r] = Bound(None)
            z.m[r][0] = Bound(Fraction(0))
            z.m[0][r] = Bound(Fraction(0))
        z.canonicalize()
        return z

    # -- identity / rendering ------------------------------------------------

    def signature(self) -> tuple:
        rows = []
        for row in self.m:
            rows.append(tuple(
                None if b.value is None
                else (b.value.numerator, b.value.denominator, b.strict)
                for b in row))
        return tuple(rows)

    def to_constraints(self, clock_names: list[str]) -> list[dict[str, Any]]:
        out = []
        names = ["t0"] + clock_names
        for i in range(self.n + 1):
            for j in range(self.n + 1):
                if i == j:
                    continue
                b = self.m[i][j]
                if b.value is None:
                    continue
                out.append({
                    "lhs": f"{names[i]} - {names[j]}",
                    "op": "<" if b.strict else "<=",
                    "bound": frac_out(b.value),
                })
        return out

    # -- witnesses ------------------------------------------------------------

    def assignment(self, clock_names: list[str],
                   strict_safe: bool = False) -> dict[str, dict[str, Any]]:
        """Concrete rational clock values inside a satisfiable zone.

        With strict_safe, values are picked strictly inside open bounds
        (required for witnesses of open cells).  Non-negativity of every
        clock is itself a zone invariant maintained by elapse/reset.
        """
        self.canonicalize()
        values: dict[str, dict[str, Any]] = {}
        for idx, name in enumerate(clock_names, start=1):
            hi_b = self.m[idx][0]
            lo_b = self.m[0][idx]
            lo = -lo_b.value if lo_b.value is not None else Fraction(0)
            if hi_b.value is None:
                val = lo + 1 if (strict_safe or lo_b.strict) else lo
            else:
                hi = hi_b.value
                if hi_b.strict or lo_b.strict or strict_safe:
                    val = (lo + hi) / 2 if hi > lo else lo
                else:
                    val = hi
            values[name] = frac_out(val)
        return values


# ---- model ------------------------------------------------------------------

@dataclass(frozen=True)
class Guard:
    lo: Fraction
    hi: Fraction
    clock: int
    clock_name: str

    def as_text(self) -> str:
        return f"{self.clock_name} in [{rat_text(self.lo)}, {rat_text(self.hi)}]"


@dataclass(frozen=True)
class Transition:
    id: str
    source: int
    target: int
    event: str
    guards: tuple[Guard, ...]
    resets: frozenset[int]
    raw_index: int

    def guard_zone(self, n: int) -> Zone:
        z = Zone.unbounded(n)
        for g in self.guards:
            z = z.with_interval(g.clock, g.lo, g.hi)
        z.canonicalize()
        return z

    def contains(self, vals: dict[int, Fraction]) -> bool:
        return all(g.lo <= vals[g.clock] <= g.hi for g in self.guards)


@dataclass
class Model:
    audit_id: str
    locations: list[str]
    clocks: list[str]
    initial: int
    finals: frozenset[int]
    transitions: list[Transition]

    @property
    def n(self) -> int:
        return len(self.clocks)


@dataclass(frozen=True)
class EventWindow:
    event: str
    lo: Fraction
    hi: Fraction
    raw_index: int


# ---- parsing & validation ---------------------------------------------------

def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ModelError(msg)


def parse_model(payload: Any) -> Model:
    if not isinstance(payload, dict):
        raise ModelError("model must be an object")
    audit_id = payload.get("audit_id")
    _require(isinstance(audit_id, str) and audit_id.strip() != "",
             "audit_id must be a non-empty stable string")
    audit_id = audit_id.strip()

    locs = payload.get("locations")
    _require(isinstance(locs, list) and 1 <= len(locs) <= MAX_LOCATIONS,
             f"locations must contain 1..{MAX_LOCATIONS} entries")
    locations: list[str] = []
    seen: set[str] = set()
    for loc in locs:
        _require(isinstance(loc, str) and loc.strip() != "",
                 "location names must be non-empty strings")
        loc = loc.strip()
        _require(loc not in seen, f"duplicate location {loc!r}")
        seen.add(loc)
        locations.append(loc)

    clocks_raw = payload.get("clocks")
    _require(isinstance(clocks_raw, list)
             and 1 <= len(clocks_raw) <= MAX_CLOCKS,
             f"clocks must contain 1..{MAX_CLOCKS} entries")
    clocks: list[str] = []
    seen_c: set[str] = set()
    for c in clocks_raw:
        _require(isinstance(c, str) and c.strip() != "",
                 "clock names must be non-empty strings")
        c = c.strip()
        _require(c not in seen_c, f"duplicate clock {c!r}")
        seen_c.add(c)
        clocks.append(c)

    loc_index = {name: i for i, name in enumerate(locations)}
    clk_index = {name: i for i, name in enumerate(clocks)}

    initial = payload.get("initial_location")
    _require(isinstance(initial, str) and initial in loc_index,
             "initial_location must name a declared location")

    finals_raw = payload.get("final_locations")
    _require(isinstance(finals_raw, list) and len(finals_raw) >= 1,
             "final_locations must be a non-empty list")
    finals: set[int] = set()
    for f in finals_raw:
        _require(isinstance(f, str) and f in loc_index,
                 "final_locations entries must be declared locations")
        _require(loc_index[f] not in finals,
                 f"duplicate final location {f!r}")
        finals.add(loc_index[f])

    tr_raw = payload.get("transitions")
    _require(isinstance(tr_raw, list) and 0 < len(tr_raw) <= MAX_TRANSITIONS,
             f"transitions must contain 1..{MAX_TRANSITIONS} entries")

    transitions: list[Transition] = []
    for k, t in enumerate(tr_raw):
        where = f"transitions[{k}]"
        _require(isinstance(t, dict), f"{where} must be an object")
        tid = t.get("id")
        _require(isinstance(tid, str) and tid.strip() != "",
                 f"{where}.id must be a non-empty string")
        tid = tid.strip()
        _require(all(x.id != tid for x in transitions),
                 f"duplicate transition id {tid!r}")
        src = t.get("source")
        dst = t.get("target")
        _require(isinstance(src, str) and src in loc_index,
                 f"{where}.source must be a declared location")
        _require(isinstance(dst, str) and dst in loc_index,
                 f"{where}.target must be a declared location")
        event = t.get("event")
        _require(isinstance(event, str) and event.strip() != "",
                 f"{where}.event must be a non-empty string")
        event = event.strip()

        guards_raw = t.get("guards", [])
        _require(isinstance(guards_raw, list)
                 and len(guards_raw) <= len(clocks),
                 f"{where}.guards malformed")
        guards: list[Guard] = []
        guarded_clocks: set[int] = set()
        for g in guards_raw:
            _require(isinstance(g, dict),
                     f"{where}.guards entries must be objects")
            cname = g.get("clock")
            _require(isinstance(cname, str) and cname in clk_index,
                     f"{where}: guard clock must be a declared clock")
            ci = clk_index[cname]
            _require(ci not in guarded_clocks,
                     f"{where}: repeated guard on clock {cname!r}")
            guarded_clocks.add(ci)
            lo = rat(g.get("lower"), f"{where}.guard.lower")
            hi = rat(g.get("upper"), f"{where}.guard.upper")
            _require(lo <= hi,
                     f"{where}: guard lower bound exceeds upper bound")
            guards.append(Guard(lo, hi, ci, cname))

        resets_raw = t.get("resets", [])
        _require(isinstance(resets_raw, list),
                 f"{where}.resets must be a list")
        resets: set[int] = set()
        for r in resets_raw:
            _require(isinstance(r, str) and r in clk_index,
                     f"{where}.resets entries must be declared clocks")
            _require(clk_index[r] not in resets,
                     f"{where}: duplicated reset {r!r}")
            resets.add(clk_index[r])

        transitions.append(Transition(
            id=tid, source=loc_index[src], target=loc_index[dst],
            event=event, guards=tuple(guards),
            resets=frozenset(resets), raw_index=k))

    model = Model(audit_id=audit_id, locations=locations, clocks=clocks,
                  initial=loc_index[initial], finals=frozenset(finals),
                  transitions=transitions)
    _validate_guard_overlap(model)
    return model


def _validate_guard_overlap(model: Model) -> None:
    """Guards of same-(source,event) transitions must be disjoint.

    Two closed boxes are feasible together iff their per-clock closed
    intervals all intersect (unguarded clocks are free).  Decided exactly by
    intersecting the guard DBMs.
    """
    groups: dict[tuple[int, str], list[Transition]] = {}
    for t in model.transitions:
        groups.setdefault((t.source, t.event), []).append(t)
    for ts in groups.values():
        for ai in range(len(ts)):
            for bi in range(ai + 1, len(ts)):
                a, b = ts[ai], ts[bi]
                z = a.guard_zone(model.n).intersect(b.guard_zone(model.n))
                if z.is_satisfiable():
                    raise ModelError(
                        f"transitions {a.id!r} and {b.id!r} have overlapping "
                        f"guards for event {a.event!r} at location "
                        f"{model.locations[a.source]!r}; same location/event "
                        f"guards must be disjoint closed intervals",
                        details=_overlap_details(model, a, b, z))


def _overlap_details(model: Model, a: Transition, b: Transition,
                     overlap_zone: Zone) -> dict[str, Any]:
    return {
        "code": "overlapping_guards",
        "location": model.locations[a.source],
        "event": a.event,
        "transition_a": a.id,
        "transition_b": b.id,
        "guards_a": [g.as_text() for g in a.guards],
        "guards_b": [g.as_text() for g in b.guards],
        "overlap_zone": overlap_zone.to_constraints(model.clocks),
        "clock_values": overlap_zone.assignment(model.clocks),
    }


def parse_capture(events: Any) -> list[EventWindow]:
    if not isinstance(events, list):
        raise ModelError("events must be a list")
    if not (1 <= len(events) <= MAX_EVENTS):
        raise ModelError(f"events must contain 1..{MAX_EVENTS} entries")
    out: list[EventWindow] = []
    for k, e in enumerate(events):
        if not isinstance(e, dict):
            raise ModelError(f"events[{k}] must be an object")
        name = e.get("event")
        if not (isinstance(name, str) and name.strip()):
            raise ModelError(f"events[{k}].event must be a non-empty string")
        lo = rat(e.get("relative_lower"), f"events[{k}].relative_lower")
        hi = rat(e.get("relative_upper"), f"events[{k}].relative_upper")
        if lo > hi:
            raise ModelError(f"events[{k}]: lower > upper")
        out.append(EventWindow(name.strip(), lo, hi, k))
    return out


# ---- exact guard coverage ---------------------------------------------------
#
# Guard cells are axis-aligned *closed boxes* (one closed interval per guarded
# clock, other clocks free).  To decide whether their union covers a zone we
# cut the clock space at every box boundary.  Each atomic cell fixes every
# guarded clock to either a single cut point or an open interval between
# consecutive cuts; box membership is constant throughout such a cell.  Zone
# membership is decided exactly by intersecting the DBM with the (possibly
# strict) cell constraints and testing feasibility.  At most 4 clocks are
# enumerated; unguarded clocks are extended by the witness picker.

# A single value: ("eq", v); an open interval: ("open", lo, hi) with None
# meaning unbounded on that side.
@dataclass(frozen=True)
class Atom:
    kind: str
    lo: Fraction | None
    hi: Fraction | None


def _projection_bounds(z: Zone, ci: int) -> tuple[Fraction, Fraction | None]:
    i = ci + 1
    lo = -z.m[0][i].value if z.m[0][i].value is not None else Fraction(0)
    hi = z.m[i][0].value
    return lo, hi


def _atomic_cells(z: Zone, ci: int,
                  cuts: list[Fraction]) -> list[Atom]:
    lo, hi = _projection_bounds(z, ci)
    eq_points = {p for p in cuts if lo <= p and (hi is None or p <= hi)}
    eq_points.add(lo)
    if hi is not None:
        eq_points.add(hi)
    pts = sorted(eq_points)
    atoms: list[Atom] = [Atom("eq", p, p) for p in pts]
    for a, b in zip(pts, pts[1:]):
        if b > a:
            atoms.append(Atom("open", a, b))
    if hi is None:
        atoms.append(Atom("open", pts[-1], None))
    return atoms


def _apply_atom(z: Zone, ci: int, atom: Atom) -> Zone:
    i = ci + 1
    if atom.kind == "eq":
        return z.with_equals(ci, atom.lo)
    out = z.copy()
    if atom.lo is not None:
        # x_i > lo  <=>  x_0 - x_i < -lo
        out.m[0][i] = tighter(out.m[0][i], Bound(-atom.lo, strict=True))
    if atom.hi is not None:
        out.m[i][0] = tighter(out.m[i][0], Bound(atom.hi, strict=True))
    return out


def _box_still_possible(t: Transition, ci: int, atom: Atom) -> bool:
    """Whether box t can cover any point of this one-clock atomic cell."""
    g = next((g for g in t.guards if g.clock == ci), None)
    if g is None:
        return True  # unguarded clock: the box spans everything
    if atom.kind == "eq":
        return g.lo <= atom.lo <= g.hi
    lo, hi = atom.lo, atom.hi
    if g.hi <= lo:
        return False
    if hi is not None and g.lo >= hi:
        return False
    return True


def uncovered_witness(zone: Zone, cells: list[Transition]
                      ) -> dict[int, Fraction] | None:
    """Return a point in zone \\ union(cells), or None when fully covered.

    Incremental DFS over the atomic decomposition induced by the closed box
    boundaries (cut points + open cells).  A subtree is pruned when no box
    can cover any of its points on clocks fixed so far, or when the zone
    intersection is infeasible.  Membership of a closed box is constant on
    each atomic cell, so one feasibility test per leaf is exact.
    """
    zone.canonicalize()
    if not zone.is_satisfiable():
        return None
    if not cells:
        return _pick(zone, {})

    cuts_by_clock: dict[int, set[Fraction]] = {}
    guarded: set[int] = set()
    for t in cells:
        for g in t.guards:
            guarded.add(g.clock)
            cuts_by_clock.setdefault(g.clock, set()).update({g.lo, g.hi})
    if not guarded:
        return None  # an unguarded cell covers the whole zone

    clock_order = sorted(guarded)
    choices: list[list[Atom]] = []
    for ci in clock_order:
        feasible = [at for at in _atomic_cells(zone, ci,
                                               sorted(cuts_by_clock[ci]))
                    if _apply_atom(zone, ci, at).is_satisfiable()]
        if not feasible:
            return None
        choices.append(feasible)

    def dfs(depth: int, z: Zone, fixed: dict[int, Fraction],
            possible: frozenset) -> dict[int, Fraction] | None:
        if depth == len(clock_order):
            # Box membership is constant over this atomic cell; re-check the
            # coupled zone exactly (a box may meet each one-clock cell yet
            # miss the zone through cross-clock DBM constraints).
            for k in possible:
                if z.intersect(cells[k].guard_zone(z.n)).is_satisfiable():
                    return None
            return _pick(z, fixed)
        ci = clock_order[depth]
        for at in choices[depth]:
            nz = _apply_atom(z, ci, at)
            if not nz.is_satisfiable():
                continue
            nfixed = {**fixed, ci: at.lo} if at.kind == "eq" else fixed
            np = frozenset(k for k in possible
                           if _box_still_possible(cells[k], ci, at))
            if not np:
                return _pick(nz, nfixed)
            found = dfs(depth + 1, nz, nfixed, np)
            if found is not None:
                return found
        return None

    return dfs(0, zone, {}, frozenset(range(len(cells))))


def _pick(z: Zone, fixed: dict[int, Fraction]) -> dict[int, Fraction]:
    """Pick full rational clock values inside feasible zone z.

    Clocks in `fixed` are pinned exactly; every other clock is pinned, one at
    a time, to the midpoint of its projection on the currently feasible zone.
    The projection of a convex DBM zone is an interval, so an interior
    midpoint always admits an extension; singleton projections are pinned at
    their only point.  This terminates after n exact feasibility tests and
    works for strict (open-cell) zones.
    """
    cur = z.copy()
    for ci, v in fixed.items():
        cur = cur.with_equals(ci, v)
    cur.canonicalize()
    out: dict[int, Fraction] = dict(fixed)
    for ci in range(z.n):
        if ci in out:
            continue
        i = ci + 1
        lo_b = cur.m[0][i]
        hi_b = cur.m[i][0]
        lo = -lo_b.value if lo_b.value is not None else Fraction(0)
        if hi_b.value is None:
            v = lo + 1
        elif hi_b.value <= lo:
            v = lo
        else:
            v = (lo + hi_b.value) / 2
        cur = cur.with_equals(ci, v)
        cur.canonicalize()
        out[ci] = v
    return out


def witness_to_names(model: Model, vals: dict[int, Fraction]
                     ) -> dict[str, dict[str, Any]]:
    return {model.clocks[ci]: frac_out(v) for ci, v in vals.items()}


# ---- review -----------------------------------------------------------------

@dataclass
class ReachState:
    location: int
    zone: Zone


def _render_states(model: Model, states: list[ReachState]) -> list[dict]:
    out = []
    for s in sorted(states, key=lambda x: (x.location, str(x.zone.signature()))):
        out.append({
            "location": model.locations[s.location],
            "zone_constraints": s.zone.to_constraints(model.clocks),
            "sample_clock_values": s.zone.assignment(model.clocks),
        })
    return out


def review(model: Model, events: list[EventWindow]) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    states = [ReachState(model.initial, Zone.initial(model.n))]

    for w in events:
        idx = w.raw_index

        # 1) advance time across the whole closed relative window
        advanced = [ReachState(st.location,
                               st.zone.restrict_elapse_window(w.lo, w.hi))
                    for st in states]

        by_loc: dict[int, list[Transition]] = {}
        for t in model.transitions:
            if t.event == w.event:
                by_loc.setdefault(t.source, []).append(t)

        merged: dict[tuple, ReachState] = {}
        branches_out: list[dict[str, Any]] = []
        failure: dict[str, Any] | None = None

        for st in advanced:
            candidates = by_loc.get(st.location, [])
            # 2) split on guard cells (exact closed-set membership)
            cell_zones = [(t, st.zone.intersect(t.guard_zone(model.n)))
                          for t in candidates]
            for _, cz in cell_zones:
                cz.canonicalize()
            enabled = [(t, cz) for t, cz in cell_zones
                       if cz.is_satisfiable()]

            # coverage: every possible valuation must enable exactly one cell
            gap = uncovered_witness(
                st.zone, [t for t, _ in cell_zones])
            if gap is not None:
                failure = {
                    "kind": "uncovered_time",
                    "event_index": idx,
                    "event": w.event,
                    "location": model.locations[st.location],
                    "clock_values": witness_to_names(model, gap),
                    "blocking_guards": [
                        {"transition": t.id,
                         "guards": [g.as_text() for g in t.guards]}
                        for t in candidates],
                }
                break

            if not enabled:
                failure = {
                    "kind": "no_transition",
                    "event_index": idx,
                    "event": w.event,
                    "location": model.locations[st.location],
                    "clock_values": witness_to_names(
                        model, _pick(st.zone, {})),
                    "blocking_guards": [],
                }
                break

            # disjointness was validated at parse time: at most one enabled
            # transition at each concrete valuation.
            for t, gz in enabled:
                post = gz.reset(c + 1 for c in t.resets)
                post.canonicalize()
                key = (t.target, post.signature())
                if key not in merged:
                    merged[key] = ReachState(t.target, post)
                branches_out.append({
                    "transition": t.id,
                    "source": model.locations[t.source],
                    "target": model.locations[t.target],
                    "guards": [g.as_text() for g in t.guards],
                    "resets": [model.clocks[c] for c in sorted(t.resets)],
                    "guard_zone": gz.to_constraints(model.clocks),
                    "post_reset_zone": post.to_constraints(model.clocks),
                    "sample_post_reset": post.assignment(model.clocks),
                })

        step_rec = {
            "event_index": idx,
            "event": w.event,
            "relative_window": {"lower": frac_out(w.lo),
                                "upper": frac_out(w.hi)},
            "pre_zones": _render_states(model, states),
            "post_elapse_zones": _render_states(model, advanced),
            "branches": branches_out,
        }

        if failure is not None:
            step_rec["failure"] = failure
            steps.append(step_rec)
            return _fail(model, steps, failure)

        states = list(merged.values())
        steps.append(step_rec)

    non_final = [s for s in states if s.location not in model.finals]
    if non_final:
        s0 = non_final[0]
        failure = {
            "kind": "not_final",
            "event_index": len(events) - 1,
            "location": model.locations[s0.location],
            "clock_values": witness_to_names(
                model, _pick(s0.zone, {})),
            "blocking_guards": [],
        }
        return _fail(model, steps, failure,
                     final_states=_render_states(model, states))

    return {
        "status": "frozen",
        "audit_id": model.audit_id,
        "message": "all possible trajectories take exactly one transition at "
                   "each event and terminate in a final location",
        "earliest_event_index": None,
        "failure": None,
        "final_states": _render_states(model, states),
        "steps": steps,
    }


def _fail(model: Model, steps: list[dict[str, Any]], failure: dict[str, Any],
          final_states=None) -> dict[str, Any]:
    out = {
        "status": "rejected",
        "audit_id": model.audit_id,
        "earliest_event_index": failure["event_index"],
        "reason": _reason_text(failure),
        "failure": failure,
        "steps": steps,
    }
    if final_states is not None:
        out["final_states"] = final_states
    return out


def _reason_text(f: dict[str, Any]) -> str:
    kind = f.get("kind")
    idx = f.get("event_index")
    if kind == "uncovered_time":
        return (f"event[{idx}] {f.get('event')!r}: some possible real instant "
                f"at location {f.get('location')!r} enables no transition "
                f"(guard coverage gap)")
    if kind == "no_transition":
        return (f"event[{idx}] {f.get('event')!r}: no transition handles this "
                f"event at location {f.get('location')!r}")
    if kind == "not_final":
        return ("after the final event a possible trajectory rests in "
                f"non-final location {f.get('location')!r}")
    return f"event[{idx}]: review failed"
