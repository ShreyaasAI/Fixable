"""Bash command classifier and phase policy (plan §12, M3; spec §8).

`classify(command, phase=…, allow=…)` returns `allow` (run autonomously), `approve` (the task
submitter must approve this exact command) or `forbid` (never runs; neither AGENTS.md `allow`
nor the user can override it).

How it works:
- The command is parsed with tree-sitter-bash. Every simple command found anywhere in the tree
  (lists, pipelines, subshells, substitutions, functions, loops, ERROR nodes) is checked, so
  chaining cannot hide a command. Nested scripts (`bash -c`, `eval`, heredocs fed to a shell,
  `find -exec`, `xargs`, wrappers like `env`/`timeout`/`command`) are analysed recursively.
- Words are resolved to their literal value (quotes, escapes, `$'..'`, concatenation, and
  variables assigned to literals earlier in the same script). Anything not resolvable is
  dynamic, which can never be `allow`.
- The most severe finding wins: forbid > approve > allow.
- Unparseable input, opaque constructs (subshells, substitutions, `eval`, interpreter `-c`,
  shells reading scripts) → approval at least.

Limits: the classifier cannot see what a program does at run time (a Python script can still
delete files). The sandbox (M2) is the security boundary; this decides what needs a human.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

import tree_sitter as ts
import tree_sitter_bash as tsb

Decision = Literal["allow", "approve", "forbid"]
_RANK: dict[str, int] = {"allow": 0, "approve": 1, "forbid": 2}

PHASES = ("prepare", "investigate", "plan", "execute", "verify", "confirm", "pr")
AGENT_PHASES = ("investigate", "execute", "verify")
READ_ONLY_PHASES = frozenset({"investigate", "plan"})

_ALL_AGENT = frozenset(AGENT_PHASES)
TOOL_PHASES: dict[str, frozenset[str]] = {
    "bash": _ALL_AGENT,
    "read_file": _ALL_AGENT,
    "list_dir": _ALL_AGENT,
    "search": _ALL_AGENT,
    "run_tests": _ALL_AGENT,
    "ask_user": _ALL_AGENT,
    "edit_file": frozenset({"execute"}),
    "write_file": frozenset({"execute"}),
    "submit_plan": frozenset({"investigate"}),
    "record_evidence": frozenset({"verify"}),
    "complete_task": frozenset({"verify"}),
}

MAX_DEPTH = 5

# --- command tables ------------------------------------------------------------------------

FORBIDDEN_COMMANDS = frozenset(
    {
        # privilege escalation
        "sudo", "su", "doas", "pkexec",
        # containers / orchestration (host access)
        "docker", "podman", "nerdctl", "ctr", "crictl", "kubectl",
        # raw network tools that bypass the proxy
        "nc", "ncat", "netcat", "socat", "ssh", "scp", "sftp", "telnet", "ftp", "nmap",
        "rsh", "rlogin",
        # GitHub API / remote changes (backend only)
        "gh",
        # DB drops
        "dropdb", "dropuser",
    }
)  # fmt: skip

READ_ONLY_COMMANDS = frozenset(
    {
        "ls", "pwd", "cd", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "tree",
        "diff", "cmp", "file", "stat", "du", "cut", "tr", "nl", "echo", "printf", "true",
        "false", ":", "test", "[", "basename", "dirname", "realpath", "readlink", "which",
        "type", "whoami", "id", "date", "uname", "sha256sum", "md5sum", "column",
    }
)  # fmt: skip

WRITER_COMMANDS = frozenset(
    {
        "mv", "cp", "touch", "mkdir", "rmdir", "ln", "chmod", "chown", "chgrp", "truncate",
        "dd", "install", "patch", "shred", "unlink", "tee", "rsync", "tar", "unzip", "gzip",
        "gunzip", "zip",
    }
)  # fmt: skip

SHELLS = frozenset({"bash", "sh", "dash", "zsh", "ksh", "ash", "mksh"})
_INTERPRETER_RE = re.compile(r"^(python[0-9.]*|node|nodejs|perl|ruby|php|lua|rscript|deno|bun)$")
_CODE_FLAGS = {"python": "c", "perl": "eE", "ruby": "e", "node": "ep", "php": "r", "lua": "e"}
DB_CLIENTS = frozenset(
    {"psql", "mysql", "mariadb", "sqlite3", "sqlcmd", "mongo", "mongosh", "redis-cli", "cqlsh"}
)
NETWORK_CLIENTS = frozenset({"curl", "wget", "pip", "pip3", "uv", "http", "https", "aria2c"})
PROXY_VARS = frozenset({"http_proxy", "https_proxy", "all_proxy", "no_proxy", "ftp_proxy"})

_PROXY_FLAGS = {
    "curl": ("--noproxy", "-x", "--proxy", "--preproxy", "--socks4", "--socks4a", "--socks5",
             "--socks5-hostname", "--proxy1.0", "--unix-socket", "--abstract-unix-socket"),
    "wget": ("--no-proxy", "-e", "--execute"),
    "pip": ("--proxy",), "pip3": ("--proxy",), "uv": ("--proxy",),
    "aria2c": ("--all-proxy", "--http-proxy", "--https-proxy", "--no-proxy"),
}  # fmt: skip

_SQL_DANGER = re.compile(
    r"\bdrop\s+(database|schema|table|user|role|owned|collection)\b|\btruncate\s+(table\s+)?\w"
    r"|\bflushall\b|\bflushdb\b|dropdatabase\s*\(|\.drop\s*\(",
    re.IGNORECASE,
)
_CREDENTIAL_PATHS = re.compile(
    r"(^|/)\.ssh(/|$)|\bid_(rsa|dsa|ecdsa|ed25519)\b|/proc/(self|thread-self|\d+)/environ"
    r"|\.git-credentials|(^|/)\.netrc\b|(^|/)\.aws/(credentials|config)|\.docker/config\.json"
    r"|\.config/gh/hosts\.ya?ml|\.kube/config|(^|/)\.pgpass\b|(^|/)\.pypirc\b|(^|/)\.npmrc\b",
)
_DOCKER_SOCKET = re.compile(r"docker\.sock|containerd\.sock|podman\.sock")
_DEV_NET = re.compile(r"/dev/(tcp|udp)/")
_SHELL_OPTS_WITH_ARG = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})

_GIT_READ = frozenset(
    {"status", "diff", "log", "show", "blame", "ls-files", "rev-parse", "grep", "shortlog",
     "describe", "cat-file", "ls-tree", "whatchanged", "reflog", "help", "version"}
)  # fmt: skip
_GIT_WRITE = frozenset(
    {"checkout", "switch", "reset", "restore", "apply", "am", "commit", "stash", "clean",
     "merge", "rebase", "cherry-pick", "revert", "rm", "mv", "add", "tag", "worktree", "pull",
     "fetch", "init", "clone", "submodule", "gc", "prune", "notes", "update-index",
     "update-ref", "branch", "config", "format-patch", "bisect", "sparse-checkout"}
)  # fmt: skip
_GIT_REMOTE_CHANGES = frozenset(
    {"add", "set-url", "remove", "rm", "rename", "set-head", "set-branches", "prune", "update"}
)
_GIT_DANGEROUS_KEYS = re.compile(
    r"^(remote\.|url\.|credential|include|core\.(hookspath|sshcommand|gitproxy|fsmonitor|askpass"
    r"|editor|pager)|https?\.proxy|filter\.|diff\.external|sequence\.editor|alias\.)",
    re.IGNORECASE,
)
_GIT_OPTS_WITH_ARG = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})

_SAFE_REDIRECT_TARGETS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "-"})
_WRITE_REDIRECTS = frozenset({">", ">>", "&>", "&>>", ">|", "<>", ">&", "&>|"})
_SED_PRINT_ONLY = re.compile(r"^[0-9$,;p\s]+$")

_PARSER = ts.Parser(ts.Language(tsb.language()))


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    reasons: tuple[str, ...] = ()


@dataclass
class _Acc:
    decision: Decision = "allow"
    reasons: list[str] = field(default_factory=list)

    def add(self, decision: Decision, reason: str) -> None:
        if _RANK[decision] > _RANK[self.decision]:
            self.decision = decision
        tag = f"{decision}: {reason}"
        if tag not in self.reasons:
            self.reasons.append(tag)


@dataclass
class _Ctx:
    read_only: bool
    allow: tuple[str, ...]
    acc: _Acc
    env: dict[str, str] = field(default_factory=dict)
    depth: int = 0


# --- public API ----------------------------------------------------------------------------


def classify(command: str, *, phase: str = "execute", allow: Sequence[str] = ()) -> Verdict:
    """Classify a bash command for `phase`; `allow` are AGENTS.md glob patterns."""
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}")
    acc = _Acc()
    if not command.strip():
        acc.add("approve", "empty command")
        return Verdict(acc.decision, tuple(acc.reasons))
    ctx = _Ctx(read_only=phase in READ_ONLY_PHASES, allow=tuple(allow), acc=acc)
    _analyze_script(command, ctx)
    return Verdict(acc.decision, tuple(acc.reasons))


def check_tool(tool: str, phase: str) -> Verdict:
    """Phase gate for agent tools (plan §12 table)."""
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}")
    phases = TOOL_PHASES.get(tool)
    if phases is None:
        return Verdict("forbid", (f"forbid: unknown tool {tool!r}",))
    if phase not in phases:
        return Verdict("forbid", (f"forbid: {tool} is not available in phase {phase}",))
    return Verdict("allow")


# --- parsing & tree walk -------------------------------------------------------------------


def _analyze_script(text: str, ctx: _Ctx) -> None:
    if ctx.depth > MAX_DEPTH:
        ctx.acc.add("approve", "nested too deeply to analyse")
        return
    tree = _PARSER.parse(text.encode("utf-8", "surrogatepass"))
    root = tree.root_node
    if root.has_error or _has_missing(root):
        ctx.acc.add("approve", "could not parse the command")
        # The partial tree may have lost words; scan the raw text for forbidden targets.
        _scan_paths([text], ctx)
    _visit(root, ctx, heredocs=())


def _has_missing(node: ts.Node) -> bool:
    return node.is_missing or any(_has_missing(c) for c in node.children)


def _nested(ctx: _Ctx, text: str) -> None:
    child = _Ctx(
        read_only=ctx.read_only,
        allow=ctx.allow,
        acc=ctx.acc,
        env=dict(ctx.env),
        depth=ctx.depth + 1,
    )
    _analyze_script(text, child)


def _txt(node: ts.Node) -> str:
    return (node.text or b"").decode("utf-8", "replace")


def _visit(node: ts.Node, ctx: _Ctx, heredocs: tuple[str, ...]) -> None:
    t = node.type
    if t == "command":
        _command(node, ctx, heredocs)
        return
    if t == "redirected_statement":
        bodies = list(heredocs)
        for r in node.children_by_field_name("redirect"):
            if r.type == "heredoc_redirect":
                bodies.extend(_txt(c) for c in r.children if c.type == "heredoc_body")
            elif r.type == "file_redirect":
                _file_redirect(r, ctx)
            elif r.type == "herestring_redirect":
                bodies.extend(_txt(c) for c in r.named_children)
        for c in node.children:
            if c.type not in {"heredoc_redirect", "file_redirect", "herestring_redirect"}:
                _visit(c, ctx, tuple(bodies))
        return
    if t == "pipeline":
        names = {_command_basename(c, ctx) for c in _commands_in(node)}
        if names & DB_CLIENTS:
            for c in _commands_in(node):
                _scan_sql(_all_words(c, ctx), ctx, "SQL drop piped into a database client")
    in_script = node.parent is not None and node.parent.type != "command"
    if t in {"variable_assignment", "variable_assignments"} and in_script:
        for a in [node] if t == "variable_assignment" else node.named_children:
            _assignment(a, ctx, scope="script")
    if t == "declaration_command":
        for a in node.named_children:
            if a.type == "variable_assignment":
                _assignment(a, ctx, scope="script")
            elif a.type in {"variable_name", "word"} and _txt(a).lower() in PROXY_VARS:
                ctx.acc.add("forbid", "changes the egress proxy settings")
    if t == "unset_command":
        for a in node.named_children:
            if _txt(a).lower() in PROXY_VARS:
                ctx.acc.add("forbid", "changes the egress proxy settings")
    if t == "subshell":
        ctx.acc.add("approve", "subshell")
    elif t == "command_substitution":
        ctx.acc.add("approve", "command substitution")
    elif t == "process_substitution":
        ctx.acc.add("approve", "process substitution")
    elif t == "function_definition":
        ctx.acc.add("approve", "function definition")
    elif t == "ERROR":
        ctx.acc.add("approve", "could not parse the command")
    for c in node.children:
        if t == "variable_assignments" and c.type == "variable_assignment":
            continue
        _visit(c, ctx, heredocs)


def _commands_in(node: ts.Node) -> Iterable[ts.Node]:
    for c in node.named_children:
        if c.type == "command":
            yield c
        elif c.type == "redirected_statement":
            yield from _commands_in(c)


def _command_basename(node: ts.Node, ctx: _Ctx) -> str | None:
    name = node.child_by_field_name("name")
    lit = _literal(name, ctx) if name is not None else None
    return _basename(lit) if lit else None


def _all_words(node: ts.Node, ctx: _Ctx) -> list[str]:
    out: list[str] = []
    for c in node.named_children:
        lit = _literal(c, ctx)
        out.append(lit if lit is not None else _txt(c))
    return out


def _assignment(node: ts.Node, ctx: _Ctx, *, scope: str) -> None:
    name_node = node.child_by_field_name("name")
    name = _txt(name_node) if name_node is not None else ""
    if name.lower() in PROXY_VARS:
        ctx.acc.add("forbid", "changes the egress proxy settings")
    if scope != "script" or not name:
        return
    value = node.child_by_field_name("value")
    lit = "" if value is None else _literal(value, ctx)
    if lit is None:
        ctx.env.pop(name, None)
    else:
        ctx.env[name] = lit


def _file_redirect(node: ts.Node, ctx: _Ctx) -> None:
    op = "".join(c.type for c in node.children if not c.is_named)
    dest_node = node.child_by_field_name("destination")
    dest = _literal(dest_node, ctx) if dest_node is not None else None
    raw = _txt(dest_node) if dest_node is not None else ""
    _scan_paths([dest if dest is not None else raw], ctx)
    if op not in _WRITE_REDIRECTS:
        return
    if dest_node is not None and dest_node.type == "number":
        return  # fd duplication such as 2>&1
    if dest in _SAFE_REDIRECT_TARGETS:
        return
    _writer(ctx, "writes a file via redirection")


# --- word resolution -----------------------------------------------------------------------

_ESCAPE_IN_DQ = re.compile(r'\\([$`"\\\n])')


def _literal(node: ts.Node | None, ctx: _Ctx) -> str | None:
    """Literal value of a word-like node, or None if it depends on run time."""
    if node is None:
        return None
    t = node.type
    if t == "command_name":
        return _literal(node.named_children[0], ctx) if node.named_children else None
    if t == "word":
        return _unquote(_txt(node))
    if t in {"number", "variable_name", "file_descriptor"}:
        return _txt(node)
    if t == "raw_string":
        return _txt(node)[1:-1]
    if t == "ansi_c_string":
        return _decode_ansi_c(_txt(node)[2:-1])
    if t in {"string", "translated_string"}:
        parts: list[str] = []
        for c in node.named_children:
            if c.type == "string_content":
                parts.append(_ESCAPE_IN_DQ.sub(r"\1", _txt(c)))
            else:
                v = _literal(c, ctx)
                if v is None:
                    return None
                parts.append(v)
        return "".join(parts)
    if t == "concatenation":
        values = [_literal(c, ctx) for c in node.named_children]
        if any(v is None for v in values):
            return None
        # tree-sitter splits words like `{}` into pieces; keep unnamed pieces too.
        joined: list[str] = []
        for c in node.children:
            v = _literal(c, ctx) if c.is_named else _txt(c)
            joined.append(v if v is not None else _txt(c))
        return "".join(joined)
    if t == "simple_expansion":
        names = [c for c in node.named_children if c.type == "variable_name"]
        return ctx.env.get(_txt(names[0])) if len(names) == 1 else None
    if t == "expansion":
        inner = node.named_children
        if len(inner) == 1 and inner[0].type == "variable_name":
            return ctx.env.get(_txt(inner[0]))
        return None
    return None


def _unquote(s: str) -> str | None:
    """Shell quote removal for a word tree-sitter kept whole (e.g. r''m, a"b"c)."""
    out: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            out.append(s[i + 1])
            i += 2
        elif ch == "'":
            end = s.find("'", i + 1)
            if end < 0:
                return None
            out.append(s[i + 1 : end])
            i = end + 1
        elif ch == '"':
            end = i + 1
            while end < len(s) and s[end] != '"':
                end += 2 if s[end] == "\\" else 1
            if end >= len(s):
                return None
            inner = s[i + 1 : end]
            if "$" in inner or "`" in inner:
                return None
            out.append(_ESCAPE_IN_DQ.sub(lambda m: m.group(1), inner))
            i = end + 1
        elif ch in "$`":
            return None
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _decode_ansi_c(s: str) -> str | None:
    try:
        return s.encode("latin-1", "backslashreplace").decode("unicode_escape")
    except (UnicodeDecodeError, ValueError):
        return None


def _basename(word: str) -> str:
    return word.rsplit("/", 1)[-1]


# --- simple commands -----------------------------------------------------------------------


def _command(node: ts.Node, ctx: _Ctx, heredocs: tuple[str, ...]) -> None:
    for a in node.children:
        if a.type == "variable_assignment":
            _assignment(a, ctx, scope="command")
    name = node.child_by_field_name("name")
    args = node.children_by_field_name("argument")
    words = [name, *args] if name is not None else list(args)
    # Substitutions inside words are commands too.
    for w in words:
        _visit_inside_word(w, ctx)
    argv = [_literal(w, ctx) for w in words]
    raw = [_txt(w) for w in words]
    _scan_paths([a if a is not None else r for a, r in zip(argv, raw, strict=True)], ctx)
    if not argv:
        return
    _argv(argv, raw, ctx, heredocs)


def _visit_inside_word(node: ts.Node, ctx: _Ctx) -> None:
    if node.type in {"command_substitution", "process_substitution"}:
        _visit(node, ctx, ())
        return
    for c in node.children:
        _visit_inside_word(c, ctx)


def _argv(argv: list[str | None], raw: list[str], ctx: _Ctx, heredocs: tuple[str, ...]) -> None:
    acc = ctx.acc
    if argv[0] is None:
        acc.add("approve", f"dynamic command name {raw[0]!r}")
        return
    name = _basename(argv[0].lstrip("\\"))
    unwrapped = _unwrap(name, argv, ctx)
    if unwrapped is not None:
        if unwrapped:
            inner_raw = raw[len(argv) - len(unwrapped) :]
            _argv(unwrapped, inner_raw, ctx, heredocs)
        return

    lname = name.lower()
    if lname in FORBIDDEN_COMMANDS:
        acc.add("forbid", f"forbidden command: {name}")
        return
    if name == "rm":
        _rm(argv, raw, ctx)
        return
    if name == "git":
        _git(argv, ctx)
        return
    if lname in SHELLS:
        _shell(argv, ctx, heredocs)
        return
    if name in {"eval"}:
        acc.add("approve", "eval")
        if all(a is not None for a in argv[1:]):
            _nested(ctx, " ".join(a for a in argv[1:] if a is not None))
        return
    if name in {"source", "."}:
        acc.add("approve", "sources a script")
        return
    if name == "unset":
        if any(a is not None and a.lower() in PROXY_VARS for a in argv[1:]):
            acc.add("forbid", "changes the egress proxy settings")
        acc.add("approve", "unset")
        return
    if lname in DB_CLIENTS:
        _scan_sql([*(a or "" for a in argv), *raw, *heredocs], ctx, "SQL drop")
        acc.add("approve", f"database client {name}")
        return
    if _INTERPRETER_RE.match(lname):
        _interpreter(lname, argv, raw, ctx, heredocs)
        return
    if lname in NETWORK_CLIENTS:
        flags = _PROXY_FLAGS.get(lname, ())
        for a in argv[1:]:
            if a is not None and any(a == f or a.startswith(f + "=") for f in flags):
                acc.add("forbid", f"{name} {a.split('=')[0]} bypasses the egress proxy")
        if lname in {"curl"}:
            for a in argv[1:]:
                if a is not None and a.startswith("-x") and len(a) > 2:
                    acc.add("forbid", "curl -x bypasses the egress proxy")
        if _allowed_by_pattern(argv, ctx):
            return
        acc.add("approve", f"network client {name}")
        return
    if name == "find":
        _find(argv, raw, ctx, heredocs)
        return
    if name == "sed":
        _sed(argv, ctx)
        return
    if name in {"awk", "gawk", "mawk", "nawk"}:
        acc.add("approve", f"{name} can run commands")
        return
    if name in WRITER_COMMANDS:
        _writer(ctx, f"{name} writes files")
        return
    if name in {"sort"} and any(a is not None and a.startswith(("-o", "--output")) for a in argv):
        _writer(ctx, "sort -o writes a file")
        return
    if any(a is None for a in argv[1:]) and not _all_dynamic_args_harmless(name):
        acc.add("approve", f"{name} has dynamic arguments")
        return
    if name in READ_ONLY_COMMANDS or name == "sort" or (name == "uniq" and len(argv) <= 2):
        return
    if _allowed_by_pattern(argv, ctx):
        return
    acc.add("approve", f"{name} is not in the allowlist")


def _all_dynamic_args_harmless(name: str) -> bool:
    return name in {"echo", "printf", "true", "false", ":"}


def _allowed_by_pattern(argv: list[str | None], ctx: _Ctx) -> bool:
    if any(a is None for a in argv):
        return False
    line = " ".join(a for a in argv if a is not None)
    return any(fnmatch.fnmatchcase(line, p) for p in ctx.allow)


def _writer(ctx: _Ctx, reason: str) -> None:
    if ctx.read_only:
        ctx.acc.add("forbid", f"{reason} in a read-only phase")
    else:
        ctx.acc.add("approve", reason)


# --- wrappers ------------------------------------------------------------------------------

_WRAPPER_OPTS_WITH_ARG: dict[str, frozenset[str]] = {
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "exec": frozenset({"-a"}),
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "ionice": frozenset({"-c", "-n", "-p", "--class", "--classdata"}),
    "xargs": frozenset({"-I", "-n", "-P", "-d", "-E", "-s", "-L", "-a", "--max-args",
                        "--max-procs", "--delimiter", "--arg-file", "--replace"}),
    "nohup": frozenset(), "time": frozenset(), "command": frozenset(), "builtin": frozenset(),
    "setsid": frozenset(), "chronic": frozenset(), "watch": frozenset({"-n", "--interval"}),
    "coproc": frozenset(), "busybox": frozenset(),
}  # fmt: skip


def _unwrap(name: str, argv: list[str | None], ctx: _Ctx) -> list[str | None] | None:
    """For wrapper commands return the wrapped argv ([] if none); None if not a wrapper."""
    if name not in _WRAPPER_OPTS_WITH_ARG:
        return None
    opts = _WRAPPER_OPTS_WITH_ARG[name]
    i = 1
    while i < len(argv):
        a = argv[i]
        if a is None:
            ctx.acc.add("approve", f"{name} has dynamic arguments")
            return []
        if name == "env" and "=" in a and not a.startswith("-"):
            if a.split("=", 1)[0].lower() in PROXY_VARS:
                ctx.acc.add("forbid", "changes the egress proxy settings")
            i += 1
            continue
        if a == "--":
            i += 1
            break
        if a.startswith("-") and a != "-":
            key = a.split("=", 1)[0]
            takes_arg = key in opts and "=" not in a
            if name == "env" and key in {"-u", "--unset"}:
                target = (
                    a.split("=", 1)[1] if "=" in a else (argv[i + 1] if i + 1 < len(argv) else "")
                )
                if (target or "").lower() in PROXY_VARS:
                    ctx.acc.add("forbid", "changes the egress proxy settings")
            if name == "env" and key in {"-S", "--split-string"}:
                ctx.acc.add("approve", "env -S re-splits its argument")
                script = (
                    a.split("=", 1)[1] if "=" in a else (argv[i + 1] if i + 1 < len(argv) else "")
                )
                if script is None:
                    return []
                _nested(
                    ctx, " ".join([script, *(x or "" for x in argv[i + (1 if "=" in a else 2) :])])
                )
                return []
            i += 2 if takes_arg else 1
            continue
        break
    if name == "timeout" and i < len(argv):
        i += 1  # the duration
    rest = argv[i:]
    if name in {"xargs", "watch"}:
        ctx.acc.add("approve", f"{name} runs a command with run-time arguments")
        if name == "watch" and rest and all(r is not None for r in rest):
            _nested(ctx, " ".join(r for r in rest if r is not None))
            return []
    if name == "env" and not rest:
        ctx.acc.add("approve", "prints the environment")
    return rest


# --- specific commands ---------------------------------------------------------------------


def _short_flags(argv: list[str | None]) -> tuple[set[str], set[str], bool]:
    """(short letters, long options, has_dynamic) up to `--`."""
    short: set[str] = set()
    long_: set[str] = set()
    dynamic = False
    for a in argv[1:]:
        if a is None:
            dynamic = True
            continue
        if a == "--":
            break
        if a.startswith("--"):
            long_.add(a.split("=", 1)[0])
        elif a.startswith("-") and len(a) > 1:
            short.update(a[1:])
    return short, long_, dynamic


def _rm(argv: list[str | None], raw: list[str], ctx: _Ctx) -> None:
    short, long_, dynamic = _short_flags(argv)
    # Flags built at run time (brace expansion `-{r,f}`, quotes) count by their raw letters.
    for a, r in zip(argv[1:], raw[1:], strict=True):
        if a is None and r.startswith("-") and not r.startswith("--"):
            short.update(ch for ch in r[1:] if ch.isalpha())
    recursive = bool(short & {"r", "R"}) or "--recursive" in long_
    force = "f" in short or "--force" in long_
    if recursive and force:
        ctx.acc.add("forbid", "rm with recursive + force (rm -rf in any form)")
        return
    if dynamic:
        ctx.acc.add("approve", "rm has dynamic arguments")
    _writer(ctx, "rm deletes files")


def _git(argv: list[str | None], ctx: _Ctx) -> None:
    acc = ctx.acc
    i = 1
    while i < len(argv):
        a = argv[i]
        if a is None:
            acc.add("approve", "git has dynamic arguments")
            return
        if not a.startswith("-"):
            break
        key = a.split("=", 1)[0]
        value = a.split("=", 1)[1] if "=" in a and key != "-c" else None
        if key == "-c" or (key in _GIT_OPTS_WITH_ARG and value is None):
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if key == "-c" and (nxt is None or _GIT_DANGEROUS_KEYS.match(nxt)):
                acc.add("forbid", "git -c sets a dangerous config key")
            i += 2
            continue
        i += 1
    sub = argv[i] if i < len(argv) else None
    rest = argv[i + 1 :]
    if sub is None:
        acc.add("approve", "git without a subcommand")
        return
    if sub == "push":
        acc.add("forbid", "git push (the backend opens the PR)")
        return
    if sub == "remote":
        action = rest[0] if rest else None
        if action is None or action in {"-v", "--verbose", "show", "get-url"}:
            acc.add("approve", "git remote")
            return
        if action in _GIT_REMOTE_CHANGES or action is None:
            acc.add("forbid", "git remote change")
            return
        acc.add("approve", "git remote")
        return
    if sub == "config":
        keys = [r for r in rest if r is not None and not r.startswith("-")]
        if any(r is None for r in rest) or any(_GIT_DANGEROUS_KEYS.match(k) for k in keys):
            acc.add("forbid", "git config changes remotes, hooks, credentials or proxies")
            return
        _writer(ctx, "git config")
        return
    if sub in _GIT_READ:
        if any(r is not None and r.startswith("--output") for r in rest):
            _writer(ctx, "git --output writes a file")
            return
        if any(r is None for r in rest):
            acc.add("approve", "git has dynamic arguments")
        return
    if sub in _GIT_WRITE:
        _writer(ctx, f"git {sub} changes the repository")
        return
    acc.add("approve", f"git {sub}")


def _shell(argv: list[str | None], ctx: _Ctx, heredocs: tuple[str, ...]) -> None:
    acc = ctx.acc
    script: str | None = None
    has_c = False
    operands: list[str | None] = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a is None:
            operands.append(None)
            i += 1
            continue
        if a in _SHELL_OPTS_WITH_ARG and not operands:
            i += 2
            continue
        if a == "--" and not operands:
            operands.extend(argv[i + 1 :])
            break
        if a[:1] in "-+" and len(a) > 1 and not a.startswith("--") and not operands:
            if a.startswith("-") and "c" in a[1:]:
                has_c = True
            i += 1
            continue
        if a.startswith("--") and not operands:
            i += 1
            continue
        operands.append(a)
        i += 1
    if has_c:
        acc.add("approve", "shell -c runs a nested script")
        script = operands[0] if operands else None
        if script is None:
            acc.add("approve", "shell -c with a dynamic script")
        else:
            _nested(ctx, script)
        return
    if operands:
        acc.add("approve", "runs a shell script file")
        return
    acc.add("approve", "shell reads commands from stdin")
    for body in heredocs:
        _nested(ctx, body)


def _interpreter(
    lname: str, argv: list[str | None], raw: list[str], ctx: _Ctx, heredocs: tuple[str, ...]
) -> None:
    family = "python" if lname.startswith("python") else lname.replace("nodejs", "node")
    code_flags = _CODE_FLAGS.get(family, "e")
    short, long_, dynamic = _short_flags(argv)
    if family == "perl" and "i" in short:
        _writer(ctx, "perl -i edits files in place")
    has_code = bool(short & set(code_flags)) or "--eval" in long_ or "--print" in long_
    reads_stdin = "-" in argv[1:] or (len(argv) == 1 and bool(heredocs))
    if has_code or reads_stdin or heredocs:
        _scan_sql([*(a or "" for a in argv), *raw, *heredocs], ctx, "SQL drop in inline code")
        ctx.acc.add("approve", f"{argv[0]} runs inline code")
        return
    if dynamic:
        ctx.acc.add("approve", f"{argv[0]} has dynamic arguments")
        return
    if _allowed_by_pattern(argv, ctx):
        return
    ctx.acc.add("approve", f"{argv[0]} runs a program")


def _find(argv: list[str | None], raw: list[str], ctx: _Ctx, heredocs: tuple[str, ...]) -> None:
    acc = ctx.acc
    i = 1
    found_action = False
    while i < len(argv):
        a = argv[i]
        if a in {"-exec", "-execdir", "-ok", "-okdir"}:
            found_action = True
            j = i + 1
            while j < len(argv) and argv[j] not in {";", "+"}:
                j += 1
            inner = [x for x in argv[i + 1 : j]]
            inner_raw = raw[i + 1 : j]
            acc.add("approve", f"find {a} runs a command")
            if inner:
                _argv(inner, inner_raw, ctx, heredocs)
            i = j + 1
            continue
        if a == "-delete":
            found_action = True
            _writer(ctx, "find -delete removes files")
            acc.add("approve", "find -delete removes files")
        elif a in {"-fprint", "-fprint0", "-fprintf", "-fls"}:
            found_action = True
            _writer(ctx, f"find {a} writes a file")
        elif a is None:
            acc.add("approve", "find has dynamic arguments")
        i += 1
    if not found_action:
        return


def _sed(argv: list[str | None], ctx: _Ctx) -> None:
    short, long_, dynamic = _short_flags(argv)
    if "i" in short or "--in-place" in long_:
        _writer(ctx, "sed -i edits files in place")
        return
    if dynamic:
        ctx.acc.add("approve", "sed has dynamic arguments")
        return
    scripts = _sed_scripts(argv)
    if "n" in short and scripts and all(_SED_PRINT_ONLY.match(s) for s in scripts):
        return
    ctx.acc.add("approve", "sed script may write files or run commands")


def _sed_scripts(argv: list[str | None]) -> list[str]:
    scripts: list[str] = []
    explicit = False
    i = 1
    while i < len(argv):
        a = argv[i] or ""
        if a in {"-e", "--expression"} and i + 1 < len(argv):
            scripts.append(argv[i + 1] or "")
            explicit = True
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        if not explicit and not scripts:
            scripts.append(a)
        i += 1
    return scripts


# --- content scans -------------------------------------------------------------------------


def _scan_paths(words: Iterable[str], ctx: _Ctx) -> None:
    for w in words:
        if _DOCKER_SOCKET.search(w):
            ctx.acc.add("forbid", "touches the container runtime socket")
        if _CREDENTIAL_PATHS.search(w):
            ctx.acc.add("forbid", "reads credentials")
        if _DEV_NET.search(w):
            ctx.acc.add("forbid", "raw network access via /dev/tcp or /dev/udp")


def _scan_sql(words: Iterable[str], ctx: _Ctx, reason: str) -> None:
    if any(_SQL_DANGER.search(w) for w in words):
        ctx.acc.add("forbid", reason)
