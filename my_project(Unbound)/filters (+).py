"""handsfree/core/filters.py -- signal conditioning for the Unbound pipeline.

Owner: Antas.

Turns raw head-pose degrees (``SignalEvent`` values from ``webcam.py``) into
normalized -1.0..1.0 stick values that ``fastpath.py`` can emit as
``Intent(kind="stick")``. Also serves the gesture thresholds in
``calibration.json`` to ``slowpath.py``.

Per-axis pose pipeline (all in ``SignalConditioner.condition``)::

    raw degrees -> One Euro Filter -> minus neutral -> deadzone -> / range -> clamp

Design notes
------------
* Hot path is allocation-light: ``__slots__`` everywhere, floats only, no
  tuples/lists/dicts created per sample. (CPython still boxes each float
  result; that is unavoidable and cheap.)
* The hot path never touches the disk. File I/O happens only in ``reload()``
  / ``reload_if_changed()``, which you call from a low-rate tick or from
  ``config.py``'s watcher, not from the capture callback.
* Calibration is an immutable snapshot swapped by a single reference
  assignment, so the capture thread never sees a half-updated config.
* Timestamps are supplied by the caller (frame-capture monotonic time), so
  this module has no clock dependency and is fully deterministic in tests.

Requires Python 3.10+ (dataclass ``slots=True``). Standard library only.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Union

__all__ = [
    "AXES",
    "SIGNAL_TO_AXIS",
    "AxisCalibration",
    "GestureThreshold",
    "Calibration",
    "OneEuroFilter",
    "SignalConditioner",
    "load_calibration",
    "parse_calibration",
    "DEFAULT_CALIBRATION",
]

log = logging.getLogger(__name__)

_TWO_PI: float = 2.0 * math.pi
_isfinite = math.isfinite

SCHEMA_VERSION: int = 1
AXES: tuple[str, ...] = ("yaw", "pitch", "roll")
SIGNAL_TO_AXIS: Dict[str, str] = {
    "head_yaw": "yaw",
    "head_pitch": "pitch",
    "head_roll": "roll",
}

PathLike = Union[str, "os.PathLike[str]"]


# --------------------------------------------------------------------------- #
# One Euro Filter
# --------------------------------------------------------------------------- #
class OneEuroFilter:
    """Casiez et al. 1 Euro filter: adaptive low-pass for noisy tracking.

    Cutoff rises with signal speed, so a still head is heavily smoothed (low
    jitter) while a fast head turn is barely lagged.

    Parameters
    ----------
    min_cutoff:
        Cutoff in Hz at zero speed. Lower = steadier when still, more lag.
    beta:
        Speed coefficient. Higher = less lag during fast motion, more noise.
        Units are Hz per (unit/second); the signal here is degrees.
    d_cutoff:
        Cutoff in Hz for the derivative estimate.
    max_gap:
        If two samples are further apart than this many seconds (tracking
        loss), the filter re-seeds from the new sample instead of smearing
        across the gap.

    Tuning: set ``beta=0``, lower ``min_cutoff`` until a still head is steady
    enough, then raise ``beta`` until fast turns stop lagging.
    """

    __slots__ = (
        "min_cutoff", "beta", "d_cutoff", "max_gap",
        "_x", "_raw", "_dx", "_t", "_primed",
    )

    def __init__(
        self,
        min_cutoff: float = 1.5,
        beta: float = 0.15,
        d_cutoff: float = 1.0,
        max_gap: float = 0.25,
    ) -> None:
        self.min_cutoff = 1.5
        self.beta = 0.15
        self.d_cutoff = 1.0
        self.max_gap = 0.25
        self.set_params(min_cutoff, beta, d_cutoff, max_gap)
        self._x = 0.0       # last filtered value
        self._raw = 0.0     # last raw value (derivative uses raw, per the paper)
        self._dx = 0.0      # filtered derivative
        self._t = 0.0       # timestamp of last accepted sample
        self._primed = False

    def set_params(
        self,
        min_cutoff: Optional[float] = None,
        beta: Optional[float] = None,
        d_cutoff: Optional[float] = None,
        max_gap: Optional[float] = None,
    ) -> None:
        """Update parameters in place, keeping filter state (no visible jump)."""
        mc = self.min_cutoff if min_cutoff is None else float(min_cutoff)
        be = self.beta if beta is None else float(beta)
        dc = self.d_cutoff if d_cutoff is None else float(d_cutoff)
        mg = self.max_gap if max_gap is None else float(max_gap)
        if not (mc > 0.0 and dc > 0.0 and mg > 0.0 and be >= 0.0):
            raise ValueError(
                "require min_cutoff > 0, d_cutoff > 0, max_gap > 0, beta >= 0"
            )
        if not all(_isfinite(v) for v in (mc, be, dc, mg)):
            raise ValueError("filter parameters must be finite")
        self.min_cutoff, self.beta, self.d_cutoff, self.max_gap = mc, be, dc, mg

    def reset(self) -> None:
        """Forget all history; next sample passes through unchanged."""
        self._primed = False
        self._dx = 0.0

    @property
    def value(self) -> Optional[float]:
        """Last filtered value, or None if the filter has no history."""
        return self._x if self._primed else None

    def __call__(self, x: float, t: float) -> float:
        """Filter sample ``x`` taken at monotonic time ``t`` (seconds)."""
        if not _isfinite(x):
            return self._x if self._primed else 0.0

        if not self._primed or (t - self._t) > self.max_gap:
            self._x = x
            self._raw = x
            self._dx = 0.0
            self._t = t
            self._primed = True
            return x

        dt = t - self._t
        if dt <= 0.0:                      # duplicate / out-of-order timestamp
            return self._x

        # Derivative of the raw signal, low-passed at d_cutoff.
        r = _TWO_PI * self.d_cutoff * dt
        dx = self._dx + (r / (r + 1.0)) * ((x - self._raw) / dt - self._dx)

        # Speed-adaptive cutoff, then low-pass the value.
        r = _TWO_PI * (self.min_cutoff + self.beta * abs(dx)) * dt
        x_hat = self._x + (r / (r + 1.0)) * (x - self._x)

        self._x = x_hat
        self._raw = x
        self._dx = dx
        self._t = t
        return x_hat


# --------------------------------------------------------------------------- #
# Calibration model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class AxisCalibration:
    neutral: float    # degrees; head pose that maps to 0.0
    deadzone: float   # degrees around neutral that output exactly 0.0
    range: float      # degrees from neutral that map to full deflection (+/-1.0)


@dataclass(frozen=True, slots=True)
class GestureThreshold:
    high: float         # Schmitt trigger: cross upward -> active
    low: float          # Schmitt trigger: cross downward -> inactive
    debounce_ms: int


@dataclass(frozen=True, slots=True)
class Calibration:
    """Immutable validated snapshot of calibration.json."""
    axes: Mapping[str, AxisCalibration]
    thresholds: Mapping[str, GestureThreshold]


_DEFAULT_AXES: Dict[str, AxisCalibration] = {
    "yaw": AxisCalibration(neutral=0.0, deadzone=2.5, range=25.0),
    "pitch": AxisCalibration(neutral=0.0, deadzone=2.5, range=20.0),
    "roll": AxisCalibration(neutral=0.0, deadzone=3.0, range=25.0),
}
_DEFAULT_THRESHOLDS: Dict[str, GestureThreshold] = {
    "jaw_open": GestureThreshold(high=0.60, low=0.35, debounce_ms=150),
    "brow_raise": GestureThreshold(high=0.55, low=0.30, debounce_ms=150),
}
DEFAULT_CALIBRATION = Calibration(
    axes=dict(_DEFAULT_AXES), thresholds=dict(_DEFAULT_THRESHOLDS)
)


# --------------------------------------------------------------------------- #
# Calibration parsing (tolerant: field-level fallback to defaults)
# --------------------------------------------------------------------------- #
def _num(value: Any) -> Optional[float]:
    """Finite real number or None (rejects bool, str, NaN, inf)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if _isfinite(f) else None


def _section(root: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    v = root.get(key)
    if v is None:
        log.debug("calibration: section %r missing, using defaults", key)
        return {}
    if not isinstance(v, dict):
        log.warning("calibration: section %r is not an object, using defaults", key)
        return {}
    return v


def _read(
    section: Mapping[str, Any],
    key: str,
    default: float,
    label: str,
    valid: Callable[[float], bool],
) -> float:
    raw = section.get(key)
    if raw is None:
        return default
    v = _num(raw)
    if v is None or not valid(v):
        log.warning("calibration: invalid %s=%r, using default %r", label, raw, default)
        return default
    return v


def _parse_threshold(
    name: str, raw: Any, fallback: Optional[GestureThreshold]
) -> Optional[GestureThreshold]:
    if not isinstance(raw, dict):
        log.warning("calibration: thresholds.%s is not an object", name)
        return fallback
    high = _num(raw.get("high"))
    low = _num(raw.get("low"))
    deb = _num(raw.get("debounce_ms"))
    if fallback is not None:                       # known gesture: per-field merge
        high = fallback.high if high is None else high
        low = fallback.low if low is None else low
        deb = float(fallback.debounce_ms) if deb is None else deb
    if high is None or low is None or deb is None:
        log.warning("calibration: thresholds.%s incomplete, ignoring", name)
        return fallback
    if not (0.0 <= low < high <= 1.0) or deb < 0.0:
        log.warning(
            "calibration: thresholds.%s invalid (need 0 <= low < high <= 1, "
            "debounce >= 0), ignoring", name,
        )
        return fallback
    return GestureThreshold(high=high, low=low, debounce_ms=int(deb))


def parse_calibration(data: Any) -> Calibration:
    """Validate parsed JSON into a Calibration.

    Raises ValueError only if the root is not a JSON object. Everything below
    the root degrades per field: a bad ``deadzone.yaw`` falls back to 2.5 and
    leaves the rest of the file intact. Unknown gesture names in
    ``thresholds`` are accepted if fully specified and valid.
    """
    if not isinstance(data, dict):
        raise ValueError("calibration root must be a JSON object")
    if data.get("version") != SCHEMA_VERSION:
        log.warning(
            "calibration: version %r != %d, parsing best-effort",
            data.get("version"), SCHEMA_VERSION,
        )

    neutral = _section(data, "neutral")
    deadzone = _section(data, "deadzone")
    rng = _section(data, "range")

    axes: Dict[str, AxisCalibration] = {}
    for axis, d in _DEFAULT_AXES.items():
        n = _read(neutral, axis, d.neutral, f"neutral.{axis}", lambda v: True)
        dz = _read(deadzone, axis, d.deadzone, f"deadzone.{axis}", lambda v: v >= 0.0)
        rg = _read(rng, axis, d.range, f"range.{axis}", lambda v: v > 0.0)
        if rg <= dz:
            log.warning(
                "calibration: %s range (%r) must exceed deadzone (%r); "
                "using default deadzone/range", axis, rg, dz,
            )
            dz, rg = d.deadzone, d.range
        axes[axis] = AxisCalibration(neutral=n, deadzone=dz, range=rg)

    thresholds: Dict[str, GestureThreshold] = dict(_DEFAULT_THRESHOLDS)
    for name, raw in _section(data, "thresholds").items():
        parsed = _parse_threshold(str(name), raw, _DEFAULT_THRESHOLDS.get(name))
        if parsed is not None:
            thresholds[name] = parsed

    return Calibration(axes=axes, thresholds=thresholds)


def load_calibration(path: PathLike) -> Calibration:
    """Read and parse calibration.json. Raises OSError / ValueError on failure.

    Uses ``utf-8-sig`` so files saved with a BOM by Windows editors still parse.
    """
    with open(path, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    return parse_calibration(data)


# --------------------------------------------------------------------------- #
# SignalConditioner
# --------------------------------------------------------------------------- #
class SignalConditioner:
    """Per-axis head-pose conditioning plus calibration management.

    Typical use in ``fastpath.py``::

        cond = SignalConditioner("calibration.json")
        ...
        # per head_* SignalEvent (t = frame-capture time):
        stick_x = cond.condition("yaw", ev.value, ev.t)

        # low-rate tick (e.g. every 250-500 ms), NOT per frame:
        cond.reload_if_changed(now)

    ``slowpath.py`` reads ``cond.threshold("jaw_open")`` for its Schmitt
    trigger bounds and debounce time.
    """

    def __init__(
        self,
        calibration_path: Optional[PathLike] = "calibration.json",
        *,
        min_cutoff: float = 1.5,
        beta: float = 0.15,
        d_cutoff: float = 1.0,
        max_gap: float = 0.25,
        reload_interval_s: float = 0.5,
    ) -> None:
        self._path: Optional[Path] = (
            Path(calibration_path) if calibration_path is not None else None
        )
        self._filters: Dict[str, OneEuroFilter] = {
            a: OneEuroFilter(min_cutoff, beta, d_cutoff, max_gap) for a in AXES
        }
        self._cal: Calibration = DEFAULT_CALIBRATION
        # Dynamic zero: runtime neutral override per axis (None = use file's).
        self._zero: Dict[str, Optional[float]] = {a: None for a in AXES}

        self._reload_interval = float(reload_interval_s)
        self._next_check = 0.0
        self._mtime_ns: Optional[int] = None
        self._failed_mtime_ns: Optional[int] = None
        self._missing_logged = False
        self._lock = threading.Lock()

        self.reload()  # missing/corrupt at startup -> hardcoded defaults

    # ---- hot path -------------------------------------------------------- #
    def condition(self, axis: str, degrees: float, t: float) -> float:
        """Raw degrees at capture time ``t`` -> normalized -1.0..1.0.

        Non-finite input returns 0.0 (stick centered), the safe output.
        Raises KeyError for an axis not in ``AXES``.
        """
        flt = self._filters[axis]
        if not _isfinite(degrees):
            return 0.0
        smoothed = flt(degrees, t)

        cal = self._cal.axes[axis]          # one snapshot read: reload-safe
        zero = self._zero[axis]
        centered = smoothed - (cal.neutral if zero is None else zero)

        mag = -centered if centered < 0.0 else centered
        dz = cal.deadzone
        if mag <= dz:
            return 0.0
        # Rescale so output ramps from 0 at the deadzone edge to 1 at ``range``
        # (no jump when leaving the deadzone).
        out = (mag - dz) / (cal.range - dz)
        if out > 1.0:
            out = 1.0
        return out if centered > 0.0 else -out

    def condition_signal(self, name: str, degrees: float, t: float) -> float:
        """Same as ``condition`` but keyed by SignalEvent name (``head_yaw``...)."""
        return self.condition(SIGNAL_TO_AXIS[name], degrees, t)

    # ---- calibration access --------------------------------------------- #
    @property
    def calibration(self) -> Calibration:
        return self._cal

    def threshold(self, name: str) -> Optional[GestureThreshold]:
        """Gesture thresholds for ``jaw_open``, ``brow_raise``, ... or None."""
        return self._cal.thresholds.get(name)

    # ---- state control --------------------------------------------------- #
    def reset(self, axis: Optional[str] = None) -> None:
        """Clear filter history (call on face loss/regain). None = all axes."""
        for a in (AXES if axis is None else (axis,)):
            self._filters[a].reset()

    def recenter(self, axis: Optional[str] = None) -> None:
        """Dynamic zeroing: treat the current filtered pose as neutral.

        Runtime-only; calibration.json is never modified. Axes with no
        filter history are skipped. Cleared by ``clear_recenter`` or when
        calibration.json reloads (a new file means a new calibration).
        """
        for a in (AXES if axis is None else (axis,)):
            v = self._filters[a].value
            if v is not None:
                self._zero[a] = v

    def clear_recenter(self, axis: Optional[str] = None) -> None:
        for a in (AXES if axis is None else (axis,)):
            self._zero[a] = None

    def set_filter_params(self, **kwargs: float) -> None:
        """Retune all axis filters live (min_cutoff, beta, d_cutoff, max_gap)."""
        for f in self._filters.values():
            f.set_params(**kwargs)

    # ---- reload ---------------------------------------------------------- #
    def reload(self) -> bool:
        """Load calibration.json now. True if a new snapshot was applied.

        * Missing file: keeps current snapshot (defaults at startup).
        * Unreadable/malformed JSON/non-object root: keeps current snapshot.
        * Partially valid file: applied, bad fields fall back to defaults.
        """
        path = self._path
        if path is None:
            return False
        with self._lock:
            try:
                mtime = path.stat().st_mtime_ns
            except FileNotFoundError:
                if not self._missing_logged:
                    log.info("calibration file %s not found; using current values", path)
                    self._missing_logged = True
                return False
            except OSError as exc:
                log.warning("calibration stat failed (%s); keeping current values", exc)
                return False
            self._missing_logged = False

            try:
                cal = load_calibration(path)
            except Exception as exc:  # noqa: BLE001 - reload must never raise
                # Not recording mtime: the writer may still be mid-write, so
                # the next poll retries. Warn once per distinct mtime.
                if mtime != self._failed_mtime_ns:
                    log.warning(
                        "calibration reload failed (%s: %s); keeping current values",
                        type(exc).__name__, exc,
                    )
                    self._failed_mtime_ns = mtime
                return False

            self._cal = cal  # atomic reference swap
            for a in AXES:
                self._zero[a] = None
            self._mtime_ns = mtime
            self._failed_mtime_ns = None
            log.info("calibration loaded from %s", path)
            return True

    def reload_if_changed(self, now: float) -> bool:
        """Rate-limited mtime poll; call from a low-rate tick, not per frame."""
        if self._path is None or now < self._next_check:
            return False
        self._next_check = now + self._reload_interval
        try:
            mtime = self._path.stat().st_mtime_ns
        except OSError:
            return False
        if mtime == self._mtime_ns:
            return False
        return self.reload()


# --------------------------------------------------------------------------- #
# Verification demo:  python handsfree/core/filters.py
# --------------------------------------------------------------------------- #
def _demo() -> None:
    import random
    import statistics
    import tempfile
    import time

    logging.basicConfig(level=logging.WARNING, format="    [log] %(levelname)s: %(message)s")
    rng = random.Random(20260610)
    hz = 60.0
    dt = 1.0 / hz
    sigma = 0.5  # degrees of landmark jitter

    # 1. Noise suppression -------------------------------------------------- #
    print("1. Noise suppression (still head, sigma=0.5 deg, 60 Hz, 15 s)")
    f = OneEuroFilter()
    raw_s, out_s = [], []
    for i in range(900):
        x = rng.gauss(0.0, sigma)
        y = f(x, i * dt)
        if i >= 60:
            raw_s.append(x)
            out_s.append(y)
    sd_raw, sd_out = statistics.pstdev(raw_s), statistics.pstdev(out_s)
    print(f"   raw std      = {sd_raw:.3f} deg")
    print(f"   filtered std = {sd_out:.3f} deg  ({sd_raw / sd_out:.1f}x quieter)")
    assert sd_out < 0.5 * sd_raw

    # 2. Step response: why the adaptive cutoff matters --------------------- #
    print("\n2. Step response 0 -> 20 deg (clean step, time to reach 90% = 18 deg)")

    def rise_ms(flt: OneEuroFilter) -> float:
        for i in range(120):
            flt(0.0, i * dt)
        for k in range(1, 240):
            if flt(20.0, (119 + k) * dt) >= 18.0:
                return k * dt * 1000.0
        return float("inf")

    fixed = rise_ms(OneEuroFilter(min_cutoff=1.5, beta=0.0))
    adaptive = rise_ms(OneEuroFilter(min_cutoff=1.5, beta=0.15))
    print(f"   beta=0.00 (plain low-pass @1.5 Hz): {fixed:6.1f} ms")
    print(f"   beta=0.15 (1 Euro)                : {adaptive:6.1f} ms")
    assert adaptive < 0.5 * fixed

    # 3. Full pipeline through SignalConditioner ---------------------------- #
    print("\n3. SignalConditioner (defaults: yaw deadzone 2.5 deg, range 25 deg)")
    cond = SignalConditioner(calibration_path=None)
    t = 0.0
    jitter_out = []
    for _ in range(300):                       # 5 s still, +/-1 deg jitter
        jitter_out.append(cond.condition("yaw", rng.gauss(0.0, 0.5), t))
        t += dt
    print(f"   still head, max |output| = {max(abs(v) for v in jitter_out):.4f} "
          "(inside deadzone -> exactly 0)")
    assert all(v == 0.0 for v in jitter_out)

    out = 0.0
    for _ in range(180):                       # hold yaw at +20 deg for 3 s
        out = cond.condition("yaw", 20.0 + rng.gauss(0.0, sigma), t)
        t += dt
    expect = (20.0 - 2.5) / (25.0 - 2.5)
    print(f"   hold +20 deg -> {out:+.3f}  (ideal {expect:+.3f})")
    assert abs(out - expect) < 0.05

    for _ in range(180):                       # past range -> saturates
        out = cond.condition("yaw", 40.0, t)
        t += dt
    print(f"   hold +40 deg -> {out:+.3f}  (clamped)")
    assert out == 1.0
    for _ in range(180):
        out = cond.condition("yaw", -40.0, t)
        t += dt
    assert out == -1.0

    cond.reset()                               # face lost, then regained off-center
    for _ in range(120):
        cond.condition("pitch", 8.0, t)
        t += dt
    before = cond.condition("pitch", 8.0, t)
    cond.recenter("pitch")                     # dynamic zeroing at +8 deg
    after = cond.condition("pitch", 8.0, t + dt)
    print(f"   pitch held at 8 deg: before recenter {before:+.3f}, after {after:+.3f}")
    assert before > 0.0 and after == 0.0
    assert cond.condition_signal("head_roll", float("nan"), t) == 0.0

    # 4. Calibration fallbacks and hot reload ------------------------------- #
    print("\n4. Calibration: missing / partial / malformed / hot reload")
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "calibration.json"
        stamp = [1_000_000_000_000]

        def write(text: str) -> None:
            p.write_text(text, encoding="utf-8")
            stamp[0] += 10_000_000_000          # force a distinct mtime
            os.utime(p, ns=(stamp[0], stamp[0]))

        c = SignalConditioner(p)                # file does not exist
        assert c.calibration.axes["yaw"].range == 25.0
        assert c.threshold("jaw_open") == GestureThreshold(0.60, 0.35, 150)
        print("   missing file        -> hardcoded defaults")

        write('{"version": 1, "neutral": {"yaw": 5.0}, "deadzone": {"yaw": "bad", "pitch": 1.0},'
              ' "range": {"roll": -3}, "thresholds": {"jaw_open": {"high": 0.7}}}')
        assert c.reload_if_changed(100.0)
        a = c.calibration.axes
        assert a["yaw"].neutral == 5.0 and a["yaw"].deadzone == 2.5      # bad -> default
        assert a["pitch"].deadzone == 1.0 and a["roll"].range == 25.0
        assert c.threshold("jaw_open") == GestureThreshold(0.7, 0.35, 150)
        assert c.threshold("brow_raise") == GestureThreshold(0.55, 0.30, 150)
        print("   partial/invalid     -> per-field fallback, valid fields kept")

        write('{"version": 1, "neutral": {"yaw": ')       # truncated mid-write
        assert not c.reload_if_changed(200.0)
        assert c.calibration.axes["yaw"].neutral == 5.0
        print("   malformed JSON      -> keeps last good calibration")

        write('{"version": 1, "neutral": {"yaw": 7.5}}')
        assert c.reload_if_changed(300.0)
        assert c.calibration.axes["yaw"].neutral == 7.5
        print("   fixed file          -> picked up automatically")

        write("[1, 2, 3]")                                # wrong root type
        assert not c.reload_if_changed(400.0)
        assert c.calibration.axes["yaw"].neutral == 7.5

    # 5. Hot-path cost ------------------------------------------------------ #
    n = 200_000
    c2 = SignalConditioner(calibration_path=None)
    vals = [rng.gauss(10.0, 3.0) for _ in range(1024)]
    t = 0.0
    start = time.perf_counter()
    for i in range(n):
        c2.condition("yaw", vals[i & 1023], t)
        t += dt
    per_call_us = (time.perf_counter() - start) / n * 1e6
    print(f"\n5. condition() cost: {per_call_us:.2f} us/call "
          f"({per_call_us / 20000 * 100:.2f}% of the 20 ms budget; 3 axes ~ {3 * per_call_us:.1f} us)")

    print("\nAll checks passed.")


if __name__ == "__main__":
    _demo()
