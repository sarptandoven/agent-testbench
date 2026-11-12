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
summary and uploads the run folder. Two things are opt-in: `comment: true` puts the verdict on the pull request
as one comment that later pushes edit ([Pull request comments](#pull-request-comments)), and `attest: true` has
GitHub sign report.json and the evidence, so anyone can check that a report came from your workflow at that commit
([Attestations](#attestations)).

Not on GitHub? `testbench ci --for gitlab|azure|circleci|shell` writes a job for that CI: it runs `testbench check`
(or named plans), shows each step as a test, keeps the runs and fails on a refusal ([ci.md](ci.md)).

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
- **A pull request comment** (`comment: true`): the verdict, what did not pass, a table of the numbers the checks
  found with how each moved since the base branch's latest run, the cost, and links to the job summary, the
  evidence and the attestation. One per plan, edited on every push.
- **An attestation** (`attest: true`): a link in the summary to GitHub's signed record that this workflow, at this
  commit, produced report.json and each evidence file. `testbench verify <run-id> --attestation` checks it.
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

### On pull requests, with a comment and a signed report

```yaml
name: testbench
on:
  pull_request:
  push:
    branches: [main]                # main's runs are what pull requests are compared with
jobs:
  smoke:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write          # comment: true - post and edit the comment
      actions: read                 # comment: true - compare with main's latest run, link the job's summary
      id-token: write               # attest: true - sign
      attestations: write           # attest: true - store the attestation
    steps:
      - uses: actions/checkout@v4
      - uses: sarptandoven/agent-testbench@v1
        with:
          plan: smoke
          extra-pip: -r requirements.txt
          playwright: true          # the plan has web steps
          comment: true             # does nothing on the push to main; that run is kept for the comparison
          attest: true
```

On a pull request from a fork, GitHub gives the job a read-only token and no id-token. The action sees that and
skips the comment and the attestation, with a notice in the summary; the summary, the artifact and the check work
as usual.

## Pull request comments

With `comment: true`, on a `pull_request` (or `pull_request_target`) event, the action posts one comment for the
plan, and on later pushes edits it instead of adding another. Reviewers see:

- **The verdict**: `PASS - plan smoke` (or `FAIL`, `ERROR`, `CANCELLED`, `REFUSED`), where it ran, how long it
  took, the cost and the worst case checked before it started, how many checks passed, the run id and the commit.
- **What did not pass**: each missed check with what was found (`expected metrics.json: accuracy >= 0.85, found
  0.71 (wanted >= 0.85)`), each error with the line that raised it, and the steps that never ran. A refused run says
  why it did not start.
- **The numbers checked**: a table of every check that found a number, with its value, its value in the base
  branch's latest run, and the change (`+0.02`, `same`, `new`), then the steps fixed or broken since. The base run's
  numbers come from its evidence artifact: the latest finished run of the same workflow on the pull request's base
  branch that kept an artifact of the same name. Without one (a new plan, a first pull request, or no
  `actions: read`), the comparison is with the previous run of the plan in this job, if there was one; the comment
  says which it used or why there is none.
- **Links**: the job's section of the run summary (the whole report), the evidence artifact, and the attestation
  when `attest: true`.

A failing run's comment, as markdown (the first line is hidden on GitHub):

~~~markdown
<!-- agent-testbench comment: testbench/testbench-smoke -->
### FAIL - plan `smoke`

Plan `smoke` **failed** on `local` in 0.4 min, about $0.000 (worst case $0.00); 3 of 4 checks passed. Run `20261101-120000-smoke` on commit `3a9dbfffcaec`.

**What did not pass**

- `train quick`: expected `outputs/metrics.json: accuracy >= 0.85`, found `0.71 (wanted >= 0.85)`

**Numbers checked**, compared with the latest run on `main` ([workflow run](https://github.com/OWNER/REPO/actions/runs/118), commit `0c1d2e3f4a5b`):

| | step | check | this run | before | change |
|---|---|---|---|---|---|
| **missed** | `train quick` | `outputs/metrics.json: accuracy >= 0.85` | 0.71 | 0.9689 | -0.2589 |
| ok | `train quick` | `outputs/metrics.json: epochs <= 15` | 15 | 15 | same |

Steps since then: broke `train quick`.

Links: [job summary](https://github.com/OWNER/REPO/actions/runs/123/attempts/1#summary-456) (the whole report); [evidence](https://github.com/OWNER/REPO/actions/runs/123/artifacts/789) (artifact `testbench-smoke`); [attestation](https://github.com/OWNER/REPO/attestations/42) (report.json and the evidence, signed by this workflow).

<sub>agent-testbench edits this comment on every push. Updated 2026-11-01 12:04 UTC for commit `3a9dbfffcaec`. To check it: unzip the evidence into `.testbench/runs/20261101-120000-smoke/` in the project and run `testbench verify 20261101-120000-smoke --attestation`.</sub>
~~~

What to know:

- **One comment per plan.** The comment starts with a hidden line naming the workflow and the artifact name
  (`<!-- agent-testbench comment: testbench/testbench-smoke -->`). A matrix that runs a plan on several systems gives
  each entry its own `artifact-name` already, so each gets its own comment. Only a comment written by a bot or by
  the token's own user is edited, never one where someone pasted that line. Two pushes in quick succession can race
  and leave two comments; `concurrency: {group: testbench-${{ github.ref }}, cancel-in-progress: true}` on the job
  prevents it.
- **Permissions.** `pull-requests: write` to post and edit it; `actions: read` (optional) to read the base branch's
  run and to link the job's own summary. Without `actions: read` the comment links the run's summary page and
  compares only within the job.
- **It never fails the job.** A fork's or Dependabot's read-only token is detected and skipped; a missing
  permission, GitHub being down or anything else unexpected becomes a notice in the job summary. On push, schedule
  and manual runs, there is no pull request, so nothing is posted.
- **The token** is `github-token` (the job's `GITHUB_TOKEN` by default). It is passed to the comment script in
  its environment, sent only to the GitHub API, never to the storage host an artifact download is redirected to,
  and never printed or written to a file. The comment is written by a small Python script in the action
  (`src/agent_testbench/github_action.py`, standard library only), so it works whatever `install:` is.
- **What is safe in it.** Step names, checks and values come from the pull request's own testbench.yaml, so they
  are shown as code: they cannot become links, HTML, headings or `@mentions`.

## Attestations

With `attest: true`, the action signs the run's `report.json` and every evidence file with
[GitHub's artifact attestations](https://docs.github.com/en/actions/security-guides/using-artifact-attestations-to-establish-provenance-for-builds):
a [SLSA build provenance](https://slsa.dev/spec/v1.0/provenance) record, signed with a short-lived Sigstore
certificate issued to this workflow run, that names the repository, the workflow file, the commit and the run
that made each file. Anyone can check it with `gh attestation verify`.

- **What is signed**: report.json, and each file under the run's `steps/`, named `<run-id>/<path>` in the
  attestation, whatever the verdict (a signed FAIL is a record too; a refused run has no report, so nothing is
  signed). First the action checks that the evidence still matches the sha256 report.json recorded, and signs
  nothing if it does not. One attestation holds at most 1024 files; a run that kept more gets report.json alone
  signed, which still covers the rest, because report.json records the sha256 of every evidence file and
  `testbench verify` checks them.
- **What reviewers see**: a line in the job summary, `Attestation: report.json and 11 evidence files, signed by this
  workflow at commit 3a9dbfffcaec`, linking to the attestation's page on GitHub (attest-build-provenance also adds
  its own list of attestations to the summary); the `attestation-url` output; and the signed bundle,
  `attestation.sigstore.json`, in the evidence artifact next to report.json, for checks without the GitHub API.
- **Permissions**: `id-token: write` (to get the signing certificate) and `attestations: write` (to store the
  attestation). `artifact-metadata: write` is not needed: it is for storage records of images pushed to a registry.
  Without `id-token: write` the action says so and the step fails; a failed attestation fails the step too (after
  the summary, the artifact and the comment are written), because `attest: true` asked for a proof that was not
  made.
- **Where it works**: public repositories on every current GitHub plan; private and internal repositories need
  GitHub Enterprise Cloud; GitHub Enterprise Server does not have artifact attestations. On pull requests from forks
  (no id-token) it is skipped with a notice, never failed.
- **Which action**: `actions/attest-build-provenance` v4.2.2, pinned to its commit
  `4d101475d8b20a2381f78447822ac1eab6504dd8`, with `subject-checksums` (a sha256 list the action writes) and
  `create-storage-record: false`. Since v4 it is a thin wrapper over `actions/attest`. Checked on 2026-11-01 against
  <https://github.com/actions/attest-build-provenance> (its README, `action.yml` and releases; v4.2.2 was released on
  2026-08-06) and the `actions/attest` README it points to, for the inputs, outputs, permissions and the
  1024-subject limit.

### Checking a report's attestation

Download the evidence into the project and let testbench check both the evidence and the attestation:

```bash
gh run download <workflow-run-id> -n testbench-smoke -D .testbench/runs/<run-id>    # the run id is in the summary
testbench verify <run-id> --attestation
```

```
20261101-120000-smoke: all 11 evidence files match their recorded sha256 (7de805b890bbbd64); ran on commit 3a9dbfffcaec
20261101-120000-smoke: report.json is attested: signed by octo/proj/.github/workflows/testbench.yml@refs/heads/main in https://github.com/octo/proj/actions/runs/123/attempts/1, at commit 3a9dbfffcaec, the commit the report names; checked against attestation.sigstore.json.
```

`--attestation` runs `gh attestation verify report.json --repo OWNER/REPO --format json` (with `--bundle` when the
artifact has `attestation.sigstore.json`), then checks that the commit GitHub signed is the commit the report
names. The repository is `--repo`, else `GITHUB_REPOSITORY`, else the `origin` remote. It needs
[gh](https://cli.github.com) (signed in, for attestations fetched from GitHub); without it the command says so and
exits 2. Exit codes: 0 both check out; 1 the evidence or the attestation does not; 2 it could not be checked.

With gh alone, any file of the run can be checked, and the signer pinned more tightly:

```bash
gh attestation verify .testbench/runs/<run-id>/report.json --repo OWNER/REPO \
  --signer-workflow OWNER/REPO/.github/workflows/testbench.yml --source-ref refs/heads/main
gh attestation verify .testbench/runs/<run-id>/steps/02-train-quick/artifacts/outputs/metrics.json --repo OWNER/REPO
```

What it proves, and what not: the files were produced by a run of that workflow, from that repository, at that
commit, and have not changed since. It does not make the workflow honest: on a pull request the workflow file and
testbench.yaml come from the pull request itself, so a report signed on a pull request proves what that pull
request's code checked. For a result to rely on, take the report of a run on a protected branch
(`--source-ref refs/heads/main`), where the workflow and the plan are the reviewed ones.

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
| `comment` | `false` | `true`: on a pull request, one comment per plan with the verdict, what did not pass, the numbers checked and how they moved since the base branch's latest run, the cost and links; edited on later pushes. Needs `pull-requests: write` (and `actions: read` for the comparison). Never fails the job. See [Pull request comments](#pull-request-comments). |
| `attest` | `false` | `true`: GitHub's artifact attestations sign report.json and every evidence file. Needs `id-token: write` and `attestations: write`; skipped on pull requests from forks. See [Attestations](#attestations). |
| `github-token` | `${{ github.token }}` | The token for the comment and the attestation: the job's `GITHUB_TOKEN`, with the permissions above. Never printed. |

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
| `comment-url` | With `comment: true`, a link to the pull request comment; empty when none was posted (not a pull request, a fork, no permission). |
| `attestation-url` | With `attest: true`, a link to the attestation of report.json and the evidence; empty when none was made. |

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
  a plan run twice in one job. The pull request comment (`comment: true`) compares with the base branch's latest
  run instead, from its evidence artifact.
- The artifact holds the run's logs as they were written. GitHub masks secrets in the job log, not in uploaded
  files, so never print a secret (refer to it by name, as testbench does).
- With the default `install`, the ref in `uses:` is the testbench you get: pin a tag or a commit to keep it fixed.
- Colab plans need a Colab sign-in on the runner, which a hosted runner does not have; prefer local, Modal or ssh
  plans in CI.
