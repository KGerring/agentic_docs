"""Graph model shared by the builder, the renderer and the site generator."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# How an edge came to exist.
EXTRACTED = "extracted"  # parsed from a source: a fact
INFERRED = "inferred"  # statistical or name-based: a hypothesis, with its evidence in `why`
CURATED = "curated"  # written by a person in a draft's frontmatter

NODE_TYPES = ("tool", "script", "fn", "session", "day", "repo", "log", "logday")

# (forward, inverse) pairs: a backlink is suppressed when the inverse edge already exists.
INVERSE = {"defines": "defined_in", "defined_in": "defines"}

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    return _SLUG.sub("-", text.lower().replace("+", "p")).strip("-") or "x"


def slug_path(parts: list[str]) -> str:
    return "--".join(slugify(p) for p in parts if p)


@dataclass(slots=True)
class Edge:
    type: str
    target: str
    provenance: str = EXTRACTED
    why: str = ""
    weight: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type, "target": self.target, "provenance": self.provenance}
        if self.why:
            d["why"] = self.why
        if self.weight != 1.0:
            d["weight"] = int(self.weight) if float(self.weight).is_integer() else round(self.weight, 3)
        return d


@dataclass(slots=True)
class Node:
    id: str
    type: str
    title: str
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)  # type-specific frontmatter
    source: dict[str, Any] = field(default_factory=dict)  # provenance of the node itself
    body: str = ""
    edges: list[Edge] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    status: str = "draft"
    date: str | None = None  # the moment the node is about (ISO 8601), if any
    notes: str = ""  # human-owned tail of the body
    extra: dict[str, Any] = field(default_factory=dict)  # human-added frontmatter keys
    machine: dict[str, Any] = field(default_factory=dict)  # tags/summary before human edits were applied

    def link(self, type_: str, target: str, why: str = "", provenance: str = EXTRACTED, weight: float = 1.0) -> None:
        for e in self.edges:
            if e.type == type_ and e.target == target:
                e.weight += weight
                return
        self.edges.append(Edge(type_, target, provenance, why, weight))
