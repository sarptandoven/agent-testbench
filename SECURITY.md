# Security

Please report security problems privately through GitHub's
[private vulnerability reporting](https://github.com/sarptandoven/agent-testbench/security/advisories/new),
not in a public issue.

agent-testbench runs code you point it at, on your machine or on cloud machines in your own accounts. Things worth
knowing:

- It never asks for or stores secret values; plans and sessions refer to secrets by name, and values come from
  your environment, Modal secrets or Colab.
- The Claude Code guard hook is a safety net for agents (blocking raw cloud launches and commands that would
  print secrets), not a sandbox. Run agents with the permissions you are comfortable with.
- Budget limits are checked before every run and session, and remote time limits are enforced by Modal itself;
  estimates use published prices, so keep your provider's own spend limit set as well.
