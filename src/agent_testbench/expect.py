"""Expectations: what a step must have produced, checked after it runs.

Each check returns {"check", "ok", "detail"} where detail states what was found, so a failure reads as a
finding ("accuracy 0.71, wanted >= 0.9") rather than a bare FAIL. A check that measured a number puts it first
("0.71 (wanted >= 0.9)"): that is how reports, history and loops read the number back.

    expect:
      exit_code: 0
      no_errors: true                       # notebooks: every cell ran without an error (on by default)
      files: [outputs/model.pt, "plots/*.png"]
      json:
        outputs/metrics.json: {accuracy: ">= 0.9", "losses.-1": "< 0.5"}
      text:
        stdout: ["epoch 10"]                 # or a file path
      media:
        out/video.mp4: {frames: ">= 120", width: 1920, audio: true}
      web:                                   # web steps
        status: 200
        text: ["Count: 2"]
        no_console_errors: true
      junit: {path: report.xml, tests: ">= 10"}       # failures: 0 and errors: 0 unless said otherwise
      metrics:                               # numbers printed: the regex's first group, in its last match
        stdout: {"val_loss=([0-9.eE+-]+)": "< 0.3"}
      gpu: {busy_pct: ">= 50", max_mem_mib: "<= 15000"}
      image:
        outputs/plot.png: {like: golden/plot.png, max_diff: 0.02}
      duration: {minutes: "<= 5"}
      mock_calls:                            # calls the step made to the mock clients (mocks: on the step)
        anthropic: {calls: "<= 5", output_tokens: "<= 2000"}       # refused: 0 unless said otherwise
        alpaca: {orders: "<= 20", orders_without_limit: 0}         # the brokers: orders, fills, fees, live, ...

Any rule can also be a tolerance ("~= 0.93 +- 0.01", "~= 100 +- 5%") or an inclusive range ("between 0.8 and 0.95").
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import counted

OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, "!=": lambda a, b: a != b, "==": lambda a, b: a == b,
       ">": lambda a, b: a > b, "<": lambda a, b: a < b}
RULE = re.compile(r"^\s*(>=|<=|!=|==|>|<)\s*(.+?)\s*$")
NEAR = re.compile(r"^\s*~=\s*(\S+?)\s*(?:\+-|\+/-|±)\s*(\S+?)\s*(%?)\s*$")
BETWEEN = re.compile(r"^\s*between\s+(\S+)\s+and\s+(\S+)\s*$", re.I)
KINDS = ("exit_code", "no_errors", "files", "json", "text", "media", "web", "junit", "metrics", "gpu", "image",
         "duration", "mock_calls")
LLM_SDKS = ("anthropic", "openai")
BROKER_SDKS = ("alpaca", "ccxt", "ib_async")
MOCK_SDKS = LLM_SDKS + BROKER_SDKS
MOCK_KEYS = ("calls", "refused", "injected", "input_tokens", "output_tokens", "tool_calls")
BROKER_KEYS = ("calls", "refused", "orders", "fills", "cancels", "orders_without_limit", "max_order_value", "fees",
               "live")
MOCK_LOG = "mock_calls.jsonl"
MEDIA_KEYS = ("frames", "width", "height", "fps", "seconds", "audio")
WEB_KEYS = ("status", "title", "text", "no_console_errors")
JUNIT_KEYS = ("tests", "passed", "failures", "errors", "skipped")
GPU_KEYS = ("busy_pct", "mean_util_pct", "max_mem_mib")
DURATION_KEYS = ("minutes", "seconds")
NO_GPU = "no GPU readings (nvidia-smi not found or the step was too short)"


def rule_error(rule) -> str | None:
    """Why a rule cannot be checked, or None. Config validation uses it, so a bad rule fails at load time."""
    if isinstance(rule, (dict, list, tuple, set)):
        return (f"a rule is one value such as \">= 0.9\", \"~= 0.93 +- 0.01\" or \"between 0.8 and 0.95\" "
                f"(or a bare value meaning equality); got {rule!r}")
    if isinstance(rule, float) and math.isnan(rule):
        return "a rule of .nan never holds, since nothing equals NaN; write the value to compare with"
    if not isinstance(rule, str):
        return None
    typo = re.match(r"\s*(=>|=<|===|=(?!=)|≥|≤|≠)", rule)
    if typo:
        return f"{rule!r}: {typo.group(1)} is not a comparison; use >=, <=, ==, !=, > or <, e.g. \">= 0.9\""
    op = RULE.match(rule)
    if re.match(r"\s*[<>=!]", rule) and (not op or op.group(2).strip()[:1] in ("", "<", ">", "=")):
        return f"{rule!r} has no value after its comparison; write one, e.g. \">= 0.9\""
    if op and op.group(1) != "!=" and isinstance(_num(op.group(2)), float) and math.isnan(_num(op.group(2))):
        return f"{rule!r} never holds, since nothing equals or orders against NaN (\"!= nan\" asks for a number)"
    if not re.match(r"\s*(~=|between\b)", rule, re.I):
        return None
    m = NEAR.match(rule) or BETWEEN.match(rule)
    try:
        a, b = (float(x) for x in m.groups()[:2]) if m else (None, None)
    except ValueError:
        a = None
    if a is None:
        return (f"{rule!r} is neither a tolerance such as \"~= 0.93 +- 0.01\" (or \"~= 100 +- 5%\") nor a range "
                "such as \"between 0.8 and 0.95\"")
    if math.isnan(a) or math.isnan(b) or m.re is NEAR and not (math.isfinite(a) and math.isfinite(b)):
        return f"{rule!r}: its numbers must be real numbers" + (" (a range may end at inf)" if m.re is BETWEEN else "")
    if m.re is BETWEEN and a > b:
        return f"{rule!r}: write the low end first, \"between {m.group(2)} and {m.group(1)}\""
    if m.re is NEAR and b < 0:
        return f"{rule!r}: the tolerance after +- must not be negative"
    return None


def compare(actual, rule) -> tuple[bool, str]:
    """rule is '>= 0.9', '== done', '~= 0.93 +- 0.01', '~= 100 +- 5%', 'between 0.8 and 0.95' (inclusive), or a
    bare value meaning equality."""
    bad = rule_error(rule)
    if bad:
        return False, f"{actual!r}, but the rule cannot be checked: {bad}"
    near = NEAR.match(rule) if isinstance(rule, str) else None
    span = BETWEEN.match(rule) if isinstance(rule, str) else None
    m = RULE.match(rule) if isinstance(rule, str) and not (near or span) else None
    op, target = (m.group(1), _num(m.group(2))) if m else ("==", rule)
    a = _num(actual)
    if isinstance(a, float) and not math.isfinite(a) and (near or span or isinstance(actual, float) or _real(target)):
        return False, f"{actual!r} is not a finite number, so it does not satisfy {rule}"      # a NaN loss passes nothing
    if near or span:
        want = f"{near.group(1)} ± {near.group(2)}{near.group(3)}" if near else \
            f"between {span.group(1)} and {span.group(2)}"
        if not _real(a):
            return False, f"{actual!r} is not a number, so it cannot be compared with {want}"
        x = _float(a)
        if span:
            return float(span.group(1)) <= x <= float(span.group(2)), f"{actual!r} (wanted {want})"
        center, tol = float(near.group(1)), float(near.group(2))
        tol = abs(center) * tol / 100 if near.group(3) else tol
        slack = 4 * math.ulp(max(abs(x), abs(center), tol))      # rounding only (0.94 is within 0.93 +- 0.01), at any scale
        ok = math.isfinite(x) and abs(x - center) <= tol + slack
        return ok, f"{actual!r} (wanted {want}; differs by {x - center:+.4g})"
    if isinstance(a, bool) and isinstance(target, str) and target.lower() in ("true", "false"):
        target = target.lower() == "true"                                                # "== true" for a JSON true
    if isinstance(a, bool) != isinstance(target, bool):                    # true is not 1, and not a number to order
        if op in ("==", "!="):
            return op == "!=", f"{actual!r} (wanted {op} {target!r})"
        return False, f"{actual!r} cannot be compared with {op} {target!r}"
    try:
        ok = OPS[op](a, target)
    except TypeError:
        return False, f"{actual!r} cannot be compared with {op} {target!r}"
    return bool(ok), f"{actual!r} (wanted {op} {target!r})"


def _real(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _float(x) -> float:
    try:
        return float(x)
    except OverflowError:                                # an integer too big for a float
        return math.inf if x > 0 else -math.inf


def _num(x):
    if isinstance(x, (int, float, bool)) or x is None:
        return x
    try:
        return float(x)
    except (TypeError, ValueError):
        return str(x).strip().strip("'\"")


def _number(text):
    """A number as printed ('12', '3.5e-4', '0.25.'), else the text itself."""
    if text is None:
        return None
    t = text.strip().rstrip(".,;:")
    try:
        return int(t) if re.fullmatch(r"[+-]?\d+", t) else float(t)
    except ValueError:
        return text


def dig(data, path: str):
    """'a.b.0' and 'losses.-1' walk dicts and lists."""
    cur = data
    for part in str(path).split("."):
        if isinstance(cur, list) and re.fullmatch(r"-?\d+", part):
            cur = cur[int(part)]
        elif isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise KeyError(path)
    return cur


def _stale(f: Path, since) -> str | None:
    """A file last written before the run started is an old result, not this run's."""
    if since and f.stat().st_mtime < since - 2:
        return "the file was last written before this run started, so it is an old result, not this run's"
    return None


def _read(source: str, workdir: Path, result: dict) -> tuple[str | None, str | None]:
    """The text of stdout (everything the step printed) or of a file; or None and why not."""
    if source == "stdout":
        return result.get("stdout", ""), None
    f = workdir / source
    if not f.is_file():
        return None, "file missing"
    try:
        stale = _stale(f, result.get("run_started"))
        return (None, stale) if stale else (f.read_text(errors="replace"), None)
    except OSError as exc:
        return None, f"cannot be read: {exc}"


def check(expect: dict, *, workdir: Path, result: dict) -> list[dict]:
    """result: the step's outcome (kind, exit_code, stdout, notebook error, web observations, gpu window, seconds
    it ran, run_started, the command it ran)."""
    out = []
    add = lambda name, ok, detail: out.append({"check": name, "ok": bool(ok), "detail": detail})
    kind = result.get("kind")
    if kind == "run" or "exit_code" in expect:
        want = expect.get("exit_code", 0)
        add(f"exit code {want}", result.get("exit_code") == want, f"exited {result.get('exit_code')}")
    if kind == "notebook" and expect.get("no_errors", True):
        err = result.get("error")
        add("every cell ran without an error", not err,
            "all cells ran" if not err else f"cell {err.get('cell')}: {err.get('ename')}: {str(err.get('evalue'))[:300]}")
    since = result.get("run_started")
    for pattern in expect.get("files", []):
        hits = sorted(glob.glob(str(workdir / pattern), recursive=True))
        fresh = [h for h in hits if not _stale(Path(h), since)]
        add(f"file {pattern}", fresh, f"{len(fresh)} found" if fresh else
            f"{len(hits)} found, all last written before this run started: old results, not this run's" if hits
            else "missing")
    for path, rules in (expect.get("json") or {}).items():
        f = workdir / path
        if any(c in str(path) for c in "*?["):           # a pattern, e.g. **/metrics.json: it must name one file
            hits = sorted({os.path.realpath(h): h for h in glob.glob(str(workdir / path), recursive=True)}.values())
            if len(hits) != 1:
                found = ", ".join(Path(h).relative_to(workdir).as_posix() for h in hits[:5])
                for key in rules:
                    add(f"{path}: {key}", False, f"{len(hits)} files match ({found}); the pattern must match exactly one"
                        if hits else "file missing (nothing matches)")
                continue
            f = Path(hits[0])
        try:
            stale = _stale(f, result.get("run_started"))
            data = None if stale else json.loads(f.read_text())
        except FileNotFoundError:
            stale = "file missing"
        except OSError as exc:
            add(f"{path}", False, f"cannot be read: {exc}")
            continue
        except (ValueError, RecursionError) as exc:      # bad JSON, not UTF-8, a number too long to read
            add(f"{path}", False, f"not valid JSON: {exc}")
            continue
        if stale:
            for key in rules:
                add(f"{path}: {key}", False, stale)
            continue
        for key, rule in rules.items():
            try:
                ok, detail = compare(dig(data, key), rule)
            except (KeyError, IndexError):
                ok, detail = False, f"no {key!r} in the file"
            add(f"{path}: {key} {rule}", ok, detail)
    for source, needles in (expect.get("text") or {}).items():
        text, why = _read(source, workdir, result)       # a log left by an earlier run does not count
        for needle in ([needles] if isinstance(needles, str) else needles):
            if text is None:
                add(f"{source} contains {needle!r}", False, why)
            else:
                add(f"{source} contains {needle!r}", needle in text, "found" if needle in text else "not found")
    for path, rules in (expect.get("media") or {}).items():
        stale = (workdir / path).is_file() and _stale(workdir / path, since)
        info = {"error": stale} if stale else probe_media(workdir / path)
        for key, rule in rules.items():
            if "error" in info:
                add(f"{path}: {key}", False, info["error"])
                continue
            ok, detail = compare(info.get(key), rule)
            add(f"{path}: {key} {rule}", ok, detail)
    if kind == "web":
        web = result.get("web") or {}
        rules = expect.get("web") or {}
        if "status" in rules:
            ok, detail = compare(web.get("status"), rules["status"])
            add(f"page status {rules['status']}", ok, detail)
        for needle in rules.get("text", []):
            add(f"page shows {needle!r}", needle in web.get("text", ""), "found" if needle in web.get("text", "") else "not found")
        if rules.get("title"):
            add(f"title contains {rules['title']!r}", rules["title"] in web.get("title", ""), repr(web.get("title")))
        if rules.get("no_console_errors", True):
            errs = web.get("console_errors", []) + web.get("page_errors", [])
            add("no console or page errors", not errs, "none" if not errs else "; ".join(errs)[:500])
        if web.get("action_errors"):
            add("every action worked", False, "; ".join(web["action_errors"])[:500])
    for spec in junit_specs(expect.get("junit")):
        _junit(spec, workdir, result, add)
    for source, patterns in (expect.get("metrics") or {}).items():
        text, why = _read(source, workdir, result)
        for pattern, rule in patterns.items():
            name = f"{source} /{pattern}/ {rule}"
            hits = list(re.finditer(pattern, text, re.M)) if text is not None else []
            if text is None or not hits:
                add(name, False, why or f"no match in {source} ({counted(len(text.splitlines()), 'line')})")
                continue
            last = hits[-1]
            ok, detail = compare(_number(last.group(1) if last.re.groups else last.group(0)), rule)
            add(name, ok, f"{detail}; {'the only match' if len(hits) == 1 else f'the last of {len(hits)} matches'}: "
                          f"{last.group(0).strip()[:80]!r}")
    for path, spec in (expect.get("image") or {}).items():
        most = spec.get("max_diff", 0.02)
        diff, note = image_diff(workdir / path, workdir / str(spec.get("like")), result.get("run_started"))
        if diff is None:
            add(f"{path} looks like {spec.get('like')}", False, note)
        else:
            ok, detail = compare(round(diff, 4), f"<= {most}")
            add(f"{path} looks like {spec.get('like')}", ok,
                f"{detail}, the mean difference per pixel and channel (0 identical, 1 opposite){note}")
    gpu = result.get("gpu")
    for key, rule in (expect.get("gpu") or {}).items():
        if not gpu:
            add(f"gpu {key} {rule}", False, NO_GPU)
            continue
        ok, detail = compare(gpu.get(key), rule)
        add(f"gpu {key} {rule}", ok, f"{detail} on {gpu.get('gpu') or 'the GPU'}, {gpu.get('readings', '?')} "
                                     "reading(s)")
    secs = result.get("seconds")
    for key, rule in (expect.get("duration") or {}).items():
        ok, detail = compare(None if secs is None else round(secs / 60, 3) if key == "minutes" else round(secs, 2), rule)
        add(f"duration {key} {rule}", ok, detail)
    spec, mocked = expect.get("mock_calls") or {}, result.get("mocks") or {}
    for sdk in [*spec, *(s for s in mocked if s not in spec)]:      # a step with mocks: on refuses no call unless said
        _mock_calls(sdk, {**(spec.get(sdk) or {}), "refused": (spec.get(sdk) or {}).get("refused", 0)}, workdir,
                    result, add)
    return out


# ---- calls to the mock clients (docs/mocks.md) ---------------------------------------------------------------

def _refusals(recs: list, kinds: list, key: str) -> str:
    hits = [r for r, k in zip(recs, kinds) if k == key]
    return "; ".join(f"call {r.get('call')}: {r['error'].get('type')}: {r['error'].get('message')}"[:300]
                     for r in hits[:2]) + (f"; and {len(hits) - 2} more" if len(hits) > 2 else "")


def _mock_calls(sdk: str, rules: dict, workdir: Path, result: dict, add) -> None:
    step_dir = result.get("step_dir")
    path = Path(step_dir) / MOCK_LOG if step_dir else workdir / MOCK_LOG
    recs = []
    for line in path.read_text(errors="replace").splitlines() if path.is_file() else []:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("sdk") == sdk:
            recs.append(rec)
    kinds = [(r.get("error") or {}).get("kind") for r in recs]
    if sdk in BROKER_SDKS:
        _broker_calls(sdk, rules, recs, kinds, result, add)
        return
    found = {"calls": len(recs), "refused": kinds.count("refused"), "injected": kinds.count("injected"),
             "tool_calls": sum(len((r.get("reply") or {}).get("tool_calls") or []) for r in recs)}
    for key in ("input_tokens", "output_tokens"):
        found[key] = sum(int((r.get("usage") or {}).get(key) or 0) for r in recs)
    if not recs:
        context = "no calls were logged" + (f" (is `mocks: [{sdk}]` on this step?)" if sdk not in
                                            (result.get("mocks") or {}) else "")
    else:
        counts = {}
        for r in recs:
            label = f"{r.get('endpoint')} {r.get('model')}" + ("" if r.get("known_model", True) else
                                                              " (not a model the mock knows)")
            counts[label] = counts.get(label, 0) + 1
        context = "; ".join(f"{label} x{n}" for label, n in counts.items())
    for key, rule in rules.items():
        ok, detail = compare(found[key], rule)
        extra = context
        if key in ("refused", "injected") and found[key]:
            extra = _refusals(recs, kinds, key)
            if key == "injected":
                hits = [r for r, k in zip(recs, kinds) if k == key]
                extra += f" ({sum(bool(r['error'].get('retried')) for r in hits)} retried by the SDK itself)"
        elif key in ("input_tokens", "output_tokens"):
            extra = f"the mock's count (words x 1.3); {context}"
        add(f"mock {sdk} {key} {rule}", ok, f"{detail}; {extra}")


def _amount(v) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return x if math.isfinite(x) else 0.0


def _order_text(rec: dict, o: dict) -> str:
    price = f" at {o['limit']}" if o.get("limit") is not None else f" stop {o['stop']}" if o.get("stop") is not None \
        else ""
    return (f"call {rec.get('call')}: {o.get('type')} {o.get('side')} {_amount(o.get('qty')):g} {o.get('symbol')}"
            f"{price}, value {_amount(o.get('value')):,.2f}")


def _broker_calls(sdk: str, rules: dict, recs: list, kinds: list, result: dict, add) -> None:
    """The mock brokers' log: every order placed, every fill and cancel, and whether a live account was asked for."""
    orders = [(r, o) for r in recs for o in r.get("orders") or [] if isinstance(o, dict)]
    fills = [f for r in recs for f in r.get("fills") or [] if isinstance(f, dict)]
    found = {"calls": len(recs), "refused": kinds.count("refused"), "orders": len(orders), "fills": len(fills),
             "cancels": sum(len(r.get("cancels") or []) for r in recs),
             "orders_without_limit": sum(o.get("limit") is None for _, o in orders),
             "max_order_value": round(max((_amount(o.get("value")) for _, o in orders), default=0.0), 6),
             "fees": round(sum(_amount(f.get("fee")) for f in fills), 6),
             "live": sum(bool(r.get("live")) for r in recs)}
    if recs:
        counts = {}
        for r in recs:
            label = f"{r.get('endpoint')}" + (" (live)" if r.get("live") else "")
            counts[label] = counts.get(label, 0) + 1
        context = "; ".join(f"{label} x{n}" for label, n in counts.items())
    else:
        context = "no calls were logged" + (f" (is `mocks: [{sdk}]` on this step?)" if sdk not in
                                            (result.get("mocks") or {}) else "")
    for key, rule in rules.items():
        ok, detail = compare(found[key], rule)
        extra = context
        if key == "refused" and found[key]:
            extra = _refusals(recs, kinds, key)
        elif key == "orders_without_limit" and found[key]:
            hits = [_order_text(r, o) for r, o in orders if o.get("limit") is None]
            extra = "; ".join(hits[:3]) + (f"; and {len(hits) - 3} more" if len(hits) > 3 else "")
        elif key == "max_order_value" and orders:
            extra = "the largest: " + _order_text(*max(orders, key=lambda ro: _amount(ro[1].get("value"))))
        elif key == "live" and found[key]:
            hits = [r for r in recs if r.get("live")]
            extra = "; ".join(f"call {r.get('call')}: {r.get('endpoint')}" + (
                f" (refused: {r['error'].get('type')})" if (r.get("error") or {}).get("kind") == "refused" else
                " (answered: allow_live is on)") for r in hits[:2]) + (f"; and {len(hits) - 2} more"
                                                                      if len(hits) > 2 else "")
        elif key == "fees":
            extra = f"{len(fills)} fill(s); {context}"
        add(f"mock {sdk} {key} {rule}", ok, f"{detail}; {extra}")


# ---- JUnit XML: a test suite's own report -------------------------------------------------------------------

def junit_specs(spec) -> list[dict]:
    """junit: report.xml | {path: report.xml, failures: 0, ...} | a list of those."""
    items = spec if isinstance(spec, list) else [spec] if spec else []
    return [{"path": x} if isinstance(x, str) else x for x in items]


# How each runner writes JUnit XML, for a step whose command ran one and wrote no report where it should.
JUNIT_HOW = (
    (r"\bcargo\b.*\bnextest\b", "cargo-nextest writes it only for a profile with a junit path: `[profile.ci.junit]` "
                                "`path = \"junit.xml\"` in .config/nextest.toml, then `cargo nextest run --profile ci` "
                                "writes target/nextest/ci/junit.xml"),
    (r"\bgotestsum\b", "gotestsum: --junitfile {path}"),
    (r"\bgo\s+test\b", "Go: `go run gotest.tools/gotestsum@v1.13.0 --junitfile {path} -- ./...` in place of go test"),
    (r"\bvitest\b", "vitest: --reporter=default --reporter=junit --outputFile.junit={path}"),
    (r"\bplaywright\b", "Playwright: --reporter=list,junit with PLAYWRIGHT_JUNIT_OUTPUT_NAME={path} in the step's env"),
    (r"\bjest\b", "jest: --reporters=default --reporters=jest-junit, with jest-junit installed; it writes junit.xml in "
                  "the working directory, or JEST_JUNIT_OUTPUT_FILE"),
    (r"\bmocha\b", "mocha: --reporter xunit --reporter-option output={path}"),
    (r"\bnode\b.*--test\b", "node --test: --test-reporter=junit --test-reporter-destination={path}"),
    (r"\bcargo\s+test\b", "cargo test writes no JUnit: cargo-nextest does (`cargo nextest run --profile ci`, with "
                          "`[profile.ci.junit]` `path = \"junit.xml\"` in .config/nextest.toml)"),
    (r"\bnpm\s+(?:run\s+)?test\b", "npm test: run its test runner with a JUnit reporter instead - jest with jest-junit, "
                                   "vitest --reporter=junit, mocha --reporter xunit"),
)


def _junit_how(command: str | None, path: str) -> str:
    how = next((h for pattern, h in JUNIT_HOW if command and re.search(pattern, command)), "pytest: add --junitxml={path}")
    return how.replace("{path}", path)


def _junit(spec: dict, workdir: Path, result: dict, add) -> None:
    path = str(spec.get("path"))
    f = workdir / path
    if not f.is_file():
        add(f"{path}: JUnit report", False, f"file missing: the test command wrote no report there "
                                            f"({_junit_how(result.get('command'), path)})")
        return
    try:
        got = _stale(f, result.get("run_started")) or read_junit(f)
    except Exception as exc:                                     # ParseError, or not a JUnit root
        got = f"not a JUnit XML report: {exc}"
    if isinstance(got, str):
        add(f"{path}: JUnit report", False, got)
        return
    for key, rule in {"failures": 0, "errors": 0, **{k: v for k, v in spec.items() if k != "path"}}.items():
        ok, detail = compare(got.get(key), rule)
        detail += "" if key == "tests" else f" of {counted(got['tests'], 'test')}"
        cases = {"failures": got["failing"], "errors": got["erroring"]}.get(key)
        if cases:
            detail += (f"; {'failed' if key == 'failures' else 'errored'}: " + "; ".join(cases[:3])
                       + (f"; and {len(cases) - 3} more" if len(cases) > 3 else ""))
        add(f"{path}: {key} {rule}", ok, detail)


def read_junit(path: Path) -> dict:
    """tests, passed, failures, errors, skipped and the failing cases of a JUnit XML report (pytest --junitxml,
    jest-junit, vitest, mocha's xunit, node --test, Playwright, gotestsum, go-junit-report, cargo-nextest, ...):
    <testsuites> or a bare <testsuite>. tests/data/junit/ holds a real report from each of those."""
    import xml.etree.ElementTree as ET
    root = ET.parse(path).getroot()
    if root.tag not in ("testsuites", "testsuite"):
        raise ValueError(f"the root element is <{root.tag}>, not <testsuites> or <testsuite>")
    out = {"tests": 0, "passed": 0, "failures": 0, "errors": 0, "skipped": 0, "failing": [], "erroring": []}
    parent = {child: el for el in root.iter() for child in el}
    cases = list(root.iter("testcase"))
    suites = [s for s in root.iter("testsuite") if s.find("testsuite") is None] or [root]
    for s in suites:
        if s.find(".//testcase") is None:                        # only totals: the suite's own counts
            got = {key: _int(s.get(key)) for key in ("tests", "failures", "errors", "skipped")}
            for key, n in got.items():
                out[key] += n
            out["passed"] += max(0, got["tests"] - got["failures"] - got["errors"] - got["skipped"])
    out["tests"] += len(cases)
    for case in cases:
        tags = {c.tag for c in case}
        out["skipped"] += "skipped" in tags
        out["passed"] += not tags & {"failure", "error", "skipped"}
        for tag, key, names in (("failure", "failures", "failing"), ("error", "errors", "erroring")):
            el = case.find(tag)
            if el is not None:
                out[key] += 1
                out[names].append(f"{_case_name(case, _suite_names(case, parent))} ({_case_why(el)})")
    more = {"failures": 0, "errors": 0}                          # e.g. a suite that failed before its cases ran
    for s in suites:
        listed = s.findall(".//testcase")
        if not listed:                                           # its totals are counted above
            continue
        said = {key: _int(s.get(key)) for key in more}
        got = {"failures": sum(c.find("failure") is not None for c in listed),
               "errors": sum(c.find("error") is not None for c in listed)}
        extra = sum(said.values()) - sum(got.values())           # the two together: mocha's xunit counts its
        if extra > 0:                                            # failures as errors, and is right about the total
            failures = min(extra, max(0, said["failures"] - got["failures"]))
            more["failures"] += failures
            more["errors"] += extra - failures
    for key, names in (("failures", "failing"), ("errors", "erroring")):
        if more[key]:
            out[names].append(f"{more[key]} more in the suites' totals than among their test cases")
            out[key] += more[key]
    return out


def _int(x) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def _suite_names(case, parent: dict) -> list[str]:
    """The names of the <testsuite>s around a test case, outermost first."""
    out, el = [], parent.get(case)
    while el is not None:
        if el.tag == "testsuite" and el.get("name"):
            out.insert(0, el.get("name"))
        el = parent.get(el)
    return out


JS_FILE = re.compile(r"\.[cm]?[jt]sx?$")                          # vitest's and Playwright's classname: the test file


def _case_name(case, suites: list[str] | None = None) -> str:
    """pytest's tests.test_math.test_add, Go's example.com/calc.TestAdd, Rust's calc.tests::adds; a file or a describe
    title before a test's title the way JS runners show it (sum.test.js > sum > adds). A case with no classname (or
    node --test's constant "test") is named after its suites, such as the package of gotestsum's TestMain."""
    cls, name = case.get("classname") or "", case.get("name") or "?"
    if cls in ("", "test"):
        return " > ".join([*(suites or []), name])
    if name == cls or name.startswith(cls + " "):
        return name
    words = re.search(r"\s", cls) or re.search(r"\s", name.split("[", 1)[0])
    js = JS_FILE.search(cls) or JS_FILE.search(case.get("file") or "")      # node 24 names the file on each case
    return f"{cls} > {name}" if words or js else f"{cls}.{name}"


GENERIC_WHY = {"failed", "failure", "error", "build error"}         # gotestsum's and go-junit-report's messages
RUNNER_LINE = re.compile(r"(?:=== (?:RUN|PAUSE|CONT|NAME)\b|--- (?:FAIL|PASS|SKIP):|●\s)")    # Go's and jest's own lines
MORE_LINE = re.compile(r"(?:left|right|Expected|Received):")        # assert_eq!'s and expect()'s values, which follow


def _case_why(el) -> str:
    """Why a case failed, in a line: the message, unless it says nothing (Go's "Failed"), else the first line of the
    text that is not the runner's own; with the lines that finish it (after a colon) and the values it compared."""
    message = " ".join((el.get("message") or "").split())
    text = (el.text or "").strip()
    if text.startswith("{"):                                       # jest-junit: a suite's error as a JSON object
        try:
            data = json.loads(text)
            text = str(data.get("message") or data.get("_originalMessage") or text) if isinstance(data, dict) else text
        except (ValueError, RecursionError):                       # not JSON after all: the text as it is
            pass
    lines = [" ".join(ln.split()) for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not RUNNER_LINE.match(ln)]
    head = message if message.lower() not in GENERIC_WHY else ""
    at = next((i for i, ln in enumerate(lines) if head and head in ln), None) if head else (0 if lines else None)
    if at is not None:
        if not head or lines[at].startswith(head):
            head = lines[at]                                       # nextest: the message is its first line, cut short
        for ln in lines[at + 1:]:
            if len(head) >= 200 or not (head.endswith(":") or MORE_LINE.match(ln)):
                break
            head += " " + ln
    return (head or message or el.get("type") or "no message")[:200]


# ---- images and media -----------------------------------------------------------------------------------------

def image_diff(path: Path, golden: Path, since=None) -> tuple[float | None, str]:
    """Mean absolute difference per pixel and channel, 0..1, with both images in RGB at the golden's size; or None
    and why not."""
    try:
        from PIL import Image, ImageChops, ImageStat
    except ImportError:
        return None, "Pillow is not installed where the step ran, so image checks cannot run: pip install pillow"
    if not path.is_file():
        return None, "file missing"
    if not golden.is_file():
        return None, f"the known-good image {golden.name} is missing (paths are relative to the project folder)"
    stale = _stale(path, since)
    if stale:
        return None, stale
    try:
        with Image.open(path) as fa, Image.open(golden) as fb:
            a, b = fa.convert("RGB"), fb.convert("RGB")
    except Exception as exc:
        return None, f"cannot read the images: {exc}"
    note = ""
    if a.size != b.size:
        note = f", after resizing it from {a.width}x{a.height} to the golden's {b.width}x{b.height}"
        a = a.resize(b.size)
    means = ImageStat.Stat(ImageChops.difference(a, b)).mean
    return sum(means) / len(means) / 255, note


def probe_media(path: Path) -> dict:
    """frames, width, height, fps, seconds and audio for a video or image, via ffprobe."""
    if not path.is_file():
        return {"error": "file missing"}
    if not shutil.which("ffprobe"):
        return {"error": "ffprobe is not installed, so media checks cannot run"}
    cmd = ["ffprobe", "-v", "error", "-count_frames", "-show_entries",
           "stream=codec_type,width,height,r_frame_rate,nb_read_frames:format=duration", "-of", "json", str(path)]
    try:
        data = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=300).stdout or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {"error": f"ffprobe failed: {exc}"}
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        return {"error": "no video or image stream"}
    return {"frames": int(video.get("nb_read_frames") or 0), "width": video.get("width"), "height": video.get("height"),
            "fps": video.get("r_frame_rate"), "seconds": float(data.get("format", {}).get("duration") or 0),
            "audio": any(s.get("codec_type") == "audio" for s in streams)}
