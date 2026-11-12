"""testbench diff: what changed between two runs, in plain words for whoever decides what to do next.

    testbench diff                    # the latest run against the run of its plan before it
    testbench diff smoke              # a plan: its last two finished runs
    testbench diff last~1 last        # any two runs, older first: ids, prefixes, last, last~N (N runs before it)
    testbench diff A B --json         # the same as data (the MCP tool diff_runs, the GitHub Action, watch use it)

It says: the verdict and each step's status; every checked number, old -> new, by how much, and whether that is
better when the check's rule has a direction (">= 0.9" wants higher, "< 0.3" lower, "~= 0.93 +- 0.02" closer to
0.93); the checks that newly pass or fail; the evidence files added, removed or changed (by sha256); cost and
duration; the code each ran (commit, uncommitted changes, and `git diff --stat` between the commits when both are in
the repository); and where each ran (backend, Python, platform, the packages that changed version).

report.py's comparison with the previous run (every report's `delta`, its "Compared with the previous run" section)
is the core: `compare` starts from `report.compare` and `report.checked`, and adds the rest.
"""
from __future__ import annotations

import datetime as dt
import re
import subprocess
from pathlib import Path

from . import report as R, runs

SHOWN = 15                                    # files, checks or packages listed by name before "and N more"
COMMIT = re.compile(r"[0-9a-f]{7,64}")        # a commit from a report is only ever passed to git when it is one
WANTED = re.compile(r"\(wanted (>=|>|<=|<) ")
NEAR = re.compile(r"\(wanted (\S+) ± ")
VERDICT = {"fixed": "FIXED", "broke": "BROKE", "still passing": "STILL PASSING", "still failing": "STILL FAILING"}


class DiffError(ValueError):
    pass


# ---- which two runs --------------------------------------------------------------------------------------------

def _finished(root: Path, plan: str, n: int, before: str | None = None) -> list[Path]:
    """The latest n finished runs of a plan (newest first), optionally only those before the run named `before`."""
    out = []
    for p in reversed(runs.all_runs(root)):
        if before is not None and p.name >= before:
            continue
        rep = R.load(p)
        if rep and rep.get("plan") == plan and rep.get("status") in runs.DONE:
            out.append(p)
            if len(out) == n:
                break
    return out


def _plan_of(run_dir: Path) -> str:
    try:
        return str(runs.read_state(run_dir).get("plan") or "")
    except (OSError, ValueError):
        return str((R.load(run_dir) or {}).get("plan") or "")


def pick(root: Path, refs: list[str], plans=()) -> tuple[Path, Path]:
    """The two runs to compare, older first. refs: [] (the latest run and the run of its plan before it), [PLAN]
    (the plan's last two finished runs), [RUN] (it and the run of its plan before it) or [A, B]."""
    refs = [r for r in refs if r]
    if len(refs) > 2:
        raise DiffError("testbench diff compares two runs: `testbench diff A B` (run ids, last, last~N), "
                        "`testbench diff PLAN` (its last two runs) or `testbench diff` (the latest run and the one "
                        "before it)")
    if len(refs) == 2:
        return runs.resolve(root, refs[0]), runs.resolve(root, refs[1])
    ref = refs[0] if refs else None
    plan = None
    if ref in plans:
        plan = ref
    elif ref is not None:
        try:
            runs.resolve(root, ref)
        except FileNotFoundError as exc:
            if not _finished(root, ref, 1):
                raise DiffError(f"{exc}; and {ref!r} is not a plan with finished runs either"
                                + (f" (plans: {', '.join(plans)})" if plans else "")) from None
            plan = ref
    if plan is not None:
        found = _finished(root, plan, 2)
        if len(found) < 2:
            raise DiffError(f"plan {plan} has {len(found)} finished run(s), and a diff needs two: `testbench run "
                            f"{plan}` makes another")
        return found[1], found[0]
    new = runs.resolve(root, ref)
    plan = _plan_of(new)
    old = _finished(root, plan, 1, before=new.name)
    if not old:
        raise DiffError(f"run {new.name} is the first finished run of plan {plan}, so there is nothing before it to "
                        "compare with; name two runs: `testbench diff A B`")
    return old[0], new


def load(run_dir: Path) -> dict:
    """A finished run's report, or a DiffError that says why there is none to compare."""
    rep = R.load(run_dir)
    if rep is None:
        raise DiffError(f"run {Path(run_dir).name} has no report to compare (it never started, or report.json was "
                        "cut short); `testbench status` says where it is")
    if rep.get("status") not in runs.DONE:
        raise DiffError(f"run {Path(run_dir).name} is still {rep.get('status')}: wait for it (`testbench wait "
                        f"{Path(run_dir).name}`), then compare")
    _readable(rep, Path(run_dir).name)
    return rep


def _readable(rep: dict, name) -> None:
    if not isinstance(rep, dict) or not isinstance(rep.get("steps"), list) or not all(isinstance(s, dict)
                                                                                     for s in rep["steps"]):
        raise DiffError(f"report.json of run {name} is not a report testbench wrote (it has no list of steps)")


# ---- the comparison --------------------------------------------------------------------------------------------

def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def wants(check: dict) -> tuple[str | None, float | None]:
    """Which way is better for the number a check found, read from its rule: ('higher', None) for >= and >,
    ('lower', None) for <= and <, ('closer', centre) for ~=, (None, None) when the rule has no direction (==,
    between, a bare value)."""
    detail = str(check.get("detail", ""))
    m = WANTED.search(detail)
    if m:
        return ("higher" if m.group(1).startswith(">") else "lower"), None
    m = NEAR.search(detail)
    if m:
        try:
            return "closer", float(m.group(1))
        except ValueError:
            pass
    return None, None


def _judge(a, b, way, centre) -> str:
    if a is None:
        return "new"
    if b is None:
        return "gone"
    if a == b:
        return "same"
    if way == "higher":
        return "better" if b > a else "worse"
    if way == "lower":
        return "better" if b < a else "worse"
    if way == "closer":
        da, db = abs(a - centre), abs(b - centre)
        return "better" if db < da else "worse" if db > da else "moved"
    return "moved"


def _numbers(a: dict, b: dict) -> list[dict]:
    na, nb = R.checked(a), R.checked(b)
    out = []
    for key in [*nb, *(k for k in na if k not in nb)]:
        va, step, check = na[key] if key in na else (None, None, None)
        vb, step, check = nb[key] if key in nb else (None, step, check)
        way, centre = wants(check)
        out.append({"name": key, "step": str(step.get("name")), "check": str(check.get("check")), "a": va, "b": vb,
                    "delta": None if va is None or vb is None else vb - va,
                    "wants": None if way is None else f"closer to {centre:g}" if way == "closer" else way,
                    "change": _judge(va, vb, way, centre)})
    return out


def _checks(rep: dict) -> dict:
    return {(str(s.get("name")), str(c.get("check"))): c for s in rep.get("steps") or []
            for c in s.get("expectations") or [] if isinstance(c, dict)}


def _check_changes(a: dict, b: dict) -> dict:
    ca, cb = _checks(a), _checks(b)
    item = lambda k, c: {"step": k[0], "check": k[1], "ok": bool(c.get("ok")), "detail": str(c.get("detail", ""))}
    return {"now_passing": [item(k, c) for k, c in cb.items() if k in ca and c.get("ok") and not ca[k].get("ok")],
            "now_failing": [item(k, c) for k, c in cb.items() if k in ca and not c.get("ok") and ca[k].get("ok")],
            "new": [item(k, c) for k, c in cb.items() if k not in ca],
            "gone": [item(k, c) for k, c in ca.items() if k not in cb]}


def _skipped(rep: dict) -> list[str]:
    s = rep.get("skipped")
    return [n for n in s if isinstance(n, str)] if isinstance(s, list) else []


def _status_of(rep: dict, name: str) -> str | None:
    for s in rep.get("steps") or []:
        if str(s.get("name")) == name:
            return str(s.get("status"))
    return "skipped" if name in _skipped(rep) else None


def _steps(a: dict, b: dict, delta: dict) -> list[dict]:
    names = [str(s.get("name")) for s in b["steps"]] + _skipped(b) + [str(s.get("name")) for s in a["steps"]] + _skipped(a)
    out = []
    for name in dict.fromkeys(names):
        sa, sb = _status_of(a, name), _status_of(b, name)
        change = ("fixed" if name in delta.get("fixed", []) else "broke" if name in delta.get("broke", [])
                  else "still failing" if name in delta.get("still_failing", []) else "new" if sa is None
                  else "gone" if sb is None else "same" if sa == sb else "changed")
        out.append({"name": name, "a": sa, "b": sb, "change": change})
    return out


def _evidence(a: dict, b: dict) -> dict:
    def files(rep):
        ev = rep.get("evidence")
        if not isinstance(ev, list):
            return None
        return {e["path"]: e.get("sha256") for e in ev if isinstance(e, dict) and isinstance(e.get("path"), str)}
    fa, fb = files(a), files(b)
    if fa is None or fb is None:
        return {"recorded": False, "added": [], "removed": [], "changed": [], "same": 0}
    return {"recorded": True, "added": sorted(p for p in fb if p not in fa), "removed": sorted(p for p in fa if p not in fb),
            "changed": sorted(p for p in fb if p in fa and fa[p] != fb[p]),
            "same": sum(1 for p in fb if p in fa and fa[p] == fb[p])}


def _git(root: Path, *args) -> str | None:
    try:
        p = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def _changes(src: dict) -> str:
    if not src.get("dirty"):
        return "no uncommitted changes"
    n = src.get("untracked_files")
    return (f"uncommitted changes (sha256 {str(src.get('changes_sha256') or '?')[:12]}"
            + (f", {n} untracked file(s)" if isinstance(n, int) and n else "") + ")")


def _code(a: dict, b: dict, root: Path | None) -> dict:
    sa = a.get("source") if isinstance(a.get("source"), dict) else None
    sb = b.get("source") if isinstance(b.get("source"), dict) else None
    out = {"a": sa, "b": sb, "same": False, "relation": None, "stat": None}
    if not sa or not sb:
        out["same"] = not sa and not sb
        out["summary"] = ("both ran on code outside git, so what changed in it is not recorded" if out["same"] else
                          f"{'A' if not sa else 'B'} ran on code outside git, so the code cannot be compared")
        return out
    ca, cb = str(sa.get("commit") or ""), str(sb.get("commit") or "")
    if ca == cb:
        out["same"] = (not sa.get("dirty") and not sb.get("dirty")) or (
            bool(sa.get("dirty")) == bool(sb.get("dirty")) and sa.get("changes_sha256") == sb.get("changes_sha256"))
        out["summary"] = (f"the same code: commit {ca[:12]}, " + _changes(sb) + (" in both" if sb.get("dirty") else "")
                          if out["same"] else f"the same commit {ca[:12]}, but not the same uncommitted changes: A had "
                          f"{_changes(sa)}, B had {_changes(sb)}")
        return out
    out["summary"] = f"A ran commit {ca[:12]} with {_changes(sa)}; B ran commit {cb[:12]} with {_changes(sb)}"
    if root is None:
        return out
    if not (COMMIT.fullmatch(ca) and COMMIT.fullmatch(cb)):
        out["relation"] = "a commit in a report is not a commit id, so git is not asked about it"
        return out
    missing = [c[:12] for c in (ca, cb) if _git(root, "cat-file", "-e", f"{c}^{{commit}}") is None]
    if missing:
        out["relation"] = (f"commit {' and '.join(missing)} is not in this repository (rebased away, or made in "
                           "another clone), so git cannot show what changed")
        return out
    ahead, behind = (_git(root, "rev-list", "--count", f"{x}..{y}") for x, y in ((ca, cb), (cb, ca)))
    ahead, behind = int((ahead or "0").strip() or 0), int((behind or "0").strip() or 0)
    out["relation"] = (f"B is {ahead} commit(s) after A" if not behind else f"B is {behind} commit(s) before A"
                       if not ahead else f"they have diverged: {ahead} commit(s) only in B, {behind} only in A")
    stat = _git(root, "diff", "--stat", "--relative", ca, cb, "--", ".", ":(exclude).testbench")
    out["stat"] = stat.rstrip() if stat is not None else None
    return out


ENV = ("python", "implementation", "platform", "machine", "executable")


def _environment(a: dict, b: dict) -> dict:
    ea = a.get("environment") if isinstance(a.get("environment"), dict) else {}
    eb = b.get("environment") if isinstance(b.get("environment"), dict) else {}
    changes = {k: [r.get(k), s.get(k)] for k, r, s in [(k, a, b) for k in ("backend", "gpu")] + [(k, ea, eb) for k in ENV]
               if r.get(k) != s.get(k)}
    pa, pb = ea.get("package_versions"), eb.get("package_versions")
    if isinstance(pa, dict) and isinstance(pb, dict):
        packages = {"known": True, "changed": {n: [pa[n], pb[n]] for n in sorted(pa.keys() & pb.keys()) if pa[n] != pb[n]},
                    "added": {n: pb[n] for n in sorted(pb.keys() - pa.keys())},
                    "removed": {n: pa[n] for n in sorted(pa.keys() - pb.keys())}}
    else:
        both = ea.get("packages_sha256") and eb.get("packages_sha256")
        packages = {"known": False, "same": ea.get("packages_sha256") == eb.get("packages_sha256") if both else None}
    packages["count"] = [ea.get("packages"), eb.get("packages")]
    return {"recorded": bool(ea and eb), "changes": changes, "packages": packages, "steps": _runtimes(a, b),
            "b": {k: eb.get(k) for k in ("python", "implementation", "platform", "machine")}}


def _runtime(rep: dict) -> dict:
    """{step name: what it ran on, in words} for the steps that recorded it (the first, when a name repeats)."""
    out = {}
    for s in rep.get("steps") or []:
        if isinstance(s, dict) and (text := R.runtime_text(s.get("runtime"))):
            out.setdefault(str(s.get("name")), text)
    return out


def _runtimes(a: dict, b: dict) -> list[dict]:
    """Steps in both runs whose interpreter changed: another Python (a project's virtualenv made, or gone), another
    version of node, go or cargo."""
    ra, rb = _runtime(a), _runtime(b)
    return [{"step": name, "a": ra[name], "b": rb[name]} for name in rb if name in ra and ra[name] != rb[name]]


def _data(a: dict, b: dict) -> dict:
    """The data each run read, by role (report.json's `data`): the file it was cut from and what the steps saw."""
    da = a.get("data") if isinstance(a.get("data"), dict) else {}
    db = b.get("data") if isinstance(b.get("data"), dict) else {}
    roles = []
    for role in dict.fromkeys([*db, *da]):
        x, y = da.get(role), db.get(role)
        x, y = (x if isinstance(x, dict) else None), (y if isinstance(y, dict) else None)
        fx, fy = ((v or {}).get("file") if isinstance((v or {}).get("file"), dict) else {} for v in (x, y))
        brief = lambda d, f: None if d is None else {                                          # noqa: E731
            "path": d.get("path"), "view": d.get("view"), "holdout_from": d.get("holdout_from"),
            "sha256": d.get("sha256"), "rows": d.get("rows"), "first": d.get("first"), "last": d.get("last"),
            "file_sha256": f.get("sha256"), "file_rows": f.get("rows"), "file_last": f.get("last")}
        change = ("new" if x is None else "gone" if y is None
                  else "file changed" if fx.get("sha256") != fy.get("sha256")
                  else "saw other rows" if x.get("sha256") != y.get("sha256") else "same")
        roles.append({"role": str(role), "a": brief(x, fx), "b": brief(y, fy), "change": change})
    return {"recorded": bool(da or db), "roles": roles, "changed": any(r["change"] != "same" for r in roles)}


def _data_lines(d: dict) -> list[str]:
    lines = []
    view = {"sealed": "the rows before the seal", "full": "the whole file", "windows": "walk-forward windows"}
    for r in d["roles"]:
        x, y, role = r["a"], r["b"], R._line(r["role"])
        if r["change"] == "new":
            lines.append(f"- {role} (`{R._line(y['path'])}`): read only by B ({y['rows']} rows, sha256 "
                         f"{str(y['sha256'])[:12]})")
        elif r["change"] == "gone":
            lines.append(f"- {role} (`{R._line(x['path'])}`): read only by A")
        elif r["change"] == "file changed":
            lines.append(f"- {role} (`{R._line(y['path'])}`): the file changed between the runs (sha256 "
                         f"{str(x['file_sha256'])[:12]} -> {str(y['file_sha256'])[:12]}; {_g(x['file_rows'])} -> "
                         f"{_g(y['file_rows'])} rows, last {x['file_last']} -> {y['file_last']}), so their numbers "
                         "are about different data")
        elif r["change"] == "saw other rows":
            lines.append(f"- {role} (`{R._line(y['path'])}`): the same file, but the steps saw other rows: "
                         f"{view.get(x['view'], x['view'])} in A ({_g(x['rows'])} rows to {x['last']}), "
                         f"{view.get(y['view'], y['view'])} in B ({_g(y['rows'])} rows to {y['last']})")
        else:
            lines.append(f"- {role} (`{R._line(y['path'])}`): the same data ({_g(y['rows'])} rows seen, sha256 "
                         f"{str(y['sha256'])[:12]})")
    return lines


def _brief(rep: dict) -> dict:
    return {k: rep.get(k) for k in ("run_id", "plan", "status", "started", "backend", "gpu", "minutes", "cost_usd")}


def _verdict(sa, sb) -> str:
    if sa == "passed":
        return "still passing" if sb == "passed" else "broke"
    return "fixed" if sb == "passed" else "still failing"


def _span(a: dict, b: dict, key: str) -> dict:
    va, vb = _num(a.get(key)), _num(b.get(key))
    return {"a": va, "b": vb, "delta": None if va is None or vb is None else round(vb - va, 4)}


def compare(a: dict, b: dict, root: Path | None = None) -> dict:
    """Everything that changed from run A's report to run B's. root: the project folder, for `git diff --stat`
    between the two commits (None leaves git out)."""
    for rep, which in ((a, "A"), (b, "B")):
        _readable(rep, rep.get("run_id", which) if isinstance(rep, dict) else which)
    try:
        delta = R.compare(a, b) or {"fixed": [], "broke": [], "still_failing": [], "metrics_moved": {}}
    except (KeyError, TypeError, AttributeError):
        raise DiffError("a report.json has steps without a name or status: it is not a report testbench wrote") from None
    return {"a": _brief(a), "b": _brief(b), "verdict": _verdict(a.get("status"), b.get("status")),
            "fixed": delta["fixed"], "broke": delta["broke"], "still_failing": delta["still_failing"],
            "steps": _steps(a, b, delta), "numbers": _numbers(a, b), "checks": _check_changes(a, b),
            "evidence": _evidence(a, b), "cost_usd": _span(a, b, "cost_usd"), "minutes": _span(a, b, "minutes"),
            "code": _code(a, b, root), "environment": _environment(a, b), "data": _data(a, b)}


# ---- in words --------------------------------------------------------------------------------------------------

def _g(v) -> str:
    return "-" if v is None else R._g(v)


def _signed(v) -> str:
    return f"{v:+.4g}" if isinstance(v, (int, float)) else "?"


def _some(items: list[str], limit: int = SHOWN) -> list[str]:
    return items[:limit] + ([f"... and {len(items) - limit} more"] if len(items) > limit else [])


def _when(t) -> str:
    return dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if isinstance(t, (int, float)) else "?"


def _number_line(n: dict) -> str:
    if n["change"] == "new":
        return f"- {R._line(n['name'])}: new in B, {_g(n['b'])}"
    if n["change"] == "gone":
        return f"- {R._line(n['name'])}: not in B (A found {_g(n['a'])})"
    way = {"better": f"better: the rule wants {n['wants']}", "worse": f"worse: the rule wants {n['wants']}",
           "moved": "the rule says no direction"}.get(n["change"], "")
    return f"- {R._line(n['name'])}: {_g(n['a'])} -> {_g(n['b'])} ({_signed(n['delta'])}; {way})"


def render(d: dict) -> str:
    """The comparison as markdown, for an agent or a person: what changed first, then the details."""
    a, b = d["a"], d["b"]
    plan = f"plan `{b['plan']}`" if a["plan"] == b["plan"] else f"plans `{a['plan']}` and `{b['plan']}`"
    went = {"still passing": "passed both times", "still failing": "did not pass either time"}.get(
        d["verdict"], f"went from {R._icon(a['status'])} to {R._icon(b['status'])}")
    lines = [f"# {VERDICT[d['verdict']]}: {plan} {went}", "",
             f"A is run `{a['run_id']}` ({a['status']}, {_when(a['started'])}); B is run `{b['run_id']}` "
             f"({b['status']}, {_when(b['started'])}).", "", "## Steps", ""]
    label = {"fixed": "Fixed", "broke": "Broke", "still failing": "Still failing", "new": "Only in B",
             "gone": "Only in A", "changed": "Changed"}
    lines += [f"- {label[s['change']]}: {R._line(s['name'])} ({s['a'] or '-'} -> {s['b'] or '-'})"
              for s in d["steps"] if s["change"] != "same"]
    same = [f"{R._line(s['name'])} ({s['b']})" for s in d["steps"] if s["change"] == "same"]
    if same:
        lines += ["- Unchanged: " + ", ".join(same)]
    if not d["steps"]:
        lines += ["- no step ran in either run"]
    lines += ["", "## Checked numbers", ""]
    moved = [n for n in d["numbers"] if n["change"] != "same"]
    lines += [_number_line(n) for n in moved]
    unchanged = [f"{R._line(n['name'])} {_g(n['b'])}" for n in d["numbers"] if n["change"] == "same"]
    if unchanged:
        lines += ["- Unchanged: " + "; ".join(_some(unchanged))]
    if not d["numbers"]:
        lines += ["- no check found a number in either run"]
    lines += ["", "## Checks", ""]
    c = d["checks"]
    for label, key in (("Now failing", "now_failing"), ("Now passing", "now_passing")):
        lines += [f"- {label}: {R._line(x['step'])}: {R._line(x['check'])} - {R._line(x['detail'])}" for x in c[key]]
    if c["new"]:
        lines += [f"- New in B ({len(c['new'])}): " + "; ".join(_some(
            [f"{R._line(x['check'])} ({'ok' if x['ok'] else 'missed'})" for x in c["new"]]))]
    if c["gone"]:
        lines += [f"- Not made in B ({len(c['gone'])}): " + "; ".join(_some([R._line(x["check"]) for x in c["gone"]]))]
    if not any(c.values()):
        lines += ["- the same checks, with the same results"]
    ev = d["evidence"]
    lines += ["", "## Evidence", ""]
    if ev["recorded"]:
        lines += [f"By sha256: {len(ev['changed'])} changed, {len(ev['added'])} added, {len(ev['removed'])} removed, "
                  f"{ev['same']} the same."]
        for label in ("changed", "added", "removed"):
            lines += [f"- {label}: `{p}`" for p in _some(ev[label])]
    else:
        lines += ["Not compared: a run from before testbench recorded evidence hashes."]
    cost, mins = d["cost_usd"], d["minutes"]
    money = lambda v: "?" if v is None else f"${v:.3f}"
    lines += ["", "## Cost and time", "",
              f"- cost: {money(cost['a'])} -> {money(cost['b'])}" + (f" ({cost['delta']:+.3f})" if cost["delta"] else ""),
              f"- minutes: {_g(mins['a'])} -> {_g(mins['b'])}" + (f" ({_signed(mins['delta'])})" if mins["delta"] else "")]
    code = d["code"]
    lines += ["", "## Code", "", code["summary"] + (f"; {code['relation']}" if code["relation"] else "") + "."]
    if code["stat"]:
        stat = code["stat"].splitlines()
        if len(stat) > 31:                                   # the files, then git's summary line
            stat = stat[:30] + [f" ... and {len(stat) - 31} more files", stat[-1]]
        lines += ["", "```", *stat, "```"]
    elif code["stat"] == "":
        lines += ["", "No file of the project changed between the two commits."]
    if (d.get("data") or {}).get("recorded"):
        lines += ["", "## Data", "", *_data_lines(d["data"])]
    lines += ["", "## Where it ran", "", *_where(d)]
    return R._encodable("\n".join(lines).rstrip() + "\n")


def _where(d: dict) -> list[str]:
    env, b = d["environment"], d["b"]
    pk = env["packages"]
    lines = [f"- {k}: {x if x is not None else '-'} -> {y if y is not None else '-'}" for k, (x, y) in
             env["changes"].items() if env["recorded"] or k in ("backend", "gpu")]
    lines += [f"- step {R._line(s['step'])} ran on another interpreter: {R._line(s['a'])} -> {R._line(s['b'])}"
              for s in env.get("steps") or []]
    if not env["recorded"]:
        return lines + ["- the Python and packages were not recorded for one run or both (cut short before its first "
                        "step, or from an older testbench)"]
    if pk["known"]:
        moved = [f"{n} {x} -> {y}" for n, (x, y) in pk["changed"].items()]
        moved += [f"added {n} {v}" for n, v in pk["added"].items()] + [f"removed {n} {v}" for n, v in pk["removed"].items()]
        if moved:
            lines += [f"- packages ({len(moved)} changed): " + "; ".join(_some(moved))]
    elif pk["same"] is False:
        lines += [f"- the installed packages differ ({_g(pk['count'][0])} -> {_g(pk['count'][1])} packages); these runs "
                  "did not record versions, so which ones changed is not known"]
    if lines:
        return lines
    e = env["b"]
    return [f"- the same: {b['backend']}{' ' + str(b['gpu']) if b.get('gpu') else ''}, {e['implementation'] or 'Python'} "
            f"{e['python']} on {e['platform']} ({e['machine']}), {_g(pk['count'][1])} packages at the same versions"]


def _short(check: str) -> str:
    return R._line(check)[:80]


def summary(d: dict, limit: int = 3) -> str:
    """One line: what changed since run A (testbench watch prints it after each run)."""
    parts = [f"broke {', '.join(d['broke'])}"] if d["broke"] else []
    parts += [f"fixed {', '.join(d['fixed'])}"] if d["fixed"] else []
    moved = [n for n in d["numbers"] if n["change"] in ("better", "worse", "moved")]
    parts += [f"{_short(n['check'])}: {_g(n['a'])} -> {_g(n['b'])}" + (f" ({n['change']})" if n["change"] != "moved"
                                                                         else "") for n in moved[:limit]]
    if len(moved) > limit:
        parts.append(f"{len(moved) - limit} more number(s) moved")
    for key, what in (("now_failing", "now failing"), ("now_passing", "now passing")):
        if d["checks"][key] and not (d["broke"] if key == "now_failing" else d["fixed"]):
            parts.append(f"{len(d['checks'][key])} check(s) {what}")
    changed = [r["role"] for r in (d.get("data") or {}).get("roles", []) if r["change"] == "file changed"]
    if changed:
        parts.append(f"the data changed ({', '.join(changed)})")
    other = (d.get("environment") or {}).get("steps") or []
    if other:
        parts.append(f"{other[0]['step']} ran on {other[0]['b']} (was {other[0]['a']})"
                     + (f" and {len(other) - 1} more step(s) on another interpreter" if len(other) > 1 else ""))
    return "; ".join(parts) or "no change in step results or checked numbers"
