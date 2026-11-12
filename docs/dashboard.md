# Live dashboard

Leave an agent working - a loop overnight, a long training run, a session on a GPU - and `testbench ui --serve`
lets you watch it from a browser: what is running, which step it is on, its log as it is written, what it has cost
so far against your limits, and the report the moment it ends.

```
$ testbench ui --serve --open
Dashboard for tiny-model at http://127.0.0.1:8770/ - live, read-only, for this machine only. Ctrl-C stops it.
```

| option | what it does |
|---|---|
| `--serve` | serve the dashboard instead of only writing the static pages |
| `--port N` | listen on port N (default 8770; `0` picks a free port and prints it) |
| `--open` | also open it in your browser |

It needs nothing installed beyond testbench (the Python standard library's HTTP server), runs on Linux, macOS and
Windows, and stops with Ctrl-C. Run it from the project folder, in a terminal of its own; it reads the project's
`.testbench` folder, so runs started by an agent, the MCP server or CI on this machine all show up.

## The pages

**The overview** (`/`) updates every 1.5 seconds:

- **Spend today**: what this project has committed today against `budget.max_day_usd`, what every project on this
  machine has committed against the `max_day_usd` in `~/.testbench/limits.yaml`, the per-run limits (refused above
  `max_run_usd`, a person approves above `ask_above_usd`, each with where it is set), and where notifications go.
  Committed means finished runs and sessions at what they cost, and those still going at their worst case.
- **Running now**: each run going, with the step it is on (step 2 of 3, its time against its `max_minutes`), the
  run's time against the sum of its steps' limits, and what it has cost so far. A run whose driver process exited
  without finishing it shows as LOST.
- **Sessions**: each live session's backend and GPU, how long it has been up and idle against its `max_minutes`
  and `idle_minutes`, and what it has cost so far; then the last few that stopped, with why.
- **Loop**: the running loop (or the last one): its best value and which try made it, tries made and kept, what it
  has spent of its budget, the time left, its stopping rules and where it stands on each, and its latest tries
  with what each changed.
- **Recent runs**: the latest 15, each linked to its page.

**A run's page** (`/runs/RUN/`) follows the run while it goes: its steps and their status, the time and cost, and
the log of the step running now, added to as it grows (only the new part is sent; a long log starts with its last
64 KB). When the run ends the page turns into its report - the same as `testbench report --format html`, with the
evidence images inline and links to every evidence file.

**The static pages** (`/ui/index.html`) are the ones `testbench ui` writes - every plan with a trend line per checked
number, every run, the loops and the sessions - rebuilt when you open them.

## Read-only, and this machine only

The dashboard shows; it never acts. No page launches, approves, cancels or spends anything, and there are no buttons
or forms to do so. Any program on this machine can reach the port - an agent with `curl` included - so an approval
must never be one click or request away: a person gives it on the command line with `--approve`. To stop a run,
`testbench cancel RUN`; to stop a session, `testbench session stop`.

What keeps it to you:

- It listens on 127.0.0.1 only, never on a network interface.
- It answers only requests whose `Host` header is `127.0.0.1` or `localhost` (with or without the port), so a web
  page you visit cannot reach it by pointing a name of its own at 127.0.0.1 (DNS rebinding). Anything else gets
  403.
- It serves files from the project's `.testbench` folder only, and of those only the static pages, the reports
  (`report.html`, `report.md`, `report.json`) and the step folders' evidence (logs, images, artifacts). The path is
  checked as asked and again where it leads, so `..`, encoded slashes and symbolic links cannot reach anything else.
  Never served: `state.json`, `plan.json` (it holds the steps' `env:` values), the ledgers, `notify.log`, a run's
  `driver.log`, and the session folders (a session's kernel connection file holds its key).
- Every name and text from the project - plans, steps, checks, loop tries, logs - is escaped, and a webhook is shown
  as its scheme and host only, as everywhere else in testbench ([Notifications](notify.md)).
- Pages run only the dashboard's own script, under a Content-Security-Policy that allows nothing else; evidence
  files are served as images, plain text or downloads, in a sandbox, never as a page that can run code.

To see it from another computer, forward the port over ssh rather than exposing it:
`ssh -L 8770:127.0.0.1:8770 the-machine`, then open `http://127.0.0.1:8770/` there.
