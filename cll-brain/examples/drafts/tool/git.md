---
id: tool/git
type: tool
title: git
summary: Version control for everything; `g` is my shell alias for it.
status: draft
tags:
- interactive
- scripted
- vcs
- daily-driver
aliases:
- g
date: '2026-10-05T15:19:11-04:00'
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
command: git
interactive_uses: 65
sessions: 23
script_refs: 2
first_seen: '2026-09-25T09:03:02-04:00'
last_seen: '2026-10-05T15:19:11-04:00'
subcommands:
  status: 17
  log: 12
  commit: 12
  diff: 7
  add: 6
  push: 4
  co: 3
  st: 3
  clone: 1
folded_spellings:
- g
relations: []
---

# git

> Version control for everything; `g` is my shell alias for it.

## Subcommands

- `git status` × 17
- `git log` × 12
- `git commit` × 12
- `git diff` × 7
- `git add` × 6
- `git push` × 4
- `git co` × 3
- `git st` × 3
- `git clone` × 1

## Recent invocations

```text
g st
g co main
git add -A && git commit -m 'scanner: handle decorators'
git clone https://kristen:[REDACTED:password]@github.com/x/y.git
git status
git log --oneline | head -20
```

<!-- cll:relations -->
## Referenced by

### calls ← 3

- [[fn/bash-scripts--sync-notes-sh--pull|pull()]] — 1× at L8 · _extracted_
- [[script/bash-scripts--sync-notes-sh|bash_scripts/sync-notes.sh]] — 1× at L8 · _extracted_
- [[script/bin--deploy|bin/deploy]] — 1× at L9 · _extracted_

### uses ← 23

- [[session/2026-10-05-1500|2026-10-05 15:00 · 10 commands]] — 4 invocation(s) · _extracted_
- [[session/2026-10-05-0900|2026-10-05 09:00 · 12 commands]] — 1 invocation(s) · _extracted_
- [[session/2026-10-04-1655|2026-10-04 16:55 · 4 commands]] — 1 invocation(s) · _extracted_
- [[session/2026-10-04-1500|2026-10-04 15:00 · 11 commands]] — 4 invocation(s) · _extracted_
- [[session/2026-10-04-0900|2026-10-04 09:00 · 11 commands]] — 2 invocation(s) · _extracted_
- [[session/2026-10-03-1500|2026-10-03 15:00 · 9 commands]] — 4 invocation(s) · _extracted_
- [[session/2026-10-03-0900|2026-10-03 09:00 · 12 commands]] — 5 invocation(s) · _extracted_
- [[session/2026-10-02-1500|2026-10-02 15:00 · 12 commands]] — 5 invocation(s) · _extracted_
- [[session/2026-10-02-0900|2026-10-02 09:00 · 9 commands]] — 1 invocation(s) · _extracted_
- [[session/2026-10-01-1500|2026-10-01 15:00 · 11 commands]] — 4 invocation(s) · _extracted_
- [[session/2026-10-01-0900|2026-10-01 09:00 · 9 commands]] — 3 invocation(s) · _extracted_
- [[session/2026-09-30-1500|2026-09-30 15:00 · 9 commands]] — 1 invocation(s) · _extracted_
- [[session/2026-09-30-0900|2026-09-30 09:00 · 9 commands]] — 4 invocation(s) · _extracted_
- [[session/2026-09-29-1500|2026-09-29 15:00 · 9 commands]] — 3 invocation(s) · _extracted_
- [[session/2026-09-29-0900|2026-09-29 09:00 · 9 commands]] — 3 invocation(s) · _extracted_
- …and 8 more
<!-- /cll:relations -->

<!-- cll:notes: everything below this line is yours; it is preserved across runs -->

Try `git worktree` for the parallel scanner branches. Related: [[tool/pre-commit]].
