# Demo: an agent finds and fixes a planted bug

A real run, 2026-10-06. A copy of `examples/tiny-model` got a silent bug: the notebook's default
`LEARNING_RATE` was set to `1.0` in `cells/02_settings.py`. Nothing crashes - training diverges and accuracy
falls to 0.17, so the `smoke` plan's `accuracy >= 0.85` check fails.

Claude Code was started headless with only this plugin loaded and only these tools allowed: `testbench`,
reading and editing files.

```bash
claude -p --plugin-dir agent-testbench --model sonnet --permission-mode acceptEdits \
  --allowedTools "Bash(testbench:*)" Read Edit Write Glob Grep Skill --max-turns 40 \
  "The smoke plan in this project is failing. Use agent-testbench (the iterate skill) to find out why and fix it.
   Investigate in a local live session if that helps, and edit the notebook through its cell files in cells/,
   rebuilding tiny_model.ipynb with testbench nb build. Do not change testbench.yaml. Stop any session you start.
   When you are done, tell me which run proves it works and what you changed."
```

What it did, from the session's own log (`.testbench/sessions/main/exec.log`):

1. Started a live session and ran the notebook's cells 1-4 in it - the training cell reported accuracy 0.1711.
2. In the same kernel, with the data still loaded, tried the model with a smaller learning rate:
   ```python
   from sklearn.neural_network import MLPClassifier
   model = MLPClassifier(hidden_layer_sizes=(HIDDEN,), max_iter=15, learning_rate_init=0.001, random_state=SEED)
   model.fit(X_train, y_train)
   model.score(X_test, y_test)        # => 0.909
   ```
3. Changed `LEARNING_RATE` in `cells/02_settings.py`, rebuilt the notebook with `testbench nb build`, and
   stopped the session.
4. Proved it with the plan: `testbench run smoke` passed both steps and all five checks.

`testbench.yaml` was left byte-for-byte unchanged. Its answer:

> **It works — run `20261006-194246-smoke` (PASS, both steps, all 5 checks) proves it.**
>
> What was wrong: `LEARNING_RATE = 1.0` in `cells/02_settings.py` made `MLPClassifier`'s optimizer diverge,
> so accuracy landed at 0.1711 (checked live in a session), far below the smoke plan's `accuracy >= 0.85`
> expectation.
>
> What I changed: edited `cells/02_settings.py`, setting `LEARNING_RATE = 0.001` (verified live gave 0.909
> accuracy), then rebuilt the notebook with `testbench nb build cells tiny_model.ipynb`. `testbench.yaml` was
> left untouched. The session (`main`) was stopped once validated, cost ~$0.

Afterwards:

```
$ testbench runs
20261006-194246-smoke  passed  backend=local  ~$0.000
$ testbench session list
main  stopped on local  (stopped by request)  ~$0.000  5 exec(s)
```

In the same setup, asked to run `modal deploy app.py` directly, the agent was stopped by the plugin's guard
hook and relayed its reason: cloud runs in this project go through `testbench run <plan>`.
