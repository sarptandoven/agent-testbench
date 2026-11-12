# Troubleshooting

Every message testbench prints says what went wrong and what to do, because the reader is often an agent that has
to fix things from the message alone. This page collects them, grouped by where you meet them: find yours by its
first words. Names in angle brackets (`<plan>`) stand for your own. Messages marked *(from the code)* need Modal,
Colab, or a machine in a broken state to appear, and are quoted from the source; the rest are from real runs on
2026-11-01.

| exit code | most commands | `testbench exec` | `testbench loop try` |
|---|---|---|---|
| `0` | passed, or done | ok | kept |
| `1` | failed | the code raised | reverted |
| `2` | refused, misconfigured or a command line it cannot read: nothing ran | refused, or no such session | refused |
| `3` | still running (`testbench wait` follows it) | interrupted at `--timeout` | still running |

Each command's own codes are in the [reference](reference.md#commands).

## The command line

`testbench` alone, or `testbench help`, lists the core commands in groups, with the ones to start with; `testbench
help --all` lists every command, and `testbench help <command>` shows its options and examples. Every refusal (exit 2)
says what was refused, why, and the command or the testbench.yaml field that fixes it.

- `` testbench: no command 'rnu'; did you mean `testbench run smoke`? `testbench help --all` lists every command. `` - a
  mistyped command. The suggestion is the whole command line, ready to run; for a word that is a subcommand it says
  where it lives: `` no command 'start'; did you mean `testbench session start` or `testbench loop start`? ``
- `` testbench: 'smoke' is not a command; the command comes first: `testbench run smoke`. `` - and for a plan's name
  on its own, `` no command 'smoke'; smoke is a plan here: `testbench run smoke` runs it. ``
- `testbench run: error: the following arguments are required: PLAN`, then `` `testbench run --help` says what it
  takes, with examples. `` - every command's help ends with examples, and the line before that one quotes the example
  that uses the option the mistake is about (`` For example: `testbench runs -n 30`. ``). `unrecognized arguments:
  --json` is the same:
  `--json` (or `--format json`) is on `report`, `diff`, `history`, `estimate`, `exec` and `rescue`.
- `argument --timeout/--wait: 'soon' is not a number` - times on the command line are seconds (`--wait`,
  `--timeout`, `--debounce`) unless the option says minutes or hours (`--max-minutes`, `--max-hours`).
- `` trian.ipynb does not exist; did you mean `testbench lint train.ipynb`? `` - the closest file of its kind there, as
  the whole command line; with none close, `<file> does not exist (paths start from <folder>)` and the files of that
  kind there. `<path> is a folder, not a file` - paths are relative to the folder testbench runs in.
- `` testbench: the other way round: `testbench nb build cells train.ipynb` (the folder of cell files, then the
  notebook to write) ``
- `no recipe 'pytst' (did you mean pytest?); the recipes are ...`, `unknown client cusor (did you mean cursor?)` and
  `` there is no llm-ap example (did you mean llm-app?); ... To copy tiny-model into a folder named llm-ap, say so:
  `testbench demo tiny-model llm-ap`. `` - `testbench demo DIR` still copies tiny-model into DIR, unless DIR looks
  like a typo of an example's name.

## Start with `testbench doctor`

It says what this machine can run, and each `no` line says what to do:

```
$ testbench doctor
ok   python >= 3.10: 3.13.17
ok   nbclient (notebook steps): installed
ok   ipykernel (notebook steps): installed
ok   playwright (web steps): installed
ok   chromium (web steps): Chromium <version> launches headless
ok   mcp (the MCP server): installed
ok   ffprobe (media checks): /usr/bin/ffprobe
ok   backends: local, modal, colab, ssh, docker
ok   modal (Modal plans and sessions): installed
no   Modal login: run `modal setup`
no   colab CLI (Colab plans and sessions): uv tool install google-colab-cli
ok   ssh client (ssh plans and sessions): /usr/bin/ssh
ok   docker CLI (docker plans and sessions): /usr/bin/docker
ok   docker daemon: server 29.9.2
no   testbench.yaml: No testbench.yaml here or in any parent of <folder>. Run `testbench init` in the project folder.
```

- `no   <module> (...): missing` - install the extra: `pip install "agent-testbench[all]"` (or `[web]`, `[mcp]`,
  `[modal]`). For web steps, also `playwright install chromium`.
- `no   chromium (web steps): Playwright 1.64.1 needs Chromium build 1248 (chromium_headless_shell-1248), which is
  not installed; /opt/pw-browsers has build 1243, for another Playwright version. Install it with: /usr/bin/python -m
  playwright install chromium` - doctor launches Chromium headless (a second or two), so it sees a browser built for
  another Playwright version; run the command it gives, which installs for the Python that runs testbench.
- `no   ffprobe (media checks): missing` - install ffmpeg; only `media:` expectations need it.
- `` no   Modal login: run `modal setup` `` - once, in your terminal (an agent should never handle the token). In CI,
  `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` in the environment count.
- `no   docker daemon: <reason> - start Docker` - the CLI is there but no daemon answers `docker info`.
- `no   backend <name>: <why>` - a backend plugin is installed but did not load; only projects that use it break.
- `no   testbench.yaml: <message>` - the file does not load; the message is one of those below.
- You only need the backends you use; `local` always works.

## Loading testbench.yaml

The file is checked when it loads, before anything runs, and a bad one stops every command (exit 2) with the
file, the field and what to write instead (`testbench plans` is a quick way to load it). The messages below start
with the file's path, `<project>/testbench.yaml: `, left out here:

- `` No testbench.yaml here or in any parent of <folder>. Run `testbench init` in the project folder. ``
  testbench looks in the current folder and its parents. `cd` into the project, or run `testbench init` there.
- `<path>/testbench.yaml is not valid YAML: while scanning for the next token found character '>' that cannot
  start any token` - a rule left unquoted: `accuracy: >= 0.9`. Quote every rule: `accuracy: ">= 0.9"`. Other
  YAML errors (`while parsing a block collection`) usually mean an indentation slip near the line they name.
- `` `docker.network` is False; write bridge (Docker's default network), none (no network at all: a sandbox), host
  (this machine's network) or the name of a network made with `docker network create` `` - YAML reads `off`,
  `no` and `false` as booleans. Write `network: none`.
- `` `project:` must be a short name of letters, digits, - or _ (it names the Modal app and volume) `` - the file
  needs `project: <name>` at the top.
- `` `plans.<plan>.steps[0]` (<step>) needs exactly one of `notebook:`, `run:` or `web:` `` - each step is one
  notebook, one shell command or one page. Split it into two steps.
- `` `plans.<plan>.steps[0].notebook`: train.ipynb does not exist (paths are relative to <project folder>) ``
- `` `plans.<plan>.steps[0].expect` has no 'jsn' check; expectations are exit_code, no_errors, files, json, text,
  media, web, junit, metrics, gpu, image, duration `` - a misspelled expectation. The list is every one there is
  ([expectations](expectations.md)).
- `` `plans.<plan>.steps[0].expect.json.m.json.acc`: '~= 0.93' is neither a tolerance such as "~= 0.93 +- 0.01"
  (or "~= 100 +- 5%") nor a range such as "between 0.8 and 0.95" `` - a tolerance needs its `+-` part.
- `` `...`: 'between 0.95 and 0.8': write the low end first, "between 0.8 and 0.95" ``
- `` `plans.<plan>.steps[0].expect.junit` needs `path:`, the JUnit XML file the test command writes, e.g.
  {path: report.xml, failures: 0, tests: ">= 10"} (pytest: --junitxml=report.xml) ``
- `` `plans.<plan>.steps[0].expect.metrics.stdout`: 'loss=([0-9.]+' is not a regular expression (missing ),
  unterminated subpattern at position 5); its first group is the number, e.g. "val_loss=([0-9.eE+-]+)" ``
- `` `plans.<plan>.backend` is 'modall'; use one of local, modal, colab, ssh, docker, or install the package that
  provides it ``
- `` `plans.<plan>.gpu` is 'T5'; modal offers T4, L4, A10, A10G, L40S, A100-40GB, A100-80GB, A100, H100, H200,
  B200, B300, RTX-PRO-6000 `` - `gpu:` names hardware the backend offers (`H100:2` for two).
- `` `plans.<plan>.gpu`: local uses whatever GPU this machine has; drop the GPU, or choose the modal or colab
  backend `` - on `local` there is nothing to choose. (On `ssh` and `docker`, `gpu:` is a label for the report.)
- `` `plans.<plan>.gpu` asks for a GPU, but containers get no GPU unless docker.gpus is set (e.g. gpus: all; needs
  NVIDIA's Container Toolkit) - set it, or drop the gpu ``
- `` `ssh.host` is needed for the ssh backend: a Host from ~/.ssh/config, or user@host, that `ssh HOST true`
  reaches without a password ``
- `` `plans.<plan>.steps[0].secrets` lists secret NAMES only, never values `` - write `secrets: [ACME_API_KEY]` and
  export the value in your shell (or create a Modal secret of that name).
- `` `plans.<plan>.steps[0].max_minutes` must be a positive number; the step is stopped when it runs out ``
- `` `budget.max_run_usd` must be a number of US dollars, e.g. 2.0 ``
- `<home>/.testbench/limits.yaml is not valid YAML (...); nothing can launch until a person fixes it` *(from the
  code)* - the machine's limits fail closed. Fix the file yourself; the Claude Code hook asks before an agent
  edits it.

## Budget: refused or needs approval

These come before anything starts, so nothing ran and nothing was spent (exit 2). `testbench plans` shows each
plan's worst case, and `testbench estimate <plan>` checks one without running it.

- `` Refused: plan <plan> could cost up to $3.10 (60 min at $0.0500/min), over budget.max_run_usd $2.00. Lower the
  max_minutes of plan <plan>'s steps in testbench.yaml (38 min in all would fit), use a cheaper GPU or backend, or have
  a person raise budget.max_run_usd in testbench.yaml. `testbench estimate <plan>` checks it again. ``
  The worst case (rate times the steps' `max_minutes`, plus start-up) is over the per-run limit. `--approve` does
  not change this. Make the plan cheaper, or a person raises `budget.max_run_usd`.
- `Refused: plan <plan> could cost up to $6.10 (120 min at $0.0500/min), over max_run_usd $5.00 in
  <home>/.testbench/limits.yaml (this machine's limit, which a project cannot raise). ... or have a person raise
  max_run_usd in <home>/.testbench/limits.yaml. ...`
  The project's budget is higher than the machine's limit, and the machine's wins. `testbench plans` says so on
  its second line (`capped by this machine: max_run_usd $5.00 in ...`). Only you raise it, in that file.
- `` Refused: $0.35 already committed today; plan <plan> could add $0.35, over budget.max_day_usd $0.60. Wait for
  running work to finish ... or have a person raise budget.max_day_usd in testbench.yaml. `testbench spend` shows
  what today's runs cost. ``
  Runs still going count at their worst case, finished ones at their measured cost (`testbench spend`). Wait for
  the running one to finish, run something cheaper, or wait until tomorrow.
- `Refused: $<n> already committed today across every project on this machine; ... over max_day_usd $<n> in
  <home>/.testbench/limits.yaml (this machine's limit, which a project cannot raise).` *(from the code)* - the
  same, for all projects together.
- `` Needs approval: plan <plan> could cost up to $0.60 (above budget.ask_above_usd $0.50). Ask a person; only they
  approve it, with `testbench run <plan> --approve`. `` (over MCP, the same yes is `approved_by_person=true` on the
  tool)
  An agent shows you the plan, its hardware and this worst case, and waits. If you agree, it reruns with
  `--approve` (in Claude Code the hook asks you again; MCP clients with elicitation ask you directly). In the
  GitHub Action, `approve: true` on the step is that yes, for every run of the workflow.
- Loops: `Needs approval: a loop on plan <plan> could spend up to $<n> in total (each run up to $<n>), above ...`,
  `` --budget-usd $0.10 has no room for one run of <plan> (worst case $0.35); give the loop at least that:
  `testbench loop start <plan> ... --budget-usd 0.35` ``, and `` the loop's budget has no room for another run:
  $<spent> of $<budget> spent, and a run could cost up to $<n>. Stop the loop with `testbench loop stop` ... ``
  *(from the code)* - a
  loop's `--budget-usd` covers all its runs; stop it and start a new one with a budget you agree to.

## Plan runs

A run that started writes a report; the failure is in it, first. `testbench report` shows the latest again, and
`--format html` makes one page with the plots and screenshots.

- `cell 3: FileNotFoundError: [Errno 2] No such file or directory: '/content/summary.json'`, then
  `raised at cell line 9: ...` - the notebook's own error, with the cell (counted among code cells from 1) and the
  line. Here a Colab-only path: `testbench lint` flags those (L008).
- `expected outputs/metrics.json: accuracy >= 0.85: 0.1711 (wanted >= 0.85)` - the code ran, the number missed.
  Fix the code, not the expectation; if the expectation was wrong, say why you changed it.
- `expected summary.json: rows: file missing` - the step did not write the file, or wrote it elsewhere: paths are
  relative to the folder the step runs in (the project folder, or its copy on a remote machine).
- `expected <file>: JUnit report: file missing: the test command wrote no report there (pytest: add
  --junitxml=report.xml)`, and `the file was last written before this run started, so it is an old result, not this
  run's` - the second also for `metrics:` reading a file, and for `image:`: a leftover file never passes those.
  (`json:`, `files:` and `text:` read a file as it is; see [what a report does not prove](why.md#what-it-does-not-prove).)
- `expected gpu busy_pct >= 50: no GPU readings (nvidia-smi not found or the step was too short)` - `gpu:` checks
  only make sense on a plan that has a GPU; a step shorter than a reading (every 10 seconds, and once at the end)
  can also miss.
- `... Pillow is not installed where the step ran, so image checks cannot run: pip install pillow` and
  `frames: ffprobe is not installed, so media checks cannot run` - install them where the step runs (`pip:` of
  the image for Modal, Docker or the ssh virtualenv).
- `` RuntimeError: files.upload() needs a person to choose files. In testbench.yaml give this step `uploads:
  [[path/to/file]]` - one list per upload() call, in order. ``
- `` SecretNotFoundError: Secret <NAME> does not exist. Headless runs read it from the environment: list it under the
  step's `secrets:` (Modal) or export it before the run. `` *(from the code)* - `userdata.get()` in a Colab
  notebook, run headless.
- `form field 'MIN_AMMOUNT' found on 0 #@param lines; the notebook's fields are: MIN_AMOUNT, TOP` - a `params:` (or
  `--params`) name that is not a form field. `testbench nb fields <notebook>` lists them.
- `web: WebSetupError: Chromium did not start: Playwright 1.64.1 needs Chromium build 1248 ..., which is not
  installed; ... Install it with: <python> -m playwright install chromium` - no browser, or one installed for a
  different Playwright version. Run the command it names (`testbench doctor` says the same).
- `web: WebSetupError: Something already answers at http://127.0.0.1:8994/ before the app started, so the step
  would test the wrong program. Stop it or use another port.` - an old server, or someone else's, holds the port.
  Stop it, or change the port in both `serve:` and `url:`.
- `<run>  lost (driver exited without finishing; see driver.log)` in `testbench runs`, or a run marked
  `error` with `the driver process exited without finishing; see driver.log` *(from the code)* - the background
  driver died (killed, out of memory, the machine rebooted). `driver.log` in the run folder says why;
  `testbench cancel <run>` also stops any remote work it left.
- `` No plan 'smok' (did you mean smoke?). Plans: <list>. `testbench plans` shows each with its hardware and worst
  case `` - from `run`, `estimate`, `watch`, `history` and `loop start`.
- `` No run '<run>'. Run ids look like 20261101-091612-tests (the latest; a prefix is enough, or last, last~1).
  `testbench runs` lists them `` - and for a plan's name where a run goes, `` tests is a plan, not a run; its latest
  run is 20261101-091612-tests (a prefix of the id is enough) ``. `` run '2026' matches 3 runs (...); give more of
  its id `` when a prefix is not enough.

`testbench verify`:

- `<run>: the evidence no longer matches its report:` then `changed: <file>`, `missing: <file>` or
  `added after the run: <file>` (exit 1) - a file under the run's `steps/` was edited, deleted or added after
  the report was written. Run the plan again for evidence you can rely on.
- `` <run> has no recorded evidence to check (it ran before agent-testbench recorded hashes, or never finished).
  `testbench status <run>` says how it stands; a new run records it: `testbench run <plan>` ``.
- `` <run> already passed, so there is nothing to cancel. `testbench report <run>` shows how it ended `` - from
  `testbench cancel`, exit 0.
- `` testbench: --repo goes with --attestation (whose workflow should have signed the report): `testbench verify
  --attestation --repo <owner/repo>` `` - `--repo` alone would
  check nothing more.

## Live sessions

- `` No session is running. Start one with `testbench session start`. `` (`testbench session stop` with none running
  says `no running sessions, so nothing to stop` and exits 0.)
- `<n> sessions are running (<names>); name one with -s` - `exec`, `files` and `session stop|sync|install|shell`
  pick the only running session; with more, name it: `testbench exec -s gpu "..."`.
- `` session 'main' is already running on local; work in it (`testbench exec -s main ...`), stop it first
  (`testbench session stop main`), or start another under a new name ``
- `` Refused: session gpu could cost up to $6.20 (60 min at $0.1000/min), over budget.max_run_usd $2.00. Start it for
  fewer minutes: `testbench session start gpu --backend docker --max-minutes 18`, ... `` - the same limits as runs.
- `` No session named 'gp' (did you mean gpu?). `testbench session list` shows them. ``
- `` testbench: nothing to run. Give exec the code (`testbench exec "print(1)"`), a file (-f train.py), notebook cells
  (--cells nb.ipynb:1-3) or code on stdin (`testbench exec < script.py`). `` - `exec` with no code in a terminal
  says so rather than wait for typed input.
- `` testbench: --params is a JSON object, e.g. '{"EPOCHS": 2}'; got '{EPOCHS: 2}' `` (quote the keys) and
  `--params goes with --cells`.
- `testbench: files get REMOTE [LOCAL] - got 0 path(s)`, and `<path> does not exist on the session's machine` from
  `files ls`.
- `(pip exited 1)` at the end of `testbench session install` - pip's own error is above it; the exit code is 1.
- `secrets not in this environment: ACME_API_KEY - export them first` - `--secret NAME` passes a variable from
  your environment; it was not set in the shell that ran testbench.
- `TIMEOUT: the code was interrupted; the kernel and its variables are still there` (exit 3) - the call ran past
  `--timeout` (default 600 seconds per cell). Nothing is lost; run it again with a longer `--timeout`, or something
  smaller.
- `session <name> is gone: its machine stopped. Start a new session.` *(from the code)* - the provider stopped it
  or it reached its time limit. Sessions stop themselves after `idle_minutes` without use and after `max_minutes`
  in any case; `testbench session list` shows why.
- `` `colab <command>` did not answer within <n> s (the Colab connection may have stalled); the session is still
  there - try again, or `testbench session list` `` *(from the code)*.
- `` a modal session can last at most 24 hours (max_minutes <= 1440): `testbench session start main --backend modal
  --max-minutes 1440` `` *(from the code)*.

## Loops

- `` the loop keeps and reverts changes with git, and this folder is not in a git repository: `git init`, commit
  the project (`git add -A && git commit -m "before the loop"`), then start the loop: `testbench loop start ...` `` -
  the same command line, ready to run again.
- `` the repository has uncommitted changes, which a revert could lose. Commit or stash them (`git stash -u`), then
  start the loop: `testbench loop start ...`. The changes: `` followed by the files (`?? scratch.py`).
- `` plan <plan> checks no 'accuracy' in summary.json, so its runs would not report the metric. The numbers it
  checks: summary.json:rows; e.g. `testbench loop start <plan> --metric summary.json:rows ...`. Or give a step of plan
  <plan> in testbench.yaml an expectation such as `expect: {json: {summary.json: {accuracy: ">= 0"}}}`, then `testbench
  loop start ...`. `` - `--metric FILE:KEY` must be a
  number one of the plan's `json:` expectations checks; or use `check:SUBSTRING` for any check that found a number.
- `` nothing changed since the best commit; edit the code, then `testbench loop try "what changed"` ``
- `` No loop is running here. Start one with `testbench loop start PLAN --metric FILE:KEY --budget-usd N`. ``
- `` loop <id> is still running here; stop it first with `testbench loop stop` `` *(from the code)*.
- `` try <n> is still running (run <run>); wait for it (`testbench wait <run>`, then `testbench loop status`)
  before the next try `` *(from the code)*.
- `there are changes outside the project folder, which the loop does not try:` *(from the code)* - the project is
  a subfolder of the repository and files elsewhere changed; commit or undo them.
- `` there are edits that were never tried; try them (`testbench loop try`) or discard them (`git checkout --
  .`), then stop: `` *(from the code)* - from `testbench loop stop`.
- `` the loop is past its --max-hours; stop it with `testbench loop stop` `` *(from the code)*.

## Backends

- ssh: `` ssh could not reach root@127.0.0.1: ssh: connect to host 127.0.0.1 port 2298: Connection refused - check
  `ssh root@127.0.0.1 true` works without a password (keys, ~/.ssh/config) `` - testbench uses ssh in BatchMode,
  so a host that wants a password or an unknown host key fails here. Make `ssh HOST true` work in your terminal
  first; extra options go in `~/.ssh/config` or `ssh.command` (`[ssh, -p, "2222"]`).
- ssh: `python3 on <host> did not answer; set ssh.python to its Python 3` *(from the code)*.
- docker: `could not build the container image from <image> (exit <n>): ... If pip could not reach its index,
  the build needs this machine's network or proxy: set docker.build_args (e.g. [--network, host]) and, behind a
  TLS-intercepting proxy, docker.ca_bundle. Or use an image that already has the basics, with docker.setup: none.`
  *(from the code)* - the image build's whole output is in the run's `docker.log`.
- docker: `` `docker` is not installed here: install Docker, or set docker.command to the container CLI you use
  (e.g. [podman]) `` *(from the code)*.
- docker: leftovers - `docker ps -a --filter label=agent-testbench`; old images -
  `docker image prune --filter label=agent-testbench`.
- Colab: `` The Colab CLI is not installed: `uv tool install google-colab-cli`, then log in once. `` *(from the
  code)*. Colab's CLI has no Windows build.
- Modal: `testbench doctor` says `` Modal login: run `modal setup` `` until there is a `~/.modal.toml` or a
  `MODAL_TOKEN_ID` in the environment.
- Plugins: `` backend '<name>' is installed but did not load (<why>); `testbench doctor` shows it - reinstall or fix
  that package `` *(from the code)*.

## The Claude Code guard hook

The plugin's hook answers some tool calls before they run: `deny` (the call does not happen; the agent sees the
reason) or `ask` (you are asked). These are its real answers:

- `` deny: agent-testbench guard: in this project, cloud runs go through `testbench run <plan>`, which enforces the
  budget in testbench.yaml and keeps the evidence. Add or edit a plan instead; if a raw command is really needed,
  ask the person to run it. `` - for `modal run|deploy|serve|shell`, `colab new|run|ssh|exec` in a project with a
  testbench.yaml. Put the work in a plan.
- `` deny: agent-testbench guard: this command could print a secret. Secrets stay with the person: refer to them by
  NAME (testbench.yaml `secrets:`, Modal secrets, Colab secrets), never by value. `` - `printenv`, `env`,
  `cat .env`, `echo $..._KEY` and similar.
- `deny: agent-testbench guard: this command could change or delete a ledger of what runs cost; it is evidence,
  written only by testbench. Leave it as it is.`
- `deny: agent-testbench guard: this command points testbench at another limits folder (TESTBENCH_USER_DIR or
  HOME). The limits in ~/.testbench stay as the person set them.`
- `ask: agent-testbench: a run with --approve spends money above the approval threshold. Plan smoke on local can
  cost up to $0.00 (6 min). Approve only if you agreed to this cost.` - say yes only to a cost you agreed to.
- `ask: agent-testbench: this edit of testbench.yaml changes its limits: budget.max_run_usd raised from $0.5 to $5.
  Approve only if you want to allow that spending.` - lowering a budget or editing plans is not asked about.
- `ask: agent-testbench: <home>/.testbench/limits.yaml holds the spending limits for every project on this
  machine. Approve only if you want the agent to change them.`
- `` ask: agent-testbench: `git push` is on this project's ask-first list (testbench.yaml guard.ask). ``
- `ask: agent-testbench: this command could rewrite testbench.yaml or ~/.testbench/limits.yaml, where the spending
  limits are. Edit the file with the edit tool instead (then only changes to the limits ask you), or approve if you
  want this.` *(from the code)* - for `sed -i`, `>` and the like on those files.

If the hook itself fails, it stays out of the way rather than block work. It is a safety net against an agent
changing its own limits, not a sandbox.

## Still stuck

Run `testbench doctor` and `testbench report` and open an issue with both:
<https://github.com/sarptandoven/agent-testbench/issues>. Every field of testbench.yaml is in the
[reference](reference.md).
