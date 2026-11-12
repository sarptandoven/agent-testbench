"""testbench-metrics-example: a metric reader for key=value logs, the lines a training loop prints.

    epoch=1 loss=0.91 acc=0.52
    saved checkpoint
    epoch=2 loss=0.25 acc=0.88

`testbench numbers kvlog train.log` finds epoch 2, loss 0.25, acc 0.88 (the last value of each key) and lines 2 (the
lines that had one). A reader subclasses agent_testbench.metrics.Reader: name, what, files, options, read(), sample().
"""
import math
import re

from agent_testbench.metrics import Reader, listed

PAIR = re.compile(r"(?<![\w.])([A-Za-z_][\w.]*)=([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|nan|inf)(?![\w.])")


class KeyValueLog(Reader):
    name = "kvlog"
    what = "the last value of each key=value in a log (loss=0.25), and how many lines had one"
    files = {"log": ("metrics.log", "train.log", "*/metrics.log")}
    options = {"keys": "only these keys (default: every key in the log, in the order they first appear)"}

    def read(self, workdir, opts):
        path = self.path(workdir, opts, "log")           # a missing file: FileNotFoundError naming it
        found, lines = {}, 0
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            pairs = PAIR.findall(line)
            lines += bool(pairs)
            found.update((key, int(value) if value.lstrip("+-").isdigit() else float(value)) for key, value in pairs)
        keys = listed(opts.get("keys")) or list(found)
        notes = [f"no {key}= in the log" for key in keys if key not in found]
        notes += [f"{key} ended at {found[key]}: not a finite number, so it is null" for key in keys
                  if key in found and not math.isfinite(found[key])]
        out = {key: found[key] if key in found and math.isfinite(found[key]) else None for key in keys}
        return {**out, "lines": lines, "notes": notes, "files": {"log": self.shown(workdir, path)}}

    def sample(self):
        return {"metrics.log": "epoch=1 loss=0.91 acc=0.52\nsaved checkpoint\nepoch=2 loss=0.25 acc=0.88\n"}
