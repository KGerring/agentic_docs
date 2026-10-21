# cll-brain

A local, background collector that turns shell history, shell scripts, git activity and log files
into a graph of markdown nodes: one file per node, YAML frontmatter for metadata, typed links
between nodes. Human edits to those files are read back and change later runs, which is what
closes the loop.

```
        ┌──────────────────────── read back (annotations) ◄──────────────────────┐
        ▼                                                                        │
  sources ──► scrub ──► event store ──► build graph ──► drafts/*.md ──► you / your ingest
  history      redact     SQLite         nodes + typed      frontmatter      edit status, tags,
  scripts                 (scrubbed      edges with         + wikilinks      aliases, relations,
  git, logs               text only)     provenance                          notes
                                              │
                                              └──► graph.json, site/index.html
```

Nothing leaves the machine. Raw history, commit subjects and log lines are scrubbed before they are
stored; the store and every draft hold only the scrubbed text.

## Install

Python ≥ 3.12. Runtime dependencies: `pyyaml`, `markdown-it-py`. The `ts` extra adds
`tree-sitter` + `tree-sitter-bash` for exact shell parsing.

```sh
uv sync --extra ts          # creates .venv, installs cll
uv run cll init             # writes ./cll.toml, pre-filled with paths that exist on this machine
uv run cll doctor           # what a run would read: paths and counts, never content
uv run cll run              # one cycle
uv run cll site             # site/index.html
uv run cll schedule install # background runs every 15 min (launchd on macOS, systemd on Linux)
```

`pixi install && pixi run collect` works from the same `pyproject.toml` (`[tool.pixi.*]` tables);
uv ignores those tables.

## Commands

| Command | Effect |
| --- | --- |
| `cll init [PATH] [--force]` | Write a starter `cll.toml`. |
| `cll doctor` | List history files, repo and script counts, parser in use. Counts only. |
| `cll run [--dry-run] [--no-collect] [--site] [--quiet] [--json]` | Read back → collect → build → write drafts. `--no-collect` re-renders from the store. Overlapping runs exit via a lock file. |
| `cll site [--from DIR] [--out DIR] [--fragment]` | Build the static site from the store, or from any directory of cll-schema markdown. |
| `cll schedule show\|install\|uninstall\|status [--interval S]` | Manage the background job. `show` prints the unit without installing. |
| `cll status` | Row counts, last run, scheduler state. |
| `cll forget history\|git\|logs\|scripts\|all` | Delete collected events (drafts untouched). Use after tightening `[redact]`. |

Config lookup: `--config` → `$CLL_CONFIG` → `./cll.toml` → `~/.config/cll/cll.toml`. Relative
paths resolve against the config file's directory. An unknown key inside a section is an error.

## Node types

Node id = path under `drafts_dir` without `.md`, e.g. `tool/git` → `drafts/tool/git.md`.

| Type | One node per | Id pattern | Source |
| --- | --- | --- | --- |
| `tool` | executable seen ≥ `min_tool_uses` times interactively, or called by any script | `tool/<name>` | derived |
| `script` | shell script under `scripts.roots` (by extension or shebang) | `script/<root>--<path>` | tree-sitter / heuristic |
| `fn` | shell function | `fn/<script>--<name>` | tree-sitter / heuristic |
| `session` | burst of history separated by > `session_gap_minutes` | `session/YYYY-MM-DD-HHMM` | history |
| `day` | calendar day with any activity | `day/YYYY-MM-DD` | derived |
| `repo` | git repository under `git.roots` | `repo/<dirname>` | `git log` |
| `log` | configured log family | `log/<name>` | logs |
| `logday` | log family × day | `logday/<name>--YYYY-MM-DD` | logs |

A session is emitted once it has been idle for a full gap. The session in progress contributes
nothing (no node, no tool statistics), so drafts change when a session closes, not every run.

## Relations

Every edge has a `type`, a `target`, a `provenance` and usually a `why` (the evidence).

| Type | From → to | Provenance | Evidence in `why` |
| --- | --- | --- | --- |
| `defines` / `defined_in` | script ↔ fn | extracted | line range |
| `sources` | script → script | extracted (literal path) / inferred (dynamic path or filename match) | line, how it was resolved |
| `calls` | script, fn → fn, tool | extracted | count and line numbers |
| `invokes` | script, fn → script | extracted / inferred | count and line numbers |
| `part_of` | script → repo | extracted | path containment |
| `uses` | session → tool | extracted | invocation count |
| `runs` | session → script | extracted (path) / inferred (filename) | run count |
| `touches` | session → repo; day → repo | extracted (`cd` to an absolute path, commits) / inferred (repo name, commits inside the session window) | which signal |
| `on_day` | session, logday → day | extracted | |
| `follows` | session → previous session | extracted | gap |
| `from_source` | logday → log | extracted | |
| `pairs_with` | tool → tool | inferred | co-occurrence count and NPMI |
| anything else | any → any | curated | whatever you wrote |

Provenance values:

- `extracted`: parsed from a source. A fact.
- `inferred`: statistical or name-based. A hypothesis; `why` carries the evidence.
- `curated`: written by you in a draft's frontmatter. Never regenerated, never dropped, even when
  the target does not exist yet.

`pairs_with` uses normalised PMI over timed sessions (support ≥ 3, NPMI ≥ 0.25, top 5 per tool),
and skips tools present in more than 60 % of sessions.

## Draft format

```markdown
---
id: tool/git                    # machine
type: tool                      # machine
title: git                      # machine
summary: '`git`: 68 interactive use(s) across 18 session(s); called by 1 script(s).'   # yours to rewrite
status: draft                   # yours: draft | keep | promote | ignore   (machine sets: stale)
tags: [interactive, scripted, vcs]      # yours to add to or remove from
aliases: []                     # yours
date: '2026-10-05T16:12:09-04:00'       # the moment the node is about
created: '2026-10-06T17:18:35-04:00'
updated: '2026-10-06T17:19:31-04:00'    # changes only when content changes
rev: 3                          # increments with every rewrite; upsert on (id, rev)
schema: cll/1
generator: cll 0.1.0
source: {collector: derived, from: [history, scripts]}
command: git                    # type-specific keys follow
interactive_uses: 68
subcommands: {push: 14, diff: 12}
relations:
- {type: pairs_with, target: tool/rg, provenance: inferred, why: together in 9 of 14 sessions (NPMI 0.41), weight: 0.41}
- {type: alternative_to, target: tool/jj, why: evaluating}     # yours: no provenance, or `curated`
---

# git

> summary

…machine body…

<!-- cll:relations -->
## Relations
### pairs_with
- [[tool/rg|rg]] — together in 9 of 14 sessions (NPMI 0.41) · _inferred_
## Referenced by
### uses ← 18
- [[session/2026-10-05-1600|2026-10-05 16:00 · 10 commands]] — 2 invocation(s) · _extracted_
<!-- /cll:relations -->

<!-- cll:notes: everything below this line is yours; it is preserved across runs -->
```

Frontmatter is emitted in block style; the flow style above is only for brevity.

Three regions, three owners:

1. **Frontmatter.** Machine keys are regenerated. The keys marked *yours* above, plus any key the
   machine does not use, are preserved.
2. **Machine body**, down to the notes marker. Regenerated; edits here are overwritten at the next
   change. The `cll:relations` block is the frontmatter `relations` plus backlinks rendered as
   links, for renderers without a backlink feature. Strip it downstream if you do not want it.
3. **Notes**, below the marker. Never touched.

Write rules:

- A file is rewritten only when its content hash changes (`updated`, `rev` bump with it).
- A draft that was moved or deleted is treated as consumed. It is re-emitted, with a higher `rev`,
  only when the node changes (`render.on_consumed = "rewrite"` re-emits it regardless).
- A node the graph no longer produces has its draft flagged `status: stale` once. Nothing is
  deleted.
- Writes are atomic (temp file + rename).

`render.link_style = "markdown"` emits `[git](../tool/git.md)` instead of wikilinks.
`render.h1 = false` drops the title heading for renderers that add their own.

## The loop

At the start of each run, drafts whose mtime or size changed since they were written are parsed
and diffed against what the machine last wrote. The difference is stored as an annotation and
applied to every later build, including after your ingest has consumed the file.

| Edit | Effect on later runs |
| --- | --- |
| `status: ignore` | Node and every edge to it leave the graph. The file stays as a tombstone; set it back to `draft` to undo. |
| `status: keep` / `promote` / any other value | Carried through unchanged, for your ingest to act on. |
| Add or remove `tags` | Kept. A removed machine tag stays removed. |
| Rewrite `summary` | Your wording replaces the generated one. |
| `aliases: [g, gst]` on `tool/git` | Those command names are attributed to `tool/git`, retroactively. `tool/g` goes stale. |
| Add a `relations` entry (no provenance, or `curated`) | Kept as a curated edge. |
| Add any other key | Preserved verbatim. |
| Text below the notes marker | Preserved verbatim. |

The graph is re-derived from the event store on every run, so an annotation applies to history
collected before it was made.

## Redaction

Applied before storage, to history lines, commit subjects, remote URLs, log lines, script
descriptions and embedded function source.

- Known credential shapes: GitHub, `sk-…`, AWS key ids, Slack, Google API keys, JWTs, npm and PyPI
  tokens, PEM private keys.
- `NAME=value` and `name: value` where the name contains `PASSWORD`, `SECRET`, `TOKEN`, `API_KEY`,
  `ACCESS_KEY`, `PRIVATE_KEY`, `CREDENTIAL`, `AUTH`, `SESSION_ID` or `COOKIE`. Variable references
  (`TOKEN="$1"`) are kept.
- `--password X`, `--token=X` and similar flags; `Authorization:` headers; `user:pass@host` URLs;
  `curl -u user:pass`.
- Mixed-case alphanumeric strings of ≥ 32 characters with Shannon entropy ≥ 4 bits/char.
  Lower-case hex (commit SHAs, checksums) is deliberately not matched.
- Arguments of secret-manager CLIs (`op`, `pass`, `bw`, `security`, `gpg`, `vault`, `age`, `sops`,
  …; see `history.sensitive_tools`) are dropped wholesale.
- `redact.extra_patterns`: your own regexes.

Limits: this is pattern matching. A secret passed as a bare positional argument with no telling
shape (`mytool hunter2`) is not detected. Review a few session drafts before pointing another
process at the drafts directory, add patterns, then `cll forget history && cll run`.

History entries are deduplicated by a salted BLAKE2b hash of the raw line; the salt stays in the
state directory.

## Shell-script graph

`scripts.parser = "auto"` uses tree-sitter-bash when installed and the built-in scanner otherwise.
Both produce the same facts: functions with line ranges and doc comments, every command call with
line numbers (heredoc text excluded), `source`/`.` targets, `set` options, variables read but never
assigned (probable environment inputs) and exports.

Resolution of a command word inside a script, first match wins:

1. a function defined in the same file;
2. a function defined in a file reachable through `source` (transitively);
3. a known script, by literal path (extracted), by `$VAR/rest` against the script's own directory
   (inferred) or by unique filename (inferred);
4. shell builtins and keywords: dropped;
5. otherwise a `tool` node.

`scripts.embed_source`: `functions` (default) embeds each function's scrubbed source in its `fn`
node; `all` also embeds the whole script (first 400 lines); `none` embeds nothing.

The heuristic scanner is a tokenizer, not a grammar: it misses commands inside heredoc command
substitutions and can misread unusual quoting. zsh scripts go through the bash grammar; files with
syntax the grammar rejects are tagged `parse-errors` and still yield whatever parsed.

## Static site

`cll site` writes one self-contained `index.html`: no framework, no build step, plain JavaScript,
data embedded as JSON. It has card and graph views, filters for type, tag, status and edge
provenance, full-text search, and a page per node with typed relations, backlinks and properties.
Routes are URL fragments (`#tool~git`), so the file works from `file://` and from any static host
without rewrite rules.

Everything visible is driven by `[site]` in `cll.toml` and by the drafts' frontmatter; there is no
front-end code to maintain. To change the look, edit the CSS custom properties at the top of
`src/cll/site_template.html`.

- Default input is the event store, so the site is complete even when drafts have been consumed.
- `cll site --from DIR` builds from any directory of markdown with this frontmatter schema, e.g.
  the vault your ingest writes to. Files without an `id` get one from their path.
- `site.google_fonts = false` removes the only external request (the font stylesheet).
- The graph draws at most `site.graph_node_cap` nodes (most linked first); filters narrow it.

Deploy by uploading `site/` to any static host. The drafts directory also opens directly as an
Obsidian vault (the wikilinks are vault-relative paths).

## Scheduling

`cll schedule install` writes `~/Library/LaunchAgents/dev.cll.collector.plist` and bootstraps it
(`StartInterval`, `RunAtLoad`, background priority). The job runs
`<this venv's python> -m cll --config <your cll.toml> run --quiet`, so reinstall it if the project
moves. Log: `<state_dir>/collector.log`. On Linux a systemd user timer is installed instead.

macOS notes:

- A launchd agent cannot read `~/Documents`, `~/Desktop` or `~/Downloads` until the interpreter is
  granted access under System Settings → Privacy & Security. Paths elsewhere in `~` need nothing.
- zsh writes history when the shell exits unless `INC_APPEND_HISTORY` or `SHARE_HISTORY` is set,
  so commands can appear late. With either option set they appear as they run.
- Plain (non-`EXTENDED_HISTORY`) zsh history and bash history without `HISTTIMEFORMAT` carry no
  timestamps. Those commands still count toward tool statistics but land in one `untimed` session
  per ingest.

## Outputs

| Path | Content |
| --- | --- |
| `drafts_dir/<type>/<slug>.md` | One node per file. |
| `drafts_dir/_index.md` | Map of content (`render.write_index`). |
| `graph_file` | Node-link JSON; with NetworkX ≥ 3.4, `nx.node_link_graph(json.load(f), edges="links")`. Edge attributes: `type`, `provenance`, `why`, `weight`. |
| `site_dir/index.html` | The static site. |
| `state_dir/cll.sqlite3` | Event store, cursors, render bookkeeping, annotations. |

## Development

```sh
uv sync --extra ts
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run pre-commit install
```

`.pre-commit-config.yaml` runs ruff, the tests, and a guard that refuses to commit `drafts/`,
`site/`, `state/`, `graph.json`, `cll.toml` or any `.sqlite3` file.

Layout:

```
src/cll/
  cli.py            argparse entry point
  config.py         cll.toml → dataclasses
  redact.py         secret scrubbing
  shparse.py        shell scanner (history lines, fallback script parser)
  collectors/       history.py · gitlog.py · logs.py · scripts.py   (sources → store)
  state.py          SQLite schema
  build.py          store + annotations → nodes and edges
  render.py         nodes → markdown, graph.json, _index.md
  feedback.py       drafts → annotations
  site.py           nodes → index.html      site_template.html
  schedule.py       launchd / systemd units
  pipeline.py       one cycle
```

`examples/drafts/` holds a few drafts generated from a synthetic home directory, including two
with human annotations (`tool/git`: alias, tag, rewritten summary, note; `tool/rg`: curated
relation).
