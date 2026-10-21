"""Git repositories → ``repos`` and ``commits`` rows (read-only ``git log``)."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from cll.config import Config
from cll.redact import Redactor
from cll.state import Store

_SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", "site-packages", ".tox", "target", "dist", "build"}
_RS, _US = "\x1e", "\x1f"


def _git(repo: Path, *args: str, timeout: int = 60) -> str | None:
    try:
        res = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout if res.returncode == 0 else None


def discover(roots: list[Path], max_depth: int) -> list[Path]:
    found: list[Path] = []

    def walk(d: Path, depth: int) -> None:
        if (d / ".git").exists():
            found.append(d)
            return
        if depth >= max_depth:
            return
        try:
            children = sorted(p for p in d.iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            return
        for child in children:
            if child.name.startswith(".") or child.name in _SKIP_DIRS:
                continue
            walk(child, depth + 1)

    for root in roots:
        if root.is_dir():
            walk(root, 0)
    return found


def collect(cfg: Config, store: Store, redactor: Redactor) -> dict[str, int]:
    stats = {"repos": 0, "new": 0}
    now = int(time.time())
    repos = {p.resolve() for p in discover(cfg.git.roots, cfg.git.max_depth)}
    repos |= {p.resolve() for p in cfg.git.repos if (p / ".git").exists()}

    for repo in sorted(repos):
        log_args = [
            "log",
            f"--since={cfg.git.lookback_days}.days",
            "-n",
            "5000",
            "--numstat",
            f"--pretty=format:{_RS}%H{_US}%at{_US}%an{_US}%ae{_US}%s",
        ]
        if cfg.git.author_only:
            email = (_git(repo, "config", "user.email") or "").strip()
            if email:
                log_args.append(f"--author={email}")
        out = _git(repo, *log_args)
        if out is None:
            continue  # not a usable repository (or no commits yet)
        stats["repos"] += 1
        remote = redactor.scrub((_git(repo, "remote", "get-url", "origin") or "").strip()) or None
        branch = (_git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "").strip() or None
        store.db.execute(
            "INSERT INTO repos VALUES (?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
            "remote=excluded.remote, branch=excluded.branch, seen=excluded.seen",
            (str(repo), "", repo.name, remote, branch, now),
        )
        rows = []
        for record in out.split(_RS)[1:]:
            header, _, body = record.partition("\n")
            parts = header.split(_US)
            if len(parts) != 5:
                continue
            sha, at, author, email, subject = parts
            files, ins, dele = [], 0, 0
            for line in body.splitlines():
                cols = line.split("\t")
                if len(cols) == 3:
                    ins += int(cols[0]) if cols[0].isdigit() else 0
                    dele += int(cols[1]) if cols[1].isdigit() else 0
                    files.append(cols[2])
            rows.append(
                (str(repo), sha, int(at), author, email, redactor.scrub(subject), json.dumps(files[:200]), ins, dele)
            )
        before = store.db.total_changes
        store.db.executemany("INSERT OR IGNORE INTO commits VALUES (?,?,?,?,?,?,?,?,?)", rows)
        stats["new"] += store.db.total_changes - before
    store.db.commit()
    return stats
