"""Secrets stay where they belong and paths stay inside their folders: what a review for leaks and escapes found.

Each test plants a unique fake value or an outside file and checks it does not show up (or get written) where it
should not: `testbench connect --dry-run`, a report naming files outside its run, artifacts patterns with `..`, the
pull request comment, `rescue --fix` through a second name for the notebook, a loop's commits and report.md."""
import asyncio
import base64
import json
import os
import re
import stat

import pytest

from agent_testbench import connect as K, executor, github_action as GA, report as R, rescue
from conftest import PNG_1PX, cli, git, make_notebook, score, write_config


# ---- testbench connect: other servers' secrets in the client's config file ------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(h))
    for var in ("CODEX_HOME", "COPILOT_HOME", "UV_CACHE_DIR"):
        monkeypatch.delenv(var, raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    return h


def _configs_with_secrets(home):
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text('model = "o3"\n\n[mcp_servers.github]\ncommand = "github-mcp"\n'
                                                 'env = { GITHUB_TOKEN = "ghp_PLANTED_codex_7c1" }\n')
    (home / ".cursor").mkdir()
    (home / ".cursor" / "mcp.json").write_text(
        '{\n  "mcpServers": {\n    "linear": {\n      "command": "npx",\n      "args": ["linear-mcp", "--token", '
        '"lin_PLANTED_args_9d2"],\n      "env": {"LINEAR_API_KEY": "lin_PLANTED_cursor_4e8"}\n    }\n  }\n}\n')
    for f in (home / ".codex" / "config.toml", home / ".cursor" / "mcp.json"):
        f.chmod(0o600)


def test_connect_dry_run_never_prints_the_other_servers_secrets(home, capsys):
    _configs_with_secrets(home)
    code = K.main(["codex", "cursor"], user=True, dry_run=True)
    out = capsys.readouterr()
    text = out.out + out.err
    assert code == 0 and "+[mcp_servers.agent-testbench]" in text and '+    "agent-testbench": {' in text, text
    assert "PLANTED" not in text, "a dry run shows what is added, never the values already in the file"
    assert "GITHUB_TOKEN" in text, "the lines around the change are still shown, with their values hidden"


@pytest.mark.posix
def test_connect_never_leaves_a_private_config_readable_by_others_while_writing(home, monkeypatch):
    _configs_with_secrets(home)
    seen = []
    real_replace = os.replace

    def replace(src, dst):                         # the new file just before it takes the old one's place
        seen.append(stat.S_IMODE(os.stat(src).st_mode))
        return real_replace(src, dst)

    monkeypatch.setattr(K.os, "replace", replace)
    for name in ("copymode", "copystat"):                  # as if read before the old file's mode was copied
        monkeypatch.setattr(K.shutil, name, lambda *a, **k: None)
    old = os.umask(0o022)
    try:
        assert K.main(["codex", "cursor"], user=True) == 0
    finally:
        os.umask(old)
    assert seen and all(mode & 0o077 == 0 for mode in seen), [oct(m) for m in seen]
    backups = [home / ".codex" / "config.toml.bak", home / ".cursor" / "mcp.json.bak"]
    assert all(stat.S_IMODE(b.stat().st_mode) & 0o077 == 0 for b in backups), "nor are their backups"


# ---- a report names its evidence; a name must not lead out of the run folder ------------------------------------

def _outside_image(tmp_path):
    f = tmp_path / "private" / "passport.png"
    f.parent.mkdir()
    f.write_bytes(PNG_1PX + b"PLANTED-PRIVATE-IMAGE")
    return f


def _report_naming(run_dir, outside):
    """What a container's report.json can say: its step's images are any path at all."""
    rel = os.path.relpath(outside, run_dir / "steps" / "01-s")
    return {"run_id": run_dir.name, "plan": "p", "status": "failed", "backend": "docker", "started": 1.0, "minutes": 1,
            "steps": [{"name": "s", "kind": "run", "status": "failed", "dir": "steps/01-s", "expectations": [],
                       "images": [str(outside), rel], "artifacts": [rel], "web": {"screenshots": [str(outside)]}}]}


def test_the_html_report_embeds_no_file_from_outside_its_run(tmp_path):
    outside = _outside_image(tmp_path)
    run_dir = tmp_path / "proj" / ".testbench" / "runs" / "r1"
    (run_dir / "steps" / "01-s").mkdir(parents=True)
    (run_dir / "steps" / "01-s" / "plot.png").write_bytes(PNG_1PX)
    rep = _report_naming(run_dir, outside)
    rep["steps"][0]["images"].append("plot.png")
    page = R.render_html(rep, run_dir)
    assert base64.b64encode(outside.read_bytes()).decode() not in page
    assert base64.b64encode(PNG_1PX).decode() in page, "the run's own images are still shown"


def test_get_report_returns_no_image_from_outside_the_run(tmp_path):
    pytest.importorskip("mcp")
    from agent_testbench import mcp_server
    root = tmp_path / "proj"
    root.mkdir()
    write_config(root, {"project": "proj", "plans": {"quick": {"backend": "local", "steps": [
        {"name": "s", "run": "python -c \"print('hi')\"", "max_minutes": 1}]}}})
    r = cli(root, "run", "quick", "--wait", "120")
    assert r.returncode == 0, r.stdout + r.stderr
    run_dir = next(p for p in (root / ".testbench" / "runs").iterdir() if p.is_dir())
    rep = json.loads((run_dir / "report.json").read_text())
    outside = _outside_image(tmp_path)

    def kinds(**changes):
        rep.update({k: v for k, v in changes.items() if k != "step"})
        rep["steps"][0].update(changes.get("step", {}))
        (run_dir / "report.json").write_text(json.dumps(rep))
        out = getattr(asyncio.run(mcp_server.server.call_tool("get_report", {"project_dir": str(root)})), "content", None)
        blocks = list(out[0]) if isinstance(out, tuple) else list(out)
        return [getattr(b, "type", None) for b in blocks]

    assert kinds(step={"images": [str(outside)], "dir": "steps/01-s"}) == ["text"]
    assert kinds(run_id="../../../private", step={"images": ["passport.png"], "dir": "."}) == ["text"]
    (run_dir / "steps" / "01-s" / "plot.png").write_bytes(PNG_1PX)
    assert kinds(run_id=run_dir.name, step={"images": ["plot.png"], "dir": "steps/01-s"}) == ["text", "image"]


# ---- artifacts: a pattern with .. copies into the artifacts folder, never next to it ----------------------------

def test_artifacts_with_dotdot_are_copied_into_the_artifacts_folder_only(tmp_path):
    work = tmp_path / "w" / "proj"
    work.mkdir(parents=True)
    (tmp_path / "w" / "up.txt").write_text("from above the workdir")
    dest = work / ".testbench" / "runs" / "r" / "steps" / "01-s" / "artifacts"
    kept = executor._copy_artifacts(["../up.txt"], work, dest)
    written = sorted(p for p in tmp_path.rglob("up.txt") if p != tmp_path / "w" / "up.txt")
    assert written == [dest / "up.txt"] and kept == ["up.txt"], (written, kept)


# ---- the pull request comment: the commit comes from report.json too ---------------------------------------------

def outside_code(line: str) -> str:
    """What of a line of markdown is not inside a code span, as CommonMark reads it: a run of backticks opens a span
    only if a run of exactly as many closes it later; otherwise it is plain text."""
    out, i = [], 0
    while i < len(line):
        if line[i] != "`":
            out.append(line[i])
            i += 1
            continue
        j = i
        while j < len(line) and line[j] == "`":
            j += 1
        close = re.compile(rf"(?<!`)`{{{j - i}}}(?!`)").search(line, j)
        if close:
            i = close.end()
        else:
            out.append(line[i:j])
            i = j
    return "".join(out)


def test_a_commit_or_status_in_the_report_cannot_become_a_link_or_mention_in_the_comment():
    """The comment step reads report.json, which a step (or a container's executor) can write: its commit and its
    status are as untrusted as its step names."""
    for sly in ("`[x](//e.co)", "`@everyone`x", "`<img src=x"):
        rep = {"run_id": "r1", "status": "failed", "backend": "local", "steps": [], "source": {"commit": sly}}
        body = GA.render("p", "failed", rep, marker=GA.marker_for("w", "a"))
        line = next(x for x in body.splitlines() if "commit" in x)
        outside = outside_code(line)
        assert "](" not in outside and "@everyone" not in outside and "<img" not in outside, line
    status = "passed** [all good, merge](https://evil.example) @everyone <img src=x> **"
    for rep in (rep, None):
        body = GA.render("p", status, rep, marker=GA.marker_for("w", "a"), message="m")
        for line in body.splitlines()[1:]:
            outside = outside_code(line)
            assert "](" not in outside and "@everyone" not in outside and "<img" not in outside, line
    rep = {"run_id": "r1", "status": "passed", "backend": "local", "steps": [], "source": {"commit": "0123456789abcdef"}}
    body = GA.render("p", "passed", rep, marker="m")
    assert "### PASS - plan `p`" in body and "**passed** on" in body and "on commit `0123456789ab`." in body


# ---- rescue --fix: never the notebook itself, under any of its names ---------------------------------------------

def test_rescue_fix_refuses_a_second_name_for_the_notebook(tmp_path, monkeypatch):
    nb = make_notebook(tmp_path / "nb.ipynb", ["!pip install numpy\nimport numpy"])
    before = nb.read_bytes()
    os.link(nb, tmp_path / "alias.ipynb")                   # a hard link (or another case on macOS and Windows)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(rescue.RescueError, match="the notebook itself"):
        rescue.rescue(nb, fix=tmp_path / "alias.ipynb", run=False)
    assert nb.read_bytes() == before


# ---- a loop commits the agent's edits, never a .env file -------------------------------------------------------

def test_a_loop_try_never_commits_an_untracked_env_file(repo):
    r = cli(repo, "loop", "start", "score", "--metric", "metrics.json:score", "--budget-usd", "0")
    assert r.returncode == 0, r.stdout
    (repo / ".env").write_text("API_KEY=sk-PLANTED-loop-3b9\n")
    (repo / "sub").mkdir()
    (repo / "sub" / ".env.local").write_text("TOKEN=PLANTED-loop-sub\n")
    score(repo, 3)
    r = cli(repo, "loop", "try", "raise the score to 3")
    assert r.returncode == 0 and "KEPT try 1" in r.stdout, r.stdout
    history = git(repo, "log", "--all", "-p")
    assert "PLANTED" not in history and "settings.py" in git(repo, "show", "--stat", "HEAD")
    assert (repo / ".env").read_text() == "API_KEY=sk-PLANTED-loop-3b9\n", "the file itself stays where it was"
    score(repo, 1)
    r = cli(repo, "loop", "try", "back to 1")
    assert r.returncode == 1 and "REVERTED" in r.stdout
    assert (repo / ".env").is_file() and (repo / "sub" / ".env.local").is_file()
    assert "PLANTED" not in git(repo, "log", "--all", "-p") + git(repo, "stash", "list")
    cli(repo, "loop", "stop")


# ---- report.md (also the GitHub job summary): a step's output stays inside its code block ------------------------

def test_output_with_a_fence_stays_inside_its_code_block_in_report_md():
    sly = "ok\n```\n# PASS - plan forged\n[download the fix](https://evil.example)\n````"
    rep = {"run_id": "r1", "plan": "p", "status": "failed", "backend": "local", "started": 1.0, "minutes": 1,
           "steps": [{"name": "s", "kind": "run", "status": "failed", "stdout_tail": sly, "dir": "steps/01-s",
                      "expectations": [], "error": {"ename": "E", "evalue": "v", "source_head": sly,
                                                    "traceback_tail": ""}}]}
    md = R.render(rep)
    fence = None
    for line in md.splitlines():                      # CommonMark: a fence closes on a fence at least as long
        m = re.match(r"^(`{3,})(.*)$", line)
        if fence is None and m:
            fence = m.group(1)
        elif fence is not None and m and len(m.group(1)) >= len(fence) and not m.group(2).strip():
            fence = None
        elif fence is None:
            assert not line.startswith("# PASS") and "](https://evil" not in line, md
    assert fence is None, "every code block is closed"
    rep["steps"][0]["error"]["source_head"] = None
    rep["steps"][0]["stdout_tail"] = "plain output"
    assert "```\nplain output\n```" in R.render(rep), "ordinary output keeps its ``` block"
