"""max_log_lines: a step fails when it printed more lines than this. An example check plug-in for agent-testbench.

    expect:
      max_log_lines: 200                                  # everything the step printed
      max_log_lines: {lines: 200, file: logs/train.log}   # or a log file the step wrote

testbench finds it by its entry point (pyproject.toml), refuses a malformed value when testbench.yaml loads, and lists
its finding with the built-in ones in the report. docs/plugins.md#checks has the interface.
"""
from agent_testbench.config import ConfigError
from agent_testbench.expect import Check

EXAMPLE = "200, or {lines: 200, file: logs/train.log}"


class MaxLogLines(Check):
    name = "max_log_lines"
    keys = ("lines", "file")
    example = 200
    schema = {"description": "The most lines the step may print: a number, or {lines, file} for a log file it writes",
              "anyOf": [{"type": "integer", "minimum": 0},
                        {"type": "object", "required": ["lines"], "additionalProperties": False,
                         "properties": {"lines": {"type": "integer", "minimum": 0}, "file": {"type": "string"}}}]}

    def validate(self, where, value):
        spec = value if isinstance(value, dict) else {"lines": value}
        lines = spec.get("lines")
        if set(spec) - set(self.keys) or not isinstance(lines, int) or isinstance(lines, bool) or lines < 0:
            raise ConfigError(f"`{where}` is the most lines the step may print, e.g. {EXAMPLE}")
        if "file" in spec and not (isinstance(spec["file"], str) and spec["file"].strip()):
            raise ConfigError(f"`{where}.file` is the log's path in the project, e.g. logs/train.log")

    def check(self, value, *, workdir, result, add):
        spec = value if isinstance(value, dict) else {"lines": value}
        most, source = spec["lines"], spec.get("file")
        name = f"{source or 'log'} at most {most} lines"
        if source is None:
            text = result.get("stdout", "")
        else:
            path = workdir / source
            if not path.is_file():
                add(name, False, "file missing")
                return
            if result.get("run_started") and path.stat().st_mtime < result["run_started"] - 2:
                add(name, False, "the file was last written before this run started, so it is an old log")
                return
            text = path.read_text(errors="replace")
        lines = len(text.splitlines())
        add(name, lines <= most, f"{lines} (wanted <= {most})")          # what it found, the number first
