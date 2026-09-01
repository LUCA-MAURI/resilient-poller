#!/usr/bin/env python3
"""Circuit breaker: stop calling a service that is already failing.

The problem it solves is not correctness, it is time. A poller that fans out
over thirty upstream sources will happily retry a dead one on every cycle, for
days. Each attempt costs the full timeout - twenty seconds of nothing -
multiplied by every broken source. The job gets slower and slower because of
endpoints that are never coming back, and eventually it overruns its own
schedule and the next run starts before the previous one finished.

I hit this twice: a regional news provider that retired its feed without
notice, and a scraper whose target started answering 403. Neither raised an
alarm. Both just made everything slow.

Three states:

    closed      normal, calls pass through
    open        too many consecutive failures: calls fail instantly, without
                touching the network, for `cooldown` seconds
    half_open   cooldown elapsed, let exactly ONE probe through:
                if it works, close; if it fails, open for a full cooldown again

Typical use, inside a fetch that must never bring the service down:

    from breaker import Registry, BreakerOpen

    breakers = Registry(logger=log)

    def fetch(url):
        host = urlparse(url).netloc
        try:
            return breakers.call(host, lambda: requests.get(url, timeout=20).text)
        except BreakerOpen:
            return ""                      # quarantined, we did not even try
        except Exception:
            log.exception("fetch failed", extra={"url": url})
            return ""

State is in memory on purpose. Restarting the process resets the counters, and
that is the correct behaviour: a restart is precisely the moment you want to
give everything another chance.

    python3 breaker.py --demo    show the time saved, with numbers
    python3 breaker.py --test    self-test
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Callable, Dict, List, Optional

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

DEFAULT_THRESHOLD = 5
DEFAULT_COOLDOWN = 300.0


class BreakerOpen(Exception):
    """Raised when the circuit is open: the call was never attempted.

    A distinct exception type matters. Callers need to tell "the upstream
    failed" apart from "we chose not to ask", because only the second one is
    free and only the first one should be logged as an error.
    """

    def __init__(self, name: str, retry_in: float):
        super().__init__(f"{name}: circuit open, retrying in {retry_in:.0f}s")
        self.name = name
        self.retry_in = retry_in


class Breaker:
    """One circuit per service - in practice, one per host."""

    def __init__(self, name: str, threshold: int = DEFAULT_THRESHOLD,
                 cooldown: float = DEFAULT_COOLDOWN, logger=None,
                 clock: Callable[[], float] = time.monotonic):
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        if cooldown <= 0:
            raise ValueError("cooldown must be > 0")
        self.name = name
        self.threshold = threshold
        self.cooldown = cooldown
        self.log = logger
        # Injectable clock, and monotonic rather than wall time: the tests run
        # instantly instead of sleeping, and an NTP correction or a DST jump
        # cannot make a cooldown expire early or never.
        self.clock = clock
        self.failures = 0
        self.opened_at: Optional[float] = None
        self.trips = 0          # how many times it opened: useful in a daily report

    # ----------------------------------------------------------------- state
    @property
    def state(self) -> str:
        if self.opened_at is None:
            return CLOSED
        if self.clock() - self.opened_at >= self.cooldown:
            return HALF_OPEN
        return OPEN

    @property
    def retry_in(self) -> float:
        """Seconds until the next probe (0 if one can go through now)."""
        if self.opened_at is None:
            return 0.0
        return max(0.0, self.cooldown - (self.clock() - self.opened_at))

    # --------------------------------------------------------------- control
    def allows(self) -> bool:
        """True if a call may start right now."""
        return self.state != OPEN

    def on_success(self) -> None:
        was = self.state
        self.failures = 0
        self.opened_at = None
        if was == HALF_OPEN and self.log:
            self.log.info("circuit closed", extra={"service": self.name})

    def on_failure(self, exc: Optional[BaseException] = None) -> None:
        # A failure in half_open reopens immediately, without resetting the
        # count. Otherwise a flapping host gets a fresh budget of `threshold`
        # attempts after every probe, and the breaker never really holds.
        if self.state == HALF_OPEN:
            self.opened_at = self.clock()
            self.trips += 1
            if self.log:
                self.log.warning("probe failed, circuit reopened",
                                 extra={"service": self.name,
                                        "cause": repr(exc) if exc else None,
                                        "cooldown_s": self.cooldown})
            return

        self.failures += 1
        if self.failures >= self.threshold and self.opened_at is None:
            self.opened_at = self.clock()
            self.trips += 1
            if self.log:
                self.log.warning("circuit opened",
                                 extra={"service": self.name,
                                        "failures": self.failures,
                                        "cause": repr(exc) if exc else None,
                                        "cooldown_s": self.cooldown})

    def call(self, fn: Callable, *args, **kwargs):
        """Run fn. Raise BreakerOpen if the circuit is open.

        Exceptions from fn propagate unchanged. This class only counts; the
        caller decides what a failure means and what to do about it.
        """
        if not self.allows():
            raise BreakerOpen(self.name, self.retry_in)
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            self.on_failure(exc)
            raise
        self.on_success()
        return result


class Registry:
    """One breaker per key, made on demand. Convenient for a list of feeds."""

    def __init__(self, threshold: int = DEFAULT_THRESHOLD,
                 cooldown: float = DEFAULT_COOLDOWN, logger=None,
                 clock: Callable[[], float] = time.monotonic):
        self.opts = {"threshold": threshold, "cooldown": cooldown,
                     "logger": logger, "clock": clock}
        self.breakers: Dict[str, Breaker] = {}

    def get(self, key: str) -> Breaker:
        if key not in self.breakers:
            self.breakers[key] = Breaker(key, **self.opts)
        return self.breakers[key]

    def call(self, key: str, fn: Callable, *args, **kwargs):
        return self.get(key).call(fn, *args, **kwargs)

    def report(self) -> List[Dict]:
        """What is broken now - print at the end of a run, or in a health check."""
        return [{"service": b.name, "state": b.state, "failures": b.failures,
                 "trips": b.trips, "retry_in_s": round(b.retry_in)}
                for b in sorted(self.breakers.values(), key=lambda b: b.name)
                if b.state != CLOSED or b.trips]


def self_test() -> None:
    fake = {"t": 1000.0}

    def clock() -> float:
        return fake["t"]

    def boom():
        raise RuntimeError("host is down")

    br = Breaker("test", threshold=3, cooldown=60, clock=clock)

    # below the threshold it stays closed
    for i in range(2):
        try:
            br.call(boom)
        except RuntimeError:
            pass
        assert br.state == CLOSED, f"opened too early on attempt {i}"

    # the third failure opens it
    try:
        br.call(boom)
    except RuntimeError:
        pass
    assert br.state == OPEN and br.trips == 1

    # while open it must not touch the network: BreakerOpen, fn never runs
    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return "ok"

    try:
        br.call(counted)
    except BreakerOpen as exc:
        assert exc.retry_in == 60
    else:
        raise AssertionError("should have raised BreakerOpen")
    assert calls["n"] == 0, "called the function while the circuit was open"

    # cooldown elapsed: half_open, one probe passes
    fake["t"] += 60
    assert br.state == HALF_OPEN
    assert br.call(counted) == "ok" and calls["n"] == 1
    assert br.state == CLOSED and br.failures == 0, "success did not close it"

    # reopen, and a failed probe must restart the full cooldown
    for _ in range(3):
        try:
            br.call(boom)
        except RuntimeError:
            pass
    assert br.state == OPEN
    fake["t"] += 60
    assert br.state == HALF_OPEN
    try:
        br.call(boom)
    except RuntimeError:
        pass
    assert br.state == OPEN, "should have reopened after the failed probe"
    assert br.retry_in == 60, "the cooldown did not restart from scratch"
    assert br.trips == 3

    # one success clears scattered, non-consecutive failures
    br2 = Breaker("mixed", threshold=3, cooldown=60, clock=clock)
    for _ in range(2):
        try:
            br2.call(boom)
        except RuntimeError:
            pass
    br2.call(lambda: "fine")
    assert br2.failures == 0
    try:
        br2.call(boom)
    except RuntimeError:
        pass
    assert br2.state == CLOSED, "non-consecutive failures must not open it"

    # the registry keeps circuits independent per host
    reg = Registry(threshold=2, cooldown=30, clock=clock)
    for _ in range(2):
        try:
            reg.call("a.example", boom)
        except RuntimeError:
            pass
    reg.call("b.example", lambda: "ok")
    assert reg.get("a.example").state == OPEN
    assert reg.get("b.example").state == CLOSED, "one dead host blocked another"
    assert [r["service"] for r in reg.report()] == ["a.example"]

    # nonsense parameters rejected at construction, not at the first call
    for bad in ({"threshold": 0}, {"cooldown": 0}):
        try:
            Breaker("x", **bad)  # type: ignore[arg-type]
        except ValueError:
            pass
        else:
            raise AssertionError(f"should have rejected {bad}")

    print("OK - breaker.py")


def demo() -> None:
    """Show the saving: 10 cycles against a dead host with a 2s timeout."""
    fake = {"t": 0.0}
    lost = {"s": 0.0}

    def clock():
        return fake["t"]

    def dead_host():
        fake["t"] += 2.0          # the timeout elapses
        lost["s"] += 2.0
        raise TimeoutError("no response")

    br = Breaker("dead-host", threshold=3, cooldown=300, clock=clock)
    blocked = 0
    for _ in range(10):
        try:
            br.call(dead_host)
        except BreakerOpen:
            blocked += 1
        except TimeoutError:
            pass
        fake["t"] += 1            # gap between cycles

    print("10 cycles against a dead host (2s timeout)")
    print(f"  real attempts   : {10 - blocked}")
    print(f"  refused instantly: {blocked}")
    print(f"  seconds burned  : {lost['s']:.0f}s instead of 20s")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    if args.test:
        self_test()
        return 0
    if args.demo:
        demo()
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
