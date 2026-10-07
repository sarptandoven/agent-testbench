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

import os
import shlex
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def _wait_up(url: str, seconds: float, proc) -> int | None:
    end = time.time() + seconds
    while time.time() < end:
        if proc is not None and proc.poll() is not None:
            return None
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
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
            proc = subprocess.Popen(shlex.split(spec["serve"]) if isinstance(spec["serve"], str) else spec["serve"],
                                    cwd=workdir, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        status = _wait_up(spec["url"], min(float(spec.get("ready_timeout", 30)), max(1, deadline - time.time())), proc)
        if proc is not None and proc.poll() is not None:
            status = None                     # the app's own server died; whatever answered is not the app
            why = f"the serve command exited with code {proc.returncode}" if proc and proc.poll() is not None else \
                f"nothing answered at {spec['url']} within {spec.get('ready_timeout', 30)} s"
            result["setup_error"] = f"The app did not come up: {why}. See the step log."
            return result
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:
                result["setup_error"] = f"Chromium did not start ({str(exc).splitlines()[0]}); run `playwright install chromium`"
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
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=10)
            except Exception:
                os.killpg(proc.pid, signal.SIGKILL)
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
