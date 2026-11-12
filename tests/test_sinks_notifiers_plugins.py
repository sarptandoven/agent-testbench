"""Sinks and notifiers are plug-ins: export: and notify: take an installed package's sink or notifier by name, the
built-ins (mlflow, wandb, csv, jsonl; the webhook and the desktop) sit in the same registry, and every one of them is
held to its kind's contract. Plug-ins are installed here the way pip does it (a module and a dist-info with entry
points on PYTHONPATH) or registered in-process. A broken sink or notifier only leaves a line in export.log or
notify.log: a run never fails or waits for one. Nothing here leaves the machine: webhooks are the local `webhook`
fixture, the desktop's command is a fake on PATH, and the contracts refuse connections to other machines."""
import json
import os
import re
import threading
import time
import tomllib
from pathlib import Path

import jsonschema
import pytest
import yaml

from agent_testbench import config as C, export as X, notify as N, plugins as P
from conftest import EXAMPLES, Webhook, cli, write_config

SECRET = "sekrit-value-0123456789"
QUICK = {"name": "hello", "run": "python -c \"print('val_loss=0.25')\"", "max_minutes": 1,
         "expect": {"metrics": {"stdout": {"val_loss=([0-9.]+)": "< 0.3"}}}}


@pytest.fixture(autouse=True)
def fresh_registry():
    """Sinks and notifiers registered by a test are forgotten after it."""
    yield
    P.reset("sinks")
    P.reset("notifiers")


def install(site: Path, dist: str, modules: dict, entry_points: dict) -> dict:
    """A package as pip leaves it: its modules and a dist-info with entry points, in a folder on PYTHONPATH."""
    site.mkdir(parents=True, exist_ok=True)
    for module, code in modules.items():
        (site / f"{module}.py").write_text(code)
    info = site / f"{dist.replace('-', '_')}-0.1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: 0.1.0\n")
    (info / "entry_points.txt").write_text("".join(f"[{group}]\n" + "".join(f"{k} = {v}\n" for k, v in eps.items())
                                                   + "\n" for group, eps in entry_points.items()))
    return {"PYTHONPATH": os.pathsep.join(p for p in (str(site), os.environ.get("PYTHONPATH")) if p)}


def install_example(site: Path, name: str) -> dict:
    """An example plug-in installed from its own pyproject.toml, so the entry point it declares is the one used."""
    folder = EXAMPLES / "plugins" / name
    meta = tomllib.loads((folder / "pyproject.toml").read_text())
    modules = {m: (folder / f"{m}.py").read_text() for m in meta["tool"]["setuptools"]["py-modules"]}
    return install(site, meta["project"]["name"], modules, meta["project"]["entry-points"])


def project(root: Path, **sections) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    write_config(root, {"project": "plugged", **sections, "plans": {"quick": {"steps": [QUICK]}}})
    return root


def until(read, seconds: float = 60):
    """read() once it gives something truthy (background work takes a moment)."""
    end = time.time() + seconds
    while time.time() < end:
        if got := read():
            return got
        time.sleep(0.2)
    raise AssertionError(f"nothing after {seconds} s")


def text(path: Path) -> str:
    return path.read_text(errors="replace") if path.is_file() else ""


def latest(root: Path) -> Path:
    return sorted((root / ".testbench" / "runs").iterdir())[-1]


# ---- the registry: built-ins and old names --------------------------------------------------------------------

def test_the_builtins_sit_in_the_registry_and_the_old_names_read_it():
    assert P.names("sinks") == ["mlflow", "wandb", "csv", "jsonl"]
    assert P.names("notifiers") == ["webhook", "desktop"]
    assert X.TARGETS == ("mlflow", "wandb", "csv", "jsonl") and X.LOCAL == ("csv", "jsonl")
    assert X.PACKAGES == {"mlflow": "MLflow", "wandb": "Weights & Biases (wandb)"}
    assert X.EXPORTERS["csv"] is X.CSV.send and X.background("mlflow") and not X.background("jsonl")
    P.register("sinks", "sheet", X.Sink(send=lambda root, run_dir, rec, opts: "a row", options=("path",)))
    assert X.TARGETS[-1] == "sheet" and "sheet" in X.LOCAL and X.sink("sheet").name == "sheet"
    with pytest.raises(AttributeError):
        X.NO_SUCH_THING


def test_a_plugin_sink_s_missing_package_says_how_to_install_it(tmp_path):
    P.register("sinks", "neptune", X.Sink(send=lambda *a: "sent", background=True, requires="no_such_tracker_pkg",
                                          package="no-such-tracker", title="Neptune"))
    with pytest.raises(X.Missing, match=r"^Neptune is not installed in this Python \(.+\): pip install no-such-tracker$"):
        X.require("neptune")
    X.require("not-a-sink")                                     # check_export names it; nothing to install here
    with pytest.raises(TypeError, match="a Sink has no setting 'colour'"):
        X.Sink("x", colour="blue")


def test_every_builtin_keeps_its_kind_s_contract(tmp_path, monkeypatch):
    for name in P.names("sinks"):
        assert P.check("sinks", X.sink(name)) == [], name
    fake = tmp_path / "bin"
    desktop(fake, tmp_path / "shown.txt")
    monkeypatch.setenv("PATH", str(fake) + os.pathsep + os.environ["PATH"])
    began = time.time()
    for name in P.names("notifiers"):
        assert P.check("notifiers", P.get("notifiers", name)) == [], name
    assert time.time() - began < 20
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))           # no way to show one: still keeps it
    assert P.check("notifiers", N.DESKTOP) == []


# ---- the contracts say what a plug-in does wrong ----------------------------------------------------------------

class Leaky:
    """A sink that does everything wrong: takes any settings, shows secrets, reaches out while in the foreground."""
    name, background = "leaky", False

    def validate(self, where, opts):
        return opts if isinstance(opts, dict) else {}

    def send(self, root, run_dir, record, opts):
        import socket
        print("the token is", os.environ.get("TESTBENCH_CHECK_TOKEN"))
        try:
            socket.create_connection(("tracker.example.com", 80), timeout=2).close()
        except OSError:
            pass
        raise RuntimeError(f"could not send with {opts}")


def test_the_sink_contract_says_what_a_sink_does_wrong():
    problems = P.check("sinks", Leaky())
    said = "\n".join(problems)
    assert "validate() took 42 as its settings" in said and "naming `export.leaky`" in said
    assert "send() raised RuntimeError" in said and "export.ExportError" in said
    assert "a secret came out" in said and "the environment as TESTBENCH_CHECK_TOKEN and one in its settings" in said
    assert "it reached for tracker.example.com:80 with background = False" in said
    assert "tb-check-" not in said, "the contract's own fake secrets are not repeated"
    assert P.check("sinks", object())[0].startswith("it cannot be made into a sink")
    nameless = X.Sink("Bad Name", send=lambda *a: "ok")
    assert P.check("sinks", nameless)[0].startswith("its name is 'Bad Name'")


class Sluggish(N.Notifier):
    name, example = "sluggish", {"url": "{url}"}

    def validate(self, where, opts):
        if not isinstance(opts, dict) or set(opts) != {"url"}:
            raise C.ConfigError(f"`{where}` takes url")
        return opts

    def send(self, event, text, fields, opts, timeout):
        time.sleep(timeout + 2)
        return True, f"posted to {opts['url']}"

    def where(self, opts):
        return opts["url"]


def test_the_notifier_contract_says_what_a_notifier_does_wrong():
    began = time.time()
    said = "\n".join(P.check("notifiers", Sluggish()))
    assert "where() shows the URL's path or query" in said
    assert "against a closed port on this machine was still going 2.5 s" in said
    assert "against a port that takes the connection and never answers was still going" in said
    assert time.time() - began < 15

    class Raising(Sluggish):
        def send(self, *args):
            raise RuntimeError("no")

        def where(self, opts):
            return N.where(opts["url"])
    assert P.check("notifiers", Raising())[0].startswith("send() against a closed port on this machine raised "
                                                         "RuntimeError")
    named_on = N.Notifier()
    named_on.name = "on"
    assert "not on or webhook_env" in P.check("notifiers", named_on)[0], "a name notify: can use"


# ---- settings: plug-in names in export: and notify:, their settings checked, secrets kept out ----------------------

def test_export_and_notify_take_plugin_names_and_keep_secrets_out(tmp_path):
    P.register("sinks", "vault", X.Sink(send=lambda *a: "ok", options=("path", "token_env"), short="path",
                                        short_hint="the file's path", hints={"path": "out.txt"}))
    assert C.check_export({"vault": "a.txt", "csv": True}) == {"vault": {"path": "a.txt"}, "csv": {}}
    assert C.check_export({"vault": {"token_env": "VAULT_TOKEN"}}) == {"vault": {"token_env": "VAULT_TOKEN"}}
    for section, says in [({"vault": {"token_env": "not a name"}}, "`export.vault.token_env` is the NAME of an "),
                          ({"vault": {"token": "x"}}, "`export.vault.token`: secrets never go in testbench.yaml"),
                          ({"vault": 3}, "`export.vault` is a mapping of settings (path, token_env), or the file's"),
                          ({"nope": {}}, "use mlflow, wandb, csv, jsonl, vault")]:
        with pytest.raises(C.ConfigError, match=re.escape(says)):
            C.check_export(section)

    class Lax(X.Sink):                                  # its own check lets a secret through; the core does not
        def validate(self, where, opts):
            return dict(opts)
    P.register("sinks", "lax", Lax(send=lambda *a: "ok"))
    with pytest.raises(C.ConfigError, match=r"`export.lax.api_key`: secrets never go in testbench.yaml"):
        C.check_export({"lax": {"api_key": SECRET}})

    class Buggy(X.Sink):
        def validate(self, where, opts):
            raise KeyError("oops")
    P.register("sinks", "buggy", Buggy())
    with pytest.raises(C.ConfigError, match=r"`export.buggy`: the buggy sink could not check its settings .*KeyError"
                                            r".*leave it out \(`buggy: false`\)"):
        C.check_export({"buggy": {}})

    base = {"project": "p", "plans": {}}
    assert C.validate({**base, "notify": {"desktop": True}}, tmp_path)["notify"] == {
        "webhook": None, "webhook_env": None, "on": list(C.NOTIFY_DEFAULT), "desktop": {}}
    both = C.validate({**base, "notify": {"webhook_env": "HOOK", "desktop": None, "on": ["run_end"]}}, tmp_path)
    assert N.chosen(both["notify"]) == {"webhook": {"webhook": None, "webhook_env": "HOOK"}, "desktop": {}}
    P.register("notifiers", "loose", N.Notifier())
    for notify, says in [({"desktop": {"sound": "ping"}}, "`notify.desktop` takes no settings: write `desktop: true`"),
                         ({"desktop": False}, "`notify` needs one of `webhook:`"),
                         ({"pager": True}, "`notify` has no 'pager'; use webhook (or webhook_env) and on"),
                         ({"pager": True}, "another notifier goes by its name: desktop, loose"),
                         ({"desktop": True, "webhook": "https://a.example/x", "webhook_env": "H"}, "needs one of")]:
        with pytest.raises(C.ConfigError, match=re.escape(says)):
            C.validate({**base, "notify": notify}, tmp_path)

    class LooseNotifier(N.Notifier):
        def validate(self, where, opts):
            return dict(opts)
    P.register("notifiers", "looser", LooseNotifier())
    with pytest.raises(C.ConfigError, match=r"`notify.looser.password`: secrets never go"):
        C.validate({**base, "notify": {"looser": {"password": SECRET}}}, tmp_path)
    assert C.validate({**base, "notify": {"looser": {"url_env": "HOOK_URL"}}}, tmp_path)["notify"]["looser"] == \
        {"url_env": "HOOK_URL"}


def test_the_schema_takes_the_new_shapes():
    schema = json.loads(C.SCHEMA.read_text())
    check = jsonschema.Draft202012Validator(schema)

    def ok(notify: dict) -> bool:                       # as an editor reads it (YAML 1.2: `on` is a word)
        return not list(check.iter_errors({"notify": notify}))
    assert ok({"desktop": True}) and ok({"webhook_env": "HOOK", "desktop": True, "on": ["run_end"]})
    assert ok({"webhook_env": "HOOK", "events": {"path": "/tmp/events.jsonl"}}), "a plug-in notifier"
    assert not ok({"on": ["run_end"]}) and not ok({}) and not ok({"desktop": "loud"})
    assert not ok({"webhook": "https://a.example/x", "webhook_env": "H", "desktop": True})


# ---- a plug-in sink, installed as pip installs it, through the CLI ---------------------------------------------------

def test_the_example_sink_installed_like_pip_does_writes_a_row_per_run(tmp_path):
    env = install_example(tmp_path / "site", "testbench-sink-example")
    root = project(tmp_path / "proj", export={"markdown": "runs.md"})
    assert cli(root, "run", "quick", "--wait", "120", env=env).returncode == 0
    run = latest(root)
    rows = until(lambda: [r for r in text(root / "runs.md").splitlines() if r.startswith(f"| {run.name} |")])
    assert rows == [f"| {run.name} | quick | PASSED | 2/2 | $0.000 | [report](.testbench/runs/{run.name}/report.json) |"]
    assert text(root / "runs.md").startswith("| run | plan | status | checks | cost | report |\n")
    assert until(lambda: "markdown: ok - a row in runs.md" in text(run / "export.log"))

    again = cli(root, "export", "--to", "markdown", env=env)                  # exported again: its row replaced
    assert again.returncode == 0 and f"{run.name} markdown: a row in runs.md" in again.stdout, again.stdout + again.stderr
    assert len(text(root / "runs.md").splitlines()) == 3
    other = cli(root, "export", "--to", "markdown", "--path", "other.md", env=env)
    assert other.returncode == 0 and (root / "other.md").is_file()
    assert cli(root, "run", "quick", "--wait", "120", env=env).returncode == 0
    until(lambda: len(text(root / "runs.md").splitlines()) == 4)
    report = cli(root, "report", env=env).stdout
    assert "## Exports" in report and "- markdown: a row in runs.md" in report

    unknown = cli(root, "export", "--to", "sheets", env=env)
    assert unknown.returncode == 2 and "`export` has no 'sheets'; use mlflow, wandb, csv, jsonl, markdown" in \
        unknown.stderr, unknown.stdout + unknown.stderr
    alone = cli(root, "export", "--to", "markdown")                             # not installed in that Python
    assert alone.returncode == 2 and "`export` has no 'markdown'" in alone.stderr


BROKEN_SINKS = """\
import os
import pathlib
from agent_testbench.export import Sink


class Crash(Sink):
    def send(self, root, run_dir, record, opts):
        raise RuntimeError(f"the sheet service fell over (token {os.environ.get('MY_TOKEN')})")


class Exits(Sink):
    def send(self, root, run_dir, record, opts):
        raise SystemExit(3)


class Later(Sink):
    background = True                         # runs in the background process after the run

    def send(self, root, run_dir, record, opts):
        pathlib.Path(root, "later.txt").write_text(record["run_id"])
        return "written later"
"""


def test_a_broken_plugin_sink_only_leaves_a_line_in_export_log(tmp_path):
    env = install(tmp_path / "site", "testbench-broken-sinks", {"broken_sinks": BROKEN_SINKS}, {
        "agent_testbench.sinks": {"crash": "broken_sinks:Crash", "exits": "broken_sinks:Exits",
                                  "later": "broken_sinks:Later", "gone": "no_such_module:Sink",
                                  "csv": "broken_sinks:Crash"}})
    root = project(tmp_path / "proj", export={"crash": True, "exits": None, "gone": {"path": "x"}, "later": True,
                                              "jsonl": True})
    r = cli(root, "run", "quick", "--wait", "120", env={**env, "MY_TOKEN": SECRET})
    run = latest(root)
    assert r.returncode == 0 and json.loads((run / "state.json").read_text())["status"] == "passed", r.stdout
    log = until(lambda: "later: ok" in text(run / "export.log") and text(run / "export.log"))
    assert "crash: failed - RuntimeError: the sheet service fell over (token <MY_TOKEN>)" in log
    assert "exits: failed - SystemExit: 3" in log
    assert re.search(r"gone: failed - sink 'gone' is installed but did not load \(.*ModuleNotFoundError.*\)", log), log
    assert "jsonl: ok - line 1 of .testbench/runs.jsonl" in log, "a built-in next to broken plug-ins still runs"
    assert "later: started - in the background" in log and "later: ok - written later" in log
    assert text(root / "later.txt") == run.name
    report = cli(root, "report", env=env).stdout
    assert "- crash: FAILED" in report and f"`testbench export {run.name} --to crash`" in report
    assert "- gone: FAILED" in report and "reinstall or fix that package" in report
    assert SECRET not in log + report
    again = cli(root, "export", "--to", "crash", env=env)
    assert again.returncode == 1 and "crash: FAILED: RuntimeError" in again.stdout


def test_a_quick_sink_that_hangs_costs_the_end_of_a_run_at_most_the_limit(tmp_path, monkeypatch):
    release = threading.Event()
    P.register("sinks", "stuck", X.Sink(send=lambda *a: release.wait(30) and "late"))
    root = project(tmp_path / "proj", export={"stuck": True, "jsonl": True})
    from test_export import fake_run
    run = fake_run(root)
    monkeypatch.setattr(X, "FOREGROUND_LIMIT", 1)
    began = time.time()
    try:
        X.after_run(root, run)
        took = time.time() - began
    finally:
        release.set()
    log = text(run / "export.log")
    assert took < 5 and "stuck: failed - no outcome after 1 s at the end of the run, so the run went on" in log, log
    assert "background = True" in log


# ---- notifiers: the desktop, and a plug-in installed as pip installs it --------------------------------------------

def desktop(folder: Path, shown: Path, code: int = 0) -> Path:
    """notify-send (and osascript, for macOS) faked: each writes its arguments to `shown` and exits with `code`."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("notify-send", "osascript"):
        fake = folder / name
        complaint = "echo 'Cannot autolaunch D-Bus without X11 $DISPLAY' >&2\n" if code else ""
        fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> '{shown}'\n{complaint}exit {code}\n")
        fake.chmod(0o755)
    return folder


def configure(root: Path, notify: dict) -> None:
    cfg = yaml.safe_load((root / "testbench.yaml").read_text())
    write_config(root, {**cfg, "notify": notify})


@pytest.mark.posix
def test_the_desktop_notifier_with_its_command_faked_on_path(repo, tmp_path, webhook):
    shown = tmp_path / "shown.txt"
    path = {"PATH": str(desktop(tmp_path / "bin", shown)) + os.pathsep + os.environ["PATH"]}
    configure(repo, {"desktop": True})
    r = cli(repo, "run", "score", env=path)
    assert r.returncode == 0, r.stdout + r.stderr
    said = until(lambda: "PASSED" in text(shown) and text(shown))
    assert "agent-testbench" in said and "scoring: run " in said and "of plan score on local PASSED" in said
    log = text(repo / ".testbench" / "notify.log")
    assert re.search(r"run_end to this machine's desktop \((notify-send|osascript)\): accepted \(exit 0\)", log), log
    t = cli(repo, "notify", "test", env=path)
    assert t.returncode == 0 and "Test message to this machine's desktop (" in t.stdout, t.stdout
    assert "a test message from `testbench notify test`" in text(shown)

    configure(repo, {"webhook": webhook.url, "desktop": True, "on": ["run_end"]})     # both, at once
    t = cli(repo, "notify", "test", env=path)
    assert t.returncode == 0 and re.search(r"to http://127\.0\.0\.1:\d+ \(notify.webhook in testbench.yaml\): "
                                           r"accepted \(HTTP 200\); to this machine's desktop", t.stdout), t.stdout
    assert [b["event"] for b in webhook.bodies] == ["test"] and Webhook.SECRET not in t.stdout

    failing = {"PATH": str(desktop(tmp_path / "failing", tmp_path / "failed.txt", code=1)) + os.pathsep
               + os.environ["PATH"]}
    configure(repo, {"desktop": True})
    r = cli(repo, "run", "score", env=failing)
    assert r.returncode == 0, "a desktop that cannot show it does not fail the run"
    assert until(lambda: "not accepted (exit 1: Cannot autolaunch D-Bus" in text(repo / ".testbench" / "notify.log"))
    t = cli(repo, "notify", "test", env=failing)
    assert t.returncode == 1 and "Each part above says what went wrong" in t.stdout, t.stdout


def test_the_desktop_notifier_where_there_is_no_way_to_show_one(repo, tmp_path, monkeypatch):
    configure(repo, {"desktop": True})
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    began = time.time()
    ok, what = N.send(repo, C.load(repo), "run_end", "a run ended", {"status": "passed"})
    assert ok is False and what.startswith("not sent: no desktop notification on this machine: ") and \
        time.time() - began < 2, what
    log = text(repo / ".testbench" / "notify.log")
    assert "run_end: nothing sent - no desktop notification on this machine" in log
    assert "Leave desktop out of notify: where nobody watches a screen" in log
    t = cli(repo, "notify", "test", env={"PATH": str(tmp_path / "empty")})
    assert t.returncode == 2 and "nothing was sent: no desktop notification on this machine" in t.stdout, t.stdout


BROKEN_NOTIFIERS = """\
import time
from agent_testbench.notify import Notifier


class Crash(Notifier):
    def send(self, event, text, fields, opts, timeout):
        raise RuntimeError("the pager is down")


class Hang(Notifier):
    def send(self, event, text, fields, opts, timeout):
        time.sleep(60)
        return True, "late"
"""


def test_the_example_notifier_installed_like_pip_does_hears_every_run(repo, tmp_path, webhook):
    env = install_example(tmp_path / "site", "testbench-notifier-example")
    events = tmp_path / "events.jsonl"
    configure(repo, {"events": {"path": str(events)}})
    assert cli(repo, "run", "score", env=env).returncode == 0
    sent = until(lambda: [json.loads(line) for line in text(events).splitlines()])
    assert sent[0]["event"] == "run_end" and sent[0]["status"] == "passed" and sent[0]["project"] == "scoring"
    assert sent[0]["text"].startswith("agent-testbench, scoring: run ")
    assert f"run_end to {events}: accepted (written)" in text(repo / ".testbench" / "notify.log")

    configure(repo, {"events": {"url": webhook.url}, "on": ["run_end"]})
    assert cli(repo, "run", "score", env=env).returncode == 0
    [posted] = until(lambda: webhook.events("run_end"))
    assert posted["path"] == Webhook.SECRET and posted["status"] == "passed"
    t = cli(repo, "notify", "test", env=env)
    assert t.returncode == 0 and re.search(r"Test message to http://127\.0\.0\.1:\d+: accepted \(HTTP 200\)\.",
                                           t.stdout), t.stdout
    log = text(repo / ".testbench" / "notify.log")
    assert "SECRET" not in log + t.stdout, "a URL's path is never shown"


def test_a_broken_plugin_notifier_only_leaves_a_line_in_notify_log(repo, tmp_path, webhook):
    env = install(tmp_path / "site", "testbench-broken-notifiers", {"broken_notifiers": BROKEN_NOTIFIERS}, {
        "agent_testbench.notifiers": {"crash": "broken_notifiers:Crash", "hang": "broken_notifiers:Hang",
                                      "gone": "no_such_module:Notifier"}})
    configure(repo, {"webhook": webhook.url, "crash": True, "hang": True, "gone": True})
    began = time.time()
    r = cli(repo, "run", "score", env=env)
    assert r.returncode == 0 and time.time() - began < 60, r.stdout + r.stderr
    log = until(lambda: "hang" in text(repo / ".testbench" / "notify.log") and text(repo / ".testbench" / "notify.log"))
    assert webhook.events("run_end")[0]["status"] == "passed", "the webhook still hears it"
    assert "run_end to crash: not accepted (RuntimeError: the pager is down)" in log
    assert "run_end to hang: not accepted (no answer within 5 s)" in log
    assert re.search(r"run_end: nothing sent - notifier 'gone' is installed but did not load", log), log
    t = cli(repo, "notify", "test", env=env)
    assert t.returncode == 1 and "accepted (HTTP 200)" in t.stdout and "Each part above says what went wrong" in \
        t.stdout, t.stdout


def test_the_example_plugins_keep_their_contracts(monkeypatch):
    for name in ("testbench-sink-example", "testbench-notifier-example"):
        monkeypatch.syspath_prepend(str(EXAMPLES / "plugins" / name))
    import testbench_notifier_example
    import testbench_sink_example
    assert P.check("sinks", testbench_sink_example.MarkdownSink) == []
    assert P.check("notifiers", testbench_notifier_example.EventsNotifier) == []
    for folder in (EXAMPLES / "plugins").iterdir():
        code = next(folder.glob("*.py")).read_text()
        assert len(code.splitlines()) <= 50, f"{folder.name} stays a short example"
        assert all(len(line) <= 120 for line in code.splitlines()), folder.name
        assert (folder / "README.md").is_file() and (folder / "pyproject.toml").is_file()
