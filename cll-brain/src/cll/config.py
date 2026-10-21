"""Configuration: one TOML file, resolved to absolute paths.

Lookup order: ``--config`` → ``$CLL_CONFIG`` → ``./cll.toml`` →
``$XDG_CONFIG_HOME/cll/cll.toml`` (``~/.config/cll/cll.toml``).
Relative paths inside the file resolve against the file's own directory.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

DEFAULT_SENSITIVE_TOOLS = (
    "op",
    "pass",
    "bw",
    "security",
    "gpg",
    "gpg2",
    "vault",
    "age",
    "sops",
    "htpasswd",
    "keyring",
    "aws-vault",
    "secret-tool",
)


@dataclass(slots=True)
class Paths:
    drafts_dir: Path
    state_dir: Path
    site_dir: Path
    graph_file: Path


@dataclass(slots=True)
class Render:
    link_style: Literal["wikilink", "markdown"] = "wikilink"
    on_consumed: Literal["skip", "rewrite"] = "skip"
    backlink_limit: int = 15
    h1: bool = True  # repeat the title as an H1 (turn off for renderers that add their own)
    write_index: bool = True  # maintain drafts_dir/_index.md


@dataclass(slots=True)
class History:
    enabled: bool = True
    files: list[Path] = field(default_factory=list)
    session_gap_minutes: int = 30
    since_days: int = 0  # 0 = everything the history file still holds
    min_tool_uses: int = 3
    sensitive_tools: tuple[str, ...] = DEFAULT_SENSITIVE_TOOLS
    ignore_tools: tuple[str, ...] = ()


@dataclass(slots=True)
class Git:
    enabled: bool = True
    roots: list[Path] = field(default_factory=list)
    repos: list[Path] = field(default_factory=list)
    max_depth: int = 3
    lookback_days: int = 365
    author_only: bool = False


@dataclass(slots=True)
class LogSource:
    name: str
    globs: list[str]
    min_level: str = "warn"


@dataclass(slots=True)
class Logs:
    enabled: bool = True
    sources: list[LogSource] = field(default_factory=list)
    max_bytes_per_run: int = 20 * 1024 * 1024


@dataclass(slots=True)
class Scripts:
    enabled: bool = True
    roots: list[Path] = field(default_factory=list)
    extensions: tuple[str, ...] = (".sh", ".bash", ".zsh", ".ksh")
    exclude: tuple[str, ...] = ("node_modules", ".git", ".venv", "venv", "__pycache__", ".oh-my-zsh")
    parser: Literal["auto", "tree-sitter", "heuristic"] = "auto"
    embed_source: Literal["functions", "all", "none"] = "functions"
    max_file_bytes: int = 512 * 1024


@dataclass(slots=True)
class Redact:
    extra_patterns: tuple[str, ...] = ()


@dataclass(slots=True)
class Site:
    title: str = "Second Brain"
    tagline: str = "Shell history, scripts, repos and logs as one linked graph."
    notice: str = ""  # one line shown above the results, e.g. "Snapshot of 2026-10-06"
    google_fonts: bool = True
    page_size: int = 60
    graph_node_cap: int = 600


@dataclass(slots=True)
class Config:
    source: Path | None
    paths: Paths
    render: Render
    history: History
    git: Git
    logs: Logs
    scripts: Scripts
    redact: Redact
    site: Site


def _expand(value: str, base: Path) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(value)))
    return p if p.is_absolute() else (base / p).resolve()


def default_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(os.path.expanduser(xdg)) / "cll" / "cll.toml"


def find_config(explicit: str | None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    if env := os.environ.get("CLL_CONFIG"):
        candidates.append(Path(env).expanduser())
    candidates += [Path.cwd() / "cll.toml", default_config_path()]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    if explicit:
        raise FileNotFoundError(f"config not found: {explicit}")
    return None


def _pick(table: dict[str, Any], cls: type, **overrides: Any) -> Any:
    """Build a dataclass from the TOML keys it declares; reject unknown keys."""
    names = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
    unknown = set(table) - names - set(overrides)
    if unknown:
        raise ValueError(f"unknown key(s) in [{cls.__name__.lower()}]: {', '.join(sorted(unknown))}")
    kwargs = {k: v for k, v in table.items() if k in names}
    kwargs.update(overrides)
    for k, v in list(kwargs.items()):
        if isinstance(v, list) and cls.__dataclass_fields__[k].type.startswith("tuple"):  # type: ignore[attr-defined]
            kwargs[k] = tuple(v)
    return cls(**kwargs)


def load(explicit: str | None = None) -> Config:
    path = find_config(explicit)
    raw: dict[str, Any] = {}
    base = Path.cwd()
    if path is not None:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        base = path.parent

    home = Path.home()
    state_home = Path(os.path.expanduser(os.environ.get("XDG_STATE_HOME") or "~/.local/state"))

    p = raw.get("paths", {})
    paths = Paths(
        drafts_dir=_expand(p.get("drafts_dir", "drafts"), base),
        state_dir=_expand(p["state_dir"], base) if "state_dir" in p else state_home / "cll",
        site_dir=_expand(p.get("site_dir", "site"), base),
        graph_file=_expand(p.get("graph_file", "graph.json"), base),
    )

    h = dict(raw.get("history", {}))
    h_files = [_expand(f, base) for f in h.pop("files", [])] or [
        f
        for f in (home / ".zsh_history", home / ".bash_history", home / ".local/share/fish/fish_history")
        if f.exists()
    ]
    history = _pick(h, History, files=h_files)

    g = dict(raw.get("git", {}))
    git = _pick(
        g,
        Git,
        roots=[_expand(r, base) for r in g.pop("roots", [])],
        repos=[_expand(r, base) for r in g.pop("repos", [])],
    )

    lg = dict(raw.get("logs", {}))
    sources = [
        LogSource(name=s["name"], globs=list(s["globs"]), min_level=s.get("min_level", "warn"))
        for s in lg.pop("source", [])
    ]
    logs = _pick(lg, Logs, sources=sources)

    s = dict(raw.get("scripts", {}))
    scripts = _pick(s, Scripts, roots=[_expand(r, base) for r in s.pop("roots", [])])

    return Config(
        source=path,
        paths=paths,
        render=_pick(dict(raw.get("render", {})), Render),
        history=history,
        git=git,
        logs=logs,
        scripts=scripts,
        redact=_pick(dict(raw.get("redact", {})), Redact),
        site=_pick(dict(raw.get("site", {})), Site),
    )


def starter_toml(home: Path | None = None) -> str:
    """A config pre-filled with whatever exists on this machine."""
    home = home or Path.home()

    def existing(*names: str) -> list[str]:
        return [f"~/{n}" for n in names if (home / n).exists()]

    def arr(items: list[str]) -> str:
        return "[" + ", ".join(f'"{i}"' for i in items) + "]"

    hist = existing(".zsh_history", ".bash_history", ".local/share/fish/fish_history")
    script_roots = existing("bash_scripts", "bin", "scripts", ".local/bin")
    repo_roots = existing("repos", "proj", "projects", "code", "src", "dev")
    return f"""# cll.toml — closed-learning-loop collector
# Relative paths resolve against this file's directory.

[paths]
drafts_dir = "drafts"          # markdown nodes land here; your other ingest reads it
site_dir   = "site"            # `cll site` writes index.html here
graph_file = "graph.json"      # node-link JSON of the whole graph (NetworkX-compatible)
# state_dir = "~/.local/state/cll"   # SQLite event store + cursors (default shown)

[render]
link_style  = "wikilink"       # "wikilink" → [[tool/git|git]]   "markdown" → [git](../tool/git.md)
on_consumed = "skip"           # a draft your ingest moved/deleted is only re-emitted when it changes
backlink_limit = 15            # "Referenced by" entries listed per relation type
h1 = true                      # false if your docs renderer adds its own title heading
write_index = true             # keep drafts_dir/_index.md up to date

[history]
enabled = true
files = {arr(hist)}
session_gap_minutes = 30       # a pause longer than this starts a new session node
since_days = 0                 # 0 = everything the history file still holds
min_tool_uses = 3              # a command needs this many uses (or a script reference) to get a node
# ignore_tools = ["clear", "ll"]

[git]
enabled = true
roots = {arr(repo_roots)}      # scanned for .git up to max_depth
max_depth = 3
lookback_days = 365
author_only = false            # true = only commits by `git config user.email`

[scripts]
enabled = true
roots = {arr(script_roots)}
extensions = [".sh", ".bash", ".zsh", ".ksh"]   # extensionless files are detected by shebang
parser = "auto"                # "tree-sitter" (needs the `ts` extra) | "heuristic" | "auto"
embed_source = "functions"     # scrubbed code in nodes: "functions" | "all" | "none"

[logs]
enabled = true
# One block per log family. Lines below min_level are counted, not itemised.
# [[logs.source]]
# name = "homebrew"
# globs = ["~/Library/Logs/Homebrew/**/*.log"]
# min_level = "warn"

[redact]
# Extra regexes whose matches are replaced with [REDACTED:custom] before anything is stored.
# extra_patterns = ["corp-[0-9a-f]{{24}}"]

[site]
title = "Second Brain"
tagline = "Shell history, scripts, repos and logs as one linked graph."
google_fonts = true            # false = system fonts only, no network request
# notice = "Private. Generated from local shell history."   # one line shown above the results
"""
