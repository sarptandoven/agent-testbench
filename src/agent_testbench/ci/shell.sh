#!/bin/sh
# agent-testbench for any CI that runs a shell script (Jenkins, Buildkite, Drone, TeamCity, a cron job), written by
# `testbench ci --for shell` (agent-testbench {{version}}). Run it in the folder with testbench.yaml (or the
# project's root, where `testbench check` finds what to run with no testbench.yaml):  sh testbench-ci.sh
# It runs `testbench check` (every plan that is free and needs no approval), or `testbench run PLAN` for each plan
# named in TESTBENCH_PLANS, and writes JUnit results to .testbench/junit/*.xml: point the CI's JUnit reporter there,
# and keep .testbench/runs/ (report.md, report.html, the logs and evidence) as an artifact. It exits as testbench
# does, and anything but 0 should fail the job: 1 failed, 2 refused (nothing ran), 3 still running.
#
# Budgets: this job never approves a cost. It runs testbench without its approve flag, and that must stay so: a plan
# whose worst case needs a person's approval is refused here (exit 2), and that person runs it themselves.
#
# Settings, from the environment:
#   TESTBENCH_PLANS          empty: testbench check; plan names, space-separated: testbench run each one
#   TESTBENCH_WAIT_SECONDS   how long to follow each named plan before cancelling it (default 3600)
#   TESTBENCH_INSTALL        what pip installs first (default agent-testbench=={{version}}, the version that wrote
#                            this script); none: use the testbench command already on PATH
#   PYTHON                   the Python whose pip installs it (default python3; a virtualenv's, for example)
# Docs: https://sarptandoven.github.io/agent-testbench/ci/
set -u
: "${TESTBENCH_PLANS={{plans}}}"
: "${TESTBENCH_INSTALL=agent-testbench=={{version}}}"
if [ "$TESTBENCH_INSTALL" != none ]; then
  if ! "${PYTHON:-python3}" -m pip install --disable-pip-version-check "$TESTBENCH_INSTALL"; then
    echo "testbench: pip could not install $TESTBENCH_INSTALL (its output is above), so nothing ran and this fails."
    exit 2
  fi
fi
if ! command -v testbench > /dev/null 2>&1; then
  echo "testbench: the testbench command is not on PATH; install agent-testbench, or leave TESTBENCH_INSTALL unset."
  exit 2
fi
{{job}}
