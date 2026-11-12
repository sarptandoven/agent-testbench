# Notifications

An agent left to work - a loop overnight, a long training run - finishes, gets stuck, or asks for approval while
nobody is looking. With a `notify:` section in testbench.yaml, testbench tells you: it POSTs a short JSON message
to a webhook when a run ends, when a loop's stopping rule fires and when the loop ends, and when a launch is
waiting for a person's yes. A Slack incoming
webhook works as it is; so does any URL that takes a JSON POST. Working at the machine yourself? `desktop: true`
shows the same messages on its screen ([The desktop](#the-desktop)), and a package can add other notifiers
([Other notifiers](#other-notifiers)).

## Set it up

```yaml
notify:
  webhook_env: TESTBENCH_WEBHOOK                 # the environment variable that holds the URL
  on: [run_end, loop_rule, loop_end, approval_needed]   # the default; watch_run and loop_try only when listed
```

```
$ export TESTBENCH_WEBHOOK=https://hooks.slack.com/services/...
$ testbench notify test
Test message to https://hooks.slack.com ($TESTBENCH_WEBHOOK): accepted (HTTP 200).
```

`webhook: https://...` writes the URL in testbench.yaml instead. Prefer `webhook_env`: a webhook URL's path is its
key (anyone who has it can post to your channel), and testbench.yaml is usually committed, and is copied with the
project into scratch copies and onto the machines that run remote plans. Set the variable where testbench runs:
your shell, the environment the MCP server starts in, or a CI secret.

`testbench notify test` sends one message whatever `on:` says, and says whether the service accepted it (exit 0),
refused it (exit 1, with the HTTP status and what the service said), or why nothing was sent (exit 2: no `notify:`
section, or the variable is not set). With more than one notifier it sends to each, and says how each went.

| field | what to write |
|---|---|
| `webhook` | the URL to POST to, `https://...` |
| `webhook_env` | instead: the NAME of an environment variable that holds the URL |
| `desktop` | `true`: a notification on this machine's screen as well ([The desktop](#the-desktop)) |
| another notifier's name | an installed plug-in's notifier: `true`, or its settings ([Other notifiers](#other-notifiers)) |
| `on` | a list of `run_end`, `loop_rule`, `loop_end`, `approval_needed`, `watch_run`, `loop_try`; default the first four, for every notifier. `loop_end` brings `loop_rule` along (a rule firing was sent as `loop_end` before `loop_rule` was its own event), so a list written then still hears it |

At most one of `webhook` and `webhook_env`, and at least one notifier: the webhook, `desktop`, or another (`false`
leaves one out). A mistake in the section stops testbench.yaml from loading, with a message that says what to write
instead (and never repeats the URL).

## Events

Every message is Slack's `{"text": "..."}` plus fields: `event`, `project`, `machine`, `time`, and these.

| event | sent when | fields |
|---|---|---|
| `run_end` | every run that finishes: passed, failed, cancelled or error (also one whose driver died, when `testbench wait` or `cancel` notices) - except the runs of `testbench watch` and of a loop, which `watch_run` and `loop_try` tell | `plan`, `status`, `run_id`, `backend`, `gpu`, `minutes`, `cost_usd`, `numbers` (what each measuring check found), `failed` (failed checks and errors), `stopped_because`, `report` |
| `watch_run` | only when listed in `on`: a run of `testbench watch` whose result differs from the run before it (a pass after a failure, or the other way round); a watch runs on every save, so its other runs send nothing ([watch.md](watch.md)) | those of `run_end`, and `watch`, `previous_status` |
| `loop_rule` | a loop's stopping rule fires (`target_reached`, `out_of_patience`, `max_tries_made`) or a try is refused because its budget or `--max-hours` is used up (`no_budget_left`, `out_of_time`). The loop is still open (`open: true`): the agent was told to stop it, or after patience to try something different in kind | `loop`, `plan`, `status`, `why`, `open`, `metric`, `goal`, `baseline`, `best`, `best_commit`, `best_run`, `tries`, `kept`, `cost_usd`, `budget_usd`, `branch` |
| `loop_end` | the loop is stopped (`testbench loop stop`; `status: stopped`) | those of `loop_rule`, with `open: false`, `ended_by` (the rule that had fired, if any) and `report` |
| `loop_try` | only when listed in `on`: each try of a loop (the baseline too) once it is judged: kept, reverted, or the baseline. A loop's runs send no `run_end`, since a run's PASSED cannot say whether the try was kept | `loop`, `plan`, `try`, `description`, `status` (`baseline`, `kept`, `reverted`), `run_id`, `run_status`, `metric`, `value`, `best`, `best_commit`, `cost_usd`, `new_direction` |
| `approval_needed` | a run, session or loop is refused with "Needs approval" (over `ask_above_usd`); a hard refusal (over `max_run_usd` or a day limit) is not sent, since it is not a question for you. When the MCP server asks you directly (elicitation), it is sent only if you have not answered within a minute (its text then ends "The agent's client is asking you too.") or the question could not be shown | `what`, `plan`, `backend`, `gpu`, `status`, `worst_case_usd`, `max_minutes`, `message`, `approve_with` |

A loop's tries are judged by the loop, so its runs send no `run_end`: by default you hear when a rule fires
(`loop_rule`) and when the loop is stopped (`loop_end`), and `loop_try` adds one message per try, with its verdict,
for a loop you want to follow.

Real messages, from the test suite's stand-in for a webhook (the last two with some fields left out):

```json
{"text": "agent-testbench, scoring: run 20261101-064252-score of plan score on local PASSED after 0.0 min, about $0.000. Checked: metrics.json: score >= 0 (found 3). Report: .testbench/runs/20261101-064252-score/report.md",
 "event": "run_end", "project": "scoring", "machine": "vm", "time": "2026-11-01T06:42:53", "plan": "score",
 "status": "passed", "run_id": "20261101-064252-score", "backend": "local", "gpu": null, "minutes": 0.0,
 "cost_usd": 0.0, "numbers": {"metrics.json: score >= 0": 3.0}, "failed": [], "stopped_because": null,
 "report": ".testbench/runs/20261101-064252-score/report.md"}
```

```json
{"text": "agent-testbench, scoring: loop 20261101-064535-score on plan score ran out of patience: score 1 -> 3 (max) in 3 tries, 1 kept, $0.000 of $0.00. That is 2 tries in a row not kept (--patience 2). The agent was told to stop the loop, or to try something different in kind. Best: try 1, commit 585d31b21, run 20261101-064538-score.",
 "event": "loop_rule", "status": "out_of_patience", "open": true, "loop": "20261101-064535-score", "plan": "score",
 "metric": "metrics.json:score", "goal": "max", "baseline": 1.0, "best": 3.0, "best_run": "20261101-064538-score",
 "tries": 3, "kept": 1, "cost_usd": 0, "budget_usd": 0.0, "branch": "testbench-loop/20261101-064535-score"}
```

```json
{"text": "agent-testbench, scoring: needs your approval: plan score could cost up to $66.00 (above budget.ask_above_usd $0.50). On fake FAKE1. The agent was told to wait for a person's yes: tell it yes to let it go ahead (`testbench run score --approve`), or no.",
 "event": "approval_needed", "what": "plan score", "plan": "score", "backend": "fake", "gpu": "FAKE1",
 "status": "waiting for approval", "worst_case_usd": 66.0, "max_minutes": 0.1,
 "message": "Needs approval: plan score could cost up to $66.00 (above budget.ask_above_usd $0.50). Ask a person; only they approve it, with `testbench run score --approve`.",
 "approve_with": "testbench run score --approve"}
```

## Delivery never gets in the way

- One attempt per event, at most 5 seconds in all (a slow name lookup included), and no retries. With several
  notifiers, all of them at once, each within those 5 seconds; one still going a second later is written off as
  `no answer within 5 s`.
- `run_end` goes out after the run's state says it finished, so `testbench wait` and the agent are not held up;
  `approval_needed` goes out after the launch lock is released, so no other launch waits on it.
- Each run is told once, whoever closes it out: a `testbench cancel` that stopped waiting for a slow driver and
  the driver finishing after all do not both send `run_end` (the first leaves `run_end.told` in the run's folder).
- Nothing about a notification can fail a run, a session or a loop. Every attempt, delivered or not, is a line in
  `.testbench/notify.log`, for example:

  ```
  2026-11-01T06:42:53 run_end to https://hooks.slack.com ($TESTBENCH_WEBHOOK): accepted (HTTP 200)
  2026-11-01T06:51:10 loop_rule to https://hooks.slack.com ($TESTBENCH_WEBHOOK): not accepted (HTTP 404 Not Found: no_service)
  ```

- Everything is sent from this machine. A remote run (Modal, Colab, ssh, Docker) is closed out here by its
  driver, so the remote machine never sees the URL or the variable.

## The URL stays secret

Only the URL's scheme and host are ever shown - in `notify.log`, in `testbench notify test`, in error messages -
never its path, query or a password in it. Errors in the `notify:` section do not repeat its values. The Claude Code
guard hook refuses commands that would print a webhook variable (`echo $SLACK_WEBHOOK_URL`, `printenv
TESTBENCH_WEBHOOK`), as it does for keys and tokens. Changing where notifications go is not a spending change, so
the hook lets edits of `notify:` through; edits that raise a budget still ask.

## The desktop

```yaml
notify:
  desktop: true                     # alone, or next to webhook_env:
```

A notification on the screen of the machine testbench runs on, for when you are working at it: no account, no
URL, no settings. It uses `notify-send` on Linux (from libnotify: `apt install libnotify-bin` or `dnf install
libnotify`), `osascript` on macOS, and a PowerShell toast on Windows (`msg` when that fails). The text is handed over
as a value, never as part of a command.

```
$ testbench notify test
Test message to this machine's desktop (notify-send): accepted (exit 0).
```

Where there is no way to show one - a server, CI, a container - nothing else happens: `notify.log` says
`nothing sent - no desktop notification on this machine: notify-send is not installed (...)`, and a command that
fails (no display to show it on) is a `not accepted (exit 1: ...)` line. Neither fails a run. Since a remote run is
closed out on this machine, its notification shows here too.

## Other notifiers

A package can add a notifier - a chat service, a pager, a file another tool watches - with an entry point in the
group `agent_testbench.notifiers`. Installed in the Python that runs testbench, it goes under `notify:` by its name,
with its settings, next to the webhook and the desktop:

```yaml
notify:
  webhook_env: TESTBENCH_WEBHOOK
  events: {path: ~/testbench-events.jsonl}      # examples/plugins/testbench-notifier-example
```

It hears every event `on:` lists, at the same time as the others, and keeps the same promises: within the same 5
seconds, never failing or holding up a run, every attempt a line in `notify.log`, a URL shown only as its scheme and
host, and secrets only through environment variables (a setting named like a token or key must end in `_env` and
hold a variable's name). If its package breaks, testbench.yaml still loads and `notify.log` says it did not load.
[Plug-in notifiers](plugins-notifiers.md) shows how to write one and the contract `testbench plugin test` checks.
