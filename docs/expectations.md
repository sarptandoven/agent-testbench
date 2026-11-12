# Expectations

A step passes when it ran without an error and every expectation under its `expect:` holds. Each expectation
becomes one or more checks, and each check says what it found, so a failure reads as a finding:

```
- expected metrics.json: accuracy >= 0.9: 0.71 (wanted >= 0.9)
```

Failing checks are listed first in `report.md`, under the step that missed them. Passing checks are listed at the
end, and every check is in `report.json` (`steps[].expectations[]`: `check`, `ok`, `detail`).

```yaml
expect:
  exit_code: 0                          # run steps: default 0
  no_errors: true                       # notebook steps: default true
  files: [outputs/model.pt, "plots/*.png"]
  json:
    outputs/metrics.json: {accuracy: ">= 0.9", "history.loss.-1": "~= 0.25 +- 0.01"}
  text:
    stdout: ["saved checkpoint"]
  media:
    out/render.mp4: {frames: ">= 240", width: 1920, audio: true}
  web:                                  # web steps
    status: "between 200 and 299"
    title: Counter
    text: ["Count: 2"]
  junit: {path: report.xml, tests: ">= 10", skipped: "<= 2"}
  metrics:
    stdout: {"val_loss=([0-9.eE+-]+)": "< 0.3"}
  numbers:                              # numbers a metric reader reads from files: json, csv, junit, trading, ...
    reader: csv
    files: logs/train.csv
    rules: {loss.last: "< 0.3", accuracy.max: ">= 0.9"}
  strategy:                             # a backtest's standard files, with guardrails (docs/trading.md)
    equity: out/equity.csv
    sharpe: ">= 1"
    beats: buy_and_hold
  gpu: {busy_pct: ">= 50", max_mem_mib: "<= 15000"}
  image:
    plots/loss.png: {like: golden/loss.png, max_diff: 0.02}
  duration: {minutes: "<= 5"}
  mock_calls:                           # steps with mocks: [anthropic, openai], [alpaca], [ccxt], [ib_async]
    anthropic: {calls: "<= 5", output_tokens: "<= 2000"}
    alpaca: {orders: "<= 20", orders_without_limit: 0}
```

Paths are relative to the folder the step runs in (the project folder, or its scratch copy).

A package can add kinds of check of its own: [Check plug-ins](plugins.md#checks).

## Rules

Wherever an expectation compares a value (json, media, web status, junit, metrics, numbers, strategy, gpu,
duration), the rule is one of:

| rule | holds when |
|---|---|
| `">= 0.9"`, `"<= 5"`, `"> 0"`, `"< 0.3"`, `"== done"`, `"!= 0"` | the comparison holds (numbers compare as numbers) |
| a bare value: `200`, `true`, `"24/1"` | the value is equal |
| `"~= 0.93 +- 0.01"` (or `±`, `+/-`) | the value is within 0.01 of 0.93, ends included |
| `"~= 100 +- 5%"` | the value is within 5% of 100 |
| `"between 0.8 and 0.95"` | 0.8 <= value <= 0.95 |

Quote rules in YAML (`">= 0.9"`, not `>= 0.9`). Tolerances suit noisy numbers (a loss that moves with the seed);
ranges suit numbers that are wrong when too high as well as too low (an F1 of 1.0 often means a leak).

A value that is NaN or infinite meets no rule (a loss that diverged to `inf` is not "< 0.3", and a NaN accuracy is
not "!= 0"; `"!= nan"` asks for a real number). `true` and `false` are not numbers: `true` is not `1` and not
">= 0.9" (`"== true"` matches a JSON `true`).

What a miss looks like:

```
- expected metrics.json: history.loss.-1 ~= 0.25 +- 0.01: 0.27 (wanted 0.25 ± 0.01; differs by +0.02)
- expected metrics.json: f1 between 0.8 and 0.95: 0.97 (wanted between 0.8 and 0.95)
- expected metrics.json: accuracy ~= 0.93 +- 0.01: 'n/a' is not a number, so it cannot be compared with 0.93 ± 0.01
```

## Load-time checks

`testbench.yaml` is checked when it loads, before anything runs. A malformed expectation stops there with the
field and what to write instead:

```
testbench: `plans.p.steps[0].expect.json.metrics.json.accuracy`: '~= 0.93' is neither a tolerance such as
"~= 0.93 +- 0.01" (or "~= 100 +- 5%") nor a range such as "between 0.8 and 0.95"
```

Unknown expectation names (`jsn:`), unknown keys (`gpu: {util: ...}`), a range with the high end first, a regex
that does not compile, a `junit:` without `path:` and an `image:` without `like:` are all refused this way, and so
are checks that could never fail or never pass: an empty text to find, a file with no keys under `json:`, a
comparison written `=>` or `≥`, a rule against NaN. So is a key given twice in one mapping (a second `json:` under
one `expect:` would replace the first without a word) and a misspelt step field (`expects:` would leave the step
with no checks).

## exit_code

```yaml
exit_code: 0        # a whole number; run steps check 0 when it is not given
```

```
- expected exit code 0: exited 1
```

A test command exits non-zero when tests fail. To let `junit:` report which ones, either expect that code
(`exit_code: 1`) or keep the default and read both checks.

## no_errors

Notebook steps: every cell ran without raising. On by default; `no_errors: false` turns it off.

```
- expected every cell ran without an error: cell 3: NameError: name 'undefined_name' is not defined
```

The report also shows the line that raised and the start of the cell.

## files

```yaml
files: [outputs/model.pt, "plots/*.png", "runs/**/events.*"]     # paths or globs
```

```
- expected file outputs/model.pt: missing
```

A passing check says how many matched (`1 found`).

## json

A number (or any value) inside a JSON file the step wrote. Keys are dotted paths; list items by index, `-1` is
the last.

```yaml
json:
  outputs/metrics.json:
    accuracy: ">= 0.9"
    "history.loss.-1": "< 0.3"
    status: done
```

```
- expected outputs/metrics.json: accuracy >= 0.9: 0.71 (wanted >= 0.9)
- expected outputs/metrics.json: history.loss.-1 < 0.3: no 'history.loss.-1' in the file
- expected outputs/metrics.json: accuracy: file missing
```

The file can be a pattern when its folder is not known in advance - `"**/metrics.json"` - as long as it matches
exactly one file; otherwise the check fails and says how many matched (`testbench init` writes its suggestions this
way). `testbench loop --metric "**/metrics.json:accuracy"` reads it the same way.

A file last written before the run started fails instead of being read: in a project that runs in place, last
run's `metrics.json` is still there when this run forgets to write one. The same holds for a file under `text:`.

## text

Text that must appear in the step's output (`stdout`: everything it printed, stdout and stderr) or in a file.

```yaml
text:
  stdout: ["saved checkpoint", "epoch 10"]
  logs/train.log: "finished"
```

```
- expected stdout contains 'saved checkpoint': not found
```

## metrics

A number the step printed, read with a regular expression. The first group of the **last** match is compared
(the whole match if the pattern has no group), so a training log's final value is what counts. The source is
`stdout` or a file.

```yaml
metrics:
  stdout:
    "val_loss=([0-9.eE+-]+)": "< 0.3"
    "(\\d+) passed": ">= 40"
  logs/eval.txt:
    "top-1: ([0-9.]+)%": "~= 76 +- 0.5"
```

```
- expected stdout /val_loss=([0-9.eE+-]+)/ < 0.3: 0.41 (wanted < 0.3); the last of 3 matches: 'val_loss=0.41'
- expected stdout /acc=([0-9.]+)/ >= 0.9: no match in stdout (3 lines)
```

The detail gives the value, how many lines matched and the text of the last match (which shows a pattern that
matched the wrong line, such as `loss=` inside `val_loss=`). A file source that is missing, or that was last
written before the run started, fails instead of being read.

## numbers

Rules on the numbers a [metric reader](plugins.md#metrics) reads from the step's files: testbench's own reading of
the evidence, by name, instead of a regular expression over what the code printed about itself. The built-in readers
are `json`, `csv` (each column's `.last`, `.mean`, `.max`, `.min`, and `rows`), `junit` and `trading` (a backtest's
[numbers](trading.md#the-numbers)); a package can add one, and `testbench numbers` lists them all with the files each
reads. `testbench numbers READER PATH` prints the names and numbers to write rules with.

```yaml
numbers:
  reader: trading
  files: {equity: out/equity.csv, trades: out/trades.csv}   # or a path, or a list in the reader's order
  options: {calendar: stocks, risk_free: 0.04}             # the reader's own
  rules: {sharpe: ">= 1", max_drawdown: ">= -0.2", trades: ">= 50"}
```

Several readers in one step: a list of such blocks. `files:` left out, the reader looks for its own (`metrics.json`,
`metrics.csv`, `junit.xml`, `equity.csv`, here or one folder down). A reader that takes `keys` (json) reads exactly
the names the rules give, so `losses.-1` is the last item of a list, as under `json:`.

```
- expected trading out/equity.csv: sharpe >= 1: 0.842 (wanted >= 1.0)
- expected trading out/equity.csv: sharpe >= 1: sharpe is undefined (null): volatility, sharpe and sortino are null: they need at least 3 rows (2 returns)
- expected json out/metrics.json: f1 >= 0.8: f1 is undefined (null): no 'f1' in out/metrics.json
- expected csv logs/train.csv: lr.last < 1: reader csv gave no number 'lr.last' (did you mean loss.last?); it gave rows, epoch.last, ...
- expected trading out/equity.csv: sharpe >= 1: out/equity.csv: no such file in /work/project (the equity file trading was told to read)
- expected trading out/equity.csv: sharpe >= 1: out/trades.csv: the file was last written before this run started, so it is an old result, not this run's
```

Each finding is named after the reader, the first file named and the number, and a measured number comes first in
its detail, so history and `testbench loop --metric check:sharpe` read it back. A number the reader did not give,
or gave as `null` because it is not defined, fails its rule with the reader's note on why: a rule never passes for
want of a number. A file that is missing, that the reader refuses, or that was last written before the run started
(any file it read, including one it found by itself) fails every rule of the block.

When `testbench.yaml` loads, a reader that is not installed, a key other than `reader`, `files`, `options` and
`rules`, an option the reader does not take or a value it refuses, a file role it does not read, a rule that
compares with text, and - for a reader that says which names it gives (`trading`, `junit`) - a name it does not
give are refused, with the near miss:

```
testbench: `plans.p.steps[0].expect.numbers.rules` names 'sharp' (did you mean sharpe?), a number reader trading does not give; it gives start_equity, end_equity, ...
testbench: `plans.p.steps[0].expect.numbers`: reader trading has no option 'risk_fre' (did you mean risk_free?); its options are risk_free, periods_per_year, calendar, symbol
```

## strategy

The trading pack's check: a backtest's standard files (equity.csv, and trades.csv and prices.csv next to it), rules
on testbench's numbers for it, and guardrails built in - fees or slippage must have been charged (`costs: none`
allows zero costs, and the report says so), at least 20 round trips (`min_trades`), and `beats: buy_and_hold` needs a
benchmark to beat. [Checking a strategy](trading.md#checking-a-strategy) has the whole of it.

```yaml
strategy:
  equity: out/equity.csv          # the default
  sharpe: ">= 1"                  # a rule on any of the trading numbers
  max_drawdown: ">= -0.25"
  beats: buy_and_hold
  min_trades: 30                  # default 20
  costs: required                 # default; or none
```

```
- expected strategy out/equity.csv: sharpe >= 1: 0.842 (wanted >= 1.0); daily bars, 252 a year; 3 years
- expected strategy out/equity.csv: beats buy and hold: no benchmark to beat: add a benchmark column to out/equity.csv (a buy-and-hold price or equity at the same times), or write prices.csv next to it (time, symbol, close; ...)
- expected strategy out/equity.csv: trades >= 20 (min_trades): 12 (wanted >= 20.0); round trips in out/trades.csv, from 24 fills; a number from this few trades means little: ...
- expected strategy out/equity.csv: costs modelled: false: every fee and slippage in out/trades.csv is 0: the backtest charged no trading costs, so its returns are better than an account would get. ...
```

## junit

A test suite's own JUnit XML report: pytest `--junitxml=report.xml`, jest, vitest, mocha, `node --test`,
Playwright, Go (gotestsum), Rust (cargo-nextest), Maven Surefire and most others ([other languages](#other-languages)
says how each writes one). Reports with a `<testsuites>` root and bare `<testsuite>` reports both work; the counts
come from the `<testcase>` elements (or from a suite's totals when it has none), and failures and errors together
are never fewer than the suites' totals say (a suite that broke before its tests ran may list no test case for it).

```yaml
junit: {path: report.xml, tests: ">= 10", skipped: "<= 2"}
# keys: tests, passed, failures, errors, skipped; failures: 0 and errors: 0 unless given
# shorthand: `junit: report.xml`; several reports: a list
```

A typical step:

```yaml
- name: tests
  run: python -m pytest -q --junitxml=report.xml
  expect:
    exit_code: 1          # or leave the default 0 and let both checks report
    junit: {path: report.xml, tests: ">= 40"}
```

The `failures` and `errors` checks name the first three failing test cases and their messages:

```
- expected report.xml: failures 0: 1 (wanted == 0) of 2 tests; failed: tests_gen.test_tiny.test_broken (AssertionError: math is broken assert (1 + 1) == 3)
- expected report.xml: tests >= 10: 2 (wanted >= 10.0)
- expected report.xml: JUnit report: file missing: the test command wrote no report there (pytest: add --junitxml=report.xml)
- expected report.xml: JUnit report: the file was last written before this run started, so it is an old result, not this run's
```

A report left over from an earlier run never passes for this one: a file older than the run's start is refused.
When there is no report, the detail says how the step's runner writes one (jest, vitest, mocha, `node --test`,
Playwright, gotestsum, cargo-nextest; pytest otherwise).

A failing case is named as its runner shows it (`tests.test_math.test_sub`, `sum.test.js > sum > adds`,
`example.com/calc.TestAdd`, `calc.tests::adds`), with its message - or, when the message says nothing (Go's
`Failed`), the first line of the failure's own text - and the values it compared (`Expected:` / `Received:`,
`left:` / `right:`).

### Other languages

These are the commands the recipes ([jest, vitest, go-test, cargo-test, playwright-ts](recipes.md)) and `testbench
init` use. Each was run for a real report, passing and failing, and those reports are in
[tests/data/junit/](../tests/data/junit), where a test checks
what `junit:` reads from each: the counts and the name and message of every failing case.

- **jest** (jest-junit 17): `npx --no -- jest --ci --reporters=default --reporters=jest-junit`, with
  `env: {JEST_JUNIT_OUTPUT_FILE: junit.xml, JEST_JUNIT_REPORT_TEST_SUITE_ERRORS: "true"}`. Without the second
  variable a test file that fails to load is left out of the report, and only the exit code and a lower `tests`
  count show it; with it, the file is an error named after it (jest-junit lists it twice, as an error and a failure).
- **vitest** (5): `npx --no -- vitest run --reporter=default --reporter=junit --outputFile.junit=junit.xml`. A file
  that fails to import is a failed test named after the file.
- **mocha** (12): `npx --no -- mocha --reporter xunit --reporter-option output=junit.xml`, mocha's own reporter. Its
  suite totals call every failure an error and leave out skips and hooks, so the counts come from its test cases; a
  `before` hook that throws is a failure of its own. (mocha-junit-reporter 2.2.1 does not run under mocha 12.)
- **node --test** (Node 22): `node --test --test-reporter=spec --test-reporter-destination=stdout
  --test-reporter=junit --test-reporter-destination=junit.xml`. A file that fails to load is a failed test named
  after the file.
- **Playwright** (1.64): `npx --no -- playwright test --reporter=list,junit` with
  `env: {PLAYWRIGHT_JUNIT_OUTPUT_NAME: junit.xml}`. A test that passed on a retry counts as passed (`--retries=0`
  makes it fail); each project (browser) runs, and counts, a test again.
- **Go** (gotestsum 1.13): `go run gotest.tools/gotestsum@v1.13.0 --junitfile junit.xml -- -count=1 ./...`. `go test`
  writes no JUnit; gotestsum converts its `-json` output, and `go run ...@version` pins it with no install (with no
  network, run an installed `gotestsum`: `go run` asks the module proxy whether the module is deprecated). A package
  that does not build fails as `<package> > TestMain`, a panic as the test that panicked, and a parent test fails with
  its failing subtest (both count). go-junit-report 2.1 (`go test -json ./... | go-junit-report -parser gojson`)
  works too, but leaves a panic's message out of its case.
- **Rust** (cargo-nextest 0.9): `cargo nextest run --profile ci`, with this in `.config/nextest.toml`; the report is
  `target/nextest/ci/junit.xml`. `cargo test` writes no JUnit. Ignored tests are left out (`skipped` stays 0), and
  nextest does not run doc tests.

  ```toml
  [profile.ci]
  fail-fast = false        # run every test, so the report names every failure

  [profile.ci.junit]
  path = "junit.xml"
  ```

`npx --no --` runs the project's own runner and never downloads one: plain `npx jest` in a project without jest
fetches the latest jest from the registry, and `npx --no jest --ci` hands `--ci` to npx, not to jest.

```
- expected junit.xml: failures 0: 1 (wanted == 0) of 2 tests; failed: sum.test.js > sum > adds (expected 3 to be 4 // Object.is equality)
- expected junit.xml: failures 0: 1 (wanted == 0) of 2 tests; failed: example.com/calc.TestAdd (calc_test.go:7: Add(1, 2) = 3, want 4)
- expected target/nextest/ci/junit.xml: failures 0: 1 (wanted == 0) of 2 tests; failed: calc.tests::adds (thread 'tests::adds' (2817) panicked at src/lib.rs:11:9: assertion `left == right` failed left: 3 right: 4)
- expected target/nextest/ci/junit.xml: JUnit report: file missing: the test command wrote no report there (cargo-nextest writes it only for a profile with a junit path: ...)
```

## gpu

How the GPU was used while the step ran, from `nvidia-smi` sampled every 10 seconds (and once more when the step
ends). Catches the expensive GPU that sat idle while the step ran on the CPU.

```yaml
gpu:
  busy_pct: ">= 50"         # share of readings with utilization above 10%
  mean_util_pct: ">= 30"    # mean utilization, %
  max_mem_mib: "<= 15000"   # most memory used, MiB
```

```
- expected gpu busy_pct >= 50: 20 (wanted >= 50.0) on NVIDIA A10G, 6 reading(s)
- expected gpu busy_pct >= 50: no GPU readings (nvidia-smi not found or the step was too short)
```

On a machine without a GPU the check fails rather than passing quietly: put `gpu:` only on plans that run on one.
The same numbers are in each step's `gpu` field of `report.json` and the report's "GPU busy" column.

## image

A picture the step made, compared with a known-good one: the mean absolute difference per pixel and channel,
from 0 (identical) to 1 (opposite), after converting both to RGB at the known-good image's size. Needs Pillow
where the step runs (`pip install pillow`; it comes with matplotlib).

```yaml
image:
  plots/loss.png: {like: golden/loss.png, max_diff: 0.02}     # max_diff: 0..1, default 0.02
```

```
- expected plots/loss.png looks like golden/loss.png: 0.1846 (wanted <= 0.02), the mean difference per pixel and channel (0 identical, 1 opposite)
- expected plots/loss.png looks like golden/loss.png: 0.0031 (wanted <= 0.02), ..., after resizing it from 1280x960 to the golden's 640x480
- expected plots/loss.png looks like golden/loss.png: the known-good image loss.png is missing (paths are relative to the project folder)
- expected plots/loss.png looks like golden/loss.png: Pillow is not installed where the step ran, so image checks cannot run: pip install pillow
```

Make the golden image from a run you have looked at and trust (copy it from the run's `artifacts/`), and commit
it. Fonts and anti-aliasing differ between machines, so leave a little room (0.01 to 0.03) for plots with text.
An image last written before the run started fails, like an old JUnit report.

## duration

The step's own run time, up to when its checks run.

```yaml
duration: {minutes: "<= 5"}       # or seconds: "< 30"
```

```
- expected duration minutes <= 5: 7.214 (wanted <= 5.0)
```

`max_minutes` stops a step; `duration` says how fast it must be to count as working.

## media

Videos and images, measured with `ffprobe` (which must be installed where the step runs). Keys: `frames`, `width`,
`height`, `fps` (as ffprobe prints it, e.g. `"24/1"`), `seconds`, `audio`.

```yaml
media:
  out/render.mp4: {frames: ">= 240", width: 1920, height: 1080, fps: "24/1", seconds: "~= 10 +- 0.1", audio: true}
```

```
- expected out/render.mp4: frames >= 240: 238 (wanted >= 240.0)
- expected out/render.mp4: frames: ffprobe is not installed, so media checks cannot run
```

## web

Web steps only: what the page showed after the actions.

```yaml
web:
  status: 200                 # any rule: "between 200 and 299"
  title: Counter              # the title contains it
  text: ["Count: 2"]          # the page shows each
  no_console_errors: true     # default true; page errors count too
```

```
- expected page status 200: 500 (wanted == 200)
- expected page shows 'Count: 2': not found
- expected title contains 'Counter': 'Error'
- expected no console or page errors: error: Uncaught TypeError: Cannot read properties of null (reading 'click')
- expected every action worked: action 1 {'click': '#increment'}: Timeout 5000ms exceeded.
```

## mock_calls

Steps with `mocks: [anthropic, openai]` (the mock LLM clients, [docs/mocks.md](mocks.md)): what the step asked of
them, read from the step's `mock_calls.jsonl`. Keys: `calls`, `refused`, `injected`, `input_tokens`,
`output_tokens`, `tool_calls`. `refused: 0` is checked for every mocked SDK unless given, so a request the real API
would refuse fails the step even when the code caught the error.

```yaml
mock_calls:
  anthropic: {calls: "<= 5", output_tokens: "<= 2000", tool_calls: ">= 1"}
  openai: {calls: ">= 1"}
```

```
- expected mock anthropic refused 0: 1 (wanted == 0); call 1: BadRequestError: messages[0] has role 'system': put the system prompt in the top-level system= parameter (...)
- expected mock anthropic calls <= 3: 5 (wanted <= 3.0); messages.create claude-opus-5-5 x5
```

Steps with `mocks: [alpaca]`, `[ccxt]` or `[ib_async]` (the mock brokers, [docs/mocks.md](mocks.md#brokers)) count
what the trading code did instead: `calls`, `refused`, `orders`, `fills`, `cancels`, `orders_without_limit`,
`max_order_value`, `fees` and `live` (calls on a live account). `refused: 0` is checked here too, so a live account
refused, an order the broker rejected or a method the mock does not model fails the step even when the code caught
the error.

```yaml
mock_calls:
  alpaca: {orders: "<= 20", orders_without_limit: 0, max_order_value: "<= 10000", live: 0}
  ccxt: {orders: 0}                              # this signal must not trade
```

## The numbers, over time

A check that measured a number starts its detail with it (`0.27 (wanted < 0.3)`). That is how the numbers of
every kind of check above (json values, junit counts, numbers printed to the log, a reader's numbers, a strategy's,
GPU use, image differences, durations) reach:

- the comparison with the previous run of the plan (`- train | stdout /val_loss=([0-9.eE+-]+)/ < 0.3: 0.25 -> 0.21`),
- `testbench history PLAN` (each number across runs),
- `testbench loop --metric`: `FILE:KEY` for a json expectation (`outputs/metrics.json:accuracy`), or
  `check:SUBSTRING` for the last check whose name contains SUBSTRING and found a number (`check:val_loss`,
  `check:report.xml: passed`). Check names are as the report prints them after "expected".
