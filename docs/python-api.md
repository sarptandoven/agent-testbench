# Python API and tools for your own agents

Two ways to use agent-testbench without MCP or a shell, for agents you build yourself (the Anthropic or OpenAI
SDK, LangGraph, a plain loop) and for notebooks:

- **`agent_testbench.Testbench`**: plans, runs, live sessions and spending as Python objects.
- **`agent_testbench.tools`**: the MCP server's tools as tool definitions for the Anthropic and OpenAI APIs, and
  `call()` to run the one the model asked for.

Both go through the same code as the CLI and the MCP server, so the guardrails are exactly the same (below).

```bash
pip install agent-testbench            # the Python API
pip install "agent-testbench[mcp]"     # agent_testbench.tools as well (it reads the MCP server's tools)
```

## The Python API

```python
from agent_testbench import Testbench, NeedsApproval, Refused

tb = Testbench("path/to/project")        # the folder with testbench.yaml, or any folder below it
tb.plans()                               # {name: {backend, gpu, steps, max_minutes, worst_case_usd, ...}}
tb.estimate("gpu-check")                 # worst case and whether it may launch now

run = tb.run("smoke")                    # launch inside the budget and follow it to the end
print(run.status, run.cost_usd)          # passed 0.0
print(run.report_md)                     # the report as an agent reads it, failures first
run.report["steps"][0]["expectations"]   # the same report as a dict (schema/report-v1.schema.json)
run.images                               # evidence plots and screenshots, as paths
tb.verify(run.id)["ok"]                  # the evidence still matches the hashes in the report
```

`tb.check()` is `testbench check`: every plan that is free and needs no approval runs (the others are listed with
their worst case, never launched), and it returns the summary `testbench check --json` prints - each plan's
`status`, `run` and why it did not run, the `runs`, `next` and `exit_code` (`plans=[...]` picks some,
`on_line=print` shows each plan's line). In a folder with no testbench.yaml, `from agent_testbench.api import
check; check("path")` runs the plans `testbench init` would write there, on this machine; `Testbench("path")` works
in that folder afterwards.

`tb.run(plan, wait=0)` returns at once; then `run.wait(timeout)` follows it (`on_line=print` shows progress) and
`run.cancel()` stops it, here and on the remote machine. `tb.runs(n)` lists the latest runs and `tb.get_run(id)`
finds one (an id, a unique prefix, or the latest); a `Run` reads its state from disk each time, so it also follows
runs started from the CLI or by an MCP client. `tb.spend()` is what runs and sessions have cost against the
project's limits and this machine's.

### Live sessions

```python
with tb.session(backend="local", idle_minutes=5) as s:     # stopped when the block ends
    s.exec("import numpy as np; x = np.arange(10)")
    r = s.exec("x.mean()")
    print(r.text)                        # [code | ok | 0.0 s]  => 4.5   (what an agent reads)
    r.result, r.ok, r.images, r.error     # the last value, ok/error/timeout, saved plot files, the error and its line
    s.cells("train.ipynb", "1-3", params={"EPOCHS": 1})    # notebook cells with form fields set
    s.exec(file="check.py"); s.install("pandas==2.2.3"); s.shell("df -h")
    s.ls("outputs"); s.get("outputs/model.pt"); s.put("data.csv", "data.csv"); s.sync()
```

`tb.session(...)` takes the CLI's options (`name`, `backend`, `gpu`, `idle_minutes`, `max_minutes`, `secrets`,
`approve`). The session stops itself after `idle_minutes` unused and at `max_minutes`, which bounds its cost; call
`s.stop()` (it returns the cost) when not using `with`. `tb.sessions()` picks up sessions started elsewhere.

### Errors

| exception | when | what it carries |
|---|---|---|
| `NeedsApproval` | the worst case is above `ask_above_usd` | `what`, `hardware`, `worst_case_usd`, `estimate`; the message |
| `Refused` | over `max_run_usd`, a day's budget or this machine's limits | `reason` (the message), and the same fields |
| `TestbenchError` | anything else: no such plan, a session already running, a broken testbench.yaml | the message |

`NeedsApproval` is a `Refused` and both are `TestbenchError`s; every message says what to do next.

```python
try:
    run = tb.run("gpu-check")
except NeedsApproval as e:
    if input(f"{e.what} on {e.hardware} could cost up to ${e.worst_case_usd:.2f}. Approve? [y/N] ") == "y":
        run = tb.run("gpu-check", approve=True)
```

## Tools for the Anthropic API

`tools.anthropic_tools()` is the MCP server's tools as `{name, description, input_schema}`; `tools.call(name,
input)` runs one and returns what the model should read: the text the MCP tool returns, or with plots and
screenshots a list of text and base64 image blocks, ready to be a `tool_result`'s content.

```python
import anthropic
from agent_testbench import tools

client = anthropic.Anthropic()
defs = tools.anthropic_tools(project_dir="path/to/project")     # the model is not offered project_dir
messages = [{"role": "user", "content": "Make the smoke plan pass and prove it."}]
while True:
    response = client.beta.messages.create(model="claude-opus-5-5", max_tokens=16000, tools=defs, messages=messages,
                                           betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    messages.append({"role": "assistant", "content": response.content})
    if response.stop_reason != "tool_use":
        break
    messages.append({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": b.id,
         "content": tools.call(b.name, b.input, project_dir="path/to/project", ask=lambda q: input(q) == "y")}
        for b in response.content if b.type == "tool_use"]})
```

A complete loop - a system prompt, a turn limit, refusals handled, a person asked at the terminal - in about 60
lines: [examples/agent-loop/agent_loop.py](../examples/agent-loop/agent_loop.py).

## Tools for the OpenAI API

`tools.openai_tools()` gives function tools for Chat Completions (`{"type": "function", "function": {...}}`);
`api="responses"` gives them for the Responses API (`{"type": "function", "name": ..., "strict": false}` - not
strict, because the schemas have optional arguments with defaults). `call()` takes the arguments as the JSON
string OpenAI sends.

```python
from openai import OpenAI
from agent_testbench import tools

client, project, MODEL = OpenAI(), "path/to/project", "..."      # the OpenAI model you use
defs = tools.openai_tools(project_dir=project)
messages = [{"role": "user", "content": "Make the smoke plan pass and prove it."}]
while True:
    reply = client.chat.completions.create(model=MODEL, messages=messages, tools=defs).choices[0].message
    messages.append(reply.model_dump(exclude_none=True))
    if not reply.tool_calls:
        break
    for c in reply.tool_calls:
        messages.append({"role": "tool", "tool_call_id": c.id,
                         "content": tools.call(c.function.name, c.function.arguments, project_dir=project,
                                               format="openai")})
```

Chat Completions tool messages take text only, so `format="openai"` returns text that names the image files. The
Responses API takes images in a function call's output:

```python
defs = tools.openai_tools(project_dir=project, api="responses")
items = [{"role": "user", "content": "Make the smoke plan pass and prove it."}]
response = client.responses.create(model=MODEL, input=items, tools=defs)
while calls := [o for o in response.output if o.type == "function_call"]:
    items += response.output
    items += [{"type": "function_call_output", "call_id": c.call_id,
               "output": tools.call(c.name, c.arguments, project_dir=project, format="openai-responses")}
              for c in calls]
    response = client.responses.create(model=MODEL, input=items, tools=defs)
print(response.output_text)
```

## What `call()` takes

| argument | |
|---|---|
| `project_dir` | run in this project whatever the model passed (use it with `*_tools(project_dir=...)`) |
| `format` | `"anthropic"` (default), `"openai-responses"`, `"openai"` or `"text"`: how images come back |
| `ask` | `ask(question) -> bool`: how a person is asked about a cost above the approval threshold, as MCP clients ask with elicitation. With it, only the person approves: the model's `approved_by_person` is ignored. Without it, the tool says what needs approval and the model may pass `approved_by_person=true` only after the person said yes, as over MCP |
| `on_progress` | `on_progress(line)` gets the progress lines of a long run |

`await tools.acall(...)` is the same for asyncio code; `call()` also works inside a running event loop (Jupyter).
`only=[...]` on `anthropic_tools()` / `openai_tools()` offers a few of the tools. Arguments that do not fit come
back as a message for the model to read, not as an exception.

The names, descriptions and schemas are read from the MCP server when asked for, and a call runs the MCP server's
own tool function: a tool added to the server is offered here too, and a tool behaves the same in both.

## The guardrails stay the same

- Every launch - `tb.run`, `tb.session`, and the `run_plan`, `session_start` and `loop_start` tools - goes through
  `budget.check`, under the same machine-wide lock as the CLI, with the same ledgers.
- `approve=True` is `--approve`: it only gets past `ask_above_usd`. `max_run_usd`, the project's day budget and
  this machine's `~/.testbench/limits.yaml` still refuse, and nothing in the API or the tools can raise a limit
  (testbench.yaml and limits.yaml are read from disk at every launch).
- Plans and sessions have the same hard stops on the machine doing the work.
