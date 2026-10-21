"""``cll`` command line."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from cll import __version__
from cll import config as config_mod


def _print_stats(stats: dict) -> None:
    def line(label: str, d: dict) -> None:
        if d:
            print(f"  {label:<10} " + "  ".join(f"{k}={v}" for k, v in d.items() if not isinstance(v, dict)))

    for key in ("feedback", "history", "git", "logs", "scripts", "redactions", "graph", "drafts"):
        if isinstance(stats.get(key), dict):
            line(key, stats[key])
    if by_type := stats.get("graph", {}).get("by_type"):
        print("  " + " " * 11 + "  ".join(f"{k}={v}" for k, v in sorted(by_type.items())))
    print(f"  {stats.get('seconds', 0)}s")


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser() if args.path else Path.cwd() / "cll.toml"
    if path.exists() and not args.force:
        print(f"{path} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config_mod.starter_toml(), encoding="utf-8")
    print(f"wrote {path}\nreview it, then: cll doctor && cll run")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from cll import pipeline

    cfg = config_mod.load(args.config)
    with pipeline.run_lock(cfg) as acquired:
        if not acquired:
            if not args.quiet:
                print("another cll run is in progress; skipping", file=sys.stderr)
            return 0
        stats = pipeline.cycle(cfg, dry_run=args.dry_run, do_collect=not args.no_collect)
        if args.site and not args.dry_run:
            from cll import site

            stats["site"] = site.build(cfg)
    if args.json:
        print(json.dumps(stats, indent=1))
    elif not args.quiet:
        print("dry run: nothing written" if args.dry_run else f"drafts → {cfg.paths.drafts_dir}")
        _print_stats(stats)
    else:
        d = stats.get("drafts", {})
        print(
            f"{datetime.now().isoformat(timespec='seconds')} nodes={stats['graph']['nodes']} "
            f"created={d.get('created', 0)} updated={d.get('updated', 0)} "
            f"stale={d.get('stale', 0)} {stats['seconds']}s"
        )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report what a run would read. Prints counts and paths only, never content."""
    from cll.collectors import gitlog, scripts

    cfg = config_mod.load(args.config)
    print(f"config      {cfg.source or '(none found: built-in defaults)'}")
    print(f"drafts      {cfg.paths.drafts_dir}")
    print(f"state       {cfg.paths.state_dir}")
    print("history     " + ("disabled" if not cfg.history.enabled else ""))
    for f in cfg.history.files if cfg.history.enabled else []:
        try:
            print(f"  {f}  {f.stat().st_size:,} bytes")
        except OSError as exc:
            print(f"  {f}  UNREADABLE ({exc.strerror})")
    if cfg.git.enabled:
        repos = gitlog.discover(cfg.git.roots, cfg.git.max_depth)
        print(f"git         {len(repos)} repositories under {len(cfg.git.roots)} root(s)")
        for root in cfg.git.roots:
            if not root.is_dir():
                print(f"  missing root: {root}")
    if cfg.scripts.enabled:
        found = scripts.discover(cfg)
        parser = (
            "tree-sitter"
            if scripts._ts_parser() is not None
            else "heuristic (install the `ts` extra for exact parsing)"
        )
        print(f"scripts     {len(found)} shell scripts under {len(cfg.scripts.roots)} root(s); parser: {parser}")
        for root in cfg.scripts.roots:
            if not root.is_dir():
                print(f"  missing root: {root}")
    print(f"logs        {len(cfg.logs.sources)} source(s)" + ("" if cfg.logs.sources else " (none configured)"))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from cll import schedule
    from cll.state import Store

    cfg = config_mod.load(args.config)
    with Store(cfg.paths.state_dir) as store:
        last = store.get_meta("last_run")
        print("last run    " + (datetime.fromtimestamp(int(last)).isoformat(timespec="seconds") if last else "never"))
        for table in ("commands", "commits", "scripts", "log_agg", "nodes", "annotations"):
            print(f"{table:<11} {store.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:,}")
    print("schedule    " + schedule.status().replace("\n", "\n            "))
    return 0


def cmd_site(args: argparse.Namespace) -> int:
    from cll import site

    cfg = config_mod.load(args.config)
    res = site.build(
        cfg,
        Path(args.source).expanduser() if args.source else None,
        Path(args.out).expanduser() if args.out else None,
        fragment=args.fragment,
    )
    print(f"{res['path']}  ({res['nodes']} nodes, {res['edges']} edges, {res['bytes'] / 1024:.0f} KB)")
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    from cll import schedule

    cfg = config_mod.load(args.config)
    match args.action:
        case "show":
            print(schedule.show(cfg, args.interval))
        case "install":
            print(schedule.install(cfg, args.interval))
        case "uninstall":
            print(schedule.uninstall())
        case "status":
            print(schedule.status())
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    """Delete stored events (not drafts). Use after tightening redaction or to start over."""
    from cll.state import Store

    cfg = config_mod.load(args.config)
    tables = {
        "history": ["commands"],
        "git": ["commits", "repos"],
        "logs": ["log_agg", "log_cursor"],
        "scripts": ["scripts"],
        "all": ["commands", "commits", "repos", "log_agg", "log_cursor", "scripts"],
    }[args.what]
    with Store(cfg.paths.state_dir) as store:
        for t in tables:
            store.db.execute(f"DELETE FROM {t}")
        if "commands" in tables:
            store.db.execute("DELETE FROM meta WHERE key LIKE 'hist:%'")
        store.db.commit()
        store.db.execute("VACUUM")
    print(f"cleared: {', '.join(tables)}  (drafts untouched; the next run re-collects)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cll", description="Closed-learning-loop second-brain collector.")
    parser.add_argument("--version", action="version", version=f"cll {__version__}")
    parser.add_argument(
        "-c", "--config", help="path to cll.toml (default: $CLL_CONFIG, ./cll.toml, ~/.config/cll/cll.toml)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="write a starter cll.toml for this machine")
    p.add_argument("path", nargs="?", help="where to write it (default: ./cll.toml)")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("run", help="one cycle: read back annotations, collect, build, write drafts")
    p.add_argument("--dry-run", action="store_true", help="collect and build, but write no drafts")
    p.add_argument("--no-collect", action="store_true", help="re-render from the store without reading sources")
    p.add_argument("--site", action="store_true", help="also rebuild the static site")
    p.add_argument("--quiet", action="store_true", help="one log line (for the scheduler)")
    p.add_argument("--json", action="store_true", help="print run statistics as JSON")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("doctor", help="show what a run would read (counts only)")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("status", help="store size, last run, scheduler state")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("site", help="build the static site (one index.html)")
    p.add_argument("--from", dest="source", help="build from a directory of cll markdown instead of the store")
    p.add_argument("--out", help="output directory (default: paths.site_dir)")
    p.add_argument("--fragment", action="store_true", help="omit the <html>/<head> wrapper (for embedding)")
    p.set_defaults(fn=cmd_site)

    p = sub.add_parser("schedule", help="background runs via launchd / systemd")
    p.add_argument("action", choices=["show", "install", "uninstall", "status"])
    p.add_argument("--interval", type=int, default=900, help="seconds between runs (default 900)")
    p.set_defaults(fn=cmd_schedule)

    p = sub.add_parser("forget", help="delete collected events from the store")
    p.add_argument("what", choices=["history", "git", "logs", "scripts", "all"])
    p.set_defaults(fn=cmd_forget)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"cll: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
