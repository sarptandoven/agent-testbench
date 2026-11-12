#!/usr/bin/env python3
"""agent-testbench's guard for Claude Code: a PreToolUse hook on Bash, on file edits, and on the run_plan /
session_start MCP tools.

Anywhere, it keeps the limits out of the agent's hands:
  1. an edit of testbench.yaml that raises a budget, lowers a backend's price (ssh/docker usd_per_hour, ...),
     changes `pricing:` or `guard:`, or loosens the docker sandbox (network: none, run_args, command, a plan
     leaving it) asks the person first (lowering a budget, or editing plans or `notify:`, does not); so does any
     edit of ~/.testbench/limits.yaml (the limits for every project on this machine) or ~/.testbench/prices.json,
     an edit that sets TESTBENCH_USER_DIR in a settings file (an MCP config's env, an rc file), and one that turns
     hooks off or takes agent-testbench out of Claude Code's settings (.claude/settings.json);
  2. editing or deleting a ledger (.testbench/ledger.jsonl, ~/.testbench/ledger.jsonl) is refused, and so is a
     command that points testbench at another limits folder (TESTBENCH_USER_DIR, HOME, USERPROFILE);
  3. a run, watch, session or loop with --approve (or approved_by_person=true) becomes a question to the person, and
     so does `testbench prices --update --url` (prices from somewhere else); commands are read with their quoting
     taken off, so --approve in quotes, with backslashes, in a variable, through xargs, `bash -c` or Python asks too;
Inside a project with a testbench.yaml, it also acts on:
  4. raw cloud launches (`modal run/deploy/serve/shell`, `colab new/run/ssh/exec`) are refused, because
     `testbench run` is what enforces the budget and keeps the evidence;
  5. commands that would print secrets (.env files, printenv, echo $..._KEY or $..._WEBHOOK) are refused;
  6. commands listed under `guard.ask` in testbench.yaml (default: git push, gh repo create, ...) ask first.
Anything else gets no decision from this hook. If the hook itself fails, it stays out of the way - unless the call
touches testbench (its command, testbench.yaml, ~/.testbench): then the person decides. It is a safety
net against an agent changing its own limits, not a sandbox: code an agent runs can still do anything (a script
that runs `testbench run --approve` is not seen here) - which is why the machine's limits in limits.yaml are
enforced by testbench itself, whatever was approved.
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
REDIRECT_LIMITS = re.compile(r"(^|[\s;&|(])(export\s+)?(TESTBENCH_USER_DIR|HOME|USERPROFILE)=")
SETTINGS_FILE = re.compile(r"(^\.|\.(json|jsonc|toml|ya?ml|env|sh|bash|zsh|fish|ps1|cfg|ini|conf)$|rc$|profile$)")
RAW_LAUNCH = re.compile(r"(^|[;&|(\s])(python3?\s+-m\s+)?(modal\s+(run|deploy|serve|shell|launch)|colab\s+(new|run|ssh|exec|console))\b")
SECRET_READ = [
    re.compile(r"\b(cat|less|more|head|tail|bat|strings|xxd|grep|rg|sed|awk)\b[^|;&]*(^|[\s/'\"])\.env(\.[\w.-]+)?\b"),
    re.compile(r"(^|[;&|\s])printenv(\s*$|\s*[;&|]|\s+\w*(KEY|TOKEN|SECRET|PASS|WEBHOOK))", re.I),
    re.compile(r"(^|[;&|]\s*)env\s*($|[;&|])"),
    re.compile(r"\becho\b[^;&|]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|PASSWD|WEBHOOK)\w*", re.I),
    re.compile(r"\bmodal\s+secret\s+create\b.*=", re.I),
    re.compile(r"\b(cat|less|more|head|tail)\b[^|;&]*(\.modal\.toml|\.netrc|credentials(\.json)?|id_rsa|id_ed25519)\b"),
]


class Decision(Exception):
    """The hook's answer to one tool call: deny or ask, and why."""

    def __init__(self, decision: str, reason: str):
        super().__init__(reason)
        self.decision, self.reason = decision, reason


def decide(decision: str, reason: str) -> None:
    raise Decision(decision, reason)


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
        out = subprocess.run([exe, "estimate", "--approve", "--json", "--", str(plan)], cwd=root, capture_output=True,
                             text=True, timeout=30).stdout              # the plan as the call named it: a value
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


PRICE_KEY = re.compile(r"(usd_per_hour|usd_per_unit|_per_s|units_per_hour)$")


def _num(v) -> bool:
    """A number a limit can be: not true/false, NaN, infinity, nor an integer too big to be a float."""
    try:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(float(v)) != float("inf")
    except OverflowError:
        return False


def _brief(v) -> str:
    text = repr(v)
    return text if len(text) <= 40 else text[:37] + "..."


def _prices(cfg: dict) -> dict:
    """Backend prices outside `pricing:` (ssh.usd_per_hour, docker.usd_per_hour, a plugin's section)."""
    return {f"{sec}.{k}": v for sec, body in cfg.items() if sec not in ("budget", "pricing", "guard", "plans")
            and isinstance(body, dict) for k, v in body.items() if isinstance(k, str) and PRICE_KEY.search(k)}


def _backend(plan):
    """A plan's backend as testbench reads it: a short form (`tests: "pytest -q"`, or a list of commands) runs on
    local, and so does a plan that names none. None for something that is not a plan."""
    if isinstance(plan, (str, list)):
        return "local"
    return str(plan.get("backend", "local")) if isinstance(plan, dict) else None


def _sandbox(old: dict, new: dict) -> list:
    """How an edit loosens the docker sandbox: what the code under test may reach."""
    od, nd = old.get("docker") or {}, new.get("docker") or {}
    out = []
    if not isinstance(od, dict) or not isinstance(nd, dict):
        return ["`docker:`"] if od != nd else []
    if od.get("network") == "none" and nd.get("network") != "none":
        out.append(f"docker.network none -> {nd.get('network', 'bridge (the default)')}")
    for key in ("run_args", "command"):
        if od.get(key) != nd.get(key):
            out.append(f"docker.{key} ({od.get(key)!r} -> {nd.get(key)!r})")
    oplans, nplans = old.get("plans") or {}, new.get("plans") or {}
    if od.get("network") == "none" and isinstance(oplans, dict) and isinstance(nplans, dict):
        for name, plan in oplans.items():
            now = _backend(nplans.get(name))
            if _backend(plan) == "docker" and now not in (None, "docker"):
                out.append(f"plan {name} leaves the docker sandbox (backend {now})")
    return out


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
    if not isinstance(ob, dict) or not isinstance(nb, dict):     # `budget: 5`: no limit anyone can read
        ob, nb = (ob if isinstance(ob, dict) else {"budget": ob}), (nb if isinstance(nb, dict) else {"budget": nb})
        if ob != nb:
            out.append(f"`budget:` ({_brief(old.get('budget'))} -> {_brief(new.get('budget'))})")
    for key, default in BUDGET_DEFAULTS.items():
        o, n = ob.get(key, default), nb.get(key, default)
        if not _num(o) or not _num(n):                    # NaN, inf, text: not a limit anyone can read
            if o != n or not _num(n):
                out.append(f"budget.{key} ({_brief(o)} -> {_brief(n)})")
        elif n > o:
            out.append(f"budget.{key} raised from ${float(o):g} to ${float(n):g}")
    op, np_ = _prices(old), _prices(new)
    for key in sorted(set(op) | set(np_)):
        o, n = op.get(key), np_.get(key)
        if o != n and not (_num(o) and _num(n) and n > o):
            out.append(f"{key} ({_brief(o)} -> {_brief(n)}; prices bound the worst case)")
    for key, what in (("pricing", "the prices that estimates use"), ("guard", "what this hook asks about")):
        if old.get(key) != new.get(key):
            out.append(f"`{key}:` changed ({what})")
    return out + _sandbox(old, new)


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
    if path == USER_DIR / "prices.json":
        decide("ask", f"agent-testbench: {path} is the price catalog every estimate on this machine uses. Approve "
                      "only if you want the agent to change prices.")
    if SETTINGS_FILE.search(path.name) and "TESTBENCH_USER_DIR" in json.dumps(inp):    # an MCP config's env, an rc file
        current = path.read_text(errors="replace") if path.is_file() else ""
        after = _after_edit(tool, inp, current)
        if after is not None and after.count("TESTBENCH_USER_DIR") > current.count("TESTBENCH_USER_DIR"):
            decide("ask", f"agent-testbench: this edit of {path.name} sets TESTBENCH_USER_DIR, which points testbench "
                          "(an MCP server's environment, a shell's) at another limits folder than ~/.testbench. Approve "
                          "only if you want the agent to run under other spending limits.")
    if path.parent.name == ".claude" and path.name in ("settings.json", "settings.local.json"):
        current = path.read_text(errors="replace") if path.is_file() else ""
        after = _after_edit(tool, inp, current)
        off = re.compile(r'"disableAllHooks"\s*:\s*true')
        if after is not None and (len(off.findall(after)) > len(off.findall(current))
                                  or after.count("agent-testbench") < current.count("agent-testbench")):
            decide("ask", f"agent-testbench: this edit of {path} turns hooks off or takes agent-testbench out of Claude "
                          "Code's settings, and with them this guard, which asks you before an agent raises its own "
                          "limits or approves its own spending. Approve only if you want that.")
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


def plain(cmd: str) -> str:
    """A command with the shell's quoting taken off, roughly as the shell hands its words to programs: quotes and
    backslashes go, so `--app""rove`, '--approve', $'--approve' and --\\approve are all --approve here as they are to
    testbench. Brackets of subshells and groups become spaces."""
    text = re.sub(r"\\\n", "", cmd)                       # a line continued
    text = re.sub(r"\$(['\"])", r"\1", text)              # $'...' and $"..."
    text = re.sub(r"\\(.)", r"\1", text)
    return re.sub(r"[(){}]", " ", re.sub(r"['\"]", "", text))


def check_limits_reach(cmd: str) -> None:
    texts = list(dict.fromkeys((cmd, plain(cmd))))
    for text in texts:
        if REDIRECT_LIMITS.search(text) and re.search(r"\b(testbench|agent-testbench|agent_testbench)\b", text):
            decide("deny", "agent-testbench guard: this command points testbench at another limits folder "
                           "(TESTBENCH_USER_DIR, HOME or USERPROFILE). The limits in ~/.testbench stay as the person "
                           "set them.")
        if writes(text, r"ledger\.jsonl"):
            decide("deny", "agent-testbench guard: this command could change or delete a ledger of what runs cost; it "
                           "is evidence, written only by testbench. Leave it as it is.")
    for text in texts:
        if writes(text, rf"(?:\.testbench|{re.escape(USER_DIR.name)})/prices\.json"):
            decide("ask", "agent-testbench: this command could rewrite ~/.testbench/prices.json, the prices every "
                          "estimate on this machine uses. Approve only if you want the agent to change prices.")
        if writes(text, r"(testbench|limits)\.yaml"):
            decide("ask", "agent-testbench: this command could rewrite testbench.yaml or ~/.testbench/limits.yaml, "
                          "where the spending limits are. Edit the file with the edit tool instead (then only changes "
                          "to the limits ask you), or approve if you want this.")


PROGRAM = re.compile(r"(?:^|(?<=[\s;&|(){}`/='\"\\]))(?:testbench|agent-testbench|agent_testbench(?:\.cli)?)(?=\s|$)")
SEGMENTS = re.compile(r"[;&|\n(){}`]+")
MENTION = re.compile(r"(?<![\w-])(?:agent[-_])?testbench(?![\w-]|\.ya?ml)")
LAUNCH_WORD = re.compile(r"(?<![\w-])(?:run|watch)(?![\w-])|(?<![\w-])(?:loop|session)\s+start(?![\w-])|"
                         r"\blaunch\s*\(|\.run\s*\(|\bloop_start\b|\bsession_start\b|\brun_plan\b")
APPROVAL = re.compile(r"(?<![\w-])--approve(?![\w-])|\bapprove(?:d_by_person)?\s*[:=]\s*(?:True|true|1|yes)\b")
ARGS_FROM_ELSEWHERE = re.compile(r"\b(?:xargs|parallel)\b|\s-exec(?:dir)?\s")


def invocations(seg: str) -> list:
    """[(subcommand, the words after it, what came before the program)] for each testbench command in one segment of
    a command line (`uvx --from agent-testbench testbench run` has two: the second is the one that runs). Options
    before the subcommand (`testbench --all run`) are skipped, as argparse does."""
    out = []
    for m in PROGRAM.finditer(seg):
        words = seg[m.end():].split()
        while words and words[0].startswith("-"):
            words = words[1:]
        out.append(((words[0] if words else ""), words[1:], seg[:m.start()]))
    return out


def check_approvals(cmd: str, root) -> None:
    """A testbench command that passes --approve (anywhere in it), or might through a variable, xargs, quotes or a
    subshell, asks the person."""
    text = plain(cmd)
    for sub, rest, before in [i for t in dict.fromkeys((cmd, text)) for s in SEGMENTS.split(t) for i in invocations(s)]:
        names = [w for w in rest if not w.startswith("-")]
        if sub == "prices" and any(w == "--url" or w.startswith("--url=") for w in rest):
            decide("ask", "agent-testbench: this fetches prices from another catalog than the project's own; a "
                          "catalog can only raise prices, but approve only if you know where this one comes from.")
        launches = sub in ("run", "watch") or (sub in ("loop", "session") and names[:1] == ["start"])
        approving = any(w == "--approve" or w.startswith("--approve=") for w in rest)
        maybe = not approving and bool(re.search(r"[$`]", " ".join(rest)) or ARGS_FROM_ELSEWHERE.search(before))
        if not launches or not (approving or maybe):         # $FLAG or xargs could be --approve; the person decides
            continue
        if sub == "run":
            plan = names[0] if names else ""
            decide("ask", "agent-testbench: a run with --approve spends money above the approval threshold"
                   + (" (this one's arguments come from a variable or another command, so it could)." if maybe else ".")
                   + (estimate(root, plan) if root else "") + " Approve only if you agreed to this cost.")
        if sub == "watch":
            plan = names[0] if names else ""
            decide("ask", "agent-testbench: `testbench watch --approve` runs a plan that costs money again every time "
                          "the project's files change, each run up to its worst case (and inside max_run_usd and the "
                          "day limits)." + (estimate(root, plan).replace(" can cost up to", ": each run can cost up to")
                                            if root else "") + " Approve only if you agreed to that spending.")
        if sub == "loop":
            decide("ask", "agent-testbench: this loop's total budget is above the approval threshold (every run of "
                          "the loop together, up to --budget-usd). Approve only if you agreed to that total.")
        decide("ask", "agent-testbench: this live session is above the approval threshold (its worst case is its "
                      "--max-minutes on the chosen machine). Approve only if you agreed to this cost.")
    others = "\n".join(s for s in SEGMENTS.split(text) if "estimate" not in [i[0] for i in invocations(s)])
    if MENTION.search(text) and (LAUNCH_WORD.search(cmd) or LAUNCH_WORD.search(text)) and APPROVAL.search(others):
        decide("ask", "agent-testbench: this command could pass --approve to a testbench launch in a way this hook "
                      "cannot follow word for word (quotes, a variable, a subshell, xargs, `bash -c`, Python). A launch "
                      "with --approve spends money above the approval threshold. Approve only if you agreed to this "
                      "cost.")


def check(data: dict) -> None:
    """Every rule, in order; the first that applies decides (raises Decision)."""
    tool = data.get("tool_name", "")
    cwd = data.get("cwd") or os.getcwd()
    inp = data.get("tool_input") or {}
    if tool in EDIT_TOOLS:
        check_edit(tool, inp, cwd)
        return
    root = find_root(cwd)
    if tool.endswith("__run_plan") or tool.endswith("__session_start") or tool.endswith("__loop_start"):
        if inp.get("project_dir"):
            root = find_root(str(Path(cwd) / Path(str(inp["project_dir"])).expanduser()))
        if inp.get("approved_by_person") and tool.endswith("__loop_start"):
            decide("ask", f"agent-testbench: this loop on plan {inp.get('plan', '?')} may spend up to "
                          f"${_dollars(inp.get('budget_usd'))} in total, above the project's approval threshold."
                   + estimate(root, inp.get("plan", "")).replace(" can cost up to", ": each run can cost up to")
                   + " Approve only if you agreed to that total.")
        if inp.get("approved_by_person"):
            decide("ask", "agent-testbench: this run is over the project's approval threshold."
                   + estimate(root, inp.get("plan", "")) + " Approve only if you agreed to this cost.")
        return
    if tool != "Bash":
        return
    cmd = str(inp.get("command", ""))
    check_limits_reach(cmd)
    if root is not None:
        for pattern in SECRET_READ:
            if pattern.search(cmd):
                decide("deny", "agent-testbench guard: this command could print a secret. Secrets stay with the person: "
                               "refer to them by NAME (testbench.yaml `secrets:`, Modal secrets, Colab secrets), never by "
                               "value.")
        if RAW_LAUNCH.search(cmd) and not re.search(r"(^|\s)(testbench|agent-testbench)\s", cmd):
            decide("deny", "agent-testbench guard: in this project, cloud runs go through `testbench run <plan>`, which "
                           "enforces the budget in testbench.yaml and keeps the evidence. Add or edit a plan instead; if a "
                           "raw command is really needed, ask the person to run it.")
    check_approvals(cmd, root)
    if root is not None:
        for phrase in ask_list(root):
            if re.search(r"(^|[;&|]\s*|\s)" + re.escape(phrase) + r"\b", cmd):
                decide("ask", f"agent-testbench: `{phrase}` is on this project's ask-first list (testbench.yaml guard.ask).")


def _dollars(v) -> str:
    """A budget an MCP call passed, as the question shows it: what it said, if it is not a number."""
    try:
        return f"{float(v or 0):.2f}"
    except (TypeError, ValueError, OverflowError):
        return repr(v)


def decision(data: dict):
    """(decision, reason) for one tool call, or None: this hook has nothing to say about it. If the hook itself
    fails on a call that touches testbench (its command, testbench.yaml, ~/.testbench), the person decides: an edit
    or a command this hook cannot read must not get past it for that reason alone. Any other failure stays out of
    the way."""
    try:
        check(data)
    except Decision as d:
        return d.decision, d.reason
    except Exception as exc:
        if re.search(r"testbench|TESTBENCH_", json.dumps(data.get("tool_input"), default=str)):
            return "ask", (f"agent-testbench: the guard could not check this call ({type(exc).__name__}: {exc}), and it "
                           "touches testbench (its limits, ledgers or launches). Approve only if you know what it does.")
    return None


def main() -> None:
    answer = decision(json.load(sys.stdin))
    if answer:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": answer[0],
                                                 "permissionDecisionReason": answer[1]}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)            # a broken guard must not block work; it simply gives no decision
