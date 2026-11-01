"""testbench connect: set up a coding agent to use agent-testbench in one command.

    testbench connect cursor            # .cursor/mcp.json and AGENTS.md in this project
    testbench connect codex --user      # ~/.codex/config.toml: every project of this user
    testbench connect gemini --dry-run  # show what would change, change nothing
    testbench connect --list            # each client: here?, set up in this project?, for the user?

It registers the agent-testbench MCP server in the client's own config file and writes the working rules into a
delimited section of AGENTS.md (the file these clients read). Files are merged, never replaced: every other entry
stays, the old file is kept as <file>.bak before a change, an agent-testbench entry that is already there is left
as it is, and running it again changes nothing. File formats, locations and the date each was checked against the
client's documentation: docs/clients.md.

The server is started as `<this python> -m agent_testbench mcp`: an absolute path to the interpreter testbench is
installed in, so it starts whatever PATH the client gives it (apps opened from a dock, and Codex, which passes the
server only PATH, HOME and a few others), and it is the same interpreter the server runs every tool with. From a
throwaway environment (uvx, pipx run) it registers `uvx --from agent-testbench[all]==VERSION testbench mcp`.
"""
from __future__ import annotations

import difflib
import importlib.util
import json
import os
import re
import shutil
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import __version__

SERVER = "agent-testbench"
NAMES = (SERVER, "testbench")                         # an entry under either name already is this server
OURS = re.compile(r"agent[-_]testbench|(^|[\s/\\])testbench(\.exe)?\s+mcp\b")
START, END = "<!-- agent-testbench:start -->", "<!-- agent-testbench:end -->"
LEGACY = ("<!-- agent-testbench -->", "<!-- /agent-testbench -->")    # the section `testbench init` writes
CODEX_TOOL_TIMEOUT = 900       # Codex stops waiting for a tool after 300 s; a 10-minute exec must be able to finish

RULES = """\
## Working here with agent-testbench

Written by `testbench connect`, which replaces only this section when run again.

Results in this project are proved on real hardware with agent-testbench: the `agent-testbench` MCP tools (pass
this project's folder as `project_dir`), or the `testbench` command.

1. Explore in a live session: `session_start`, then `session_exec` (code, a file, or notebook cells; variables
   persist, plots come back as images, errors name the line). `session_stop` as soon as you are done.
2. Prove with a plan: `plans` lists them with their worst-case cost, `run_plan` runs one, `get_report` says what
   failed and why and what changed since the last run. A session is not proof. Look at plots yourself.
3. Change one thing at a time and record what you learned (`add_note`). To raise a number, use a loop:
   `loop_start`, then `loop_try` for each idea.
4. Money is the person's decision: before anything that can cost money, say what will run, where, and its worst
   case, and wait for a yes. Never set `approved_by_person` (or `--approve`) yourself. Never raise `budget:` values
   in testbench.yaml, edit ~/.testbench/limits.yaml or touch a ledger (`ledger.jsonl`); if a limit is in the way,
   say which one. Start cloud work only through testbench (no raw `modal run` or `colab new`).
5. Secrets by name only; never print or copy a value. Paid APIs get strict mocks in tests. Never weaken an
   expectation to make a run pass.
6. Done means a passing run of the plan that matters: name the run, what it cost, and what is still unproven.
"""

HOLDS = """\
Guardrails that hold in every client, because they are in testbench's own code:
  - every run, session and loop is priced before it starts and refused over max_run_usd or the day's budget;
  - ~/.testbench/limits.yaml caps every project on this machine (a project can lower it, never raise it);
  - a hard stop on the machine doing the work (Modal's timeouts, the watchdog on Colab VMs, ssh machines and in
    containers), so remote work stops at its limit even if the client or this machine goes away;
  - above ask_above_usd a launch needs a person's yes: the server asks the person directly when the client
    supports MCP elicitation, and otherwise refuses and says what needs approval."""


def _home() -> Path:
    return Path.home()


def _env_dir(var: str, default: str) -> Path:
    return Path(os.environ[var]).expanduser() if os.environ.get(var) else _home() / default


@dataclass
class Client:
    key: str
    name: str
    docs: str                                      # where the format was checked (docs/clients.md has the dates)
    fmt: str                                       # json | toml
    project: tuple | None                          # (path relative to the project, key holding the servers)
    user: Callable[[], tuple] | None               # () -> (absolute path, key holding the servers)
    exes: tuple = ()                               # commands that show it is installed
    dirs: tuple = ()                               # folders under HOME that show it is installed
    apps: tuple = ()                               # absolute paths that show it is installed (macOS apps)
    entry: Callable[[str, list, bool], dict] = None    # (command, args, user) -> the server's entry
    hook: str = ""                                 # what about the guard hook in this client
    notes: list = field(default_factory=list)      # what to do next, printed after a change


def _stdio(cmd, args, user):
    return {"type": "stdio", "command": cmd, "args": args}


def _plain(cmd, args, user):
    return {"command": cmd, "args": args}


def _vscode(cmd, args, user):
    return _plain(cmd, args, user) if user else _stdio(cmd, args, user)


def _codex(cmd, args, user):
    return {"command": cmd, "args": args, "tool_timeout_sec": CODEX_TOOL_TIMEOUT}


CLIENTS = {c.key: c for c in (
    Client("claude", "Claude Code", "https://code.claude.com/docs/en/mcp", "json",
           (".mcp.json", "mcpServers"), lambda: (_home() / ".claude.json", "mcpServers"),
           exes=("claude",), dirs=(".claude",), entry=_stdio,
           hook="The guard hook comes with the Claude Code plugin, not with this file: `claude plugin marketplace add "
                "sarptandoven/agent-testbench && claude plugin install agent-testbench@agent-testbench` adds it and "
                "the iterate and setup skills.",
           notes=["Claude Code asks once before it starts a server from a project's .mcp.json: say yes, then /mcp "
                  "shows agent-testbench."]),
    Client("cursor", "Cursor", "https://cursor.com/docs/mcp", "json",
           (".cursor/mcp.json", "mcpServers"), lambda: (_home() / ".cursor" / "mcp.json", "mcpServers"),
           exes=("cursor", "cursor-agent"), dirs=(".cursor",), apps=("/Applications/Cursor.app",), entry=_plain,
           hook="Cursor runs no testbench hook (the Cursor plugin declares none).",
           notes=["In Cursor: Settings > MCP shows agent-testbench; turn it on if it is off."]),
    Client("vscode", "VS Code (GitHub Copilot agent mode)",
           "https://code.visualstudio.com/docs/agents/reference/mcp-configuration", "json",
           (".vscode/mcp.json", "servers"), lambda: (_env_dir("COPILOT_HOME", ".copilot") / "mcp-config.json",
                                                     "mcpServers"),
           exes=("code", "code-insiders"), dirs=(".vscode", ".copilot"),
           apps=("/Applications/Visual Studio Code.app",), entry=_vscode,
           hook="VS Code runs no testbench hook (connect installs none).",
           notes=["In VS Code: trust the workspace (servers do not start in Restricted Mode), run `MCP: List Servers` "
                  "and start agent-testbench, then use Copilot Chat in Agent mode."]),
    Client("codex", "OpenAI Codex", "https://developers.openai.com/codex/mcp", "toml",
           (".codex/config.toml", "mcp_servers"), lambda: (_env_dir("CODEX_HOME", ".codex") / "config.toml",
                                                           "mcp_servers"),
           exes=("codex",), dirs=(".codex",), entry=_codex,
           hook="Codex runs no testbench hook (connect installs none).",
           notes=["Codex reads a project's .codex/config.toml only once the project is trusted (it asks when you "
                  "open it); `/mcp` in codex lists agent-testbench."]),
    Client("gemini", "Gemini CLI", "https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md",
           "json", (".gemini/settings.json", "mcpServers"), lambda: (_home() / ".gemini" / "settings.json",
                                                                     "mcpServers"),
           exes=("gemini",), dirs=(".gemini",), entry=_plain,
           hook="Gemini CLI runs no testbench hook (connect installs none).",
           notes=["Gemini CLI reads a project's .gemini/settings.json in a trusted folder; `/mcp` lists "
                  "agent-testbench. It hides variables named like *KEY*, *TOKEN* or *SECRET* from servers: a secret a "
                  "plan passes by name must be listed in the server's env (\"NAME\": \"$NAME\")."]),
)}
UNSUPPORTED = {"windsurf": "Windsurf (now Devin Desktop) is moving its MCP config between files and its documentation "
                           "could not be checked here; add the server by hand (docs/clients.md has the entry)."}


# ---------------------------------------------------------------- the command the client runs

def _ephemeral(exe: str) -> bool:
    """True when this interpreter lives in a throwaway environment (uvx, pipx run) that may be gone tomorrow."""
    parts = Path(exe).parts
    cache = os.environ.get("UV_CACHE_DIR")
    if cache and Path(exe).is_relative_to(Path(cache).expanduser()):
        return True
    return any(p in ("archive-v0", "builds-v0") for p in parts) or ("pipx" in parts and ".cache" in parts)


def server_command(exe: str | None = None) -> tuple[str, list]:
    exe = os.path.abspath(exe or sys.executable)
    if _ephemeral(exe):
        return "uvx", ["--from", f"agent-testbench[all]=={__version__}", "testbench", "mcp"]
    return exe, ["-m", "agent_testbench", "mcp"]


# ---------------------------------------------------------------- reading and writing the files

class Refused(Exception):
    """A file this will not change, and why (with what to add by hand)."""


def _read(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _is_ours(name: str, entry) -> bool:
    if name in NAMES:
        return True
    if not isinstance(entry, dict):
        return False
    args = entry.get("args") if isinstance(entry.get("args"), list) else []
    return bool(OURS.search(" ".join(str(x) for x in [entry.get("command", ""), *args])))


def _missing(entry) -> str | None:
    """The entry's command when it is an absolute path that is not on this machine (a file written elsewhere)."""
    cmd = entry.get("command") if isinstance(entry, dict) else None
    return cmd if isinstance(cmd, str) and os.path.isabs(cmd) and not os.path.exists(cmd) else None


def _already(name: str, entry) -> str:
    named = "" if name == SERVER else f" (as \"{name}\")"
    out = f"already has the {SERVER} server{named}: left as it is"
    if cmd := _missing(entry):
        out += (f", but its command {cmd} is not on this machine, so the client cannot start it: remove that entry "
                "and run this again, or use --user")
    return out


NUMBER = re.compile(r"-?\d+(\.\d+)?([eE][+-]?\d+)?|true|false|null")


class JSONText:
    """A JSON file edited as text: a member is added by inserting its text, so everything already there - each
    entry, its formatting, and the comments and trailing commas VS Code's JSONC allows - stays as it was."""

    def __init__(self, path: Path, text: str):
        self.path, self.t = path, text if text.strip() else "{}\n"
        self.nl = _newline(text)
        m = re.search(r"\n([ \t]+)\S", text)
        self.unit = m.group(1) if m else "  "

    def _bad(self, i: int, what: str):
        line = self.t.count("\n", 0, i) + 1
        return Refused(f"{self.path} is not JSON this can read ({what} on line {line})")

    def _ws(self, i: int) -> int:
        t, n = self.t, len(self.t)
        while i < n:
            if t[i] in " \t\r\n﻿":
                i += 1
            elif t.startswith("//", i):
                j = t.find("\n", i)
                i = n if j == -1 else j
            elif t.startswith("/*", i):
                j = t.find("*/", i + 2)
                if j == -1:
                    raise self._bad(i, "a comment that is not closed")
                i = j + 2
            else:
                break
        return i

    def _string(self, i: int) -> int:
        t, i = self.t, i + 1
        while i < len(t):
            if t[i] == "\\":
                i += 2
            elif t[i] == '"':
                return i + 1
            elif t[i] == "\n":
                break
            else:
                i += 1
        raise self._bad(i, "a string that is not closed")

    def _value(self, i: int) -> int:
        """Where the value starting at i ends."""
        c = self.t[i:i + 1]
        if c == "{":
            return self._object(i)[2]
        if c == "[":
            i = self._ws(i + 1)
            while self.t[i:i + 1] != "]":
                i = self._ws(self._value(i))
                if self.t[i:i + 1] == ",":
                    i = self._ws(i + 1)
                elif self.t[i:i + 1] != "]":
                    raise self._bad(i, "no , or ] after a value")
            return i + 1
        if c == '"':
            return self._string(i)
        m = NUMBER.match(self.t, i)
        if not m:
            raise self._bad(i, f"{c!r} where a value should be" if c else "the end of the file too early")
        return m.end()

    def _object(self, i: int) -> tuple:
        """(start, members, end) of the object at i; each member is (key, key start, value start, value end)."""
        start, members, i = i, [], self._ws(i + 1)
        while self.t[i:i + 1] != "}":
            if self.t[i:i + 1] != '"':
                raise self._bad(i, "a key that is not a string")
            ke = self._string(i)
            key = json.loads(self.t[i:ke])
            j = self._ws(ke)
            if self.t[j:j + 1] != ":":
                raise self._bad(j, "no : after a key")
            vs = self._ws(j + 1)
            ve = self._value(vs)
            members.append((key, i, vs, ve))
            i = self._ws(ve)
            if self.t[i:i + 1] == ",":
                i = self._ws(i + 1)
            elif self.t[i:i + 1] != "}":
                raise self._bad(i, "no , or } after a value")
        return start, members, i + 1

    def root(self) -> tuple:
        i = self._ws(0)
        if self.t[i:i + 1] != "{":
            raise Refused(f"{self.path} does not hold a JSON object")
        obj = self._object(i)
        if self._ws(obj[2]) != len(self.t):
            raise self._bad(obj[2], "more after the end of the object")
        return obj

    def data(self):
        """The parsed file, comments and trailing commas left out."""
        out, t, i, n = [], self.t, 0, len(self.t)
        while i < n:
            c = t[i]
            if c == '"':
                j = self._string(i)
                out.append(t[i:j])
                i = j
            elif t.startswith("//", i) or t.startswith("/*", i) or c == "﻿":
                i = self._ws(i)
            elif c == "," and t[self._ws(i + 1):self._ws(i + 1) + 1] in ("}", "]"):
                i += 1
            else:
                out.append(c)
                i += 1
        return json.loads("".join(out))

    def _dump(self, v, ind: str) -> str:
        if isinstance(v, dict) and v:
            inner = ind + self.unit
            return "{" + ",".join(f"{self.nl}{inner}{json.dumps(k, ensure_ascii=False)}: {self._dump(x, inner)}"
                                  for k, x in v.items()) + self.nl + ind + "}"
        return json.dumps(v, ensure_ascii=False)

    def _line_indent(self, i: int) -> str:
        start = self.t.rfind("\n", 0, i) + 1
        return re.match(r"[ \t]*", self.t[start:]).group(0)

    def _insert(self, obj: tuple, key: str, value) -> None:
        start, members, end = obj
        t, close = self.t, end - 1
        if members:
            _, ks, _, ve = members[-1]
            j = ve + len(t[ve:]) - len(t[ve:].lstrip(" \t"))
            comma = t[j:j + 1] == ","                             # a trailing comma (JSONC) is there already
            at = j + 1 if comma else ve
            if t.rfind("\n", start, ks) == -1:                    # all on one line: stay on it
                self.t = (t[:ve] + ("" if comma else ",") + t[ve:at] + f" {json.dumps(key)}: "
                          + json.dumps(value, ensure_ascii=False) + t[at:])
                return
            k = at + len(t[at:]) - len(t[at:].lstrip(" \t"))
            if t.startswith("//", k):                             # a comment ends that line: it stays there
                eol = t.find("\n", k)
                at = len(t) if eol == -1 else eol - (t[eol - 1] == "\r")
            ind = self._line_indent(ks)
            self.t = (t[:ve] + ("" if comma else ",") + t[ve:at] + f"{self.nl}{ind}{json.dumps(key)}: "
                      + self._dump(value, ind) + t[at:])
            return
        base = self._line_indent(start)
        ind = base + self.unit
        member = f"{self.nl}{ind}{json.dumps(key)}: {self._dump(value, ind)}"
        if t[start + 1:close].strip():                            # only comments inside: they follow the member
            self.t = t[:start + 1] + member + t[start + 1:]
        else:
            self.t = t[:start + 1] + member + self.nl + base + t[close:]

    def put(self, path: list, value) -> None:
        """Set the member at path (keys from the root) to value: the objects on the way are added if missing; an
        existing value at the end of the path is replaced (only its text)."""
        obj = self.root()
        for depth, key in enumerate(path):
            m = next((m for m in reversed(obj[1]) if m[0] == key), None)
            if m is None:
                for k in reversed(path[depth + 1:]):
                    value = {k: value}
                self._insert(obj, key, value)
                return
            if depth == len(path) - 1:
                self.t = self.t[:m[2]] + json.dumps(value, ensure_ascii=False) + self.t[m[3]:]
                return
            if self.t[m[2]:m[2] + 1] != "{":
                raise Refused(f"{self.path}: \"{key}\" is not an object")
            obj = self._object(m[2])


def _json_load(path: Path, text: str) -> dict:
    doc = JSONText(path, text)
    doc.root()
    return doc.data()


def _json_servers(path: Path, data: dict, key: str) -> dict:
    servers = data.get(key, {})
    if not isinstance(servers, dict):
        raise Refused(f"{path}: \"{key}\" is not an object")
    return servers


def _gemini_names(data: dict):
    """Gemini CLI reads GEMINI.md unless context.fileName names others: the names with AGENTS.md added, keeping what
    it reads now; None when AGENTS.md is there already (or context is not an object)."""
    ctx = data.get("context", {})
    if not isinstance(ctx, dict):
        return None
    names = ctx.get("fileName")
    names = ["GEMINI.md"] if names in (None, "", []) else [names] if isinstance(names, str) else list(names)
    return None if "AGENTS.md" in names else names + ["AGENTS.md"]


def merge_json(path: Path, text: str, key: str, entry: dict, gemini: bool = False) -> tuple[str, list]:
    """The file with the server added (and for Gemini CLI, AGENTS.md among its context files), and what changed.
    Only new text is inserted, and the result must read back as the old data plus exactly that."""
    doc = JSONText(path, text)
    doc.root()
    data = doc.data()
    servers = _json_servers(path, data, key)
    have = next((n for n, e in servers.items() if _is_ours(n, e)), None)
    done = []
    if have:
        done.append(_already(have, servers[have]))
    else:
        doc.put([key, SERVER], entry)
        data.setdefault(key, {})[SERVER] = entry
        done.append(f"added the {SERVER} server under \"{key}\"")
    names = _gemini_names(data) if gemini else None
    if names:
        doc.put(["context", "fileName"], names)
        data.setdefault("context", {})["fileName"] = names
        done.append("context.fileName now also names AGENTS.md (Gemini CLI reads only GEMINI.md otherwise)")
    if have and not names:
        return text, done
    try:
        same = JSONText(path, doc.t).data() == data
    except (Refused, ValueError):
        same = False
    if not same:
        raise Refused(f"{path}: adding the entry as text did not give the expected file")
    return doc.t, done


def _toml_module():
    for name in ("tomllib", "tomli", "pip._vendor.tomli"):
        try:
            return __import__(name, fromlist=["loads"])
        except ImportError:
            continue
    return None


TOML_HEADER = re.compile(r"""^[ \t]*\[[ \t]*mcp_servers[ \t]*\.[ \t]*("([^"]+)"|'([^']+)'|([A-Za-z0-9_-]+))[ \t]*\]""",
                         re.M)


def toml_servers(path: Path, text: str) -> dict:
    """{name: entry} under mcp_servers. With a TOML parser (Python 3.11+, or tomli) that is exact; without one, the
    names of [mcp_servers.NAME] tables and the text of each."""
    toml = _toml_module()
    if toml is not None:
        try:
            servers = toml.loads(text).get("mcp_servers", {})
        except Exception as exc:
            raise Refused(f"{path} is not valid TOML ({exc})") from None
        if not isinstance(servers, dict):
            raise Refused(f"{path}: mcp_servers is not a table")
        return servers
    heads = list(TOML_HEADER.finditer(text))
    out = {}
    for i, m in enumerate(heads):
        body = text[m.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)].split("\n[", 1)[0]
        cmd = re.search(r"""^[ \t]*command[ \t]*=[ \t]*("(?:[^"\\]|\\.)*"|'[^']*')""", body, re.M)
        try:
            cmd = json.loads(cmd.group(1)) if cmd and cmd.group(1)[0] == '"' else cmd.group(1)[1:-1] if cmd else ""
        except ValueError:
            cmd = cmd.group(1)
        out[m.group(2) or m.group(3) or m.group(4)] = {
            "command": cmd, "args": re.findall(r"^[ \t]*args[ \t]*=(.*)$", body, re.M)}
    return out


def toml_str(s: str) -> str:
    """A TOML basic string: quotes, backslashes and control characters escaped (Windows paths come out right)."""
    out = []
    for ch in s:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append({"\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}.get(ch, f"\\u{ord(ch):04X}"))
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return toml_str(str(v))


def merge_toml(path: Path, text: str, key: str, entry: dict) -> tuple[str, list]:
    """The file with a [mcp_servers.agent-testbench] table appended; everything already in it, comments included,
    stays byte for byte."""
    servers = toml_servers(path, text)
    have = next((n for n, e in servers.items() if _is_ours(n, e)), None)
    if have:
        return text, [_already(have, servers[have])]
    first_table = re.search(r"^[ \t]*\[", text, re.M)
    if re.search(rf"^[ \t]*{key}[ \t]*=", text[:first_table.start()] if first_table else text, re.M):
        raise Refused(f"{path} sets {key} as one inline table, which TOML does not let a [{key}.{SERVER}] table "
                      "extend")
    nl = _newline(text)
    lines = [f"# {SERVER}: added by `testbench connect`", f"[{key}.{SERVER}]"]
    lines += [f"{k} = {toml_value(v)}" for k, v in entry.items()]
    block = nl.join(lines) + nl
    out = (text.rstrip("\r\n") + nl + nl if text.strip() else "") + block
    toml = _toml_module()
    if toml is not None:
        try:
            ok = toml.loads(out)[key][SERVER] == entry
        except Exception:
            ok = False
        if not ok:
            raise Refused(f"{path}: with the table added it would not read back as TOML holding the server")
    return out, [f"added [{key}.{SERVER}]"]


def put_section(text: str, body: str) -> str:
    """text with the agent-testbench section replaced (or the one `testbench init` wrote), or appended."""
    nl = _newline(text)
    block = (f"{START}\n{body.rstrip()}\n{END}\n").replace("\n", nl)
    for start, end in ((START, END), LEGACY):
        i = text.find(start)
        j = text.find(end, i + len(start)) if i != -1 else -1
        if j != -1:
            rest = text[j + len(end):]
            rest = rest[len(nl):] if rest.startswith(nl) else rest.lstrip("\r\n")
            return text[:i] + block + rest
    return (text.rstrip("\r\n") + nl + nl if text.strip() else "") + block


CLAUDE_IMPORT = re.compile(r"(?<![`\w])@(\./)?AGENTS\.md\b")


# ---------------------------------------------------------------- the changes, then writing them

@dataclass
class Change:
    path: Path
    old: str | None            # None: the file is new
    new: str
    what: list
    ours: list | None = None   # a client's config: the only values a dry run shows (the others can be secrets)

    @property
    def changed(self) -> bool:
        return self.old != self.new


def _strings(v) -> list:
    if isinstance(v, dict):
        return [s for k, x in v.items() for s in (str(k), *_strings(x))]
    if isinstance(v, list):
        return [s for x in v for s in _strings(x)]
    return [v] if isinstance(v, str) else []


QUOTED = re.compile(r""""(?:[^"\\\n]|\\.)*"(\s*:)?|'[^'\n]*'""")     # a quoted value, or a JSON key (before :)


def _hidden(text: str, ours: list) -> str:
    """text with every quoted value shown as "..." but the ones this adds: the other servers in a client's config
    can hold secrets (an API key in their env, args or headers), which a dry run must never print. Keys stay."""
    keep = {q(s) for s in ours for q in (json.dumps, toml_str, lambda x: json.dumps(x, ensure_ascii=False))}
    return QUOTED.sub(lambda m: m.group(0) if m.group(1) or m.group(0) in keep else '"..."', text)


def _in_project(path: Path, project: Path) -> Path:
    """A project file this writes, refused when it really is outside the project (a link, or in a linked folder): a
    repository can ship .cursor/mcp.json as a link to ~/.cursor/mcp.json, whose servers' keys would be copied in."""
    if not path.resolve().is_relative_to(project.resolve()):
        raise Refused(f"{path.relative_to(project)} is a link to {path.resolve()}, outside the project, so this does "
                      "not write it; replace the link with a file of the project's own, or remove it")
    return path


def _plan_config(client: Client, project: Path, user: bool, cmd: str, args: list) -> Change:
    if user:
        path, key = client.user()
    else:
        rel, key = client.project
        path = _in_project(project / rel, project)
    old = _read(path) if path.exists() else None
    entry = client.entry(cmd, args, user)
    if client.fmt == "toml":
        new, what = merge_toml(path, old or "", key, entry)
    else:
        new, what = merge_json(path, old or "", key, entry, gemini=client.key == "gemini")
    return Change(path, old, new, what, ours=[key, SERVER, "context", "fileName", "AGENTS.md", *_strings(entry)])


def _plan_text(path: Path, transform, what: str) -> Change:
    _in_project(path, path.parent)
    old = _read(path) if path.exists() else None
    new = transform(old or "")
    return Change(path, old, new, [what if new != old else "already up to date"])


def _plan_rules(client: Client, project: Path) -> list:
    changes = [_plan_text(project / "AGENTS.md", lambda t: put_section(t, RULES),
                          "the agent-testbench section (the working rules)")]
    if client.key == "claude":
        # Claude Code reads AGENTS.md itself only when the project has no CLAUDE.md; a CLAUDE.md that imports it
        # works in every version.
        claude = project / "CLAUDE.md"
        text = _read(claude) if claude.exists() else ""
        if START in text or not CLAUDE_IMPORT.search(text):       # unless it imports AGENTS.md already
            changes.append(_plan_text(claude, lambda t: put_section(t, "@AGENTS.md"),
                                      "imports AGENTS.md, so Claude Code reads the working rules"))
    return changes


def _new_file(path: Path, data: bytes, private: bool) -> None:
    """data in a new file at path; private: readable by this user only until the caller copies the old file's mode
    onto it (a client's config can hold API keys, and its copies are never readable by others, even for a moment)."""
    path.unlink(missing_ok=True)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o666), "wb") as f:
        f.write(data)


def _write(change: Change) -> str | None:
    """Write the change: the old file kept as <file>.bak first, then the new one put in place at once."""
    change.path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if change.old is not None:
        backup = change.path.with_name(change.path.name + ".bak")
        _new_file(backup, change.path.read_bytes(), private=True)
        shutil.copystat(change.path, backup)
    tmp = change.path.with_name(change.path.name + ".testbench-tmp")
    _new_file(tmp, change.new.encode("utf-8"), private=change.old is not None)
    if change.old is not None:
        shutil.copymode(change.path, tmp)
    os.replace(tmp, change.path)
    return str(backup) if backup else None


def _shown(path: Path, project: Path) -> str:
    for base, prefix in ((project, ""), (_home(), "~/")):
        try:
            return prefix + path.relative_to(base).as_posix()
        except ValueError:
            continue
    return str(path)


def _diff(change: Change, label: str) -> str:
    lines = difflib.unified_diff((change.old or "").splitlines(keepends=True), change.new.splitlines(keepends=True),
                                 fromfile=label if change.old is not None else "/dev/null", tofile=label)
    return "".join(x if x.endswith("\n") else x + "\n" for x in lines)


def project_dir(start: Path | None = None) -> tuple[Path, bool]:
    """(the project folder, whether it has a testbench.yaml): the nearest folder with one, else this folder."""
    here = Path(start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / "testbench.yaml").is_file():
            return d, True
    return here, False


def connect(key: str, user: bool = False, dry_run: bool = False, start: Path | None = None) -> tuple[int, str]:
    """Set up one client; (exit code, what was done, in words for whoever reads it next)."""
    if key in UNSUPPORTED:
        return 2, f"testbench connect: {key} is not supported: {UNSUPPORTED[key]}"
    client = CLIENTS[key]
    project, has_config = project_dir(start)
    cmd, args = server_command()
    out = [f"{client.name}:"]
    if cmd != "uvx" and importlib.util.find_spec("mcp") is None:
        msg = (f"the MCP server needs the mcp package in {cmd}, which does not have it: "
               f"`{cmd} -m pip install \"agent-testbench[mcp]\"`, then run this again.")
        if not dry_run:
            return 2, f"testbench connect: {msg}"
        out.append(f"  note: {msg}")
    try:
        changes = [_plan_config(client, project, user, cmd, args)]
    except Refused as exc:
        entry = client.entry(cmd, args, user)
        by_hand = (f"[{client.project[1]}.{SERVER}]\n" + "\n".join(f"{k} = {toml_value(v)}" for k, v in entry.items())
                   if client.fmt == "toml" else json.dumps({SERVER: entry}, indent=2))
        return 2, (f"testbench connect: {exc}. Nothing was changed. Add this entry yourself under the file's "
                   f"servers:\n{by_hand}")
    except (OSError, UnicodeDecodeError) as exc:
        return 2, f"testbench connect: could not read {client.name}'s config ({exc}). Nothing was changed."
    rules = not user or has_config
    try:
        changes += _plan_rules(client, project) if rules else []
    except Refused as exc:
        return 2, f"testbench connect: {exc}. Nothing was changed."
    except (OSError, UnicodeDecodeError) as exc:
        return 2, f"testbench connect: could not read the rules file ({exc}). Nothing was changed."
    for ch in changes:
        label = _shown(ch.path, project)
        if not ch.changed:
            out.append(f"  {label}: {'; '.join(ch.what)}; nothing to change")
        elif dry_run:
            out.append(f"  would change {label}: {'; '.join(ch.what)}")
            diff = _diff(ch, label)
            out.append((diff if ch.ours is None else _hidden(diff, ch.ours)).rstrip("\n"))
        else:
            try:
                backup = _write(ch)
            except OSError as exc:
                out.append(f"  could not write {label} ({exc}); it is as it was")
                return 2, "\n".join(out)
            out.append(f"  wrote {label}: {'; '.join(ch.what)}" +
                       (f" (the old file is {_shown(Path(backup), project)})" if backup else ""))
    if not rules:
        out.append("  AGENTS.md: not written (no testbench.yaml here); run `testbench connect "
                   f"{key}` in a project to add its working rules")
    elif not has_config:
        out.append("  This folder has no testbench.yaml yet: `testbench init` writes one with plans for the project.")
    out.append(f"The server starts with: {' '.join(_quote(x) for x in [cmd, *args])}")
    if dry_run:
        out.append("Dry run: nothing was written.")
    elif any(ch.changed for ch in changes):
        out += [f"Next: {n}" for n in client.notes]
    out.append(client.hook)
    return 0, "\n".join(out)


NOT_HERE = """\
Not in {where}: the guard hook (the Claude Code plugin's), which asks before an edit raises a budget or changes
pricing:, guard: or ~/.testbench/limits.yaml, refuses edits to the ledgers, raw cloud launches and commands that
print secrets, and turns an agent's own approved_by_person=true into a question for the person. There, an
approval the agent passes is taken at its word (still never above max_run_usd or the day's limits): keep the
client's own confirmation on for run_plan, session_start and loop_start, and review changes to testbench.yaml."""


def guardrails(keys: list) -> str:
    """What still holds in these clients, and what does not (the guard hook)."""
    names = ["Claude Code without the plugin" if k == "claude" else CLIENTS[k].name for k in keys]
    where = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
    return HOLDS + "\n" + textwrap.fill(" ".join(NOT_HERE.format(where=where).split()), 116)


def _quote(s: str) -> str:
    return s if re.fullmatch(r"[\w@%+=:,./\\[\]-]+", s) else json.dumps(s)


# ---------------------------------------------------------------- --list

def _found(client: Client) -> str:
    for exe in client.exes:
        if shutil.which(exe):
            return f"yes ({exe})"
    for d in client.dirs:
        if (_home() / d).is_dir():
            return f"yes (~/{d})"
    for app in client.apps:
        if Path(app).exists():
            return f"yes ({app})"
    return "no"


def _configured(client: Client, path: Path, key: str, project: Path) -> str:
    label = _shown(path, project)
    if not path.exists():
        return f"no ({label})"
    try:
        text = _read(path)
        if client.fmt == "toml":
            servers = toml_servers(path, text)
        else:
            servers = _json_servers(path, _json_load(path, text), key)
    except (Refused, OSError, UnicodeDecodeError):
        return f"unreadable ({label})"
    ours = [e for n, e in servers.items() if _is_ours(n, e)]
    if ours and all(_missing(e) for e in ours):
        return f"broken ({label}: its command is not on this machine)"
    return f"yes ({label})" if ours else f"no ({label})"


def status(start: Path | None = None) -> str:
    project, _ = project_dir(start)
    rows = [("client", "on this machine", "this project", "for the user")]
    for c in CLIENTS.values():
        rows.append((c.key, _found(c), _configured(c, project / c.project[0], c.project[1], project),
                     _configured(c, *c.user(), project)))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip() for r in rows]
    lines.append("")
    lines.append(f"Project: {project}. Set one up: testbench connect CLIENT (this project) or "
                 "testbench connect CLIENT --user (every project); --dry-run shows the change first.")
    lines += [f"Not supported: {k} - {why}" for k, why in UNSUPPORTED.items()]
    return "\n".join(lines)


def main(clients: list, user: bool = False, dry_run: bool = False, list_: bool = False) -> int:
    if list_ or not clients:
        print(status())
        return 0
    unknown = [c for c in clients if c not in CLIENTS and c not in UNSUPPORTED]
    if unknown:
        print(f"testbench connect: unknown client {', '.join(unknown)}; one of {', '.join(CLIENTS)}", file=sys.stderr)
        return 2
    code, done = 0, []
    for i, key in enumerate(dict.fromkeys(clients)):
        rc, text = connect(key, user=user, dry_run=dry_run)
        print(("\n" if i else "") + text, file=sys.stderr if rc else sys.stdout)
        code = max(code, rc)
        done += [key] if rc == 0 else []
    if done:
        print("\n" + guardrails(done))
    return code
