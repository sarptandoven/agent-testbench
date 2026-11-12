---
name: setup
description: Set up agent-testbench in a project - install it, write testbench.yaml with the budget, session defaults (backend, GPU, idle and max minutes) and a ladder of plans (local, short remote check, full run) with real expectations, add strict mocks for paid APIs, and turn notebooks into editable cell files. Use when a project has no testbench.yaml yet, or when adding a plan, backend, mock or check.
---

# Setting up agent-testbench

1. **Install.** `pip install "agent-testbench[all]"` (or `uv tool install "agent-testbench[all]"`), then `testbench doctor`
   to see what this machine can run: Modal needs `modal setup`; Colab needs
   `uv tool install google-colab-cli` and one login; web steps need `playwright install chromium`.
   Never ask the person to paste a token into the chat - they run the login commands themselves.
2. **Start the config.** In the project folder: `testbench init`. It writes `testbench.yaml` with a plan for each
   notebook, test suite (pytest; `npm test` with jest, vitest, mocha, node --test or Playwright; Go; Rust - judged
   by the runner's JUnit report), training script and web app it finds - already checking what it can - and the
   working rules in `AGENTS.md` (read by Codex and others; `CLAUDE.md` points to it). Lines marked FILL IN need the
   number only the person knows ("accuracy >= 0.9", "tests >= 120"): ask, do not guess.
   **When the person's goal matches a recipe, start from it**: `testbench recipes` lists them (pytest, jest,
   vitest, go-test, cargo-test, playwright-ts, notebook-colab, pytorch-train, hf-finetune, web-app, llm-eval,
   docker-sandbox, ssh-gpu-box, data-pipeline) with each plan's worst case. `testbench init --recipe NAME` writes that ladder of plans plus what init detects;
   `testbench init --add --recipe NAME` adds it to an existing testbench.yaml without changing anything already
   there. Then adapt its FILL IN lines to the project with the person.
3. **Ask the person** for what only they can decide: the budget (`max_run_usd`, `ask_above_usd`,
   `max_day_usd`), which backends they have (Modal, Colab, neither), and what "working" means in numbers.
4. **Set session defaults** - where `testbench session start` puts its kernel when no flags are given:
   ```yaml
   sessions:
     backend: modal          # local | modal | colab
     gpu: A10                # optional; what the project's work actually needs
     idle_minutes: 10        # stops itself after this long unused
     max_minutes: 60         # and after this long in any case - this bounds a session's cost
   ```
5. **Write a ladder of plans**, cheapest first:
   - `smoke` (local): lint plus a tiny version of the job - few epochs, few frames, small inputs via `params:`.
   - `check` (remote, small GPU, a few minutes): the same code on the target hardware.
   - `full`: the real thing, with `max_minutes` set to what it truly needs (it is also the hard stop).
6. **Give every step expectations that fail when the result is wrong**, not just when it crashes:
   ```yaml
   expect:
     files: [outputs/model.pt]
     json: {outputs/metrics.json: {accuracy: ">= 0.9", "loss_history.-1": "< 0.3"}}
     media: {outputs/render.mp4: {frames: ">= 240", width: 1920, audio: true}}
     text: {stdout: ["saved checkpoint"]}
     web: {status: 200, text: ["Saved"], no_console_errors: true}
     junit: {path: junit.xml, failures: 0}          # the project's own tests: run them with --junitxml=junit.xml
     metrics: {stdout: {"val_loss=([0-9.]+)": "< 0.3"}}      # a number the code prints
     gpu: {busy_pct: ">= 50"}                       # on a GPU plan: an idle GPU is a failure
   ```
   Noisy numbers take a tolerance or a range: `"~= 0.93 +- 0.02"`, `"between 0.8 and 0.95"`. Every expectation:
   docs/expectations.md in the repository. Collect what a person should look at with
   `artifacts: [outputs/*.png, renders/*.mp4]`.
7. **Code nobody has reviewed** (a new dependency, a downloaded notebook) runs best in `backend: docker` with
   `docker: {network: none}`: a fresh container per run, a copy of the project, no network.
8. **Mock paid APIs.** Code that calls the `openai` or `anthropic` Python SDK needs one line on its test steps:
   `mocks: [openai, anthropic]` - strict mock clients answer instead, with no key and no network; a replies file
   gives the answers and tool calls a test needs, and `expect: {mock_calls: {anthropic: {calls: "<= 5"}}}` bounds
   the calls (docs/mocks.md). For any other paid API, make a `mocks/` folder with a module named like the real client
   (e.g. `mocks/acme.py`) built on `agent_testbench.mocks.StrictAPI`: it checks arguments against the service's
   published schema, returns a reply of the real shape (a generated test video, a fixed JSON), logs calls, and
   refuses anything else. Add `pythonpath: [mocks]` to the test steps. Real calls stay out of tests. Trading code
   on `alpaca`, `ccxt` or `ib_async` / `ib_insync` gets `mocks: {alpaca: {prices: data/prices.csv}}` (or `ccxt`,
   `ib_async`): orders fill on a paper account from that CSV, a live account is refused, and
   `mock_calls: {alpaca: {orders: "<= 20", live: 0}}` bounds what it may do (docs/mocks.md#brokers).
9. **Notebooks.** Keep them Colab-friendly (form fields `NAME = value  #@param`, no hard-coded `/content`
   paths, small previews instead of embedded video). For editing, `testbench nb split nb.ipynb cells/` and
   `testbench nb build cells/ nb.ipynb`. A step's `uploads: [[file]]` answers each `files.upload()` in order;
   secrets come from the environment under the same names as Colab secrets.
10. **Prove the setup**: `testbench run smoke` must pass before you rely on it, and show the person
   `testbench plans` so they see every plan's worst-case cost. If they use Modal or Colab, start and stop one
   short session there (`testbench session start check --backend modal --max-minutes 5`, `testbench exec "1+1"`,
   `testbench session stop check`) to prove the account works.

Reference for every field: the README of https://github.com/sarptandoven/agent-testbench.
