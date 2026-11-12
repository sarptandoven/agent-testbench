"""testbench share: one run's proof as a static page others can open and check. Every file is listed with its size
and why before anything goes out; state, plans, .env files, secret and env: values and this machine's paths never
do; a folder, a git branch (committed, never pushed, the working tree untouched) or S3 through the aws CLI; and
`testbench verify --from` checks the bundle with no project, catching a file edited after it was published."""
import base64
import functools
import http.server
import json
import os
import subprocess
import sys
import tarfile
import threading
from pathlib import Path

import pytest

from agent_testbench import config as C, share as S
from conftest import PNG_1PX, REPO, cli, write_config

SECRET = "s3cr3t-value-never-shared-81f2"
ENV_VALUE = "env-value-never-shared-42aa"
MAKE = """\
import base64, json, pathlib
out = pathlib.Path("out")
out.mkdir(exist_ok=True)
(out / "plot.png").write_bytes(base64.b64decode({png!r}))
json.dump({{"n": 3, "loss": 0.25}}, open(out / "m.json", "w"))
(out / ".env").write_text("API_TOKEN=caught-by-an-artifact-glob\\n")
print("made the plot")
"""


@pytest.fixture
def proj(tmp_path) -> Path:
    """A finished run whose evidence holds an image, a file its checks read, a .env an artifact glob caught, and logs
    holding a secret's value, an env: value and the project folder's path."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "make.py").write_text(MAKE.format(png=base64.b64encode(PNG_1PX).decode()))
    py = f'"{sys.executable}"'
    write_config(root, {"project": "proj", "plans": {"p": {"steps": [
        {"name": "make", "run": f"{py} make.py", "max_minutes": 2, "artifacts": ["out/*", "out/.env"],
         "expect": {"json": {"out/m.json": {"n": ">= 3", "loss": "< 0.5"}}}},
        {"name": "env", "run": f"{py} -c \"import os; print(os.environ['SETTING'])\"", "max_minutes": 1,
         "env": {"SETTING": ENV_VALUE}},
        {"name": "secret", "run": f"{py} -c \"import os; print('token', os.environ['TB_SHARE_SECRET'])\"",
         "max_minutes": 1, "secrets": ["TB_SHARE_SECRET"]},
        {"name": "where", "run": f"{py} -c \"import os; print(os.getcwd())\"", "max_minutes": 1}]}}})
    r = cli(root, "run", "p", "--wait", "120", env={"TB_SHARE_SECRET": SECRET})
    assert r.returncode == 0, r.stdout + r.stderr
    return root


def share(root, *args, env=None):
    return subprocess.run([sys.executable, "-m", "agent_testbench", "share", *args], cwd=root, capture_output=True,
                          text=True, timeout=300, stdin=subprocess.DEVNULL,
                          env={**os.environ, "TB_SHARE_SECRET": SECRET, **(env or {})})


def run_id(root) -> str:
    return next((root / ".testbench" / "runs").iterdir()).name


def bundle_text(folder: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in folder.rglob("*") if p.is_file())


def never_leaks(text: str, root: Path) -> None:
    for value in (SECRET, ENV_VALUE, "caught-by-an-artifact-glob", str(root.resolve())):
        assert value not in text, f"{value!r} was published"


def test_dry_run_lists_every_file_and_why_and_publishes_nothing(proj, tmp_path):
    r = share(proj, "--to", str(tmp_path / "public"), "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    out, run = r.stdout, run_id(proj)
    assert f"Run {run} (plan p: 6 checks passed) to {tmp_path / 'public' / run}" in out
    for path, why in (("index.html", "the report as one page"), ("report.json", "the report as data"),
                      ("badge.svg", "a README badge: testbench: 6 checks passed"),
                      ("steps/01-make/artifacts/out/m.json", "a file the step kept, which its checks read"),
                      ("steps/01-make/artifacts/out/plot.png", "an image the step kept"),
                      ("steps/01-make/log.txt", "the step's output"),
                      (S.MANIFEST, "the sha256 of every file here")):
        line = next((x for x in out.splitlines() if x.strip().startswith(path + " ")), "")
        assert why in line and (" B " in line or " KB " in line), (path, out)
    assert "in all;" in out and "Left out:" in out
    left = out.split("Left out:")[1].split("Never published:")[0]
    assert "out/.env" in left and "never published" in left
    assert "steps/02-env/log.txt" in left and "holds the value of env SETTING (never published)" in left
    published = out.split("Left out:")[0]                # the run already put [secret] where the value was printed
    assert "steps/03-secret/log.txt" in published and "steps/03-secret/log.txt" not in left, out
    assert "steps/04-where/log.txt" in left and "holds the project folder (--keep-paths" in left
    assert f"the project folder {proj.resolve()} -> <project>" in out, "what was scrubbed, with what it was"
    assert SECRET not in out and ENV_VALUE not in out, "a value is named, never shown"
    assert "Nothing was published (--dry-run)" in out and not (tmp_path / "public").exists()


def test_a_folder_gets_the_bundle_and_verify_from_checks_it_anywhere(proj, tmp_path):
    public = tmp_path / "public"
    r = share(proj, "--to", str(public))
    assert r.returncode == 2 and "takes a person's yes" in r.stdout and not public.exists(), \
        "not in a terminal and no --yes: nothing goes out"
    r = share(proj, "--to", str(public), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    run = run_id(proj)
    folder = public / run
    assert f"Check it: testbench verify --from {folder}" in r.stdout
    names = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}
    assert {"index.html", "report.json", "badge.svg", S.MANIFEST, "steps/01-make/log.txt",
            "steps/01-make/artifacts/out/plot.png", "steps/01-make/artifacts/out/m.json"} <= names
    assert not any(n.endswith((".env", "state.json", "plan.json", "ledger.jsonl", "NOTES.md")) for n in names)
    assert not {"steps/02-env/log.txt", "steps/04-where/log.txt"} & names
    assert "token [secret]" in (folder / "steps" / "03-secret" / "log.txt").read_text(), "scrubbed when the run ended"
    never_leaks(bundle_text(folder), proj)
    page = (folder / "index.html").read_text()
    assert "src='steps/01-make/artifacts/out/plot.png'" in page and "data:image" not in page, "images are linked"
    assert "steps/02-env/log.txt <span class=muted>(not published)</span>" in page
    assert "<img src='badge.svg' alt='testbench: 6 checks passed'" in page
    rep = json.loads((folder / "report.json").read_text())
    assert rep["evidence"] == json.loads((proj / ".testbench" / "runs" / run / "report.json").read_text())["evidence"]
    assert "<project>" in json.dumps(rep) and SECRET not in json.dumps(rep)

    elsewhere = tmp_path / "elsewhere"                                   # no testbench.yaml here or above
    elsewhere.mkdir()
    r = cli(elsewhere, "verify", "--from", str(folder))
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"run {run} (plan p, passed): all" in r.stdout and "files the manifest lists match it" in r.stdout
    assert "were published, each matching the sha256 the run recorded" in r.stdout and "left out when" in r.stdout
    assert "compare that evidence_sha256 with the one the run printed" in r.stdout
    assert cli(elsewhere, "verify", "--from", str(folder / "index.html")).returncode == 0, "the page names its folder"
    r = cli(elsewhere, "verify", "--from", str(public))
    assert r.returncode == 2 and f"name one: testbench verify --from {folder}" in r.stdout

    m = folder / "steps" / "01-make" / "artifacts" / "out" / "m.json"
    m.write_text('{"n": 9, "loss": 0.01}')                                  # a better number, after the fact
    r = cli(elsewhere, "verify", "--from", str(folder))
    assert r.returncode == 1 and "changed: steps/01-make/artifacts/out/m.json" in r.stdout
    manifest = json.loads((folder / S.MANIFEST).read_text())                # and the manifest made to agree
    for f in manifest["files"]:
        if f["path"].endswith("m.json"):
            f["bytes"], f["sha256"] = m.stat().st_size, S._sha256(m.read_bytes())
    (folder / S.MANIFEST).write_text(json.dumps(manifest))
    r = cli(elsewhere, "verify", "--from", str(folder))
    assert r.returncode == 1 and "its sha256 is not the one the run recorded" in r.stdout
    (folder / "extra.html").write_text("<p>trust me</p>")
    assert "not in the manifest: extra.html" in cli(elsewhere, "verify", "--from", str(folder)).stdout

    r = share(proj, "--to", str(public), "--yes")
    assert r.returncode == 2 and "does not list (extra.html" in r.stdout, "files it did not make are never replaced"
    (folder / "extra.html").unlink()
    assert share(proj, "--to", str(public), "--yes").returncode == 0, "a share made before is replaced"
    assert cli(elsewhere, "verify", "--from", str(folder)).returncode == 0


def test_keep_paths_publishes_evidence_with_paths_as_it_is(proj, tmp_path):
    r = share(proj, "--to", str(tmp_path / "public"), "--yes", "--keep-paths")
    assert r.returncode == 0, r.stdout + r.stderr
    folder = tmp_path / "public" / run_id(proj)
    assert (folder / "steps" / "04-where" / "log.txt").read_text().strip() == str(proj.resolve()), \
        "byte for byte: its sha256 is the proof"
    secret_log = folder / "steps" / "03-secret" / "log.txt"
    assert not secret_log.exists() or SECRET not in secret_log.read_text(), "a secret's value never, whatever the flag"
    assert "<project>" in (folder / "report.json").read_text(), "the report is still scrubbed"
    assert str(proj.resolve()) not in (folder / "index.html").read_text() + (folder / "report.json").read_text()
    assert cli(tmp_path, "verify", "--from", str(folder)).returncode == 0


@pytest.mark.parametrize("value", [None, ""])
def test_a_secret_not_set_where_it_is_shared_keeps_its_step_output_out(proj, tmp_path, value):
    env = {k: v for k, v in os.environ.items() if k != "TB_SHARE_SECRET"}
    if value is not None:
        env["TB_SHARE_SECRET"] = value
    r = subprocess.run([sys.executable, "-m", "agent_testbench", "share", "--to", str(tmp_path / "public"), "--yes"],
                       cwd=proj, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "steps/03-secret/log.txt" in r.stdout and "uses TB_SHARE_SECRET, which is not set here" in r.stdout
    folder = tmp_path / "public" / run_id(proj)
    text = bundle_text(folder)
    assert SECRET not in text and "which was not set where it was shared" in text
    assert (folder / "steps" / "01-make" / "log.txt").is_file(), "the other steps' evidence still goes"
    assert cli(tmp_path, "verify", "--from", str(folder)).returncode == 0


def test_a_run_whose_evidence_changed_is_not_shared(proj, tmp_path):
    log = proj / ".testbench" / "runs" / run_id(proj) / "steps" / "01-make" / "log.txt"
    log.write_text("all good, trust me\n")
    r = share(proj, "--to", str(tmp_path / "public"), "--dry-run")
    assert r.returncode == 1 and "changed: steps/01-make/log.txt" in r.stdout and "proves nothing" in r.stdout


def test_a_person_answers_in_a_terminal(proj, tmp_path, monkeypatch):
    monkeypatch.setenv("TB_SHARE_SECRET", SECRET)
    cfg, public, said, asked = C.load(proj), tmp_path / "public", [], []
    ask = lambda answer: lambda q: asked.append(q) or answer                     # noqa: E731
    assert S.share(proj, cfg, None, str(public), ask=ask("n"), tty=True, say=said.append) == 2
    assert "Anyone who can read it sees them. [y/N]" in asked[0] and said[-1] == "Nothing was published."
    assert not public.exists()
    assert S.share(proj, cfg, None, str(public), ask=ask("y"), tty=True, say=said.append) == 0
    assert (public / run_id(proj) / "index.html").is_file() and len(asked) == 2
    with pytest.raises(S.ShareError, match="inside .testbench"):
        S.share(proj, cfg, None, str(proj / ".testbench" / "public"), yes=True, say=said.append)


def test_the_scrub_covers_home_and_user_name(tmp_path, monkeypatch):
    home = tmp_path / "home" / "ada-lovelace"
    root = home / "work" / "proj"
    root.mkdir(parents=True)
    monkeypatch.setattr(S, "_home", lambda: home)
    monkeypatch.setattr(S, "_user", lambda: "ada-lovelace")
    s = S.Scrub(root, {"secret K": "k-value-123456"})
    counts = __import__("collections").Counter()
    text = (f"{root}/train.py line 3; {home}/.venv/bin/python; /home/ada-lovelace/other; {root}2/x; "
            f"key k-value-123456; " + json.dumps(str(root / 'a.csv')))
    out = s.text(text, counts)
    assert out.startswith("<project>/train.py line 3; ~/.venv/bin/python; /home/<user>/other; ")
    assert f"~{os.sep}work{os.sep}proj2/x" in out, "a longer name is not the project"   # the rest keeps its separators
    assert "<K>" in out and "k-value-123456" not in out and '"<project>/a.csv"' in out
    assert {label for label, _, _ in counts} == {"the project folder", "the home folder", "the user name",
                                                 "the value of secret K"}
    found = s.found(f"{root}/x and k-value-123456".encode())
    assert found["path"] == {"the project folder": 1} and found["value"] == {"the value of secret K": 1}
    assert S.never("steps/01-x/artifacts/.env.local") and S.never("steps/01-x/artifacts/keys/id_ed25519")
    assert S.never("steps/01-x/artifacts/state.json") and S.never("steps/01-x/artifacts/.git/config")
    assert not S.never("steps/01-x/artifacts/out/m.json")
    assert not S.safe_path("../x") and not S.safe_path("/etc/passwd") and not S.safe_path("a/.hidden")
    assert not S.safe_path("a\\b") and not S.safe_path("C:/x") and S.safe_path("steps/01-x/log.txt")


def git(root, *args) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, capture_output=True,
                          text=True, check=True).stdout


def test_a_git_branch_gets_a_commit_and_nothing_else_changes(proj, tmp_path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    (proj / ".gitignore").write_text(".testbench/\n")
    git(proj, "init", "-q")
    git(proj, "add", "-A")
    git(proj, "commit", "-qm", "start")
    git(proj, "remote", "add", "origin", str(origin))
    git(proj, "push", "-q", "origin", "HEAD")
    (proj / "make.py").write_text((proj / "make.py").read_text() + "# an edit not committed\n")
    git(proj, "add", "make.py")
    (proj / "untracked.txt").write_text("x\n")
    before = {k: git(proj, *a) for k, a in {"status": ["status", "--porcelain"], "head": ["rev-parse", "HEAD"],
                                            "branch": ["symbolic-ref", "HEAD"], "index": ["diff", "--cached"],
                                            "remote": ["ls-remote", str(origin)]}.items()}
    branch = git(proj, "symbolic-ref", "--short", "HEAD").strip()

    r = share(proj, "--to", f"git:{branch}/runs", "--yes")
    assert r.returncode == 2 and "is checked out in" in r.stdout, "never the branch someone has checked out"
    r = share(proj, "--to", "git:gh-pages/runs", "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    run = run_id(proj)
    assert f"Committed {run} to branch gh-pages, folder runs/{run}/" in r.stdout
    assert "Nothing was pushed. To publish it, a person runs: git push origin gh-pages" in r.stdout
    after = {k: git(proj, *a) for k, a in {"status": ["status", "--porcelain"], "head": ["rev-parse", "HEAD"],
                                           "branch": ["symbolic-ref", "HEAD"], "index": ["diff", "--cached"],
                                           "remote": ["ls-remote", str(origin)]}.items()}
    assert after == before, "the working tree, the index, the current branch and the remote are as they were"
    files = git(proj, "ls-tree", "-r", "--name-only", "gh-pages").split()
    assert ".nojekyll" in files and f"runs/{run}/index.html" in files and f"runs/{run}/{S.MANIFEST}" in files
    message = git(proj, "log", "-1", "--format=%B", "gh-pages")
    rep = json.loads((proj / ".testbench" / "runs" / run / "report.json").read_text())
    assert f"evidence_sha256 {rep['evidence_sha256']}" in message, "the commit names what the run recorded"

    out = tmp_path / "checkout"
    out.mkdir()
    tar = tmp_path / "pages.tar"
    subprocess.run(["git", "archive", "-o", str(tar), "gh-pages"], cwd=proj, check=True)
    with tarfile.open(tar) as t:
        t.extractall(out, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    r = cli(tmp_path, "verify", "--from", str(out / "runs" / run))
    assert r.returncode == 0, r.stdout + r.stderr
    never_leaks(bundle_text(out), proj)

    first = git(proj, "rev-parse", "gh-pages").strip()
    assert share(proj, "--to", "git:gh-pages/runs", "--yes").returncode == 0
    subprocess.run(["git", "merge-base", "--is-ancestor", first, "gh-pages"], cwd=proj, check=True)  # on top of it
    assert len(git(proj, "ls-tree", "-r", "--name-only", "gh-pages").split()) == len(files), "it replaces the run's"


FAKE_AWS = r"""
import json, os, shutil, sys
from pathlib import Path
with open(os.environ["FAKE_AWS_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
if os.environ.get("FAKE_AWS") == "fail":
    print("An error occurred (AccessDenied) when calling the PutObject operation: Access Denied", file=sys.stderr)
    sys.exit(1)
src, dest = sys.argv[3], sys.argv[4]
bucket, _, key = dest[len("s3://"):].partition("/")
shutil.copytree(src, Path(os.environ["FAKE_S3"]) / bucket / key, dirs_exist_ok=True)
"""


@pytest.mark.posix
def test_s3_goes_through_the_aws_cli(proj, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "aws").write_text(f"#!{sys.executable}\n{FAKE_AWS}")
    (bin_dir / "aws").chmod(0o755)
    log, bucket = tmp_path / "aws.jsonl", tmp_path / "s3"
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_AWS_LOG": str(log), "FAKE_S3": str(bucket)}
    run = run_id(proj)
    r = share(proj, "--to", "s3://my-bucket/runs", "--yes", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    call = json.loads(log.read_text().splitlines()[0])
    assert call[:2] == ["s3", "cp"] and call[3] == f"s3://my-bucket/runs/{run}/" and "--recursive" in call
    assert f"https://my-bucket.s3.amazonaws.com/runs/{run}/index.html" in r.stdout
    assert cli(tmp_path, "verify", "--from", str(bucket / "my-bucket" / "runs" / run)).returncode == 0

    r = share(proj, "--to", "s3://my-bucket/runs", "--yes", env={**env, "FAKE_AWS": "fail"})
    assert r.returncode == 1 and "AccessDenied" in r.stdout and "aws sts get-caller-identity" in r.stdout
    r = share(proj, "--to", "s3://my-bucket/runs", "--yes", env={"PATH": str(tmp_path / "empty")})
    assert r.returncode == 2 and "the aws CLI is not on PATH" in r.stdout
    r = share(proj, "--to", "s3://my-bucket/runs", "--dry-run", env={"PATH": str(tmp_path / "empty")})
    assert r.returncode == 0 and "Publishing there would be refused: the aws CLI is not on PATH" in r.stdout
    assert share(proj, "--to", "s3://my-bucket/../x", "--dry-run").returncode == 2
    assert share(proj, "--to", "https://example.com/x", "--dry-run").returncode == 2


def test_verify_from_a_url(proj, tmp_path):
    public = tmp_path / "public"
    assert share(proj, "--to", str(public), "--yes").returncode == 0
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(public))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}/{run_id(proj)}/"
        code, lines = S.verify_from(base + "index.html")
        assert code == 0, lines
        code, lines = S.verify_from(f"http://127.0.0.1:{server.server_address[1]}/nothing-here/")
        assert code == 2 and "is not there" in lines[0]
        (public / run_id(proj) / "badge.svg").write_text("<svg/>")
        code, lines = S.verify_from(base)
        assert code == 1 and "changed: badge.svg" in "\n".join(lines)
    finally:
        server.shutdown()
        server.server_close()


def test_a_manifest_cannot_point_outside_its_bundle(tmp_path):
    (tmp_path / "secret.txt").write_text("x")
    b = tmp_path / "b"
    b.mkdir()
    for bad in ("../secret.txt", "/etc/passwd", "a/../../secret.txt", ".hidden"):
        (b / S.MANIFEST).write_text(json.dumps({"testbench_share": 1, "files": [
            {"path": bad, "bytes": 1, "sha256": "0" * 64}]}))
        code, lines = S.verify_from(str(b))
        assert code == 1 and "inside the bundle" in lines[0], bad
    (b / S.MANIFEST).write_text(json.dumps({"testbench_share": 99, "files": []}))
    assert S.verify_from(str(b))[0] == 2


def test_the_guard_asks_before_a_share_but_not_a_dry_run(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    write_config(root, {"project": "p", "plans": {"t": "true"}})

    def guard(cmd, cwd=root):
        p = subprocess.run([sys.executable, str(REPO / "hooks" / "guard.py")], capture_output=True, text=True,
                           input=json.dumps({"cwd": str(cwd), "tool_name": "Bash", "tool_input": {"command": cmd}}),
                           timeout=30)
        return json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] if p.stdout.strip() else None

    for cmd in ("testbench share --to ./public", "testbench share last --to git:gh-pages/runs --yes",
                "cd x && python -m agent_testbench share --to s3://b/p"):
        assert guard(cmd) == "ask", cmd
        assert guard(cmd, cwd=tmp_path) == "ask", f"outside a project too: {cmd}"
    assert guard("testbench share --to ./public --dry-run") is None, "a dry run publishes nothing"
    assert guard("git push --dry-run") is None and guard("git push origin main") == "ask"
    assert guard("testbench share --to x --dry-run; git push") == "ask"
    assert "testbench share" in C.DEFAULTS["guard"]["ask"]
    assert "testbench share" in json.loads((REPO / "src" / "agent_testbench" / "schema" / "testbench.schema.json")
                                           .read_text())["properties"]["guard"]["properties"]["ask"]["default"]


def test_a_terminal_with_no_line_to_read_is_a_refusal_not_a_traceback(proj, tmp_path):
    """Windows' NUL device says it is a terminal, then input() finds no line: nothing goes out, and the person is told
    why, as when there is no terminal at all."""
    from agent_testbench import config as Cfg

    def no_line(prompt):
        raise EOFError
    with pytest.raises(S.ShareError, match="nobody could answer here") as exc:
        S.share(proj, Cfg.load(proj), None, str(tmp_path / "public"), ask=no_line, tty=True, say=lambda *a: None)
    assert exc.value.code == 2 and not (tmp_path / "public").exists()
