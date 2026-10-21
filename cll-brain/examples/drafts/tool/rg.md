---
id: tool/rg
type: tool
title: rg
summary: '`rg`: 16 interactive use(s) across 11 session(s); called by 1 script(s).'
status: draft
tags:
- interactive
- scripted
- search
aliases: []
date: '2026-10-05T15:10:52-04:00'
created: '2026-10-06T17:29:11-04:00'
updated: '2026-10-06T17:29:21-04:00'
rev: 2
schema: cll/1
generator: cll 0.1.0
source:
  collector: derived
  from:
  - history
  - scripts
command: rg
interactive_uses: 16
sessions: 11
script_refs: 1
first_seen: '2026-09-25T09:00:00-04:00'
last_seen: '2026-10-05T15:10:52-04:00'
relations:
- type: pairs_with
  target: tool/uv
  provenance: inferred
  why: together in 10 of 11 sessions (NPMI 0.78)
  weight: 0.782
- type: pairs_with
  target: tool/wc
  provenance: inferred
  why: together in 8 of 8 sessions (NPMI 0.71)
  weight: 0.71
- type: pairs_with
  target: tool/ruff
  provenance: inferred
  why: together in 8 of 9 sessions (NPMI 0.60)
  weight: 0.603
- type: pairs_with
  target: tool/sqlite3
  provenance: inferred
  why: together in 6 of 7 sessions (NPMI 0.45)
  weight: 0.452
- type: alternative_to
  target: tool/find
  provenance: curated
  why: rg and fd interactively; find stays in scripts for portability
---

# rg

> `rg`: 16 interactive use(s) across 11 session(s); called by 1 script(s).

## Recent invocations

```text
rg TODO src | wc -l
rg -n 'def scan' src
rg -l 'toctree' docs
```

<!-- cll:relations -->
## Relations

### pairs_with

- [[tool/uv|uv]] — together in 10 of 11 sessions (NPMI 0.78) · _inferred_
- [[tool/wc|wc]] — together in 8 of 8 sessions (NPMI 0.71) · _inferred_
- [[tool/ruff|ruff]] — together in 8 of 9 sessions (NPMI 0.60) · _inferred_
- [[tool/sqlite3|sqlite3]] — together in 6 of 7 sessions (NPMI 0.45) · _inferred_

### alternative_to

- [[tool/find|find]] — rg and fd interactively; find stays in scripts for portability · _curated_

## Referenced by

### calls ← 2

- [[fn/dotfiles--zsh--functions-zsh--hgrep|hgrep()]] — 1× at L11 · _extracted_
- [[script/dotfiles--zsh--functions-zsh|dotfiles/zsh/functions.zsh]] — 1× at L11 · _extracted_

### uses ← 11

- [[session/2026-10-05-1500|2026-10-05 15:00 · 10 commands]] — 2 invocation(s) · _extracted_
- [[session/2026-10-03-0900|2026-10-03 09:00 · 12 commands]] — 1 invocation(s) · _extracted_
- [[session/2026-10-02-1500|2026-10-02 15:00 · 12 commands]] — 1 invocation(s) · _extracted_
- [[session/2026-10-01-1500|2026-10-01 15:00 · 11 commands]] — 2 invocation(s) · _extracted_
- [[session/2026-09-30-1500|2026-09-30 15:00 · 9 commands]] — 1 invocation(s) · _extracted_
- [[session/2026-09-29-1500|2026-09-29 15:00 · 9 commands]] — 1 invocation(s) · _extracted_
- [[session/2026-09-29-0900|2026-09-29 09:00 · 9 commands]] — 2 invocation(s) · _extracted_
- [[session/2026-09-27-1500|2026-09-27 15:00 · 9 commands]] — 1 invocation(s) · _extracted_
- [[session/2026-09-26-0900|2026-09-26 09:00 · 12 commands]] — 2 invocation(s) · _extracted_
- [[session/2026-09-25-1500|2026-09-25 15:00 · 12 commands]] — 1 invocation(s) · _extracted_
- [[session/2026-09-25-0900|2026-09-25 09:00 · 12 commands]] — 2 invocation(s) · _extracted_

### pairs_with ← 4

- [[tool/uv|uv]] — together in 10 of 11 sessions (NPMI 0.78) · _inferred_
- [[tool/sqlite3|sqlite3]] — together in 6 of 7 sessions (NPMI 0.45) · _inferred_
- [[tool/ruff|ruff]] — together in 8 of 9 sessions (NPMI 0.60) · _inferred_
- [[tool/wc|wc]] — together in 8 of 8 sessions (NPMI 0.71) · _inferred_
<!-- /cll:relations -->

<!-- cll:notes: everything below this line is yours; it is preserved across runs -->
