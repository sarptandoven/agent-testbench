# testbench check runs every plan that is free and needs no approval; with plan names in
# TESTBENCH_PLANS, testbench run runs each of those instead. Anything but exit 0 fails the job:
# 1 failed, 2 refused (nothing ran, so nothing was proved), 3 still running.
mkdir -p .testbench/runs .testbench/junit
code=0
if [ -z "${TESTBENCH_PLANS:-}" ]; then
  testbench check --junit .testbench/junit/check.xml || code=$?
else
  n=0
  for plan in $TESTBENCH_PLANS; do
    n=$((n + 1))
    case "$plan" in
      -* | *[!A-Za-z0-9_-]*) junit=".testbench/junit/plan-$n.xml" ;;
      *) junit=".testbench/junit/$plan.xml" ;;
    esac
    c=0
    testbench run --wait "${TESTBENCH_WAIT_SECONDS:-3600}" --junit "$junit" -- "$plan" || c=$?
    if [ "$c" = 3 ]; then
      echo "testbench: plan $plan was still running after ${TESTBENCH_WAIT_SECONDS:-3600} s;" \
           "this job cancels it."
      testbench cancel || true
    fi
    if [ "$code" = 0 ]; then code=$c; fi
  done
fi
case "$code" in
  0) echo "testbench: passed. Reports and evidence: .testbench/runs/ (kept as an artifact);" \
          "JUnit: .testbench/junit/." ;;
  1) echo "testbench: FAILED: a plan did not pass. Its report is above, and in .testbench/runs/" \
          "(kept as an artifact)." ;;
  2) echo "testbench: REFUSED (exit 2): nothing ran, so nothing was proved, and this job fails." \
          "The lines above say what was refused and what fixes it. This job never approves a" \
          "cost: a plan above the approval threshold (budget.ask_above_usd) is run by a person," \
          "not by CI." ;;
  3) echo "testbench: STILL RUNNING (exit 3) when testbench stopped following it, so nothing" \
          "was proved yet; raise TESTBENCH_WAIT_SECONDS for named plans, or shorten the plan." ;;
  *) echo "testbench: exited with code $code, which is none of its own (0 passed, 1 failed," \
          "2 refused, 3 still running): the output above says what went wrong." ;;
esac
exit "$code"
