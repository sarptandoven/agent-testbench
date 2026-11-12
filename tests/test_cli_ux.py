"""The command line as a newcomer or an agent meets it: help with examples, mistakes that say what to type instead,
and exit codes that mean the same everywhere (0 passed or done, 1 failed, 2 refused or bad input, 3 still running)."""
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from agent_testbench import cli as CLI
from agent_testbench.core import CORE_MODULES
from conftest import cli, make_notebook, write_config

REPO = Path(__file__).resolve().parents[1]


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
        "slow": {"steps": [{"name": "nap", "run": "python slow.py", "max_minutes": 2}]}}})
    # slow runs until the test lets it go, so it is still running when status and wait --wait 0 look, however
    # loaded the machine is (a fixed sleep made them race)
    (root / "slow.py").write_text("import pathlib, time\nt = time.time()\n"
                                  "while not pathlib.Path('go').exists() and time.time() - t < 100:\n"
                                  "    time.sleep(0.05)\n")
    env = {"TESTBENCH_USER_DIR": str(root.parent / "user")}
    LAUNCHES.update({"run smoke": cli(root, "run", "smoke", env=env).returncode,
                     "run bad": cli(root, "run", "bad", env=env).returncode,
                     "run slow --detach": cli(root, "run", "slow", "--detach", env=env).returncode,
                     "status (running)": cli(root, "status", env=env).returncode,
                     "wait --wait 0 (running)": cli(root, "wait", "--wait", "0", env=env).returncode})
    (root / "go").write_text("")
    LAUNCHES.update({"wait": cli(root, "wait", env=env).returncode,
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
    ("session list", 0), ("session stop", 0), ("session stop --all", 0), ("nb fields nb.ipynb", 0), ("schema", 0),
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
    ("plans --long", "testbench plans: error: unrecognized arguments: --long"),
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
    assert code == 2 and "No testbench.yaml here or in any parent of" in out, out
    assert "Run `testbench check` in the project folder" in out and "or `testbench init` to write" in out, out


def test_no_command_shows_the_core_commands_in_groups(tmp_path):
    wide = {"COLUMNS": "100"}
    r = cli(tmp_path, env=wide)
    assert r.returncode == 2 and "Traceback" not in r.stderr, r.stderr
    assert r.stdout == cli(tmp_path, "--help", env=wide).stdout == cli(tmp_path, "help", env=wide).stdout
    top = CLI._subcommands(CLI.build_parser()).choices
    core = [n for title in CLI.CORE for n in CLI.GROUPS[title]]
    for title in CLI.CORE:
        assert f"\n  {title}\n" in r.stdout
        for name, line in CLI.GROUPS[title].items():
            assert f"    {name:<9} {line}" in r.stdout, name
    for name in set(top) - set(core):
        assert f"\n    {name} " not in r.stdout, f"{name} is not a core command: only `help --all` lists it"
    assert f"\n{len(top) - len(core)} more, for setup, money, notebooks and the rest: testbench help --all\n" \
        in r.stdout
    assert "start with:\n  testbench check" in r.stdout and "exit codes: 0 passed or done, 1 failed, 2 refused" \
        in r.stdout
    assert len(r.stdout.splitlines()) < 45, "the core at a glance"
    r = cli(tmp_path, "rnu")
    assert r.returncode == 2 and "did you mean `testbench run`?" in r.stderr and "Traceback" not in r.stderr
    assert "`testbench help --all` lists every command" in r.stderr
    assert "invalid choice" not in r.stderr, "not argparse's list of every command"


def test_help_all_lists_every_command_in_its_group(tmp_path):
    wide = {"COLUMNS": "100"}
    r = cli(tmp_path, "help", "--all", env=wide)
    assert r.returncode == 0 and "Traceback" not in r.stderr, r.stderr
    assert r.stdout == cli(tmp_path, "--help", "--all", env=wide).stdout == cli(tmp_path, "--all", env=wide).stdout
    for title, names in CLI.GROUPS.items():
        assert f"\n  {title}\n" in r.stdout
        for name, line in names.items():
            assert f"    {name:<9} {line}" in r.stdout, name
    for name in CLI._subcommands(CLI.build_parser()).choices:
        assert f"\n    {name} " in r.stdout, f"`testbench help --all` lists every command; not {name}"
    assert "more, for setup" not in r.stdout and "start with:\n  testbench check" in r.stdout


def test_help_lists_only_commands_the_parser_has(monkeypatch, capsys):
    """A group may name a command the parser lacks (it is left out, and so is a group left empty), and a command
    GROUPS lacks still shows under `other` in `help --all`."""
    monkeypatch.setitem(CLI.GROUPS, "start", {**CLI.GROUPS["start"], "no-such-command": "nothing"})
    monkeypatch.setitem(CLI.GROUPS, "nothing here yet", {"no-such-command": "nothing"})
    monkeypatch.setattr(CLI, "CORE", (*CLI.CORE, "nothing here yet"))
    monkeypatch.setitem(CLI.GROUPS, "proof", {k: v for k, v in CLI.GROUPS["proof"].items() if k != "history"})
    code, core = tb(capsys, "--help")
    assert code == 0 and "no-such-command" not in core and "nothing here yet" not in core
    assert "\n  start\n    demo " in core and "\n    doctor " in core
    code, every = tb(capsys, "help", "--all")
    assert code == 0 and "no-such-command" not in every and "\n  other\n    history " in every


def test_every_command_is_in_one_group_with_a_short_line():
    top = CLI._subcommands(CLI.build_parser()).choices
    grouped = [n for names in CLI.GROUPS.values() for n in names]
    assert sorted(grouped) == sorted(top), "a new command needs a line in cli.GROUPS"
    assert all(len(f"    {n:<9} {line}") <= 78 for names in CLI.GROUPS.values() for n, line in names.items()), \
        "one line each in an 80-column terminal"
    assert list(CLI.GROUPS)[:len(CLI.CORE)] == list(CLI.CORE), "the core groups come first in `help --all` too"
    core = [n for title in CLI.CORE for n in CLI.GROUPS[title]]
    assert len(core) <= 12, "the core is a dozen commands"


def _section(path: Path, heading: str) -> str:
    """The part of a Markdown file under `## heading`, up to the next `## ` heading."""
    text = path.read_text(encoding="utf-8")
    start = text.index(f"\n## {heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start:end if end > 0 else None]


def _cells(row: str) -> list[str]:
    """A Markdown table row's cells (`\\|` inside one is a pipe, not a border)."""
    return [c.strip() for c in re.split(r"(?<!\\)\|", row.strip())[1:-1]]


def _words(cell: str) -> list[str]:
    """The words of a cell's code spans: `session sync`, `install PKG...` -> session, sync, install, PKG..."""
    return [w for span in re.findall(r"`([^`]+)`", cell) for w in re.split(r"\s+|\\\|", span) if w]


def test_reference_lists_every_command_in_its_group_with_its_line():
    """docs/reference.md#commands: the groups of `testbench help --all`, in its order (a bold row each), and under each
    its commands, in its order. A command's first row says what it does in the line `help --all` shows, then more; a
    command documented as a row per subcommand (session, loop) names every subcommand instead."""
    top = CLI._subcommands(CLI.build_parser()).choices
    titles, rows = [], {}
    for line in _section(REPO / "docs" / "reference.md", "Commands").splitlines():
        if not line.startswith("| ") or set(line) <= set("|-: "):
            continue
        cells = _cells(line)
        if bold := re.fullmatch(r"\*\*(.+)\*\*", cells[0]):
            titles.append(bold.group(1))
        elif titles:                                       # not the header row
            rows.setdefault(titles[-1], {}).setdefault(_words(cells[0])[0], []).append(cells)
    assert titles == list(CLI.GROUPS), (f"docs/reference.md#commands has the groups {titles}, `testbench help --all` "
                                        f"{list(CLI.GROUPS)}: a bold row (| **group** | | |) for each, in that order")
    for title, names in CLI.GROUPS.items():
        found = list(rows.get(title, {}))
        assert found == list(names), (f"under **{title}**, docs/reference.md#commands has rows for {found}; "
                                      f"cli.GROUPS has {list(names)}: a row for each, in that order")
        for name, line in names.items():
            first = rows[title][name]
            subs = CLI._subcommands(top[name])
            if subs and len(first) > 1:
                missing = set(subs.choices) - {w for cells in first for w in _words(cells[0])}
                assert not missing, f"docs/reference.md#commands has no row for `testbench {name}` {sorted(missing)}"
                continue
            line = re.sub(rf"^{name} \w+: ", "", line)          # `plugin test: check ...` is the plugin test row's
            assert first[0][1].startswith(line), (
                f"docs/reference.md#commands: the row for `{name}` says what it does in the words `testbench help "
                f"--all` uses ({line!r}), then more; it says {first[0][1]!r}")


def test_readme_shows_the_core_with_their_lines_and_the_rest_by_group():
    """README.md#commands: the core as `testbench --help` shows it (each group, each command and its line), then the
    rest of `testbench help --all`'s groups and their commands."""
    section = _section(REPO / "README.md", "Commands")
    assert re.findall(r"^\| \*\*(.+?)\*\* \|", section, re.M) == list(CLI.CORE), \
        f"README.md#commands: a bold row for each group of cli.CORE, in order: {list(CLI.CORE)}"
    core = re.findall(r"^\| `testbench ([\w-]+)` \| (.+?) \|$", section, re.M)
    want = [(name, line) for title in CLI.CORE for name, line in CLI.GROUPS[title].items()]
    assert core == want, ("README.md#commands lists the core commands as `testbench --help` does, in its words: "
                          f"| `testbench NAME` | LINE |, in this order: {want}")
    rest = {group: [w.split()[0] for w in re.findall(r"`([^`]+)`", cell)]
            for group, cell in re.findall(r"^\| ([a-z][a-z ,]*) \| (.+) \|$", section, re.M)}
    want = {title: list(names) for title, names in CLI.GROUPS.items() if title not in CLI.CORE}
    assert rest == want, f"README.md#commands lists the other groups as | group | `name`, `name` |, these: {want}"


# `testbench --version` and `--help` load the standard library, cli and little else: 97 to 105 modules on Python 3.10
# to 3.13 on Linux, up to 128 on 3.14 (counted with -X importtime, the interpreter's own included). The names are the
# check; the count is a backstop: PyYAML and the core add about 70 (`testbench plans` loads 172), nbformat about 150.
MAX_START_MODULES = 150
HEAVY = ("yaml", "nbformat", "nbclient", "jupyter_client", "ipykernel", "IPython", "mcp", "modal", "playwright",
         "numpy", "pandas")


@pytest.mark.parametrize("args", [["--version"], ["--help"]])
def test_version_and_help_load_no_yaml_notebooks_or_core(tmp_path, args):
    r = subprocess.run([sys.executable, "-X", "importtime", "-m", "agent_testbench", *args], cwd=tmp_path,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    loaded = [line.rsplit("|", 1)[1].strip() for line in r.stderr.splitlines() if line.startswith("import time:")]
    loaded = [m for m in loaded if m != "imported package"]
    slow = sorted(m for m in loaded if m.split(".")[0] in HEAVY or m in CORE_MODULES[1:]
                  or m in ("agent_testbench.notebook", "agent_testbench.lint"))
    assert not slow, (f"`testbench {args[0]}` imports {slow}, which only some commands need: import it inside the "
                      "command that uses it (the modules cli.py names at its top are cli._Later)")
    assert len(loaded) <= MAX_START_MODULES, f"`testbench {args[0]}` imports {len(loaded)} modules: {loaded}"


def test_plugin_test_help_names_every_kind():
    """`plugin test --kind` lists the kinds as written in cli.py (so making the parser loads no core module, and
    Python 3.14's argparse, which expands help as options are added, has nothing left to fill in): the same as
    plugins.KINDS."""
    from agent_testbench.plugins import KINDS
    assert CLI.PLUGIN_KINDS.split(", ") == list(KINDS)
    text = CLI.commands(CLI.build_parser())["plugin test"].format_help()
    assert " ".join(f"test it as this kind: {CLI.PLUGIN_KINDS}".split()) in " ".join(text.split())


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
