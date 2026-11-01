# Demo: an agent fixes a failing test suite in a Docker sandbox

A real run, 2026-10-31. A small package, `textstats`, had a bug: `top_words` sorted word counts ascending, so it
returned the least common words. One of its five tests failed. Its only plan runs the suite in a fresh container
with no network, judged by the suite's own JUnit report:

```yaml
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
  image: python:3.11-slim
  pip: [pytest==8.3.3]
  network: none
```

(The real file also had two lines for the proxy of the machine it ran on: `build_args` and `ca_bundle`, used only
to build the image.)

The failing report names the test and what it got:

```
- expected junit.xml: failures 0: 1 (wanted == 0) of 5 tests; failed: tests.test_textstats.test_top_words_are_the_most_common
  (AssertionError: assert [('c', 1), ('b', 2)] == [('a', 3), ('b', 2)] ...)
```

Claude Code was started headless with only this plugin and only these tools: `testbench`, reading and editing
files.

```bash
claude -p --plugin-dir agent-testbench --model sonnet --permission-mode acceptEdits \
  --allowedTools "Bash(testbench:*)" Read Edit Write Glob Grep Skill --max-turns 40 \
  "The tests plan in this project fails. Use agent-testbench to find out why and fix it, and prove the fix with the
   plan - it runs the test suite in a Docker sandbox with no network. Do not change testbench.yaml or the tests.
   When you are done, tell me what was wrong, what you changed, and which run proves it."
```

It took 10 turns and 32 seconds, and cost $0.10 in tokens. It loaded the `iterate` skill, ran the plan, read the
report and the source, changed one line, ran the plan again, and ran `testbench verify` on the passing run:

```diff
-    return sorted(counts.items(), key=lambda kv: kv[1])[:n]
+    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
```

```
$ testbench runs
20261031-064601-tests  failed  backend=docker  ~$0.000
20261031-064654-tests  failed  backend=docker  ~$0.000
20261031-064708-tests  passed  backend=docker  ~$0.000
```

Its answer, condensed:

> The `tests` plan passes now: run `20261031-064708-tests` (all 5 tests, in the Docker sandbox with no network);
> `testbench verify` confirms its evidence hashes. What was wrong: `top_words` sorted counts ascending, so it
> returned the least common words. What I changed: the sort key to `(-kv[1], kv[0])` - highest counts first, ties
> alphabetical so the order is stable. I didn't touch `testbench.yaml` or the tests. The run records the code as
> commit `6308a33814de` plus uncommitted changes, since I haven't committed the fix.

Checked afterwards: the only change in the repository is that line; the proof run's report records the commit
and that uncommitted changes were part of it, which is exactly what the agent said.

To try it yourself: `testbench demo test-suite` sets up a package like this one, with the same kind of bug, a plan
for this machine and a `sandbox` plan for Docker ([examples](examples.md#test-suite)).
