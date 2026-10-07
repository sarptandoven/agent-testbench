#!/usr/bin/env python3
"""agent-testbench's guard for Claude Code: a PreToolUse hook on Bash and on the run_plan / session_start MCP tools.

It acts only inside a project with a testbench.yaml, and only on four things:
  1. raw cloud launches (`modal run/deploy/serve/shell`, `colab new/run/ssh/exec`) are refused, because
     `testbench run` is what enforces the budget and keeps the evidence;
  2. a run or session with --approve (or approved_by_person=true) becomes a question to the person;
  3. commands that would print secrets (.env files, printenv, echo $..._KEY) are refused;
  4. commands listed under `guard.ask` in testbench.yaml (default: git push, gh repo create, ...) ask first.
Anything else gets no decision from this hook. If the hook itself fails, it stays out of the way.
Standard library only, so it runs with any python3.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_ASK = ["git push", "gh repo create", "gh pr merge", "gh release create"]
RAW_LAUNCH = re.compile(r"(^|[;&|(\s])(python3?\s+-m\s+)?(modal\s+(run|deploy|serve|shell|launch)|colab\s+(new|run|ssh|exec|console))\b")
SECRET_READ = [
    re.compile(r"\b(cat|less|more|head|tail|bat|strings|xxd|grep|rg|sed|awk)\b[^|;&]*(^|[\s/'\"])\.env(\.[\w.-]+)?\b"),
    re.compile(r"(^|[;&|\s])printenv(\s*$|\s*[;&|]|\s+\w*(KEY|TOKEN|SECRET|PASS))", re.I),
    re.compile(r"(^|[;&|]\s*)env\s*($|[;&|])"),
    re.compile(r"\becho\b[^;&|]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|PASSWD)\w*", re.I),
    re.compile(r"\bmodal\s+secret\s+create\b.*=", re.I),
    re.compile(r"\b(cat|less|more|head|tail)\b[^|;&]*(\.modal\.toml|\.netrc|credentials(\.json)?|id_rsa|id_ed25519)\b"),
]


def decide(decision: str, reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                             "permissionDecisionReason": reason}}))
    sys.exit(0)


def find_root(cwd: str):
    here = Path(cwd).resolve()
    for d in (here, *here.parents):
        if (d / "testbench.yaml").is_file():
            return d
    return None


def ask_list(root: Path) -> list:
    try:
        import yaml
        cfg = yaml.safe_load((root / "testbench.yaml").read_text()) or {}
        return list((cfg.get("guard") or {}).get("ask", DEFAULT_ASK))
    except Exception:
        return DEFAULT_ASK


def estimate(root: Path, plan: str) -> str:
    exe = shutil.which("testbench")
    if not exe or not plan:
        return ""
    try:
        out = subprocess.run([exe, "estimate", plan, "--approve", "--json"], cwd=root, capture_output=True, text=True,
                             timeout=30).stdout
        est = json.loads(out)
        hw = est["backend"] + (f" {est['gpu']}" if est.get("gpu") else "")
        return f" Plan {plan} on {hw} can cost up to ${est['worst_case_usd']:.2f} ({est['max_minutes']:.0f} min)."
    except Exception:
        return ""


def main() -> None:
    data = json.load(sys.stdin)
    tool = data.get("tool_name", "")
    root = find_root(data.get("cwd") or os.getcwd())
    if root is None:
        return
    inp = data.get("tool_input") or {}
    if tool.endswith("__run_plan") or tool.endswith("__session_start"):
        if inp.get("approved_by_person"):
            decide("ask", "agent-testbench: this run is over the project's approval threshold." + estimate(root, inp.get("plan", ""))
                   + " Approve only if you agreed to this cost.")
        return
    if tool != "Bash":
        return
    cmd = inp.get("command", "")
    for pattern in SECRET_READ:
        if pattern.search(cmd):
            decide("deny", "agent-testbench guard: this command could print a secret. Secrets stay with the person: refer "
                           "to them by NAME (testbench.yaml `secrets:`, Modal secrets, Colab secrets), never by value.")
    if RAW_LAUNCH.search(cmd) and not re.search(r"(^|\s)(testbench|agent-testbench)\s", cmd):
        decide("deny", "agent-testbench guard: in this project, cloud runs go through `testbench run <plan>`, which enforces "
                       "the budget in testbench.yaml and keeps the evidence. Add or edit a plan instead; if a raw "
                       "command is really needed, ask the person to run it.")
    m = re.search(r"\b(?:testbench|agent-testbench|agent_testbench)\s+run\s+([\w-]+)[^;&|]*--approve\b", cmd)
    if m:
        decide("ask", "agent-testbench: a run with --approve spends money above the approval threshold."
               + estimate(root, m.group(1)) + " Approve only if you agreed to this cost.")
    if re.search(r"\b(?:testbench|agent-testbench|agent_testbench)\s+session\s+start\b[^;&|]*--approve\b", cmd):
        decide("ask", "agent-testbench: this live session is above the approval threshold (its worst case is its "
                      "--max-minutes on the chosen machine). Approve only if you agreed to this cost.")
    for phrase in ask_list(root):
        if re.search(r"(^|[;&|]\s*|\s)" + re.escape(phrase) + r"\b", cmd):
            decide("ask", f"agent-testbench: `{phrase}` is on this project's ask-first list (testbench.yaml guard.ask).")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)            # a broken guard must not block work; it simply gives no decision
