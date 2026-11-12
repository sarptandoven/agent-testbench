# Other coding agents

`testbench connect CLIENT` sets agent-testbench up in a coding agent in one command: it registers the MCP server in
the client's own config file and writes the working rules into `AGENTS.md`, the file these agents read.

```
testbench connect --list               # each client: on this machine?, set up in this project?, for the user?
testbench connect cursor               # this project: .cursor/mcp.json and AGENTS.md
testbench connect codex gemini         # several at once
testbench connect vscode --user        # every project of this user: ~/.copilot/mcp-config.json
testbench connect claude --dry-run     # the change as a diff (other entries' values as "..."); nothing is written
```

| client | `testbench connect` | project file | `--user` file | reads the rules from |
|---|---|---|---|---|
| Claude Code | `claude` | `.mcp.json` | `~/.claude.json` | `CLAUDE.md`, which connect makes import `AGENTS.md` |
| Cursor | `cursor` | `.cursor/mcp.json` | `~/.cursor/mcp.json` | `AGENTS.md` |
| VS Code (GitHub Copilot agent mode) | `vscode` | `.vscode/mcp.json` (`servers`) | `$COPILOT_HOME/mcp-config.json`, else `~/.copilot/mcp-config.json` | `AGENTS.md` |
| OpenAI Codex | `codex` | `.codex/config.toml` | `$CODEX_HOME/config.toml`, else `~/.codex/config.toml` | `AGENTS.md` |
| Gemini CLI | `gemini` | `.gemini/settings.json` | `~/.gemini/settings.json` | `AGENTS.md`, once connect adds it to `context.fileName` |

For you rather than the agent, [the VS Code extension](vscode.md) shows the plans, runs, loop and today's spend in
the editor, and asks you before anything that needs approval runs.

The project is the nearest folder at or above this one with a `testbench.yaml`, else this folder: run it in the
folder you open in the client. Without `--user` nothing outside the project is touched; with `--user` the client's
file goes under your home folder, and `AGENTS.md` is still written when you are in a testbench project.

## What it writes, and how

- **The server entry**, named `agent-testbench`. For the server it registers `<python> -m agent_testbench mcp`,
  where `<python>` is the absolute path of the interpreter testbench is installed in (`sys.executable`). That
  starts whatever `PATH` the client passes: an editor opened from the dock gets no shell `PATH`, and Codex gives
  servers only `PATH`, `HOME` and a few others. It works whether testbench came from a virtualenv, `pip install
  --user`, `uv tool install` or `pipx install`, because it does not depend on where the installer put the
  `testbench` script. It is also the interpreter the server runs every tool with. A path to the `testbench`
  script would break in the same situations and gains nothing. When testbench runs from a throwaway environment
  (`uvx`, `pipx run`), that path will not last, so connect registers
  `uvx --from agent-testbench[all]==VERSION testbench mcp` instead. If this interpreter does not have the `mcp`
  package, connect stops and names the command that installs it.
- **A project file names this machine's interpreter.** Committed to git, it will not start on a teammate's machine.
  For a team, have each person run `testbench connect CLIENT --user`, or keep the file out of git. connect and
  `--list` point out an existing entry whose command is not on this machine ("broken"); connect leaves that entry
  for you to remove.
- **Merging, not replacing.** Each file is read and the entry is added as new text. Every other entry stays,
  formatted as it was, along with comments and trailing commas where the format has them (VS Code's `mcp.json`
  is JSONC, and a comment after the last server stays right after it, the new entry below it; a TOML file keeps
  everything byte for byte, with the new table appended). The result has to read back
  as the old content plus exactly the new entry before anything is written. The old file is kept as
  `<file>.bak`, and the new one replaces it in a single step. If a file already has an `agent-testbench` or
  `testbench` entry, or any entry that starts this server, connect says so and leaves the file alone. Running it
  again changes nothing. If a file cannot be read (broken JSON, or a Codex file that sets `mcp_servers` as one
  inline table), connect changes nothing, exits with code 2 and prints the entry to add by hand.
- **The working rules**: a section of `AGENTS.md` between `<!-- agent-testbench:start -->` and
  `<!-- agent-testbench:end -->`. Each run replaces that section, or the one `testbench init` wrote. Nothing else in
  the file is touched, so the section never appears twice. The rules are the short form of the `iterate` skill:
  explore in a session, prove with a plan and read its report, and treat money as the person's decision. That
  means never setting `approved_by_person`, never raising a budget, never editing `~/.testbench/limits.yaml`,
  never touching a ledger, and starting cloud work only through testbench. Secrets are used by name only.

## Guardrails: what holds where

These are in testbench's own code, so they hold in every client, and with no client at all:

- every run, session and loop is priced before it starts (`budget.check`), and refused over `max_run_usd` or the
  day's budget;
- `~/.testbench/limits.yaml` caps every project on the machine; a project can lower those limits, never raise them;
- a hard stop runs on the machine doing the work: Modal's timeouts, or the kernelkit watchdog on Colab VMs, ssh
  machines and containers. It holds even if the client or your laptop goes away;
- above `ask_above_usd` a launch needs a person's yes. The server asks the person directly when the client supports
  MCP elicitation; otherwise the tool refuses and says what needs approval.

The guard hook runs only in Claude Code with the plugin installed. The Cursor plugin manifest declares no hooks,
and connect installs none anywhere. That hook:

- asks before an edit raises a budget in `testbench.yaml`, or changes its `pricing:` or `guard:`, or edits
  `~/.testbench/limits.yaml`;
- refuses edits to the ledgers, raw `modal run` / `colab new` launches, and commands that would print secrets;
- turns an agent's own `approved_by_person=true` (or `--approve`) into a question for the person.

In the other clients, an approval the agent passes is taken at its word. It still cannot go above `max_run_usd`
or the day's limits, because no approval raises those. So keep the client's own confirmation on for `run_plan`,
`session_start` and `loop_start` (for Gemini CLI, never set `trust: true` on the server), and review changes to
`testbench.yaml` like any other diff. connect prints all of this each time it runs.

## Each client

### Claude Code

`.mcp.json` at the project root (`mcpServers`, `"type": "stdio"`); `--user` writes the top-level `mcpServers` of
`~/.claude.json`, the same place `claude mcp add --scope user` writes to. Claude Code asks once before it starts a
server from a project's `.mcp.json`. It reads `AGENTS.md` only when the project has no `CLAUDE.md`, so connect
adds an `@AGENTS.md` import to `CLAUDE.md` in a delimited section, unless `CLAUDE.md` imports it already. The guard
hook and the `iterate` and `setup` skills come with the plugin, not with `.mcp.json`:
`claude plugin marketplace add sarptandoven/agent-testbench && claude plugin install agent-testbench@agent-testbench`.

Checked 2026-11-01: [code.claude.com/docs/en/mcp](https://code.claude.com/docs/en/mcp) (scopes, `.mcp.json`,
`~/.claude.json`) and [code.claude.com/docs/en/memory](https://code.claude.com/docs/en/memory) (`AGENTS.md`, imports).

### Cursor

`.cursor/mcp.json`, or `~/.cursor/mcp.json` with `--user`: `mcpServers` with `command` and `args`. When a
server appears in both, the project's entry wins. Cursor reads `AGENTS.md` at the project root. No hook.

Checked 2026-11-01, indirectly: cursor.com could not be reached from the machine this was written on. The format
and both paths come from search results for [cursor.com/docs/mcp](https://cursor.com/docs/mcp) and
[cursor.com/docs/rules](https://cursor.com/docs/rules) (AGENTS.md). The two paths are also confirmed by VS Code's
MCP reference, which reads both files for its "Cursor" discovery source.

### VS Code (GitHub Copilot agent mode)

`.vscode/mcp.json` with a top-level `servers` object and `"type": "stdio"`. That is VS Code's own format: its
local harness reads it, and VS Code forwards the server to Copilot sessions. With `--user` connect writes Copilot's
user file, `$COPILOT_HOME/mcp-config.json` (else `~/.copilot/mcp-config.json`; top-level `mcpServers`), which
VS Code's guided flow calls "Copilot Global" and the Copilot CLI reads too. VS Code's newer guided flow labels
`.vscode/mcp.json` deprecated but still reads it. The workspace-root `.mcp.json` that `testbench connect claude`
writes is the portable Copilot format; VS Code reads it when the experimental `chat.mcp.workspaceRootConfig.enabled`
setting is on. Servers do not start in Restricted Mode: trust the workspace, run `MCP: List Servers`, and use
Copilot Chat in Agent mode. Copilot reads `AGENTS.md`.

VS Code's Copilot harness can run Claude-style `PreToolUse` hooks from `.claude/settings.json`. connect does not
install testbench's guard there, and the guard has not been tested with VS Code.

Checked 2026-11-01:
[code.visualstudio.com/docs/agents/reference/mcp-configuration](https://code.visualstudio.com/docs/agents/reference/mcp-configuration),
[code.visualstudio.com/docs/agent-customization/mcp-servers](https://code.visualstudio.com/docs/agent-customization/mcp-servers)
and [code.visualstudio.com/docs/agent-customization/custom-instructions](https://code.visualstudio.com/docs/agent-customization/custom-instructions),
read from their source in [microsoft/vscode-docs](https://github.com/microsoft/vscode-docs) (approved 2026-10-28).

### OpenAI Codex

`[mcp_servers.agent-testbench]` with `command`, `args` and `tool_timeout_sec = 900`, appended to
`.codex/config.toml` at the project root, or with `--user` to `$CODEX_HOME/config.toml` (else
`~/.codex/config.toml`). Codex loads a project's `.codex/config.toml` only once the project is trusted. By default
it stops waiting for a tool after 300 seconds, which would cut off a 10-minute `session_exec` (the default
per-cell timeout), hence 900. A run keeps going past any timeout, and `wait_run` picks it up again.

Codex gives a server only `HOME`, `LOGNAME`, `PATH`, `SHELL`, `USER`, `LANG`, `LC_ALL`, `TERM`, `TMPDIR` and `TZ`
from its environment. A secret that a plan or session passes by name has to be listed in the entry, for example
`env_vars = ["HF_TOKEN"]`. Modal and Colab logins are files in your home folder, so they work without this. Codex
reads `AGENTS.md`.

Checked 2026-11-01: [developers.openai.com/codex/mcp](https://developers.openai.com/codex/mcp) could not be reached
from the machine this was written on, so the format was checked against Codex's own source in
[openai/codex](https://github.com/openai/codex):

- `codex-rs/core/config.schema.json` (`mcp_servers`, `RawMcpServerConfig`);
- `codex-rs/config/src/loader/mod.rs` (`${CODEX_HOME}/config.toml`, and the repository's `.codex/config.toml`,
  "loaded but disabled when untrusted");
- `codex-rs/rmcp-client/src/utils.rs` (the variables passed on);
- `codex-rs/codex-mcp/src/rmcp_client.rs` (30-second startup and 300-second tool timeouts).

### Gemini CLI

`.gemini/settings.json`, or `~/.gemini/settings.json` with `--user`: `mcpServers` with `command` and `args`.
Gemini CLI reads only `GEMINI.md` unless `context.fileName` names other files, so connect sets it to the files it
reads now plus `AGENTS.md` (`["GEMINI.md", "AGENTS.md"]` by default). Project settings apply in a trusted folder.
Gemini CLI hides environment variables named like `*KEY*`, `*TOKEN*`, `*SECRET*` or `*PASSWORD*` from servers. A
secret a plan passes by name must be listed in the server's `env`, for example `"HF_TOKEN": "$HF_TOKEN"`. Never add
`"trust": true`, which skips Gemini CLI's confirmation before each tool call.

Checked 2026-11-01:
[docs/tools/mcp-server.md](https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md),
[docs/reference/configuration.md](https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/configuration.md)
and [docs/cli/gemini-md.md](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/gemini-md.md) in
google-gemini/gemini-cli.

### Not supported

- **Windsurf** (now Devin Desktop): its documentation could not be reached from the machine this was written on.
  Search results describe the MCP config moving from `~/.codeium/windsurf/mcp_config.json` to Devin's own files,
  and the editor has no project-level file. connect leaves it out rather than write to a file it may not read. The
  entry is the usual `mcpServers` one; open the file with Cascade's "Open MCP config" and add:

  ```json
  { "mcpServers": { "agent-testbench": { "command": "/path/to/python", "args": ["-m", "agent_testbench", "mcp"] } } }
  ```

  `testbench connect --dry-run cursor` prints the exact `command` for this machine.
- **Any other MCP client** (Claude Desktop, Zed, JetBrains, Cline, opencode, ...): their formats were not checked.
  Register the same command (`/path/to/python -m agent_testbench mcp`, or
  `uvx --from "agent-testbench[all]" testbench mcp`) in the client's own MCP settings, and copy the `AGENTS.md`
  section into whatever rules file it reads.
