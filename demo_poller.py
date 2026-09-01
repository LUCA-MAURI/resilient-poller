#!/usr/bin/env python3
"""The two pieces working together, in the shape of a real poller.

Run it:  python3 demo_poller.py

No network and no configuration: the upstreams are simulated, so the output is
identical on any machine. Three of the five sources are broken in the three
ways that actually happen in production - a host that never answers, a host
that answers 500, and a host that answers 200 with an empty body - and the run
shows what each one costs and how it is contained.

The third one is the interesting case. A circuit breaker only ever sees
exceptions, so an upstream that cheerfully returns 200 and nothing else will
never trip it: it is "successful" forever, and the poller keeps paying for it
every cycle while quietly producing no data. If an empty body is a failure in
your domain, you have to make it one - that is what `fetch_checked` below does,
and it is the single line that turns a silent outage into a contained one.

What to watch for in the output:

  * broken hosts stop being called after `threshold` failures, so every later
    cycle against them costs 0 seconds instead of a full timeout;
  * a healthy host is never affected by a broken one - circuits are per host;
  * every failure left a structured record with a stack trace, so the last
    section answers "what breaks most often" and not just "what broke last".
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import applog
from breaker import BreakerOpen, Registry

TIMEOUT = 2.0
CYCLES = 6
SOURCES = ["good-a.example", "good-b.example",
           "timeout.example", "error500.example", "empty.example"]


class EmptyResponse(RuntimeError):
    """A 200 with nothing in it. Named so it shows up in the error report."""


class Simulated:
    """A fake clock and a fake network, so the demo is instant and repeatable."""

    def __init__(self) -> None:
        self.t = 0.0
        self.wasted = 0.0
        self.calls = 0

    def clock(self) -> float:
        return self.t

    def fetch(self, source: str) -> str:
        self.calls += 1
        if source == "timeout.example":
            self.t += TIMEOUT           # the full timeout elapses
            self.wasted += TIMEOUT
            raise TimeoutError(f"no response from {source} after {TIMEOUT}s")
        if source == "error500.example":
            self.t += 0.3
            self.wasted += 0.3
            raise RuntimeError(f"{source} returned HTTP 500")
        if source == "empty.example":
            self.t += 0.2
            self.wasted += 0.2
            return ""                   # 200 OK, and nothing in it
        self.t += 0.2
        return f"<items from {source}>"

    def fetch_checked(self, source: str) -> str:
        """Turn 'succeeded with no content' into a failure the breaker can count."""
        body = self.fetch(source)
        if not body.strip():
            raise EmptyResponse(f"{source} returned 200 with an empty body")
        return body


def main() -> None:
    net = Simulated()

    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)
        log = applog.setup("demo-poller", log_dir, stderr=False)
        breakers = Registry(threshold=2, cooldown=600, logger=log,
                            clock=net.clock)

        print(f"{CYCLES} cycles over {len(SOURCES)} sources "
              f"({TIMEOUT:.0f}s timeout on a dead host)\n")

        refused = 0
        for cycle in range(1, CYCLES + 1):
            got = []
            for source in SOURCES:
                try:
                    breakers.call(source, net.fetch_checked, source)
                except BreakerOpen as exc:
                    # Not an error: we deliberately did not ask. Logged at INFO
                    # so it never pollutes the error report.
                    refused += 1
                    log.info("skipped, circuit open",
                             extra={"source": source,
                                    "retry_in_s": round(exc.retry_in)})
                    continue
                except Exception:
                    log.exception("fetch failed", extra={"source": source})
                    continue
                got.append(source)
            print(f"  cycle {cycle}: {len(got)}/{len(SOURCES)} sources returned data")
            net.t += 60             # a minute between cycles

        possible = CYCLES * len(SOURCES)
        no_breaker = CYCLES * (TIMEOUT + 0.3 + 0.2)
        print(f"\n  network calls attempted     : {net.calls} of {possible}")
        print(f"  refused by the breaker      : {refused}")
        print(f"  seconds burned on bad hosts : {net.wasted:.1f}s "
              f"(without a breaker: {no_breaker:.1f}s)")

        print("\n  circuit state at the end of the run:")
        for row in breakers.report():
            print(f"    {row['service']:<22} {row['state']:<10} "
                  f"trips={row['trips']}  retry_in={row['retry_in_s']}s")

        print("\n  what actually broke (applog --errors):\n")
        applog.cmd_errors("demo-poller", log_dir)


if __name__ == "__main__":
    main()
