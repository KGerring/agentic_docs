"""A small POSIX-ish shell scanner.

Used for interactive history lines and as the fallback script parser when
tree-sitter is not installed. It understands quoting, command substitution,
pipelines, lists, redirections, subshells and ``case`` patterns well enough to
answer one question: *which commands does this text run?* It is not a full
grammar; scripts get exact results from the tree-sitter path instead.
"""

from __future__ import annotations

import re

# Reserved words that may precede a command on the same list item.
PREFIX_KEYWORDS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{", "coproc"})
# Heads that mean "this item is syntax, not a command".
NON_COMMAND_HEADS = frozenset(
    {
        "for",
        "case",
        "select",
        "function",
        "[[",
        "((",
        "fi",
        "done",
        "esac",
        "}",
        "in",
        "]]",
        "))",
    }
)
# Transparent wrappers: the command of interest is what follows them.
WRAPPERS = frozenset(
    {
        "sudo",
        "doas",
        "command",
        "exec",
        "nohup",
        "env",
        "noglob",
        "nocorrect",
        "builtin",
        "time",
        "nice",
        "ionice",
        "caffeinate",
        "stdbuf",
        "setsid",
    }
)
BUILTINS = frozenset(
    {
        ".",
        ":",
        "[",
        "alias",
        "autoload",
        "bg",
        "bind",
        "bindkey",
        "break",
        "builtin",
        "caller",
        "cd",
        "command",
        "compdef",
        "compgen",
        "complete",
        "continue",
        "declare",
        "dirs",
        "disown",
        "echo",
        "emulate",
        "enable",
        "eval",
        "exec",
        "exit",
        "export",
        "false",
        "fc",
        "fg",
        "getopts",
        "hash",
        "help",
        "history",
        "jobs",
        "let",
        "local",
        "logout",
        "mapfile",
        "popd",
        "print",
        "printf",
        "pushd",
        "pwd",
        "read",
        "readarray",
        "readonly",
        "return",
        "set",
        "setopt",
        "shift",
        "shopt",
        "source",
        "suspend",
        "test",
        "times",
        "trap",
        "true",
        "type",
        "typeset",
        "ulimit",
        "umask",
        "unalias",
        "unset",
        "unsetopt",
        "wait",
        "whence",
        "zmodload",
        "zstyle",
        "zle",
        "rehash",
        "unfunction",
        "functions",
        "integer",
        "float",
        "vared",
        "where",
        "which",
    }
)
# Tools whose first positional argument is a subcommand worth recording.
SUBCOMMAND_TOOLS = frozenset(
    {
        "git",
        "uv",
        "pixi",
        "docker",
        "podman",
        "brew",
        "npm",
        "pnpm",
        "yarn",
        "cargo",
        "kubectl",
        "gh",
        "pip",
        "pip3",
        "conda",
        "mamba",
        "launchctl",
        "systemctl",
        "apt",
        "apt-get",
        "go",
        "poetry",
        "terraform",
        "helm",
        "gcloud",
        "aws",
        "az",
        "pre-commit",
        "tmux",
        "rustup",
        "pyenv",
        "nvm",
        "pipx",
        "hatch",
        "ruff",
        "defaults",
        "diskutil",
        "journalctl",
        "dnf",
        "pacman",
        "mise",
        "just",
    }
)

_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
_TOOL_OK = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+\-]*")
_SUB_OK = re.compile(r"[a-z][a-z0-9\-]*")

Command = tuple[int, list[str]]  # (character offset of the first word, words)


def split_commands(text: str) -> list[Command]:
    """Every simple command in ``text``, including those inside ``$(…)``."""
    out: list[Command] = []
    _scan(text, 0, None, out)
    out.sort(key=lambda c: c[0])
    return out


def _scan(text: str, i: int, closer: str | None, out: list[Command]) -> int:
    n = len(text)
    words: list[str] = []
    cur: list[str] = []
    in_word = False
    word_start = cmd_start = -1
    skip_next = False  # the next completed word is a redirection target
    depth = 0  # '(' opened at this level
    chain = len(out)  # where the current pipeline started; a case pattern `a|b)` unwinds to here

    def end_word() -> None:
        nonlocal in_word, skip_next, cmd_start
        if in_word:
            if skip_next:
                skip_next = False
            else:
                if not words:
                    cmd_start = word_start
                words.append("".join(cur))
            cur.clear()
            in_word = False

    def end_cmd() -> None:
        nonlocal skip_next
        end_word()
        skip_next = False
        if words:
            out.append((cmd_start, words.copy()))
            words.clear()

    def begin(at: int) -> None:
        nonlocal in_word, word_start
        if not in_word:
            in_word, word_start = True, at

    while i < n:
        c = text[i]
        if closer is not None and c == closer and depth == 0:
            end_cmd()
            return i + 1
        if c == "\\":
            if i + 1 < n and text[i + 1] == "\n":
                i += 2
                continue
            begin(i)
            cur.append(text[i + 1 : i + 2])
            i += 2
        elif c == "'":
            begin(i)
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            cur.append(text[i + 1 : j])
            i = j + 1
        elif c == '"':
            begin(i)
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    cur.append(text[i + 1])
                    i += 2
                elif text.startswith("$(", i) and not text.startswith("$((", i):
                    i = _scan(text, i + 2, ")", out)
                    cur.append("$(…)")
                elif text[i] == "`":
                    i = _scan(text, i + 1, "`", out)
                    cur.append("$(…)")
                else:
                    cur.append(text[i])
                    i += 1
            i += 1
        elif text.startswith("$((", i) or (text.startswith("((", i) and not in_word and not words):
            j = text.find("))", i)
            j = n if j < 0 else j + 2
            begin(i)
            cur.append(text[i:j])
            i = j
        elif text.startswith(("$(", "<(", ">("), i):
            begin(i)
            i = _scan(text, i + 2, ")", out)
            cur.append("$(…)")
        elif text.startswith("${", i):
            begin(i)
            j, level = i + 2, 1
            while j < n and level:
                level += (text[j] == "{") - (text[j] == "}")
                j += 1
            cur.append(text[i:j])
            i = j
        elif c == "`":
            begin(i)
            i = _scan(text, i + 1, "`", out)
            cur.append("$(…)")
        elif c == "#" and not in_word:
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c in " \t":
            end_word()
            i += 1
        elif c in "\n;":
            end_cmd()
            chain = len(out)
            i += 1
        elif c == "|":
            end_cmd()
            if text[i + 1 : i + 2] == "|":
                chain = len(out)
                i += 2
            else:
                i += 2 if text[i + 1 : i + 2] == "&" else 1
        elif c == "&":
            if text[i + 1 : i + 2] == ">":  # &> file
                end_word()
                skip_next = True
                i += 3 if text[i + 2 : i + 3] == ">" else 2
            else:
                end_cmd()
                chain = len(out)
                i += 2 if text[i + 1 : i + 2] == "&" else 1
        elif c in "<>":
            if in_word and "".join(cur).isdigit():  # fd prefix: 2>file
                cur.clear()
                in_word = False
            end_word()
            j = i
            while j < n and text[j] in "<>&|-":
                j += 1
            skip_next = True
            i = j
        elif c == "(":
            if text[i + 1 : i + 2] == ")" and (in_word or words):  # name() — a function definition
                cur.clear()
                in_word = False
                words.clear()
                i += 2
            else:
                end_cmd()
                chain = len(out)
                depth += 1
                i += 1
        elif c == ")":
            if depth:
                depth -= 1
                end_cmd()
            else:  # an unmatched ')' closes a case pattern, which is not a command
                cur.clear()
                in_word = False
                words.clear()
                del out[chain:]
            chain = len(out)
            i += 1
        else:
            begin(i)
            cur.append(c)
            i += 1
    end_cmd()
    return i


def command_head(words: list[str]) -> tuple[str, list[str]] | None:
    """``(argv0, args)`` after stripping keywords, ``VAR=x`` prefixes and wrappers."""
    i, n = 0, len(words)
    while i < n:
        w = words[i]
        if w in PREFIX_KEYWORDS:
            i += 1
        elif w in NON_COMMAND_HEADS:
            return None
        elif _ASSIGN.match(w):
            i += 1
        elif w in WRAPPERS and i + 1 < n:
            i += 1
            while i < n and (words[i].startswith("-") or _ASSIGN.match(words[i])):
                i += 1
        else:
            return w, words[i + 1 :]
    return None


def tool_name(argv0: str) -> str | None:
    """Basename of an executable word, or ``None`` when it is not a literal name."""
    if not argv0 or (argv0[0] in "$-~" and "/" not in argv0) or "$" in argv0 or "…" in argv0:
        return None
    name = argv0.rstrip("/").rsplit("/", 1)[-1]
    return name if _TOOL_OK.fullmatch(name) else None


def subcommand(tool: str, args: list[str]) -> str | None:
    if tool not in SUBCOMMAND_TOOLS:
        return None
    for a in args:
        if a.startswith("-"):
            continue
        return a if _SUB_OK.fullmatch(a) else None
    return None


_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def blank_heredocs(text: str) -> str:
    """Replace heredoc bodies with spaces, keeping every offset and newline in place."""
    lines = text.split("\n")
    out: list[str] = []
    waiting: list[str] = []
    for line in lines:
        if waiting:
            if line.strip() == waiting[0]:
                waiting.pop(0)
            out.append(" " * len(line))
            continue
        out.append(line)
        if "<<" in line and "<<<" not in line:
            code = line.split("#", 1)[0] if re.search(r"(^|\s)#", line) else line
            waiting = [m[2] for m in _HEREDOC.finditer(code)]
    return "\n".join(out)
