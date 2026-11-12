"""Refusals: exit 2, nothing ran and nothing changed, and the message says what was refused, why, and the exact
command or edit that fixes it - a `testbench ...` command in backticks, or for testbench.yaml and this machine's
limits.yaml the file and the field to change. The reader is often an agent with only the message to go on, so each
row names the fix it must find, not just any command."""
import copy
import hashlib
import shlex
import sys

import pytest

from agent_testbench import budget, cli as CLI
from conftest import SCORER, cli, git, make_notebook, write_config

STEP = {"name": "a", "run": "true", "max_minutes": 1}
PRICED = {"project": "p", "budget": {"max_run_usd": 2, "ask_above_usd": 0.5, "max_day_usd": 3},
          "docker": {"usd_per_hour": 6},                                  # $0.10 a minute, 2 minutes to start
          "plans": {"smoke": {"steps": [STEP]},
                    "big": {"backend": "docker", "steps": [{**STEP, "max_minutes": 60}]},     # worst case $6.20
                    "mid": {"backend": "docker", "steps": [{**STEP, "max_minutes": 9}]}}}     # worst case $1.10
SCORED = {"project": "scoring", "plans": {"score": {"steps": [
    {"name": "score", "run": "python score.py", "max_minutes": 1,
     "expect": {"json": {"metrics.json": {"score": ">= 0"}}}}]}}}
LOOP = "loop start score --metric metrics.json:score --budget-usd 0"
DAEMON_DOWN = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"
# a docker CLI whose daemon is not running: it says so and exits 1, whatever it is asked
DOCKER_DOWN = [sys.executable, "-c", f"import sys; sys.stderr.write({DAEMON_DOWN!r}); sys.exit(1)"]


def make(tmp_path, monkeypatch, machine, kind: str):
    """A project of the given kind in tmp_path/p (this machine's limits folder is `machine`, outside it)."""
    root = tmp_path / "p"
    root.mkdir()
    if kind == "none":
        return root
    if kind == "docker-down":                            # free docker plans, on a machine whose Docker is stopped
        write_config(root, {"project": "p", "docker": {"command": DOCKER_DOWN},
                            "plans": {"box": {"backend": "docker", "steps": [STEP]}}})
        return root
    if kind in ("git", "dirty", "nogit"):
        (root / "score.py").write_text(SCORER)
        (root / "settings.py").write_text("SCORE = 1\n")
        write_config(root, SCORED)
        if kind == "nogit":
            monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))     # not inside some repository above
            return root
        git(root, "init", "-q")
        git(root, "add", "-A")
        git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "start")
        if kind == "dirty":
            (root / "scratch.py").write_text("x = 1\n")
        return root
    make_notebook(root / "train.ipynb", ["x = 1", "print(x)"])
    cfg = copy.deepcopy(PRICED)
    if kind == "nan-budget":
        cfg["budget"]["max_run_usd"] = float("nan")
    elif kind == "budget-list":
        cfg["budget"] = [1]
    elif kind == "bad-backend":
        cfg["plans"]["big"]["backend"] = "dockr"
    elif kind == "lost-notebook":
        cfg["plans"]["nb"] = {"steps": [{"name": "train", "notebook": "trian.ipynb", "max_minutes": 1}]}
    write_config(root, cfg)
    machine.mkdir(parents=True, exist_ok=True)
    if kind == "spent":                                  # a run still going today, at its worst case
        budget.record(root, {"event": "launch", "run": "20260101-000000-big", "plan": "big", "backend": "docker",
                             "worst_case_usd": 2.5})
    elif kind == "machine-day":                          # another project's run, on this machine's ledger
        (machine / "limits.yaml").write_text("max_day_usd: 1.00\n")
        budget.record(tmp_path / "other", {"event": "launch", "run": "20260101-000000-x", "plan": "x",
                                           "backend": "docker", "worst_case_usd": 0.5})
    elif kind == "machine-run":
        (machine / "limits.yaml").write_text("max_run_usd: 0.50\n")
    elif kind == "machine-broken":
        (machine / "limits.yaml").write_text("max_run_usd: lots\n")
    return root


@pytest.fixture(scope="module")
def ran(tmp_path_factory):
    """A project with one finished run of a free plan (read only: the rows on it change nothing)."""
    root = tmp_path_factory.mktemp("ran") / "p"
    root.mkdir()
    write_config(root, {"project": "p", "plans": {"smoke": {"steps": [STEP]}}})
    r = cli(root, "run", "smoke", env={"TESTBENCH_USER_DIR": str(root.parent / "user")})
    assert r.returncode == 0, r.stdout + r.stderr
    return root


def files(root) -> dict:
    """Every file in the project but git's own, by content: a refusal changes none of them."""
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*"))
            if p.is_file() and ".git" not in p.relative_to(root).parts and "__pycache__" not in p.parts}


FILE = "file"   # a fix that is an edit: (FILE, the file's name, the field to change)

REFUSALS = [
    # where to start
    ("none", "plans", 2, ["No testbench.yaml here or in any parent of"], "`testbench init`"),
    # the command line: an unknown command or plan, a value that is not one
    ("priced", "rnu smoke", 2, ["no command 'rnu'"], "`testbench run smoke`"),
    ("priced", "run nope", 2, ["No plan 'nope'. Plans: smoke, big, mid"], "`testbench plans`"),
    ("priced", "estimate smok", 2, ["No plan 'smok' (did you mean smoke?)"], "`testbench plans`"),
    ("priced", "runs -n zero", 2, ["argument -n: 'zero' is not a whole number"], "`testbench runs -n 30`"),
    ("priced", "wait --timeout soon", 2, ["'soon' is not a number"], "`testbench wait 20261101-0916 --timeout 60`"),
    # money: over a limit, over a day's budget, or a person's yes needed
    ("priced", "run big", 2, ["Refused: plan big could cost up to $6.20", "over budget.max_run_usd $2.00",
                              "Lower the max_minutes of plan big's steps in testbench.yaml (18 min in all would fit)",
                              "have a person raise budget.max_run_usd in testbench.yaml"], "`testbench estimate big`"),
    ("priced", "run big --approve", 2, ["Refused: plan big"], "`testbench estimate big`"),
    ("priced", "run mid", 2, ["Needs approval: plan mid could cost up to $1.10 (above budget.ask_above_usd $0.50). "
                              "Ask a person; only they approve it, with"], "`testbench run mid --approve`"),
    ("priced", "estimate mid", 2, ["Needs approval: plan mid"], "`testbench run mid --approve`"),
    ("spent", "run mid --approve", 2, ["Refused: $2.50 already committed today; plan mid could add $1.10, over "
                                       "budget.max_day_usd $3.00", "budget.max_day_usd in testbench.yaml"],
     "`testbench spend`"),
    ("machine-day", "run mid --approve", 2, ["already committed today across every project on this machine",
                                             "over max_day_usd $1.00 in", "limits.yaml"], "`testbench spend`"),
    ("machine-run", "run mid --approve", 2, ["over max_run_usd $0.50 in", "(this machine's limit, which a project "
                                             "cannot raise)", "have a person raise max_run_usd in"],
     "`testbench estimate mid`"),
    ("machine-broken", "run smoke", 2, ["must be a number of US dollars", "nothing can launch until a person fixes it"],
     (FILE, "limits.yaml", "`max_run_usd`")),
    ("priced", "session start gpu --backend docker --max-minutes 60", 2,
     ["Refused: session gpu could cost up to $6.20", "over budget.max_run_usd $2.00"],
     "`testbench session start gpu --backend docker --max-minutes 18`"),
    ("priced", "session start gpu --backend docker --max-minutes 9", 2, ["Needs approval: session gpu", "Ask a person"],
     "`testbench session start gpu --backend docker --max-minutes 9 --approve`"),
    ("priced", "watch big", 2, ["Refused: plan big can cost up to $6.20 a run"], "`testbench watch big --approve`"),
    # testbench.yaml that does not load: the file and the field
    ("nan-budget", "plans", 2, ["must be a number of US dollars"], (FILE, "testbench.yaml", "`budget.max_run_usd`")),
    ("budget-list", "run smoke", 2, ["is a mapping of settings"], (FILE, "testbench.yaml", "`budget:`")),
    ("bad-backend", "estimate smoke", 2, ["is 'dockr'; use one of"], (FILE, "testbench.yaml", "`plans.big.backend`")),
    ("lost-notebook", "run nb", 2, ["trian.ipynb does not exist"],
     (FILE, "testbench.yaml", "`plans.nb.steps[0].notebook`")),
    # a file named on the command line that is not there
    ("priced", "lint trian.ipynb", 2, ["trian.ipynb does not exist"], "`testbench lint train.ipynb`"),
    ("priced", "rescue trian.ipynb", 2, ["trian.ipynb does not exist"], "`testbench rescue train.ipynb`"),
    ("priced", "nb fields trian.ipynb", 2, ["trian.ipynb does not exist"], "`testbench nb fields train.ipynb`"),
    ("priced", "lint notes/x.ipynb", 2, ["notes/x.ipynb does not exist (paths start from"], "`testbench lint --help`"),
    # live sessions
    ("priced", "exec 1+1", 2, ["No session is running."], "`testbench session start`"),
    ("priced", "session stop nope", 2, ["No session named 'nope'"], "`testbench session list`"),
    ("priced", "session start 'my gpu'", 2, ["session names use letters, digits, - or _"],
     "`testbench session start gpu-1`"),
    ("priced", "session start long --backend modal --max-minutes 2000", 2,      # refused here: nothing reaches Modal
     ["a modal session can last at most 24 hours"], "`testbench session start long --backend modal --max-minutes 1440`"),
    # the machine: a backend that could only fail is refused before a run or session is recorded
    ("docker-down", "run box", 2, ["Refused: plan box runs on the docker backend", "Docker is installed but not running "
                                   "here: start it (Docker Desktop, or `sudo systemctl start docker` on Linux)",
                                   "Nothing was started or recorded."], "`testbench run box`"),
    ("docker-down", "session start s1 --backend docker", 2, ["Refused: session s1 runs on the docker backend",
                                                             "Docker is installed but not running here: start it"],
     "`testbench session start s1 --backend docker`"),
    # loops
    ("nogit", LOOP, 2, ["this folder is not in a git repository", "`git init`"], f"`testbench {LOOP}`"),
    ("dirty", LOOP, 2, ["uncommitted changes, which a revert could lose", "?? scratch.py", "`git stash -u`"],
     f"`testbench {LOOP}`"),
    ("git", "loop start score --metric metrics.json:loss --budget-usd 0", 2,
     ["plan score checks no 'loss' in metrics.json", "The numbers it checks: metrics.json:score",
      "a step of plan score in testbench.yaml"], f"`testbench {LOOP}`"),
    ("git", "loop try x", 2, ["No loop is running here."],
     "`testbench loop start PLAN --metric FILE:KEY --budget-usd N`"),
    # setting up
    ("priced", "init", 2, ["testbench.yaml already exists (use --force to replace it)"], "`testbench init --force`"),
    ("priced", "init --recipe pytest", 2, ["already exists (use --force to replace it, or --add"],
     "`testbench init --add --recipe pytest`"),
    ("priced", "init --add", 2, ["--add adds a recipe's plans to testbench.yaml"],
     "`testbench init --add --recipe NAME`"),
    ("priced", "export", 2, ["Nothing to export to: testbench.yaml has no `export:` section"],
     "`testbench export --to csv`"),
    ("priced", "ui --port 9", 2, ["--port goes with --serve"], "`testbench ui --serve --port 9`"),
    # runs that are not there, or are over
    ("priced", "report", 2, ["No runs yet."], "`testbench run <plan>`"),
    ("ran", "verify nope", 2, ["No run 'nope'. Run ids look like"], "`testbench runs`"),
    ("ran", "report nope", 2, ["No run 'nope'. Run ids look like"], "`testbench runs`"),
    ("ran", "verify --repo a/b", 2, ["--repo goes with --attestation"], "`testbench verify --attestation --repo a/b`"),
    ("ran", "cancel", 0, ["already passed, so there is nothing to cancel."], "`testbench report 20"),
]


@pytest.mark.parametrize("kind, args, code, says, fix", REFUSALS, ids=[f"{r[0]}: {r[1]}" for r in REFUSALS])
def test_a_refusal_says_what_why_and_the_fix(tmp_path, monkeypatch, capsys, machine_limits, ran, kind, args, code,
                                             says, fix):
    root = ran if kind == "ran" else make(tmp_path, monkeypatch, machine_limits, kind)
    monkeypatch.chdir(root)
    before = files(root)
    with pytest.raises(SystemExit) as ex:
        CLI.main(shlex.split(args))
    printed = capsys.readouterr()
    out = printed.out + printed.err
    assert (ex.value.code or 0) == code, f"testbench {args}: exit {ex.value.code}\n{out}"
    assert "Traceback" not in out and all(s in out for s in says), f"testbench {args}:\n{out}"
    if isinstance(fix, tuple):
        assert fix[1] in out and fix[2] in out, f"testbench {args} names no file and field to change:\n{out}"
    else:
        assert fix in out, f"testbench {args} does not name the fix {fix}:\n{out}"
    assert files(root) == before, f"testbench {args} changed the project while refusing"


def test_every_row_names_a_command_or_a_field():
    """The rule itself: a fix is a backticked `testbench ...` command, or an edit named by its file and field."""
    for kind, args, code, says, fix in REFUSALS:
        assert fix[0] == FILE and fix[1].endswith(".yaml") and fix[2].startswith("`") if isinstance(fix, tuple) \
            else fix.startswith("`testbench "), (kind, args, fix)


def test_approval_is_a_persons_yes_never_the_agents(tmp_path, monkeypatch, capsys, machine_limits):
    """A "Needs approval" refusal says a person approves; it never tells the agent to pass --approve on its own."""
    root = make(tmp_path, monkeypatch, machine_limits, "priced")
    monkeypatch.chdir(root)
    for args in ("run mid", "session start gpu --backend docker --max-minutes 9"):
        with pytest.raises(SystemExit):
            CLI.main(shlex.split(args))
        out = capsys.readouterr()
        text = out.out + out.err
        said = text.split("Needs approval:", 1)[1]
        assert "Ask a person; only they approve it, with `testbench " in said and "--approve`" in said, text
        assert "rerun with --approve" not in said and "you may approve" not in said.lower(), text


def test_a_stopped_docker_is_refused_before_anything_is_recorded(tmp_path, monkeypatch, capsys, machine_limits):
    """Docker installed, its daemon stopped: `run` and `session start` say so and how to start it (exit 2) before a
    run or session folder, a ledger line or the machine's ledger is written - not a run that errors after starting.
    Money is still the first question: a plan over the budget hears that."""
    root = make(tmp_path, monkeypatch, machine_limits, "docker-down")
    monkeypatch.chdir(root)
    for args in ("run box", "run box --foreground", "session start s1 --backend docker"):
        with pytest.raises(SystemExit) as ex:
            CLI.main(shlex.split(args))
        out = capsys.readouterr()
        text = out.out + out.err
        assert ex.value.code == 2 and "Docker is installed but not running here" in text, (args, text)
        assert "Traceback" not in text, text
    assert not (root / ".testbench").exists(), "no run folder, session folder or ledger in the project"
    assert not (machine_limits / "ledger.jsonl").exists(), "and nothing on this machine's ledger"
    over = {"project": "p", "budget": {"max_run_usd": 1}, "docker": {"command": DOCKER_DOWN, "usd_per_hour": 60},
            "plans": {"box": {"backend": "docker", "steps": [{**STEP, "max_minutes": 30}]}}}
    write_config(root, over)
    with pytest.raises(SystemExit):
        CLI.main(["run", "box"])
    text = capsys.readouterr().out
    assert text.startswith("Refused: plan box could cost up to") and "Docker" not in text, text


@pytest.mark.parametrize("says, code, finding", [
    (DAEMON_DOWN, 1, "Docker is installed but not running here: start it"),
    ("Got permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock", 1,
     "Docker is running, but this user may not use it (permission denied on its socket): add yourself to the docker "
     "group (`sudo usermod -aG docker $USER`, then log in again)"),
    ("error: something else went wrong", 3, "`"),
    (None, 0, ""),
])
def test_the_docker_preflight_says_what_is_wrong(says, code, finding):
    from agent_testbench.backends import docker_backend as D
    script = f"import sys; sys.stderr.write({says!r}); sys.exit({code})" if says else "print('27.3.1')"
    said = D.backend.preflight({"docker": {"command": [sys.executable, "-c", script]}})
    assert said.startswith(finding) and (said == "") == (says is None), said
    if code == 3:
        assert "info` did not work (exit 3: error: something else went wrong); check with `testbench doctor`" in said
    gone = D.backend.preflight({"docker": {"command": ["no-such-docker-cli-here"]}})
    assert gone.startswith("Docker is not installed here (no `no-such-docker-cli-here` command): install it"), gone
    silent = D.backend.preflight({"docker": {"command": [sys.executable, "-c", "import sys; sys.exit(0)"]}})
    assert silent.startswith("`"), "a docker info that names no server version: its daemon did not answer"
