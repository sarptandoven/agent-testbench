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

Any rule can also be a tolerance ("~= 0.93 +- 0.01", "~= 100 +- 5%") or an inclusive range ("between 0.8 and 0.95").
"""
from __future__ import annotations

import glob
import json
import re
import shutil
import subprocess
from pathlib import Path

OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, "!=": lambda a, b: a != b, "==": lambda a, b: a == b,
       ">": lambda a, b: a > b, "<": lambda a, b: a < b}
RULE = re.compile(r"^\s*(>=|<=|!=|==|>|<)\s*(.+?)\s*$")
NEAR = re.compile(r"^\s*~=\s*(\S+?)\s*(?:\+-|\+/-|±)\s*(\S+?)\s*(%?)\s*$")
BETWEEN = re.compile(r"^\s*between\s+(\S+)\s+and\s+(\S+)\s*$", re.I)
KINDS = ("exit_code", "no_errors", "files", "json", "text", "media", "web", "junit", "metrics", "gpu", "image",
         "duration")
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
    if not isinstance(rule, str) or not re.match(r"\s*(~=|between\b)", rule, re.I):
        return None
    m = NEAR.match(rule) or BETWEEN.match(rule)
    try:
        a, b = (float(x) for x in m.groups()[:2]) if m else (None, None)
    except ValueError:
        a = None
    if a is None:
        return (f"{rule!r} is neither a tolerance such as \"~= 0.93 +- 0.01\" (or \"~= 100 +- 5%\") nor a range "
                "such as \"between 0.8 and 0.95\"")
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
    if near or span:
        a = _num(actual)
        want = f"{near.group(1)} ± {near.group(2)}{near.group(3)}" if near else \
            f"between {span.group(1)} and {span.group(2)}"
        if not isinstance(a, (int, float)) or isinstance(a, bool):
            return False, f"{actual!r} is not a number, so it cannot be compared with {want}"
        if span:
            return float(span.group(1)) <= a <= float(span.group(2)), f"{actual!r} (wanted {want})"
        center, tol = float(near.group(1)), float(near.group(2))
        tol = abs(center) * tol / 100 if near.group(3) else tol
        ok = abs(a - center) <= tol + 1e-12 * max(1.0, abs(center), tol)        # 0.94 is within 0.93 +- 0.01
        return ok, f"{actual!r} (wanted {want}; differs by {a - center:+.4g})"
    m = RULE.match(rule) if isinstance(rule, str) else None
    op, target = (m.group(1), _num(m.group(2))) if m else ("==", rule)
    try:
        a = _num(actual)
        ok = OPS[op](a, target)
    except TypeError:
        return False, f"{actual!r} cannot be compared with {op} {target!r}"
    return bool(ok), f"{actual!r} (wanted {op} {target!r})"


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
    stale = _stale(f, result.get("run_started"))
    return (None, stale) if stale else (f.read_text(errors="replace"), None)


def check(expect: dict, *, workdir: Path, result: dict) -> list[dict]:
    """result: the step's outcome (kind, exit_code, stdout, notebook error, web observations, gpu window, seconds
    it ran, run_started)."""
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
    for pattern in expect.get("files", []):
        hits = sorted(glob.glob(str(workdir / pattern), recursive=True))
        add(f"file {pattern}", hits, f"{len(hits)} found" if hits else "missing")
    for path, rules in (expect.get("json") or {}).items():
        f = workdir / path
        if any(c in str(path) for c in "*?["):           # a pattern, e.g. **/metrics.json: it must name one file
            hits = sorted(glob.glob(str(workdir / path), recursive=True))
            if len(hits) != 1:
                found = ", ".join(Path(h).relative_to(workdir).as_posix() for h in hits[:5])
                for key in rules:
                    add(f"{path}: {key}", False, f"{len(hits)} files match ({found}); the pattern must match exactly one"
                        if hits else "file missing (nothing matches)")
                continue
            f = Path(hits[0])
        try:
            data = json.loads(f.read_text())
        except FileNotFoundError:
            for key in rules:
                add(f"{path}: {key}", False, "file missing")
            continue
        except json.JSONDecodeError as exc:
            add(f"{path}", False, f"not valid JSON: {exc}")
            continue
        for key, rule in rules.items():
            try:
                ok, detail = compare(dig(data, key), rule)
            except (KeyError, IndexError):
                ok, detail = False, f"no {key!r} in the file"
            add(f"{path}: {key} {rule}", ok, detail)
    for source, needles in (expect.get("text") or {}).items():
        if source == "stdout":
            text = result.get("stdout", "")
        else:
            f = workdir / source
            text = f.read_text(errors="replace") if f.is_file() else None
        for needle in ([needles] if isinstance(needles, str) else needles):
            if text is None:
                add(f"{source} contains {needle!r}", False, "file missing")
            else:
                add(f"{source} contains {needle!r}", needle in text, "found" if needle in text else "not found")
    for path, rules in (expect.get("media") or {}).items():
        info = probe_media(workdir / path)
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
                add(name, False, why or f"no match in {source} ({len(text.splitlines())} lines)")
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
    return out


# ---- JUnit XML: a test suite's own report -------------------------------------------------------------------

def junit_specs(spec) -> list[dict]:
    """junit: report.xml | {path: report.xml, failures: 0, ...} | a list of those."""
    items = spec if isinstance(spec, list) else [spec] if spec else []
    return [{"path": x} if isinstance(x, str) else x for x in items]


def _junit(spec: dict, workdir: Path, result: dict, add) -> None:
    path = str(spec.get("path"))
    f = workdir / path
    if not f.is_file():
        add(f"{path}: JUnit report", False, f"file missing: the test command wrote no report there (pytest: add "
                                            f"--junitxml={path})")
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
        detail += "" if key == "tests" else f" of {got['tests']} tests"
        cases = {"failures": got["failing"], "errors": got["erroring"]}.get(key)
        if cases:
            detail += (f"; {'failed' if key == 'failures' else 'errored'}: " + "; ".join(cases[:3])
                       + (f"; and {len(cases) - 3} more" if len(cases) > 3 else ""))
        add(f"{path}: {key} {rule}", ok, detail)


def read_junit(path: Path) -> dict:
    """tests, passed, failures, errors, skipped and the failing cases of a JUnit XML report (pytest --junitxml,
    jest-junit, go-junit-report, ...): <testsuites> or a bare <testsuite>."""
    import xml.etree.ElementTree as ET
    root = ET.parse(path).getroot()
    if root.tag not in ("testsuites", "testsuite"):
        raise ValueError(f"the root element is <{root.tag}>, not <testsuites> or <testsuite>")
    out = {"tests": 0, "passed": 0, "failures": 0, "errors": 0, "skipped": 0, "failing": [], "erroring": []}
    cases = list(root.iter("testcase"))
    if not cases:                                                # only totals: the innermost suites' own counts
        suites = [s for s in root.iter("testsuite") if s.find("testsuite") is None] or [root]
        for key in ("tests", "failures", "errors", "skipped"):
            out[key] = sum(_int(s.get(key)) for s in suites)
        out["passed"] = max(0, out["tests"] - out["failures"] - out["errors"] - out["skipped"])
        return out
    out["tests"] = len(cases)
    for case in cases:
        tags = {c.tag for c in case}
        out["skipped"] += "skipped" in tags
        out["passed"] += not tags & {"failure", "error", "skipped"}
        for tag, key, names in (("failure", "failures", "failing"), ("error", "errors", "erroring")):
            el = case.find(tag)
            if el is not None:
                out[key] += 1
                out[names].append(f"{_case_name(case)} ({_case_why(el)})")
    return out


def _int(x) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def _case_name(case) -> str:
    cls, name = case.get("classname") or "", case.get("name") or "?"
    return name if not cls or name == cls or name.startswith(cls + " ") else f"{cls}.{name}"


def _case_why(el) -> str:
    text = el.get("message") or next((ln for ln in (el.text or "").splitlines() if ln.strip()), "") \
        or el.get("type") or "no message"
    return " ".join(text.split())[:200]


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
