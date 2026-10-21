"""Event store → graph of typed, linked nodes.

The whole graph is re-derived from the store on every run (the store is the
source of truth; drafts are a projection of it). That keeps aggregate nodes —
a tool's usage counts, a log's recurring signatures — correct without
incremental bookkeeping, and lets an annotation such as a new alias on
``tool/git`` re-attribute history that was collected months ago.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any

from cll import shparse
from cll.config import Config
from cll.model import CURATED, EXTRACTED, INFERRED, Edge, Node, slug_path, slugify
from cll.state import Store

SHELLS = frozenset({"bash", "sh", "zsh", "ksh", "dash"})

# Default topic tags for well-known commands; anything else is tagged by how it is used.
TOOL_TAGS: dict[str, tuple[str, ...]] = {
    "vcs": ("git", "gh", "hg", "svn", "jj", "lazygit", "tig"),
    "search": ("rg", "grep", "egrep", "fgrep", "ag", "fd", "find", "fzf", "locate", "mdfind", "ack"),
    "python": (
        "python",
        "python3",
        "uv",
        "uvx",
        "pip",
        "pip3",
        "pipx",
        "conda",
        "mamba",
        "pixi",
        "poetry",
        "pytest",
        "ruff",
        "mypy",
        "ipython",
        "jupyter",
        "pyenv",
        "hatch",
        "sphinx-build",
        "sphinx-autobuild",
        "pre-commit",
        "black",
        "tox",
        "nox",
    ),
    "node": ("node", "npm", "npx", "pnpm", "yarn", "bun", "deno", "tsc", "vercel", "mintlify", "mint"),
    "containers": ("docker", "podman", "docker-compose", "kubectl", "helm", "colima", "orb"),
    "packages": ("brew", "apt", "apt-get", "dnf", "pacman", "port", "mas", "nix"),
    "network": (
        "curl",
        "wget",
        "http",
        "httpie",
        "ping",
        "dig",
        "nslookup",
        "nc",
        "ssh",
        "scp",
        "rsync",
        "sftp",
        "mosh",
        "openssl",
        "nmap",
        "traceroute",
    ),
    "text": (
        "sed",
        "awk",
        "jq",
        "yq",
        "cut",
        "sort",
        "uniq",
        "tr",
        "wc",
        "head",
        "tail",
        "cat",
        "bat",
        "less",
        "more",
        "xargs",
        "tee",
        "column",
        "paste",
        "diff",
        "comm",
        "pandoc",
    ),
    "editor": ("vim", "nvim", "vi", "nano", "emacs", "code", "cursor", "subl", "hx", "pycharm", "zed"),
    "files": (
        "ls",
        "eza",
        "exa",
        "tree",
        "cp",
        "mv",
        "rm",
        "mkdir",
        "rmdir",
        "touch",
        "chmod",
        "chown",
        "ln",
        "du",
        "df",
        "stat",
        "file",
        "open",
        "tar",
        "zip",
        "unzip",
        "gzip",
        "trash",
    ),
    "process": (
        "ps",
        "top",
        "htop",
        "btop",
        "kill",
        "killall",
        "pkill",
        "pgrep",
        "launchctl",
        "systemctl",
        "lsof",
        "watch",
        "tmux",
        "screen",
        "cron",
        "crontab",
        "time",
        "sleep",
    ),
    "build": ("make", "cmake", "ninja", "cargo", "go", "gcc", "clang", "just", "bazel", "rustc"),
    "shell": ("bash", "zsh", "sh", "fish", "omz", "tldr", "man", "explainshell", "shellcheck", "shfmt"),
    "ai": ("claude", "ollama", "llm", "aider", "codex", "gemini", "graphify"),
    "data": ("sqlite3", "psql", "mysql", "duckdb", "redis-cli", "mongosh"),
}
_TAG_OF = {tool: tag for tag, tools in TOOL_TAGS.items() for tool in tools}

_MAX_SESSION_LISTED = 300
_VAR_PREFIX = re.compile(r"(?:\$\{[^}]*\}|\$\w+|\$\(…\)|\$\([^)]*\))/(.+)")
_HOME = str(Path.home())


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _tilde(path: str) -> str:
    return "~" + path[len(_HOME) :] if path == _HOME or path.startswith(_HOME + os.sep) else path


def _dur(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    h, m = divmod(seconds // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _fence(text: str, lang: str = "text") -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}{lang}\n{text}\n{fence}"


def _code(text: str) -> str:
    """Inline code that survives backticks and table pipes."""
    text = text.replace("`", "'").replace("|", "¦").replace("\n", " ")
    return f"`{text}`"


def _lines(nums: list[int], limit: int = 6) -> str:
    shown = ", ".join(f"L{n}" for n in nums[:limit])
    return shown + (f" (+{len(nums) - limit} more)" if len(nums) > limit else "")


@dataclass(slots=True)
class _Script:
    id: str
    path: Path
    root: Path
    rel: str
    sha: str
    mtime: float
    facts: dict[str, Any]
    fns: dict[str, str] = field(default_factory=dict)  # function name → node id
    visible: list[_Script] = field(default_factory=list)  # transitively sourced scripts


@dataclass(slots=True)
class _Tool:
    slug: str
    names: Counter = field(default_factory=Counter)
    uses: int = 0
    first: int | None = None
    last: int | None = None
    subs: Counter = field(default_factory=Counter)
    samples: deque = field(default_factory=lambda: deque(maxlen=6))
    sessions: int = 0
    script_refs: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _Session:
    start: int
    end: int
    exact: bool
    source: str
    shells: set[str] = field(default_factory=set)
    cmds: list[tuple[int, str]] = field(default_factory=list)
    tools: Counter = field(default_factory=Counter)
    runs: dict[str, list] = field(default_factory=dict)  # script id → [count, provenance, why]
    cds: list[str] = field(default_factory=list)
    id: str = ""


class Builder:
    def __init__(self, cfg: Config, store: Store, now: int) -> None:
        self.cfg, self.store, self.db, self.now = cfg, store, store.db, now
        self.nodes: dict[str, Node] = {}
        self.ann = store.annotations()
        self.ignored = {i for i, a in self.ann.items() if a.get("status") == "ignore"}
        # alias → tool slug, from `aliases:` a person added to a tool draft
        self.alias: dict[str, str] = {
            str(al): nid[5:] for nid, a in self.ann.items() if nid.startswith("tool/") for al in a.get("aliases") or []
        }
        self.scripts: dict[str, _Script] = {}
        self.by_base: dict[str, list[_Script]] = defaultdict(list)
        self.tools: dict[str, _Tool] = {}
        self.repos: dict[str, dict[str, Any]] = {}  # repo path → {id, name, …}
        self.sessions: list[_Session] = []
        self.day_facts: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"sessions": 0, "commands": 0, "commits": defaultdict(list), "logs": []}
        )

    def run(self) -> dict[str, Node]:
        self._load_repos()
        self._load_scripts()
        self._history()
        self._script_nodes()
        self._session_nodes()
        self._tool_nodes()
        self._repo_nodes()
        self._log_nodes()
        self._day_nodes()
        self._annotate()
        self._prune()
        return self.nodes

    # -- lookups --------------------------------------------------------------

    def _tool(self, name: str) -> _Tool:
        slug = self.alias.get(name) or slugify(name)
        tool = self.tools.get(slug)
        if tool is None:
            tool = self.tools[slug] = _Tool(slug)
        return tool

    def _script_for(self, word: str, base_dir: Path | None = None) -> tuple[_Script, str, str] | None:
        """Resolve a word to a known script: exact path → extracted, filename only → inferred."""
        w = word.strip("\"'")
        if not w or "…" in w:
            return None
        base = w.rstrip("/").rsplit("/", 1)[-1]
        if not base or "$" in base:
            return None
        dynamic = "$" in w
        if not dynamic and ("/" in w or w.startswith("~")):
            p = Path(os.path.expanduser(w))
            if not p.is_absolute() and base_dir is not None:
                p = base_dir / p
            if p.is_absolute() and (hit := self.scripts.get(os.path.normpath(p))) is not None:
                return hit, EXTRACTED, ""
        # `$DIR/lib/x.sh`, `$(dirname "$0")/x.sh`: a leading variable is almost always the script's own directory.
        if dynamic and base_dir is not None and (m := _VAR_PREFIX.match(w)) and "$" not in m[1]:
            if (hit := self.scripts.get(os.path.normpath(base_dir / m[1]))) is not None:
                return hit, INFERRED, f"`{w}` resolved against the script's own directory"
        hits = self.by_base.get(base, [])
        if base_dir is not None and len(hits) > 1:
            hits = [h for h in hits if h.path.parent == base_dir] or hits
        if len(hits) == 1:
            why = f"matched by filename `{base}`" + ("; the path is built at run time" if dynamic else "")
            return hits[0], INFERRED, why
        return None

    def _repo_of(self, path: Path) -> dict[str, Any] | None:
        best = None
        for rpath, repo in self.repos.items():
            if path == Path(rpath) or Path(rpath) in path.parents:
                if best is None or len(rpath) > len(best["path"]):
                    best = repo
        return best

    # -- loading --------------------------------------------------------------

    def _load_repos(self) -> None:
        rows = self.db.execute(
            "SELECT * FROM repos WHERE seen = (SELECT MAX(seen) FROM repos) ORDER BY path"
        ).fetchall()
        used: set[str] = set()
        for r in rows:
            p = Path(r["path"])
            slug = slugify(p.name)
            if slug in used:
                slug = slug_path([p.parent.name, p.name])
            if slug in used:
                slug += "-" + hashlib.sha1(r["path"].encode()).hexdigest()[:6]
            used.add(slug)
            commits = self.db.execute("SELECT * FROM commits WHERE repo=? ORDER BY ts DESC", (r["path"],)).fetchall()
            by_file: dict[str, list] = defaultdict(list)  # file → its 10 most recent commits
            for c in commits:
                for f in json.loads(c["files"]):
                    if len(by_file[f]) < 10:
                        by_file[f].append(c)
            self.repos[r["path"]] = {
                "id": f"repo/{slug}",
                "path": r["path"],
                "name": p.name,
                "remote": r["remote"],
                "branch": r["branch"],
                "commits": commits,
                "by_file": by_file,
            }

    def _load_scripts(self) -> None:
        used: set[str] = set()
        for r in self.db.execute("SELECT * FROM scripts ORDER BY path"):
            root, rel = Path(r["root"]), Path(r["rel"])
            slug = slug_path([root.name, *rel.parts])
            if slug in used:
                slug += "-" + hashlib.sha1(r["path"].encode()).hexdigest()[:6]
            used.add(slug)
            s = _Script(
                f"script/{slug}",
                Path(r["path"]),
                root,
                f"{root.name}/{r['rel']}",
                r["sha"],
                r["mtime"],
                json.loads(r["facts"]),
            )
            seen: Counter[str] = Counter()
            for fn in s.facts["functions"]:
                seen[fn["name"]] += 1
                suffix = f"-{seen[fn['name']]}" if seen[fn["name"]] > 1 else ""
                fn["id"] = f"fn/{slug}--{slugify(fn['name'])}{suffix}"
                s.fns.setdefault(fn["name"], fn["id"])
            self.scripts[os.path.normpath(r["path"])] = s
            self.by_base[s.path.name].append(s)

        # `source` edges first: a sourced file's functions are callable from the sourcing script.
        direct: dict[str, list[tuple[_Script, str, str, int]]] = {}
        for key, s in self.scripts.items():
            direct[key] = []
            unresolved = []
            for src in s.facts["sources"]:
                hit = self._script_for(src["target"], s.path.parent)
                if hit is not None and hit[0] is not s:
                    direct[key].append((hit[0], hit[1], hit[2], src["line"]))
                else:
                    unresolved.append(src["target"].strip("\"'"))
            s.facts["_sourced"] = direct[key]
            s.facts["_unresolved_sources"] = sorted(set(unresolved))
        for key, s in self.scripts.items():
            seen_keys, queue = {key}, [x[0] for x in direct[key]]
            while queue:
                other = queue.pop(0)
                okey = os.path.normpath(other.path)
                if okey in seen_keys:
                    continue
                seen_keys.add(okey)
                s.visible.append(other)
                queue.extend(x[0] for x in direct[okey])

    # -- history --------------------------------------------------------------

    def _history(self) -> None:
        gap = self.cfg.history.session_gap_minutes * 60
        skip = set(self.cfg.history.ignore_tools)
        cur: _Session | None = None
        # The session still in progress is held back entirely (no node, no tool statistics),
        # so drafts change when a session closes rather than on every run while you work.
        open_from = self.now + 1
        last = None
        for (ts,) in self.db.execute("SELECT ts FROM commands WHERE ts_exact=1 ORDER BY ts DESC"):
            if last is None:
                if self.now - ts > gap:
                    break
            elif last - ts > gap:
                break
            open_from = last = ts
        rows = self.db.execute("SELECT source, shell, ts, ts_exact, text, heads FROM commands ORDER BY ts, rowid")
        for r in rows:
            ts, exact = r["ts"], bool(r["ts_exact"])
            held = exact and ts >= open_from
            if (
                cur is None
                or exact != cur.exact
                or (exact and ts - cur.end > gap)
                or (not exact and (r["source"] != cur.source or ts != cur.end))
            ):
                cur = _Session(ts, ts, exact, r["source"])
                self.sessions.append(cur)
            cur.end = ts
            cur.shells.add(r["shell"])
            cur.cmds.append((ts, r["text"]))
            if held:
                continue
            for name, *args in json.loads(r["heads"]):
                if name == "cd":
                    if args and not args[0].startswith("-"):
                        cur.cds.append(args[0])
                    continue
                target = None
                if name in SHELLS or name in {"source", "."}:
                    target = next((a for a in args if not a.startswith("-")), None)
                elif "/" in name or name in self.by_base:
                    target = name
                if target is not None and (hit := self._script_for(target)) is not None:
                    slot = cur.runs.setdefault(hit[0].id, [0, hit[1], hit[2]])
                    slot[0] += 1
                    if target is name:
                        continue
                if name in shparse.BUILTINS:
                    continue
                tool = shparse.tool_name(name)
                if tool is None or tool in shparse.BUILTINS or tool in skip:
                    continue
                t = self._tool(tool)
                t.names[tool] += 1
                t.uses += 1
                if exact:
                    t.first = ts if t.first is None else min(t.first, ts)
                    t.last = ts if t.last is None else max(t.last, ts)
                canon = self.alias.get(tool, tool)
                if (sub := shparse.subcommand(canon, args)) is not None:
                    t.subs[sub] += 1
                cur.tools[t.slug] += 1
                sample = r["text"].replace("\n", " ⏎ ")[:200]
                if sample not in t.samples:
                    t.samples.append(sample)
        for s in self.sessions:
            for slug in s.tools:
                self.tools[slug].sessions += 1

    # -- nodes ----------------------------------------------------------------

    def _resolve_call(self, name: str, s: _Script) -> tuple[str, str, str] | None:
        """``(node id, provenance, note)`` for a command word used inside script ``s``."""
        if name in s.fns:
            return s.fns[name], EXTRACTED, ""
        for other in s.visible:
            if name in other.fns:
                return other.fns[name], EXTRACTED, f"defined in sourced {other.rel}"
        if ("/" in name or name in self.by_base) and (hit := self._script_for(name, s.path.parent)) is not None:
            if hit[0] is not s:
                return hit[0].id, hit[1], hit[2]
        if name in shparse.BUILTINS:
            return None
        tool = shparse.tool_name(name)
        if tool is None or tool in shparse.BUILTINS:
            return None
        t = self._tool(tool)
        t.names[tool] += 0  # register the spelling without counting an interactive use
        t.script_refs.add(s.id)
        return f"tool/{t.slug}", EXTRACTED, ""

    def _call_edges(self, node: Node, calls: dict[str, list[int]], s: _Script) -> None:
        for name, lines in sorted(calls.items()):
            hit = self._resolve_call(name, s)
            if hit is None or hit[0] == node.id:
                continue
            target, prov, note = hit
            kind = "invokes" if target.startswith("script/") else "calls"
            why = f"{len(lines)}× at {_lines(lines)}" + (f"; {note}" if note else "")
            node.link(kind, target, why, prov, len(lines))

    def _script_nodes(self) -> None:
        for s in self.scripts.values():
            f = s.facts
            fns = f["functions"]
            first_doc = f["description"].split("\n", 1)[0].strip()
            node = Node(
                id=s.id,
                type="script",
                title=s.rel,
                summary=first_doc or f"{f['dialect']} script, {f['loc']} lines, {len(fns)} function(s).",
                tags=sorted(
                    {
                        f["dialect"],
                        s.root.name,
                        *(["strict-mode"] if "-e" in f["set_flags"] else []),
                        *(["has-functions"] if fns else []),
                        *(["parse-errors"] if f["has_errors"] else []),
                    }
                ),
                date=_iso(s.mtime),
                source={
                    "collector": "scripts",
                    "path": _tilde(str(s.path)),
                    "sha256": s.sha[:16],
                    "modified": _iso(s.mtime),
                    "parser": f["parser"],
                },
                meta={
                    "dialect": f["dialect"],
                    "shebang": f["shebang"],
                    "loc": f["loc"],
                    "functions": len(fns),
                    "set_flags": f["set_flags"],
                    "env_reads": f["env_reads"],
                    "exports": f["exports"],
                    "unresolved_sources": f["_unresolved_sources"],
                    "parse_errors": f["has_errors"],
                },
            )
            parts: list[str] = []
            if f["description"] and f["description"].strip() != node.summary:
                parts.append(f["description"].replace("\n", "  \n"))  # keep the comment's line breaks
            for other, prov, note, line in f["_sourced"]:
                node.link("sources", other.id, f"L{line}" + (f"; {note}" if note else ""), prov)
            for fn in fns:
                node.link("defines", fn["id"], f"L{fn['start']}–{fn['end']}" + (f" — {fn['doc']}" if fn["doc"] else ""))
            self._call_edges(node, f["calls"], s)
            repo = self._repo_of(s.path)
            if repo is not None:
                node.link("part_of", repo["id"], f"file lives under {_tilde(repo['path'])}")
                relpath = str(s.path.relative_to(repo["path"]))
                touching = repo["by_file"].get(relpath, [])
                if touching:
                    parts.append(
                        "## Recent commits touching this file\n\n"
                        + "\n".join(f"- {_day(c['ts'])} {_code(c['sha'][:8])} {c['subject']}" for c in touching)
                    )
            if f["env_reads"] or f["exports"]:
                env = ["## Environment", ""]
                if f["env_reads"]:
                    env.append("- Reads (never assigned here): " + ", ".join(_code(v) for v in f["env_reads"]))
                if f["exports"]:
                    env.append("- Exports: " + ", ".join(_code(v) for v in f["exports"]))
                parts.append("\n".join(env))
            if f.get("code"):
                tail = "\n\n_Truncated._" if f.get("truncated") else ""
                parts.append("## Source\n\n" + _fence(f["code"], "bash") + tail)
            node.body = "\n\n".join(parts)
            self.nodes[node.id] = node

            for fn in fns:
                n = fn["end"] - fn["start"] + 1
                fnode = Node(
                    id=fn["id"],
                    type="fn",
                    title=f"{fn['name']}()",
                    summary=fn["doc"] or f"Function in {s.rel}, {n} line(s).",
                    tags=sorted({f["dialect"], s.root.name}),
                    date=_iso(s.mtime),
                    source={
                        "collector": "scripts",
                        "path": _tilde(str(s.path)),
                        "lines": f"L{fn['start']}-L{fn['end']}",
                        "parser": f["parser"],
                    },
                    meta={"name": fn["name"], "script": s.rel, "loc": n},
                )
                fnode.link("defined_in", s.id, f"L{fn['start']}–{fn['end']}")
                self._call_edges(fnode, fn["calls"], s)
                if fn.get("code"):
                    tail = "\n\n_Truncated._" if fn.get("truncated") else ""
                    fnode.body = "## Source\n\n" + _fence(fn["code"], "bash") + tail
                self.nodes[fnode.id] = fnode

    def _session_nodes(self) -> None:
        gap = self.cfg.history.session_gap_minutes * 60
        commit_ts = sorted((c["ts"], repo["id"]) for repo in self.repos.values() for c in repo["commits"])
        repo_names: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for repo in self.repos.values():
            repo_names[repo["name"]].append(repo)
        prev: _Session | None = None
        for s in self.sessions:
            if s.exact and self.now - s.end <= gap:
                continue  # still open: emitted once it has been idle for a full gap
            start = datetime.fromtimestamp(s.start)
            day = start.strftime("%Y-%m-%d")
            if s.exact:
                s.id = f"session/{start:%Y-%m-%d-%H%M}"
                title = f"{start:%Y-%m-%d %H:%M} · {len(s.cmds)} commands"
            else:
                shell = "-".join(sorted(s.shells))
                s.id = f"session/untimed-{shell}-{start:%Y-%m-%d-%H%M}"
                title = f"Untimed {shell} history · {len(s.cmds)} commands"
            top = [self.tools[t].names.most_common(1)[0][0] for t, _ in s.tools.most_common(4)]
            node = Node(
                id=s.id,
                type="session",
                title=title,
                summary=(
                    f"{_dur(s.end - s.start)} of shell work"
                    if s.exact
                    else "History without timestamps, grouped at ingest time"
                )
                + (f"; mostly {', '.join(top)}." if top else "."),
                tags=sorted({start.strftime("%Y-%m"), *s.shells, *([] if s.exact else ["untimed"])}),
                date=_iso(s.start),
                source={
                    "collector": "history",
                    "files": sorted({_tilde(s.source)}),
                    "timing": "exact" if s.exact else "ingest",
                },
                meta={
                    "started": _iso(s.start),
                    "ended": _iso(s.end),
                    "duration_s": s.end - s.start,
                    "commands": len(s.cmds),
                    "shells": sorted(s.shells),
                },
            )
            for slug, n in s.tools.most_common():
                node.link("uses", f"tool/{slug}", f"{n} invocation(s)", EXTRACTED, n)
            for sid, (n, prov, why) in s.runs.items():
                node.link("runs", sid, f"{n} run(s)" + (f"; {why}" if why else ""), prov, n)
            if s.exact:
                for arg in s.cds:
                    p = Path(os.path.expanduser(arg))
                    if p.is_absolute() and (repo := self._repo_of(Path(os.path.normpath(p)))) is not None:
                        node.link("touches", repo["id"], f"`cd {arg}`")
                    elif len(hits := repo_names.get(arg.rstrip("/").rsplit("/", 1)[-1], [])) == 1:
                        node.link("touches", hits[0]["id"], f"`cd {arg}` matches the repository name", INFERRED)
                lo = bisect.bisect_left(commit_ts, (s.start, ""))
                hi = bisect.bisect_right(commit_ts, (s.end + 300, "￿"))
                for rid, n in Counter(r for _, r in commit_ts[lo:hi]).items():
                    node.link("touches", rid, f"{n} commit(s) timestamped inside the session window", INFERRED, n)
                node.link("on_day", f"day/{day}")
                if prev is not None:
                    node.link("follows", prev.id, f"previous session, {_dur(s.start - prev.end)} earlier")
                prev = s
                facts = self.day_facts[day]
                facts["sessions"] += 1
                facts["commands"] += len(s.cmds)
            listed = s.cmds[-_MAX_SESSION_LISTED:]
            rows = [
                (f"{datetime.fromtimestamp(ts):%H:%M:%S}  " if s.exact else "")
                + text.replace("\n", "\n" + " " * (10 if s.exact else 0))
                for ts, text in listed
            ]
            note = f"Showing the last {len(listed)} of {len(s.cmds)} commands.\n\n" if len(listed) < len(s.cmds) else ""
            node.body = "## Commands\n\n" + note + _fence("\n".join(rows), "text")
            self.nodes[node.id] = node

    def _tool_nodes(self) -> None:
        floor = self.cfg.history.min_tool_uses
        qualified = {slug for slug, t in self.tools.items() if t.uses >= floor or t.script_refs}

        # Inferred `pairs_with`: normalised PMI over timed sessions.
        timed = [set(s.tools) & qualified for s in self.sessions if s.exact]
        n = len(timed)
        df: Counter[str] = Counter()
        co: Counter[tuple[str, str]] = Counter()
        for tools in timed:
            df.update(tools)
            co.update(combinations(sorted(tools), 2))
        pairs: dict[str, list[tuple[float, int, str]]] = defaultdict(list)
        for (a, b), nab in co.items():
            # A tool present in most sessions co-occurs with everything; that says nothing.
            if nab < 3 or df[a] < 5 or df[b] < 5 or max(df[a], df[b]) > 0.6 * n:
                continue
            npmi = math.log(nab * n / (df[a] * df[b])) / -math.log(nab / n)
            if npmi >= 0.25:
                pairs[a].append((npmi, nab, b))
                pairs[b].append((npmi, nab, a))

        for slug in sorted(qualified):
            t = self.tools[slug]
            name = t.names.most_common(1)[0][0] if t.names else slug
            spellings = sorted(set(t.names) - {name})
            how = ([f"{t.uses} interactive use(s) across {t.sessions} session(s)"] if t.uses else []) + (
                [f"called by {len(t.script_refs)} script(s)"] if t.script_refs else []
            )
            node = Node(
                id=f"tool/{slug}",
                type="tool",
                title=name,
                summary=f"{_code(name)}: " + "; ".join(how) + ".",
                tags=sorted(
                    {
                        *([_TAG_OF[name]] if name in _TAG_OF else []),
                        *(["interactive"] if t.uses else []),
                        *(["scripted"] if t.script_refs else []),
                    }
                ),
                date=_iso(t.last) if t.last else None,
                source={"collector": "derived", "from": ["history", "scripts"]},
                meta={
                    "command": name,
                    "interactive_uses": t.uses,
                    "sessions": t.sessions,
                    "script_refs": len(t.script_refs),
                    "first_seen": _iso(t.first) if t.first else None,
                    "last_seen": _iso(t.last) if t.last else None,
                    "subcommands": dict(t.subs.most_common(12)),
                    "folded_spellings": spellings,
                },
            )
            for npmi, nab, other in sorted(pairs.get(slug, []), reverse=True)[:5]:
                node.link(
                    "pairs_with",
                    f"tool/{other}",
                    f"together in {nab} of {min(df[slug], df[other])} sessions (NPMI {npmi:.2f})",
                    INFERRED,
                    round(npmi, 3),
                )
            parts = []
            if t.subs:
                parts.append(
                    "## Subcommands\n\n"
                    + "\n".join(f"- {_code(f'{name} {sub}')} × {c}" for sub, c in t.subs.most_common(12))
                )
            if t.samples:
                parts.append("## Recent invocations\n\n" + _fence("\n".join(t.samples), "text"))
            node.body = "\n\n".join(parts)
            self.nodes[node.id] = node

    def _repo_nodes(self) -> None:
        for repo in self.repos.values():
            commits = repo["commits"]
            files: Counter[str] = Counter()
            for c in commits:
                files.update(json.loads(c["files"]))
                self.day_facts[_day(c["ts"])]["commits"][repo["id"]].append(c)
            node = Node(
                id=repo["id"],
                type="repo",
                title=repo["name"],
                summary=f"Git repository at {_tilde(repo['path'])}; {len(commits)} commit(s) in the lookback window.",
                tags=["git"],
                date=_iso(commits[0]["ts"]) if commits else None,
                source={"collector": "git", "path": _tilde(repo["path"])},
                meta={
                    "remote": repo["remote"],
                    "branch": repo["branch"],
                    "commits": len(commits),
                    "authors": len({c["email"] for c in commits}),
                    "first_commit": _iso(commits[-1]["ts"]) if commits else None,
                    "last_commit": _iso(commits[0]["ts"]) if commits else None,
                },
            )
            parts = []
            if commits:
                parts.append(
                    "## Recent commits\n\n"
                    + "\n".join(
                        f"- {_day(c['ts'])} {_code(c['sha'][:8])} {c['subject']} (+{c['ins']}/−{c['del']})"
                        for c in commits[:30]
                    )
                )
            if files:
                parts.append(
                    "## Most changed files\n\n" + "\n".join(f"- {_code(f)} × {n}" for f, n in files.most_common(10))
                )
            node.body = "\n\n".join(parts)
            self.nodes[node.id] = node

    def _log_nodes(self) -> None:
        rows = self.db.execute("SELECT * FROM log_agg ORDER BY source, day").fetchall()
        by_source: dict[str, list] = defaultdict(list)
        for r in rows:
            by_source[r["source"]].append(r)
        globs = {s.name: [_tilde(os.path.expanduser(g)) for g in s.globs] for s in self.cfg.logs.sources}
        for name, items in by_source.items():
            lid = f"log/{slugify(name)}"
            totals: Counter[str] = Counter()
            sigs: dict[tuple[str, str], dict[str, Any]] = {}
            days: dict[str, list] = defaultdict(list)
            for r in items:
                totals[r["level"]] += r["count"]
                days[r["day"]].append(r)
                if r["sig"]:
                    slot = sigs.setdefault((r["level"], r["sig"]), {"count": 0, "days": set(), "sample": r["sample"]})
                    slot["count"] += r["count"]
                    slot["days"].add(r["day"])
            node = Node(
                id=lid,
                type="log",
                title=name,
                summary=f"Log family: {sum(totals.values())} line(s) over {len(days)} day(s); "
                f"{totals['error']} error(s), {totals['warn']} warning(s).",
                tags=["logs", *(["errors"] if totals["error"] else [])],
                date=f"{max(days)}T00:00:00" if days else None,
                source={"collector": "logs", "globs": globs.get(name, [])},
                meta={"lines": dict(totals), "days": len(days), "signatures": len(sigs)},
            )
            recurring = sorted(sigs.items(), key=lambda kv: (-len(kv[1]["days"]), -kv[1]["count"]))[:20]
            if recurring:
                table = [
                    "## Recurring signatures",
                    "",
                    "| Level | Days | Count | First | Last | Signature |",
                    "| --- | ---: | ---: | --- | --- | --- |",
                ]
                for (level, sig), v in recurring:
                    table.append(
                        f"| {level} | {len(v['days'])} | {v['count']} | {min(v['days'])} | "
                        f"{max(v['days'])} | {_code(sig)} |"
                    )
                node.body = "\n".join(table)
            self.nodes[lid] = node

            for day, drows in days.items():
                counts: Counter[str] = Counter()
                for r in drows:
                    counts[r["level"]] += r["count"]
                detailed = sorted((r for r in drows if r["sig"]), key=lambda r: -r["count"])
                dnode = Node(
                    id=f"logday/{slugify(name)}--{day}",
                    type="logday",
                    title=f"{name} · {day}",
                    summary=f"{sum(counts.values())} line(s): {counts['error']} error(s), "
                    f"{counts['warn']} warning(s), {len(detailed)} distinct signature(s).",
                    tags=sorted({day[:7], *(["errors"] if counts["error"] else [])}),
                    date=f"{day}T00:00:00",
                    source={"collector": "logs", "family": name},
                    meta={"lines": dict(counts), "signatures": len(detailed)},
                )
                dnode.link("from_source", lid)
                dnode.link("on_day", f"day/{day}")
                parts = []
                for r in detailed[:15]:
                    new = " · first seen this day" if min(sigs[(r["level"], r["sig"])]["days"]) == day else ""
                    parts.append(
                        f"### {r['level']} × {r['count']}{new}\n\n{_code(r['sig'])}\n\n"
                        + _fence(r["sample"] or "", "text")
                    )
                dnode.body = "\n\n".join(parts)
                self.nodes[dnode.id] = dnode
                self.day_facts[day]["logs"].append(dnode.id)

    def _day_nodes(self) -> None:
        for day, f in sorted(self.day_facts.items()):
            ncommits = sum(len(v) for v in f["commits"].values())
            bits = (
                ([f"{f['sessions']} session(s), {f['commands']} command(s)"] if f["sessions"] else [])
                + ([f"{ncommits} commit(s) in {len(f['commits'])} repo(s)"] if ncommits else [])
                + ([f"{len(f['logs'])} log digest(s)"] if f["logs"] else [])
            )
            d = datetime.strptime(day, "%Y-%m-%d")
            node = Node(
                id=f"day/{day}",
                type="day",
                title=f"{day} ({d:%a})",
                summary="; ".join(bits) + ".",
                tags=[day[:7], d.strftime("%A").lower()],
                date=f"{day}T00:00:00",
                source={"collector": "derived", "from": ["history", "git", "logs"]},
                meta={
                    "sessions": f["sessions"],
                    "commands": f["commands"],
                    "commits": ncommits,
                    "log_digests": len(f["logs"]),
                },
            )
            parts = []
            for rid, commits in sorted(f["commits"].items()):
                node.link("touches", rid, f"{len(commits)} commit(s)", EXTRACTED, len(commits))
                title = self.nodes[rid].title if rid in self.nodes else rid
                parts.append(
                    f"## Commits in {title}\n\n"
                    + "\n".join(
                        f"- {datetime.fromtimestamp(c['ts']):%H:%M} {_code(c['sha'][:8])} {c['subject']} "
                        f"(+{c['ins']}/−{c['del']}, {len(json.loads(c['files']))} file(s))"
                        for c in sorted(commits, key=lambda c: c["ts"])
                    )
                )
            node.body = "\n\n".join(parts)
            self.nodes[node.id] = node

    # -- the loop's return path ------------------------------------------------

    def _annotate(self) -> None:
        for node in self.nodes.values():
            node.machine = {"tags": list(node.tags), "summary": node.summary}
        for nid, a in self.ann.items():
            node = self.nodes.get(nid)
            if node is None:
                continue
            if a.get("status"):
                node.status = a["status"]
            removed = set(a.get("tags_remove") or [])
            node.tags = [t for t in node.tags if t not in removed]
            node.tags += [t for t in a.get("tags_add") or [] if t not in node.tags]
            if a.get("summary"):
                node.summary = a["summary"]
            node.aliases = list(a.get("aliases") or [])
            for r in a.get("relations") or []:
                if r.get("type") and r.get("target"):
                    node.edges.append(
                        Edge(
                            str(r["type"]),
                            str(r["target"]),
                            CURATED,
                            str(r.get("why") or ""),
                            float(r.get("weight") or 1),
                        )
                    )
            node.extra = dict(a.get("extra") or {})
            node.notes = a.get("notes") or ""

    def _prune(self) -> None:
        for nid in self.ignored:
            self.nodes.pop(nid, None)
        for node in self.nodes.values():
            node.edges = [
                e for e in node.edges if e.provenance == CURATED or (e.target in self.nodes and e.target != node.id)
            ]


def build(cfg: Config, store: Store, now: int) -> tuple[dict[str, Node], set[str]]:
    b = Builder(cfg, store, now)
    return b.run(), b.ignored
