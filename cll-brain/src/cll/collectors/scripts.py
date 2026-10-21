"""Shell scripts → structural facts (functions, calls, sourced files, variables).

Two parsers produce the same ``facts`` dict:

* ``tree-sitter`` — exact, from the tree-sitter-bash grammar (``uv sync --extra ts``).
* ``heuristic``   — the scanner in :mod:`cll.shparse`; dependency-free, best effort.

``facts`` schema::

    {parser, has_errors, shebang, dialect, loc, description, set_flags,
     functions: [{name, start, end, doc, calls: {name: [line, …]}}],
     calls:   {name: [line, …]},      # every command in the file, any scope
     sources: [{target, line}],       # `source x` / `. x`
     env_reads: [NAME, …],            # read but never assigned in this file
     exports:   [NAME, …]}
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from cll import shparse
from cll.config import Config
from cll.redact import Redactor
from cll.state import Store

_SHEBANG = re.compile(r"#!\s*(?:\S*/env\s+(?:-\S+\s+)*)?(?:\S*/)?(bash|zsh|sh|ksh|dash|ash)\b")
_SPECIAL_VARS = frozenset(
    {
        "HOME",
        "PATH",
        "PWD",
        "OLDPWD",
        "USER",
        "SHELL",
        "IFS",
        "RANDOM",
        "LINENO",
        "SECONDS",
        "HOSTNAME",
        "BASH_SOURCE",
        "BASH_VERSION",
        "FUNCNAME",
        "PIPESTATUS",
        "REPLY",
        "OPTARG",
        "OPTIND",
        "EUID",
        "UID",
        "PPID",
        "TERM",
        "LANG",
        "TMPDIR",
        "ZSH_VERSION",
        "BASHPID",
        "OSTYPE",
        "HOSTTYPE",
        "MACHTYPE",
        "COLUMNS",
        "LINES",
        "BASH_REMATCH",
        "EPOCHSECONDS",
        "SHLVL",
        "argv",
        "status",
        "LOGNAME",
    }
)


def _ts_parser():
    try:
        import tree_sitter_bash
        from tree_sitter import Language, Parser
    except ImportError:
        return None
    return Parser(Language(tree_sitter_bash.language()))


def _header(text: str) -> tuple[str | None, str | None, str]:
    """(shebang, dialect, leading comment block used as the description)."""
    lines = text.split("\n")
    shebang = lines[0].strip() if lines and lines[0].startswith("#!") else None
    dialect = None
    if shebang and (m := _SHEBANG.match(shebang)):
        dialect = m[1]
    doc: list[str] = []
    for line in lines[1 if shebang else 0 :]:
        s = line.strip()
        if s.startswith("#") and not s.startswith("#!"):
            body = s.lstrip("#").strip()
            if body.startswith(("shellcheck", "vim:", "-*-")):
                continue
            doc.append(body)
        elif s == "" and not doc:
            continue
        else:
            break
    while doc and not doc[-1]:
        doc.pop()
    return shebang, dialect, "\n".join(doc)[:600]


def _callable(word: str) -> str | None:
    """A command word worth recording: a literal name, or a path whose last segment is literal
    (``"$DIR/notify.sh"``). A bare ``$cmd`` says nothing about what runs."""
    w = word.strip("\"'")
    if not w:
        return None
    if "$" not in w and "…" not in w:
        return w
    base = w.rsplit("/", 1)[-1]
    return w if "/" in w and base and "$" not in base and "…" not in base else None


def _set_flags(text: str) -> list[str]:
    """Options enabled by ``set``: ``set -euo pipefail`` → ``['-e', '-u', 'pipefail']``."""
    flags: set[str] = set()
    for m in re.finditer(r"^\s*set\s+([-+][^\n#;]*)", text, re.M):
        toks = m[1].split()
        i = 0
        while i < len(toks) and toks[i][0] in "-+" and toks[i] != "--":
            sign, letters = toks[i][0], toks[i][1:]
            if letters.endswith("o") and i + 1 < len(toks) and re.fullmatch(r"[a-z]+", toks[i + 1]):
                flags.add(toks[i + 1] if sign == "-" else f"+{toks[i + 1]}")
                letters = letters[:-1]
                i += 1
            flags.update(sign + c for c in letters if c.isalpha())
            i += 1
    return sorted(flags)


def _doc_above(lines: list[str], start_line: int) -> str:
    """Comment lines directly above a function definition (1-based ``start_line``)."""
    out: list[str] = []
    i = start_line - 2
    while i >= 0 and lines[i].strip().startswith("#") and not lines[i].startswith("#!"):
        out.append(lines[i].strip().lstrip("#").strip())
        i -= 1
    return " ".join(reversed(out))[:300]


def _finish(facts: dict[str, Any], text: str, reads: set[str], assigned: set[str], exports: set[str]) -> dict[str, Any]:
    shebang, dialect, desc = _header(text)
    flags = _set_flags(text)
    facts.update(
        shebang=shebang,
        dialect=dialect,
        loc=text.count("\n") + (not text.endswith("\n")),
        description=desc,
        set_flags=flags,
        env_reads=sorted(v for v in reads - assigned - _SPECIAL_VARS if v.isupper()),
        exports=sorted(exports),
    )
    return facts


# -- tree-sitter -------------------------------------------------------------


def parse_tree_sitter(parser, text: str) -> dict[str, Any]:
    src = text.encode("utf-8", errors="replace")
    tree = parser.parse(src)
    lines = text.split("\n")
    functions: list[dict[str, Any]] = []
    calls: dict[str, list[int]] = {}
    sources: list[dict[str, Any]] = []
    reads: set[str] = set()
    assigned: set[str] = set()
    exports: set[str] = set()

    def txt(node) -> str:
        return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    # Iterative walk; each stack entry carries the enclosing function's call table.
    stack: list[tuple[Any, dict[str, list[int]] | None]] = [(tree.root_node, None)]
    while stack:
        node, scope = stack.pop()
        kind = node.type
        if kind == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                start = node.start_point[0] + 1
                fn = {
                    "name": txt(name_node),
                    "start": start,
                    "end": node.end_point[0] + 1,
                    "doc": _doc_above(lines, start),
                    "calls": {},
                }
                functions.append(fn)
                scope = fn["calls"]
        elif kind == "command":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                line = node.start_point[0] + 1
                words = [txt(name_node), *(txt(a) for a in node.children_by_field_name("argument"))]
                head = shparse.command_head(words)
                if head is not None:
                    name, args = head
                    if name in {"source", "."}:
                        if args:
                            sources.append({"target": args[0], "line": line})
                    elif (name := _callable(name)) is not None:
                        calls.setdefault(name, []).append(line)
                        if scope is not None:
                            scope.setdefault(name, []).append(line)
        elif kind == "variable_assignment":
            n = node.child_by_field_name("name")
            if n is not None:
                assigned.add(txt(n).split("[", 1)[0])
        elif kind == "declaration_command":
            head = txt(node.children[0]) if node.children else ""
            for c in node.children[1:]:
                if c.type == "variable_assignment":
                    target = c.child_by_field_name("name")
                elif c.type in {"variable_name", "word"}:
                    target = c
                else:
                    target = None
                if target is not None:
                    name = txt(target).split("[", 1)[0]
                    if re.fullmatch(r"[A-Za-z_]\w*", name):
                        assigned.add(name)
                        if head == "export":
                            exports.add(name)
        elif (
            kind == "variable_name"
            and node.parent is not None
            and node.parent.type in {"simple_expansion", "expansion"}
        ):
            reads.add(txt(node))
        elif kind in {"for_statement", "select_statement"}:
            n = node.child_by_field_name("variable")
            if n is not None:
                assigned.add(txt(n))
        if kind == "comment":
            continue
        stack.extend((c, scope) for c in reversed(node.children))

    # `read VAR …` assigns too.
    for m in re.finditer(r"\bread\s+(?:-\w+\s+(?:\S+\s+)?)*((?:[A-Za-z_]\w*\s*)+)", text):
        assigned.update(m[1].split())
    functions.sort(key=lambda f: f["start"])
    facts = {
        "parser": "tree-sitter",
        "has_errors": bool(tree.root_node.has_error),
        "functions": functions,
        "calls": calls,
        "sources": sources,
    }
    return _finish(facts, text, reads, assigned, exports)


# -- heuristic ---------------------------------------------------------------

_FN_DEF = re.compile(
    r"^(?P<indent>[ \t]*)(?:function[ \t]+(?P<n1>[A-Za-z_][\w:.\-]*)[ \t]*(?:\(\))?|(?P<n2>[A-Za-z_][\w:.\-]*)[ \t]*\(\))[ \t]*(?P<rest>.*)$",
    re.M,
)


def parse_heuristic(text: str) -> dict[str, Any]:
    clean = shparse.blank_heredocs(text)
    lines = clean.split("\n")
    line_starts = [0]
    for ln in lines[:-1]:
        line_starts.append(line_starts[-1] + len(ln) + 1)

    functions: list[dict[str, Any]] = []
    for m in _FN_DEF.finditer(clean):
        name = m["n1"] or m["n2"]
        if name in shparse.PREFIX_KEYWORDS or name in shparse.NON_COMMAND_HEADS:
            continue
        start = bisect.bisect_right(line_starts, m.start()) or 1
        rest = m["rest"].strip()
        if rest.startswith("{") and rest.endswith("}") and len(rest) > 1:
            end = start  # one-liner
        else:
            close = re.compile(rf"^{re.escape(m['indent'])}\}}")
            end = next((i + 1 for i in range(start, len(lines)) if close.match(lines[i])), len(lines))
        functions.append(
            {"name": name, "start": start, "end": end, "doc": _doc_above(text.split("\n"), start), "calls": {}}
        )

    calls: dict[str, list[int]] = {}
    sources: list[dict[str, Any]] = []
    for offset, words in shparse.split_commands(clean):
        head = shparse.command_head(words)
        if head is None:
            continue
        name, args = head
        line = bisect.bisect_right(line_starts, offset) or 1
        if name in {"source", "."}:
            if args:
                sources.append({"target": args[0], "line": line})
            continue
        if (name := _callable(name)) is None:
            continue
        calls.setdefault(name, []).append(line)
        owner = next((f for f in reversed(functions) if f["start"] <= line <= f["end"]), None)
        if owner is not None and not (line == owner["start"] and name == owner["name"]):
            owner["calls"].setdefault(name, []).append(line)

    reads = set(re.findall(r"\$\{?[#!]?([A-Za-z_][A-Za-z0-9_]*)", clean))
    assigned = set(
        re.findall(
            r"(?:^|[\s;(])(?:(?:export|local|declare|typeset|readonly)\s+(?:-\w+\s+)*)?([A-Za-z_]\w*)\+?=", clean
        )
    )
    assigned |= set(re.findall(r"\bfor\s+([A-Za-z_]\w*)\s+in\b", clean))
    for m in re.finditer(r"\bread\s+(?:-\w+\s+(?:\S+\s+)?)*((?:[A-Za-z_]\w*\s*)+)", clean):
        assigned.update(m[1].split())
    exports = set(re.findall(r"\bexport\s+(?:-\w+\s+)*([A-Za-z_]\w*)", clean))
    facts = {"parser": "heuristic", "has_errors": False, "functions": functions, "calls": calls, "sources": sources}
    return _finish(facts, text, reads, assigned, exports)


# -- discovery ---------------------------------------------------------------


def _is_shell_script(path: Path, extensions: tuple[str, ...]) -> bool:
    if path.suffix.lower() in extensions:
        return True
    if path.suffix:
        return False
    try:
        with path.open("rb") as fh:
            first = fh.readline(200)
    except OSError:
        return False
    return bool(_SHEBANG.match(first.decode("utf-8", errors="replace")))


def _same_settings(facts_json: str, parser: str, cfg: Config) -> bool:
    prev = json.loads(facts_json)
    return prev.get("parser") == parser and prev.get("embed") == cfg.scripts.embed_source


def discover(cfg: Config) -> list[tuple[Path, Path]]:
    """``(root, file)`` pairs for every shell script under the configured roots."""
    found: list[tuple[Path, Path]] = []
    exclude = set(cfg.scripts.exclude)
    for root in cfg.scripts.roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in exclude)
            for name in sorted(filenames):
                p = Path(dirpath) / name
                if p.is_symlink() and not p.exists():
                    continue
                try:
                    if p.stat().st_size > cfg.scripts.max_file_bytes:
                        continue
                except OSError:
                    continue
                if _is_shell_script(p, cfg.scripts.extensions):
                    found.append((root, p))
    return found


_MAX_FN_LINES = 150
_MAX_SCRIPT_LINES = 400


def _embed(facts: dict[str, Any], text: str, redactor: Redactor, mode: str) -> None:
    """Attach scrubbed source so the event store never holds raw script text."""
    lines = text.split("\n")
    facts["description"] = redactor.scrub(facts["description"])
    for fn in facts["functions"]:
        fn["doc"] = redactor.scrub(fn["doc"])
        if mode == "none":
            continue
        chunk = lines[fn["start"] - 1 : fn["end"]]
        fn["truncated"] = len(chunk) > _MAX_FN_LINES
        fn["code"] = redactor.scrub("\n".join(chunk[:_MAX_FN_LINES]))
    if mode == "all":
        facts["truncated"] = len(lines) > _MAX_SCRIPT_LINES
        facts["code"] = redactor.scrub("\n".join(lines[:_MAX_SCRIPT_LINES]))


def collect(cfg: Config, store: Store, redactor: Redactor) -> dict[str, Any]:
    stats: dict[str, Any] = {"files": 0, "parsed": 0, "parser": "heuristic"}
    parser = None
    if cfg.scripts.parser in {"auto", "tree-sitter"}:
        parser = _ts_parser()
        if parser is None and cfg.scripts.parser == "tree-sitter":
            raise RuntimeError("scripts.parser = 'tree-sitter' but tree-sitter is not installed (uv sync --extra ts)")
    stats["parser"] = "tree-sitter" if parser is not None else "heuristic"
    now = int(time.time())
    known = {r["path"]: r for r in store.db.execute("SELECT path, sha, facts FROM scripts")}
    for root, path in discover(cfg):
        stats["files"] += 1
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        sha = hashlib.sha256(raw).hexdigest()
        prev = known.get(str(path))
        if prev is not None and prev["sha"] == sha and _same_settings(prev["facts"], stats["parser"], cfg):
            store.db.execute("UPDATE scripts SET seen=? WHERE path=?", (now, str(path)))
            continue
        text = raw.decode("utf-8", errors="replace")
        facts = parse_tree_sitter(parser, text) if parser is not None else parse_heuristic(text)
        _embed(facts, text, redactor, cfg.scripts.embed_source)
        facts["embed"] = cfg.scripts.embed_source
        if facts["dialect"] is None:
            facts["dialect"] = path.suffix.lstrip(".").lower() or "sh"
        stats["parsed"] += 1
        store.db.execute(
            "INSERT INTO scripts VALUES (?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
            "root=excluded.root, rel=excluded.rel, sha=excluded.sha, mtime=excluded.mtime, "
            "facts=excluded.facts, seen=excluded.seen",
            (
                str(path),
                str(root),
                str(path.relative_to(root)),
                sha,
                path.stat().st_mtime,
                json.dumps(facts, sort_keys=True),
                now,
            ),
        )
    # Files that vanished from disk leave the graph on the next build. A root that is
    # merely unavailable right now (unmounted volume) keeps its rows.
    roots = {str(r) for r in cfg.scripts.roots}
    for row in store.db.execute("SELECT path, root FROM scripts WHERE seen < ?", (now,)).fetchall():
        if row["root"] not in roots or (Path(row["root"]).is_dir() and not Path(row["path"]).exists()):
            store.db.execute("DELETE FROM scripts WHERE path=?", (row["path"],))
    store.db.commit()
    return stats
