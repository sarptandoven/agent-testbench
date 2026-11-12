# Notifiers

A notifier is a place `notify:` tells people that a run ended, a loop's rule fired or ended it, or a launch waits
for a person's approval ([Notifications](notify.md)). Two ship: the **webhook** (a JSON POST, Slack-compatible)
and the **desktop** (a notification on this machine's screen). A package adds more, and they sit in the same
registry: every notifier `notify:` names gets every event `on:` lists, all at once.

```yaml
# testbench.yaml
notify:
  webhook_env: TESTBENCH_WEBHOOK                # the webhook, as always: webhook: or webhook_env:
  desktop: true                                 # the desktop notifier: no settings
  events: {path: ~/testbench-events.jsonl}      # a plug-in notifier, by its name, with its settings
  on: [run_end, loop_rule, loop_end, approval_needed]
```

`webhook:` / `webhook_env:` and `on:` are `notify:`'s own keys; every other key is a notifier's name, set to
`true` (or left empty) for one without settings, to its settings, or to `false` to leave it out. A section names
at least one notifier.

## Make one

A notifier is an object with a name, a check of its settings, `send()` and `where()`. Subclassing
`agent_testbench.notify.Notifier` is optional (it gives a notifier without settings, `NAME: true`). This is the
heart of
[examples/plugins/testbench-notifier-example](../examples/plugins/testbench-notifier-example/),
which writes each event as a JSON line to a file, or POSTs it to a service on this machine:

```python
import json, urllib.request
from agent_testbench.config import ConfigError
from agent_testbench.notify import Notifier, where


class EventsNotifier(Notifier):
    name = "events"
    example = {"url": "{url}"}           # `testbench plugin test` makes {url} an address that does not answer

    def validate(self, field, opts):      # field is "notify.events"
        if not isinstance(opts, dict) or len(opts) != 1 or set(opts) - {"path", "url"}:
            raise ConfigError(f"`{field}` takes path (a file) or url (an http:// address on this machine)")
        ...
        return opts

    def send(self, event, text, fields, opts, timeout):
        try:
            ...                           # one attempt, within `timeout` seconds
            return True, "written"
        except Exception as exc:          # never raise: say what went wrong, without the URL
            return False, f"{type(exc).__name__}: {getattr(exc, 'reason', None) or exc}"

    def where(self, opts):
        return opts["path"] if "path" in opts else where(opts["url"])     # a URL: scheme and host only
```

```toml
[project.entry-points."agent_testbench.notifiers"]
events = "testbench_notifier_example:EventsNotifier"
```

## The interface

| | what it is |
|---|---|
| `name` | its key under `notify:`: a short lowercase word, not `on` or `webhook_env` (notify:'s own keys). Left empty, the entry point's name |
| `example` | settings that validate, for `testbench plugin test`; a value `"{url}"` there becomes the address of a closed port, then of one that never answers |
| `validate(where, opts) -> dict` | the settings as written under `notify.NAME` (`None` or `true` for none) -> the settings the others get. Anything it cannot use raises `agent_testbench.config.ConfigError` naming the field (`where` is `"notify.NAME"`) and saying what to write instead |
| `send(event, text, fields, opts, timeout) -> (ok, detail)` | tell people about one event: `text` is the message (`"agent-testbench, my-project: run ... PASSED ..."`), `fields` the same facts as data (`event`, `project`, `machine`, `time` and the event's own - the table in [notify.md](notify.md#events)). One attempt, at most `timeout` seconds, and it never raises: `(True, "HTTP 200")` when the message was taken, `(False, what went wrong)` otherwise |
| `where(opts) -> str` | where messages go, as `notify.log` and `testbench notify test` show it. For a URL only its scheme and host (`notify.where(url)` does that): never its path, query or a password, which are often the secret |
| `missing(opts) -> str` (optional) | what this machine lacks to send now - a variable that is not set, a command that is not installed - or `""`. When it says something, nothing is tried and `notify.log` says that instead |

Secrets stay out of testbench.yaml: take an environment variable's NAME, in a setting ending in `_env`, as
`webhook_env` does. A setting named like a token, key, secret or password is refused unless it ends in `_env`
and holds a variable's name - whatever the notifier's own check allows.

## What testbench promises around a notifier

The guarantees of the webhook hold for every notifier:

- **Delivery never raises and never holds anything up.** Each notifier's `send()` runs in a thread of its own,
  all of them at once, given 5 seconds (`timeout`); one still going a second after that is left behind and
  written down as `no answer within 5 s`. Whatever `send()` raises becomes `not accepted (ExceptionName: ...)`.
- **Every attempt is a line in `.testbench/notify.log`**, delivered or not: `run_end to <where()>: accepted
  (<detail>)`, `... not accepted (...)`, or `run_end: nothing sent - <missing()>`.
- **The same events, from the same places**: `run_end` once per run (from `runs.finalize`, never for a loop's
  tries), `loop_rule` / `loop_end` / `loop_try` from the loop, `approval_needed` when a launch is refused with
  "Needs approval" - each sent from this machine, also for remote runs.
- **Secrets stay hidden**: before a line is written, any URL in the notifier's settings (or in a variable an
  `_env` setting names) is cut back to its scheme and host, and such a variable's value is blanked out.
- **A package that breaks does not stop the project**: a notifier whose package is installed but does not load
  keeps testbench.yaml loading; each event says in `notify.log` that it did not load. A name nothing provides is
  refused when testbench.yaml loads, with the names that exist.

`testbench notify test` sends one test message to every notifier: exit 0 when all took it, 1 when one did not
(each part of the answer says what happened), 2 when none could be tried here.

## The desktop notifier

`desktop: true` shows a notification on the screen of the machine testbench runs on - for someone working at it,
with no account and no URL. It uses `notify-send` on Linux (from libnotify: `apt install libnotify-bin`, `dnf
install libnotify`), `osascript` on macOS, and a PowerShell toast on Windows (`msg` when that fails). The text is
passed as a value (in PowerShell's case through the environment), never as part of a command. Where none is found
- a server, CI - `notify.log` says `nothing sent - no desktop notification on this machine: ...` and nothing else
happens; a command that fails (no display) is a `not accepted (exit 1: ...)` line, never a failed run.

## The contract

`testbench plugin test` checks a notifier before anyone installs it (`plugins.check("notifiers", obj)`, which is
`notify.KIND.contract`); the webhook and the desktop pass it, and a test runs it on each:

- it has a name `notify:` can use, and `validate()`, `where()` and `send()`;
- `validate()` refuses junk (`42`, a list, an unknown setting) with a `ConfigError` that names `notify.NAME`;
- with its `example` settings, against a closed port on this machine and then against one that takes the
  connection and never answers (each URL with a secret path and query), `send()` returns `(ok, detail)` within
  its timeout plus one second, never raises, never says `True` for an address nothing answers, and shows the URL's
  path or query neither in its answer nor in `where()` or `missing()`.

## Test yours

Install it editable, add it to `notify:` and run `testbench notify test`; `.testbench/notify.log` has every
attempt. testbench's own tests install the example the way pip does (a `*.dist-info` with `entry_points.txt` on
`PYTHONPATH`), run a plan through the CLI and read the file it wrote, or the local webhook stand-in it posted to
(`tests/test_sinks_notifiers_plugins.py`). In-process, `plugins.register("notifiers", "name", obj)` adds one by
hand.
