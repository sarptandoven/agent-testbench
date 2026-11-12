# testbench-notifier-example

A notifier for agent-testbench in one file: every event `notify:` sends (a run ended, a loop's rule fired, a launch
waits for approval) becomes one JSON line in a file, or a JSON POST to a service on this machine - something of
your own that watches a file or listens on a port.

```bash
pip install -e examples/plugins/testbench-notifier-example     # into the Python that runs testbench
```

```yaml
# testbench.yaml
notify:
  events: {path: ~/testbench-events.jsonl}       # or {url: http://127.0.0.1:8080/hook}
  on: [run_end, loop_rule, loop_end]
```

`testbench notify test` sends one test event and says whether it was taken; every attempt is a line in
`.testbench/notify.log`. It can sit next to the built-ins: `notify: {webhook_env: TESTBENCH_WEBHOOK, desktop: true,
events: {...}}` sends each event to all three at once. `testbench plugin test` checks it against the contract every
notifier keeps.

How it is made (see [docs/plugins.md#notifiers](../../../docs/plugins.md#notifiers)):

- `pyproject.toml` declares the entry point: group `agent_testbench.notifiers`, name `events` (its key under
  `notify:`), value `testbench_notifier_example:EventsNotifier`.
- `validate()` refuses what it cannot use with a `ConfigError` that names the field (`notify.events.path`).
- `send()` makes one attempt within the timeout it is given and never raises: it returns `(True, "written")` or
  `(False, what went wrong)`, never the URL.
- `where()` says where events go as notify.log may show it: the file, or only the URL's scheme and host
  (`notify.where(url)`), since a URL's path can be a secret.
