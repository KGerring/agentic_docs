"""Background scheduling: launchd (macOS), systemd user timer (Linux) or a cron line.

Nothing here runs unless ``cll schedule install`` is invoked; ``cll schedule show``
prints the unit files without touching the system.
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from cll.config import Config

LABEL = "dev.cll.collector"


def command(cfg: Config) -> list[str]:
    """Absolute argv for one cycle, pinned to the interpreter that is running now."""
    argv = [sys.executable, "-m", "cll"]
    if cfg.source is not None:
        argv += ["--config", str(cfg.source)]
    return [*argv, "run", "--quiet"]


def _path_env() -> str:
    # launchd and systemd start with a minimal PATH; git must be reachable.
    extra = [str(Path(p).parent) for p in (shutil.which("git"), shutil.which("uv")) if p]
    return os.pathsep.join(dict.fromkeys([*extra, "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]))


def launchd_plist(cfg: Config, interval: int) -> str:
    args = "\n".join(f"        <string>{escape(a)}</string>" for a in command(cfg))
    log = escape(str(cfg.paths.state_dir / "collector.log"))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args}
    </array>
    <key>StartInterval</key>
    <integer>{interval}</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>LowPriorityIO</key>
    <true/>
    <key>Nice</key>
    <integer>10</integer>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{escape(_path_env())}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>{log}</string>
    <key>StandardErrorPath</key>
    <string>{log}</string>
</dict>
</plist>
"""


def systemd_units(cfg: Config, interval: int) -> tuple[str, str]:
    service = f"""[Unit]
Description=cll second-brain collector

[Service]
Type=oneshot
ExecStart={shlex.join(command(cfg))}
Environment=PATH={_path_env()}
Nice=10
"""
    timer = f"""[Unit]
Description=Run the cll collector every {interval}s

[Timer]
OnBootSec=120
OnUnitActiveSec={interval}
Persistent=true

[Install]
WantedBy=timers.target
"""
    return service, timer


def cron_line(cfg: Config, interval: int) -> str:
    minutes = max(1, interval // 60)
    log = shlex.quote(str(cfg.paths.state_dir / "collector.log"))
    return f"*/{minutes} * * * * {shlex.join(command(cfg))} >> {log} 2>&1"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _systemd_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def show(cfg: Config, interval: int) -> str:
    if platform.system() == "Darwin":
        return f"# {_plist_path()}\n{launchd_plist(cfg, interval)}"
    service, timer = systemd_units(cfg, interval)
    d = _systemd_dir()
    return (
        f"# {d / 'cll-collector.service'}\n{service}\n# {d / 'cll-collector.timer'}\n{timer}\n"
        f"# or, as a crontab entry:\n{cron_line(cfg, interval)}\n"
    )


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True)


def install(cfg: Config, interval: int) -> str:
    cfg.paths.state_dir.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Darwin":
        path = _plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(launchd_plist(cfg, interval), encoding="utf-8")
        domain = f"gui/{os.getuid()}"
        _run("launchctl", "bootout", f"{domain}/{LABEL}")  # replace a previous install; fine if absent
        res = _run("launchctl", "bootstrap", domain, str(path))
        if res.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap failed: {res.stderr.strip() or res.stdout.strip()}")
        return f"installed {path}\nruns every {interval}s; log: {cfg.paths.state_dir / 'collector.log'}"
    if shutil.which("systemctl"):
        d = _systemd_dir()
        d.mkdir(parents=True, exist_ok=True)
        service, timer = systemd_units(cfg, interval)
        (d / "cll-collector.service").write_text(service, encoding="utf-8")
        (d / "cll-collector.timer").write_text(timer, encoding="utf-8")
        _run("systemctl", "--user", "daemon-reload")
        res = _run("systemctl", "--user", "enable", "--now", "cll-collector.timer")
        if res.returncode != 0:
            raise RuntimeError(f"systemctl enable failed: {res.stderr.strip()}")
        return (
            f"installed {d / 'cll-collector.timer'}\nruns every {interval}s; logs: journalctl --user -u cll-collector"
        )
    raise RuntimeError("no launchd or systemd here; add this line with `crontab -e`:\n" + cron_line(cfg, interval))


def uninstall() -> str:
    if platform.system() == "Darwin":
        _run("launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}")
        path = _plist_path()
        existed = path.exists()
        path.unlink(missing_ok=True)
        return f"removed {path}" if existed else "nothing installed"
    d = _systemd_dir()
    _run("systemctl", "--user", "disable", "--now", "cll-collector.timer")
    removed = [p for p in (d / "cll-collector.service", d / "cll-collector.timer") if p.exists()]
    for p in removed:
        p.unlink()
    _run("systemctl", "--user", "daemon-reload")
    return "removed " + ", ".join(map(str, removed)) if removed else "nothing installed"


def status() -> str:
    if platform.system() == "Darwin":
        res = _run("launchctl", "print", f"gui/{os.getuid()}/{LABEL}")
        if res.returncode != 0:
            return "not installed"
        keep = [
            ln.strip()
            for ln in res.stdout.splitlines()
            if ln.strip().startswith(("state =", "runs =", "last exit code =", "run interval ="))
        ]
        return "installed\n" + "\n".join(keep)
    res = _run("systemctl", "--user", "list-timers", "cll-collector.timer", "--no-pager")
    return res.stdout.strip() or "not installed"
