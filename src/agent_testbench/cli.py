"""testbench - a test bench your coding agent operates. See `testbench <command> -h`.

Exit codes: 0 passed / done, 1 failed, 2 refused or misconfigured, 3 still running.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import __version__, budget, config as C, lint as L, notebook as NB, report as R, runs

INIT_TEMPLATE = """\
# agent-testbench: what the agent may run, where, and for how much. Reference: https://github.com/sarptandoven/agent-testbench
project: {project}

budget:
  max_run_usd: 2.00      # refuse any run that could cost more than this
  ask_above_usd: 0.50    # above this, a person must approve (testbench run --approve)
  max_day_usd: 10.00     # all runs today together

sessions:                # where `testbench session start` puts its kernel by default
  backend: local         # local | modal | colab
  idle_minutes: 10       # a session stops itself after this long unused
  max_minutes: 60        # and after this long in any case (bounds its cost)

# modal:                 # image for Modal plans and sessions
#   python: "3.11"
#   pip: [numpy==2.2.6]
#   cpu: 2
#   memory_gb: 8

plans:
{plans}"""

EMPTY_PLAN = """\
  tests:
    backend: local
    steps:
      - name: tests
        run: python -m pytest -q
        max_minutes: 10
"""

AGENTS_SECTION = """\
<!-- agent-testbench -->
## Working here (agent-testbench)

This project uses agent-testbench. Change things, then prove them on real hardware - never with "it should work".

1. Explore in a live session: `testbench session start` (add `--backend modal --gpu T4` or `--backend colab`),
   then `testbench exec "..."` or `testbench exec --cells nb.ipynb:1-3`. Variables persist between calls; images
   come back as files; errors name the line. `testbench session stop` as soon as you are done.
2. Prove with a plan: `testbench plans` lists them with worst-case cost; `testbench run <plan>` prints a report
   naming the failing cell and line, the missed expectations, and what changed since the previous run.
3. Look at the evidence (plots, screenshots, videos) yourself before calling a visual result good.
4. Fix one thing at a time, rerun, and record what you learned: `testbench note "..."`.
5. Never pass `--approve` yourself: show the person the estimate and wait for a yes. Never raise `budget:` values
   or edit ~/.testbench/limits.yaml. Never print or copy secret values; refer to secrets by NAME. Paid APIs get
   strict mocks in tests.
6. Done means a passing report for the plan that matters. Say which run proves it and what it cost.
<!-- /agent-testbench -->
"""


def _root_cfg():
    root = C.find_root()
    return root, C.load(root)


def _print_report(run_dir: Path) -> None:
    md = run_dir / "report.md"
    print(md.read_text() if md.exists() else runs.status_line(run_dir))


def cmd_init(a):
    root = Path.cwd()
    target = root / C.CONFIG_NAME
    if target.exists() and not a.force:
        print(f"{target} already exists (use --force to replace it)")
        return 2
    from .scaffold import detect
    found = detect(root)
    plans = "".join(text for _, text, _ in found) or EMPTY_PLAN
    project = "".join(c if c.isalnum() or c in "-_" else "-" for c in root.name)[:40].strip("-") or "project"
    target.write_text(INIT_TEMPLATE.format(project=project, plans=plans))
    for name in ("AGENTS.md", "CLAUDE.md"):
        f = root / name
        text = f.read_text() if f.exists() else ""
        if "<!-- agent-testbench" not in text:          # init's section, or the one `testbench connect` writes
            body = AGENTS_SECTION if name == "AGENTS.md" else "See AGENTS.md for how to work in this project (agent-testbench).\n"
            f.write_text((text.rstrip() + "\n\n" if text.strip() else "") + body)
    first = next(iter(C.load(root)["plans"]))
    machine = budget.write_limits_if_missing()
    print(f"Wrote {target.name}" + (":" if found else " with a starter plan (no notebooks, tests, app or training "
                                                         "script found):"))
    for name, _, what in found:
        print(f"  plan {name}: {what}")
    print("and the working rules in AGENTS.md (CLAUDE.md points to it).")
    if machine:
        print(f"Wrote {machine}: limits for every project on this machine, which a project's budget can lower but never "
              "raise (edit it yourself to raise them).")
    print(f"Next: fill in the lines marked FILL IN in {target.name} - the numbers that mean \"working\" - then "
          f"`testbench run {first}`.")
    return 0


def cmd_plans(a):
    root, cfg = _root_cfg()
    lim = budget.limits(cfg)
    print(f"project {cfg['project']}: runs over ${lim['max_run_usd'][0]:.2f} refused, over "
          f"${lim['ask_above_usd'][0]:.2f} need approval, ${budget.spent_today(root):.2f} of "
          f"${cfg['budget']['max_day_usd']:.2f} committed today")
    capped = [where for _, where in lim.values() if "this machine's limit" in where]
    if capped:
        print("capped by this machine: " + "; ".join(w.split(" (")[0] for w in capped))
    print()
    for name, plan in cfg["plans"].items():
        est = budget.worst_case(cfg, name)
        hw = plan["backend"] + (f" {plan['gpu']}" if plan.get("gpu") else "")
        print(f"{name}: {hw}, {len(plan['steps'])} step(s), up to {est['max_minutes']:.0f} min, worst case "
              f"${est['worst_case_usd']:.2f}" + (f" - {plan['description']}" if plan.get("description") else ""))
        for s in plan["steps"]:
            what = s.get("notebook") or s.get("run") or s["web"]["url"]
            print(f"    - {s['name']} ({s['kind']}: {what}, {s['max_minutes']} min)")
    return 0


def cmd_estimate(a):
    root, cfg = _root_cfg()
    if a.plan not in cfg["plans"]:
        print(f"no plan {a.plan!r}", file=sys.stderr)
        return 2
    ok, msg, est = budget.check_launch(cfg, a.plan, approve=a.approve)
    if a.json:
        print(json.dumps({**est, "allowed": ok, "message": msg}))
    else:
        print(msg)
    return 0 if ok else 2


def cmd_run(a):
    root, cfg = _root_cfg()
    try:
        run_dir = runs.launch(root, cfg, a.plan, approve=a.approve, foreground=a.foreground)
    except runs.LaunchRefused as exc:
        print(str(exc))
        return 2
    print(f"[testbench] run {run_dir.name} started ({runs.read_state(run_dir)['budget_message']})", flush=True)
    if a.detach:
        print(f"Follow it with `testbench wait {run_dir.name}`; read it with `testbench report {run_dir.name}`.")
        return 3
    return _follow(run_dir, a.wait)


def _follow(run_dir: Path, timeout: float) -> int:
    state = runs.wait(run_dir, timeout)
    if state.get("status") in runs.DONE:
        print()
        _print_report(run_dir)
        return 0 if state["status"] == "passed" else 1
    print(f"\n[testbench] still running after {timeout:.0f} s: {runs.status_line(run_dir)}\n"
          f"Continue with `testbench wait {run_dir.name}`.")
    return 3


def cmd_wait(a):
    root, _ = _root_cfg()
    return _follow(runs.resolve(root, a.run), a.timeout)


def cmd_status(a):
    root, _ = _root_cfg()
    print(runs.status_line(runs.resolve(root, a.run)))
    return 0


def cmd_runs(a):
    root, _ = _root_cfg()
    for p in runs.all_runs(root)[-a.n:]:
        print(runs.status_line(p))
    return 0


def cmd_report(a):
    root, _ = _root_cfg()
    run_dir = runs.resolve(root, a.run)
    fmt = "json" if a.json else a.format
    rep = R.load(run_dir)
    if fmt == "md" or (rep is None and fmt != "json"):
        _print_report(run_dir)
        return 0
    if fmt == "json":
        text = json.dumps(rep, indent=1) if rep else "{}"
    elif fmt == "junit":
        text = R.render_junit(rep)
    else:
        target = Path(a.output) if a.output else run_dir / "report.html"
        target.write_text(R.render_html(rep, run_dir))
        print(f"wrote {target} (one self-contained page: open it in a browser)")
        return 0
    if a.output:
        Path(a.output).write_text(text)
        print(f"wrote {a.output}")
    else:
        print(text)
    return 0


def cmd_history(a):
    root, cfg = _root_cfg()
    reports = [rep for p in runs.all_runs(root) if (rep := R.load(p)) and rep.get("plan") == a.plan
               and rep.get("status") in runs.DONE]
    if a.plan not in cfg["plans"] and not reports:
        print(f"no plan {a.plan!r} and no runs of it. Plans: {', '.join(cfg['plans'])}")
        return 2
    h = R.history(reports[-a.n:])
    print(json.dumps(h, indent=1) if a.json else R.render_history(a.plan, h))
    return 0


def cmd_prices(a):
    from . import backends, prices
    if a.update:
        try:
            changes = prices.update(a.url or prices.URL)
        except prices.PriceError as exc:
            print(f"testbench: {exc}")
            return 2
        got = prices.cached()
        print(f"price catalog {got.get('version')} kept in {prices.cache_path()}: "
              + ("; ".join(changes) if changes else "no price above the built-in table, so nothing changes")
              + ". (A catalog can raise prices, never lower them.)\n")
    try:
        root, cfg = _root_cfg()
        where = f"project {cfg['project']}"
    except C.ConfigError:
        cfg, where = C.validate({"project": "none"}, Path.cwd()), "no project here (testbench.yaml overrides not shown)"
    got = prices.cached()
    print(f"Prices for worst-case estimates ({where}); catalog: "
          + (f"{got.get('version')}, fetched {got.get('fetched', '?')}" if got else "none fetched (`testbench prices --update`)"))
    for name, b in backends.registry().items():
        lines = b.prices(cfg)
        if lines:
            print(f"{name}\n" + "\n".join(lines))
    return 0


def cmd_ui(a):
    from . import ui
    root, cfg = _root_cfg()
    index = ui.build(root, cfg)
    print(f"wrote {index} (static pages: open it in a browser; each run's page is report.html in its folder)")
    if a.open:
        import webbrowser
        webbrowser.open(index.as_uri())
    return 0


def cmd_verify(a):
    from . import provenance
    root, _ = _root_cfg()
    run_dir = runs.resolve(root, a.run)
    rep = R.load(run_dir)
    if not rep or "evidence" not in rep:
        print(f"{run_dir.name} has no recorded evidence to check (it ran before agent-testbench recorded hashes, or "
              "never finished)")
        return 2
    problems = provenance.verify(run_dir, rep)
    if problems:
        print(f"{run_dir.name}: the evidence no longer matches its report:\n  " + "\n  ".join(problems))
        return 1
    src = rep.get("source") or {}
    print(f"{run_dir.name}: all {len(rep['evidence'])} evidence files match their recorded sha256 "
          f"({rep['evidence_sha256'][:16]}); ran on "
          + (f"commit {src['commit'][:12]}" + (" with uncommitted changes" if src.get("dirty") else "") if src
             else "code outside git"))
    return 0


def cmd_cancel(a):
    root, _ = _root_cfg()
    print(runs.cancel(runs.resolve(root, a.run)))
    return 0


def cmd_spend(a):
    root, cfg = _root_cfg()
    rows = budget.entries(root)
    finished = [e for e in rows if e["event"] == "finish"]
    total = sum(e["usd"] for e in finished)
    print(f"today: ${budget.spent_today(root):.3f} committed (budget ${cfg['budget']['max_day_usd']:.2f}); "
          f"all time: ${total:.3f} over {len(finished)} finished run(s) (estimates from time x price)")
    machine = budget.user_limits()
    print(f"this machine, every project: ${budget.spent_today_everywhere():.3f} committed today"
          + (f" (limit ${machine['max_day_usd']:.2f} in {budget.limits_path()})" if "max_day_usd" in machine
             else f" (no machine-wide day limit; set max_day_usd in {budget.limits_path()})"))
    for e in finished[-a.n:]:
        print(f"  {e['time']}  {e['run']}  {e['backend']}  {e['status']}  {e.get('minutes')} min  ${e['usd']:.3f}")
    if a.modal:
        from .backends.modal_backend import app_name
        start = (rows[0]["time"][:10] if rows else None)
        cmd = [sys.executable, "-m", "modal", "billing", "report", "--json", "--resolution", "d"] + (["--start", start] if start else [])
        try:
            data = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout or "[]")
            usd = sum(float(r["cost"]) for r in data if r.get("description") == app_name(cfg))
            print(f"Modal's own bill for app {app_name(cfg)} since {start}: ${usd:.3f} (Modal bills with a delay of about an hour)")
        except Exception as exc:
            print(f"could not read Modal's bill: {exc}")
    return 0


def cmd_lint(a):
    worst = 0
    for path in a.notebooks:
        findings = L.lint(path)
        print(L.render(path, findings))
        worst = max(worst, 1 if findings else 0)
    return worst if a.strict else 0


def cmd_nb(a):
    if a.nb_cmd == "split":
        files = NB.split(a.notebook, a.cells_dir)
        print(f"wrote {len(files)} cell files to {a.cells_dir}; edit them, then `testbench nb build {a.cells_dir} {a.notebook}`")
    elif a.nb_cmd == "build":
        NB.build(a.cells_dir, a.notebook)
        print(f"built {a.notebook}")
    elif a.nb_cmd == "fields":
        import nbformat
        nb = nbformat.read(a.notebook, as_version=4)
        n = 0
        for c in nb.cells:
            if c.cell_type != "code":
                continue
            n += 1
            for name, value in NB.form_fields(c.source).items():
                print(f"cell {n}: {name} = {value}")
    return 0


def cmd_note(a):
    root, _ = _root_cfg()
    import datetime as dt
    notes = root / ".testbench" / "NOTES.md"
    notes.parent.mkdir(parents=True, exist_ok=True)
    last = runs.all_runs(root)
    ref = f" (after run {last[-1].name})" if last else ""
    with notes.open("a") as f:
        f.write(f"- {dt.datetime.now():%Y-%m-%d %H:%M}{ref}: {' '.join(a.text)}\n")
    print(f"noted in {notes.relative_to(root)}")
    return 0


def cmd_doctor(a):
    import importlib.util as iu
    import shutil
    from . import backends
    checks = [("python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0])]
    for mod, why in (("nbclient", "notebook steps"), ("ipykernel", "notebook steps"), ("playwright", "web steps"),
                     ("mcp", "the MCP server")):
        checks.append((f"{mod} ({why})", iu.find_spec(mod) is not None, "installed" if iu.find_spec(mod) else "missing"))
    checks.append(("ffprobe (media checks)", bool(shutil.which("ffprobe")), shutil.which("ffprobe") or "missing"))
    checks.append(("backends", True, ", ".join(backends.names())))
    for b in backends.registry().values():
        checks += b.doctor()
    for name, why in backends.broken().items():
        checks.append((f"backend {name}", False, why))
    try:
        root = C.find_root()
        C.load(root)
        checks.append(("testbench.yaml", True, str(root / C.CONFIG_NAME)))
    except C.ConfigError as exc:
        checks.append(("testbench.yaml", False, str(exc)))
    for name, ok, detail in checks:
        print(f"{'ok  ' if ok else 'no  '} {name}: {detail}")
    return 0


def _session_cmd(fn):
    """Session commands share their error handling: a SessionError is a finding, not a crash."""
    def wrapped(a):
        from .session_backends import SessionError
        try:
            return fn(a)
        except (SessionError, C.ConfigError, NB.ParamError) as exc:
            print(f"testbench: {exc}")
            return 2
    return wrapped


@_session_cmd
def cmd_session(a):
    from . import sessions as S
    root, cfg = _root_cfg()
    if a.sess_cmd == "start":
        print(f"[testbench] starting session {a.name} ...", flush=True)
        s = S.start(root, cfg, a.name, backend=a.backend, gpu=a.gpu, idle_minutes=a.idle_minutes,
                    max_minutes=a.max_minutes, secrets=a.secret, approve=a.approve)
        print(S.describe(S.sessions_dir(root) / a.name))
        if (s.get("handles") or {}).get("remote_stop"):
            print(f"Hard stop {s['handles']['remote_stop']}.")
        print(f"{s['budget_message']} Work in it with `testbench exec`; it stops by itself after "
              f"{s['idle_minutes']:g} idle minutes - stop it sooner with `testbench session stop {a.name}`.")
        return 0
    if a.sess_cmd == "list":
        found = S.all_sessions(root)
        print("\n".join(S.describe(d) for d in found) if found else "no sessions")
        return 0
    if a.sess_cmd == "stop":
        targets = [d for d in S.all_sessions(root) if S.read(d).get("status") in S.ACTIVE] if a.all else [S.resolve(root, a.name)]
        for d in targets:
            was = S.read(d).get("status")
            s = S.stop(root, cfg, d)
            if was not in S.ACTIVE:
                print(f"{s['name']} was not running ({was}: {s.get('stop_reason', '')})")
                continue
            print(f"{s['name']} stopped after {(s['stopped'] - (s.get('started') or s['created'])) / 60:.1f} min, "
                  f"about ${s.get('cost_usd', 0):.3f}")
        if not targets:
            print("no running sessions")
        return 0
    sdir = S.resolve(root, a.name)
    if a.sess_cmd == "sync":
        print(S.sync(root, cfg, sdir))
    elif a.sess_cmd == "install":
        print(S.install(root, cfg, sdir, a.packages))
    elif a.sess_cmd == "shell":
        print(S.shell(root, cfg, sdir, a.command, a.timeout))
    return 0


def render_exec(res: dict) -> str:
    head = f"[{res.get('label', 'code')} | {res['status']} | {res['seconds']:.1f} s]"
    lines = [head]
    if res.get("stdout", "").strip():
        out = res["stdout"].rstrip()
        cut = out.splitlines()
        lines += cut[-60:] if len(cut) > 60 else cut
    if res.get("stderr", "").strip():
        lines += ["stderr:"] + res["stderr"].rstrip().splitlines()[-20:]
    for d in res.get("displays", []):
        lines += [f"display: {d[:500]}"]
    if res.get("result") is not None:
        lines += [f"=> {res['result'][:3000]}"]
    for img in res.get("images", []):
        lines += [f"image: {img}"]
    e = res.get("error")
    if e:
        lines += [f"ERROR {e.get('ename')}: {e.get('evalue')}"]
        if e.get("line"):
            lines += [f"  at {e['line']}"]
        if res["status"] != "timeout":
            lines += ["  traceback (end):"] + ["    " + ln for ln in (e.get("traceback_tail") or "").splitlines()[-12:]]
    if res["status"] == "timeout":
        lines += ["TIMEOUT: the code was interrupted; the kernel and its variables are still there"]
    return "\n".join(lines)


@_session_cmd
def cmd_exec(a):
    from . import sessions as S
    root, cfg = _root_cfg()
    sdir = S.resolve(root, a.session)
    if a.cells:
        nb, _, spec = a.cells.partition(":")
        results = S.exec_cells(root, cfg, sdir, root / nb, spec or None, json.loads(a.params or "{}"), a.timeout)
    else:
        code = Path(a.file).read_text() if a.file else (a.code if a.code is not None else sys.stdin.read())
        results = [S.exec_code(root, cfg, sdir, code, a.timeout, label=a.file or None)]
    if a.json:
        print(json.dumps(results, indent=1))
    else:
        print("\n\n".join(render_exec(r) for r in results))
    worst = {"ok": 0, "error": 1, "timeout": 3}
    return max(worst.get(r["status"], 1) for r in results)


@_session_cmd
def cmd_files(a):
    from . import sessions as S
    root, cfg = _root_cfg()
    print(S.files(root, cfg, S.resolve(root, a.session), a.op, *a.paths))
    return 0


def cmd_loop(a):
    from . import loop as LP
    root, cfg = _root_cfg()
    try:
        if a.loop_cmd == "start":
            state = LP.start(root, cfg, a.plan, a.metric, a.budget_usd, goal=a.goal, max_hours=a.max_hours,
                             min_delta=a.min_delta, approve=a.approve)
            print(f"[testbench] loop {state['id']} started on branch {state['branch']}; running the baseline "
                  f"(run {state['tries'][0]['run']})", flush=True)
        elif a.loop_cmd == "try":
            state = LP.try_change(root, cfg, " ".join(a.description))
            t = state["tries"][state["pending"]]
            print(f"[testbench] loop try {t['n']} committed as {t['commit'][:9]}; run {t['run']} started (loop spent "
                  f"${LP.spent(state):.3f} of ${state['budget_usd']:.2f})", flush=True)
        elif a.loop_cmd == "status":
            state = LP.judge(root, cfg, LP._current(root))
            print(LP.describe(state))
            return 3 if state["pending"] is not None else 0
        else:
            state, report = LP.stop(root, cfg)
            print(report.read_text() + f"\n(report: {report.relative_to(root)}; back on {state['original_branch']})")
            return 0
        state = LP.follow(root, cfg, state, a.wait)
    except LP.LoopError as exc:
        print(f"testbench: {exc}")
        return 2
    if state["pending"] is not None:
        t = state["tries"][state["pending"]]
        print(f"\n[testbench] try {t['n']} is still running; `testbench wait {t['run']}`, then `testbench loop status`.")
        return 3
    last = state["tries"][-1]
    print("\n" + last["verdict"])
    return 0 if last.get("kept") else 1


DEMO_NEEDS = {"sklearn": "scikit-learn==1.5.2", "matplotlib": "matplotlib==3.9.2"}


def _example(name: str) -> Path:
    """An example shipped in the wheel, or the repository's when running from a checkout."""
    for d in (Path(__file__).parent / "examples" / name, Path(__file__).resolve().parents[2] / "examples" / name):
        if (d / C.CONFIG_NAME).is_file():
            return d
    raise FileNotFoundError(f"the {name} example is not in this installation; reinstall agent-testbench")


def cmd_demo(a):
    import importlib.util as iu
    import shutil
    target = Path(a.dir).resolve()
    if target.exists() and any(target.iterdir()):
        print(f"{target} is not empty; give `testbench demo` a new folder")
        return 2
    missing = [pkg for mod, pkg in DEMO_NEEDS.items() if iu.find_spec(mod) is None]
    if missing:
        print(f"The demo trains a small model, which needs {' and '.join(missing)}: "
              f"pip install {' '.join(missing)} - then run `testbench demo {a.dir}` again.")
        return 2
    shutil.copytree(_example("tiny-model"), target, ignore=shutil.ignore_patterns(".testbench", "outputs", "__pycache__"))
    (target / ".gitignore").write_text("outputs/\n__pycache__/\n")
    if shutil.which("git"):                          # a repository, so `testbench loop` works there too
        for cmd in (["init", "-q"], ["add", "-A"], ["-c", "user.name=testbench demo", "-c", "user.email=demo@localhost",
                                                    "commit", "-qm", "tiny-model demo"]):
            subprocess.run(["git", *cmd], cwd=target, capture_output=True)
    print(f"Copied the tiny-model example to {target}: a Colab-style notebook that trains a small network, and plans "
          "for this machine, Modal and Colab.\n")
    if a.no_run:
        code = 0
    else:
        print("Running its free plan, `smoke` (lint, then a quick train on this machine):\n", flush=True)
        code = subprocess.call([sys.executable, "-m", "agent_testbench", "run", "smoke", "--wait", "300"], cwd=target)
    print(f"""
Next, in {target}:
  testbench session start && testbench exec --cells tiny_model.ipynb:1-3      # work live in a kernel
  testbench exec "model.score(X_test, y_test)" && testbench session stop
  testbench loop start smoke --metric outputs/metrics.json:accuracy --budget-usd 0   # then edit cells/02_settings.py,
  testbench nb build cells tiny_model.ipynb && testbench loop try "what changed"      # rebuild, and try it
  testbench ui                                                                       # every run as a page
Or open it in Claude Code with the plugin and ask: "use testbench loop to raise the smoke plan's accuracy".""")
    return code


def cmd_mcp(a):
    from .mcp_server import main as serve
    serve(http=a.http, host=a.host, port=a.port)
    return 0


def cmd_connect(a):
    from .connect import main as connect
    return connect(a.clients, user=a.user, dry_run=a.dry_run, list_=a.list)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="testbench", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"agent-testbench {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="write testbench.yaml and the working rules (AGENTS.md) for this project")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)
    sub.add_parser("plans", help="list plans, their hardware and worst-case cost").set_defaults(fn=cmd_plans)
    p = sub.add_parser("estimate", help="worst-case cost of a plan and whether it may launch")
    p.add_argument("plan")
    p.add_argument("--approve", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_estimate)
    p = sub.add_parser("run", help="run a plan and print its report")
    p.add_argument("plan")
    p.add_argument("--approve", action="store_true", help="a person approved this run's cost (never set this yourself)")
    p.add_argument("--detach", action="store_true", help="start it and return at once")
    p.add_argument("--wait", type=float, default=540, help="follow for at most this many seconds (default 540)")
    p.add_argument("--foreground", action="store_true", help="run in this process (no background driver)")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("wait", help="follow a run until it ends or the timeout passes")
    p.add_argument("run", nargs="?")
    p.add_argument("--timeout", type=float, default=540)
    p.set_defaults(fn=cmd_wait)
    for name, fn, helptext in (("status", cmd_status, "one line about a run"), ("report", cmd_report, "a run's report"),
                               ("cancel", cmd_cancel, "stop a run, locally and remotely"),
                               ("verify", cmd_verify, "check a run's evidence still matches the hashes in its report")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("run", nargs="?", help="run id or prefix (default: the latest)")
        if name == "report":
            p.add_argument("--format", choices=["md", "json", "html", "junit"], default="md",
                           help="md (default), json, html (a page with the evidence images; written to the run "
                                "folder) or junit (XML for CI)")
            p.add_argument("--json", action="store_true", help="same as --format json")
            p.add_argument("-o", "--output", help="write to this file")
        p.set_defaults(fn=fn)
    p = sub.add_parser("prices", help="the prices estimates use and where each comes from; --update fetches the catalog")
    p.add_argument("--update", action="store_true", help="fetch the price catalog (it can raise prices, never lower them)")
    p.add_argument("--url", default=None, help="fetch the catalog from here instead of the repository")
    p.set_defaults(fn=cmd_prices)
    p = sub.add_parser("ui", help="the project's runs as static pages: .testbench/ui/index.html")
    p.add_argument("--open", action="store_true", help="also open it in the browser")
    p.set_defaults(fn=cmd_ui)
    p = sub.add_parser("history", help="a plan over time: each run's outcome and the numbers its checks found")
    p.add_argument("plan")
    p.add_argument("-n", type=int, default=30, help="the latest N runs (default 30)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_history)
    p = sub.add_parser("runs", help="recent runs")
    p.add_argument("-n", type=int, default=10)
    p.set_defaults(fn=cmd_runs)
    p = sub.add_parser("spend", help="what runs have cost")
    p.add_argument("-n", type=int, default=10)
    p.add_argument("--modal", action="store_true", help="also read Modal's own bill for this project's app")
    p.set_defaults(fn=cmd_spend)
    p = sub.add_parser("lint", help="check notebooks for Colab and headless pitfalls")
    p.add_argument("notebooks", nargs="+")
    p.add_argument("--strict", action="store_true", help="exit 1 when anything is found")
    p.set_defaults(fn=cmd_lint)
    p = sub.add_parser("nb", help="notebooks as plain cell files: split, build, fields")
    nsub = p.add_subparsers(dest="nb_cmd", required=True)
    q = nsub.add_parser("split", help="write a notebook's cells as NN_name.py / .md files")
    q.add_argument("notebook")
    q.add_argument("cells_dir")
    q = nsub.add_parser("build", help="assemble cell files into a notebook")
    q.add_argument("cells_dir")
    q.add_argument("notebook")
    q = nsub.add_parser("fields", help="list a notebook's Colab form fields")
    q.add_argument("notebook")
    p.set_defaults(fn=cmd_nb)
    p = sub.add_parser("note", help="add a line to the project's lab notebook (.testbench/NOTES.md)")
    p.add_argument("text", nargs="+")
    p.set_defaults(fn=cmd_note)
    p = sub.add_parser("session", help="a live kernel on this machine, a Modal GPU or a Colab VM: start, list, stop, sync, install, shell")
    ssub = p.add_subparsers(dest="sess_cmd", required=True)
    q = ssub.add_parser("start", help="start a session (inside the budget)")
    q.add_argument("name", nargs="?", default="main")
    q.add_argument("--backend", help="local, modal, colab, or an installed backend (`testbench doctor` lists them)")
    q.add_argument("--gpu")
    q.add_argument("--idle-minutes", type=float, help="stop after this long without use (default from testbench.yaml)")
    q.add_argument("--max-minutes", type=float, help="stop after this long in any case; bounds the cost")
    q.add_argument("--secret", action="append", default=[], help="a secret NAME to pass in (repeatable)")
    q.add_argument("--approve", action="store_true", help="a person approved this session's cost (never set this yourself)")
    ssub.add_parser("list", help="sessions, their hardware, uptime and cost")
    q = ssub.add_parser("stop", help="stop a session (default: the only running one)")
    q.add_argument("name", nargs="?")
    q.add_argument("--all", action="store_true")
    for verb, helptext in (("sync", "copy the project's files to the session's machine again (variables are kept)"),):
        q = ssub.add_parser(verb, help=helptext)
        q.add_argument("name", nargs="?")
    q = ssub.add_parser("install", help="pip install packages on the session's machine")
    q.add_argument("packages", nargs="+")
    q.add_argument("-s", "--name")
    q = ssub.add_parser("shell", help="run a shell command on the session's machine (nvidia-smi, ls, df ...)")
    q.add_argument("command")
    q.add_argument("-s", "--name")
    q.add_argument("--timeout", type=float, default=120)
    p.set_defaults(fn=cmd_session)
    p = sub.add_parser("exec", help="run code, a file, or notebook cells in a live session and show what came back")
    p.add_argument("code", nargs="?", help="Python code (or use -f, --cells, or stdin)")
    p.add_argument("-s", "--session")
    p.add_argument("-f", "--file", help="a .py file to run")
    p.add_argument("--cells", help="notebook cells, e.g. train.ipynb:3-5 (code cells from 1); train.ipynb alone runs all")
    p.add_argument("--params", help='form fields for --cells, as JSON: \'{"EPOCHS": 2}\'')
    p.add_argument("--timeout", type=float, default=600, help="seconds per cell before it is interrupted (default 600)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_exec)
    p = sub.add_parser("files", help="files on a session's machine: ls [PATH] | get REMOTE [LOCAL] | put LOCAL REMOTE")
    p.add_argument("op", choices=["ls", "get", "put"])
    p.add_argument("paths", nargs="*")
    p.add_argument("-s", "--session")
    p.set_defaults(fn=cmd_files)
    p = sub.add_parser("loop", help="an improvement loop: each try is committed, proved by a plan run, kept or reverted")
    lsub = p.add_subparsers(dest="loop_cmd", required=True)
    q = lsub.add_parser("start", help="start a loop on its own git branch and run the baseline")
    q.add_argument("plan")
    q.add_argument("--metric", required=True, help="FILE:KEY of a number a json expectation of the plan checks, "
                                                    "e.g. outputs/metrics.json:accuracy; or check:SUBSTRING, the "
                                                    "number of the check whose name contains it, e.g. check:val_loss")
    q.add_argument("--budget-usd", type=float, required=True, help="what the whole loop may spend, every run together")
    q.add_argument("--goal", choices=["max", "min"], default="max", help="max: higher is better (default); min: lower")
    q.add_argument("--min-delta", type=float, default=0.0, help="keep a try only if it beats the best by more than this")
    q.add_argument("--max-hours", type=float, default=8.0, help="no new tries after this long (default 8)")
    q.add_argument("--approve", action="store_true", help="a person approved the loop's total budget (never set this yourself)")
    q.add_argument("--wait", type=float, help="follow the baseline run at most this many seconds (default: until it ends)")
    q = lsub.add_parser("try", help="commit the edits as a try, run the plan, keep it or revert it")
    q.add_argument("description", nargs="+", help="what this try changed, in a few words")
    q.add_argument("--wait", type=float, help="follow the run at most this many seconds (default: until it ends)")
    lsub.add_parser("status", help="the loop's tries: kept or reverted, the metric, the cost")
    lsub.add_parser("stop", help="write the loop's report and go back to the branch it started from")
    p.set_defaults(fn=cmd_loop)
    sub.add_parser("doctor", help="check what this machine can run").set_defaults(fn=cmd_doctor)
    p = sub.add_parser("demo", help="copy a small example project to DIR, run its free plan, and show what to try next")
    p.add_argument("dir", nargs="?", default="testbench-demo")
    p.add_argument("--no-run", action="store_true", help="only copy it")
    p.set_defaults(fn=cmd_demo)
    p = sub.add_parser("mcp", help="serve these commands as MCP tools (stdio, or --http)")
    p.add_argument("--http", action="store_true", help="streamable HTTP at http://HOST:PORT/mcp instead of stdio")
    p.add_argument("--host", default="127.0.0.1", help="with --http (default 127.0.0.1: this machine only)")
    p.add_argument("--port", type=int, default=8765, help="with --http (default 8765)")
    p.set_defaults(fn=cmd_mcp)
    p = sub.add_parser("connect", help="set up a coding agent to use testbench: its MCP config and AGENTS.md "
                                       "(claude, cursor, vscode, codex, gemini); --list shows each one's state")
    p.add_argument("clients", nargs="*", metavar="CLIENT", help="claude, cursor, vscode, codex or gemini")
    p.add_argument("--user", action="store_true", help="the client's config for every project of this user, "
                                                       "instead of this project's")
    p.add_argument("--dry-run", action="store_true", help="show what would change; write nothing")
    p.add_argument("--list", action="store_true", help="each client: on this machine?, set up here?, for the user?")
    p.set_defaults(fn=cmd_connect)
    return ap


def _utf8_on_windows(argv) -> None:
    """Windows Python reads and writes text in the legacy code page unless in UTF-8 mode, and reports and logs carry
    whatever programs print. Run once more in UTF-8 mode; everything started from there inherits it."""
    if os.name == "nt" and argv is None and not sys.flags.utf8_mode:
        os.environ["PYTHONUTF8"] = "1"
        sys.exit(subprocess.call([sys.executable, "-X", "utf8", "-m", "agent_testbench", *sys.argv[1:]]))


def main(argv=None):
    _utf8_on_windows(argv)
    a = build_parser().parse_args(argv)
    try:
        sys.exit(a.fn(a))
    except (C.ConfigError, FileNotFoundError, NB.ParamError) as exc:
        print(f"testbench: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
