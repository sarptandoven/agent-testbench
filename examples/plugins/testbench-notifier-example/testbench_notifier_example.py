"""A notifier for agent-testbench: every event as one JSON line in a file, or POSTed to a service on this machine.

    notify:
      events: {path: ~/testbench-events.jsonl}      # or {url: http://127.0.0.1:8080/hook}
      on: [run_end, loop_end]
"""
import json
import os
import urllib.request

from agent_testbench.config import ConfigError
from agent_testbench.notify import Notifier, where


class EventsNotifier(Notifier):
    name = "events"
    example = {"url": "{url}"}              # `testbench plugin test` makes {url} an address that does not answer

    def validate(self, field, opts):
        if not isinstance(opts, dict) or len(opts) != 1 or set(opts) - {"path", "url"}:
            raise ConfigError(f"`{field}` takes path (a file that gets one JSON line per event) or url (an http:// "
                              "address on this machine to POST to), e.g. `events: {path: ~/testbench-events.jsonl}`")
        (key, value), = opts.items()
        if key == "url" and not (isinstance(value, str) and value.startswith(("http://", "https://"))):
            raise ConfigError(f"`{field}.url` is an http:// URL, e.g. http://127.0.0.1:8080/hook")
        if key == "path" and not (isinstance(value, str) and os.path.isabs(os.path.expanduser(value))):
            raise ConfigError(f"`{field}.path` is a full path (or ~/...): events come from wherever testbench runs")
        return {key: os.path.expanduser(value) if key == "path" else value}

    def send(self, event, text, fields, opts, timeout):
        line = json.dumps({"text": text, **fields}, default=str)
        try:
            if "path" in opts:
                with open(opts["path"], "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                return True, "written"
            request = urllib.request.Request(opts["url"], data=line.encode(), method="POST",
                                             headers={"Content-Type": "application/json"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # a service on this machine
            with opener.open(request, timeout=timeout) as answer:
                return 200 <= answer.status < 300, f"HTTP {answer.status}"
        except Exception as exc:            # never raise; say what went wrong, without the URL
            return False, f"{type(exc).__name__}: {getattr(exc, 'reason', None) or exc}"[:200]

    def where(self, opts):
        return opts["path"] if "path" in opts else where(opts["url"])        # a URL: its scheme and host only
