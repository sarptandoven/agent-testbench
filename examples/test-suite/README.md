# test-suite

`textstats`: a small Python package (word counts, the most common words, reading time) with five pytest tests and
one planted bug. Both plans fail until it is fixed; that is the point: hand it to your agent.

- `tests` runs the suite here and judges it by pytest's own JUnit report: no failures, no errors, at least 5 tests
  (so a suite that silently collects nothing fails too). Free, seconds.
- `sandbox` runs the same suite in a fresh Docker container with `network: none`, after a step that proves the
  container cannot connect anywhere: the way to run code nobody has reviewed yet. Needs a running Docker; the first
  run builds the image (a minute). Behind a proxy, see [docs/docker.md](../../docs/docker.md).

```bash
testbench demo test-suite       # or, in this folder: testbench run tests
testbench run sandbox
```

The failing report names the test and what it got: `test_top_words_are_the_most_common (AssertionError: assert
[('c', 1), ('b', 2)] == [('a', 3), ('b', 2)] ...)`. Ask your agent: "the tests plan fails: find out why, fix the code
(not the tests), and prove it with the plan". The fix is one line in `textstats/__init__.py`.
