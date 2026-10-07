"""Expectations: what a step must have produced, checked after it runs.

Each check returns {"check", "ok", "detail"} where detail states what was found, so a failure reads as a
finding ("accuracy 0.71, wanted >= 0.9") rather than a bare FAIL.

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


def compare(actual, rule) -> tuple[bool, str]:
    """rule is '>= 0.9', '== done', or a bare value meaning equality."""
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


def check(expect: dict, *, workdir: Path, result: dict) -> list[dict]:
    """result: the step's outcome (kind, exit_code, stdout, notebook error, web observations)."""
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
    return out


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
