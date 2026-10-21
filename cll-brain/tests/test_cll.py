"""End-to-end and unit tests. Everything runs against a synthetic home under ``tmp_path``."""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from cll import config as config_mod
from cll import pipeline, schedule, shparse, site
from cll.collectors import history, logs, scripts
from cll.redact import Redactor
from cll.render import parse_document

DAY = 86400
NOW = int(time.time())


# -- units ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("export OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456", "sk-abcdef"),
        ("curl -u kristen:hunter2 https://api.example.com", "hunter2"),
        ("git clone https://kristen:s3cr3tpw@github.com/x/y.git", "s3cr3tpw"),
        ("mysql --password=topsecret -u root", "topsecret"),
        ("curl -H 'Authorization: Bearer abcdef0123456789abcdef' https://x", "abcdef0123456789abcdef"),
        ("echo ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abc"),
        ("DB_PASSWORD='p@ss word' ./run.sh", "p@ss word"),
        ("aws configure set aws_secret_access_key wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY12", "wJalrXUtnFEMI"),
    ],
)
def test_redaction_removes_secret(raw: str, secret: str) -> None:
    assert secret not in Redactor().scrub_command(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "git log --author=kristen --oneline",
        "git checkout 3f2a9c1b0d4e5f60718293a4b5c6d7e8f9012345",
        "ls /usr/local/lib/python3.13/site-packages/markdown_it/rules_inline",
        'TOKEN="$1" ./deploy',
    ],
)
def test_redaction_leaves_ordinary_commands_alone(raw: str) -> None:
    assert Redactor().scrub_command(raw) == raw


def test_sensitive_tool_arguments_are_dropped() -> None:
    out = Redactor(sensitive_tools=("op",)).scrub_command("op read op://vault/item/password")
    assert out == "op read [REDACTED:args]"


def _heads(text: str) -> list[str]:
    return [h[0] for _, w in shparse.split_commands(text) if (h := shparse.command_head(w))]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "git status | grep -v foo && sudo -n rm -rf x ; FOO=1 uv run pytest 2>&1 >/dev/null",
            ["git", "grep", "rm", "uv"],
        ),
        ('for f in *.sh; do bash "$f"; done', ["bash"]),
        ("if [[ -f x ]]; then echo hi; fi", ["echo"]),
        ("case $1 in start) a ;; stop|halt) b ;; *) c ;; esac", ["a", "b", "c"]),
        ('echo "a # not a comment" # real comment; rm x', ["echo"]),
        ("diff <(sort a) <(sort b)", ["diff", "sort", "sort"]),
        ('x=$(dirname "$(which python)")', ["dirname", "which"]),
        ("foo() { cat <<<'x' | jq . ; }", ["cat", "jq"]),
    ],
)
def test_shell_scanner(text: str, expected: list[str]) -> None:
    assert _heads(text) == expected


def test_history_parsers() -> None:
    zsh = ": 1700000000:0;git status\n: 1700000060:5;echo a\\\nb\nplain line\n"
    assert list(history.parse_zsh(zsh)) == [(1700000000, "git status"), (1700000060, "echo a\nb"), (None, "plain line")]
    bash = "#1700000000\nls -la\nmake\n"
    assert list(history.parse_bash(bash)) == [(1700000000, "ls -la"), (None, "make")]
    fish = "- cmd: git status\n  when: 1700000000\n- cmd: echo a\\nb\n  when: 1700000100\n"
    assert list(history.parse_fish(fish)) == [(1700000000, "git status"), (1700000100, "echo a\nb")]
    assert history.unmetafy(b"caf\x83\xe3\x83\x89") == "café".encode()


def test_log_signature_collapses_variable_parts() -> None:
    a = logs.signature(
        "2026-10-05 09:00:10 ERROR db 10.0.0.1:5432 failed after 3 retries (id=3f2a9c1b-1111-2222-3333-444455556660)"
    )
    b = logs.signature(
        "2026-10-06 11:42:01 ERROR db 10.0.0.9:5432 failed after 7 retries (id=aaaaaaaa-1111-2222-3333-444455556661)"
    )
    assert a == b
    assert logs.classify("WARNING slow query") == "warn"
    assert logs.classify("Traceback (most recent call last):") == "error"


SCRIPT = """#!/usr/bin/env bash
# Nightly backup.
set -euo pipefail
DIR="$(dirname "$0")"
source "$DIR/lib/common.sh"
export BACKUP_ROOT="${BACKUP_ROOT:-/tmp}"

snapshot() {
  require rsync
  rsync -a "$VAULT_DIR/" "$BACKUP_ROOT/" | tee -a "$LOG_FILE"
}

case "${1:-}" in
  --dry-run) log "dry"; exit 0 ;;
esac
snapshot
cat <<MSG
rm -rf this-is-heredoc-text
MSG
"$DIR/notify.sh" done
"""


@pytest.mark.parametrize("parser", ["heuristic", "tree-sitter"])
def test_script_parsers_agree_on_structure(parser: str) -> None:
    if parser == "tree-sitter":
        ts = scripts._ts_parser()
        if ts is None:
            pytest.skip("tree-sitter extra not installed")
        facts = scripts.parse_tree_sitter(ts, SCRIPT)
    else:
        facts = scripts.parse_heuristic(SCRIPT)
    assert [f["name"] for f in facts["functions"]] == ["snapshot"]
    assert (facts["functions"][0]["start"], facts["functions"][0]["end"]) == (8, 11)
    assert set(facts["functions"][0]["calls"]) == {"require", "rsync", "tee"}
    assert {"dirname", "snapshot", "log", "cat", "rsync"} <= set(facts["calls"])
    assert "rm" not in facts["calls"]  # heredoc text is not code
    assert [s["target"].strip('"') for s in facts["sources"]] == ["$DIR/lib/common.sh"]
    assert facts["set_flags"] == ["-e", "-u", "pipefail"]
    assert facts["exports"] == ["BACKUP_ROOT"]
    assert {"VAULT_DIR", "LOG_FILE"} <= set(facts["env_reads"])


# -- end to end ----------------------------------------------------------------


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> config_mod.Config:
    home = tmp_path / "home"
    (home / "bash_scripts" / "lib").mkdir(parents=True)
    (home / "bash_scripts" / "lib" / "common.sh").write_text(
        "#!/usr/bin/env bash\n# Shared helpers.\n\n# Print a log line.\nlog() {\n  printf '%s\\n' \"$*\" >&2\n}\n\n"
        'require() {\n  command -v "$1" >/dev/null || exit 1\n}\n'
    )
    (home / "bash_scripts" / "backup.sh").write_text(SCRIPT)
    (home / "bash_scripts" / "notify.sh").write_text(
        '#!/bin/sh\n# Notify.\ncurl -fsS -d "$1" https://ntfy.example/home\n'
    )

    repo = home / "repos" / "kg"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "reindex.sh").write_text(
        "#!/usr/bin/env bash\n# Rebuild the index.\nuv run python -m kg.index\n"
    )

    def git(*args: str, when: int) -> None:
        env = {
            "GIT_AUTHOR_DATE": f"@{when} +0000",
            "GIT_COMMITTER_DATE": f"@{when} +0000",
            "GIT_AUTHOR_NAME": "K",
            "GIT_AUTHOR_EMAIL": "k@example.com",
            "GIT_COMMITTER_NAME": "K",
            "GIT_COMMITTER_EMAIL": "k@example.com",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        }
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    git("init", "-q", "-b", "main", when=NOW)
    git("add", "-A", when=NOW)
    git("commit", "-q", "-m", "Initial pipeline", when=NOW - 2 * DAY + 600)

    lines: list[str] = []
    base = ["git status", "git diff", "uv run pytest -q", "rg -n TODO src | head", "jq . data.json", "g st"]
    for back in (5, 4, 3, 2):
        start = NOW - back * DAY
        for i, cmd in enumerate(base):
            lines.append(f": {start + i * 60}:0;{cmd}")
    start = NOW - 2 * DAY + 400
    for i, cmd in enumerate(
        [
            f"cd {repo}",
            "export API_TOKEN=sk-abcdefghijklmnopqrstuvwxyz123456",
            f"{home}/bash_scripts/backup.sh --dry-run",
            "less notes.txt",
            "less a",
            "less b",
        ]
    ):
        lines.append(f": {start + i * 60}:0;{cmd}")
    lines.append(f": {NOW - 120}:0;git status")  # the session still in progress
    (home / ".zsh_history").write_text("\n".join(lines) + "\n")

    day = time.strftime("%Y-%m-%d", time.localtime(NOW - DAY))
    (home / "app.log").write_text(
        f"{day} 09:00:01 INFO started\n"
        + "".join(f"{day} 09:0{i}:10 ERROR connect to 10.0.0.{i}:5432 failed token=abc123SECRETxyz\n" for i in range(4))
        + f"{day} 10:00:00 WARNING slow query took 1532 ms\n"
    )

    work = tmp_path / "work"
    work.mkdir()
    (work / "cll.toml").write_text(f'''
[paths]
state_dir = "state"
[history]
files = ["{home}/.zsh_history"]
[git]
roots = ["{home}/repos"]
[scripts]
roots = ["{home}/bash_scripts", "{repo}/scripts"]
parser = "heuristic"
[[logs.source]]
name = "app"
globs = ["{home}/app.log"]
''')
    monkeypatch.setenv("HOME", str(home))
    return config_mod.load(str(work / "cll.toml"))


def _doc(cfg: config_mod.Config, node_id: str) -> tuple[dict, str]:
    doc = parse_document((cfg.paths.drafts_dir / f"{node_id}.md").read_text())
    assert doc is not None
    return doc


def _edges(fm: dict, edge_type: str) -> dict[str, dict]:
    return {e["target"]: e for e in fm["relations"] if e["type"] == edge_type}


def _rewrite(cfg: config_mod.Config, node_id: str, mutate, append: str = "") -> None:
    path = cfg.paths.drafts_dir / f"{node_id}.md"
    fm, body = _doc(cfg, node_id)
    mutate(fm)
    path.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n" + body + append)


def test_cycle_builds_linked_drafts(world: config_mod.Config) -> None:
    stats = pipeline.cycle(world)
    assert stats["drafts"]["created"] == stats["graph"]["nodes"] > 20

    fm, body = _doc(world, "script/bash-scripts--backup-sh")
    assert fm["schema"] == "cll/1" and fm["rev"] == 1 and fm["status"] == "draft"
    assert "script/bash-scripts--lib--common-sh" in _edges(fm, "sources")
    assert _edges(fm, "sources")["script/bash-scripts--lib--common-sh"]["provenance"] == "inferred"
    assert "fn/bash-scripts--backup-sh--snapshot" in _edges(fm, "defines")
    assert "fn/bash-scripts--lib--common-sh--log" in _edges(fm, "calls")  # resolved through `source`
    assert "tool/rsync" in _edges(fm, "calls")
    assert "script/bash-scripts--notify-sh" in _edges(fm, "invokes")
    assert "[[fn/bash-scripts--backup-sh--snapshot|snapshot()]]" in body
    assert "<!-- cll:notes" in body

    fm, _ = _doc(world, "script/scripts--reindex-sh")
    assert "repo/kg" in _edges(fm, "part_of")

    sessions = sorted((world.paths.drafts_dir / "session").glob("*.md"))
    assert len(sessions) == 4  # five closed bursts, two of them inside one 30-minute gap; the open one is held back
    with_repo = [parse_document(p.read_text())[0] for p in sessions]
    touching = [fm for fm in with_repo if _edges(fm, "touches")]
    assert touching and "script/bash-scripts--backup-sh" in _edges(touching[0], "runs")
    assert _edges(touching[0], "runs")["script/bash-scripts--backup-sh"]["provenance"] == "extracted"

    fm, body = _doc(world, "tool/git")
    assert fm["interactive_uses"] == 8 and fm["subcommands"] == {"status": 4, "diff": 4}

    digests = list((world.paths.drafts_dir / "logday").glob("*.md"))
    assert len(digests) == 1 and "error × 4" in digests[0].read_text()

    everything = "\n".join(p.read_text() for p in world.paths.drafts_dir.rglob("*.md"))
    everything += (world.paths.state_dir / "cll.sqlite3").read_bytes().decode("latin-1")
    assert "sk-abcdef" not in everything and "abc123SECRETxyz" not in everything

    graph = json.loads(world.paths.graph_file.read_text())
    ids = {n["id"] for n in graph["nodes"]}
    assert all(link["source"] in ids and link["target"] in ids for link in graph["links"])


def test_second_cycle_writes_nothing(world: config_mod.Config) -> None:
    first = pipeline.cycle(world)
    before = {p: p.stat().st_mtime_ns for p in world.paths.drafts_dir.rglob("*.md")}
    second = pipeline.cycle(world)
    assert second["drafts"] == {"unchanged": first["graph"]["nodes"]}
    assert second["history"]["new"] == 0
    assert before == {p: p.stat().st_mtime_ns for p in world.paths.drafts_dir.rglob("*.md")}


def test_annotations_flow_back_into_the_graph(world: config_mod.Config) -> None:
    pipeline.cycle(world)

    def edit_git(fm: dict) -> None:
        fm["aliases"] = ["g"]
        fm["tags"] = [t for t in fm["tags"] if t != "vcs"] + ["daily-driver"]
        fm["summary"] = "My main VCS."
        fm["project"] = "kg"
        fm["relations"].append({"type": "alternative_to", "target": "tool/jj", "why": "evaluating"})

    _rewrite(world, "tool/git", edit_git, append="\nUse worktrees more.\n")
    _rewrite(world, "tool/less", lambda fm: fm.__setitem__("status", "ignore"))
    _rewrite(world, "script/bash-scripts--notify-sh", lambda fm: fm.__setitem__("status", "promote"))

    stats = pipeline.cycle(world)
    assert stats["feedback"]["read"] == 3 and stats["graph"]["ignored"] == 1

    fm, body = _doc(world, "tool/git")
    assert fm["interactive_uses"] == 12  # the four `g st` lines now count as git
    assert fm["subcommands"]["st"] == 4
    assert fm["summary"] == "My main VCS." and fm["project"] == "kg" and fm["rev"] == 2
    assert "daily-driver" in fm["tags"] and "vcs" not in fm["tags"]
    assert _edges(fm, "alternative_to")["tool/jj"]["provenance"] == "curated"
    assert body.rstrip().endswith("Use worktrees more.")

    assert _doc(world, "tool/g")[0]["status"] == "stale"  # folded into git; flagged, not deleted
    assert _doc(world, "tool/less")[0]["status"] == "ignore"  # tombstone left for you to undo
    assert _doc(world, "script/bash-scripts--notify-sh")[0]["status"] == "promote"
    for path in (world.paths.drafts_dir / "session").glob("*.md"):
        targets = {e["target"] for e in parse_document(path.read_text())[0]["relations"]}
        assert "tool/less" not in targets and "tool/g" not in targets

    third = pipeline.cycle(world)
    assert set(third["drafts"]) == {"unchanged"}  # the edits are absorbed; nothing oscillates

    # Undo the ignore: the node comes back.
    _rewrite(world, "tool/less", lambda fm: fm.__setitem__("status", "draft"))
    pipeline.cycle(world)
    assert _doc(world, "tool/less")[0]["interactive_uses"] == 3


def test_consumed_drafts_reappear_only_when_they_change(world: config_mod.Config) -> None:
    pipeline.cycle(world)
    target = world.paths.drafts_dir / "tool" / "git.md"
    target.unlink()  # the downstream ingest took it
    assert pipeline.cycle(world)["drafts"]["consumed"] == 1
    assert not target.exists()

    hist = world.history.files[0]
    lines = hist.read_text().splitlines()
    stale_open = lines.pop()  # close the open session by ageing it
    lines += [re.sub(r"\d{10}", str(NOW - 7200), stale_open), f": {NOW - 7100}:0;git push"]
    hist.write_text("\n".join(lines) + "\n")
    pipeline.cycle(world)
    assert target.exists() and _doc(world, "tool/git")[0]["rev"] == 2


def test_site_builds_from_store_and_from_directory(world: config_mod.Config, tmp_path: Path) -> None:
    pipeline.cycle(world)
    res = site.build(world)
    page = Path(res["path"]).read_text()
    blob = re.search(r'<script type="application/json" id="cll-data">(.*?)</script>', page, re.S)
    assert blob is not None
    data = json.loads(blob[1].replace("<\\/", "</"))
    assert len(data["nodes"]) == res["nodes"] and page.startswith("<!doctype html>")
    backup = next(n for n in data["nodes"] if n["id"] == "script/bash-scripts--backup-sh")
    assert "cll:relations" not in backup["html"] and "<h1>" not in backup["html"]

    other = site.build(world, source=world.paths.drafts_dir, out_dir=tmp_path / "out", fragment=True)
    assert other["nodes"] == res["nodes"]
    assert not (tmp_path / "out" / "index.html").read_text().startswith("<!doctype")


def test_schedule_units_point_at_this_interpreter(world: config_mod.Config) -> None:
    plist = schedule.launchd_plist(world, 900)
    assert "<integer>900</integer>" in plist and str(world.source) in plist and "<string>run</string>" in plist
    service, timer = schedule.systemd_units(world, 600)
    assert "OnUnitActiveSec=600" in timer and " -m cll " in service
