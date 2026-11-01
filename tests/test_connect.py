"""testbench connect: each client's config file written into a project (or, with --user, under HOME only), merged
with what is there, backed up, idempotent; AGENTS.md's section replaced, never duplicated; --list; and the command
it registers really starts the MCP server."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading

import pytest

from agent_testbench import connect as K
from conftest import cli, write_config

try:
    import tomllib
except ImportError:                       # Python 3.10
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None


def read_toml(text: str) -> dict:
    """TOML as a dict: with tomllib/tomli when there is one, else the subset these tests write ([a.b] tables and
    key = value lines whose values read as JSON - strings, arrays of strings, numbers)."""
    if tomllib is not None:
        return tomllib.loads(text)
    out, table = {}, None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            table = out
            for part in re.findall(r'"([^"]+)"|([A-Za-z0-9_-]+)', line.strip("[]")):
                table = table.setdefault(part[0] or part[1], {})
            continue
        key, value = (x.strip() for x in line.split("=", 1))
        (table if table is not None else out)[key.strip('"')] = json.loads(value.split("  #")[0])
    return out


def servers(client: str, text: str, user: bool = False) -> dict:
    c = K.CLIENTS[client]
    key = c.user()[1] if user else c.project[1]
    data = read_toml(text) if c.fmt == "toml" else K.JSONText(None, text).data()
    return data.get(key, {})


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A HOME of its own (never the real ~/.cursor, ~/.codex, ~/.gemini ...)."""
    h = tmp_path / "home"
    h.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(h))
    for var in ("CODEX_HOME", "COPILOT_HOME", "UV_CACHE_DIR"):
        monkeypatch.delenv(var, raising=False)
    return h


@pytest.fixture
def proj(tmp_path, home, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    write_config(root, {"project": "proj", "plans": {"quick": {"backend": "local", "steps": [
        {"name": "hello", "run": "python -c \"print('hi')\"", "max_minutes": 1}]}}})
    monkeypatch.chdir(root)
    return root


def run(*clients, user=False, dry_run=False, list_=False, capsys=None):
    code = K.main(list(clients), user=user, dry_run=dry_run, list_=list_)
    out = capsys.readouterr() if capsys else None
    return code, (out.out + out.err) if out else ""


EXISTING = {
    "claude": '{\n  "mcpServers": {\n    "other": {"command": "npx", "args": ["-y", "other"]}\n  },\n  "z": 1\n}\n',
    "cursor": '{\n    "mcpServers": {\n        "other": {"command": "npx"}\n    }\n}\n',
    "vscode": '{\n\t"inputs": [],\n\t"servers": {\n\t\t"other": {"type": "stdio", "command": "npx"}\n\t}\n}\n',
    "codex": '# my settings\nmodel = "o5"  # the model\n\n[mcp_servers.other]\ncommand = "npx"\nargs = ["-y", "other"]\n',
    "gemini": '{\n  "theme": "Dracula",\n  "mcpServers": {"other": {"command": "npx"}}\n}\n',
}
KEEP = {"claude": '"other": {"command": "npx", "args": ["-y", "other"]}', "cursor": '"other": {"command": "npx"}',
        "vscode": '\t\t"other": {"type": "stdio", "command": "npx"}', "codex": EXISTING["codex"],
        "gemini": '"theme": "Dracula"'}


@pytest.mark.parametrize("client", list(K.CLIENTS))
def test_each_client_file_is_merged_backed_up_and_idempotent(client, proj, capsys):
    path = proj / K.CLIENTS[client].project[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXISTING[client])
    code, out = run(client, capsys=capsys)
    assert code == 0, out
    text = path.read_text()
    assert KEEP[client] in text, "what was there stays, in its own formatting"
    got = servers(client, text)
    assert set(got) == {"other", "agent-testbench"}
    ours = got["agent-testbench"]
    assert ours["command"] == os.path.abspath(sys.executable) and ours["args"] == ["-m", "agent_testbench", "mcp"]
    assert ours.get("type") == ("stdio" if client in ("claude", "vscode") else None)
    if client == "codex":
        assert ours["tool_timeout_sec"] == K.CODEX_TOOL_TIMEOUT and text.startswith(EXISTING["codex"])
    if client == "gemini":
        assert K.JSONText(None, text).data()["context"]["fileName"] == ["GEMINI.md", "AGENTS.md"]
    backup = path.with_name(path.name + ".bak")
    assert backup.read_text() == EXISTING[client]
    assert f"the old file is {K.CLIENTS[client].project[0]}.bak" in out
    assert "Not in" in out and "guard hook" in out and "limits.yaml" in out, "it says which guardrails hold"

    os.utime(backup, (1, 1))
    code, again = run(client, capsys=capsys)
    assert code == 0 and path.read_text() == text, "a second run changes nothing"
    assert "already has the agent-testbench server: left as it is" in again
    assert backup.stat().st_mtime == 1 and backup.read_text() == EXISTING[client], "and makes no new backup"


@pytest.mark.parametrize("client", list(K.CLIENTS))
def test_a_new_file_and_the_working_rules(client, proj, capsys):
    code, out = run(client, capsys=capsys)
    assert code == 0, out
    path = proj / K.CLIENTS[client].project[0]
    assert "agent-testbench" in servers(client, path.read_text())
    assert not path.with_name(path.name + ".bak").exists(), "nothing to back up"
    rules = (proj / "AGENTS.md").read_text()
    assert rules.count(K.START) == 1 and rules.count(K.END) == 1
    assert "approved_by_person" in rules and "limits.yaml" in rules and "ledger" in rules
    if client == "claude":
        assert "@AGENTS.md" in (proj / "CLAUDE.md").read_text(), "Claude Code reads AGENTS.md through CLAUDE.md"
    else:
        assert not (proj / "CLAUDE.md").exists()


def test_an_existing_testbench_entry_is_left_as_it_is(proj, capsys):
    path = proj / ".cursor" / "mcp.json"
    path.parent.mkdir()
    before = '{"mcpServers": {"testbench": {"command": "uvx", "args": ["--from", "agent-testbench", "testbench", "mcp"]}}}'
    path.write_text(before)
    code, out = run("cursor", capsys=capsys)
    assert code == 0 and path.read_text() == before
    assert 'already has the agent-testbench server (as "testbench"): left as it is' in out
    assert not path.with_name("mcp.json.bak").exists()

    toml = proj / ".codex" / "config.toml"
    toml.parent.mkdir()
    gone = str(proj.parent / "someone-elses-venv" / "python")
    toml.write_text(f'[mcp_servers.tb]\ncommand = {K.toml_str(gone)}\nargs = ["-m", "agent_testbench", "mcp"]\n')
    code, out = run("codex", capsys=capsys)
    assert code == 0 and 'as "tb"' in out and "agent-testbench" not in servers("codex", toml.read_text())
    assert f"its command {gone} is not on this machine" in out, "a file written elsewhere is pointed out"
    assert re.search(r"codex .*broken \(\.codex/config\.toml", run(list_=True, capsys=capsys)[1])


@pytest.mark.parametrize("client", list(K.CLIENTS))
def test_user_writes_under_home_only(client, proj, home, capsys):
    code, out = run(client, user=True, capsys=capsys)
    assert code == 0, out
    path, _ = K.CLIENTS[client].user()
    assert home in path.parents
    assert "agent-testbench" in servers(client, path.read_text(), user=True)
    written = {p.relative_to(proj).as_posix() for p in proj.rglob("*")}
    assert written <= {"testbench.yaml", "AGENTS.md", "CLAUDE.md"}, "no project config file with --user"
    assert path.parent == home or path.parent.parent == home


def test_user_follows_codex_home_and_copilot_home(proj, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot-home"))
    assert run("codex", "vscode", user=True, capsys=capsys)[0] == 0
    assert "agent-testbench" in servers("codex", (tmp_path / "codex-home" / "config.toml").read_text())
    data = json.loads((tmp_path / "copilot-home" / "mcp-config.json").read_text())
    assert data["mcpServers"]["agent-testbench"]["args"][-1] == "mcp" and "type" not in data["mcpServers"]["agent-testbench"]


def test_dry_run_writes_nothing(proj, home, capsys):
    (proj / ".gemini").mkdir()
    (proj / ".gemini" / "settings.json").write_text('{"theme": "x"}\n')
    before = sorted(p.as_posix() for p in (*proj.rglob("*"), *home.rglob("*")))
    code, out = run("gemini", "codex", dry_run=True, capsys=capsys)
    assert code == 0
    assert sorted(p.as_posix() for p in (*proj.rglob("*"), *home.rglob("*"))) == before
    assert (proj / ".gemini" / "settings.json").read_text() == '{"theme": "x"}\n'
    assert "+++ .codex/config.toml" in out and "+[mcp_servers.agent-testbench]" in out
    assert "would change .gemini/settings.json" in out and "Dry run: nothing was written." in out


def test_agents_md_section_is_replaced_not_duplicated(proj, capsys):
    mine = "# My project\n\nRules of my own.\n\n"
    legacy = "<!-- agent-testbench -->\n## Working here (agent-testbench)\nold rules\n<!-- /agent-testbench -->\n"
    (proj / "AGENTS.md").write_text(mine + legacy + "\n## After\nmore of mine\n")
    assert run("codex", capsys=capsys)[0] == 0
    text = (proj / "AGENTS.md").read_text()
    assert text.startswith(mine) and text.endswith("## After\nmore of mine\n")
    assert "old rules" not in text and "<!-- /agent-testbench -->" not in text, "init's section is replaced too"
    assert text.count(K.START) == 1

    edited = text.replace("Done means", "Finished means")
    (proj / "AGENTS.md").write_text(edited)
    assert run("cursor", "gemini", capsys=capsys)[0] == 0
    again = (proj / "AGENTS.md").read_text()
    assert again == text, "rerunning puts the section back as it was and touches nothing else"
    assert run("vscode", capsys=capsys)[0] == 0
    assert (proj / "AGENTS.md").read_text() == text


def test_init_after_connect_does_not_add_a_second_section(tmp_path, home):
    root = tmp_path / "fresh"
    root.mkdir()
    env = {"HOME": str(home), "USERPROFILE": str(home)}
    r = cli(root, "connect", "cursor", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "wrote .cursor/mcp.json" in r.stdout and "no testbench.yaml yet" in r.stdout
    assert cli(root, "init", env=env).returncode == 0
    text = (root / "AGENTS.md").read_text()
    assert text.count(K.START) == 1 and K.LEGACY[0] not in text, "init sees connect's section and adds none"


def test_claude_md_that_imports_agents_md_already_is_left(proj, capsys):
    (proj / "CLAUDE.md").write_text("Read @AGENTS.md first.\n")
    assert run("claude", capsys=capsys)[0] == 0
    assert (proj / "CLAUDE.md").read_text() == "Read @AGENTS.md first.\n"
    (proj / "CLAUDE.md").write_text("See AGENTS.md for how to work in this project (agent-testbench).\n")
    assert run("claude", capsys=capsys)[0] == 0
    text = (proj / "CLAUDE.md").read_text()
    assert text.startswith("See AGENTS.md") and f"{K.START}\n@AGENTS.md\n{K.END}\n" in text
    assert run("claude", capsys=capsys)[0] == 0 and (proj / "CLAUDE.md").read_text() == text


def test_gemini_keeps_the_context_files_it_reads(proj, capsys):
    path = proj / ".gemini" / "settings.json"
    path.parent.mkdir()
    path.write_text('{\n  "context": {"fileName": "CONTEXT.md"}\n}\n')
    assert run("gemini", capsys=capsys)[0] == 0
    assert K.JSONText(None, path.read_text()).data()["context"]["fileName"] == ["CONTEXT.md", "AGENTS.md"]


def test_vscode_jsonc_comments_and_trailing_commas_survive(proj, capsys):
    path = proj / ".vscode" / "mcp.json"
    path.parent.mkdir()
    before = ('// servers for this repo\n{\n  "servers": {\n    "other": {"type": "stdio", "command": "npx"}, '
              '// keep me\n  },\n  /* prompts */ "inputs": [],\n}\n')
    path.write_text(before)
    assert run("vscode", capsys=capsys)[0] == 0
    text = path.read_text()
    for kept in ("// servers for this repo", "// keep me", "/* prompts */ \"inputs\": [],"):
        assert kept in text
    assert set(K.JSONText(None, text).data()["servers"]) == {"other", "agent-testbench"}


def test_crlf_files_stay_crlf(proj, capsys):
    path = proj / ".cursor" / "mcp.json"
    path.parent.mkdir()
    path.write_bytes(b'{\r\n  "mcpServers": {}\r\n}\r\n')
    (proj / "AGENTS.md").write_bytes(b"# Mine\r\n")
    assert run("cursor", capsys=capsys)[0] == 0
    for f in (path, proj / "AGENTS.md"):
        raw = f.read_bytes()
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), f


def test_a_file_it_cannot_read_is_not_touched(proj, capsys):
    path = proj / ".cursor" / "mcp.json"
    path.parent.mkdir()
    path.write_text('{"mcpServers": {"a": 1,')
    code, out = run("cursor", capsys=capsys)
    assert code == 2 and "Nothing was changed" in out and '"agent-testbench": {' in out, "it says what to add by hand"
    assert path.read_text() == '{"mcpServers": {"a": 1,' and not (proj / ".cursor" / "mcp.json.bak").exists()
    assert not (proj / "AGENTS.md").exists()

    toml = proj / ".codex" / "config.toml"
    toml.parent.mkdir()
    toml.write_text('mcp_servers = { other = { command = "npx" } }\n')
    code, out = run("codex", capsys=capsys)
    assert code == 2 and "inline table" in out and "[mcp_servers.agent-testbench]" in out


def test_toml_strings_are_quoted_right():
    weird = 'C:\\Users\\Ada "A" Lovelace\\venv\\Scripts\\python.exe'
    out, _ = K.merge_toml(None, "", "mcp_servers", {"command": weird, "args": ["-m", "x\ty"], "tool_timeout_sec": 900})
    got = read_toml(out)["mcp_servers"]["agent-testbench"]
    assert got == {"command": weird, "args": ["-m", "x\ty"], "tool_timeout_sec": 900}
    assert K.toml_str("a\x7fb\x01") == '"a\\u007Fb\\u0001"'


def test_a_throwaway_environment_registers_uvx(monkeypatch):
    cmd, args = K.server_command("/home/ada/.cache/uv/archive-v0/Xy12/bin/python")
    assert cmd == "uvx" and args[:2] == ["--from", f"agent-testbench[all]=={K.__version__}"] and args[-1] == "mcp"
    venv = os.path.abspath("/home/ada/.venv/bin/python")      # D:\home\... on Windows
    assert K.server_command("/home/ada/.venv/bin/python") == (venv, ["-m", "agent_testbench", "mcp"])


def test_list_shows_each_client(proj, home, tmp_path, monkeypatch, capsys):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name in ("codex", "codex.bat"):
        (bin_ / name).write_text("@echo off\n" if name.endswith(".bat") else "#!/bin/sh\n")
        (bin_ / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_))
    assert run("cursor", capsys=capsys)[0] == 0
    assert run("gemini", user=True, capsys=capsys)[0] == 0
    code, out = run(list_=True, capsys=capsys)
    assert code == 0
    rows = {line.split()[0]: line for line in out.splitlines() if line.split() and line.split()[0] in K.CLIENTS}
    assert set(rows) == set(K.CLIENTS)
    assert re.search(r"codex\s+yes \(codex\)\s+no \(\.codex/config\.toml\)\s+no \(~/\.codex/config\.toml\)", rows["codex"])
    assert re.search(r"cursor\s+.*yes \(\.cursor/mcp\.json\)\s+no \(~/\.cursor/mcp\.json\)", rows["cursor"])
    assert re.search(r"gemini\s+yes \(~/\.gemini\)\s+no \(\.gemini/settings\.json\)\s+yes \(~/\.gemini/settings\.json\)",
                     rows["gemini"])
    assert "Not supported: windsurf" in out
    assert run(capsys=capsys)[1] == out, "no client at all shows the same list"


def test_the_cli_takes_the_command(proj, home):
    env = {"HOME": str(home), "USERPROFILE": str(home)}
    r = cli(proj, "connect", "nosuch", env=env)
    assert r.returncode == 2 and "unknown client nosuch" in r.stderr
    r = cli(proj, "connect", "windsurf", env=env)
    assert r.returncode == 2 and "not supported" in r.stderr
    r = cli(proj, "connect", "--list", env=env)
    assert r.returncode == 0 and "vscode" in r.stdout


def _initialize(argv, cwd, timeout=90) -> dict:
    """Start argv as an MCP stdio server and send initialize; its reply."""
    proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True)
    reply = {}

    def read():
        line = proc.stdout.readline()
        reply.update(json.loads(line) if line.strip() else {})

    try:
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}}) + "\n")
        proc.stdin.flush()
        t = threading.Thread(target=read, daemon=True)
        t.start()
        t.join(timeout)
        return reply
    finally:
        proc.kill()
        proc.wait(30)


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="the mcp package is not installed")
@pytest.mark.parametrize("client", list(K.CLIENTS))
def test_the_registered_command_starts_the_server(client, proj, capsys):
    # find_spec, not importorskip: importing mcp here, under capsys, would bind its stdio_client's default errlog to
    # the capture object for the rest of the session, and test_mcp.py would then fail
    assert run(client, capsys=capsys)[0] == 0
    entry = servers(client, (proj / K.CLIENTS[client].project[0]).read_text())["agent-testbench"]
    reply = _initialize([entry["command"], *entry["args"]], cwd=proj)
    assert reply.get("result", {}).get("serverInfo", {}).get("name") == "agent-testbench", reply
