---
id: script/bash-scripts--backup-sh
type: script
title: bash_scripts/backup.sh
summary: Nightly backup of the notes vault to the NAS.
status: keep
tags:
- bash
- bash_scripts
- has-functions
- strict-mode
aliases: []
date: '2026-10-06T17:18:35-04:00'
created: '2026-10-06T17:29:11-04:00'
updated: '2026-10-06T17:29:21-04:00'
rev: 2
schema: cll/1
generator: cll 0.1.0
source:
  collector: scripts
  path: ~/bash_scripts/backup.sh
  sha256: fecd72fe656a8cb8
  modified: '2026-10-06T17:18:35-04:00'
  parser: tree-sitter
dialect: bash
shebang: '#!/usr/bin/env bash'
loc: 33
functions: 2
set_flags:
- -e
- -u
- pipefail
env_reads:
- LOG_FILE
- VAULT_DIR
exports:
- BACKUP_ROOT
parse_errors: false
relations:
- type: defines
  target: fn/bash-scripts--backup-sh--prune-old
  provenance: extracted
  why: L18–20
- type: defines
  target: fn/bash-scripts--backup-sh--snapshot
  provenance: extracted
  why: L12–16
- type: sources
  target: script/bash-scripts--lib--common-sh
  provenance: inferred
  why: L7; `$DIR/lib/common.sh` resolved against the script's own directory
- type: invokes
  target: script/bash-scripts--notify-sh
  provenance: inferred
  why: 1× at L33; `$DIR/notify.sh` resolved against the script's own directory
- type: calls
  target: fn/bash-scripts--lib--common-sh--log
  provenance: extracted
  why: 2× at L14, L23; defined in sourced bash_scripts/lib/common.sh
  weight: 2
- type: calls
  target: fn/bash-scripts--backup-sh--prune-old
  provenance: extracted
  why: 1× at L28
- type: calls
  target: fn/bash-scripts--backup-sh--snapshot
  provenance: extracted
  why: 1× at L27
- type: calls
  target: fn/bash-scripts--lib--common-sh--require
  provenance: extracted
  why: 1× at L13; defined in sourced bash_scripts/lib/common.sh
- type: calls
  target: tool/cat
  provenance: extracted
  why: 1× at L29
- type: calls
  target: tool/date
  provenance: extracted
  why: 1× at L30
- type: calls
  target: tool/dirname
  provenance: extracted
  why: 1× at L6
- type: calls
  target: tool/find
  provenance: extracted
  why: 1× at L19
- type: calls
  target: tool/grep
  provenance: extracted
  why: 1× at L24
- type: calls
  target: tool/rsync
  provenance: extracted
  why: 1× at L15
- type: calls
  target: tool/tee
  provenance: extracted
  why: 1× at L15
- type: calls
  target: tool/xargs
  provenance: extracted
  why: 1× at L19
- type: depends_on
  target: script/bash-scripts--rotate-logs-sh
  provenance: curated
  why: run log rotation first so archives are small
---

# bash_scripts/backup.sh

> Nightly backup of the notes vault to the NAS.

Nightly backup of the notes vault to the NAS.  
Usage: backup.sh [--dry-run]

## Environment

- Reads (never assigned here): `LOG_FILE`, `VAULT_DIR`
- Exports: `BACKUP_ROOT`

<!-- cll:relations -->
## Relations

### defines

- [[fn/bash-scripts--backup-sh--prune-old|prune_old()]] — L18–20 · _extracted_
- [[fn/bash-scripts--backup-sh--snapshot|snapshot()]] — L12–16 · _extracted_

### sources

- [[script/bash-scripts--lib--common-sh|bash_scripts/lib/common.sh]] — L7; `$DIR/lib/common.sh` resolved against the script's own directory · _inferred_

### invokes

- [[script/bash-scripts--notify-sh|bash_scripts/notify.sh]] — 1× at L33; `$DIR/notify.sh` resolved against the script's own directory · _inferred_

### calls

- [[fn/bash-scripts--lib--common-sh--log|log()]] — 2× at L14, L23; defined in sourced bash_scripts/lib/common.sh · _extracted_
- [[fn/bash-scripts--backup-sh--prune-old|prune_old()]] — 1× at L28 · _extracted_
- [[fn/bash-scripts--backup-sh--snapshot|snapshot()]] — 1× at L27 · _extracted_
- [[fn/bash-scripts--lib--common-sh--require|require()]] — 1× at L13; defined in sourced bash_scripts/lib/common.sh · _extracted_
- [[tool/cat|cat]] — 1× at L29 · _extracted_
- [[tool/date|date]] — 1× at L30 · _extracted_
- [[tool/dirname|dirname]] — 1× at L6 · _extracted_
- [[tool/find|find]] — 1× at L19 · _extracted_
- [[tool/grep|grep]] — 1× at L24 · _extracted_
- [[tool/rsync|rsync]] — 1× at L15 · _extracted_
- [[tool/tee|tee]] — 1× at L15 · _extracted_
- [[tool/xargs|xargs]] — 1× at L19 · _extracted_

### depends_on

- [[script/bash-scripts--rotate-logs-sh|bash_scripts/rotate-logs.sh]] — run log rotation first so archives are small · _curated_

## Referenced by

### invokes ← 1

- [[script/bash-scripts--sync-notes-sh|bash_scripts/sync-notes.sh]] — 1× at L13; `$DIR/backup.sh` resolved against the script's own directory · _inferred_

### runs ← 3

- [[session/2026-10-04-0900|2026-10-04 09:00 · 11 commands]] — 2 run(s) · _extracted_
- [[session/2026-10-01-0900|2026-10-01 09:00 · 9 commands]] — 1 run(s) · _extracted_
- [[session/2026-09-28-0900|2026-09-28 09:00 · 9 commands]] — 1 run(s) · _extracted_
<!-- /cll:relations -->

<!-- cll:notes: everything below this line is yours; it is preserved across runs -->
