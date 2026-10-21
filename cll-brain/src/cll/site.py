"""Static site: one self-contained ``index.html`` (no build step, no framework).

The page embeds every node as JSON and renders it with plain JavaScript:
card and graph views, type/tag/status/provenance filters, full-text search,
and a page per node with typed relations and backlinks. Routes are URL
fragments (``#tool~git``), so the file works from ``file://`` and from any
static host without rewrite rules.

Input is either the live graph from the event store (default) or any directory
of cll-schema markdown (``--from``), e.g. the vault your own ingest writes to.
"""

from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt

from cll.config import Config
from cll.render import BASE_KEYS, documents, parse_document, split_notes, strip_relations
from cll.state import Store

_FONTS = {
    True: (
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500'
        '&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Serif:wght@500;600&display=swap">'
    ),
    False: "",
}
_STACKS = {
    "__FONT_DISPLAY__": '"IBM Plex Serif", "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif',
    "__FONT_BODY__": '"IBM Plex Sans", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
    "__FONT_MONO__": '"IBM Plex Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace',
}
_WIKILINK = re.compile(r"\[\[([^\[\]|#]+)(?:\|([^\[\]]*))?\]\]")
_MD_LINK = re.compile(r"\]\((?:\.\./)*([a-z]+/[a-z0-9\-_]+)\.md\)")
_H1 = re.compile(r"\A\s*# [^\n]*\n")
_LEDE = re.compile(r"\A\s*> [^\n]*\n")


def _href(node_id: str) -> str:
    return "#" + node_id.replace("/", "~")


def _resolve_links(md: str, known: set[str]) -> str:
    """Rewrite wikilinks and relative ``.md`` links to in-page routes, outside code only."""
    out: list[str] = []
    fence: str | None = None
    for line in md.split("\n"):
        stripped = line.lstrip()
        if fence is None and (m := re.match(r"(`{3,}|~{3,})", stripped)):
            fence = m[1]
        elif fence is not None:
            if stripped.startswith(fence) and not stripped[len(fence) :].strip():
                fence = None
        elif "[[" in line or ".md)" in line:
            parts = re.split(r"(`+[^`]*`+)", line)  # keep inline code untouched
            for i in range(0, len(parts), 2):
                parts[i] = _WIKILINK.sub(
                    lambda m: f"[{(m[2] or m[1]).strip()}]({_href(m[1].strip())})" if m[1].strip() in known else m[0],
                    parts[i],
                )
                parts[i] = _MD_LINK.sub(lambda m: f"]({_href(m[1])})" if m[1] in known else m[0], parts[i])
            line = "".join(parts)
        out.append(line)
    return "\n".join(out)


def _to_html(md: MarkdownIt, body: str, known: set[str]) -> str:
    machine, notes = split_notes(body)
    machine = _LEDE.sub("", _H1.sub("", strip_relations(machine), count=1), count=1)
    text = machine.strip()
    if notes.strip():
        heading = "" if notes.lstrip().startswith("#") else "## Notes\n\n"
        text += "\n\n" + heading + notes.strip()
    return md.render(_resolve_links(text, known))


def _plain(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def load_directory(root: Path) -> list[tuple[dict[str, Any], str]]:
    docs = []
    for path in sorted(root.rglob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            doc = parse_document(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if doc is None:
            continue
        fm, text = doc
        fm.setdefault("id", str(path.relative_to(root).with_suffix("")))
        fm.setdefault("type", path.parent.name if path.parent != root else "note")
        fm.setdefault("title", path.stem)
        docs.append((fm, text))
    return docs


def payload(docs: list[tuple[dict[str, Any], str]], cfg: Config) -> dict[str, Any]:
    md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
    known = {str(fm["id"]) for fm, _ in docs}
    nodes = []
    for fm, text in docs:
        if fm.get("status") == "ignore":
            continue
        props = {**(fm.get("source") or {}), **{k: v for k, v in fm.items() if k not in BASE_KEYS}}
        for k in ("created", "updated", "rev"):
            if fm.get(k) is not None:
                props[k] = fm[k]
        nodes.append(
            _plain(
                {
                    "id": str(fm["id"]),
                    "type": str(fm.get("type") or "note"),
                    "title": str(fm.get("title") or fm["id"]),
                    "summary": str(fm.get("summary") or ""),
                    "status": str(fm.get("status") or "draft"),
                    "tags": [str(t) for t in fm.get("tags") or []],
                    "date": fm.get("date"),
                    "updated": fm.get("updated"),
                    "props": props,
                    "edges": [
                        {
                            "type": str(e.get("type")),
                            "target": str(e.get("target")),
                            "provenance": str(e.get("provenance") or "curated"),
                            "why": str(e.get("why") or ""),
                            "weight": e.get("weight", 1),
                        }
                        for e in fm.get("relations") or []
                        if isinstance(e, dict) and e.get("target")
                    ],
                    "html": _to_html(md, text, known),
                }
            )
        )
    return {
        "config": {"title": cfg.site.title, "page_size": cfg.site.page_size, "graph_node_cap": cfg.site.graph_node_cap},
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "nodes": nodes,
    }


def render_page(data: dict[str, Any], cfg: Config, fragment: bool = False) -> str:
    template = resources.files("cll").joinpath("site_template.html").read_text(encoding="utf-8")
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/").replace("<!--", "<\\!--")
    page = template.replace("__FONT_LINK__", _FONTS[bool(cfg.site.google_fonts)])
    for key, stack in _STACKS.items():
        page = page.replace(key, stack)
    page = page.replace("__TITLE__", html.escape(cfg.site.title)).replace("__TAGLINE__", html.escape(cfg.site.tagline))
    page = page.replace("__NOTICE__", html.escape(cfg.site.notice))
    page = page.replace("__DATA__", blob)  # last: node text may contain the other placeholders
    if fragment:
        return page
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<meta name="generator" content="cll">\n</head>\n<body>\n' + page + "</body>\n</html>\n"
    )


def build(
    cfg: Config, source: Path | None = None, out_dir: Path | None = None, fragment: bool = False
) -> dict[str, Any]:
    if source is not None:
        docs = load_directory(source)
    else:
        from cll import pipeline  # late import: pipeline imports the collectors

        with Store(cfg.paths.state_dir) as store:
            nodes, _ = pipeline.graph(cfg, store, int(time.time()))
            rows = store.node_rows()
            docs = []
            for nid, (fm, text) in documents(nodes, cfg).items():
                row = rows.get(nid)
                if row is not None:
                    fm.update(created=row["created"], updated=row["updated"], rev=row["rev"])
                docs.append((fm, text))
    data = payload(docs, cfg)
    out = out_dir or cfg.paths.site_dir
    out.mkdir(parents=True, exist_ok=True)
    target = out / "index.html"
    target.write_text(render_page(data, cfg, fragment), encoding="utf-8")
    return {
        "path": str(target),
        "nodes": len(data["nodes"]),
        "edges": sum(len(n["edges"]) for n in data["nodes"]),
        "bytes": target.stat().st_size,
    }
