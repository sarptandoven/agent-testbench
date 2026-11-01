"""Web steps: start the app, open it in a headless browser, act like a user, and record what happened.

    web:
      serve: python -m http.server 8000 -d site     # optional: started first, stopped afterwards
      url: http://localhost:8000
      actions:
        - click: "#increment"
        - fill: {selector: "#name", text: "Ada"}
        - press: Enter
        - wait_for: "text=Saved"
        - screenshot: after-save                   # extra screenshots by name
      screenshot: true                             # a full-page screenshot at the end

Needs `pip install "agent-testbench[web]"` and `playwright install chromium`.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import procs


LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
LAUNCH = """\
import json
try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        print(json.dumps({"ok": True, "version": b.version}))
        b.close()
except Exception as exc:
    print(json.dumps({"ok": False, "error": str(exc)}))
"""


def install_command(deps: bool = False) -> str:
    """The command that installs Chromium (or, with deps, the system libraries it needs) for this Python's
    Playwright: a bare `playwright` on PATH may belong to another Python, with another Playwright."""
    return f"{shlex.quote(sys.executable)} -m playwright {'install-deps' if deps else 'install'} chromium"


def explain_launch_error(error: str) -> str:
    """What a Chromium that did not launch lacks, in plain words, and the command that fixes it."""
    missing = re.search(r"Executable doesn't exist at (.+)", error)      # to the end of the line: a path may hold spaces
    if missing:
        path = Path(missing.group(1).strip())
        folder = next((p for p in path.parents if re.fullmatch(r"[a-z_]+-\d+", p.name)), None)
        if folder is not None:
            kind, build = folder.name.rsplit("-", 1)
            try:
                have = sorted(p.name.rsplit("-", 1)[1] for p in folder.parent.glob(f"{kind}-*")
                              if p.name.rsplit("-", 1)[1].isdigit())
            except OSError:
                have = []
            try:
                from importlib.metadata import version
                who = f"Playwright {version('playwright')}"
            except Exception:
                who = "This Playwright"
            return (f"{who} needs Chromium build {build} ({folder.name}), which is not installed; {folder.parent} has "
                    + (f"build{'s' * (len(have) > 1)} {', '.join(have)}, for another Playwright version" if have
                       else "none") + f". Install it with: {install_command()}")
        return f"Chromium is not installed ({path} is missing). Install it with: {install_command()}"
    if "missing dependencies" in error or "error while loading shared libraries" in error:
        return ("Chromium is installed, but this machine lacks system libraries it needs. Install them with: "
                f"{install_command(deps=True)} (it uses the system's package manager, so it may ask for sudo)")
    first = next((ln.strip() for ln in error.splitlines() if ln.strip()), "no reason given")
    return f"Chromium did not start ({first[:300]}). Reinstall it with: {install_command()}"


def chromium_check(timeout: float = 30) -> tuple[bool, str]:
    """(whether Playwright's Chromium launches headless here, what was found): a real launch, in a process of its
    own, since an installed playwright package says nothing about the browser build it needs."""
    try:
        p = subprocess.run([sys.executable, "-c", LAUNCH], capture_output=True, text=True, timeout=timeout)
        got = json.loads((p.stdout.strip().splitlines() or ["{}"])[-1])
    except subprocess.TimeoutExpired:
        return False, f"Chromium did not start within {timeout:g} s. Reinstall it with: {install_command()}"
    except (OSError, ValueError) as exc:
        return False, f"the check could not run ({exc})"
    if got.get("ok"):
        return True, f"Chromium {got.get('version')} launches headless"
    return False, explain_launch_error(str(got.get("error") or p.stderr or "no reason given"))


def _wait_up(url: str, seconds: float, proc) -> int | None:
    """The HTTP status at url once something answers, None if nothing does in time. This machine's own ports are
    asked directly: a proxy (HTTP_PROXY, or macOS's system settings, which urllib reads) cannot reach them."""
    local = urllib.parse.urlsplit(url).hostname in LOOPBACK
    opener = urllib.request.build_opener(*([urllib.request.ProxyHandler({})] if local else []))
    end = time.time() + seconds
    while time.time() < end:
        if proc is not None and proc.poll() is not None:
            return None
        try:
            with opener.open(url, timeout=3) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except Exception:
            time.sleep(0.5)
    return None


def run(spec: dict, *, workdir: Path, out_dir: Path, env: dict, deadline: float, log_path: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"status": None, "title": "", "text": "", "console_errors": [], "page_errors": [], "failed_requests": [],
              "action_errors": [], "screenshots": []}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        result["setup_error"] = 'Playwright is not installed: pip install "agent-testbench[web]" && playwright install chromium'
        return result
    proc = None
    log = open(log_path, "a", buffering=1)
    try:
        if spec.get("serve"):
            if _wait_up(spec["url"], 1, None) is not None:
                result["setup_error"] = (f"Something already answers at {spec['url']} before the app started, so the "
                                         "step would test the wrong program. Stop it or use another port.")
                return result
            argv = shlex.split(spec["serve"]) if isinstance(spec["serve"], str) else list(spec["serve"])
            if os.name == "nt" and argv:                 # npm is npm.cmd there, which only a PATHEXT lookup finds
                argv[0] = shutil.which(argv[0], path=env.get("PATH")) or argv[0]
            proc = subprocess.Popen(argv, cwd=workdir, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    **procs.group_kwargs())
        status = _wait_up(spec["url"], min(float(spec.get("ready_timeout", 30)), max(1, deadline - time.time())), proc)
        died = proc is not None and proc.poll() is not None     # whatever answered then is not the app
        if died or (proc is not None and status is None):
            why = f"the serve command exited with code {proc.returncode}" if died else \
                f"nothing answered at {spec['url']} within {spec.get('ready_timeout', 30)} s"
            result["setup_error"] = f"The app did not come up: {why}. See the step log."
            return result
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:
                result["setup_error"] = f"Chromium did not start: {explain_launch_error(str(exc))}"
                return result
            w, h = spec.get("viewport", [1280, 800])
            page = browser.new_page(viewport={"width": int(w), "height": int(h)})
            page.on("console", lambda m: result["console_errors"].append(f"{m.type}: {m.text}") if m.type == "error" else None)
            page.on("pageerror", lambda e: result["page_errors"].append(str(e)))
            page.on("requestfailed", lambda r: result["failed_requests"].append(f"{r.method} {r.url}: {r.failure}"))
            page.set_default_timeout(max(1000, min(30000, (deadline - time.time()) * 1000)))
            response = page.goto(spec["url"], wait_until="load")
            result["status"] = response.status if response else None
            for i, action in enumerate(spec.get("actions", []), 1):
                try:
                    _act(page, action, out_dir, result)
                except Exception as exc:
                    result["action_errors"].append(f"action {i} {action}: {str(exc).splitlines()[0]}")
                    break
            page.wait_for_timeout(200)
            result["title"] = page.title()
            result["text"] = page.inner_text("body")[:20000]
            if spec.get("screenshot", True):
                shot = out_dir / "page.png"
                page.screenshot(path=str(shot), full_page=True)
                result["screenshots"].append(shot.name)
            browser.close()
    finally:
        if proc and proc.poll() is None:
            procs.stop_and_wait(proc.pid, grace=10, proc=proc)
        log.close()
    return result


def _act(page, action, out_dir: Path, result: dict) -> None:
    if isinstance(action, str):
        action = {action: True}
    (verb, arg), = action.items()
    if verb == "click":
        page.click(arg)
    elif verb == "fill":
        page.fill(arg["selector"], str(arg["text"]))
    elif verb == "press":
        page.keyboard.press(arg)
    elif verb == "wait_for":
        page.wait_for_selector(arg)
    elif verb == "wait_ms":
        page.wait_for_timeout(int(arg))
    elif verb == "goto":
        page.goto(arg)
    elif verb == "screenshot":
        shot = out_dir / f"{arg}.png"
        page.screenshot(path=str(shot), full_page=True)
        result["screenshots"].append(shot.name)
    else:
        raise ValueError(f"unknown action {verb!r}; use click, fill, press, wait_for, wait_ms, goto or screenshot")
