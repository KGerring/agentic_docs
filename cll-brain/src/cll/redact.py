"""Secret redaction, applied before any text reaches the state database.

Raw history, commit subjects and log lines are never persisted: every
collector passes text through :meth:`Redactor.scrub` first, so the event store
and every rendered draft only ever hold the scrubbed form.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable

MARK = "[REDACTED:{}]"

# Known credential shapes (prefix-identifiable).
_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("aws-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[abeprs]-[A-Za-z0-9\-]{10,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b")),
    ("pypi-token", re.compile(r"\bpypi-[A-Za-z0-9_\-]{40,}")),
)

_VALUE = r"""(?:'[^']*'|"[^"]*"|\$'[^']*'|[^\s;&|]+)"""

# NAME=value / NAME: value where NAME looks secret-bearing.
_ASSIGN = re.compile(
    r"(?i)\b([A-Z0-9_.\-]*(?:PASSWORD|PASSWD|PASSPHRASE|SECRET|TOKEN|API_?KEY|ACCESS_?KEY|"
    r"PRIVATE_?KEY|CREDENTIALS?|AUTH(?![A-Za-z])|SESSION_?ID|COOKIE)[A-Z0-9_]*)(\s*[=:]\s*)(" + _VALUE + ")"
)
# --password value / --token=value / --api-key value
_FLAG = re.compile(
    r"(?i)(?<![\w\-])(--?(?:password|passwd|passphrase|pass|token|secret|api[-_]?key|access[-_]?key|"
    r"auth[-_]?token|bearer|client[-_]secret|pw)(?:[-_][a-z]+)?)(=|\s+)(" + _VALUE + ")"
)
_HEADER = re.compile(r"(?i)\b((?:proxy-)?authorization\s*:\s*(?:bearer|basic|token)?\s*)([A-Za-z0-9._~+/=\-]{8,})")
_URL_USERINFO = re.compile(r"\b([a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^/\s@]+)@")
_CURL_USER = re.compile(r"((?:^|\s)(?:-u|--user)(?:=|\s+))(['\"]?)([^\s:'\"]+):([^\s'\"]+)(['\"]?)")
_CANDIDATE = re.compile(r"(?<![A-Za-z0-9+/=_\-])[A-Za-z0-9+/=_\-]{32,}(?![A-Za-z0-9+/=_\-])")

_VAR_REF = re.compile(r"""["']?\$(?:[A-Za-z_]\w*|\d+|[@*#?!]|\{[A-Za-z_]\w*\})["']?""")

_ENTROPY_FLOOR = 4.0


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _looks_random(tok: str) -> bool:
    """Mixed-case alphanumerics with high per-character entropy.

    Lower-case hex (git SHAs, checksums) and slash-heavy paths are deliberately
    left alone: they are common, useful, and not credentials on their own.
    """
    if tok.count("/") > 2 or tok.count("-") > 4 or tok.count("_") > 4:
        return False
    return (
        any(c.isdigit() for c in tok)
        and any(c.islower() for c in tok)
        and any(c.isupper() for c in tok)
        and _entropy(tok) >= _ENTROPY_FLOOR
    )


class Redactor:
    def __init__(self, extra_patterns: Iterable[str] = (), sensitive_tools: Iterable[str] = ()) -> None:
        self._extra = tuple(re.compile(p) for p in extra_patterns)
        self._sensitive = frozenset(sensitive_tools)
        self.hits: Counter[str] = Counter()

    def _sub(self, kind: str, pattern: re.Pattern[str], repl, text: str) -> str:
        out, n = pattern.subn(repl, text)
        if n:
            self.hits[kind] += n
        return out

    def scrub(self, text: str) -> str:
        for kind, pat in _SHAPES:
            text = self._sub(kind, pat, MARK.format(kind), text)
        for pat in self._extra:
            text = self._sub("custom", pat, MARK.format("custom"), text)
        text = self._sub("assignment", _ASSIGN, lambda m: f"{m[1]}{m[2]}{self._keep_or_mask(m[3], 'value')}", text)
        text = self._sub("flag", _FLAG, lambda m: f"{m[1]}{m[2]}{self._keep_or_mask(m[3], 'value')}", text)
        text = self._sub("header", _HEADER, lambda m: m[1] + MARK.format("credential"), text)
        text = self._sub("url-userinfo", _URL_USERINFO, lambda m: f"{m[1]}{m[2]}:{MARK.format('password')}@", text)
        text = self._sub("curl-user", _CURL_USER, lambda m: f"{m[1]}{m[2]}{m[3]}:{MARK.format('password')}{m[5]}", text)

        def high_entropy(m: re.Match[str]) -> str:
            if _looks_random(m[0]):
                self.hits["high-entropy"] += 1
                return MARK.format("high-entropy")
            return m[0]

        return _CANDIDATE.sub(high_entropy, text)

    @staticmethod
    def _keep_or_mask(value: str, kind: str) -> str:
        # A variable reference or an already-masked value carries no secret.
        if value.startswith("[REDACTED") or _VAR_REF.fullmatch(value):
            return value
        if value in {'""', "''"}:
            return value
        return MARK.format(kind)

    def scrub_command(self, text: str) -> str:
        """Scrub a shell command; arguments of secret-manager CLIs are dropped wholesale."""
        head = text.lstrip().split(None, 2)
        if head:
            tool = head[0].rsplit("/", 1)[-1]
            if tool in self._sensitive and len(head) > 1:
                self.hits["sensitive-tool"] += 1
                sub = head[1] if re.fullmatch(r"[a-z][a-z0-9\-]*", head[1]) else ""
                rest = MARK.format("args") if len(head) > 2 or not sub else ""
                return " ".join(x for x in (head[0], sub, rest) if x)
        return self.scrub(text)
