# Demo: an agent improves a model with `testbench loop`

A real run, 2026-10-31. A copy of `examples/tiny-model` was set to a weak starting point - `HIDDEN = 8` in
`cells/02_settings.py` - which still passes the `smoke` plan's `accuracy >= 0.85` check (accuracy 0.86) but leaves
room to improve. The project was a git repository with that commit and nothing else.

Claude Code was started headless with only this plugin loaded and only these tools allowed: `testbench`, reading
and editing files. It could not run git, Python or anything else directly.

```bash
claude -p --plugin-dir agent-testbench --model sonnet --permission-mode acceptEdits \
  --allowedTools "Bash(testbench:*)" Read Edit Write Glob Grep Skill --max-turns 60 \
  "The smoke plan in this project passes, but its accuracy is mediocre. Use agent-testbench's loop (testbench loop)
   to raise the smoke plan's accuracy (outputs/metrics.json:accuracy) as far as you can in at most 6 tries, with a
   budget of \$0 - it runs on this machine. Edit the notebook through its cell files in cells/ and rebuild
   tiny_model.ipynb with testbench nb build before each try. Do not change testbench.yaml. Stop the loop when you
   are done, then tell me which changes helped, by how much, and which run proves the best result."
```

It took 12 turns and 78 seconds, and cost $0.24 in API tokens. The runs themselves were free, on this machine.

## What it did

It loaded the `iterate` skill, read the cell files and `testbench plans`, and started the loop:
`testbench loop start smoke --metric outputs/metrics.json:accuracy --budget-usd 0`. For each idea it edited
`cells/02_settings.py`, rebuilt the notebook, and ran `testbench loop try "..."`. testbench committed the try, ran
the plan on it, and kept or reverted it:

| try | change | accuracy | verdict |
|---|---|---|---|
| baseline | `HIDDEN = 8`, `LEARNING_RATE = 0.01` | 0.8600 | run `20261031-054550-smoke` |
| 1 | learning rate 0.01 -> 0.05 | 0.8689 | kept (+0.0089) |
| 2 | hidden units 8 -> 64 | 0.9689 | kept (+0.1) |
| 3 | learning rate 0.05 -> 0.1 | 0.9689 | reverted: not better |
| 4 | hidden units 64 -> 256 | 0.9711 | kept (+0.0022) |
| 5 | learning rate 0.05 -> 0.02 | 0.9711 | reverted: not better |
| 6 | hidden units 256 -> 512 | 0.9756 | kept (+0.0045) |

Then it ran `testbench loop stop`, which wrote the loop's report and went back to `master`. The loop's branch holds
exactly the four kept changes, and the reverted ones left nothing behind:

```
$ git log --oneline testbench-loop/20261031-054550-smoke
e648b55 testbench loop try 6: hidden units 256 -> 512
8240e09 testbench loop try 4: hidden units 64 -> 256
509b79c testbench loop try 2: hidden units 8 -> 64
89636a1 testbench loop try 1: learning rate 0.01 -> 0.05
402b1fb tiny model
```

## Its answer

> The smoke plan's accuracy went from 0.86 to 0.9756 (+0.1156) in 6 tries; run `20261031-054645-smoke` proves it
> (passed). Widening the network from 8 to 64 hidden units gave nearly all the gain - the smoke plan fixes EPOCHS
> at 15, and 8 units was too small to fit the digits in that time. The best code is on
> `testbench-loop/20261031-054550-smoke`; I haven't merged it - that's your call. `testbench.yaml` is unchanged.

(condensed; it also listed every try, as in the table above, and noted it had not run `testbench verify`.)

## Checked afterwards

```
$ testbench verify 20261031-054645-smoke
20261031-054645-smoke: all 11 evidence files match their recorded sha256 (74d4c33272563f15); ran on commit e648b55a48fc
```

Every number in its answer matches the loop's own record (`.testbench/loops/<id>/state.json`), the loop is stopped,
the checkout is back on `master`, and `testbench.yaml` was left unchanged. The verdicts are testbench's, not the
agent's: a try was kept only because its run passed and its accuracy beat the best so far.
