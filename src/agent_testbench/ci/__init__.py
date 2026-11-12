"""testbench ci: a ready CI job for GitLab, Azure Pipelines, CircleCI or any CI that runs a shell script, and the
JUnit file `testbench check --junit` and `testbench run --junit` write for the CI to show.

The jobs are files in this folder, so they are versioned with the package and tested (tests/test_ci.py):

    gitlab.yml     .gitlab-ci.yml           the test report from artifacts:reports:junit
    azure.yml      azure-pipelines.yml      the Tests tab from PublishTestResults@2
    circleci.yml   .circleci/config.yml     the Tests tab from store_test_results
    shell.sh       testbench-ci.sh          any other CI: its JUnit reporter reads .testbench/junit/*.xml
    job.sh         what each of them runs, put where its {{job}} line is

Each installs this version of agent-testbench, runs `testbench check` (every free plan) or `testbench run` for each
plan named in TESTBENCH_PLANS, keeps .testbench/runs/ and the JUnit files, and fails on anything but exit 0: 1 failed,
2 refused (nothing ran, so nothing was proved), 3 still running. None of them approves a cost: a plan above the
approval threshold is refused in CI, and a person runs it themselves.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from .. import __version__

JUNIT_DIR = ".testbench/junit"
PLAN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,40}")          # config._plan's rule for a plan's name
TEMPLATES = {   # name: (file here, where the CI reads it, the CI, what shows the JUnit results)
    "gitlab": ("gitlab.yml", ".gitlab-ci.yml", "GitLab CI/CD", "artifacts:reports:junit"),
    "azure": ("azure.yml", "azure-pipelines.yml", "Azure Pipelines", "PublishTestResults@2"),
    "circleci": ("circleci.yml", ".circleci/config.yml", "CircleCI", "store_test_results"),
    "shell": ("shell.sh", "testbench-ci.sh", "any CI that runs a shell script", f"{JUNIT_DIR}/*.xml"),
}
SET_UP = {      # what the person does once the file is written
    "gitlab": "Commit it: every pipeline runs the job, and the pipeline's and merge request's test report show each "
              "step. Paid plans need their credentials as masked CI/CD variables (Settings > CI/CD > Variables).",
    "azure": "Commit it, then create a pipeline from it (Pipelines > New pipeline > Existing Azure Pipelines YAML "
             "file): every branch and pull request runs it, and the run's Tests tab shows each step. Paid plans need "
             "their credentials as secret variables, passed to the testbench step under env:.",
    "circleci": "Commit it and set the project up in CircleCI: every push runs the job, and its Tests tab shows each "
                "step. Paid plans need their credentials as project environment variables or a context.",
    "shell": "Have the CI run `sh testbench-ci.sh` in the project's folder, read JUnit from .testbench/junit/*.xml "
             "and keep .testbench/runs/ as an artifact; the script's exit code is the job's.",
}


def _read(name: str) -> str:
    from importlib import resources               # here, as the XML below: `testbench --help` reads TEMPLATES only
    return (resources.files(__package__) / name).read_text(encoding="utf-8")


def render(kind: str, plans=(), version: str = __version__) -> str:
    """The CI job `kind` as text: job.sh in place of its {{job}} line (indented as that line is), this version of
    agent-testbench pinned, and `plans` (names; none: testbench check) as TESTBENCH_PLANS."""
    if kind not in TEMPLATES:
        raise ValueError(f"no CI template {kind!r}; there are {', '.join(TEMPLATES)}")
    plans = list(plans)
    for name in plans:
        if not isinstance(name, str) or not PLAN_NAME.fullmatch(name):
            raise ValueError(f"{name!r} is not a plan name (letters, digits, - or _, as testbench.yaml names plans)")
    job, lines = _read("job.sh").splitlines(), []
    for line in _read(TEMPLATES[kind][0]).splitlines():
        if line.strip() == "{{job}}":
            indent = line[:len(line) - len(line.lstrip())]
            lines += [indent + j if j else "" for j in job]
        else:
            lines.append(line)
    return "\n".join(lines).replace("{{version}}", version).replace("{{plans}}", " ".join(plans)) + "\n"


def listing() -> str:
    """`testbench ci` with no --for: each template, the file it becomes and where its JUnit results show."""
    rows = [f"  {name:<9} {writes:<21} {ci}; JUnit: {junit}" for name, (_, writes, ci, junit) in TEMPLATES.items()]
    about = ("CI jobs that run `testbench check` (every free plan) - or `testbench run PLAN` for each --plan - publish "
             "the JUnit results the CI shows, keep .testbench/runs/ as artifacts and fail on anything but exit 0 (a "
             f"refusal, exit 2, included). Each pins agent-testbench {__version__} and never approves a cost.")
    how = ("`testbench ci --for gitlab` prints one; `-o .gitlab-ci.yml` writes it; `--plan NAME` (repeatable) runs "
           "those plans instead of testbench check.")
    return "\n".join([about, "", *rows, "", how])


def main(kind: str | None, plans: list[str], output: str | None = None, force: bool = False) -> int:
    """`testbench ci`: the templates, or one printed or written (-o). Exit 0, or 2 with what to fix."""
    if not kind:
        print(listing())
        return 0
    try:
        text = render(kind, plans)
    except ValueError as exc:
        print(f"testbench ci: {exc}", file=sys.stderr)
        return 2
    unknown = _unknown_plan(plans)
    if unknown:
        print(f"testbench ci: {unknown}", file=sys.stderr)
        return 2
    if not output:
        sys.stdout.write(text)
        return 0
    path = Path(output)
    if path.exists() and not force:
        print(f"testbench ci: {path} is there already; nothing was written. Merge the job from `testbench ci --for "
              f"{kind}` into it by hand, or pass --force to replace it.", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if kind == "shell":
        path.chmod(path.stat().st_mode | 0o111)
    runs = (f"`testbench run` for {', '.join(plans)}" if plans else "`testbench check`")
    print(f"wrote {path}: {TEMPLATES[kind][2]} runs {runs} with agent-testbench {__version__}. {SET_UP[kind]}")
    return 0


def _unknown_plan(plans: list[str]) -> str | None:
    """Where a testbench.yaml is found, a --plan it does not have (with the closest one); else None."""
    if not plans:
        return None
    from .. import config as C
    try:
        root = C.find_root()
        if not (root / C.CONFIG_NAME).is_file():
            return None
        cfg = C.load(root)
    except C.ConfigError:
        return None                       # no testbench.yaml here yet, or one the job itself will report on
    missing = [p for p in plans if p not in cfg["plans"]]
    return C.no_plan(cfg, missing[0]) if missing else None


# The JUnit file: each run's own test suite (report.render_junit: a test case per step), and a suite of one test case
# for each plan that did not run - an error when it was refused (a refusal is never a pass), skipped when it costs
# money or needs a tool this machine lacks, as `testbench check` says.

def _clean(text) -> str:
    from ..report import NOT_XML
    return NOT_XML.sub("", str(text if text is not None else ""))


def _one_case(name: str, case: str, kind: str, message: str, detail: str = ""):
    import xml.etree.ElementTree as ET
    suite = ET.Element("testsuite", name=_clean(name))
    tc = ET.SubElement(suite, "testcase", classname=_clean(name), name=case, time="0.0")
    if kind == "skipped":
        ET.SubElement(tc, "skipped", message=_clean(message)[:500])
    else:
        ET.SubElement(tc, kind, message=_clean(message)[:500], type="refused" if kind == "error" else kind).text = \
            _clean(detail or message)
    return suite


def _suite(entry: dict):
    import xml.etree.ElementTree as ET
    from .. import report as R, runs
    plan, status, why = entry["plan"], entry.get("status"), entry.get("why") or ""
    run_dir = Path(entry["run_dir"]) if entry.get("run_dir") else None
    rep = R.load(run_dir) if run_dir else None
    if rep:
        suite = ET.fromstring(R.render_junit(rep)).find("testsuite")
        if status not in runs.DONE:                 # still running: nothing it found is the plan's verdict yet
            tc = ET.SubElement(suite, "testcase", classname=_clean(plan), name="run finished", time="0.0")
            ET.SubElement(tc, "error", message="still running when testbench stopped following it",
                          type="still running").text = _clean(runs.status_line(run_dir))
        return suite
    if run_dir is not None:                         # it started, and ended (or is going) without a report
        return _one_case(plan, "run", "error", f"{status or 'no report'}: the run wrote no report",
                         runs.status_line(run_dir))
    if status in ("skipped", "not run"):
        return _one_case(plan, "launch", "skipped", why + (f". Install: {entry['install']}" if entry.get("install")
                                                           else ""))
    return _one_case(plan, "launch", "error", f"refused, nothing ran: {why}", why)


def junit(entries: list[dict], message: str | None = None, title: str = "testbench check") -> str:
    """JUnit XML for the plans of a `testbench check` or `testbench run`. entries: {plan, status, why, install,
    run_dir} as check reports them. With no entries and a message (nothing to check, no such plan, a testbench.yaml
    that does not load), one error test case says why nothing ran."""
    import xml.etree.ElementTree as ET
    root = ET.Element("testsuites", name="agent-testbench")
    suites = [_suite(e) for e in entries]
    if not suites:
        suites = [_one_case(title, "check" if title == "testbench check" else "launch", "error",
                            f"nothing ran: {message or 'nothing to check'}", message or "")]
    totals = dict.fromkeys(("tests", "failures", "errors", "skipped"), 0)
    seconds = 0.0
    for suite in suites:
        cases = suite.findall("testcase")
        counts = {"tests": len(cases), "failures": sum(c.find("failure") is not None for c in cases),
                  "errors": sum(c.find("error") is not None for c in cases),
                  "skipped": sum(c.find("skipped") is not None for c in cases)}
        for k, v in counts.items():
            suite.set(k, str(v))
            totals[k] += v
        seconds += float(suite.get("time") or 0)
        suite.set("time", suite.get("time") or "0.0")
        root.append(suite)
    for k, v in totals.items():
        root.set(k, str(v))
    root.set("time", f"{seconds:.1f}")
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def write_junit(path: str | os.PathLike, entries: list[dict], message: str | None = None,
                title: str = "testbench check") -> bool:
    """Write junit(...) to path (its folder made as needed). A file that cannot be written is said on stderr and does
    not change the command's exit code, which is the run's."""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(junit(entries, message, title), encoding="utf-8")
        return True
    except OSError as exc:
        print(f"testbench: could not write the JUnit file {target}: {exc}", file=sys.stderr)
        return False
