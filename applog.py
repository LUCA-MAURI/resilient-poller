#!/usr/bin/env python3
"""Structured logs for services that run 24/7.

The pattern this replaces is the one every long-running script grows on its
own: print() for progress, and a bare `except: items = []` for anything that
goes wrong. When an upstream dies, nothing records what failed or when. The
supervisor sees an empty result and the traceback - the only thing that would
have told you why - was discarded at the moment it existed.

Here every event becomes one JSON line: timestamp, level, service, message,
your own extra fields, and on exceptions the full traceback. The file rotates
itself, so an unattended box does not fill its disk with logs.

    from applog import setup
    log = setup("importer")

    log.info("starting", extra={"sources": 12})
    try:
        ...
    except Exception:
        log.exception("source is down", extra={"url": url})   # traceback included

Reading them back:

    python3 applog.py --tail importer                # recent events, readable
    python3 applog.py --tail importer --level ERROR
    python3 applog.py --tail importer --trace        # with stack traces
    python3 applog.py --errors importer              # grouped by error, worst first

A human-readable copy still goes to stderr, so whatever your supervisor
captures - journald, launchd, the Windows Task Scheduler - stays as useful as
it was before.

`--errors` is the one worth knowing about: it answers "what breaks most
often", which is a different and more useful question than "what broke last".
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

MAX_BYTES = 5_000_000
BACKUPS = 3
DEFAULT_DIR = Path.home() / ".local/state/applog"
HUMAN_FIELD_MAX = 120

# Fields every LogRecord already has. Anything else is something we passed in
# via extra=, and that is exactly what we want to serialise.
_STD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message", "asctime", "taskName"}

# Keys this formatter owns. An extra= field using one of these gets prefixed.
RESERVED = {"ts", "level", "service", "msg", "error", "trace"}

# Logs routinely carry credentials by accident - a token in a URL, a
# connection string in a stack trace. The file is created owner-only for the
# same reason ~/.ssh is. On Windows this degrades to the directory ACL.
LOG_MODE = 0o600
LOG_DIR_MODE = 0o700

# Control characters, kept out of terminal output. Log values come from
# upstreams you do not control - an RSS title, an API error, a filename - and
# a value containing ANSI escapes can clear the screen, recolour text, or
# fabricate convincing lines when someone reads the log. The bytes stay intact
# in the file; only the rendering is neutralised.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def safe(text) -> str:
    """Render untrusted log content harmless for a terminal."""
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", str(text))


def _jsonable(value) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


# Never follow a symlink when creating a log file: anyone who can write into
# the log directory could otherwise redirect a log onto a file of their
# choosing. Absent on Windows, where it is not needed.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _secure_open(path, flags):
    """opener= for open(): owner-only on creation, and never via a symlink."""
    return os.open(path, flags | _NOFOLLOW, LOG_MODE)


class SecureRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler that keeps every file it creates owner-only.

    The base class opens each new file with the process umask, so a log that
    was created 0600 becomes 0644 the moment it first rotates - silently,
    weeks later, on a machine nobody is watching. Setting the mode once in
    setup() is not enough; it has to happen on every rollover.

    The mode is applied by the opener, at creation time, so there is no window
    in which the file exists with the wrong mode. Renamed backups keep the mode
    they already had.
    """

    def _open(self):
        return open(self.baseFilename, self.mode, encoding=self.encoding,
                    errors=getattr(self, "errors", None), opener=_secure_open)


class JsonFormatter(logging.Formatter):
    """One JSON line per event, traceback included."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict = {
            "ts": datetime.fromtimestamp(record.created).isoformat(timespec="seconds"),
            "level": record.levelname,
            "service": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STD or key.startswith("_"):
                continue
            # An extra= field named like one of ours would silently overwrite
            # it, or be overwritten by it. Either way the log is corrupted in a
            # way nobody notices until a report double-counts. Namespace it.
            out_key = f"x_{key}" if key in RESERVED else key
            payload[out_key] = value if _jsonable(value) else repr(value)
        # `exc_info=True` outside an except block gives (None, None, None) -
        # a tuple, and therefore truthy. Testing the tuple alone made this
        # formatter raise, and a formatter that raises means logging drops the
        # record entirely: the one line you asked to be recorded is the one
        # that disappears. Test the exception type, not the tuple.
        info = record.exc_info
        if info and info[0] is not None:
            exc_type, exc, tb = info
            payload["error"] = f"{exc_type.__name__}: {exc}"
            payload["trace"] = "".join(
                traceback.format_exception(exc_type, exc, tb))
        return json.dumps(payload, ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    """Readable line for stderr, with the extra fields appended.

    Without this, the supervisor log reads "source unreachable" and omits the
    one thing you need: which source. Long values are truncated here only -
    the untruncated version is always in the JSON file.

    This stream usually ends up on a terminal - yours, or a supervisor's - so
    it gets the same control-character treatment as the `--tail` viewer. A
    hostile upstream value must not be able to repaint a screen just because
    it took the live path instead of the stored one.
    """

    def format(self, record: logging.LogRecord) -> str:
        base = safe(super().format(record))
        parts = []
        for key, value in record.__dict__.items():
            if key in _STD or key.startswith("_"):
                continue
            text = safe(value)
            if len(text) > HUMAN_FIELD_MAX:
                text = text[:HUMAN_FIELD_MAX] + "..."
            parts.append(f"{key}={text}")
        return f"{base}  {' '.join(parts)}" if parts else base


def log_path(service: str, log_dir: Optional[Path] = None) -> Path:
    return (Path(log_dir) if log_dir else DEFAULT_DIR) / f"{service}.jsonl"


def setup(service: str, log_dir: Optional[Path] = None,
          level: str = "INFO", stderr: bool = True) -> logging.Logger:
    """A ready logger: rotating JSON file, readable text on stderr.

    Calling it twice with the same name does not duplicate handlers, so it is
    safe in a module that gets imported from more than one place.

    If the log file cannot be opened - full disk, read-only volume, wrong
    permissions - it says so once and carries on with stderr. Logging must
    never be the reason a service dies.
    """
    log = logging.getLogger(service)
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    log.propagate = False
    if log.handlers:
        return log

    path = log_path(service, log_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # 0o700, owner only - stricter than semgrep's suggested 0o644.
            os.chmod(path.parent, LOG_DIR_MODE)  # nosemgrep: insecure-file-permissions
        except OSError:
            pass
        # Create the file with the right mode BEFORE the handler opens it:
        # creating it first and chmod-ing after leaves a window in which the
        # default umask applies and the log is world-readable.
        if not path.exists():
            os.close(os.open(str(path), os.O_CREAT | os.O_WRONLY | _NOFOLLOW,
                             LOG_MODE))
        else:
            try:
                os.chmod(path, LOG_MODE)
            except OSError:
                pass
        fh = SecureRotatingFileHandler(
            str(path), maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        log.addHandler(fh)
    except OSError as exc:
        print(f"applog: log file not writable ({exc}), stderr only",
              file=sys.stderr)

    if stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(HumanFormatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
        log.addHandler(sh)
    return log


# -------------------------------------------------------------------- reading
def read(service: str, log_dir: Optional[Path] = None,
         level: Optional[str] = None, limit: int = 40) -> List[Dict]:
    """Most recent events, already filtered by level."""
    path = log_path(service, log_dir)
    if not path.exists():
        return []
    wanted = None
    if level:
        floor = getattr(logging, level.upper(), logging.INFO)
        wanted = {name for name in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
                  if getattr(logging, name) >= floor}
    out: List[Dict] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue          # a half-written line during rotation
            if wanted and rec.get("level") not in wanted:
                continue
            out.append(rec)
    return out[-limit:]


def services(log_dir: Optional[Path] = None) -> List[str]:
    base = Path(log_dir) if log_dir else DEFAULT_DIR
    return sorted(p.stem for p in base.glob("*.jsonl")) if base.exists() else []


def _print_records(records: List[Dict], show_trace: bool) -> None:
    for rec in records:
        extra = {k: v for k, v in rec.items()
                 if k not in ("ts", "level", "service", "msg", "error", "trace")}
        tail = f"  {safe(extra)}" if extra else ""
        print(f"{safe(rec.get('ts',''))} {safe(rec.get('level','')):<7} "
              f"{safe(rec.get('msg',''))}{tail}")
        if rec.get("error"):
            print(f"          {safe(rec['error'])}")
        if show_trace and rec.get("trace"):
            for line in rec["trace"].rstrip().splitlines():
                print(f"          {safe(line)}")


def cmd_errors(service: str, log_dir: Optional[Path]) -> int:
    """Errors grouped by kind: what breaks most, not just what broke last."""
    records = read(service, log_dir, level="WARNING", limit=100_000)
    if not records:
        print(f"no warnings or errors for {service}")
        return 0
    counts: Dict[str, Dict] = {}
    for rec in records:
        key = rec.get("error") or rec.get("msg", "?")
        slot = counts.setdefault(key, {"n": 0, "last": ""})
        slot["n"] += 1
        slot["last"] = rec.get("ts", "")
    print(f"{service} - {len(records)} events at WARNING or above\n")
    for key, slot in sorted(counts.items(), key=lambda kv: -kv[1]["n"]):
        print(f"{slot['n']:>5}x  {safe(key)}   (last {safe(slot['last'])})")
    return 0


def self_test() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        log = setup("probe", tmp_dir, stderr=False)

        log.info("started", extra={"sources": 3})
        try:
            raise ValueError("source is down")
        except ValueError:
            log.exception("fetch failed", extra={"url": "http://x.example/rss"})

        recs = read("probe", tmp_dir)
        assert len(recs) == 2, recs
        assert recs[0]["msg"] == "started" and recs[0]["sources"] == 3
        assert recs[0]["level"] == "INFO"

        err = recs[1]
        assert err["level"] == "ERROR"
        assert err["url"] == "http://x.example/rss"
        assert err["error"] == "ValueError: source is down"
        assert "Traceback" in err["trace"] and "source is down" in err["trace"], \
            "the stack trace never made it into the log"

        # level filtering must exclude the INFO line
        only_err = read("probe", tmp_dir, level="ERROR")
        assert len(only_err) == 1 and only_err[0]["level"] == "ERROR"

        # every line must be valid JSON, exactly one per event
        lines = log_path("probe", tmp_dir).read_text().strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            json.loads(line)

        # a non-serialisable object must not blow up the logger
        log.warning("odd", extra={"obj": object()})
        assert isinstance(read("probe", tmp_dir)[-1]["obj"], str)

        # an extra= field colliding with a reserved key must not overwrite it:
        # a caller passing extra={"error": ...} would otherwise corrupt the
        # error report, and nothing anywhere would report a problem
        log.warning("collision", extra={"error": "mine"})
        rec = read("probe", tmp_dir)[-1]
        assert rec["level"] == "WARNING"
        assert rec["x_error"] == "mine" and "error" not in rec

        # setting up twice must not duplicate handlers or double the lines
        again = setup("probe", tmp_dir, stderr=False)
        again.info("once only")
        assert sum(1 for r in read("probe", tmp_dir) if r["msg"] == "once only") == 1

        assert services(tmp_dir) == ["probe"]

        # unicode survives the round trip (ensure_ascii=False, utf-8 handler)
        log.info("accented", extra={"city": "Citta di Castello", "sym": "ok"})
        assert read("probe", tmp_dir)[-1]["city"] == "Citta di Castello"

        # Terminal-escape injection. An upstream value carrying ANSI escapes
        # must never reach the terminal raw: it can clear the screen or forge
        # convincing lines for whoever is reading the log. The bytes stay in
        # the file; only the rendering is neutralised.
        import contextlib
        import io

        esc = "\x1b[2J\x1b[1;31mFAKE: all clear\x1b[0m"
        log.warning(f"upstream said {esc}")
        try:
            raise ValueError(f"parse failed on {esc}")
        except ValueError:
            log.exception("fetch failed")
        log.warning("field", extra={"title": esc})

        hostile = read("probe", tmp_dir)[-3:]
        assert any("\x1b" in str(v) for r in hostile for v in r.values()), \
            "the raw bytes must be preserved in the record"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_records(hostile, show_trace=True)
        rendered = buf.getvalue()
        assert "\x1b" not in rendered, "escape sequence reached the terminal"
        assert "\\x1b" in rendered, "it should be shown, just declawed"

        # The live stderr path is a terminal too. StreamHandler binds the
        # stream when it is built, so the sink has to be swapped in before
        # setup() - getting that wrong is how this gap stayed hidden.
        real_stderr = sys.stderr
        sys.stderr = io.StringIO()
        try:
            live = setup("liveterm", tmp_dir, stderr=True)
            live.warning("upstream", extra={"title": esc})
            try:
                raise ValueError(f"boom {esc}")
            except ValueError:
                live.exception("failed")
            streamed = sys.stderr.getvalue()
        finally:
            sys.stderr = real_stderr
        assert streamed, "nothing reached stderr, the test proves nothing"
        assert "\x1b" not in streamed, "escape sequence reached stderr raw"

        # exc_info=True with no active exception must not lose the record:
        # (None, None, None) is truthy, and a formatter that raises makes
        # logging drop the line altogether
        before = len(read("probe", tmp_dir))
        log.error("no active exception", exc_info=True)
        after = read("probe", tmp_dir)
        assert len(after) == before + 1, "the record was dropped"
        assert after[-1]["msg"] == "no active exception"
        assert "error" not in after[-1] and "trace" not in after[-1]

        # the log file must not be readable by other users - and must still
        # not be after it rotates, which is where this quietly broke before
        if os.name == "posix":
            mode = log_path("probe", tmp_dir).stat().st_mode & 0o777
            assert mode == LOG_MODE, oct(mode)

            global MAX_BYTES
            was, MAX_BYTES = MAX_BYTES, 2000
            try:
                spin = setup("rotator", tmp_dir, stderr=False)
                for i in range(400):
                    spin.info("filling", extra={"i": i, "pad": "x" * 60})
                rotated = sorted(Path(tmp_dir).glob("rotator.jsonl*"))
                assert len(rotated) > 1, "the log never rotated, test proves nothing"
                for f in rotated:
                    got = f.stat().st_mode & 0o777
                    assert got == LOG_MODE, f"{f.name} is {oct(got)} after rotation"
            finally:
                MAX_BYTES = was

    print("OK - applog.py")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tail", metavar="SERVICE")
    ap.add_argument("--errors", metavar="SERVICE")
    ap.add_argument("--level", default=None)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--dir", default=None, help=f"default {DEFAULT_DIR}")
    ap.add_argument("--trace", action="store_true", help="print stack traces")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()

    log_dir = Path(args.dir) if args.dir else None

    if args.test:
        self_test()
        return 0
    if args.list:
        found = services(log_dir)
        print("\n".join(safe(f) for f in found) if found else "no logs found")
        return 0
    if args.errors:
        return cmd_errors(args.errors, log_dir)
    if args.tail:
        recs = read(args.tail, log_dir, args.level, args.limit)
        if not recs:
            print(f"no events for {args.tail}")
            return 1
        _print_records(recs, args.trace)
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
