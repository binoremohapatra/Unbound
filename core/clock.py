"""One clock for the whole project.

Why not time.monotonic(): on Windows with Python < 3.13 it ticks at ~15.6 ms, which is
useless for a 60 Hz loop. time.perf_counter() is high resolution and monotonic.

RULE: every SignalEvent.t, and every `now` passed between modules, comes from now().
Mixing time.monotonic() and time.perf_counter() values gives nonsense ages.
"""
import time

now = time.perf_counter
