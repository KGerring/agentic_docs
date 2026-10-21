"""One cycle of the loop: read back → collect → build → render."""

from __future__ import annotations

import fcntl
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from cll import build, feedback, render
from cll.collectors import gitlog, history, logs, scripts
from cll.config import Config
from cll.model import Node
from cll.redact import Redactor
from cll.state import Store


@contextmanager
def run_lock(cfg: Config) -> Iterator[bool]:
    """Non-blocking single-instance lock, so an overlapping scheduled run exits instead of queueing."""
    cfg.paths.state_dir.mkdir(parents=True, exist_ok=True)
    with (cfg.paths.state_dir / "run.lock").open("w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def collect(cfg: Config, store: Store) -> dict[str, Any]:
    redactor = Redactor(cfg.redact.extra_patterns, cfg.history.sensitive_tools)
    stats: dict[str, Any] = {}
    if cfg.history.enabled:
        stats["history"] = history.collect(cfg, store, redactor)
    if cfg.git.enabled:
        stats["git"] = gitlog.collect(cfg, store, redactor)
    if cfg.logs.enabled and cfg.logs.sources:
        stats["logs"] = logs.collect(cfg, store, redactor)
    if cfg.scripts.enabled:
        stats["scripts"] = scripts.collect(cfg, store, redactor)
    stats["redactions"] = dict(redactor.hits)
    return stats


def graph(cfg: Config, store: Store, now: int | None = None) -> tuple[dict[str, Node], set[str]]:
    return build.build(cfg, store, now or int(time.time()))


def cycle(cfg: Config, *, now: int | None = None, dry_run: bool = False, do_collect: bool = True) -> dict[str, Any]:
    now = now or int(time.time())
    started = time.perf_counter()
    with Store(cfg.paths.state_dir) as store:
        stats: dict[str, Any] = {"feedback": dict(feedback.read_back(cfg, store))}
        if do_collect:
            stats.update(collect(cfg, store))
        nodes, ignored = graph(cfg, store, now)
        by_type: dict[str, int] = {}
        for n in nodes.values():
            by_type[n.type] = by_type.get(n.type, 0) + 1
        stats["graph"] = {
            "nodes": len(nodes),
            "edges": sum(len(n.edges) for n in nodes.values()),
            "ignored": len(ignored),
            "by_type": by_type,
        }
        stats["drafts"] = dict(render.write(nodes, ignored, cfg, store, now, dry_run=dry_run))
        store.set_meta("last_run", str(now))
    stats["seconds"] = round(time.perf_counter() - started, 2)
    return stats
