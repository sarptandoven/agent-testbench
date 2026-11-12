# Watch: rerun a plan on every change

`testbench watch PLAN` runs a plan now, then again each time the project's files change, and prints one line per run:
PASS or FAIL, and what changed since the run before it. It is the quick loop while you or an agent edit: save, and the
plan that proves the work runs by itself.

```
$ testbench watch q --debounce 0.3
[watch] watching 4 files in /home/me/proj for plan q: a run starts 0.3 s after the last change, one at a time. Left out: exclude:, .gitignore, caches, and what the plan writes (out/*, out/m.json). Ctrl-C stops.
[watch] run 20261101-082031-q started (the first run of this watch)
[watch] PASS run 20261101-082031-q in 0.0 min, $0.000; since 20261101-081956-q: fixed score; out/m.json: score >= 2: 1 -> 3 (better); out/m.json: loss < 0.5: 0.5 -> 0.25 (better)
[watch] run 20261101-082033-q started (score.py changed)
[watch] PASS run 20261101-082033-q in 0.0 min, $0.000; since 20261101-082031-q: no change in step results or checked numbers
[watch] run 20261101-082034-q started (settings.py changed)
[watch] settings.py changed while run 20261101-082034-q is going: one more run when it ends
[watch] FAIL run 20261101-082034-q in 0.05 min, $0.000; since 20261101-082033-q: broke score; out/m.json: score >= 2: 3 -> 1 (worse); out/m.json: loss < 0.5: 0.25 -> 0.5 (worse). `testbench report 20261101-082034-q` says why
[watch] run 20261101-082037-q started (score.py, settings.py changed)
[watch] PASS run 20261101-082037-q in 0.0 min, $0.000; since 20261101-082034-q: fixed score; out/m.json: score >= 2: 1 -> 4 (better); out/m.json: loss < 0.5: 0.5 -> 0.2 (better)
^C[watch] stopped after 4 run(s): 3 passed, 1 did not; the last was passed.
```

(A real session, shortened a little.) The full comparison behind each line is `testbench diff`
([proof-records.md](proof-records.md#comparing-two-runs-testbench-diff)); each run's report is where it always is,
`testbench report RUN`.

## Free plans only, unless a person says yes

Watching runs a plan on every save, so a plan is watched without approval only when its worst case is $0: backend
`local`, or `docker`, `ssh` or `kubernetes` at `usd_per_hour: 0`. Anything else is refused and says why:

```
$ testbench watch gpu-check
Refused: plan gpu-check can cost up to $0.32 a run (modal A10, 10 min), and watching runs it again on every change of
the project's files. Only plans that cost nothing are watched without approval (backend local, or docker, ssh or
kubernetes at usd_per_hour 0). Ask a person; with their yes, `testbench watch gpu-check --approve` checks every run
against max_run_usd and the day limits before it starts. Or watch a free plan, and run this one once: `testbench run
gpu-check`.
```

With `--approve` (a person's yes, never the agent's own; in Claude Code the guard hook turns it into a question), each
run is still an ordinary launch: priced and checked against `max_run_usd`, the project's `max_day_usd` and this
machine's day limit before it starts. The first run refused stops the watch, since every later one would be refused
too.

## What counts as a change

Every `--interval` seconds (default 0.5) watch reads the modification time and size of the project's files, and
starts a run once nothing has changed for `--debounce` seconds (default 1): a save that writes several files makes
one run. It polls rather than using file-system events, so it needs nothing installed and works the same on Linux,
macOS and Windows; touching a file counts. Left out:

- testbench.yaml's `exclude:` and testbench's own `.testbench/` folder;
- caches and editor files: `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.tox`, `.coverage`, `*.egg-info`, `.idea`,
  `.vscode`, swap and backup files;
- the patterns in the project's `.gitignore`, and in a git repository whatever git ignores (every `.gitignore`,
  `.git/info/exclude`, the global one);
- what the plan itself writes and checks: its steps' `artifacts:` and the files their expectations read (`json:`,
  `files:`, `junit:`, `media:`, `image:`, and `text:` or `metrics:` read from a file), so a run does not set off the
  next.

If a run still rewrites the files that started it - an output the plan does not declare - watch notices after three
runs in a row and stops rerunning on them, with a line that names the files: add them to `exclude:` (or
`.gitignore`). testbench.yaml is a project file too: change a plan and the next run uses it as it is then.

## One run at a time

A change while a run is going does not start a second run beside it: it queues one more run, which starts when this
one ends and covers every change made meanwhile - never more than one waiting. Runs are the same background runs as
`testbench run` starts, so `testbench runs`, `wait`, `report`, `diff` and `ui` see them as usual.

Ctrl-C stops watching. A run that is still going is cancelled first (its driver and everything it started are
stopped, and its report says it was cancelled), so nothing is left running. SIGTERM, and a console break on Windows,
do the same - though on Windows stopping a watch has not been checked yet (its tests are skipped there; use
`--max-runs` to be sure it ends). `--max-runs N` stops after N runs (exit 0 if the last passed, 1 if not); a refused
plan exits 2.

## Notifications

A watch's runs send no `run_end` notification: a message for every save is noise. To hear when a watched plan breaks
or is fixed, add `watch_run` to `notify.on` ([notify.md](notify.md)): it is sent only for a run whose result differs
from the run before it.

```yaml
notify:
  webhook_env: TESTBENCH_WEBHOOK
  on: [run_end, loop_rule, loop_end, approval_needed, watch_run]
```
