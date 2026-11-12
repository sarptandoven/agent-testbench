"""The command line as a newcomer or an agent meets it: help with examples, mistakes that say what to type instead,
and exit codes that mean the same everywhere (0 passed or done, 1 failed, 2 refused or bad input, 3 still running)."""
import shlex

import pytest

from agent_testbench import cli as CLI
from conftest import cli, make_notebook, write_config


def tb(capsys, *args) -> tuple[int, str]:
    """testbench ARGS in this process: (exit code, everything it printed). Any exception but SystemExit fails the test:
    it would be a traceback for whoever ran the command."""
    with pytest.raises(SystemExit) as ex:
        CLI.main(list(args))
    out = capsys.readouterr()
    return ex.value.code or 0, out.out + out.err


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    """A project with a plan that passes (smoke), one that fails (bad), a notebook, and a finished run of each."""
    root = tmp_path_factory.mktemp("ux") / "proj"
    make_notebook(root / "nb.ipynb", ["x = 1", "print(x)"])
    write_config(root, {"project": "proj", "plans": {
        "smoke": {"steps": [{"name": "ok", "run": "true", "max_minutes": 1}]},
        "bad": {"steps": [{"name": "no", "run": "false", "max_minutes": 1}]},
        "slow": {"steps": [{"name": "nap", "run": "python -c \"import time; time.sleep(3)\"", "max_minutes": 1}]}}})
    env = {"TESTBENCH_USER_DIR": str(root.parent / "user")}
    LAUNCHES.update({"run smoke": cli(root, "run", "smoke", env=env).returncode,
                     "run bad": cli(root, "run", "bad", env=env).returncode,
                     "run slow --detach": cli(root, "run", "slow", "--detach", env=env).returncode,
                     "status (running)": cli(root, "status", env=env).returncode,
                     "wait --wait 0 (running)": cli(root, "wait", "--wait", "0", env=env).returncode,
                     "wait": cli(root, "wait", env=env).returncode,
                     "run nope": cli(root, "run", "nope", env=env).returncode})
    return root


LAUNCHES = {}


def test_launches_follow_the_exit_code_table(project):
    assert LAUNCHES == {"run smoke": 0, "run bad": 1, "run slow --detach": 3, "status (running)": 3,
                        "wait --wait 0 (running)": 3, "wait": 0, "run nope": 2}


@pytest.mark.parametrize("args, code", [
    ("plans", 0), ("estimate smoke", 0), ("estimate smoke --json", 0), ("status", 0), ("status last~1", 1),
    ("report", 0), ("report last~1", 0), ("report --format junit", 0), ("verify", 0), ("history smoke", 0),
    ("runs", 0), ("spend", 0), ("cancel", 0), ("diff last~1 last", 0), ("prices", 0), ("recipes", 0),
    ("lint nb.ipynb", 0), ("note found it", 0), ("doctor", 0), ("connect --list", 0), ("demo --list", 0),
    ("session list", 0), ("session stop", 0), ("session stop --all", 0), ("nb fields nb.ipynb", 0),
    # refused or bad input: nothing ran
    ("estimate nope", 2), ("history nope", 2), ("report nope", 2), ("exec 1+1", 2), ("files ls", 2),
    ("loop status", 2), ("loop try x", 2), ("loop stop", 2), ("notify test", 2), ("export", 2), ("init", 2),
    ("recipes nope", 2), ("connect nope", 2), ("watch nope", 2), ("ui --port 1", 2), ("verify --repo a/b", 2),
])
def test_each_command_follows_the_exit_code_table(project, monkeypatch, capsys, args, code):
    monkeypatch.chdir(project)
    got, out = tb(capsys, *shlex.split(args))
    assert got == code, f"testbench {args}: exit {got}\n{out}"


@pytest.mark.parametrize("args, says", [
    # unknown commands: the closest one, in full
    ("rnu smoke", "no command 'rnu'; did you mean `testbench run smoke`?"),
    ("repot", "did you mean `testbench report`"),
    ("session strat", "testbench session: no command 'strat'; did you mean `testbench session start`?"),
    ("start", "did you mean `testbench session start` or `testbench loop start`?"),
    ("smoke", "smoke is a plan here: `testbench run smoke` runs it"),
    ("version", "did you mean `testbench --version`?"),
    # the words in the wrong order
    ("smoke run --wait 5", "the command comes first: `testbench run smoke --wait 5`"),
    ("session main start", "the command comes first: `testbench session start main`"),
    ("nb build nb.ipynb cells", "the other way round: `testbench nb build cells nb.ipynb`"),
    ("nb split cells nb.ipynb", "the other way round: `testbench nb split nb.ipynb cells`"),
    # a plan, a run, a session, a recipe, a client or an example that is not there
    ("run smok", "No plan 'smok' (did you mean smoke?). Plans: smoke, bad, slow"),
    ("estimate smok", "did you mean smoke?"),
    ("watch smok", "did you mean smoke?"),
    ("history smok", "did you mean smoke?"),
    ("loop start smok --metric a:b --budget-usd 0", "did you mean smoke?"),
    ("report nope", "No run 'nope'. Run ids look like"),
    ("report smoke", "smoke is a plan, not a run; its latest run is"),
    ("status last~99", "last~99 is before the first run"),
    ("exec -s nope 1", "No session named 'nope'. `testbench session list` shows them."),
    ("session stop nope", "No session named 'nope'"),
    ("recipes pytst", "no recipe 'pytst' (did you mean pytest?)"),
    ("connect cusor", "unknown client cusor (did you mean cursor?)"),
    ("demo llm-ap", "there is no llm-ap example (did you mean llm-app?)"),
    # missing or wrong arguments: argparse's message, and where the examples are
    ("run", "the following arguments are required: PLAN"),
    ("loop start smoke", "the following arguments are required: --metric, --budget-usd"),
    ("plans --json", "testbench plans: error: unrecognized arguments: --json"),
    ("wait --timeout soon", "'soon' is not a number"),
    ("runs -n 0", "'0' must be 1 or more"),
    ("files get", "files get REMOTE [LOCAL] - got 0 path(s)"),
    ("exec --cells nb.ipynb --params {bad", "--params is a JSON object"),
    ("exec --params {}", "--params goes with --cells"),
    ("lint nope.ipynb", "nope.ipynb does not exist"),
    ("lint .", ". is a folder, not a file"),
    ("verify --repo a/b", "--repo goes with --attestation"),
    ("exec 1+1", "No session is running. Start one with `testbench session start`."),
    ("loop try x", "No loop is running here. Start one with `testbench loop start PLAN"),
])
def test_a_mistake_says_what_to_do(project, monkeypatch, capsys, args, says):
    monkeypatch.chdir(project)
    code, out = tb(capsys, *shlex.split(args))
    assert code == 2 and says in out, f"testbench {args}: exit {code}\n{out}"
    if "argument" in out:
        assert f"`testbench {args.split()[0]}" in out and "--help` says what it takes, with examples" in out
    assert not (project / "llm-ap").exists(), "a typo of an example's name copies nothing"


@pytest.mark.parametrize("args", ["plans", "run smoke", "report", "session start", "exec 1", "loop status", "ui --serve",
                                  "diff", "watch smoke"])
def test_outside_a_project_every_command_says_where_to_start(tmp_path, monkeypatch, capsys, args):
    monkeypatch.chdir(tmp_path)
    code, out = tb(capsys, *shlex.split(args))
    assert code == 2 and "No testbench.yaml here or in any parent of" in out and "Run `testbench init`" in out, out


def test_no_command_shows_the_commands_in_groups(tmp_path):
    wide = {"COLUMNS": "100"}
    r = cli(tmp_path, env=wide)
    assert r.returncode == 2 and "Traceback" not in r.stderr, r.stderr
    assert r.stdout == cli(tmp_path, "--help", env=wide).stdout == cli(tmp_path, "help", env=wide).stdout
    for title, names in CLI.GROUPS.items():
        assert f"\n  {title}\n" in r.stdout
        for name, line in names.items():
            assert f"    {name:<9} {line}" in r.stdout, name
    assert "start with:\n  testbench demo" in r.stdout and "exit codes: 0 passed or done, 1 failed, 2 refused" in r.stdout
    r = cli(tmp_path, "rnu")
    assert r.returncode == 2 and "did you mean `testbench run`?" in r.stderr and "Traceback" not in r.stderr
    assert "invalid choice" not in r.stderr, "not argparse's list of every command"


def test_every_command_is_in_one_group_with_a_short_line():
    top = CLI._subcommands(CLI.build_parser()).choices
    grouped = [n for names in CLI.GROUPS.values() for n in names]
    assert sorted(grouped) == sorted(top), "a new command needs a line in cli.GROUPS"
    assert all(len(f"    {n:<9} {line}") <= 78 for names in CLI.GROUPS.values() for n, line in names.items()), \
        "one line each in an 80-column terminal"


def test_every_command_has_examples_that_parse():
    ap = CLI.build_parser()
    found = CLI.commands(ap)
    assert {"run", "session start", "loop try", "nb build"} <= set(found)
    for path, p in found.items():
        text = p.format_help()
        assert "\nexamples:\n  testbench " in text, f"`testbench {path} --help` has no example"
        assert p.description, path
        for action in p._actions:
            if action.option_strings and action.dest != "help":
                assert action.help, f"testbench {path} {action.option_strings[0]} has no help"
        for line in CLI.examples()[path].splitlines():
            argv = shlex.split(line, comments=True)[1:]
            assert line.startswith("  testbench ") and len(line) <= 80, line
            assert CLI._check_command(ap, argv) is None, line
            assert not ap.parse_known_args(argv)[1], line
            assert argv[:len(path.split())] == path.split(), f"an example under {path} runs another command: {line}"


def test_aliases_keep_every_spelling_working():
    ap = CLI.build_parser()
    for argv, key, value in [
        (["wait", "--wait", "5"], "timeout", 5.0), (["wait", "--timeout", "5"], "timeout", 5.0),
        (["session", "install", "-s", "gpu", "x"], "name", "gpu"), (["session", "install", "--name", "gpu", "x"], "name", "gpu"),
        (["session", "install", "--session", "gpu", "x"], "name", "gpu"), (["session", "shell", "--session", "gpu", "ls"], "name", "gpu"),
        (["session", "stop", "-s", "gpu"], "session", "gpu"), (["session", "stop", "gpu"], "name", "gpu"),
        (["session", "sync", "--session", "gpu"], "session", "gpu"), (["session", "start", "-s", "gpu"], "session", "gpu"),
        (["diff", "--format", "json"], "format", "json"), (["diff", "--json"], "json", True),
        (["history", "p", "--format", "json"], "format", "json"), (["estimate", "p", "--format", "json"], "format", "json"),
        (["exec", "--format", "json", "1"], "format", "json"), (["rescue", "n.ipynb", "--format", "json"], "format", "json"),
        (["report", "--format", "json"], "format", "json"), (["report", "--json"], "json", True),
    ]:
        assert getattr(ap.parse_args(argv), key) == value, argv


def test_format_json_is_json(project, monkeypatch, capsys):
    import json
    monkeypatch.chdir(project)
    for args in (["estimate", "smoke", "--format", "json"], ["history", "smoke", "--format", "json"],
                 ["diff", "last~1", "last", "--format", "json"]):
        code, out = tb(capsys, *args)
        assert code == 0 and json.loads(out), args


def test_help_with_a_command_is_its_help(capsys):
    code, out = tb(capsys, "help", "loop", "try")
    assert code == 0 and out.startswith("usage: testbench loop try") and '\n  testbench loop try "' in out
