"""An export sink for agent-testbench: one markdown table row per finished run, in a file you can read on GitHub.

    export:
      markdown: docs/runs.md            # or {path: docs/runs.md}; true means .testbench/runs.md
"""
from pathlib import Path

from agent_testbench.export import ExportError, Sink

HEADER = "| run | plan | status | checks | cost | report |\n|---|---|---|---|---|---|\n"


class MarkdownSink(Sink):
    name = "markdown"
    background = False                       # a local file, written in milliseconds at the end of the run
    options = ("path",)                      # Sink.validate checks these: `path` is a non-empty string
    short, short_hint = "path", "the file's path"
    hints = {"path": ".testbench/runs.md"}
    example = {"path": "runs.md"}            # settings `testbench plugin test` tries it with

    def send(self, root, run_dir, record, opts):
        path = Path(root) / (opts.get("path") or ".testbench/runs.md")
        row = (f"| {record['run_id']} | {record['plan']} | {str(record['status']).upper()} | "
               f"{record['checks_passed']}/{record['checks']} | ${record['cost_usd'] or 0:.3f} | "
               f"[report]({record['report']}) |\n")
        try:
            old = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.is_file() else [HEADER]
            rows = [r for r in old if not r.startswith(f"| {record['run_id']} |")]    # exported again: replaced
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(rows) + row, encoding="utf-8")
        except OSError as exc:
            raise ExportError(f"could not write {path}: {exc.strerror or exc}; check that the folder can be "
                              "written, or set export.markdown.path") from None
        return f"a row in {opts.get('path') or '.testbench/runs.md'}"
