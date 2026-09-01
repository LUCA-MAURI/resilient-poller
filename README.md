# resilient-poller

**Two things a service needs when it depends on third parties that break:
a circuit breaker so a dead upstream cannot slow you down, and structured logs
so a failure leaves evidence.**

Python 3.9+, standard library only, no dependencies.

---

## The problem

A poller that fans out over thirty upstream sources will retry a dead one on
every cycle, for days. Each attempt costs the full timeout — twenty seconds of
nothing — multiplied by every broken source. The job gets slower and slower
because of endpoints that are never coming back, until it overruns its own
schedule and the next run starts before the previous one finished.

Meanwhile the code that swallowed the error (`except RequestException: items = []`)
threw away the traceback at the exact moment it existed. So you know the output
is empty. You do not know why, or since when.

Neither of these is a correctness bug. Both are the kind of thing that quietly
degrades a service until someone finally looks.

## See it, with numbers

```bash
git clone https://github.com/LUCA-MAURI/resilient-poller && cd resilient-poller
python3 demo_poller.py
```

No network, no configuration — the upstreams are simulated, so the output is
identical on any machine. Five sources, three broken in the three ways that
actually happen: a host that never answers, a host that answers 500, and a host
that answers 200 with an empty body.

```
  network calls attempted     : 18 of 30
  refused by the breaker      : 12
  seconds burned on bad hosts : 5.0s (without a breaker: 15.0s)

  circuit state at the end of the run:
    empty.example          open       trips=1  retry_in=298s
    error500.example       open       trips=1  retry_in=298s
    timeout.example        open       trips=1  retry_in=298s

  what actually broke (applog --errors):
    3x  circuit opened
    2x  TimeoutError: no response from timeout.example after 2.0s
    2x  RuntimeError: error500.example returned HTTP 500
    2x  EmptyResponse: empty.example returned 200 with an empty body
```

```bash
python3 breaker.py --demo    # the saving, in isolation
python3 breaker.py --test    # self-test
python3 applog.py --test     # self-test
```

## breaker.py

```python
from breaker import Registry, BreakerOpen

breakers = Registry(logger=log)

def fetch(url):
    host = urlparse(url).netloc
    try:
        return breakers.call(host, lambda: requests.get(url, timeout=20).text)
    except BreakerOpen:
        return ""                       # quarantined, we did not even try
    except Exception:
        log.exception("fetch failed", extra={"url": url})
        return ""
```

Three states: `closed` (calls pass), `open` (calls fail instantly, without
touching the network), `half_open` (cooldown elapsed, exactly one probe gets
through).

Design decisions worth knowing about:

- **`BreakerOpen` is its own exception type.** Callers need to tell "the
  upstream failed" apart from "we chose not to ask" — only the second one is
  free, and only the first should be logged as an error.
- **A failure in `half_open` reopens immediately, without resetting the count.**
  Otherwise a flapping host gets a fresh budget of `threshold` attempts after
  every probe and the breaker never really holds.
- **The clock is injectable and monotonic.** The tests run instantly instead of
  sleeping, and an NTP correction or a DST jump cannot make a cooldown expire
  early or never.
- **State is in memory, on purpose.** A restart resets the counters, and that is
  correct: a restart is exactly when you want to give everything another chance.

### The failure a breaker cannot see

A breaker only ever observes exceptions. An upstream that returns `200` with an
empty body is "successful" forever — it will never trip anything, and the poller
keeps paying for it every cycle while producing no data.

If an empty response is a failure in your domain, you have to make it one:

```python
def fetch_checked(url):
    body = fetch(url)
    if not body.strip():
        raise EmptyResponse(f"{url} returned 200 with an empty body")
    return body
```

That single line is the difference between a silent outage and a contained one.
`demo_poller.py` shows both sides.

## applog.py

One JSON line per event — timestamp, level, service, your own fields, and on
exceptions the **full traceback**. The file rotates itself, so an unattended box
does not fill its disk. A human-readable copy still goes to stderr, so whatever
your supervisor captures stays as useful as it was.

```python
from applog import setup
log = setup("importer")

log.info("starting", extra={"sources": 12})
try:
    ...
except Exception:
    log.exception("source is down", extra={"url": url})   # traceback included
```

```bash
python3 applog.py --tail importer --level ERROR --trace
python3 applog.py --errors importer     # grouped by error, worst first
```

`--errors` is the one worth knowing about. It answers **"what breaks most
often"**, which is a different and far more useful question than "what broke
last".

Two details that matter more than they look:

- **A field named like a reserved key gets namespaced, not merged.** Passing
  `extra={"error": ...}` would otherwise overwrite the exception field and
  silently corrupt every report built on it. There is a regression test for
  this, because it happened here first.
- **If the log file cannot be opened** — full disk, read-only volume, wrong
  permissions — it says so once and continues on stderr. Logging must never be
  the reason a service dies.

## Security

Logs are the place credentials go to be forgotten about, and log *values* come
from upstreams you do not control.

- **Log files are owner-only** (`0600`, in a `0700` directory) - **including
  after they rotate**, which is where this quietly broke the first time.
  `RotatingFileHandler` opens each new file with the process umask, so a log
  created `0600` becomes `0644` the moment it first rolls over, silently, weeks
  later. The mode is applied by the opener at creation time, and the same call
  passes `O_NOFOLLOW` so a symlink planted in the log directory is refused
  rather than followed. There is a regression test that rotates for real and
  checks every resulting file.
- **Control characters are neutralised on the way to a terminal** - on both
  paths. A hostile RSS title or API error containing ANSI escapes could
  otherwise clear your screen or forge convincing lines while you read the log.
  That covers the stored view (`--tail`, `--errors`) *and* the live stderr
  stream, which is a terminal just as often; sanitising only the first is an
  easy gap to leave. The raw bytes stay in the file - only the rendering is
  declawed - so nothing is lost for forensics.
- **Reserved keys are namespaced, not merged**, so a caller passing
  `extra={"error": ...}` cannot overwrite the real exception field and corrupt
  every report built on it.
- **A log file that cannot be opened does not stop the service.** Logging must
  never be the reason something dies.

Do not log secrets. Nothing here can tell a token from any other string; the
file modes limit the blast radius, they do not remove it. See
[SECURITY.md](SECURITY.md).

## Verify it yourself

Do not take the section above on trust - it is the kind of claim that is easy
to make and cheap to get wrong. Everything is checked by standard tools, in one
command:

```bash
./check.sh
```

| Tool | What it covers |
|---|---|
| self-tests | the behaviour each module claims, run for real |
| ruff | lint, latent bugs, and the `S` security ruleset |
| bandit | Python security scanner (OWASP-oriented) |
| mypy | static types |
| semgrep | dataflow analysis, `p/python` + `p/security-audit` |
| gitleaks | credentials, in the tree and in the history |
| shellcheck | the shell scripts |
| PSScriptAnalyzer | the PowerShell, using Microsoft's own linter |

Current status: **ALL CLEAR** on every one of them.

A tool that is not installed is skipped rather than failing, so the script is
usable before you have all of them - but a skipped tool is never counted as a
pass. The final line only says `ALL CLEAR` when every tool actually ran;
otherwise it tells you how many did not.

Where a finding is suppressed, the suppression sits next to the code with the
reason written out - `# noqa`, `# nosec`, `# nosemgrep`, or an entry in
`pyproject.toml`. There are four kinds, and no others: asserts inside
self-tests, the `subprocess` call that is the entire point of the tool, a
`urlopen` whose scheme is validated and whose redirects are refused, and a
`chmod` to `0o700` that semgrep flags because it cannot resolve the constant
and suggests the looser `0o644` instead.

## What it deliberately does not do

- **No async, and not thread-safe.** Synchronous polling loops, one thread.
  `Breaker` takes no lock: concurrent calls can race on the failure counter, so
  a shared breaker under threads may open a little late. Give each thread its
  own `Registry`, or add a lock. `asyncio` needs a different breaker.
- **No shared state between processes.** Per process, in memory. Coordinated
  breaking across a fleet is a different problem with a different answer.
- **No log shipping.** JSONL on disk. Point Vector, Filebeat or Promtail at it
  if you need it centralised.
- **No retry logic.** The breaker decides *whether* to call, not how many times.
  Retries belong to the caller, or to
  [job-watchdog](https://github.com/LUCA-MAURI/job-watchdog).

## Related

- [job-watchdog](https://github.com/LUCA-MAURI/job-watchdog) — know when a
  scheduled job stops working, including when it keeps succeeding.
- [winservice-kit](https://github.com/LUCA-MAURI/winservice-kit) — ship a Python
  service to a Windows box as a double-clickable installer.

MIT licensed. Both pieces come out of production services polling free public
APIs that go down, change shape, or quietly start returning nothing.
