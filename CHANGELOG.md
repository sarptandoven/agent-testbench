# Changelog

## 0.1.2 - 2026-10-07

- Installs on Intel Macs: the `mcp`/`all` extras keep `cryptography` below 49 there (49+ ships no Intel-Mac wheels).
- README: a quick start for Claude Code, MCP clients (no install with `uvx`) and the command line.

## 0.1.1 - 2026-10-07

- Corrected the package summary and the MCP server's instructions.

## 0.1.0 - 2026-10-07

First release.

- Live sessions: a kernel that stays up on this machine, a Modal Sandbox with any GPU, or a Colab VM - run code,
  files or notebook cells, inspect, install, move files, sync; stops itself when idle and at a time limit.
- Plans: notebooks, scripts and web UIs run end to end on local, Modal or Colab, checked against expectations,
  with reports that compare against the previous run.
- Budgets and guardrails, a Claude Code plugin (skills + guard hook), an MCP server, examples and tests.
