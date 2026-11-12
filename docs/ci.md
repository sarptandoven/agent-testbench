# CI: GitLab, Azure Pipelines, CircleCI and any shell

On GitHub, use [the GitHub Action](github-action.md). For every other CI, `testbench ci` writes a ready job that
runs your plans and shows their results the way that CI shows test results:

```
testbench ci                                            # the templates, and the file each one becomes
testbench ci --for gitlab -o .gitlab-ci.yml             # write one (refuses to replace a file: --force)
testbench ci --for azure --plan smoke -o azure-pipelines.yml    # testbench run smoke, instead of testbench check
testbench ci --for shell > testbench-ci.sh              # print it
```

| `--for` | file | where the results show |
|---|---|---|
| `gitlab` | `.gitlab-ci.yml` | the pipeline's and the merge request's test report (`artifacts:reports:junit`) |
| `azure` | `azure-pipelines.yml` | the run's Tests tab (`PublishTestResults@2`) |
| `circleci` | `.circleci/config.yml` | the job's Tests tab (`store_test_results`) |
| `shell` | `testbench-ci.sh` | any CI that runs a shell script (Jenkins, Buildkite, Drone, TeamCity, a cron job): point its JUnit reporter at `.testbench/junit/*.xml` |

The templates are files in the package (`src/agent_testbench/ci/`), versioned and tested with it. All four run the
same job (`ci/job.sh`), so they behave the same.

## What every job does

1. **Installs `agent-testbench==VERSION`**: the version of the `testbench` that wrote the job, so CI runs what you
   ran. Write the job again with `testbench ci` after you upgrade, or edit the version in the file.
2. **Runs your plans.** With `TESTBENCH_PLANS` empty (the default), it runs `testbench check`. That runs every plan
   that is free and needs no approval, or, with no testbench.yaml, the plans `testbench init` would write. Every
   other plan is listed with its worst case and never started. With plan names in `TESTBENCH_PLANS` (`--plan NAME`
   sets them, space-separated), it runs `testbench run` for each one, following it for up to
   `TESTBENCH_WAIT_SECONDS` (default 3600). A run still going after that is cancelled. Each name is passed after
   `--`, so it is always a value and never an option: `--approve` in the variable is a plan that does not exist,
   and it is refused.
3. **Writes JUnit** to `.testbench/junit/` (`--junit FILE` on `testbench check` and `testbench run`). Each plan
   that ran is a test suite, with a test case per step: a failed check is a failure, and an error or timeout is an
   error. A plan that was refused, or a check with nothing to run, is an error test case that says why. A plan
   that costs money, or needs a tool this machine lacks, is skipped with the reason (and the command that
   installs the tool).
4. **Keeps `.testbench/runs/`** as an artifact, pass or fail. Each run's folder has `report.md`, `report.json`
   (the proof record), its logs and evidence, so `testbench verify RUN` can check it later.
5. **Fails on anything but exit 0**, with a line that says which:

| exit | the job | it prints |
|---|---|---|
| `0` | passes | `testbench: passed.` |
| `1` | fails | `testbench: FAILED: a plan did not pass.` The report is above it, and in the artifact |
| `2` | fails | `testbench: REFUSED (exit 2): nothing ran, so nothing was proved.` Over budget, needs approval, no such plan, a testbench.yaml that does not load, or nothing to check. A refusal is never a pass |
| `3` | fails | `testbench: STILL RUNNING (exit 3)`: a named plan outlasted `TESTBENCH_WAIT_SECONDS` and was cancelled |

## Budgets in CI

No job approves a cost, and none should. Each template says so at the top. A plan whose worst case is above
`budget.ask_above_usd` is refused in CI (exit 2), and the person runs it themselves (`testbench run PLAN`, then
they are asked). Adding the approve flag to a CI job would approve, in advance, every cost the plan can reach on
every push. `max_run_usd` and the day's budget still hold in CI, as everywhere.

A CI job starts with an empty `.testbench/`, so `budget.max_day_usd` counts only that job's runs. To hold a day's
budget across jobs for plans that cost money, keep `.testbench/ledger.jsonl` in the CI's cache. `testbench check`
never starts a plan that costs money, so the default job spends nothing.

Plans on Modal or another paid machine need its credentials in the job's environment, as secrets:
`MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` for Modal. Each section below says where.

## GitLab CI/CD

```bash
testbench ci --for gitlab -o .gitlab-ci.yml
```

In short (the file itself pins the version that wrote it, and has the job written out in full):

```yaml
testbench:
  image: python:3.12
  variables:
    TESTBENCH_PLANS: ""                 # empty: testbench check; plan names: testbench run each one
    TESTBENCH_WAIT_SECONDS: "3600"
  script:
    - python -m pip install "agent-testbench==VERSION"     # the version that wrote it
    - |
      # job.sh: testbench check --junit .testbench/junit/check.xml, or testbench run for each plan
  artifacts:
    when: always
    expire_in: 30 days
    paths: [.testbench/runs/, .testbench/junit/]
    reports:
      junit: .testbench/junit/*.xml
```

What you set up: commit the file (or merge the `testbench` job into the one you have). Every pipeline runs it, and
the merge request widget and the pipeline's **Tests** tab show each step. For paid plans, add the credentials as
masked CI/CD variables (Settings > CI/CD > Variables). Add `- python -m pip install -r requirements.txt` (the
commented line) when your plans need the project's packages.

The job is checked against GitLab's published schema for `.gitlab-ci.yml`
(`app/assets/javascripts/editor/schema/ci.json`), and CircleCI's and Azure's against theirs:
`TESTBENCH_TEST_CI_SCHEMAS=1 pytest tests/test_ci.py` fetches them.

## Azure Pipelines

```bash
testbench ci --for azure -o azure-pipelines.yml
```

It uses an `ubuntu-latest` agent and three steps besides installing: `UsePythonVersion@0` (3.12), the testbench
step (`bash:`), and, with `condition: succeededOrFailed()` so that they run when testbench fails,
`PublishTestResults@2` (`testResultsFormat: JUnit`, `testResultsFiles: .testbench/junit/*.xml`) and
`PublishPipelineArtifact@1` (`.testbench/runs`, as `testbench-runs-<attempt>`).

What you set up: commit the file, then create the pipeline from it (Pipelines > New pipeline > your repository >
Existing Azure Pipelines YAML file). `trigger:` and `pr:` are left out, so every branch and pull request runs it.
Results show in the run's **Tests** tab. Secret variables do not reach a script on their own: uncomment the
`env:` block of the testbench step and map each one there (`MODAL_TOKEN_ID: $(MODAL_TOKEN_ID)`).

## CircleCI

```bash
testbench ci --for circleci -o .circleci/config.yml
```

A `testbench` job on `cimg/python:3.12`: `checkout`, install, the testbench step, then `store_test_results`
(`.testbench/junit`) and `store_artifacts` (`.testbench/runs`, as `testbench-runs`), both with `when: always`. A
`testbench` workflow runs it.

What you set up: commit the file and follow the project in CircleCI. Results show in the job's **Tests** tab and
the runs under **Artifacts**. For paid plans, add the credentials as project environment variables or in a
context. If you have a config already, merge the job and the workflow entry into it.

## Any other CI: a shell script

```bash
testbench ci --for shell -o testbench-ci.sh
```

A POSIX `sh` script. Have the CI run `sh testbench-ci.sh` in the project's folder, read JUnit from
`.testbench/junit/*.xml` and keep `.testbench/runs/` as an artifact. The script exits as testbench does, so the
CI fails the job on anything but 0. Settings come from the environment:

| variable | default | |
|---|---|---|
| `TESTBENCH_PLANS` | empty (or the `--plan` names) | empty: `testbench check`; plan names, space-separated: `testbench run` each |
| `TESTBENCH_WAIT_SECONDS` | `3600` | how long to follow each named plan before cancelling it |
| `TESTBENCH_INSTALL` | `agent-testbench==VERSION` | what pip installs first; `none` uses the `testbench` already on `PATH` (a virtualenv you set up, for example) |
| `PYTHON` | `python3` | the Python whose pip installs it |

If pip cannot install it, or no `testbench` is on `PATH`, the script says so and exits 2: nothing ran.

## The same, by hand

The jobs only put the usual commands together, so a CI step of your own can do the same:

```bash
testbench check --junit .testbench/junit/check.xml        # every free plan; JUnit for the CI
testbench run --wait 3600 --junit .testbench/junit/smoke.xml -- smoke
```

Treat every exit code but 0 as a failure. Exit 2 means nothing ran (see [Exit codes](reference.md#exit-codes)).
