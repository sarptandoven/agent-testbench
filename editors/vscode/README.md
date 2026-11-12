# agent-testbench for VS Code

See what your coding agent is proving, and what it costs, without leaving the editor. The extension shows a
project's [agent-testbench](https://github.com/sarptandoven/agent-testbench) plans, runs and loop, keeps today's spend
in the status bar, and opens each run's report. It calls the same `testbench` command your agent uses, so everything
it shows is what testbench itself says.

Money decisions stay with you. A plan whose worst case needs approval runs only after you click **Approve and run**
in a dialog that shows that worst case. The extension has no setting that approves anything for you.

## What it does

**The Testbench view** (the flask in the activity bar):

- **Plans**: each plan with its hardware and worst-case cost, and whether it may run now. The play button next to a
  plan runs it:
  - a free plan runs at once;
  - a plan that costs money but is under your approval threshold asks first, showing its worst case;
  - a plan above the threshold (`ask_above_usd`) shows its worst case in a modal dialog with **Approve and run** and
    **Cancel** (see [Approvals](#approvals));
  - a plan over a limit (`max_run_usd`, a day limit) is refused, with testbench's reason and what would fix it.

  A started run is followed in a notification and in the Testbench output channel, and at its end you get its
  outcome and a button for its report.
- **Runs**: recent runs, newest first, with a status icon each (passed, failed, error, cancelled, running, lost).
  Click one to open its HTML report; right-click to verify its evidence, or cancel a run that is still going.
- **Loop**: the running loop (or the last one), read-only: its best value and which try made it, tries made and kept,
  spend against the loop's budget, time left, its stopping rules, and for a strategy whether the best try is better
  than luck (the deflated Sharpe ratio over every try, and PBO). Its tries are listed with what each changed; a click
  opens a try's report. The extension never keeps or reverts a try: `testbench loop status --json`, which this view
  reads, only reads.

**The status bar**: what is committed today in this project against its `budget.max_day_usd`, or on this machine
against `max_day_usd` in `~/.testbench/limits.yaml` when that is closer to its limit. It turns to the warning color
at 80% of a day limit (`agentTestbench.spendWarningPercent`) and to the error color at the limit, where testbench
refuses new runs. Click it for `testbench spend`.

**Commands** (Command Palette, `Testbench:`):

| command | runs | |
|---|---|---|
| Check this project | `testbench check --json` | every plan that is free and needs no approval, run now; the others are only listed. With no testbench.yaml, what `testbench init` would set up, run here for free |
| Run plan... | `testbench run PLAN --detach` | pick a plan; it runs as its play button would (an approval dialog first when it needs one) |
| Show latest report | `testbench report --format html` | the latest run's report |
| Show report... | the same | a run's report, picked from the recent ones |
| Open dashboard | `testbench ui --serve --open` | the [live dashboard](https://sarptandoven.github.io/agent-testbench/dashboard/) in a terminal of its own (read-only, on 127.0.0.1) |
| Verify run | `testbench verify RUN` | check a run's evidence files against the sha256 its report recorded |
| Cancel run | `testbench cancel RUN` | stop a run here and on its remote machine (after a confirmation) |
| Show spend | `testbench spend` | what runs and sessions have cost |
| Refresh | | read everything again (the views also refresh when a run, the ledger, the loop or testbench.yaml changes) |

**Reports** open in a webview with a strict Content-Security-Policy: nothing loads but the page's own stylesheet,
images inside the page, and the extension's own small script, which only asks to open an evidence file. A link to an
evidence file opens it in the editor only if it is a file inside that run's folder (after following links); the
webview may load nothing from anywhere else.

**Autocomplete for testbench.yaml**, with the [YAML extension](https://marketplace.visualstudio.com/items?itemName=redhat.vscode-yaml):
the extension asks `testbench schema` for the schema of the version you have installed (with your installed check
plug-ins), keeps it in its own storage, and gives it to the YAML extension for every `testbench.yaml` - so it works
offline. Until then, or if testbench cannot be started, the schema on GitHub is used. A first line
`# yaml-language-server: $schema=...` in the file (which `testbench init` writes) takes precedence over both; delete
it to use the installed version's schema.

## Approvals

testbench refuses to start a plan whose worst case is above `ask_above_usd` unless a person approves it with
`testbench run PLAN --approve`. In the extension, that person is you, and the approval is your click:

1. You press a plan's play button (or **Run plan...**). The extension reads the plan as testbench sees it now
   (`testbench plans --json`).
2. A modal dialog shows where it runs, its steps and minutes, its worst case, testbench's own verdict ("Needs
   approval: plan train could cost up to $0.87 (above budget.ask_above_usd $0.50)") and what is committed today.
   Its buttons are **Approve and run** and **Cancel**. Cancel, Escape or closing the dialog runs nothing.
3. After **Approve and run**, the extension reads the plan once more. If its worst case is now higher than what the
   dialog showed (testbench.yaml changed meanwhile), or a limit now refuses it, nothing runs and you are told why.
4. Only then does it run `testbench run PLAN --approve --detach`. testbench still checks every limit:
   `max_run_usd`, the day limits and this machine's `~/.testbench/limits.yaml`.

How this is kept in the code: one function in `src/approval.ts` (`launchApproved`) is the only place the extension
writes `--approve`. It is reached only from the dialog's handler, after the click on **Approve and run** is checked,
and it carries a one-time consent issued by that handler for exactly that launch. The CLI wrapper (`src/cli.ts`)
refuses any argument that looks like `--approve` without such a consent, so no other path can pass it - not a
command, not a setting, not a plan named like the flag. There is no setting to approve automatically, and the tests
check that there is none and that `--approve` appears in that one function only.

An agent working in the same project never approves anything through the extension: it has no way to click the
dialog, and the extension does not listen to agents.

## Install

The extension is not on the Marketplace yet. Build it and install the `.vsix`:

```bash
cd editors/vscode
npm ci                                    # TypeScript and the type definitions (nothing it needs at run time)
npm run compile
npx @vscode/vsce package                  # writes agent-testbench-0.1.0.vsix
code --install-extension agent-testbench-0.1.0.vsix
```

(or in VS Code: Extensions view, `...` menu, **Install from VSIX...**). It needs VS Code 1.90 or newer and
agent-testbench newer than 0.2.0 (the first with `testbench plans --json`): `pip install -U agent-testbench`, or
`uvx agent-testbench` with the setting below. It starts in a folder with a `testbench.yaml` or a `.testbench` folder,
and only in a folder you trust (checking a project runs its code).

## Settings

| setting | default | |
|---|---|---|
| `agentTestbench.command` | `testbench` | how to start testbench: a program on `PATH` or an absolute path, then its arguments - `uvx agent-testbench`, or a list such as `["C:\\Tools\\testbench.exe"]` for a path with spaces. User settings only: a workspace cannot choose the program the extension starts |
| `agentTestbench.runsShown` | `20` | how many recent runs the Runs view lists |
| `agentTestbench.refreshSeconds` | `30` | how often the views and the status bar are read again, besides on changes |
| `agentTestbench.spendWarningPercent` | `80` | the share of a day limit at which the status bar turns to its warning color |

What keeps it safe to have open:

- The command is started directly with an array of arguments, never through a shell, and found on `PATH` without
  looking in the project folder, so a project cannot put a `testbench` of its own in front of yours.
- Plan and run names are checked to be names (letters, digits, `- _ . :`, no leading `-`) and passed after `--`, so a
  name is never read as an option.
- Labels and tooltips are plain text: a plan name or a try's description cannot become a link or a command.
- The extension writes only in its own storage (the schema, and a report page while it opens). It runs plans only
  when you press play or run **Check this project**, and approves only as described above.

## Development

```bash
cd editors/vscode
npm ci
npm test            # type-checks, compiles, and runs the unit tests with node --test (no VS Code is downloaded)
```

The tests cover the CLI wrapper (argument arrays, the PATH lookup, JSON parsing, exit codes), the approval flow, the
views' data, the report page's policy, the schema hand-off and package.json's contributions. One runs the extension
itself against a stand-in for the `vscode` module and a stand-in testbench: the commands it registers, the views,
the status bar, the approval dialog (modal, with the worst case; only **Approve and run** leads to `--approve`) and
a report's webview. Where Python can start this repository's testbench, another runs the flows against the real CLI
on plans that are free or only estimated. To try it in VS Code: `code --extensionDevelopmentPath=$PWD` after `npm run compile`.

Publishing to the Marketplace or Open VSX is the owner's decision; `publisher` in package.json names the account it
would go out under.
