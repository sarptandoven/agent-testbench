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
  gpu: {busy_pct: ">= 50", max_mem_mib: "<= 15000"}
  image:
    plots/loss.png: {like: golden/loss.png, max_diff: 0.02}
  duration: {minutes: "<= 5"}
```

Paths are relative to the folder the step runs in (the project folder, or its scratch copy).

## Rules

Wherever an expectation compares a value (json, media, web status, junit, metrics, gpu, duration), the rule is
one of:

| rule | holds when |
|---|---|
| `">= 0.9"`, `"<= 5"`, `"> 0"`, `"< 0.3"`, `"== done"`, `"!= 0"` | the comparison holds (numbers compare as numbers) |
| a bare value: `200`, `true`, `"24/1"` | the value is equal |
| `"~= 0.93 +- 0.01"` (or `±`, `+/-`) | the value is within 0.01 of 0.93, ends included |
| `"~= 100 +- 5%"` | the value is within 5% of 100 |
| `"between 0.8 and 0.95"` | 0.8 <= value <= 0.95 |

Quote rules in YAML (`">= 0.9"`, not `>= 0.9`). Tolerances suit noisy numbers (a loss that moves with the seed);
ranges suit numbers that are wrong when too high as well as too low (an F1 of 1.0 often means a leak).

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
that does not compile, a `junit:` without `path:` and an `image:` without `like:` are all refused this way.

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

## junit

A test suite's own JUnit XML report: pytest `--junitxml=report.xml`, jest-junit, go-junit-report, Maven
Surefire and most others. Reports with a `<testsuites>` root and bare `<testsuite>` reports both work; the counts
come from the `<testcase>` elements (or from the suites' totals when a report has none).

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

## The numbers, over time

A check that measured a number starts its detail with it (`0.27 (wanted < 0.3)`). That is how the numbers of
every kind of check above (json values, junit counts, numbers printed to the log, GPU use, image differences,
durations) reach:

- the comparison with the previous run of the plan (`- train | stdout /val_loss=([0-9.eE+-]+)/ < 0.3: 0.25 -> 0.21`),
- `testbench history PLAN` (each number across runs),
- `testbench loop --metric`: `FILE:KEY` for a json expectation (`outputs/metrics.json:accuracy`), or
  `check:SUBSTRING` for the last check whose name contains SUBSTRING and found a number (`check:val_loss`,
  `check:report.xml: passed`). Check names are as the report prints them after "expected".
