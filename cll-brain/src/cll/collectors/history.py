"""Shell history → ``commands`` rows.

Formats: zsh (plain and ``EXTENDED_HISTORY``, metafied bytes decoded), bash
(with or without ``HISTTIMEFORMAT`` timestamps) and fish.

History files are rewritten and truncated by the shell, so byte offsets are
not a safe cursor. Each run re-reads the file and inserts with a content key;
``INSERT OR IGNORE`` makes the pass idempotent:

* timestamped entry   → key = H(source, ts, text, nth-duplicate)
* untimestamped entry → key = H(source, text, nth-occurrence), ts = ingest time

``H`` is a salted BLAKE2b of the raw line; only the scrubbed text is stored.
A file whose mtime and size have not changed is skipped outright.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from cll import shparse
from cll.config import Config
from cll.redact import Redactor
from cll.state import Store

Entry = tuple[int | None, str]

_ZSH_EXT = re.compile(r": (\d{9,11}):\d+;(.*)", re.S)
_BASH_TS = re.compile(r"#(\d{9,11})\s*")


def unmetafy(data: bytes) -> bytes:
    """Undo zsh's on-disk metafication (0x83 marks the next byte as XOR 0x20)."""
    if b"\x83" not in data:
        return data
    out = bytearray()
    it = iter(data)
    for b in it:
        if b == 0x83:
            nxt = next(it, None)
            if nxt is None:
                break
            out.append(nxt ^ 0x20)
        else:
            out.append(b)
    return bytes(out)


def detect_shell(path: Path, head: str) -> str:
    name = path.name.lower()
    if "fish" in name or head.startswith("- cmd:"):
        return "fish"
    if "zsh" in name or _ZSH_EXT.match(head.split("\n", 1)[0] or ""):
        return "zsh"
    return "bash"


def parse_zsh(text: str) -> Iterator[Entry]:
    lines = text.split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        i += 1
        # A trailing backslash continues the entry on the next physical line.
        while line.endswith("\\") and i < n:
            line = line[:-1] + "\n" + lines[i]
            i += 1
        if not line.strip():
            continue
        if m := _ZSH_EXT.fullmatch(line):
            yield int(m[1]), m[2]
        else:
            yield None, line


def parse_bash(text: str) -> Iterator[Entry]:
    pending: int | None = None
    for line in text.split("\n"):
        if m := _BASH_TS.fullmatch(line):
            pending = int(m[1])
            continue
        if line.strip():
            yield pending, line
            pending = None


def parse_fish(text: str) -> Iterator[Entry]:
    cmd: str | None = None
    when: int | None = None
    for line in text.split("\n"):
        if line.startswith("- cmd: "):
            if cmd is not None:
                yield when, cmd
            cmd, when = line[7:].replace("\\n", "\n").replace("\\\\", "\\"), None
        elif line.startswith("  when: ") and line[8:].strip().isdigit():
            when = int(line[8:].strip())
    if cmd is not None:
        yield when, cmd


_PARSERS = {"zsh": parse_zsh, "bash": parse_bash, "fish": parse_fish}


def heads(command: str) -> list[list[str]]:
    """``[argv0, first args…]`` for each simple command in a history line."""
    out: list[list[str]] = []
    for _, words in shparse.split_commands(command):
        if (head := shparse.command_head(words)) is not None:
            name, args = head
            out.append([name[:200], *(a[:200] for a in args[:4])])
    return out


def collect(cfg: Config, store: Store, redactor: Redactor) -> dict[str, int]:
    stats = {"files": 0, "unchanged": 0, "seen": 0, "new": 0}
    now = int(time.time())
    floor = now - cfg.history.since_days * 86400 if cfg.history.since_days else 0
    # Keys are a salted hash of the *raw* entry, so an already-ingested line is recognised
    # without scrubbing or parsing it again. The salt never leaves the state directory.
    salt = store.get_meta("salt")
    if salt is None:
        salt = secrets.token_hex(16)
        store.set_meta("salt", salt)
    for path in cfg.history.files:
        try:
            st = path.stat()
            raw = path.read_bytes()
        except OSError:
            continue
        source = str(path)
        fingerprint = f"{st.st_mtime_ns}:{st.st_size}:{cfg.history.since_days}"
        if store.get_meta(f"hist:{source}") == fingerprint:
            stats["unchanged"] += 1
            continue
        stats["files"] += 1
        text = unmetafy(raw).decode("utf-8", errors="replace")
        shell = detect_shell(path, text[:200])
        known = {r[0] for r in store.db.execute("SELECT key FROM commands WHERE source=?", (source,))}
        nth: Counter[tuple[int | None, str]] = Counter()
        rows = []
        for ts, cmd in _PARSERS[shell](text):
            cmd = cmd.strip()
            if not cmd:
                continue
            stats["seen"] += 1
            if ts is not None and ts < floor:
                continue
            ident = (ts, cmd)
            nth[ident] += 1
            key = hashlib.blake2b(
                f"{source}\x1f{ts}\x1f{cmd}\x1f{nth[ident]}".encode(), key=bytes.fromhex(salt), digest_size=16
            ).hexdigest()
            if key in known:
                continue
            clean = redactor.scrub_command(cmd)
            rows.append(
                (
                    key,
                    source,
                    shell,
                    ts if ts is not None else now,
                    int(ts is not None),
                    clean,
                    json.dumps(heads(clean), ensure_ascii=False),
                )
            )
        store.db.executemany("INSERT OR IGNORE INTO commands VALUES (?,?,?,?,?,?,?)", rows)
        stats["new"] += len(rows)
        store.set_meta(f"hist:{source}", fingerprint)
    store.db.commit()
    return stats
