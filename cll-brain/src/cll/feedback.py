"""Drafts → annotations: the return half of the loop.

Before each collection the drafts directory is scanned and every human edit is
diffed against what the machine last wrote for that node. The difference is
stored as an annotation and applied to all later builds, including after the
downstream ingest has consumed the file.

==============================  ===============================================
Edit in a draft                 Effect on later runs
==============================  ===============================================
``status: ignore``              node and every edge to it leave the graph
``status: keep`` / ``promote``  carried through (for your ingest to act on)
add / remove ``tags``           kept; removed machine tags stay removed
rewrite ``summary``             your wording replaces the generated one
``aliases`` on a ``tool/…``     those command names are folded into that tool
``relations`` entry, curated    kept as an edge with ``provenance: curated``
any other new key               preserved verbatim
text below the notes marker     preserved verbatim
==============================  ===============================================
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from typing import Any

from cll import SCHEMA
from cll.config import Config
from cll.render import BASE_KEYS, curated, file_sig, parse_document, split_notes
from cll.state import Store

_MACHINE_STATUS = {"draft", "stale", None, ""}


def _plain(value: Any) -> Any:
    """YAML scalars such as dates → JSON-safe values."""
    return json.loads(json.dumps(value, default=str))


def read_back(cfg: Config, store: Store) -> Counter[str]:
    stats: Counter[str] = Counter()
    drafts = cfg.paths.drafts_dir
    if not drafts.is_dir():
        return stats
    rows = store.node_rows()
    known = store.annotations()
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    for path in sorted(drafts.rglob("*.md")):
        # A draft whose mtime and size match what was last written or read holds no new edits.
        expected = rows.get(str(path.relative_to(drafts).with_suffix("")))
        try:
            sig = file_sig(path)
            if expected is not None and expected["file_sig"] == sig:
                stats["untouched"] += 1
                continue
            doc = parse_document(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if doc is None:
            continue
        fm, body = doc
        nid = fm.get("id")
        row = rows.get(nid) if isinstance(nid, str) else None
        if fm.get("schema") != SCHEMA or row is None:
            continue
        stats["read"] += 1
        store.set_file_sig(nid, sig)
        machine = json.loads(row["machine"])
        tags = [str(t) for t in fm.get("tags") or []]
        status = fm.get("status")
        data = {
            "status": None if status in _MACHINE_STATUS else str(status),
            "tags_add": [t for t in tags if t not in machine.get("tags", [])],
            "tags_remove": [t for t in machine.get("tags", []) if t not in tags],
            "summary": fm.get("summary") if fm.get("summary") != machine.get("summary") else None,
            "aliases": [str(a) for a in fm.get("aliases") or []],
            "relations": _plain(curated(fm.get("relations") or [])),
            "extra": _plain(
                {k: v for k, v in fm.items() if k not in BASE_KEYS and k not in machine.get("meta_keys", [])}
            ),
            "notes": split_notes(body)[1],
        }
        data = {k: v for k, v in data.items() if v}
        if data:
            if known.get(nid) != data:
                stats["changed"] += 1
            store.put_annotation(nid, data, stamp)
            stats["annotated"] += 1
        elif nid in known:
            store.drop_annotation(nid)
            stats["cleared"] += 1
    store.db.commit()
    return stats
