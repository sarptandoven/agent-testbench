"""testbench - a test bench your coding agent operates. See `testbench <command> -h`.

Exit codes: 0 passed / done, 1 failed, 2 refused or bad input (nothing ran), 3 still running.
"""
from __future__ import annotations

import argparse
import difflib
import functools
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

from . import __version__, budget, config as C, lint as L, notebook as NB, report as R, runs

INIT_TEMPLATE = """\
# yaml-language-server: $schema=""" + C.SCHEMA_URL + """
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

# notify:                # a JSON POST when a run or loop ends or a launch needs approval (`testbench notify test`)
#   webhook_env: TESTBENCH_WEBHOOK      # the environment variable that holds the URL (e.g. a Slack webhook)

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
   strict mocks in tests (for the openai and anthropic SDKs: `mocks: [openai, anthropic]` on the step).
6. Done means a passing report for the plan that matters. Say which run proves it and what it cost.
<!-- /agent-testbench -->
"""


def _root_cfg():
    root = C.find_root()
    return root, C.load(root)


def _print_report(run_dir: Path) -> None:
    md = run_dir / "report.md"
    print(md.read_text() if md.exists() else runs.status_line(run_dir))


def _write_agent_rules(root: Path) -> None:
    for name in ("AGENTS.md", "CLAUDE.md"):
        f = root / name
        text = f.read_text() if f.exists() else ""
        if "<!-- agent-testbench" not in text:          # init's section, or the one `testbench connect` writes
            body = AGENTS_SECTION if name == "AGENTS.md" else "See AGENTS.md for how to work in this project (agent-testbench).\n"
            f.write_text((text.rstrip() + "\n\n" if text.strip() else "") + body)


def _print_machine_limits(machine) -> None:
    if machine:
        print(f"Wrote {machine}: limits for every project on this machine, which a project's budget can lower but never "
              "raise (edit it yourself to raise them).")


def cmd_init(a):
    root = Path.cwd()
    target = root / C.CONFIG_NAME
    for f in [target] + ([] if a.add else [root / "AGENTS.md", root / "CLAUDE.md"]):
        C.own_file(root, f)
    if a.add and not a.recipe:
        print("--add adds a recipe's plans to testbench.yaml, so it needs the recipe: `testbench init --add --recipe "
              "NAME` (`testbench recipes` lists them)")
        return 2
    if a.add and target.exists():
        return _add_recipes(root, target, a.recipe)
    if target.exists() and not a.force:
        chosen = shlex.join(w for r in a.recipe for w in ("--recipe", r))
        print(f"{target} already exists (use --force to replace it"
              + (f", or --add to add the recipe's plans to it): `testbench init --add {chosen}` adds them and changes "
                 f"nothing else; `testbench init {chosen} --force` replaces the file" if a.recipe else
                 "): `testbench plans` shows what it holds; `testbench init --force` writes a new one in its place, "
                 "and `testbench init --add --recipe NAME` adds a recipe's plans to it"))
        return 2
    from .scaffold import detect
    found = detect(root)
    project = C.project_name(root)
    if a.recipe:
        return _init_from_recipes(root, target, project, found, a.recipe)
    plans = "".join(text for _, text, _ in found) or EMPTY_PLAN
    target.write_text(INIT_TEMPLATE.format(project=project, plans=plans))
    _write_agent_rules(root)
    first = next(iter(C.load(root)["plans"]))
    machine = budget.write_limits_if_missing()
    print(f"Wrote {target.name}" + (":" if found else " with a starter plan (no notebooks, tests, app or training "
                                                         "script found):"))
    for name, _, what in found:
        print(f"  plan {name}: {what}")
    if not found:
        print(f"  plan {first}: python -m pytest -q, a starting point")
    print("and the working rules in AGENTS.md (CLAUDE.md points to it).")
    _print_machine_limits(machine)
    if found:
        print(f"Next: fill in the lines marked FILL IN in {target.name} - the numbers that mean \"working\" - then "
              f"`testbench run {first}`.")
    else:
        print(f"Next: make plan {first} in {target.name} run what shows this project works (`testbench recipes` has "
              f"ready plans for common goals), then `testbench run {first}`.")
    return 0


def _print_recipe_plans(cfg: dict, chosen: list[dict]) -> None:
    from . import recipes as RC
    for r in chosen:
        for plan in r["plans"]:
            print(f"  plan {plan}: {RC.describe(cfg, plan)} (recipe {r['name']})")
        for stand_in, own in r["swapped"].items():
            print(f"    runs {own}, found here, where the recipe says {stand_in}")


def _init_from_recipes(root: Path, target: Path, project: str, found: list, wanted: list[str]) -> int:
    """testbench.yaml from recipes, plus the plans init detects that the recipes do not already cover."""
    import yaml
    from . import recipes as RC
    try:
        chosen = [RC.load(name, root) for name in dict.fromkeys(wanted)]
        theirs = {plan: spec for r in chosen for plan, spec in r["plans"].items()}
        covered = RC.subjects(theirs)
        kept, left = [], []
        for name, text, what in found:
            plan = yaml.safe_load("plans:\n" + text)["plans"]
            (left if name in theirs or RC.subjects(plan) & covered else kept).append((name, text, what))
        text, notes = RC.compose(INIT_TEMPLATE.format(project=project, plans="".join(t for _, t, _ in kept)), chosen,
                                 fresh=True)
        cfg = RC.check(text, root)
    except RC.RecipeError as exc:
        print(f"testbench: {exc}")
        return 2
    target.write_text(text)
    _write_agent_rules(root)
    machine = budget.write_limits_if_missing()
    print(f"Wrote {target.name} from recipe{'s' * (len(chosen) > 1)} {', '.join(r['name'] for r in chosen)}:")
    _print_recipe_plans(cfg, chosen)
    for name, _, what in kept:
        print(f"  plan {name}: {what}, found here")
    for name, _, what in left:
        print(f"  found {what}: the recipe's plans already cover it, so it gets no plan of its own")
    for note in notes:
        print(note)
    print("and the working rules in AGENTS.md (CLAUDE.md points to it).")
    _print_machine_limits(machine)
    print(f"Next: fill in the lines marked FILL IN in {target.name} - the commands, paths and numbers that mean "
          f"\"working\" - then `testbench run {next(iter(chosen[0]['plans']))}`, the cheapest plan first.")
    return 0


def _add_recipes(root: Path, target: Path, wanted: list[str]) -> int:
    """A recipe's plans (and the sections the file lacks) added to an existing testbench.yaml; nothing in it changes."""
    from . import recipes as RC
    try:
        C.load(root)
    except C.ConfigError as exc:
        print(f"testbench: {exc}\nA recipe is added only to a {C.CONFIG_NAME} that loads; fix it, then run this again.")
        return 2
    try:
        chosen = [RC.load(name, root) for name in dict.fromkeys(wanted)]
        text, notes = RC.compose(target.read_text(), chosen)
        cfg = RC.check(text, root)
    except RC.RecipeError as exc:
        print(f"testbench: {exc}")
        return 2
    target.write_text(text)
    print(f"Added to {target.name} (the plans, budget and sections already there are unchanged):")
    _print_recipe_plans(cfg, chosen)
    for note in notes:
        print(note)
    print(f"Next: fill in the lines marked FILL IN under the new plans - the commands, paths and numbers that mean "
          f"\"working\" - then `testbench run {next(iter(chosen[0]['plans']))}`.")
    return 0


def cmd_recipes(a):
    from . import recipes as RC
    if a.name:
        try:
            print(RC.load(a.name)["text"].rstrip())
        except RC.RecipeError as exc:
            print(f"testbench: {exc}")
            return 2
        return 0
    try:
        base = C.load(C.find_root())
    except C.ConfigError:
        base = None                                  # no project here (or one that does not load): default prices
    print(RC.listing(base))
    return 0


def cmd_schema(a):
    print(C.SCHEMA.read_text(encoding="utf-8"), end="")
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
        print(C.no_plan(cfg, a.plan), file=sys.stderr)
        return 2
    ok, msg, est = budget.check_launch(cfg, a.plan, approve=a.approve)
    if a.json:
        print(json.dumps({**est, "allowed": ok, "message": msg}))
    else:
        print(msg)
    return 0 if ok else 2


def cmd_check(a):
    from .check import main
    return main(a.plan, as_json=a.json)


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
    run_dir = runs.resolve(root, a.run)
    line = runs.status_line(run_dir)
    print(line)
    status = runs.read_state(run_dir).get("status")                  # the exit code says it too, as `wait` does
    if status == "passed":
        return 0
    return 3 if status in ("queued", "running") and "  lost (" not in line else 1


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
        from .export import summary
        if exports := summary(run_dir):
            print(exports)
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
        print(C.no_plan(cfg, a.plan))
        return 2
    h = R.history(reports[-a.n:])
    print(json.dumps(h, indent=1) if a.json else R.render_history(a.plan, h))
    return 0


def cmd_diff(a):
    from . import diff as D
    root = C.find_root()
    try:
        plans = list(C.load(root)["plans"])
    except C.ConfigError:
        plans = []                                   # a testbench.yaml that does not load still leaves its runs to read
    try:
        old, new = D.pick(root, a.runs, plans)
        d = D.compare(D.load(old), D.load(new), root)
    except D.DiffError as exc:
        print(f"testbench: {exc}")
        return 2
    print(json.dumps(d, indent=1) if a.json else D.render(d).rstrip())
    return 0


def cmd_watch(a):
    from . import watch as W
    root, cfg = _root_cfg()
    return W.watch(root, cfg, a.plan, approve=a.approve, interval=a.interval, debounce=a.debounce,
                   max_runs=a.max_runs)


def cmd_export(a):
    from . import export as X
    root, cfg = _root_cfg()
    if a.to:
        flags = {"mlflow": {"tracking_uri": a.tracking_uri, "experiment": a.experiment},
                 "wandb": {"project": a.project, "entity": a.entity, "mode": a.mode}}.get(a.to, {"path": a.path})
        flags = {k: v for k, v in flags.items() if v is not None} | ({"artifacts": False} if a.no_artifacts else {})
        targets = C.check_export({a.to: {**(cfg["export"].get(a.to) or {}), **flags}})
    else:
        targets = cfg["export"]
    if not targets:
        print("Nothing to export to: testbench.yaml has no `export:` section and no --to was given. Name a target, "
              "e.g. `testbench export --to csv` (a row per run in .testbench/runs.csv; or mlflow, wandb, jsonl), or "
              "add `export: {csv: .testbench/runs.csv}` to testbench.yaml to export after every run (docs/export.md)")
        return 2
    try:
        for t in targets:
            X.require(t)
    except X.Missing as exc:
        print(f"testbench: {exc}")
        return 2
    if a.run == "all":
        dirs = [p for p in runs.all_runs(root) if runs.read_state(p).get("status") in runs.DONE]
    else:
        dirs = [runs.resolve(root, a.run)]
    worst = 0
    for run_dir in dirs:
        for target, ok, msg, code in X.export(root, run_dir, targets, cfg["project"]):
            print(f"{run_dir.name} {target}: " + (msg if ok else f"FAILED: {msg}"))
            worst = max(worst, code)
    if not dirs:
        print("No finished runs to export yet.")
    return worst


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
    if a.serve:
        from . import dashboard
        return dashboard.serve(root, cfg, dashboard.PORT if a.port is None else a.port, a.open)
    if a.port is not None:
        print(f"testbench: --port goes with --serve (the live dashboard): `testbench ui --serve --port {a.port}`",
              file=sys.stderr)
        return 2
    index = ui.build(root, cfg)
    print(f"wrote {index} (static pages: open it in a browser; each run's page is report.html in its folder)")
    if a.open:
        import webbrowser
        webbrowser.open(index.as_uri())
    return 0


def cmd_verify(a):
    from . import provenance
    if a.repo and not a.attestation:
        fix = shlex.join(["testbench", "verify", *([a.run] if a.run else []), "--attestation", "--repo", a.repo])
        print(f"testbench: --repo goes with --attestation (whose workflow should have signed the report): `{fix}`",
              file=sys.stderr)
        return 2
    root, _ = _root_cfg()
    run_dir = runs.resolve(root, a.run)
    rep = R.load(run_dir)
    if not rep or "evidence" not in rep:
        plan = runs.read_state(run_dir).get("plan") or "PLAN"
        print(f"{run_dir.name} has no recorded evidence to check (it ran before agent-testbench recorded hashes, or "
              f"never finished). `testbench status {run_dir.name}` says how it stands; a new run records it: "
              f"`testbench run {plan}`")
        return 2
    problems = provenance.verify(run_dir, rep)
    if problems:
        print(f"{run_dir.name}: the evidence no longer matches its report:\n  " + "\n  ".join(problems)
              + ("\nThe attestation was not checked: the evidence does not match the report it would vouch for."
                 if a.attestation else ""))
        return 1
    src = rep.get("source") or {}
    print(f"{run_dir.name}: all {len(rep['evidence'])} evidence files match their recorded sha256 "
          f"({rep['evidence_sha256'][:16]}); ran on "
          + (f"commit {src['commit'][:12]}" + (" with uncommitted changes" if src.get("dirty") else "") if src
             else "code outside git"))
    if not a.attestation:
        return 0
    code, text = provenance.verify_attestation(run_dir, rep, a.repo or provenance.github_repo(root))
    print(f"{run_dir.name}: {text}")
    return code


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


def cmd_rescue(a):
    from . import rescue
    return rescue.main(a)


def cmd_nb(a):
    if a.nb_cmd in ("split", "build") and a.cells_dir.endswith(".ipynb") and not a.notebook.endswith((".ipynb", ".py")):
        order = [a.cells_dir, a.notebook] if a.nb_cmd == "split" else [a.notebook, a.cells_dir]
        print(f"testbench: the other way round: `testbench nb {a.nb_cmd} {shlex.join(order)}` "
              + ("(the notebook, then the folder for its cells)" if a.nb_cmd == "split" else
                 "(the folder of cell files, then the notebook to write)"))
        return 2
    if a.nb_cmd == "split":
        files = NB.split(a.notebook, a.cells_dir)
        print(f"wrote {len(files)} cell files to {a.cells_dir}; edit them, then `testbench nb build {a.cells_dir} {a.notebook}`")
    elif a.nb_cmd == "build":
        NB.build(a.cells_dir, a.notebook)
        print(f"built {a.notebook}")
    elif a.nb_cmd == "fields":
        nb = NB.read(a.notebook)
        found = NB.fields(nb)
        for f in found:
            kind = "Colab form field" if f["kind"] == "form" else "papermill parameter"
            print(f"cell {f['cell']}: {f['name']} = {f['value']}  ({kind})")
        if not found:
            print(f"{a.notebook}: nothing params can set (no `NAME = value  #@param` form fields, no code cell tagged "
                  "`parameters`)")
        if NB.language(nb) != "python":
            print(f"{a.notebook} is in {NB.language(nb)}: params are set only in Python notebooks; give it values with "
                  "the step's env: instead")
    return 0


def cmd_note(a):
    root, _ = _root_cfg()
    import datetime as dt
    notes = C.own_file(root, root / ".testbench" / "NOTES.md")
    notes.parent.mkdir(parents=True, exist_ok=True)
    last = runs.all_runs(root)
    ref = f" (after run {last[-1].name})" if last else ""
    with notes.open("a") as f:
        f.write(f"- {dt.datetime.now():%Y-%m-%d %H:%M}{ref}: {' '.join(a.text)}\n")
    print(f"noted in {notes.relative_to(root)}")
    return 0


def cmd_selftest(a):
    from .selftest import main
    return main(keep=a.keep)


def cmd_doctor(a):
    import importlib.util as iu
    import shutil
    from . import backends
    checks = [("python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0])]
    for mod, why in (("nbclient", "notebook steps"), ("ipykernel", "notebook steps"), ("playwright", "web steps"),
                     ("mcp", "the MCP server")):
        checks.append((f"{mod} ({why})", iu.find_spec(mod) is not None, "installed" if iu.find_spec(mod) else "missing"))
        if mod == "playwright" and iu.find_spec(mod):
            from .web import chromium_check
            checks.append(("chromium (web steps)", *chromium_check()))
    checks.append(("ffprobe (media checks)", bool(shutil.which("ffprobe")), shutil.which("ffprobe") or "missing"))
    checks.append(("backends", True, ", ".join(backends.names())))
    for b in backends.registry().values():
        checks += b.doctor()
    checks += _plugin_checks()
    try:
        root = C.find_root()
        C.load(root)
        checks.append(("testbench.yaml", True, str(root / C.CONFIG_NAME)) if (root / C.CONFIG_NAME).is_file() else
                      ("testbench.yaml", True, f"none; the plans `testbench check` found, in {root / C.CHECKED}"))
    except C.ConfigError as exc:
        checks.append(("testbench.yaml", False, str(exc)))
    for name, ok, detail in checks:
        print(f"{'ok  ' if ok else 'no  '} {name}: {detail}")
    return 0


def _plugin_checks() -> list[tuple[str, bool, str]]:
    """`testbench doctor`'s lines about plug-ins: what is installed, and each broken one (any kind) with why."""
    from . import plugins as P
    out, installed = [], []
    for row in P.inventory():
        installed += [f"{p['name']} ({row['kind']})" for p in row["installed"] if p["from"]]
        if row.get("error"):
            out.append((f"{row['kind']} plug-ins", False, row["error"]))
        for b in row["broken"]:
            fix = f"; `testbench plugin test {b['package'].split()[0]}` says more" if b["package"] else ""
            out.append((f"{row['one']} {b['name']}", False, f"{b['why']}{fix}"))
        for w in row["waiting"]:
            source = f"{w['from']} ({w['package']})" if w["package"] else w["from"]
            out.append((f"{row['kind']} plug-in {w['name']}", False, f"{source}: this version of testbench does not "
                        f"take {row['kind']} plug-ins yet, so it is not used; upgrade agent-testbench"))
    if installed:
        out.insert(0, ("plug-ins", True, f"{', '.join(installed)} (`testbench plugins` lists them)"))
    return out


def cmd_plugins(a):
    from . import plugins as P
    rows = P.inventory()
    if a.json:
        print(json.dumps({"kinds": rows}, indent=2))
        return 0

    def came(p: dict) -> str:
        return (f"{p['from']}, {p['package']}" if p["package"] else p["from"]) or "added in this process"
    for row in rows:
        print(f"{row['kind']:<10} {row['what'] if row['open'] else 'not yet open to plug-ins'}")
        lines = [("built in", ", ".join(row["builtin"]))] if row["builtin"] else []
        lines += [("error", row["error"])] if row.get("error") else []
        lines += [("installed", f"{p['name']} ({came(p)})") for p in row["installed"]]
        lines += [("broken", f"{b['name']}: {b['why']}" + (f" ({b['package']})" if b["package"] else ""))
                  for b in row["broken"]]
        lines += [("waiting", f"{w['name']} ({came(w)}): not used until this kind takes plug-ins")
                  for w in row["waiting"]]
        for label, text in lines:
            print(f"  {label:<10} {text}")
    print(f"\nA package adds one with an entry point in {P.GROUP.format(kind='KIND')} (e.g. "
          f"{P.GROUP.format(kind='backends')}).\n`testbench plugin test PACKAGE` checks it keeps its kind's contract.\n"
          "How to write one: https://sarptandoven.github.io/agent-testbench/plugins/")
    return 0


def cmd_plugin(a):
    from . import plugins as P
    if a.target and a.installed:
        print(f"testbench: test {a.target} or --installed, not both", file=sys.stderr)
        return 2
    try:
        results = P.conformance(a.target or "", a.kind or "", a.installed)
    except P.PluginError as exc:
        print(f"testbench: {exc}", file=sys.stderr)
        return 2
    failed = [r for r in results if r["problems"]]
    if a.json:
        print(json.dumps({"ok": not failed, "results": results}, indent=2))
        return 1 if failed else 0
    for r in results:
        where = r["source"] + (f", {r['package']}" if r["package"] else "")
        label = f"{r['kind']} {r['name'] or '(no name)'} ({where})"
        if not r["problems"]:
            print(f"ok   {label}: keeps its kind's contract")
            continue
        print(f"FAIL {label}: {C.counted(len(r['problems']), 'problem')}")
        for problem in r["problems"]:
            print(f"     - {problem}")
    if not results:
        print("No plug-ins installed" + (f" for {a.kind}" if a.kind else "") + "; nothing to test.")
    elif len(results) > 1:
        print(f"\n{C.counted(len(results), 'plug-in')} tested: " + (
            f"{len(failed)} {'does' if len(failed) == 1 else 'do'} not keep its kind's contract" if failed
            else "each keeps its kind's contract"))
    return 1 if failed else 0


def _session_cmd(fn):
    """Session commands share their error handling: a SessionError is a finding, not a crash."""
    def wrapped(a):
        from .session_backends import SessionError
        try:
            return fn(a)
        except (SessionError, C.ConfigError, NB.NotebookError) as exc:
            print(f"testbench: {exc}")
            return 2
    return wrapped


@_session_cmd
def cmd_session(a):
    from . import sessions as S
    root, cfg = _root_cfg()
    if getattr(a, "session", None):                  # -s NAME, as exec and files take it
        a.name = a.session
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
        running = [d for d in S.all_sessions(root) if S.read(d).get("status") in S.ACTIVE]
        targets = running if a.all or (not a.name and not running) else [S.resolve(root, a.name)]
        for d in targets:
            was = S.read(d).get("status")
            s = S.stop(root, cfg, d)
            if was not in S.ACTIVE:
                print(f"{s['name']} was not running ({was}: {s.get('stop_reason', '')})")
                continue
            print(f"{s['name']} stopped after {(s['stopped'] - (s.get('started') or s['created'])) / 60:.1f} min, "
                  f"about ${s.get('cost_usd', 0):.3f}")
        if not targets:
            print("no running sessions, so nothing to stop (`testbench session list` shows the stopped ones)")
        return 0
    sdir = S.resolve(root, a.name)
    if a.sess_cmd == "sync":
        print(S.sync(root, cfg, sdir))
    elif a.sess_cmd == "install":
        text = S.install(root, cfg, sdir, a.packages)
        print(text)
        if re.search(r"\(pip exited \d+\)\s*$", text):
            return 1
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
    if a.params and not a.cells:
        print("testbench: --params goes with --cells (it sets a notebook's form fields or papermill parameters)")
        return 2
    try:
        params = json.loads(a.params or "{}")
    except json.JSONDecodeError as exc:
        params = exc
    if not isinstance(params, dict):
        print(f"testbench: --params is a JSON object, e.g. '{{\"EPOCHS\": 2}}'; got {a.params!r}")
        return 2
    sdir = S.resolve(root, a.session)
    if a.cells:
        nb, _, spec = a.cells.partition(":")
        results = S.exec_cells(root, cfg, sdir, root / nb, spec or None, params, a.timeout)
    else:
        if a.file or a.code is not None:
            code = Path(a.file).read_text() if a.file else a.code
        elif sys.stdin is None or sys.stdin.isatty():
            code = ""                                    # a terminal: waiting for typed code would look like a hang
        else:
            code = sys.stdin.read()
        if not code.strip():
            print("testbench: nothing to run. Give exec the code (`testbench exec \"print(1)\"`), a file (-f train.py), "
                  "notebook cells (--cells nb.ipynb:1-3) or code on stdin (`testbench exec < script.py`).")
            return 2
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
    want = {"ls": (0, 1, "ls [PATH]"), "get": (1, 2, "get REMOTE [LOCAL]"), "put": (2, 2, "put LOCAL REMOTE")}[a.op]
    if not want[0] <= len(a.paths) <= want[1]:
        print(f"testbench: files {want[2]} - got {len(a.paths)} path(s). E.g. `testbench files get outputs/model.pt`, "
              "`testbench files put data.csv data/data.csv`.")
        return 2
    root, cfg = _root_cfg()
    print(S.files(root, cfg, S.resolve(root, a.session), a.op, *a.paths))
    return 0


def cmd_loop(a):
    from . import loop as LP
    root, cfg = _root_cfg()
    try:
        if a.loop_cmd == "start":
            state = LP.start(root, cfg, a.plan, a.metric, a.budget_usd, goal=a.goal, max_hours=a.max_hours,
                             min_delta=a.min_delta, approve=a.approve, target=a.target, patience=a.patience,
                             max_tries=a.max_tries)
            print(f"[testbench] loop {state['id']} started on branch {state['branch']}; running the baseline "
                  f"(run {state['tries'][0]['run']})", flush=True)
        elif a.loop_cmd == "try":
            state = LP.try_change(root, cfg, " ".join(a.description), new_direction=a.new_direction)
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


def cmd_notify(a):
    from . import notify as N
    root, cfg = _root_cfg()
    if not cfg.get("notify"):
        print("testbench: testbench.yaml has no `notify:` section, so nothing is sent. Add one, e.g.\n"
              "  notify:\n    webhook_env: TESTBENCH_WEBHOOK      # an environment variable that holds the URL\n"
              "    on: [run_end, loop_rule, loop_end, approval_needed]")
        return 2
    url, source = N.target(cfg)
    if not url:
        print(f"testbench: nothing was sent: {source}.")
        return 2
    ok, what = N.send(root, cfg, "test", f"a test message from `testbench notify test`. Notifications for "
                      f"{', '.join(cfg['notify']['on'])} go here.", {"status": "test"}, force=True)
    print(f"Test message {what}." + ("" if ok else " Check the URL and that the service takes a JSON POST; every "
                                                     f"attempt is in {N.log_path(root).relative_to(root).as_posix()}."))
    if ok and cfg["notify"]["webhook"]:
        print("The URL is written in testbench.yaml; if that file is committed or copied to other machines, so is "
              "the webhook's secret. `webhook_env: NAME` reads it from an environment variable instead.")
    return 0 if ok else 1


DEMOS = {   # the examples `testbench demo NAME` copies: what each is, its free plan, what it needs, what to try next
    "tiny-model": {
        "about": "a Colab-style notebook that trains a small network, and plans for this machine, Modal and Colab",
        "list": "a Colab-style notebook that trains a small network (needs scikit-learn and matplotlib)",
        "plan": "smoke", "plan_about": "lint, then a quick train on this machine", "dir": "testbench-demo", "exit": 0,
        "needs": {"sklearn": "scikit-learn>=1.5", "matplotlib": "matplotlib>=3.9"},     # wheels for every Python
        "why": "The demo trains a small model, which needs {names}: pip install {pips}",
        "next": """
Next, in {target}:
  testbench session start && testbench exec --cells tiny_model.ipynb:1-3      # work live in a kernel
  testbench exec "model.score(X_test, y_test)" && testbench session stop
  testbench loop start smoke --metric outputs/metrics.json:accuracy --budget-usd 0   # then edit cells/02_settings.py,
  testbench nb build cells tiny_model.ipynb && testbench loop try "what changed"      # rebuild, and try it
  testbench ui                                                                       # every run as a page
Or open it in Claude Code with the plugin and ask: "use testbench loop to raise the smoke plan's accuracy"."""},
    "llm-app": {
        "about": "a support bot on the Anthropic SDK with one tool (look up an order), and a plan that runs its "
                 "tool-use loop against testbench's strict mock of the SDK: no API key, no network, no cost",
        "list": "a support bot on the Anthropic SDK with one tool, tested against the mock SDK (no API key)",
        "plan": "mocked", "plan_about": "five questions against the mock anthropic package, then again with a 429",
        "dir": "llm-app", "exit": 0, "needs": {},
        "next": """
Next, in {target}:
  testbench report --format html                 # the run as one page: every check, and what each request asked
  head -c 600 .testbench/runs/*/steps/01-answer-questions/mock_calls.jsonl     # a request, as the API would get it
Break the loop and run `testbench run mocked` again: in support_bot.py, send a tool_result with the wrong
tool_use_id, or put SYSTEM first in messages as a system message - the report names the 400 the real API would send.
Or open it in Claude Code with the plugin and ask: "add a cancel_order tool to the support bot and prove it works with
the mocked plan"."""},
    "web-app": {
        "about": "a page with a form and a small JSON API, and a plan that fills the form in headless Chromium, "
                 "checks the answer and the console, and takes screenshots",
        "list": "a page with a form, filled in and checked in headless Chromium (needs Playwright)",
        "plan": "ui", "plan_about": "split a bill in headless Chromium, then a bad one", "dir": "web-app", "exit": 0,
        "needs": {"playwright": '"agent-testbench[web]"'},
        "why": "The demo opens the page in headless Chromium, which needs Playwright: pip install {pips} && "
               "playwright install chromium",
        "next": """
Next, in {target}:
  testbench report --format html       # the run as one page, with its screenshots
  testbench ui                         # every run as a page
Break the app and run `testbench run ui` again: round down in split() in app.py (ROUND_DOWN), or rename #result in
static/index.html - the report says which text the page no longer shows, or which error it threw.
Or open it in Claude Code with the plugin and ask: "let the form take a tip in dollars too, and prove it with the ui
plan"."""},
    "test-suite": {
        "about": "a small Python package whose pytest suite fails on a planted bug, with plans that judge the suite by "
                 "its JUnit report here and in a Docker sandbox with no network",
        "list": "a package whose pytest suite fails on a planted bug, here and in a Docker sandbox: fix it",
        "plan": "tests", "plan_about": "the pytest suite on this machine, judged by its JUnit report",
        "dir": "test-suite", "exit": 1, "needs": {"pytest": "pytest"},
        "why": "The demo runs a pytest suite, which needs {names}: pip install {pips}",
        "next": """
The plan fails: that is the point. The report names the failing test and what it got.
Ask your agent to fix it: open {target} in Claude Code with the plugin and ask
  "the tests plan fails: find out why, fix the code (not the tests), and prove it with the plan"
With Docker running, `testbench run sandbox` runs the same suite in a fresh container with no network."""},
}


def _example(name: str) -> Path:
    """An example shipped in the wheel, or the repository's when running from a checkout."""
    for d in (Path(__file__).parent / "examples" / name, Path(__file__).resolve().parents[2] / "examples" / name):
        if (d / C.CONFIG_NAME).is_file():
            return d
    raise FileNotFoundError(f"the {name} example is not in this installation; reinstall agent-testbench")


def cmd_demo(a):
    import importlib.util as iu
    import shutil
    if a.list:
        print("`testbench demo NAME [DIR]` copies an example to a new folder (a git repository) and runs its free plan:")
        for name, d in DEMOS.items():
            print(f"  {name:<11} {'(the default) ' if name == 'tiny-model' else ''}{d['list']}")
        return 0
    name, folder = a.name, a.dir
    if name not in DEMOS:
        if name is not None and folder is not None:
            print(f"testbench: there is no {name} example{C.near(name, DEMOS)}; the examples are {', '.join(DEMOS)} "
                  "(`testbench demo --list` says what each shows)")
            return 2
        typo = C.near(name, DEMOS, cutoff=0.85) if name is not None and not set("/\\") & set(name) else ""
        if typo and not Path(name).exists():         # `testbench demo llm-ap` is not tiny-model in a folder llm-ap
            print(f"testbench: there is no {name} example{typo}; the examples are {', '.join(DEMOS)}. "
                  f"To copy tiny-model into a folder named {name}, say so: `testbench demo tiny-model {name}`.")
            return 2
        name, folder = "tiny-model", name            # `testbench demo` and `testbench demo DIR`, as before
    demo, given = DEMOS[name], [x for x in (a.name, a.dir) if x is not None]
    target = Path(folder or demo["dir"]).resolve()
    if target.exists() and any(target.iterdir()):
        print(f"{target} is not empty, so the example is not copied there; give it a new folder: "
              f"`testbench demo {name} {shlex.quote(target.name + '-2')}`")
        return 2
    missing = [pkg for mod, pkg in demo["needs"].items() if iu.find_spec(mod) is None]
    if missing:
        print(demo["why"].format(names=" and ".join(missing), pips=" ".join(missing))
              + f" - then run `{' '.join(['testbench', 'demo', *given])}` again.")
        return 2
    shutil.copytree(_example(name), target,
                    ignore=shutil.ignore_patterns(".testbench", "outputs", "__pycache__", ".pytest_cache"))
    (target / ".gitignore").write_text("outputs/\n__pycache__/\n.pytest_cache/\n")
    if shutil.which("git"):                          # a repository, so `testbench loop` works there too
        for cmd in (["init", "-q"], ["add", "-A"], ["-c", "user.name=testbench demo", "-c", "user.email=demo@localhost",
                                                    "commit", "-qm", f"{name} demo"]):
            subprocess.run(["git", *cmd], cwd=target, capture_output=True)
    print(f"Copied the {name} example to {target}: {demo['about']}.\n")
    if a.no_run:
        code = 0
    else:
        print(f"Running its free plan, `{demo['plan']}` ({demo['plan_about']}):\n", flush=True)
        code = subprocess.call([sys.executable, "-m", "agent_testbench", "run", demo["plan"], "--wait", "300"],
                               cwd=target)
        code = 0 if code == demo["exit"] else (code or 1)   # test-suite's plan fails on purpose
    print(demo["next"].format(target=target))
    return code


def cmd_mcp(a):
    try:
        from .mcp_server import main as serve
    except ImportError as exc:
        if (getattr(exc, "name", None) or "").split(".")[0] != "mcp":
            raise
        print('testbench mcp needs the MCP package: pip install "agent-testbench[mcp]" (or "agent-testbench[all]")',
              file=sys.stderr)
        return 2
    serve(http=a.http, host=a.host, port=a.port)
    return 0


def cmd_connect(a):
    from .connect import main as connect
    return connect(a.clients, user=a.user, dry_run=a.dry_run, list_=a.list)


class _Parser(argparse.ArgumentParser):
    """No abbreviated options: `--appr` must not mean `--approve` to testbench while the guard hook sees no
    `--approve` (every subcommand, at every level, is one of these). Help keeps the examples' lines as written, and a
    mistake says where they are."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        kwargs.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
        super().__init__(*args, **kwargs)

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n{self._example(message)}`{self.prog} --help` says what it takes, "
                     "with examples.\n")

    def _example(self, message: str) -> str:
        """'For example: `testbench runs -n 30`. ': the command's example that uses the option the mistake is about,
        else its first; '' for a parser without examples."""
        if not (self.epilog or "").startswith("examples:"):
            return ""
        lines = [ln.strip().split("  #")[0].strip() for ln in self.epilog.splitlines() if ln.startswith("  testbench ")]
        about = re.match(r"argument ([^:]+):", message)
        named = set(about.group(1).split("/")) if about else set()
        hit = next((ln for ln in lines if named & set(ln.split())), lines[0] if lines else "")
        return f"For example: `{hit}`. " if hit else ""


def _number(text: str) -> float:
    """A real number on the command line; nan and inf would compare false with every limit."""
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not math.isfinite(v):
        raise argparse.ArgumentTypeError(f"{text!r} is not a finite number")
    return v


def _dollars(text: str) -> float:
    v = _number(text)
    if v < 0:
        raise argparse.ArgumentTypeError(f"{text!r} must be 0 or more")
    return v


def _positive(text: str) -> float:
    v = _number(text)
    if v <= 0:
        raise argparse.ArgumentTypeError(f"{text!r} must be more than 0")
    return v


def _count(text: str) -> int:
    try:
        v = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if v < 1:
        raise argparse.ArgumentTypeError(f"{text!r} must be 1 or more")
    return v


GROUPS = {   # `testbench help --all`: every command, in the groups README's Commands table uses, one short line each
    "try it and set up": {
        "demo": "copy an example project, run its free plan, show what is next",
        "init": "write testbench.yaml and AGENTS.md for this project",
        "recipes": "ready plans for a goal, each with its worst-case cost",
        "schema": "testbench.yaml's JSON Schema, for editors to autocomplete it",
        "connect": "set up Cursor, VS Code, Codex, Gemini or Claude Code to use it",
        "doctor": "check what this machine can run, and what to fix",
        "plugins": "the kinds of plug-in: built in, installed, broken and why",
        "plugin": "plugin test: check a plug-in keeps its kind's contract",
        "selftest": "a minute or so: check the main promises hold on this machine",
        "mcp": "serve these commands as MCP tools"},
    "plans": {
        "check": "run every free plan here, even with no testbench.yaml",
        "plans": "the plans, their hardware and worst-case cost",
        "run": "run a plan and print its report",
        "wait": "follow a run until it ends",
        "status": "one line about a run",
        "runs": "recent runs",
        "cancel": "stop a run, here and on its remote machine",
        "watch": "run a free plan again whenever the files change"},
    "proof": {
        "report": "a run's report: markdown, json, html or junit",
        "verify": "check a run's evidence against its recorded hashes",
        "diff": "what changed between two runs",
        "history": "a plan's runs over time, with the numbers its checks found",
        "ui": "every run as a page, or a live dashboard (--serve)",
        "export": "a run to MLflow, W&B, csv or jsonl"},
    "live sessions": {
        "session": "a live kernel: start, list, stop, sync, install, shell",
        "exec": "run code or notebook cells in a session, see what came back",
        "files": "list, get and put files on a session's machine"},
    "loops and notifications": {
        "loop": "try changes one at a time; keep what a plan run shows is better",
        "notify": "send a test message to testbench.yaml's notify: webhook"},
    "money": {
        "estimate": "a plan's worst case, and whether it may launch now",
        "spend": "what runs and sessions have cost",
        "prices": "the prices behind every estimate"},
    "notebooks and the lab notebook": {
        "lint": "check notebooks for Colab and headless pitfalls",
        "rescue": "why a notebook does not run, and what to do",
        "nb": "a notebook as plain cell files: split, build, fields",
        "note": "add a line to the lab notebook (.testbench/NOTES.md)"},
}

CORE = {   # `testbench --help`: the commands a newcomer needs, each with its line from GROUPS; `help --all`: every one
    "start": ("demo", "check", "doctor"),
    "prove it with a plan": ("plans", "estimate", "run", "report", "verify", "ui"),
    "work live, improve in a loop": ("session", "exec", "loop"),
}

ABOUT = ("testbench runs your code on real machines - this one, a container, an ssh box, a Modal GPU, a Colab VM - "
         "and proves what it did with plans: steps, checks and evidence, inside a budget. Coding agents operate it "
         "with these commands or as an MCP server.")

START = """\
start with:
  testbench check        run what is here, for free, with no setup, and prove it
  testbench demo         a small example project, run here for free
  testbench init         plans for the notebooks, tests, app or training here
  testbench plans        each plan, its hardware and its worst-case cost
  testbench run PLAN     run one: what passed, what failed and why, the cost
  testbench report       the latest run's report (--format html: one page)

exit codes: 0 passed or done, 1 failed, 2 refused or bad input (nothing ran),
3 still running. `testbench help COMMAND` shows its options and examples.
All of them: https://sarptandoven.github.io/agent-testbench/reference/#commands"""

EXAMPLES = """
selftest
  testbench selftest                     # ten checks in a temporary folder
  testbench selftest --keep              # and keep that folder and its runs
init
  testbench init                         # a plan for each thing it finds here
  testbench init --recipe pytorch-train  # start from a recipe instead
  testbench init --add --recipe web-app  # add its plans to your testbench.yaml
recipes
  testbench recipes                      # each recipe, its plans and their cost
  testbench recipes pytest               # print one
schema
  testbench schema                       # print it; init's first line links it
check
  testbench check                        # every free plan, or what init finds
  testbench check --plan tests           # only this plan (repeatable)
  testbench check --json                 # a summary for scripts and agents
plans
  testbench plans
estimate
  testbench estimate smoke               # its worst case; may it launch now?
  testbench estimate train-gpu --json
run
  testbench run smoke                    # follow it, then print the report
  testbench run train-gpu --wait 1800    # follow it up to 30 minutes
  testbench run train-gpu --detach       # start it and return (exit 3)
wait
  testbench wait                         # the latest run
  testbench wait 20261101-0916 --timeout 60
status
  testbench status                       # the latest run
  testbench status last~1                # the run before it
runs
  testbench runs
  testbench runs -n 30
cancel
  testbench cancel                       # the latest run
  testbench cancel 20261101-0916         # a run, by a prefix of its id
watch
  testbench watch smoke                  # now, then after every change
  testbench watch tests --max-runs 3
report
  testbench report                       # the latest run's report
  testbench report --format html         # one page, with the plots
  testbench report last~1 --format junit -o report.xml
verify
  testbench verify                       # the latest run's evidence files
  testbench verify 20261101-0916 --attestation --repo OWNER/REPO
diff
  testbench diff                         # the latest run and the one before it
  testbench diff smoke                   # the plan's last two finished runs
  testbench diff last~1 last --json
history
  testbench history smoke
  testbench history smoke -n 5 --json
ui
  testbench ui --open                    # static pages, opened in the browser
  testbench ui --serve                   # live at http://127.0.0.1:8770/
export
  testbench export --to csv              # the latest run, to a csv file
  testbench export all --to mlflow --tracking-uri http://127.0.0.1:5000
session
  testbench session start                # a kernel on this machine
  testbench session start gpu --backend modal --gpu T4 --max-minutes 30
  testbench session list
  testbench session stop gpu
session start
  testbench session start                # session main, on sessions.backend
  testbench session start gpu --backend modal --gpu T4 --max-minutes 30
session list
  testbench session list
session stop
  testbench session stop                 # the only running session
  testbench session stop gpu
  testbench session stop --all
session sync
  testbench session sync                 # after edits here; variables are kept
session install
  testbench session install pandas==2.2.3 -s gpu
session shell
  testbench session shell "nvidia-smi" -s gpu
exec
  testbench exec "x = 41"
  testbench exec "x + 1"                 # => 42: variables persist
  testbench exec -f train.py -s gpu
  testbench exec --cells train.ipynb:1-3 --params '{"EPOCHS": 2}'
files
  testbench files ls outputs
  testbench files get outputs/model.pt   # to .testbench/sessions/NAME/outputs/
  testbench files put data.csv data/data.csv -s gpu
loop
  testbench loop start smoke --metric out/metrics.json:accuracy --budget-usd 0
  testbench loop try "lower the learning rate to 0.01"
  testbench loop status
  testbench loop stop
loop start
  testbench loop start smoke --metric out/metrics.json:accuracy --budget-usd 0
  testbench loop start train --metric check:val_loss --goal min --budget-usd 2
loop try
  testbench loop try "lower the learning rate to 0.01"
loop status
  testbench loop status
loop stop
  testbench loop stop                    # its report; back on the first branch
notify
  testbench notify test
spend
  testbench spend
  testbench spend --modal                # also Modal's own bill
prices
  testbench prices
  testbench prices --update              # fetch the price catalog
lint
  testbench lint train.ipynb
  testbench lint a.ipynb b.ipynb --strict    # exit 1 when anything is found
rescue
  testbench rescue train.ipynb           # run it here and say why it fails
  testbench rescue train.ipynb --fix train_fixed.ipynb
  testbench rescue shared.ipynb --no-run # only read it
nb
  testbench nb split train.ipynb cells   # cells/01_imports.py, 02_intro.md ...
  testbench nb build cells train.ipynb
  testbench nb fields train.ipynb
nb split
  testbench nb split train.ipynb cells
nb build
  testbench nb build cells train.ipynb
nb fields
  testbench nb fields train.ipynb
note
  testbench note "lr 0.1 diverges by epoch 3; 0.01 is stable"
demo
  testbench demo                         # tiny-model, into ./testbench-demo
  testbench demo llm-app                 # a bot tested against the mock SDK
  testbench demo --list
connect
  testbench connect --list               # each client, and what is set up
  testbench connect cursor --dry-run
  testbench connect claude --user
doctor
  testbench doctor
plugins
  testbench plugins                      # built in, installed, broken
  testbench plugins --json
plugin
  testbench plugin test testbench-backend-example  # each plug-in it declares
  testbench plugin test --installed      # every installed plug-in
plugin test
  testbench plugin test testbench-backend-example  # each plug-in it declares
  testbench plugin test testbench_nice:backend --kind backends
  testbench plugin test --installed      # every installed plug-in
mcp
  testbench mcp                          # stdio, for an MCP client's config
  testbench mcp --http --port 8765
"""


@functools.cache
def examples() -> dict:
    """{"run": "  testbench run smoke ...", "session start": ...}: what each command's --help ends with."""
    out, key = {}, None
    for line in EXAMPLES.splitlines():
        if line and not line.startswith(" "):
            key, out[line] = line, []
        elif line.strip():
            out[key].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def _width() -> int:
    return min(shutil.get_terminal_size().columns, 102) - 2


def _wrap(text: str) -> str:
    return "\n\n".join(textwrap.fill(par, _width()) for par in text.split("\n\n"))


def _json(p, what: str) -> None:
    """--json, and --format json as `testbench report` takes it."""
    p.add_argument("--json", action="store_true", help=f"{what} as JSON")
    p.add_argument("--format", choices=["text", "json"], default="text", help="text (default) or json: the same as --json")


def _subcommands(parser):
    return next((x for x in parser._actions if isinstance(x, argparse._SubParsersAction)), None)


def commands(ap) -> dict:
    """{"run": its parser, "session start": ..., ...}: every command and subcommand."""
    out, todo = {}, [("", ap)]
    while todo:
        path, parser = todo.pop()
        sub = _subcommands(parser)
        for name, p in (sub.choices.items() if sub else ()):
            out[f"{path} {name}".strip()] = p
            todo.append((f"{path} {name}".strip(), p))
    return out


def _listing(groups: dict, top: dict) -> list[str]:
    """Each group's title and its commands, one line each. A name the parser lacks is left out, and so is a group
    left with none: CORE may name a command another branch adds."""
    lines = []
    for title, names in groups.items():
        shown = [textwrap.fill(f"{n:<9} {summary}", _width(), initial_indent="    ", subsequent_indent=" " * 14)
                 for n, summary in names.items() if n in top]
        lines += [f"  {title}"] * bool(shown) + shown
    return lines


def _finish(ap) -> None:
    """Every command's --help gets a description and ends with its examples. `testbench --help` lists the core
    commands (CORE) and how many more there are; `testbench help --all` lists every one in the groups README uses."""
    found = commands(ap)
    for path, p in found.items():
        sub = _subcommands(ap if " " not in path else found[path.rsplit(" ", 1)[0]])
        p.summary = next((x.help for x in sub._choices_actions if x.dest == path.split()[-1]), "") or ""
        p.description = _wrap(p.description or (p.summary[:1].upper() + p.summary[1:] + "."))
        if path in examples():
            p.epilog = "examples:\n" + examples()[path]
    top = _subcommands(ap).choices
    line = {n: summary for names in GROUPS.values() for n, summary in names.items()}
    other = {n: p.summary for n, p in top.items() if n not in line}
    core = {title: {n: line.get(n) or top[n].summary for n in names if n in top} for title, names in CORE.items()}
    more = len(set(top) - {n for names in core.values() for n in names})
    ap.description = "\n".join([_wrap(ABOUT), "", "commands:", *_listing(core, top)] + [
        "", f"{more} more, for setup, money, notebooks and the rest: testbench help --all"] * bool(more))
    ap.every_command = "\n".join([_wrap(ABOUT), "", "commands:", *_listing({**GROUPS, "other": other}, top)])


RUN = "a run id, a prefix of one, last or last~N (default: the latest run)"
PLAN = "a plan in testbench.yaml (`testbench plans` lists them)"
SESSION = "the session's name (default: the only running one)"


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(prog="testbench", usage="testbench [-h] [--all] [--version] COMMAND ...", epilog=START)
    ap.add_argument("--all", action="store_true", help="with --help: every command, not only the core ones")
    ap.add_argument("--version", action="version", version=f"agent-testbench {__version__}")
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND", help=argparse.SUPPRESS, prog="testbench", parser_class=_Parser)
    p = sub.add_parser("init", help="write testbench.yaml and the working rules (AGENTS.md) for this project",
                       description="Look at the project - notebooks, a test suite (pytest, npm test, go test, cargo "
                                   "test), training scripts, a web app - and write testbench.yaml with a plan for each "
                                   "and a budget, and the working rules for agents in AGENTS.md (CLAUDE.md points to "
                                   "it). Each plan already checks something; the lines marked FILL IN are the numbers "
                                   "only you know.")
    p.add_argument("--force", action="store_true", help="replace the testbench.yaml that is there")
    p.add_argument("--recipe", action="append", default=[], metavar="NAME",
                   help="start from a recipe (repeatable; `testbench recipes` lists them)")
    p.add_argument("--add", action="store_true",
                   help="with --recipe: add the recipe's plans to the existing testbench.yaml, changing nothing in it")
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("recipes", help="testbench.yaml starting points for a goal: list them with their costs, or print one")
    p.add_argument("name", nargs="?", metavar="NAME", help="print this recipe")
    p.set_defaults(fn=cmd_recipes)
    sub.add_parser("schema", help="print testbench.yaml's JSON Schema",
                   description="Print the JSON Schema (draft 2020-12) of testbench.yaml, so an editor can complete keys "
                               "and flag mistakes as you type. `testbench init` writes a first line that points the "
                               "YAML language server (VS Code's YAML extension) at it; or save this output and set "
                               "VS Code's yaml.schemas to it. It needs no testbench.yaml.").set_defaults(fn=cmd_schema)
    p = sub.add_parser("check", help="run every plan that is free here; with no testbench.yaml, what init would set up",
                       description="A first proven result in about a minute. With a testbench.yaml (here or above): run "
                                   "every plan that is free and needs no approval, and list each other plan with its "
                                   "worst case and the command a person runs for it - nothing that costs money starts. "
                                   "With none: run here, for free, the plans `testbench init` would write for what it "
                                   "finds (a test suite: pytest, npm test, go test, cargo; notebooks; a web app; a "
                                   "training script), as ordinary runs with full proof records, and keep what it found "
                                   "in .testbench/check.json, so report, runs, verify, ui and diff work here afterwards. "
                                   "It writes no testbench.yaml, AGENTS.md or ~/.testbench/limits.yaml. A plan whose "
                                   "tool is missing (node, go, cargo-nextest, pytest, Chromium) is skipped with the "
                                   "command that installs it. Exit 0 every plan that ran passed, 1 one failed, 2 nothing "
                                   "to check or refused, 3 still running.")
    p.add_argument("--plan", action="append", default=[], metavar="NAME",
                   help="only this plan (repeatable); a plan that costs money is still only listed")
    _json(p, "the summary")
    p.set_defaults(fn=cmd_check)
    sub.add_parser("plans", help="list plans, their hardware and worst-case cost").set_defaults(fn=cmd_plans)
    p = sub.add_parser("estimate", help="worst-case cost of a plan and whether it may launch (exit 0 yes, 2 no)")
    p.add_argument("plan", metavar="PLAN", help=PLAN)
    p.add_argument("--approve", action="store_true", help="as if a person approved the cost: may it launch then?")
    _json(p, "the estimate")
    p.set_defaults(fn=cmd_estimate)
    p = sub.add_parser("run", help="run a plan and print its report",
                       description="Run PLAN once its worst-case cost fits the budget, follow it, and print its report: "
                                   "what passed, what failed and why (the cell and line, the missed expectation), what "
                                   "changed since the previous run, and what it cost. Exit 0 passed, 1 failed, 2 refused "
                                   "(nothing ran), 3 still running after --wait (the run goes on).")
    p.add_argument("plan", metavar="PLAN", help=PLAN)
    p.add_argument("--approve", action="store_true", help="a person approved this run's cost (never set this yourself)")
    p.add_argument("--detach", action="store_true", help="start it and return at once (exit 3)")
    p.add_argument("--wait", type=_number, default=540, metavar="SECONDS",
                   help="follow for at most this many seconds (default 540)")
    p.add_argument("--foreground", action="store_true", help="run in this process (no background driver)")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("wait", help="follow a run until it ends or the timeout passes",
                       description="Follow a run until it ends, then print its report. Exit 0 passed, 1 failed, 3 still "
                                   "running after --timeout (the run goes on: wait again).")
    p.add_argument("run", nargs="?", metavar="RUN", help=RUN)
    p.add_argument("--timeout", "--wait", dest="timeout", type=_number, default=540, metavar="SECONDS",
                   help="follow for at most this many seconds (default 540); --wait is the same, as `run` spells it")
    p.set_defaults(fn=cmd_wait)
    for name, fn, helptext in (("status", cmd_status, "one line about a run (exit 0 passed, 1 failed, 3 still running)"),
                               ("report", cmd_report, "a run's report: markdown, json, html (one page) or junit"),
                               ("cancel", cmd_cancel, "stop a run, locally and remotely"),
                               ("verify", cmd_verify, "check a run's evidence still matches the hashes in its report")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("run", nargs="?", metavar="RUN", help=RUN)
        if name == "report":
            p.add_argument("--format", choices=["md", "json", "html", "junit"], default="md",
                           help="md (default), json, html (a page with the evidence images; written to the run "
                                "folder) or junit (XML for CI)")
            p.add_argument("--json", action="store_true", help="same as --format json")
            p.add_argument("-o", "--output", metavar="FILE", help="write to this file")
        if name == "verify":
            p.add_argument("--attestation", action="store_true",
                           help="also check report.json's GitHub artifact attestation (the action's attest: true) with "
                                "`gh attestation verify`: made by a workflow of the repository, at the report's commit")
            p.add_argument("--repo", metavar="OWNER/REPO",
                           help="with --attestation: the repository whose workflow made it (default: "
                                "GITHUB_REPOSITORY, else the origin remote)")
        p.set_defaults(fn=fn)
    p = sub.add_parser("prices", help="the prices estimates use and where each comes from; --update fetches the catalog")
    p.add_argument("--update", action="store_true", help="fetch the price catalog (it can raise prices, never lower them)")
    p.add_argument("--url", default=None, help="fetch the catalog from here instead of the repository")
    p.set_defaults(fn=cmd_prices)
    p = sub.add_parser("ui", help="the project's runs as static pages (.testbench/ui/index.html), or live with --serve")
    p.add_argument("--open", action="store_true", help="also open it in the browser")
    p.add_argument("--serve", action="store_true", help="a live, read-only dashboard at http://127.0.0.1:8770/ "
                                                        "(this machine only; Ctrl-C stops it)")
    p.add_argument("--port", type=int, default=None, help="with --serve (default 8770; 0 picks a free one)")
    p.set_defaults(fn=cmd_ui)
    p = sub.add_parser("history", help="a plan over time: each run's outcome and the numbers its checks found")
    p.add_argument("plan", metavar="PLAN", help=PLAN)
    p.add_argument("-n", type=_count, default=30, help="the latest N runs (default 30)")
    _json(p, "the history")
    p.set_defaults(fn=cmd_history)
    p = sub.add_parser("diff", help="what changed between two runs: verdict, steps, every checked number, checks, "
                                    "evidence, cost, code and environment",
                       description="What changed from run A to run B, in plain words: the verdict and each step, every "
                                   "checked number (old -> new, by how much, better or worse by the check's rule), "
                                   "checks that newly pass or fail, evidence files added, removed or changed (sha256), "
                                   "cost and minutes, the code (commits, uncommitted changes, git diff --stat) and "
                                   "where it ran (Python, platform, packages that changed version).")
    p.add_argument("runs", nargs="*", metavar="RUN", help="A B: two runs, older first (run ids, prefixes, last, last~N); "
                                                          "one PLAN: its last two finished runs; one RUN: it and the "
                                                          "run of its plan before it; none: the latest run and the one "
                                                          "before it")
    _json(p, "the comparison")
    p.set_defaults(fn=cmd_diff)
    p = sub.add_parser("watch", help="run a plan again whenever the project's files change; free plans only, unless a "
                                     "person approves (--approve), and every run is checked against the budget",
                       description="Run PLAN now, then again whenever the project's files change (polled every "
                                   "--interval seconds; a run starts once nothing has changed for --debounce seconds). "
                                   "A change while a run is going queues one more run, never more. Each run is a normal "
                                   "launch, priced and checked against the budget first. Only plans whose worst case "
                                   "is $0 (local, or docker, ssh or kubernetes at usd_per_hour 0) are watched without "
                                   "--approve; with it, every run is still checked against max_run_usd and the "
                                   "project's and this machine's day limits, and watching stops at the first refusal. "
                                   "Left out: exclude:, .gitignore, caches and editor files, and the files the plan "
                                   "itself writes. Runs it starts send no run_end notification (notify.on: watch_run "
                                   "sends one when a result changes). Ctrl-C cancels the run that is going and stops.")
    p.add_argument("plan", metavar="PLAN", help=PLAN)
    p.add_argument("--approve", action="store_true", help="a person approved watching a plan that costs money (never "
                                                          "set this yourself); each run still has to fit max_run_usd "
                                                          "and the day limits")
    p.add_argument("--debounce", type=_positive, default=1.0, metavar="SECONDS",
                   help="seconds without a change before a run starts (default 1)")
    p.add_argument("--interval", type=_positive, default=0.5, metavar="SECONDS",
                   help="seconds between looks at the files (default 0.5)")
    p.add_argument("--max-runs", type=_count, metavar="N", help="stop after N runs (default: until Ctrl-C)")
    p.set_defaults(fn=cmd_watch)
    p = sub.add_parser("export", help="a run in MLflow, W&B, a csv or a jsonl file, with its numbers and evidence "
                                      "(again: updates it, no duplicate)")
    p.add_argument("run", nargs="?", metavar="RUN",
                   help="run id or prefix, latest (the default), or all (every finished run)")
    p.add_argument("--to", choices=["mlflow", "wandb", "csv", "jsonl"],
                   help="where to (default: every target in testbench.yaml's export: section)")
    p.add_argument("--tracking-uri", help="with --to mlflow (default: export.mlflow.tracking_uri, MLFLOW_TRACKING_URI)")
    p.add_argument("--experiment", help="with --to mlflow (default: the project's name)")
    p.add_argument("--project", help="with --to wandb: the W&B project (default: the project's name)")
    p.add_argument("--entity", help="with --to wandb: the W&B team or user")
    p.add_argument("--mode", choices=["online", "offline", "disabled"], help="with --to wandb")
    p.add_argument("--path", help="with --to csv or jsonl: the file (default: .testbench/runs.csv or .jsonl)")
    p.add_argument("--no-artifacts", action="store_true", help="trackers: leave out the report and evidence files")
    p.set_defaults(fn=cmd_export)
    p = sub.add_parser("runs", help="recent runs, one line each")
    p.add_argument("-n", type=_count, default=10, help="the latest N runs (default 10)")
    p.set_defaults(fn=cmd_runs)
    p = sub.add_parser("spend", help="what runs and sessions have cost, today and in all")
    p.add_argument("-n", type=_count, default=10, help="list the latest N (default 10)")
    p.add_argument("--modal", action="store_true", help="also read Modal's own bill for this project's app")
    p.set_defaults(fn=cmd_spend)
    p = sub.add_parser("lint", help="check notebooks (.ipynb, percent-format .py) for Colab and headless pitfalls")
    p.add_argument("notebooks", nargs="+", metavar="NOTEBOOK")
    p.add_argument("--strict", action="store_true", help="exit 1 when anything is found")
    p.set_defaults(fn=cmd_lint)
    p = sub.add_parser("rescue", help="why a notebook does not run and what to do: read it, run it top to bottom here, "
                                      "diagnose the first failure",
                       description="Read a notebook, run it top to bottom in a fresh kernel here, and diagnose the "
                                   "first failure: what broke, why, what to do, and a plan that proves it once fixed. "
                                   "It needs no testbench.yaml. Exit 0 it ran, 1 it failed or ran out of time, 2 it "
                                   "could not be read.")
    p.add_argument("notebook", metavar="NOTEBOOK")
    p.add_argument("--timeout", type=_number, default=120, metavar="SECONDS", help="seconds the run may take (default 120)")
    p.add_argument("--fix", metavar="OUT.ipynb", help="also write a copy with mechanical fixes only (cells never moved); "
                   "for a percent-format .py notebook, OUT.py keeps it a .py")
    _json(p, "the diagnosis")
    p.add_argument("--no-run", action="store_true", help="only read it; run nothing (for a notebook nobody has reviewed)")
    p.set_defaults(fn=cmd_rescue)
    p = sub.add_parser("nb", help="notebooks as plain cell files: split, build, fields")
    nsub = p.add_subparsers(dest="nb_cmd", metavar="COMMAND", required=True)
    q = nsub.add_parser("split", help="write a notebook's cells as NN_name.py / .md files")
    q.add_argument("notebook", metavar="NOTEBOOK")
    q.add_argument("cells_dir", metavar="CELLS_DIR")
    q = nsub.add_parser("build", help="assemble cell files into a notebook")
    q.add_argument("cells_dir", metavar="CELLS_DIR")
    q.add_argument("notebook", metavar="NOTEBOOK")
    q = nsub.add_parser("fields", help="list what params can set: Colab form fields and papermill parameters")
    q.add_argument("notebook", metavar="NOTEBOOK")
    p.set_defaults(fn=cmd_nb)
    p = sub.add_parser("note", help="add a line to the project's lab notebook (.testbench/NOTES.md)")
    p.add_argument("text", nargs="+", metavar="TEXT", help="what you found, in a few words")
    p.set_defaults(fn=cmd_note)
    p = sub.add_parser("session", help="a live kernel here, in a container, on an ssh box or a cluster, a Modal GPU or a "
                                       "Colab VM: start, list, stop, sync, install, shell")
    ssub = p.add_subparsers(dest="sess_cmd", metavar="COMMAND", required=True)
    q = ssub.add_parser("start", help="start a session (inside the budget)",
                        description="Start a kernel that keeps its variables between `testbench exec` calls, inside the "
                                    "budget (its worst case is --max-minutes of its hardware). It stops by itself after "
                                    "--idle-minutes unused and after --max-minutes in any case.")
    q.add_argument("name", nargs="?", default="main", metavar="NAME", help="its name (default main)")
    q.add_argument("-s", "--session", metavar="NAME", help="the same as NAME, as exec and files spell it")
    q.add_argument("--backend", help="local, docker, ssh, kubernetes, modal, colab, or an installed backend "
                                     "(`testbench doctor` lists them; default: sessions.backend)")
    q.add_argument("--gpu", help="e.g. T4, or H100:2 on modal (`testbench prices` lists each backend's)")
    q.add_argument("--idle-minutes", type=_positive, metavar="MINUTES",
                   help="stop after this long without use (default from testbench.yaml)")
    q.add_argument("--max-minutes", type=_positive, metavar="MINUTES", help="stop after this long in any case; bounds the cost")
    q.add_argument("--secret", action="append", default=[], metavar="NAME", help="a secret NAME to pass in (repeatable)")
    q.add_argument("--approve", action="store_true", help="a person approved this session's cost (never set this yourself)")
    ssub.add_parser("list", help="sessions, their hardware, uptime and cost")
    q = ssub.add_parser("stop", help="stop a session (default: the only running one)")
    q.add_argument("name", nargs="?", metavar="NAME", help=SESSION)
    q.add_argument("-s", "--session", metavar="NAME", help="the same as NAME")
    q.add_argument("--all", action="store_true", help="every running session")
    for verb, helptext in (("sync", "copy the project's files to the session's machine again (variables are kept)"),):
        q = ssub.add_parser(verb, help=helptext)
        q.add_argument("name", nargs="?", metavar="NAME", help=SESSION)
        q.add_argument("-s", "--session", metavar="NAME", help="the same as NAME")
    q = ssub.add_parser("install", help="pip install packages on the session's machine")
    q.add_argument("packages", nargs="+", metavar="PACKAGE", help="pin them, e.g. pandas==2.2.3")
    q.add_argument("-s", "--name", "--session", dest="name", metavar="NAME", help=SESSION)
    q = ssub.add_parser("shell", help="run a shell command on the session's machine (nvidia-smi, ls, df ...)")
    q.add_argument("command", metavar="COMMAND", help="one shell command; quote it")
    q.add_argument("-s", "--name", "--session", dest="name", metavar="NAME", help=SESSION)
    q.add_argument("--timeout", type=_number, default=120, metavar="SECONDS", help="stop it after this long (default 120)")
    p.set_defaults(fn=cmd_session)
    p = sub.add_parser("exec", help="run code, a file, or notebook cells in a live session and show what came back",
                       description="Run code, a file, or notebook cells in a live session (`testbench session start`) "
                                   "and show what came back: what it printed, its last value (=> ...), images saved as "
                                   "files, and for an error the exception and the line. Exit 0 ok, 1 the code raised, "
                                   "2 refused or no such session, 3 interrupted at --timeout (the kernel and its "
                                   "variables are kept).")
    p.add_argument("code", nargs="?", metavar="CODE", help="Python code (or use -f, --cells, or stdin)")
    p.add_argument("-s", "--session", metavar="NAME", help=SESSION)
    p.add_argument("-f", "--file", help="a .py file to run")
    p.add_argument("--cells", metavar="NOTEBOOK[:CELLS]",
                   help="notebook cells, e.g. train.ipynb:3-5 (code cells from 1); train.ipynb alone runs all")
    p.add_argument("--params", metavar="JSON",
                   help='params for --cells (form fields, papermill parameters), as JSON: \'{"EPOCHS": 2}\'')
    p.add_argument("--timeout", type=_number, default=600, metavar="SECONDS",
                   help="seconds per cell before it is interrupted (default 600)")
    _json(p, "the results")
    p.set_defaults(fn=cmd_exec)
    p = sub.add_parser("files", help="files on a session's machine: ls [PATH] | get REMOTE [LOCAL] | put LOCAL REMOTE")
    p.add_argument("op", choices=["ls", "get", "put"], metavar="ls|get|put", help="list, copy back, or copy there")
    p.add_argument("paths", nargs="*", metavar="PATH", help="ls: a folder (default: the project's); get: the remote file, "
                                                            "then where to put it (default: the session's outputs/); "
                                                            "put: the local file, then the remote path")
    p.add_argument("-s", "--session", metavar="NAME", help=SESSION)
    p.set_defaults(fn=cmd_files)
    p = sub.add_parser("loop", help="an improvement loop: each try is committed, proved by a plan run, kept or reverted")
    lsub = p.add_subparsers(dest="loop_cmd", metavar="COMMAND", required=True)
    q = lsub.add_parser("start", help="start a loop on its own git branch and run the baseline",
                        description="Start a loop on its own git branch (testbench-loop/<id>) and run PLAN on the code "
                                    "as it is: the baseline. Then edit and `testbench loop try`. Needs a git repository "
                                    "with no uncommitted changes. Exit 0 the baseline passed, 1 it did not (the loop "
                                    "still started), 2 refused, 3 still running.")
    q.add_argument("plan", metavar="PLAN", help=PLAN)
    q.add_argument("--metric", required=True, metavar="FILE:KEY",
                   help="FILE:KEY of a number a json expectation of the plan checks, e.g. outputs/metrics.json:accuracy; "
                        "or check:SUBSTRING, the number of the check whose name contains it, e.g. check:val_loss")
    q.add_argument("--budget-usd", type=_dollars, required=True, metavar="USD",
                   help="what the whole loop may spend, every run together")
    q.add_argument("--goal", choices=["max", "min"], default="max", help="max: higher is better (default); min: lower")
    q.add_argument("--min-delta", type=_number, default=0.0, metavar="X",
                   help="keep a try only if it beats the best by more than this")
    q.add_argument("--max-hours", type=_positive, default=8.0, metavar="HOURS", help="no new tries after this long (default 8)")
    q.add_argument("--approve", action="store_true", help="a person approved the loop's total budget (never set this yourself)")
    q.add_argument("--target", type=_number, metavar="VALUE", help="done when the best reaches this (>= for --goal "
                                                                 "max, <= for min): further tries are refused")
    q.add_argument("--patience", type=int, metavar="N", help="refuse a try after N tries in a row were not kept")
    q.add_argument("--max-tries", type=int, metavar="N", help="refuse a try after N tries (the baseline not counted)")
    q.add_argument("--wait", type=_number, metavar="SECONDS",
                   help="follow the baseline run at most this many seconds (default: until it ends)")
    q = lsub.add_parser("try", help="commit the edits as a try, run the plan, keep it or revert it",
                        description="Commit every change in the project folder as a try, run the plan on it, and keep "
                                    "it if the metric beats the best, else revert it. Exit 0 kept, 1 reverted, 2 refused "
                                    "(nothing committed), 3 still running (`testbench loop status` judges it once it "
                                    "ends).")
    q.add_argument("description", nargs="+", metavar="WHAT", help="what this try changed, in a few words")
    q.add_argument("--wait", type=_number, metavar="SECONDS",
                   help="follow the run at most this many seconds (default: until it ends)")
    q.add_argument("--new-direction", action="store_true", help="after --patience refused a try: this one is "
                                                                 "different in kind, and the count starts again")
    lsub.add_parser("status", help="the loop's tries: kept or reverted, the metric, the cost (exit 3 while one runs)")
    lsub.add_parser("stop", help="write the loop's report and go back to the branch it started from")
    p.set_defaults(fn=cmd_loop)
    p = sub.add_parser("notify", help="notifications (testbench.yaml `notify:`): `notify test` sends one test message")
    p.add_argument("notify_cmd", choices=["test"], metavar="test",
                   help="test: send one test message and say whether it was accepted")
    p.set_defaults(fn=cmd_notify)
    sub.add_parser("doctor", help="check what this machine can run; each `no` line says what to do").set_defaults(fn=cmd_doctor)
    from .plugins import KINDS
    p = sub.add_parser("plugins", help="each kind of plug-in: its built-ins, what is installed (and from which "
                                       "package), what is broken and why")
    _json(p, "every kind and its plug-ins")
    p.set_defaults(fn=cmd_plugins)
    p = sub.add_parser("plugin", help="plug-ins: `plugin test` checks one keeps its kind's contract")
    psub = p.add_subparsers(dest="plugin_cmd", metavar="COMMAND", required=True)
    q = psub.add_parser("test", help="check plug-ins keep their kind's contract: a line for each, and each problem "
                                     "(exit 0 all do, 1 one does not)",
                        description="The conformance kit: load each plug-in and check it against its kind's contract "
                                    "(a backend prices its hardware, stops itself on the remote side, hands out "
                                    "classes with the right methods, ...). TARGET is module:attr (one object), or an "
                                    "installed package's name (every plug-in it declares); --installed tests every "
                                    "installed plug-in. In a plug-in's own tests: "
                                    "agent_testbench.plugins.assert_conforms(KIND, obj).")
    q.add_argument("target", nargs="?", metavar="TARGET",
                   help="module:attr, or an installed package's name (e.g. testbench-backend-example)")
    q.add_argument("--installed", action="store_true", help="every installed plug-in, of every kind")
    q.add_argument("--kind", metavar="KIND", help=f"test it as this kind: {', '.join(KINDS)} (default: the kind its "
                                                  "entry point declares, or the only one that takes it)")
    _json(q, "the results")
    p.set_defaults(fn=cmd_plugin)
    p = sub.add_parser("selftest", help="a minute or so, local and free: check that the main promises hold on this "
                                        "machine, one line each")
    p.add_argument("--keep", action="store_true", help="keep the temporary project and its runs to look at")
    p.set_defaults(fn=cmd_selftest)
    p = sub.add_parser("demo", help="copy a small example project to DIR, run its free plan, and show what to try next")
    p.add_argument("name", nargs="?", metavar="NAME", help=f"the example: {', '.join(DEMOS)} (default tiny-model)")
    p.add_argument("dir", nargs="?", metavar="DIR", help="a new folder (default: testbench-demo for tiny-model, the "
                                                         "example's name for the others)")
    p.add_argument("--list", action="store_true", help="the examples, and what each shows")
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
    _finish(ap)
    return ap


def _is_plan(word: str) -> bool:
    try:
        return word in C.load(C.find_root())["plans"]
    except (C.ConfigError, OSError):
        return False


def _no_command(parser, sub, prog: str, word: str, rest: list, i: int) -> str:
    """`testbench rnu`, `testbench tests run`, `testbench start`: the command it most likely is, in full."""
    later = next((k for k in range(i + 1, len(rest)) if rest[k] in sub.choices), None)
    if later is not None:                            # `testbench tests run`: the command goes first
        return (f"{word!r} is not a command; the command comes first: "
                f"`{prog} {shlex.join([rest[later], *rest[:later], *rest[later + 1:]])}`.")
    inside = [f"`{prog} {name} {word}`" for name, p in sub.choices.items()
              if (s := _subcommands(p)) is not None and word in s.choices]
    close = difflib.get_close_matches(word, list(sub.choices), n=3)
    if f"--{word}" in parser._option_string_actions:          # `testbench version`
        hint = f"did you mean `{prog} --{word}`?"
    elif inside:
        hint = f"did you mean {' or '.join(inside)}?"
    elif prog == "testbench" and _is_plan(word):
        hint = f"{word} is a plan here: `testbench run {shlex.join([word, *rest[i + 1:]])}` runs it."
    elif close:
        hint = f"did you mean `{prog} {shlex.join([close[0], *rest[i + 1:]])}`" + (
            f" (or {' or '.join(close[1:])})?" if close[1:] else "?")
    else:
        hint = ""
    every = ("`testbench help --all` lists every command" if prog == "testbench"
             else f"`{prog} --help` lists the commands")
    return f"no command {word!r}" + (f"; {hint}" if hint else ".") + f" {every}."


def _check_command(ap, argv: list) -> int | None:
    """No command, or one testbench does not have: the help, the closest command or the words in their order, instead
    of argparse's list of every choice. None when argparse takes it from here."""
    parser, prog, rest = ap, "testbench", list(argv)
    while (sub := _subcommands(parser)) is not None:
        i = next((k for k, w in enumerate(rest) if not w.startswith("-")), len(rest))
        if {"-h", "--help", "--version", "--"} & set(rest[:i]):
            return None
        if i == len(rest):
            parser.print_help()
            return 2
        if rest[i] not in sub.choices:
            print(f"{prog}: {_no_command(parser, sub, prog, rest[i], rest, i)}", file=sys.stderr)
            return 2
        parser, prog, rest = sub.choices[rest[i]], f"{prog} {rest[i]}", rest[i + 1:]
    return None


def _chosen(ap, a):
    """The (sub)command parser argparse ended in, so a mistake is reported with its usage, not testbench's."""
    parser = ap
    while (sub := _subcommands(parser)) is not None and getattr(a, sub.dest, None) in sub.choices:
        parser = sub.choices[getattr(a, sub.dest)]
    return parser


def _os_error(exc: OSError) -> str:
    if exc.filename and isinstance(exc, PermissionError) and Path(exc.filename).is_dir():
        return f"{exc.filename} is a folder, not a file"          # what Windows says when a folder is opened
    for kind, what in ((FileNotFoundError, "does not exist"), (IsADirectoryError, "is a folder, not a file"),
                       (NotADirectoryError, "is not a folder"), (PermissionError, "may not be read or written here")):
        if isinstance(exc, kind) and exc.filename:
            return f"{exc.filename} {what}"
    return str(exc)


def _look_again(parser, argv: list, exc: OSError) -> str:
    """For a file named on the command line that is not there: the closest file of its kind next to it, as the whole
    command line again; else where paths start from and what is there. '' for a path the command line did not name."""
    if not isinstance(exc, FileNotFoundError) or not exc.filename:
        return ""
    path = Path(exc.filename)
    at = next((k for k, w in enumerate(argv) if str(exc.filename) in w or path.name and path.name in (
        Path(w).name, Path(w.rsplit(":", 1)[0]).name)), None)             # nb.ipynb:1-3 names nb.ipynb
    if at is None:
        return ""
    folder = path.parent if path.parent.is_dir() else None
    names = sorted(p.name for p in folder.iterdir() if p.is_file() and p.suffix == path.suffix) if folder else []
    close = difflib.get_close_matches(path.name, names, n=1, cutoff=0.5) or names[:1] * (len(names) == 1)
    if close:
        fixed = argv[at].replace(str(exc.filename), str(path.with_name(close[0])), 1)
        fixed = fixed if fixed != argv[at] else argv[at].replace(path.name, close[0], 1)
        return f"; did you mean `{shlex.join(['testbench', *argv[:at], fixed, *argv[at + 1:]])}`?"
    kind = f"{path.suffix} files" if path.suffix else "files"
    return (f" (paths start from {Path.cwd()})" + (f"; the {kind} in {folder}: {', '.join(names[:6])}" if names else "")
            + f". `{parser.prog} --help` says what it takes")


def _utf8_on_windows(argv) -> None:
    """Windows Python reads and writes text in the legacy code page unless in UTF-8 mode, and reports and logs carry
    whatever programs print. Run once more in UTF-8 mode; everything started from there inherits it."""
    if os.name == "nt" and argv is None and not sys.flags.utf8_mode:
        os.environ["PYTHONUTF8"] = "1"
        sys.exit(subprocess.call([sys.executable, "-X", "utf8", "-m", "agent_testbench", *sys.argv[1:]]))


def main(argv=None):
    _utf8_on_windows(argv)
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["help"]:                         # `testbench help run` is `testbench run --help`
        argv = [*argv[1:], "--help"]
    ap = build_parser()
    if "--all" in argv and set(argv) <= {"--all", "-h", "--help"}:       # `testbench help --all`: every command
        ap.description = ap.every_command
        ap.print_help()
        sys.exit(0)
    code = _check_command(ap, argv)
    if code is not None:
        sys.exit(code)
    a, extra = ap.parse_known_args(argv)
    if extra:
        _chosen(ap, a).error(f"unrecognized arguments: {' '.join(extra)}")
    if not hasattr(a, "fn"):
        ap.print_help()
        sys.exit(2)
    if getattr(a, "format", None) == "json" and hasattr(a, "json"):
        a.json = True                                # --format json and --json are the same everywhere
    try:
        sys.exit(a.fn(a))
    except (C.ConfigError, NB.NotebookError) as exc:
        print(f"testbench: {exc}", file=sys.stderr)
        sys.exit(2)
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError, PermissionError) as exc:
        print(f"testbench: {_os_error(exc)}{_look_again(_chosen(ap, a), argv, exc)}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
