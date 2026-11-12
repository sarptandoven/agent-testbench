"""testbench check: a first proven result in about a minute, with or without a testbench.yaml.

With a testbench.yaml (here or in a parent folder) it runs every plan that is free and may launch without approval,
and lists each other plan with its worst case and the command a person runs for it: nothing that costs money starts.
With none, it runs here the plans `testbench init` would write for what it finds (scaffold.detect, so the plan names
are the same and after `init` their history goes on), on this machine, and keeps what it found in
.testbench/check.json: `testbench report`, `runs`, `verify`, `ui` and `diff` then work in the folder (config.checked).
It writes no testbench.yaml, AGENTS.md or ~/.testbench/limits.yaml.

A plan that needs what this machine lacks (Node.js, Go, cargo-nextest, pytest, the packages a notebook imports,
Chromium for web steps, a running Docker) is skipped with the command that installs it. Every run goes through
runs.launch, as `testbench run` does: the budget check, the ledgers, the launch lock and a full proof record.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import budget, config as C, procs, runs

# a detected test plan, and the recipes `init` takes to keep it and also run the suite in a container with no network
SANDBOX_RECIPES = {"tests": "pytest --recipe docker-sandbox", "jest-tests": "jest", "vitest-tests": "vitest",
                   "go-tests": "go-test", "cargo-tests": "cargo-test"}
TOOLS = {"npm": "node", "npx": "node", "node": "node", "go": "go", "cargo": "cargo"}
RUSTUP = "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
INSTALL = {   # what a plan's command needs, where it comes from, and the command that installs it on each system
    "node": ("Node.js: https://nodejs.org", {"darwin": "brew install node", "win32": "winget install OpenJS.NodeJS.LTS",
                                             "linux": "sudo apt-get install -y nodejs npm"}),
    "go": ("Go: https://go.dev/dl/", {"darwin": "brew install go", "linux": "sudo apt-get install -y golang-go",
                                      "win32": "winget install GoLang.Go"}),
    "cargo": ("Rust: https://rustup.rs", {"darwin": RUSTUP, "linux": RUSTUP,
                                          "win32": "winget install Rustlang.Rustup"}),
}
VERDICT = {"passed": "PASS", "failed": "FAIL", "error": "ERROR", "cancelled": "CANCELLED", "running": "STILL RUNNING"}


def _install(tool: str) -> tuple[str, str]:
    what, how = INSTALL[tool]
    return what, how.get(sys.platform, how["linux"])


def _commands(line: str) -> list[list[str]]:
    """The words of each simple command in a shell line (leading VAR=value assignments dropped); what does not
    split cleanly is left out, and the run itself says what is wrong with it."""
    out = []
    for part in re.split(r"&&|\|\||[;|\n]", line or ""):
        try:
            words = shlex.split(part, comments=True)
        except ValueError:
            continue
        while words and re.fullmatch(r"[A-Za-z_]\w*=.*", words[0]):
            words = words[1:]
        if words:
            out.append(words)
    return out


def _imports(python: str, module: str) -> bool:
    if Path(python) == Path(sys.executable):
        return importlib.util.find_spec(module) is not None
    try:
        return subprocess.run([python, "-c", f"import {module}"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _pip(python: str, packages: list[str]) -> str:
    """The command that installs packages for that Python: into testbench's own environment when that is the one
    (uvx, uv tool, pipx), else with that Python's pip (uv's, for a venv uv made: those have no pip)."""
    from .executor import tool_environment
    kind = tool_environment() if Path(python) == Path(sys.executable) else None
    q = subprocess.list2cmdline if os.name == "nt" else shlex.join
    withs = " ".join(f"--with {q([p])}" for p in packages)
    if kind == "uvx":
        return f"uvx {withs} agent-testbench check"
    if kind == "uv tool":
        return f"uv tool install --force {withs} agent-testbench"
    if kind == "pipx":
        return f"pipx inject agent-testbench {q(packages)}"
    cfg = Path(python).parent.parent / "pyvenv.cfg"
    if cfg.is_file() and re.search(r"(?m)^uv\s*=", cfg.read_text(errors="replace")):
        return f"uv pip install --python {q([python])} {q(packages)}"
    return f"{q([python])} -m pip install {q(packages)}"


def _notebook_needs(notebook: Path, root: Path) -> tuple[str, str] | None:
    """The packages a notebook imports that the Python its kernel runs lacks (rescue reads the notebook for them)."""
    from . import rescue
    try:
        info = rescue.read(notebook, root)
    except Exception:
        return None                                  # the run says what is wrong with it
    missing = [p for p in info["packages"] if not p["installed"] and not p["guarded"]
               and not (p["notebook_installs"] and p["notebook_installs"] <= p["cells"][0])]
    if not missing:
        return None
    python, guess = rescue.kernel_python(), [p["import"] for p in missing if not p["sure"]]
    return (f"{notebook.name} imports {', '.join(p['import'] for p in missing)}, which the Python its kernel runs "
            f"({python}) does not have" + (f" (the pip name of {', '.join(guess)} is a guess)" if guess else ""),
            _pip(python, list(dict.fromkeys(p["pip"] for p in missing if p["pip"]))))


def _command_needs(words: list[str], root: Path, path: str) -> tuple[str, str] | None:
    exe = Path(words[0]).name
    if exe in TOOLS and not shutil.which(exe, path=path):
        where, how = _install(TOOLS[exe])
        return f"`{exe}` is not on PATH ({where})", how
    if exe in ("npm", "npx") and (root / "package.json").is_file() and not (root / "node_modules").is_dir():
        return "the project's npm packages are not installed (no node_modules folder)", "npm install"
    if any("jest-junit" in w for w in words) and not (root / "node_modules" / "jest-junit").is_dir():
        from .scaffold import JS_TESTS
        return "jest-junit, the reporter that writes the JUnit report the plan reads, is not installed", \
            f"npm install --save-dev {JS_TESTS['jest'][5]}"
    if exe == "cargo" and words[1:2] == ["nextest"]:
        cargo_bin = Path(os.environ.get("CARGO_HOME") or Path.home() / ".cargo") / "bin"
        if not shutil.which("cargo-nextest", path=path) and not shutil.which("cargo-nextest", path=str(cargo_bin)):
            return "cargo-nextest, which runs the tests and writes their JUnit report, is not installed", \
                "cargo install cargo-nextest --locked"
        profile = next((w.split("=", 1)[1] for w in words if w.startswith("--profile=")), None) or \
            next((words[i + 1] for i, w in enumerate(words[:-1]) if w == "--profile"), None)
        toml = root / ".config" / "nextest.toml"
        text = toml.read_text(errors="replace") if toml.is_file() else ""
        if profile and profile != "default" and not re.search(rf"(?m)^\[profile\.{re.escape(profile)}\.junit\]", text):
            lines = f"[profile.{profile}]\\nfail-fast = false\\n[profile.{profile}.junit]\\npath = \"junit.xml\"\\n"
            return (f"cargo-nextest has no profile {profile} with a JUnit report (.config/nextest.toml)",
                    f"mkdir -p .config && printf '{lines}' >> .config/nextest.toml")
    if exe == "pytest" and not shutil.which("pytest", path=path):
        return "pytest is not on PATH", _pip(shutil.which("python", path=path) or sys.executable, ["pytest"])
    if re.fullmatch(r"python[\d.]*", exe):
        python = shutil.which(exe, path=path)
        if python is None:
            venv = r".venv\Scripts\activate" if os.name == "nt" else ". .venv/bin/activate"
            return f"there is no `{exe}` on PATH", f"python3 -m venv .venv && {venv}"
        if words[1:3] == ["-m", "pytest"] and not _imports(python, "pytest"):
            return f"pytest is not installed for {python}", _pip(python, ["pytest"])
    return None


def _web_needs() -> tuple[str, str] | None:
    from . import web
    from .executor import tool_environment
    if importlib.util.find_spec("playwright") is None:
        why = "Playwright, which web steps drive Chromium with, is not installed for the Python that runs testbench"
        if tool_environment() == "uvx":
            return why, ('uvx --from "agent-testbench[web]" playwright install chromium && '
                         'uvx --from "agent-testbench[web]" agent-testbench check')
        return why, f"{_pip(sys.executable, ['playwright'])} && {web.install_command()}"
    ok, detail = web.chromium_check()
    if ok:
        return None
    how = re.search(r"\s*(?:Install|Reinstall) (?:it|them) with: (.+?)(?: \(it uses .*)?$", detail)
    return (detail[:how.start()] if how else detail).rstrip(" .;"), how.group(1) if how else ""


def needs(plan: dict, root: Path, seen: dict | None = None) -> tuple[str, str] | None:
    """(what this machine lacks for the plan, the command that installs it), or None. Only what testbench can name:
    the runners `init` writes plans for, Chromium for web steps, and the backend's own tools (a running Docker);
    anything else, the run itself reports. seen keeps the slow answers (a Chromium launch) for one check."""
    seen = {} if seen is None else seen
    if plan["backend"] != "local":
        from . import backends
        if plan["backend"] not in seen:
            try:
                seen[plan["backend"]] = next((f"{name}: {detail}" for name, ok, detail in
                                              backends.get(plan["backend"]).doctor() if not ok), None)
            except Exception as exc:
                seen[plan["backend"]] = f"its backend could not be checked ({exc})"
        return (seen[plan["backend"]], "") if seen[plan["backend"]] else None
    from .executor import step_path
    path = step_path(root)
    for step in plan["steps"]:
        if step["kind"] == "web":
            if "web" not in seen:
                seen["web"] = _web_needs()
            if seen["web"]:
                return seen["web"]
        if step["kind"] == "notebook" and (hit := _notebook_needs(root / step["notebook"], root)):
            return hit
        line = step.get("run") if step["kind"] == "run" else (step["web"].get("serve") if step["kind"] == "web" else "")
        for words in _commands(line or ""):
            hit = _command_needs(words, root, path)
            if hit:
                return hit
    return None


def _about(plan: dict) -> str:
    hw = plan["backend"] + (f" {plan['gpu']}" if plan.get("gpu") else "")
    return plan.get("description") or f"{hw}: " + ", ".join(s["name"] for s in plan["steps"])


def _not_a_project(root: Path) -> str | None:
    if root == Path.home().resolve() or (root / ".testbench").resolve() == budget.user_dir().resolve():
        return (f"{root} is your home folder, where ~/.testbench keeps the limits for every project, not a project. "
                f"Run `{tb()} check` in a project's folder.")
    return None


def _docker_running() -> bool:
    try:
        from . import backends
        return all(ok for _, ok, _ in backends.get("docker").doctor())
    except Exception:
        return False


def _summary(root: Path, mode: str | None, entries: list, began: float, message: str | None = None,
             why: str | None = None) -> dict:
    """What `testbench check --json` prints. why: what kept it from checking anything (home, nothing, no plans,
    no such plan), for the next step."""
    count = lambda *s: [e for e in entries if e["status"] in s]                     # noqa: E731
    passed, failed, running = count("passed"), count("failed", "error", "cancelled"), count("running")
    skipped, refused, not_run = count("skipped"), count("refused"), count("not run")
    ran = passed + failed + running
    code = 1 if failed else 3 if running else 2 if refused or not ran else 0
    config = root / (C.CONFIG_NAME if mode == "config" else C.CHECKED)
    return {"root": str(root), "config": str(config) if mode else None,
            "found_by_check": mode == "found", "plans": entries, "passed": len(passed), "failed": len(failed),
            "skipped": len(skipped), "refused": len(refused), "not_run": len(not_run), "still_running": len(running),
            "runs": [e["run"] for e in entries if e["run"]], "seconds": round(time.time() - began, 1),
            "message": message, "next": _next(mode, entries, why), "exit_code": code}


def tb() -> str:
    """testbench as it was started: `uvx agent-testbench` from uvx (which puts no `testbench` on PATH)."""
    from .executor import tool_environment
    return "uvx agent-testbench" if tool_environment() == "uvx" else "testbench"


def _next(mode: str | None, entries: list, why: str | None) -> str:
    """The single most useful thing to do next in this folder."""
    me = tb()
    if why == "home":
        return f"cd into a project's folder, then `{me} check`."
    if why == "nothing":
        return f"`{me} init` writes testbench.yaml with a starter plan; `{me} recipes` lists ready plans for goals."
    if why == "no plans":
        return f"add a plan to testbench.yaml (`{me} recipes` has ready ones), then `{me} check`."
    if why == "no such plan":
        return f"`{me} check --plan NAME` with a plan named above, or `{me} check` for every plan."
    failed = [e for e in entries if e["status"] in ("failed", "error", "cancelled")]
    if failed:
        e = failed[0]
        return (f"fix what the report of {e['plan']} names (`{me} report {e['run']}` shows it again), then "
                f"`{me} check` again.")
    running = [e for e in entries if e["status"] == "running"]
    if running:
        return f"`{me} wait {running[0]['run']}` follows it to the end and prints its report."
    passed = [e for e in entries if e["status"] == "passed"]
    if not passed:
        skipped = [e for e in entries if e["status"] == "skipped" and e.get("install")]
        refused = [e for e in entries if e["status"] == "refused"]
        paid = sorted((e for e in entries if e["status"] == "not run"), key=lambda e: e["worst_case_usd"])
        if skipped:
            e = skipped[0]
            if e["install"].endswith(" agent-testbench check"):        # uvx: the check again, with what it needs
                return f"`{e['install']}` runs the check again with what {e['plan']} needs."
            return f"install what {e['plan']} needs: `{e['install']}`, then `{me} check` again."
        if refused:
            return f"read why {refused[0]['plan']} was refused, above; `{me} spend` shows what was spent today."
        if paid:
            return (f"`{me} estimate {paid[0]['plan']}` shows the cheapest plan's worst case; a person decides "
                    f"whether to spend it, then runs `{me} run {paid[0]['plan']}`.")
        return f"`{me} check` again once what is said above is done."
    if mode == "found":
        recipe = next((SANDBOX_RECIPES[e["plan"]] for e in passed if e["plan"] in SANDBOX_RECIPES), None)
        if recipe and _docker_running():
            return (f"`{me} init --recipe {recipe}` keeps these plans in testbench.yaml and adds one that runs "
                    "the tests again in a fresh Docker container with no network.")
        return (f"`{me} init` keeps these plans in testbench.yaml (the same names, so their history goes on) and "
                "marks where to add the checks that mean \"working\" (the FILL IN lines).")
    return f"`{me} ui --open` shows every run as a page, with its evidence."


def _entry(name: str, what: str, plan: dict) -> dict:
    return {"plan": name, "what": what, "backend": plan["backend"], "gpu": plan.get("gpu"), "status": None,
            "run": None, "seconds": None, "worst_case_usd": None, "why": None, "install": None, "command": None,
            "report": None}


def _classify(cfg: dict, root: Path, e: dict, seen: dict) -> None:
    """Whether the plan may run here: free, allowed without approval, and with what it needs; else why not."""
    name = e["plan"]
    est = budget.worst_case(cfg, name)
    ok, message, _ = budget.check_launch(cfg, name, approve=False)
    e["worst_case_usd"] = cost = est["worst_case_usd"]
    hw = e["backend"] + (f" {e['gpu']}" if e["gpu"] else "")
    if cost != 0:                                        # NaN too: what cannot be priced is never free
        e.update(status="not run", command=f"{tb()} run {name}")
        why = (f"its worst case cannot be priced (`{tb()} estimate {name}` says why)" if not C.number(cost) else
               f"it can cost up to ${cost:.2f} on {hw}"
               + (", which needs a person's approval" if message.startswith("Needs approval") else
                  ", over the budget" if not ok else ""))
        e["why"] = f"{why}; a person runs it with `{e['command']}`"
    elif not ok:
        e.update(status="refused", why=message)
    elif hit := needs(cfg["plans"][name], root, seen):
        e.update(status="skipped", why=hit[0], install=hit[1] or None)


def _follow(run_dir: Path, seconds: float) -> dict:
    """The run's state once it has ended, or after `seconds` (a run whose driver died is closed by runs.settle)."""
    end = time.time() + seconds
    while True:
        state = runs.settle(run_dir)
        if state.get("status") in runs.DONE or time.time() >= end:
            return state
        time.sleep(0.25)


def run(start: str | Path = ".", names: list[str] | None = None, show=None) -> dict:
    """Check the project at or above `start`, found as every command finds it (config.find_root: a testbench.yaml, or
    a folder check ran in before); with neither, `start` itself. show(event, x) is told the mode ("intro", text) and
    each plan as it starts ("start", entry) and once it is settled ("end", entry). Returns the summary
    `testbench check --json` prints; its exit_code is the command's."""
    began, show = time.time(), show or (lambda event, x: None)
    here = Path(start).resolve()
    try:
        root = C.find_root(here)
    except C.ConfigError:
        root = here
    record = None
    if (root / C.CONFIG_NAME).is_file():
        cfg, mode = C.load(root), "config"
        about = {name: _about(plan) for name, plan in cfg["plans"].items()}
        show("intro", f"{C.CONFIG_NAME} in {root}: each plan that is free and needs no approval runs now; nothing "
                      "that costs money starts.")
    else:
        mode = "found"
        refusal = _not_a_project(root)
        if refusal:
            return _summary(root, None, [], began, refusal, "home")
        from .scaffold import detect, project_name
        found = detect(root)
        if not found:
            return _summary(root, None, [], began, (
                f"Nothing to check in {root}: no {C.CONFIG_NAME}, and none of what testbench looks for - notebooks "
                "(.ipynb, or .py with # %% cells), a test suite (pytest; npm test with jest, vitest, mocha, node "
                "--test or Playwright; go test; cargo), a training script (train.py, ...) or a web app (index.html, "
                f"npm run dev). Run `{tb()} check` in the project's folder."), "nothing")
        record = {"by": "testbench check", "time": dt.datetime.now().isoformat(timespec="seconds"),
                  "about": f"What `testbench check` found and ran here, where there is no {C.CONFIG_NAME}: the plans "
                           f"`testbench init` would write. `testbench init` writes them to {C.CONFIG_NAME} (the same "
                           "names), which then takes over.",
                  "project": project_name(root), "found": {name: what for name, _, what in found},
                  "plans": "".join(text for _, text, _ in found)}
        cfg = C.checked(root, record)
        about = {name: what.split("; it needs ")[0] for name, _, what in found}
        show("intro", f"No {C.CONFIG_NAME} here: running what `testbench init` would set up, on this machine, for "
                      "free (it writes only .testbench/).")
    if not cfg["plans"]:
        return _summary(root, mode, [], began, f"{root / C.CONFIG_NAME} has no plans.", "no plans")
    picked = list(dict.fromkeys(names or cfg["plans"]))
    unknown = [n for n in picked if n not in cfg["plans"]]
    if unknown:
        return _summary(root, mode, [], began, C.no_plan(cfg, unknown[0]), "no such plan")
    entries, seen = [_entry(n, about[n], cfg["plans"][n]) for n in picked], {}
    for e in entries:
        _classify(cfg, root, e, seen)
    if record is not None and any(e["status"] is None for e in entries):
        marker = root / C.CHECKED
        marker.parent.mkdir(parents=True, exist_ok=True)
        procs.write_text_atomic(marker, json.dumps(record, indent=1) + "\n")
    current = None
    try:
        for e in entries:
            if e["status"] is not None:
                show("end", e)
                continue
            show("start", e)
            t = time.time()
            try:
                current = runs.launch(root, cfg, e["plan"], origin={"check": True})
            except runs.LaunchRefused as exc:
                e.update(status="refused", why=str(exc))
                show("end", e)
                continue
            e.update(run=current.name, report=str(current / "report.md"))
            state = _follow(current, C.plan_minutes(cfg["plans"][e["plan"]]) * 60 + 300)
            e.update(status=state.get("status") if state.get("status") in runs.DONE else "running",
                     seconds=round(time.time() - t, 1))
            current = None
            show("end", e)
    except KeyboardInterrupt:
        if current is not None:
            runs.cancel(current)
            e.update(status="cancelled", seconds=round(time.time() - t, 1))
            show("end", e)
        for rest in entries:
            if rest["status"] is None:
                rest.update(status="skipped", why="testbench check was stopped (Ctrl-C) before it ran")
    return _summary(root, mode, entries, began)


def line(e: dict) -> str:
    """One plan as `testbench check` prints it."""
    return f"{e['plan']}: {e['what']} ... {outcome(e)}"


def outcome(e: dict) -> str:
    """How a plan came out: PASS (2.1 s), or why it did not run and what to do."""
    if e["status"] in VERDICT:
        return f"{VERDICT[e['status']]} ({e['seconds']:.1f} s)"
    if e["status"] == "refused":
        return e["why"]
    return f"{e['status']}: {e['why'].rstrip(' .;')}" + (f". Install: `{e['install']}`" if e.get("install") else "")


def _junit(path: str, s: dict) -> bool:
    """--junit FILE: each run's steps as test cases, and each plan that did not run with why (ci.junit)."""
    from .ci import write_junit
    entries = [{**e, "run_dir": Path(e["report"]).parent if e["run"] and e.get("report") else None}
               for e in s["plans"]]
    return write_junit(path, entries, s["message"])


def main(names: list[str] | None = None, as_json: bool = False, junit: str | None = None) -> int:
    tty, started = sys.stdout.isatty() and not as_json, set()

    def show(event, x):
        if as_json:
            return
        if event == "intro":
            print(x, flush=True)
        elif event == "start" and tty:
            print(f"{x['plan']}: {x['what']} ...", end=" ", flush=True)
            started.add(x["plan"])
        elif event == "end":
            print(outcome(x) if x["plan"] in started else line(x), flush=True)

    try:
        s = run(".", names, show)
    except C.ConfigError as exc:                  # a testbench.yaml that does not load: the CI shows why too
        if junit:
            _junit(junit, {"plans": [], "message": str(exc)})
        raise
    if junit and not _junit(junit, s):
        junit = None
    if as_json:
        print(json.dumps(s, indent=1))
        return s["exit_code"]
    if s["message"]:
        print(s["message"])
    for e in s["plans"]:
        if e["status"] in ("failed", "error", "cancelled") and e["run"]:
            md = Path(e["report"])
            print("\n" + (md.read_text() if md.is_file() else runs.status_line(md.parent)).rstrip())
    if s["plans"]:
        kinds = (("passed", "passed"), ("failed", "failed"), ("still_running", "still running"), ("skipped", "skipped"),
                 ("refused", "refused"), ("not_run", "not run"))
        bits = [f"{s[k]} {w}" for k, w in kinds if s[k] or k in ("passed", "failed")]
        print(f"\n{C.counted(len(s['plans']), 'plan')}: {', '.join(bits)}, in {s['seconds']:.0f} s.")
    ids = s["runs"]
    if ids:
        whose = "its" if len(ids) == 1 else "a run's"
        print(f"Run{'s' * (len(ids) > 1)} {', '.join(ids)}: `{tb()} verify {ids[0]}` checks {whose} evidence "
              "against the sha256 its report recorded.")
    if junit:
        print(f"JUnit results: {junit}")
    print(f"Next: {s['next']}")
    return s["exit_code"]
