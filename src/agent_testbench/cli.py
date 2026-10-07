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

PLAN_TEMPLATE = """\
  {name}:
    backend: local
    description: run {nb} top to bottom on this machine
    steps:
      - name: {nb_stem}
        notebook: {nb}
        max_minutes: 20
        # params: {{EPOCHS: 1}}          # Colab form fields (NAME = value  #@param) to override
        # expect:
        #   files: [outputs/metrics.json]
        #   json: {{outputs/metrics.json: {{accuracy: ">= 0.9"}}}}
"""

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
5. Never pass `--approve` yourself: show the person the estimate and wait for a yes. Never print or copy
   secret values; refer to secrets by NAME. Paid APIs get strict mocks in tests.
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
    nbs = [p for p in sorted(root.rglob("*.ipynb")) if ".ipynb_checkpoints" not in p.parts and ".testbench" not in p.parts
           and ".venv" not in p.parts][:5]
    plans = "".join(PLAN_TEMPLATE.format(name=f"{p.stem[:30].replace(' ', '-').replace('.', '-')}-local",
                                         nb=p.relative_to(root).as_posix(), nb_stem=p.stem) for p in nbs) or EMPTY_PLAN
    project = "".join(c if c.isalnum() or c in "-_" else "-" for c in root.name)[:40].strip("-") or "project"
    target.write_text(INIT_TEMPLATE.format(project=project, plans=plans))
    for name in ("AGENTS.md", "CLAUDE.md"):
        f = root / name
        text = f.read_text() if f.exists() else ""
        if "<!-- agent-testbench -->" not in text:
            body = AGENTS_SECTION if name == "AGENTS.md" else "See AGENTS.md for how to work in this project (agent-testbench).\n"
            f.write_text((text.rstrip() + "\n\n" if text.strip() else "") + body)
    first = next(iter(C.load(root)["plans"]))
    print(f"Wrote {target.name}" + (f" with a plan for each of {len(nbs)} notebook(s)" if nbs else "")
          + ", and the working rules in AGENTS.md (CLAUDE.md points to it).\n"
          + f"Next: fill in the plans' expect: blocks, then `testbench run {first}`.")
    return 0


def cmd_plans(a):
    root, cfg = _root_cfg()
    b = cfg["budget"]
    print(f"project {cfg['project']}: runs over ${b['max_run_usd']:.2f} refused, over ${b['ask_above_usd']:.2f} need "
          f"approval, ${budget.spent_today(root):.2f} of ${b['max_day_usd']:.2f} committed today\n")
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
    if a.json:
        print((run_dir / "report.json").read_text() if (run_dir / "report.json").exists() else "{}")
    else:
        _print_report(run_dir)
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
    checks = [("python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0])]
    for mod, why in (("nbclient", "notebook steps"), ("ipykernel", "notebook steps"), ("modal", "Modal plans"),
                     ("playwright", "web steps"), ("mcp", "the MCP server")):
        checks.append((f"{mod} ({why})", iu.find_spec(mod) is not None, "installed" if iu.find_spec(mod) else "missing"))
    checks.append(("ffprobe (media checks)", bool(shutil.which("ffprobe")), shutil.which("ffprobe") or "missing"))
    checks.append(("colab CLI (Colab plans)", bool(shutil.which("colab")), shutil.which("colab") or "uv tool install google-colab-cli"))
    if iu.find_spec("modal"):
        tok = Path.home() / ".modal.toml"
        checks.append(("Modal login", tok.exists() or bool(os.environ.get("MODAL_TOKEN_ID")),
                       "found" if tok.exists() or os.environ.get("MODAL_TOKEN_ID") else "run `modal setup`"))
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
            s = S.stop(root, cfg, d)
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


def cmd_mcp(a):
    from .mcp_server import main as serve
    serve()
    return 0


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
                               ("cancel", cmd_cancel, "stop a run, locally and remotely")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("run", nargs="?", help="run id or prefix (default: the latest)")
        if name == "report":
            p.add_argument("--json", action="store_true")
        p.set_defaults(fn=fn)
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
    q.add_argument("--backend", choices=list(C.BACKENDS))
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
    sub.add_parser("doctor", help="check what this machine can run").set_defaults(fn=cmd_doctor)
    sub.add_parser("mcp", help="serve these commands as MCP tools (stdio)").set_defaults(fn=cmd_mcp)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        sys.exit(a.fn(a))
    except (C.ConfigError, FileNotFoundError, NB.ParamError) as exc:
        print(f"testbench: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
