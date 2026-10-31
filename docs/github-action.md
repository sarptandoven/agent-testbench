# The GitHub Action

Agents open pull requests that touch notebooks, training code and web pages - changes ordinary CI cannot check.
The agent-testbench action runs a plan from your `testbench.yaml` in a workflow and turns the run into what a
reviewer looks at: the report as the job summary, a JUnit file, the evidence as an artifact, and a check that
passes or fails.

```yaml
- uses: actions/checkout@v4
- uses: sarptandoven/agent-testbench@v1     # or a tag or commit of your choice
  with:
    plan: smoke
```

The action sets up Python, installs agent-testbench from its own folder (so the action and the `testbench` command
always match), runs `testbench run <plan>` in your project, and always - pass, fail or refusal - writes the
summary and uploads the run folder.

## What reviewers see

- **The job summary**: one line with the status, the cost, the run id and the commit the run proved, then the
  run's report: what failed first (the cell, the error, the line that raised it, each expectation that missed and
  what was found instead), the steps, the evidence files and the checks that passed. A refused run shows the
  refusal and what would let it run. A link to the evidence artifact comes last.
- **A check**: the step fails when the plan does not pass, with an annotation naming the first failure
  (`plan smoke failed: train quick: expected outputs/metrics.json: accuracy >= 0.85: 0.71 ...`). With
  `fail-on-failure: false` it only warns. A refused run fails the step either way: nothing was checked.
- **The evidence**: the run folder, `.testbench/runs/<run-id>`, as an artifact (default name `testbench-<plan>`):
  `report.md`, `report.json` (the proof record: commit, environment, sha256 of every evidence file),
  `report.html` (one page with the plots and screenshots inline), `junit.xml`, and a folder per step with its
  log, executed notebook, images and artifacts. To check it later, put the folder back under
  `.testbench/runs/` in the project and run `testbench verify <run-id>`.
- **Test results**: `junit.xml` has a test case per step (failures carry the missed expectations and the
  traceback). GitHub does not show JUnit files by itself; hand `junit-path` to the test reporter you already use,
  for example:

  ```yaml
  - uses: mikepenz/action-junit-report@v5
    if: ${{ !cancelled() }}
    with:
      report_paths: ${{ steps.testbench.outputs.junit-path }}
  ```

## Examples

### A free plan on every push

```yaml
name: testbench
on: [push]
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

### On pull requests, with the summary as a comment

```yaml
name: testbench
on: [pull_request]
jobs:
  smoke:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write          # only for the comment
    steps:
      - uses: actions/checkout@v4
      - id: testbench
        uses: sarptandoven/agent-testbench@v1
        with:
          plan: smoke
          extra-pip: -r requirements.txt
          playwright: true          # the plan has web steps
      - name: comment the report on the pull request
        if: ${{ !cancelled() && steps.testbench.outputs.summary-path != '' }}
        env:
          GH_TOKEN: ${{ github.token }}
          SUMMARY: ${{ steps.testbench.outputs.summary-path }}
          PR: ${{ github.event.pull_request.number }}
        run: gh pr comment "$PR" --repo "$GITHUB_REPOSITORY" --body-file "$SUMMARY"
```

Pull requests from forks get a read-only token, so the comment step fails there; the summary and the check still
work.

### A GPU plan on Modal

Create a token in Modal's settings (API Tokens; it has an id and a secret) and add the two values as repository
secrets (Settings, Secrets and variables, Actions) named `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`. The Modal
client reads them from the environment; the action installs the `modal` package when the plan runs on Modal.

```yaml
name: gpu-check
on:
  workflow_dispatch:                # by hand from the Actions tab
  pull_request:
    paths: [train.ipynb, "src/**"]
concurrency: gpu-check              # one GPU run at a time
jobs:
  gpu:
    # pull requests from forks get no secrets; skip them rather than fail at Modal's door
    if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@v4
      - uses: sarptandoven/agent-testbench@v1
        env:
          MODAL_TOKEN_ID: ${{ secrets.MODAL_TOKEN_ID }}
          MODAL_TOKEN_SECRET: ${{ secrets.MODAL_TOKEN_SECRET }}
        with:
          plan: gpu-check
          # approve: true           # only if the plan is above budget.ask_above_usd - read "Budgets" below
```

Budgets still apply in CI: the plan's worst case (its hardware's price times its steps' `max_minutes`, plus
start-up) is checked against `budget:` in testbench.yaml before anything starts, and Modal itself stops the run at
the plan's time limit. If the workflow is cancelled, the action cancels the run on Modal as well.

## Budgets and approval

- **The limits in force** are the project's `budget:` in testbench.yaml. A GitHub-hosted runner has no
  `~/.testbench/limits.yaml` and starts with an empty ledger, so `max_day_usd` counts only that job's runs. On a
  self-hosted runner, a `limits.yaml` in the runner user's home caps every project, and its ledger carries over
  from job to job.
- **Over `max_run_usd` or a day limit**: refused, always. `approve: true` does not change that.
- **Over `ask_above_usd`**: refused unless `approve: true`. Setting it is a person approving the cost in advance -
  the repository owner, by committing the workflow - and it is a standing yes: it approves every run of that
  workflow, including runs started by pull requests that agents open. Use it on workflows you trigger yourself
  (`workflow_dispatch`, pushes to protected branches), and review a pull request that changes `approve:` or
  `budget:` as you would one that spends money, because it does.
- An agent working in the repository is told never to pass `--approve` itself; editing a workflow to set
  `approve: true` is the same decision and belongs to a person.

## Inputs

| input | default | what it does |
|---|---|---|
| `plan` | required | The plan in testbench.yaml to run (`testbench plans` lists them). |
| `project-dir` | `.` | The folder with testbench.yaml, relative to the workspace or absolute. Spaces are fine. |
| `python-version` | `3.12` | The Python that actions/setup-python installs. |
| `setup-python` | `true` | `false` skips actions/setup-python and uses the `python` already on PATH (an activated virtualenv, conda). |
| `install` | empty | Empty: agent-testbench from the action's own folder, with the `web` extra when `playwright` is true and the `modal` extra when the plan runs on Modal. A pip spec such as `agent-testbench[all]==0.2.0` installs that instead. `none` installs nothing and uses the `testbench` on PATH. |
| `extra-pip` | empty | What the project needs, as pip arguments separated by spaces or new lines, installed in the project folder together with testbench: `scikit-learn==1.5.2 matplotlib==3.9.2`, or `-r requirements.txt`. |
| `approve` | `false` | `true` passes `--approve`: a plan above `budget.ask_above_usd` may run. A standing yes from whoever commits it; see above. It never lifts `max_run_usd` or the day limits. |
| `wait-minutes` | empty | How long to follow the run. Empty: the plan's own time limit (the sum of its steps' `max_minutes`) plus 15 minutes. A run still going after this is cancelled and counts as not passed. |
| `fail-on-failure` | `true` | `false`: a plan that does not pass only warns, and `status` says what happened. A refused run fails the step either way. |
| `artifact-name` | empty | The evidence artifact's name; empty means `testbench-<plan>`. It must be unique in the workflow run: set it per matrix entry (`testbench-smoke-${{ matrix.os }}`). |
| `playwright` | `false` | `true` installs headless Chromium (and its system libraries on Linux) for the plan's `web:` steps. |

## Outputs

| output | what it holds |
|---|---|
| `status` | `passed`, `failed`, `error` or `cancelled` (the run's own status), or `refused`: the run did not start (over budget, needs approval, no such plan, testbench.yaml invalid). |
| `run-id` | The run's id; empty when it did not start. |
| `run-dir` | The run folder `.testbench/runs/<run-id>` as an absolute path; the artifact holds the same files. |
| `report-path` | The run's `report.md`. |
| `html-path` | The run's `report.html`: one page with the evidence images inline. |
| `junit-path` | The run's `junit.xml`, a test case per step, for a test reporter. |
| `summary-path` | The markdown written to the job summary, for a pull request comment for example. Set even when the run did not start. |
| `cost-usd` | What the run cost in US dollars (time x published price, as in the report), such as `0.0123`; `0.0000` when it did not start. |
| `artifact-url` | A link to the uploaded evidence artifact. |

Paths are absolute. How `testbench run` ends decides the status and the step:

| `testbench run` | `status` | the step |
|---|---|---|
| exit 0 | `passed` | passes |
| exit 1 | `failed`, `error` or `cancelled` | fails (warns with `fail-on-failure: false`) |
| exit 2 | `refused` | fails, always; the summary shows the reason |
| exit 3 (still running after `wait-minutes`) | `cancelled`, after the action cancels the run | fails (warns with `fail-on-failure: false`) |

## Notes

- Linux and macOS runners are what the action's own tests run on. agent-testbench itself runs on Windows too,
  and the action's steps are bash (Git Bash on Windows runners), but they have not been run on a Windows runner yet.
- A fresh runner has no earlier runs, so the report's "compared with the previous run" section appears only for
  a plan run twice in one job.
- The artifact holds the run's logs as they were written. GitHub masks secrets in the job log, not in uploaded
  files, so never print a secret (refer to it by name, as testbench does).
- With the default `install`, the ref in `uses:` is the testbench you get: pin a tag or a commit to keep it fixed.
- Colab plans need a Colab sign-in on the runner, which a hosted runner does not have; prefer local, Modal or ssh
  plans in CI.
