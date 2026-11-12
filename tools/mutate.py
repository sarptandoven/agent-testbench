#!/usr/bin/env python3
"""Mutation testing of the money paths, by hand: does a test fail when the code that guards money is wrong?

    python tools/mutate.py                       # every mutant of budget.py and the guard hook's decisions
    python tools/mutate.py --target guard --jobs 2
    python tools/mutate.py --list                # the mutants, without running anything
    python tools/mutate.py --target budget --only 3 --tests tests/test_budget_rules.py

Each mutant is one small change to src/agent_testbench/budget.py or to a decision function of hooks/guard.py: a
comparison turned (< and <=, > and >=, == and !=, in and not in, is and is not), a number moved by one, True and False
swapped, a `not` removed, `and` and `or` swapped. One at a time, in a copy of the repository (never this checkout),
the fast tests of that file run against it (`-x`: the first failure kills it). A mutant no test kills is a survivor:
a test is missing, or the change does not alter what the code does (an equivalent mutant), which the report must say.
Standard library and pytest only; not part of the test suite (tests/test_mutate_tool.py runs it on one mutant).
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TARGETS = {
    "budget": {"file": "src/agent_testbench/budget.py", "functions": None,         # every function
               "tests": ["tests/test_budget_rules.py", "tests/test_fuzz_budget.py", "tests/test_limits.py",
                         "tests/test_guardrails.py", "tests/test_refusals.py"]},
    "guard": {"file": "hooks/guard.py",
              "functions": {"_after_edit", "_num", "_prices", "_backend", "_sandbox", "_limits_changes", "_is_ledger",
                            "check_edit", "writes", "plain", "check_limits_reach", "invocations", "check_approvals",
                            "check", "decision", "ask_list", "find_root"},
              "tests": ["tests/test_guard_hook.py", "tests/test_fuzz_guard.py", "tests/test_limits.py",
                        "tests/test_guardrails.py"]},
}
SWAP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
        ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is}
IGNORED = (".git", ".testbench", "__pycache__", ".scratch", "site", ".venv", "venv", ".pytest_cache", "node_modules")


class Sites(ast.NodeVisitor):
    """Every place a mutation can go, in source order: (node position, kind, what changes)."""

    def __init__(self, functions):
        self.functions, self.inside, self.sites = functions, [], []

    def visit_FunctionDef(self, node):
        self.inside.append(node.name)
        body = node.body[1:] if ast.get_docstring(node) else node.body      # a docstring is not code
        for child in body:
            self.visit(child)
        for d in node.decorator_list + node.args.defaults + node.args.kw_defaults:
            if d is not None:
                self.visit(d)
        self.inside.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_JoinedStr(self, node):
        return                                           # message text, not a decision

    def _here(self) -> bool:
        return bool(self.inside) and (self.functions is None or self.inside[0] in self.functions)

    def add(self, node, kind, i, what):
        if self._here():
            self.sites.append({"line": node.lineno, "col": node.col_offset, "end": (node.end_lineno, node.end_col_offset),
                               "kind": kind, "i": i, "what": what, "function": self.inside[0],
                               "type": type(node).__name__})

    def visit_Compare(self, node):
        for i, op in enumerate(node.ops):
            if type(op) in SWAP:
                self.add(node, "compare", i, f"{type(op).__name__} -> {SWAP[type(op)].__name__}")
        self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, bool):
            self.add(node, "bool", 0, f"{node.value} -> {not node.value}")
        elif isinstance(node.value, (int, float)):
            self.add(node, "number", 0, f"{node.value!r} -> {node.value + 1!r}")

    def visit_UnaryOp(self, node):
        if isinstance(node.op, ast.Not):
            self.add(node, "not", 0, "`not x` -> `x`")
        self.generic_visit(node)

    def visit_BoolOp(self, node):
        self.add(node, "boolop", 0, f"{type(node.op).__name__} -> {'Or' if isinstance(node.op, ast.And) else 'And'}")
        self.generic_visit(node)


class Apply(ast.NodeTransformer):
    """Make one site's change in a fresh tree."""

    def __init__(self, site):
        self.site = site

    def _is(self, node) -> bool:
        where = (getattr(node, "lineno", None), getattr(node, "col_offset", None),
                 (getattr(node, "end_lineno", None), getattr(node, "end_col_offset", None)), type(node).__name__)
        return where == (self.site["line"], self.site["col"], tuple(self.site["end"]), self.site["type"])

    def visit_Compare(self, node):
        self.generic_visit(node)
        if self._is(node) and self.site["kind"] == "compare":
            node.ops[self.site["i"]] = SWAP[type(node.ops[self.site["i"]])]()
        return node

    def visit_Constant(self, node):
        if self._is(node):
            node.value = (not node.value) if self.site["kind"] == "bool" else node.value + 1
        return node

    def visit_UnaryOp(self, node):
        self.generic_visit(node)
        return node.operand if self._is(node) and self.site["kind"] == "not" else node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        if self._is(node):
            node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
        return node


_SOURCES: dict[str, str] = {}


def source(file: str) -> str:
    """A target file as it was when this run began: edits made while it runs change nothing here."""
    if file not in _SOURCES:
        _SOURCES[file] = (REPO / file).read_text()
    return _SOURCES[file]


def mutants(target: str) -> list[dict]:
    spec = TARGETS[target]
    src = source(spec["file"])
    finder = Sites(spec["functions"])
    finder.visit(ast.parse(src))
    lines = src.splitlines()
    return [{**s, "target": target, "file": spec["file"], "code": lines[s["line"] - 1].strip()} for s in finder.sites]


def mutated(site: dict) -> str:
    tree = Apply(site).visit(ast.parse(source(site["file"])))
    return ast.unparse(ast.fix_missing_locations(tree))


def copy_repo(dest: Path) -> Path:
    shutil.copytree(REPO, dest, ignore=shutil.ignore_patterns(*IGNORED), symlinks=True)
    return dest


def run_tests(copy: Path, tests: list[str], timeout: float) -> tuple[str, float]:
    """'killed' (a test failed), 'survived' (all passed), or 'timeout'; and the seconds it took."""
    tests = [t for t in tests if (copy / t.split("::")[0]).exists()]      # a test file not written yet
    home = copy / ".home"                                       # a mutant that ignores TESTBENCH_USER_DIR must not
    home.mkdir(exist_ok=True)                                   # reach this machine's real ~/.testbench
    env = {**os.environ, "PYTHONPATH": str(copy / "src"), "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(home),
           "USERPROFILE": str(home), "TESTBENCH_USER_DIR": str(home / ".testbench")}
    env.pop("TESTBENCH_FUZZ_EXAMPLES", None)                     # the suite's own, derandomized examples
    began = time.time()
    try:
        p = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", "-p", "no:warnings",
                            *tests], cwd=copy, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "timeout", time.time() - began
    return ("survived" if p.returncode == 0 else "killed"), time.time() - began


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    ap.add_argument("--target", choices=[*TARGETS, "all"], default="all")
    ap.add_argument("--list", action="store_true", help="list the mutants and stop")
    ap.add_argument("--only", type=int, action="append", help="run only mutant N of the target (repeatable)")
    ap.add_argument("--tests", nargs="+", help="the tests to run instead of the target's own")
    ap.add_argument("--jobs", type=int, default=1, help="mutants tested side by side, each in its own copy")
    ap.add_argument("--timeout", type=float, default=600, help="seconds one mutant's tests may take")
    ap.add_argument("--json", metavar="FILE", help="also write every result to FILE")
    a = ap.parse_args(argv)
    names = list(TARGETS) if a.target == "all" else [a.target]
    todo = [{**m, "id": f"{t}#{n}"} for t in names for n, m in enumerate(mutants(t)) if not a.only or n in a.only]
    if a.list:
        for m in todo:
            print(f"{m['id']:<11} {m['file']}:{m['line']}  {m['function']}: {m['what']}   | {m['code'][:70]}")
        return 0
    with tempfile.TemporaryDirectory(prefix="testbench-mutants-") as tmp:
        copies = [copy_repo(Path(tmp) / f"copy{j}") for j in range(max(1, a.jobs))]
        for c in copies:                                         # the sources as they were read for the mutants
            for t in names:
                (c / TARGETS[t]["file"]).write_text(source(TARGETS[t]["file"]))
        print(f"{len(todo)} mutants; baseline first (the tests must pass on the code as it is)", flush=True)
        for t in names:
            tests = a.tests or TARGETS[t]["tests"]
            (copies[0] / TARGETS[t]["file"]).write_text(ast.unparse(ast.parse(source(TARGETS[t]["file"]))))
            outcome, took = run_tests(copies[0], tests, a.timeout)
            (copies[0] / TARGETS[t]["file"]).write_text(source(TARGETS[t]["file"]))
            if outcome != "survived":
                print(f"baseline {t}: the tests do not pass on the unmutated code ({outcome}); fix that first: "
                      f"python -m pytest -x {' '.join(tests)}")
                return 2
            print(f"baseline {t}: passed in {took:.0f} s", flush=True)
        free = list(copies)

        def one(m):
            copy = free.pop()
            try:
                target = copy / m["file"]
                target.write_text(mutated(m))
                m["outcome"], m["seconds"] = run_tests(copy, a.tests or TARGETS[m["target"]]["tests"], a.timeout)
                target.write_text(source(m["file"]))
            finally:
                free.append(copy)
            print(f"{m['id']:<11} {m['outcome']:<8} {m['seconds']:5.0f} s  {m['file']}:{m['line']} "
                  f"{m['function']}: {m['what']}", flush=True)
            return m

        with ThreadPoolExecutor(max_workers=len(copies)) as pool:
            done = list(pool.map(one, todo))
    survived = [m for m in done if m["outcome"] != "killed"]
    score = 100 * (len(done) - len(survived)) / len(done) if done else 100.0
    print(f"\n{len(done)} mutants: {len(done) - len(survived)} killed, {len(survived)} survived "
          f"(mutation score {score:.1f}%)")
    for m in survived:
        print(f"  SURVIVED {m['id']}: {m['file']}:{m['line']} in {m['function']}: {m['what']}\n      {m['code']}")
    if a.json:
        Path(a.json).write_text(json.dumps(done, indent=1))
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
