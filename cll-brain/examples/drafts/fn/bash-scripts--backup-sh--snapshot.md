---
id: fn/bash-scripts--backup-sh--snapshot
type: fn
title: snapshot()
summary: Function in bash_scripts/backup.sh, 5 line(s).
status: draft
tags:
- bash
- bash_scripts
aliases: []
date: '2026-10-06T17:18:35-04:00'
created: '2026-10-06T17:29:11-04:00'
updated: '2026-10-06T17:29:11-04:00'
rev: 1
schema: cll/1
generator: cll 0.1.0
source:
  collector: scripts
  path: ~/bash_scripts/backup.sh
  lines: L12-L16
  parser: tree-sitter
name: snapshot
script: bash_scripts/backup.sh
loc: 5
relations:
- type: defined_in
  target: script/bash-scripts--backup-sh
  provenance: extracted
  why: L12–16
- type: calls
  target: fn/bash-scripts--lib--common-sh--log
  provenance: extracted
  why: 1× at L14; defined in sourced bash_scripts/lib/common.sh
- type: calls
  target: fn/bash-scripts--lib--common-sh--require
  provenance: extracted
  why: 1× at L13; defined in sourced bash_scripts/lib/common.sh
- type: calls
  target: tool/rsync
  provenance: extracted
  why: 1× at L15
- type: calls
  target: tool/tee
  provenance: extracted
  why: 1× at L15
---

# snapshot()

> Function in bash_scripts/backup.sh, 5 line(s).

## Source

```bash
snapshot() {
  require rsync
  log "syncing $VAULT_DIR"
  rsync -a --delete "$VAULT_DIR/" "$BACKUP_ROOT/vault/" | tee -a "$LOG_FILE"
}
```

<!-- cll:relations -->
## Relations

### defined_in

- [[script/bash-scripts--backup-sh|bash_scripts/backup.sh]] — L12–16 · _extracted_

### calls

- [[fn/bash-scripts--lib--common-sh--log|log()]] — 1× at L14; defined in sourced bash_scripts/lib/common.sh · _extracted_
- [[fn/bash-scripts--lib--common-sh--require|require()]] — 1× at L13; defined in sourced bash_scripts/lib/common.sh · _extracted_
- [[tool/rsync|rsync]] — 1× at L15 · _extracted_
- [[tool/tee|tee]] — 1× at L15 · _extracted_

## Referenced by

### calls ← 1

- [[script/bash-scripts--backup-sh|bash_scripts/backup.sh]] — 1× at L27 · _extracted_
<!-- /cll:relations -->

<!-- cll:notes: everything below this line is yours; it is preserved across runs -->
