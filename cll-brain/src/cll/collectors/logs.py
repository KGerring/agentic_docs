"""Log files → per-day signature aggregates.

Each file is tailed from a stored ``(inode, offset)`` cursor; rotation or
truncation resets the cursor. Lines are scrubbed, reduced to a *signature*
(numbers, ids, paths and quoted strings replaced by placeholders) and counted
per ``(source, day, level, signature)``, so a recurring failure collapses into
one row however often it fires.
"""

from __future__ import annotations

import glob
import os
import re
from datetime import datetime
from pathlib import Path

from cll.config import Config, LogSource
from cll.redact import Redactor
from cll.state import Store

LEVELS = ("debug", "info", "warn", "error")
_LEVEL_WORD = re.compile(
    r"\b(TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERR(?:OR)?|CRIT(?:ICAL)?|FATAL|PANIC|ALERT|EMERG)\b"
    r"|\blevel=\"?([A-Za-z]+)"
)
_ERRORISH = re.compile(
    r"Traceback \(most recent call last\)|\b[A-Z][A-Za-z]*(?:Error|Exception)\b|\berror:|\bfailed\b", re.I
)
_ISO = re.compile(r"(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)")
_SYSLOG = re.compile(r"^([A-Z][a-z]{2}) +(\d{1,2}) (\d\d:\d\d:\d\d)")
_EPOCH = re.compile(r"^\[?(\d{10})(?:\.\d+)?\]?\s")
_TS_PREFIX = re.compile(
    r"^\[?(?:\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?|"
    r"[A-Z][a-z]{2} +\d{1,2} \d\d:\d\d:\d\d|\d{10}(?:\.\d+)?)\]?\s*"
)
_MONTHS = {
    m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)
}

_SIG_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\"[^\"]{0,200}\"|'[^']{0,200}'"), "<str>"),
    (re.compile(r"(?<![\w.])(?:~|\.{1,2})?/[\w.\-/@+]+"), "<path>"),
    (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{8,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d+(?:\.\d+)*\b"), "<n>"),
)


def classify(line: str) -> str:
    if m := _LEVEL_WORD.search(line):
        word = (m[1] or m[2] or "").upper()
        if word.startswith(("ERR", "CRIT", "FATAL", "PANIC", "ALERT", "EMERG")):
            return "error"
        if word.startswith("WARN"):
            return "warn"
        if word in {"INFO", "NOTICE"}:
            return "info"
        if word in {"DEBUG", "TRACE"}:
            return "debug"
    return "error" if _ERRORISH.search(line) else "info"


def timestamp(line: str, fallback: int) -> int:
    try:
        if m := _ISO.search(line[:64]):
            return int(datetime.fromisoformat(f"{m[1]}T{m[2]}").timestamp())
        if m := _SYSLOG.match(line):
            ref = datetime.fromtimestamp(fallback)
            h, mi, s = (int(x) for x in m[3].split(":"))
            dt = datetime(ref.year, _MONTHS[m[1]], int(m[2]), h, mi, s)
            if dt.timestamp() > fallback + 86400 * 2:  # a December line read in January
                dt = dt.replace(year=ref.year - 1)
            return int(dt.timestamp())
        if m := _EPOCH.match(line):
            return int(m[1])
    except (ValueError, KeyError, OverflowError):
        pass
    return fallback


def signature(line: str) -> str:
    sig = _TS_PREFIX.sub("", line).strip()
    for pat, repl in _SIG_RULES:
        sig = pat.sub(repl, sig)
    return re.sub(r"\s+", " ", sig)[:180]


def _files(src: LogSource) -> list[Path]:
    seen: dict[Path, None] = {}
    for pattern in src.globs:
        for hit in glob.glob(os.path.expanduser(os.path.expandvars(pattern)), recursive=True):
            p = Path(hit)
            if p.is_file():
                seen[p] = None
    return list(seen)


def collect(cfg: Config, store: Store, redactor: Redactor) -> dict[str, int]:
    stats = {"files": 0, "lines": 0}
    for src in cfg.logs.sources:
        floor = LEVELS.index(src.min_level) if src.min_level in LEVELS else 2
        for path in _files(src):
            try:
                st = path.stat()
            except OSError:
                continue
            row = store.db.execute("SELECT * FROM log_cursor WHERE path=?", (str(path),)).fetchone()
            offset = row["offset"] if row and row["inode"] == st.st_ino and row["offset"] <= st.st_size else 0
            last_ts = row["last_ts"] if row and offset else int(st.st_mtime)
            if offset == st.st_size:
                continue
            stats["files"] += 1
            agg: dict[tuple[str, str, str], list] = {}
            with path.open("rb") as fh:
                fh.seek(offset)
                chunk = fh.read(cfg.logs.max_bytes_per_run)
            cut = chunk.rfind(b"\n") + 1  # only complete lines; the tail waits for the next run
            if cut == 0 and len(chunk) >= cfg.logs.max_bytes_per_run:
                cut = len(chunk)  # one line longer than the read window: take it as-is
            for raw in chunk[:cut].decode("utf-8", errors="replace").splitlines():
                if not raw.strip():
                    continue
                stats["lines"] += 1
                last_ts = ts = timestamp(raw, last_ts)
                level = classify(raw)
                day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                if LEVELS.index(level) >= floor:
                    clean = redactor.scrub(raw.rstrip())
                    key = (day, level, signature(clean))
                    sample = _TS_PREFIX.sub("", clean)[:400]
                else:
                    key, sample = (day, level, ""), None
                slot = agg.setdefault(key, [0, sample, ts, ts])
                slot[0] += 1
                slot[3] = ts
            store.db.executemany(
                "INSERT INTO log_agg VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(source, day, level, sig) DO UPDATE SET "
                "count = count + excluded.count, last_ts = MAX(last_ts, excluded.last_ts), "
                "first_ts = MIN(first_ts, excluded.first_ts)",
                [(src.name, d, lv, sg, v[0], v[1], v[2], v[3]) for (d, lv, sg), v in agg.items()],
            )
            store.db.execute(
                "INSERT INTO log_cursor VALUES (?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                "inode=excluded.inode, offset=excluded.offset, last_ts=excluded.last_ts",
                (str(path), st.st_ino, offset + cut, last_ts),
            )
            store.db.commit()
    return stats
