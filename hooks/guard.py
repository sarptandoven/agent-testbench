#!/usr/bin/env python3
"""agent-testbench's guard for Claude Code: a PreToolUse hook on Bash, on file edits, and on the run_plan /
session_start MCP tools.

Anywhere, it keeps the limits out of the agent's hands:
  1. an edit that raises a budget in testbench.yaml, or changes its `pricing:` or `guard:`, asks the person
     first (lowering a budget, or editing plans or `notify:`, does not); so does any edit of
     ~/.testbench/limits.yaml, the limits for every project on this machine;
  2. editing or deleting a ledger (.testbench/ledger.jsonl, ~/.testbench/ledger.jsonl) is refused, and so is a
     command that points testbench at another limits folder (TESTBENCH_USER_DIR, HOME);
Inside a project with a testbench.yaml, it also acts on:
  3. raw cloud launches (`modal run/deploy/serve/shell`, `colab new/run/ssh/exec`) are refused, because
     `testbench run` is what enforces the budget and keeps the evidence;
  4. a run, session or loop with --approve (or approved_by_person=true) becomes a question to the person;
  5. commands that would print secrets (.env files, printenv, echo $..._KEY or $..._WEBHOOK) are refused;
  6. commands listed under `guard.ask` in testbench.yaml (default: git push, gh repo create, ...) ask first.
Anything else gets no decision from this hook. If the hook itself fails, it stays out of the way. It is a safety
net against an agent changing its own limits, not a sandbox: code an agent runs can still do anything.
Standard library only (PyYAML when it is there), so it runs with any python3.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_ASK = ["git push", "gh repo create", "gh pr merge", "gh release create"]
BUDGET_DEFAULTS = {"max_run_usd": 2.0, "ask_above_usd": 0.5, "max_day_usd": 10.0}
EDIT_TOOLS = ("Edit", "Write", "MultiEdit")
USER_DIR = Path(os.environ.get("TESTBENCH_USER_DIR") or Path.home() / ".testbench").resolve()
REDIRECT_LIMITS = re.compile(r"(^|[\s;&|(])(export\s+)?(TESTBENCH_USER_DIR|HOME)=")
RAW_LAUNCH = re.compile(r"(^|[;&|(\s])(python3?\s+-m\s+)?(modal\s+(run|deploy|serve|shell|launch)|colab\s+(new|run|ssh|exec|console))\b")
SECRET_READ = [
    re.compile(r"\b(cat|less|more|head|tail|bat|strings|xxd|grep|rg|sed|awk)\b[^|;&]*(^|[\s/'\"])\.env(\.[\w.-]+)?\b"),
    re.compile(r"(^|[;&|\s])printenv(\s*$|\s*[;&|]|\s+\w*(KEY|TOKEN|SECRET|PASS|WEBHOOK))", re.I),
    re.compile(r"(^|[;&|]\s*)env\s*($|[;&|])"),
    re.compile(r"\becho\b[^;&|]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|PASSWD|WEBHOOK)\w*", re.I),
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


def _after_edit(tool: str, inp: dict, current: str):
    """The file's text after the edit, or None if the edit does not apply (the tool will fail on its own)."""
    if tool == "Write":
        return inp.get("content", "")
    text = current
    for e in (inp.get("edits") or []) if tool == "MultiEdit" else [inp]:
        old, new = e.get("old_string", ""), e.get("new_string", "")
        if old not in text:
            return None
        text = text.replace(old, new) if e.get("replace_all") else text.replace(old, new, 1)
    return text


def _limits_changes(before: str, after: str) -> list:
    """What an edit of testbench.yaml would do to the limits: budgets raised, prices or the guard changed."""
    try:
        import yaml
    except ImportError:                                   # cannot read YAML here: a changed line near the limits asks
        changed = set(before.splitlines()) ^ set(after.splitlines())
        near = [ln for ln in changed if re.search(r"budget|_usd|pricing|guard|_per_s|units_per_hour|ask:", ln)]
        return ["the budget, pricing or guard settings (PyYAML is not here to tell how)"] if near else []
    try:
        old = (yaml.safe_load(before) or {}) if before.strip() else {}
        new = yaml.safe_load(after) or {}
    except Exception:
        return ["the file (it would not be valid YAML, so its limits cannot be checked)"]
    if not isinstance(old, dict) or not isinstance(new, dict):
        return ["the file's limits"]
    out = []
    ob, nb = old.get("budget") or {}, new.get("budget") or {}
    for key, default in BUDGET_DEFAULTS.items():
        o, n = ob.get(key, default), nb.get(key, default)
        if not isinstance(o, (int, float)) or not isinstance(n, (int, float)):
            if o != n:
                out.append(f"budget.{key} ({o!r} -> {n!r})")
        elif n > o:
            out.append(f"budget.{key} raised from ${o:g} to ${n:g}")
    for key, what in (("pricing", "the prices that estimates use"), ("guard", "what this hook asks about")):
        if old.get(key) != new.get(key):
            out.append(f"`{key}:` changed ({what})")
    return out


def _is_ledger(path: Path) -> bool:
    return path.name == "ledger.jsonl" and (path.parent.name == ".testbench" or path.parent == USER_DIR)


def check_edit(tool: str, inp: dict, cwd: str) -> None:
    raw = inp.get("file_path") or ""
    if not raw:
        return
    path = (Path(cwd) / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    if _is_ledger(path):
        decide("deny", f"agent-testbench guard: {path} is a ledger of what runs cost; it is evidence, written only by "
                       "testbench. Leave it as it is.")
    if path == USER_DIR / "limits.yaml":
        decide("ask", f"agent-testbench: {path} holds the spending limits for every project on this machine. Approve "
                      "only if you want the agent to change them.")
    if path.name != "testbench.yaml":
        return
    current = path.read_text() if path.exists() else ""
    after = _after_edit(tool, inp, current)
    if after is None:
        return
    changes = _limits_changes(current, after)
    if changes:
        decide("ask", f"agent-testbench: this edit of {path.name} changes its limits: {'; '.join(changes)}. "
                      "Approve only if you want to allow that spending.")


def writes(cmd: str, name: str) -> bool:
    """Whether a shell command could change or remove the file `name` (a regex): reading it is fine."""
    f = rf"\S*{name}"
    return bool(re.search(rf"(\brm|\bmv|\btruncate|\bshred|\bsed\s+(-\w+\s+)*-i\w*|\bperl\s+-\w*i\w*|\byq\b[^;&|]*\s-i)"
                          rf"[^;&|]*{f}|>>?\s*{f}|\btee\b[^;&|]*{f}|\b(cp|mv|ln)\b[^;&|]*\s{f}\s*($|[;&|)])|"
                          rf"{f}[^;&|]*(write_text|unlink|truncate)|open\([^)]*{f}[^)]*['\"][wa+]", cmd))


def check_limits_reach(cmd: str) -> None:
    if REDIRECT_LIMITS.search(cmd) and re.search(r"\b(testbench|agent-testbench|agent_testbench)\b", cmd):
        decide("deny", "agent-testbench guard: this command points testbench at another limits folder "
                       "(TESTBENCH_USER_DIR or HOME). The limits in ~/.testbench stay as the person set them.")
    if writes(cmd, r"ledger\.jsonl"):
        decide("deny", "agent-testbench guard: this command could change or delete a ledger of what runs cost; it "
                       "is evidence, written only by testbench. Leave it as it is.")
    if writes(cmd, r"(testbench|limits)\.yaml"):
        decide("ask", "agent-testbench: this command could rewrite testbench.yaml or ~/.testbench/limits.yaml, "
                      "where the spending limits are. Edit the file with the edit tool instead (then only changes "
                      "to the limits ask you), or approve if you want this.")


def main() -> None:
    data = json.load(sys.stdin)
    tool = data.get("tool_name", "")
    cwd = data.get("cwd") or os.getcwd()
    inp = data.get("tool_input") or {}
    if tool in EDIT_TOOLS:
        check_edit(tool, inp, cwd)
        return
    if tool == "Bash":
        check_limits_reach(inp.get("command", ""))
    root = find_root(cwd)
    if root is None:
        return
    if tool.endswith("__run_plan") or tool.endswith("__session_start") or tool.endswith("__loop_start"):
        if inp.get("approved_by_person") and tool.endswith("__loop_start"):
            decide("ask", f"agent-testbench: this loop on plan {inp.get('plan', '?')} may spend up to "
                          f"${float(inp.get('budget_usd') or 0):.2f} in total, above the project's approval threshold."
                   + estimate(root, inp.get("plan", "")).replace(" can cost up to", ": each run can cost up to")
                   + " Approve only if you agreed to that total.")
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
    if re.search(r"\b(?:testbench|agent-testbench|agent_testbench)\s+loop\s+start\b[^;&|]*--approve\b", cmd):
        decide("ask", "agent-testbench: this loop's total budget is above the approval threshold (every run of the loop "
                      "together, up to --budget-usd). Approve only if you agreed to that total.")
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
