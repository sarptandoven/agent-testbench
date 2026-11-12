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

## Is the best try luck?

An agent that tries many variants of a trading strategy will find one with a great backtest by luck alone, and
the more variants it tries, the better the luckiest one looks. A loop already counts every try, so a loop over a
strategy says, in every report, whether its best try is better than that luck:

```
best of 7 tries: deflated Sharpe 0.85 (probability the true Sharpe is above 0); not significant at 0.95 - the improvement may be luck
PBO 0.59 over 7 tries: the best in-sample try is usually not the best out of sample
```

- **Returns.** `--returns PATH` (MCP: `returns`) names each try's `equity.csv` in the standard format
  ([Trading](trading.md#equitycsv-one-row-per-bar)), or a CSV of per-period returns: one column, or a column named
  `return` or `returns`, with or without a header line. The path is the one the plan writes, such as
  `out/equity.csv`; it is looked for in the run's evidence (a step's `artifacts:`, the last step first), then in
  the run's own folder, then in its work copy (runs on this machine). Without `--returns`, the loop takes the one
  `equity.csv` the plan keeps as evidence, says so after the baseline ("returns for the luck check:
  out/equity.csv, ..."), and uses that path for every try; with two, it asks for `--returns`. A plan that keeps
  none gets a hint to add `artifacts: [out/equity.csv]` to the step that writes it. Returns are one per row of the
  equity file, with no risk-free rate taken off.
- **Every try counts.** After each try, kept or reverted, and after the baseline, its returns are kept in the
  loop's folder (`.testbench/loops/<id>/returns/<try>.json`; a series longer than 20,000 periods keeps only its
  Sharpe ratio, length, skewness and kurtosis, in `state.json`). The reverted tries are the point: they are the
  variants the best one was picked from. A try whose run kept no returns cannot be counted; `loop status` and the
  report list it and why.
- **The deflated Sharpe line** appears once 2 or more tries have returns: after each `loop try` verdict, in
  `loop status` (as `luck: ...`), in the report of `loop stop` (a section "Is the best try luck?"), in the
  `loop_try`, `loop_rule` and `loop_end` notifications, and so in the MCP loop tools' output. It is the
  [deflated Sharpe ratio](trading-stats.md#deflated-sharpe-ratio-dsr) of the loop's best try: the probability
  that its true Sharpe ratio is above 0, once the number of tries N and the spread of their Sharpe ratios are
  counted. The words are `trading.stats.deflate_tries`'s; N counts the baseline. When the try the loop kept for its
  metric is not the one with the highest Sharpe, the line names that one too; when the kept try has no returns,
  the one with the highest Sharpe stands in and the line says so. Significant means at least 0.95, or
  `--min-deflated-sharpe` when given.
- **PBO**, the [probability of backtest overfitting](trading-stats.md#probability-of-backtest-overfitting-pbo),
  is the second line once 4 or more tries have returns over the same periods (as many returns, the same first and
  last time). It is CSCV over the tries x periods table, cut into S blocks: the largest even number up to 16 that
  leaves each block 4 periods or more (so T >= 8). `loop status` says why it is missing otherwise.
- **`--min-deflated-sharpe P`** (with `--target`; MCP: `min_deflated_sharpe`): the target is met only when the
  best try also has a deflated Sharpe of at least P over all tries so far. Until then the verdict says the target
  does not count yet and the loop goes on: each further try of the same kind raises the bar the best must clear,
  so only a clearly better strategy meets it. The report says whether it was met ("Its best reached the target
  (sharpe >= 1) but not --min-deflated-sharpe 0.95 (...), so the target was not met").
- **Without it**, a `--target` met by a lucky try is still reported honestly: the line comes right after "Target
  reached" in the verdict, in the refusal of the next try, in the `loop_rule` message and in the report.
- **What is not counted**: only this loop's tries. Runs of the plan outside the loop (`testbench run`, an earlier
  loop, a backtest run by hand) are not; `testbench history PLAN` lists them, and `loop status` says how many ran
  since the loop started. Variants tried there make the true N higher and the deflated Sharpe lower than the line
  says. Counting every try as an independent trial errs the other way: near copies of each other are fewer
  independent trials, so on that count the line is cautious.
- Notifications carry `luck` (the line), `deflated_sharpe`, `deflated_sharpe_tries` and `pbo` as fields.
- A loop whose plan keeps no returns, such as a model's accuracy, says nothing about luck.

A real loop: an agent tunes a moving-average crossover on 300 days of a random walk, where there is nothing to
find. Its second idea reaches the target Sharpe of 1, but the loop does not count it:

```
$ testbench loop start backtest --metric out/metrics.json:sharpe --budget-usd 0 --target 1 \
    --min-deflated-sharpe 0.95 --max-tries 6
BASELINE: sharpe 0.6135, run 20261112-090200-backtest passed.
returns for the luck check: out/equity.csv, the one equity.csv the plan keeps as evidence (each try's own;
--returns at loop start names another)

$ testbench loop try "fast 5, slow 50"
REVERTED try 1: sharpe 0.4958 is not better than the best 0.6135. The branch is back at 82c8065db.
best of 2 tries: deflated Sharpe 0.73 (probability the true Sharpe is above 0); not significant at 0.95 - it may be luck

$ testbench loop try "fast 20, slow 50"
KEPT try 2: sharpe 1.471 (best was 0.6135, +0.8574). Run 20261112-090205-backtest passed. Best is now bdbbd3c60.
Best sharpe 1.471 reaches the target (sharpe >= 1), but the target counts only once the best try's deflated Sharpe
is at least 0.95 (--min-deflated-sharpe): its deflated Sharpe is 0.87 over 3 tries, so the loop goes on. A clearly
better strategy raises it, while more tries of the same kind lower it; or stop the loop with `testbench loop stop`
and report.
best of 3 tries: deflated Sharpe 0.87 (probability the true Sharpe is above 0); not significant at 0.95 - the improvement may be luck

$ testbench loop try "fast 10, slow 100"
REVERTED try 3: sharpe 0.4934 is not better than the best 1.471. The branch is back at bdbbd3c60. The target
(sharpe >= 1) still does not count: its deflated Sharpe is 0.86 over 4 tries (--min-deflated-sharpe 0.95).
best of 4 tries: deflated Sharpe 0.86 (probability the true Sharpe is above 0); not significant at 0.95 - the improvement may be luck
PBO 0.35 over 4 tries: the best in-sample try usually stays above the median try out of sample
...
$ testbench loop try "fast 15, slow 60"
REVERTED try 6: sharpe 1.209 is not better than the best 1.471. The branch is back at bdbbd3c60. That was the last
try the loop may make (--max-tries 6): stop the loop with `testbench loop stop` and report.
best of 7 tries: deflated Sharpe 0.85 (probability the true Sharpe is above 0); not significant at 0.95 - the improvement may be luck
PBO 0.59 over 7 tries: the best in-sample try is usually not the best out of sample

$ testbench loop stop
# Loop `20261112-090200-backtest` on plan `backtest`: sharpe 0.6135 -> 1.471 (max)

6 tries, 1 kept, $0.000 of $0.00, 0.0 h. ...
Stopping rules: target sharpe >= 1 with a deflated Sharpe of at least 0.95; at most 6 tries (6 made). It had made
all its tries when it was stopped. Its best reached the target (sharpe >= 1) but not --min-deflated-sharpe 0.95
(its deflated Sharpe is 0.85 over 7 tries), so the target was not met.

## Is the best try luck?

- best of 7 tries: deflated Sharpe 0.85 (probability the true Sharpe is above 0); not significant at 0.95 - the improvement may be luck
- PBO 0.59 over 7 tries: the best in-sample try is usually not the best out of sample
- returns: out/equity.csv in each try's run, the one equity.csv the plan keeps as evidence; per period, no risk-free
  rate taken off
- only this loop's tries are counted (the baseline too, kept or reverted): runs of plan backtest outside it are not
  (`testbench history backtest`); if they tried variants, the true number of tries is higher and the deflated
  Sharpe lower
...
```

The plan: the backtest writes `out/equity.csv` and keeps it as evidence, and testbench's own metrics step computes
the Sharpe ratio the loop judges by ([Trading](trading.md#checking-a-strategy)):

```yaml
plans:
  backtest:
    steps:
      - name: backtest
        run: python backtest.py                 # writes out/equity.csv
        artifacts: [out/equity.csv]
      - name: metrics
        run: python -m agent_testbench.trading.metrics out/equity.csv --out out/metrics.json
        expect:
          json:
            out/metrics.json: {sharpe: ">= -10"}
```

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
