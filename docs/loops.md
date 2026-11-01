# Loops

<!-- The first part is README.md's "Loops", and "How a loop goes" is docs/reference.md's "Loops": edit them there.
     The sections after those are this page's own. -->
--8<-- "README.md#loops"

## How a loop goes

--8<-- "docs/reference.md#loops"

A real run, with every try and verdict: [an agent improves a model with `testbench loop`](demo-loop.md).

## End to end

A real loop on a small project whose plan writes a `score` and checks it (`metrics.json: score >= 0`), started
with two stopping rules. Progress lines are left out and long lines wrapped.

```
$ testbench loop start score --metric metrics.json:score --budget-usd 0 --target 5 --patience 2
[testbench] loop 20261101-064535-score started on branch testbench-loop/20261101-064535-score; running the baseline (run 20261101-064535-score)

BASELINE: score 1, run 20261101-064535-score passed.

$ testbench loop try "raise the score to 3"
KEPT try 1: score 3 (best was 1, +2). Run 20261101-064538-score passed. Best is now 585d31b21.

$ testbench loop try "lower it to 2"
REVERTED try 2: score 2 is not better than the best 3. The branch is back at 585d31b21.

$ testbench loop try "2.5"
REVERTED try 3: score 2.5 is not better than the best 3. The branch is back at 585d31b21. That is 2 tries in a row
not kept (--patience 2): stop the loop with `testbench loop stop` and report, or try something different in kind
with `testbench loop try --new-direction "..."`.

$ testbench loop try "2.8"                                        # exit 2: refused, nothing committed
testbench: 2 tries in a row were not kept (--patience 2):
  try 2: lower it to 2 - score 2 is not better than the best 3.
  try 3: 2.5 - score 2.5 is not better than the best 3.
Best score so far: 3 (try 1, commit 585d31b21, run 20261101-064538-score). More of the same is unlikely to help.
Either stop the loop with `testbench loop stop` and report, or try an idea different in kind from these with
`testbench loop try --new-direction "what is different"` (the count starts again from it). The edits made for this
try are still there, uncommitted: undo them (`git checkout -- .`, or edit them back) before `testbench loop stop`.

$ testbench loop status
loop 20261101-064535-score (running) on plan score: max score in metrics.json; best 3 at 585d31b21; 3 tries, 1 kept; $0.000 of $0.00 spent; 8.0 h left
branch testbench-loop/20261101-064535-score (from master at 9b00803e9)
stopping rules: target score >= 5; patience 2 (2 in a row not kept now)
  -> That is 2 tries in a row not kept (--patience 2): stop the loop with `testbench loop stop` and report, or try something different in kind with `testbench loop try --new-direction "..."`.
    0  baseline  score=1  $0.000  20261101-064535-score  baseline (the code as it was)
    1  kept      score=3  $0.000  20261101-064538-score  raise the score to 3
    2  reverted  score=2  $0.000  20261101-064540-score  lower it to 2
    3  reverted  score=2.5  $0.000  20261101-064543-score  2.5

$ testbench loop try "a different model" --new-direction
KEPT try 4: score 6 (best was 3, +3). Run 20261101-064546-score passed. Best is now fa5a94a8b. Target reached
(score >= 5): stop the loop with `testbench loop stop` and report.

$ testbench loop try "seven"                                      # exit 2
testbench: the loop's target is reached: best score 6 (try 4, commit fa5a94a8b, run 20261101-064546-score), and the
target was score >= 5. No new try is made; stop the loop with `testbench loop stop` and report the result. ...

$ git checkout -- . && testbench loop stop
# Loop `20261101-064535-score` on plan `score`: score 1 -> 6 (max)

4 tries, 2 kept, $0.000 of $0.00, 0.0 h. Branch `testbench-loop/20261101-064535-score` from `master` at `9b00803e9`.
Stopping rules: target score >= 5; patience 2 (0 in a row not kept at the end). It had reached its target when it was stopped.

## Kept

- try 1 `585d31b21`: raise the score to 3 - score 1 -> 3 (+2), run `20261101-064538-score`, $0.000
- try 4 `fa5a94a8b`: [new direction] a different model - score 3 -> 6 (+3), run `20261101-064546-score`, $0.000

## Tried and reverted

- try 2: lower it to 2 - score 2 is not better than the best 3.
- try 3: 2.5 - score 2.5 is not better than the best 3.
...
```

Exit codes: `loop try` is 0 when the try is kept, 1 when it is reverted, 3 while its run is still going, and 2
when it is refused (a stopping rule, no budget left, past `--max-hours`, nothing changed).

## Stopping rules

Without them a loop goes on until its budget or `--max-hours` runs out, and an agent working alone has no rule
for when it is done or when to give up. Give it one or more at `loop start` (the MCP tool `loop_start` takes
`target`, `patience` and `max_tries`):

| flag | the next `loop try` is refused once |
|---|---|
| `--target VALUE` | the best reaches VALUE: `>=` for `--goal max`, `<=` for `min` (the baseline counts) |
| `--patience N` | the last N tries in a row were not kept (a kept try starts the count again) |
| `--max-tries N` | N tries were made, the baseline not counted |

- The verdict of the try that makes a rule fire already says so ("Target reached (score >= 5): stop the loop..."),
  so the agent can stop without trying again.
- A refused try commits nothing and runs nothing. The message says which rule, the best so far, its commit and
  its run, and what to do: stop the loop and report. For `--patience` it lists the tries that were not kept, and
  offers the other way on: `testbench loop try --new-direction "..."` (MCP: `new_direction=true`), for an idea
  different in kind from those. The count starts again from that try, and status and the report mark it
  `[new direction]`. `--new-direction` is refused when patience has not run out, so it cannot be used to skip the
  rule ahead of time.
- The rules are kept in the loop's `state.json`, shown by `loop status` (with where the loop stands on each) and
  in the report, which also says which rule had fired when the loop was stopped.
- A loop started without them behaves as before.

## Notifications

With a `notify:` webhook in testbench.yaml ([Notifications](notify.md)), the person hears about the loop without
looking: a `loop_rule` message when a stopping rule fires or a try is refused because the budget or
`--max-hours` is used up (the loop is still open; the agent was told what to do), a `loop_end` when the loop is
stopped (with the report's path), and `approval_needed` if the loop's budget needs a person's yes. A loop's runs
send no `run_end`; list `loop_try` for one message per try with its verdict (kept or reverted, and why).

```yaml
notify:
  webhook_env: TESTBENCH_WEBHOOK
  on: [run_end, loop_rule, loop_end, approval_needed]      # the default; add loop_try to follow every try
```

## Leaving a loop overnight

- **Start it while you are there.** A budget above `ask_above_usd` needs your yes for the loop's total, once
  (`--approve`, which the agent must not pass by itself). `testbench notify test` checks the webhook first.
- **Bound it four ways**: `--budget-usd` (money for every run together), `--max-hours`, `--max-tries`, and a
  `--target` that means done. Add `--patience` (3 to 5 is usual) so a run of failed ideas ends the loop instead of
  the budget.
- **Make each run cheap and short**: a plan with small `max_minutes` is both a fast try and a small worst case.
  Remote plans are stopped on the remote side at their time limit, so the bound holds with your laptop asleep; but
  the loop itself (committing, judging, reverting) runs on this machine, so it must stay awake.
- **Tell the agent what done means**: the skill `iterate` says to stop and report when a try is refused for a
  rule. A prompt such as "use testbench loop to raise accuracy; stop when the loop refuses a try, then tell me
  which changes helped and which run proves the best" works.
- **In the morning**: `testbench loop status`, the loop's `report.md`, and `.testbench/NOTES.md` (every verdict);
  `testbench verify <run>` on the run that proves the best result; then decide whether to merge the loop's branch.
