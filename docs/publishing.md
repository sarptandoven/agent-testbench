# Publishing

For the maintainer: how agent-testbench reaches PyPI, the GitHub Action, the documentation site, the MCP Registry,
Anthropic's plugin directory, the Cursor Marketplace and the Agent Skills CLI. Do the steps in this order: the MCP
Registry reads the README of the release on PyPI, so PyPI comes first.

Each section says what was **verified** against the live documentation or code on 2026-10-31, and what is
**inferred** (not checked, or only checked through search results). agentskills.io and cursor.com could not be
reached from the session that wrote this page; their specifications were read from their GitHub repositories
instead, as each section says.

## Every release: the version, in every place

A release changes the version in all of these, to the same number:

| file | field |
|---|---|
| `pyproject.toml` | `version` |
| `.claude-plugin/plugin.json` | `version` |
| `.claude-plugin/marketplace.json` | `plugins[0].version` |
| `.cursor-plugin/plugin.json` | `version` |
| `server.json` | `version`, `packages[0].version`, and the `==` pin in `packages[0].runtimeArguments[0].value` |
| `.cursor-plugin/mcp.json` | the `==` pin in `args` |
| `CHANGELOG.md` | the heading of the release |

## 1. PyPI

`.github/workflows/release.yml` builds the package, checks it with `twine check --strict` and publishes it with
PyPI trusted publishing (no API token), when a GitHub release is published or when it is run by hand.

1. On pypi.org, project `agent-testbench`, Settings, Publishing: there must be a trusted publisher for repository
   `sarptandoven/agent-testbench`, workflow `release.yml`, environment `pypi`. 0.1.x was published this way, so it
   should already be there (inferred).
2. In `CHANGELOG.md`, replace "0.2.0 - not yet released" with the date.
3. Tag and publish the release from a commit whose README.md has the `mcp-name` comment (section 4); publishing
   it starts the workflow:

   ```bash
   git tag v0.2.0 && git push origin v0.2.0
   gh release create v0.2.0 --title 0.2.0 --notes "See CHANGELOG.md"
   ```

4. Check it: `uvx --from "agent-testbench[all]==0.2.0" testbench --version` prints `agent-testbench 0.2.0`.

Verified: the workflow (read), and that a build of this tree passes `twine check --strict` with the README's
`mcp-name` comment in the package description. Inferred: the PyPI-side settings.

## 2. The GitHub Action: tag `v1`

`uses: sarptandoven/agent-testbench@v1` needs a tag named `v1`. Point it at the release, and move it with each
release that stays compatible:

```bash
git tag -f v1 "v0.2.0^{commit}" && git push -f origin v1
```

To list the action on the GitHub Marketplace, tick "Publish this Action to the GitHub Marketplace" when drafting a
release; `action.yml` already has a name, a description and `branding` (inferred from GitHub's documentation, not
checked today).

## 3. The documentation site

`.github/workflows/docs.yml` runs `mkdocs build --strict` on every push and pull request (a broken link, a missing
anchor or a page left out of the nav fails it) and, on pushes to `main`, deploys the site to GitHub Pages with
`actions/upload-pages-artifact` and `actions/deploy-pages`.

1. In the repository: Settings, Pages, Build and deployment, Source: **GitHub Actions**. Until then the deploy job
   fails (inferred).
2. Push to `main` (or re-run the docs workflow). The site is at <https://sarptandoven.github.io/agent-testbench/>
   (`site_url` in `mkdocs.yml`; change it there for a custom domain).
3. To read it locally: `pip install -r docs/requirements.txt && mkdocs serve`.

The pages are the repository's own Markdown: `docs/index.md` is README.md, and the Loops, Proof records, ssh and
Plugins pages take their sections from README.md and docs/reference.md. `docs/hooks.py` does this at build time and
points GitHub links at the site's pages, so edit README.md and docs/reference.md, never a copy.

Material for MkDocs reaches end of life on 2027-05-05; its build says so, and names Zensical, from the same team,
as its successor (verified: printed by mkdocs-material 9.8.0). The workflow hides that banner; plan the move
before then.

## 4. The MCP Registry

Verified against the registry's documentation (github.com/modelcontextprotocol/registry, `docs/`), its PyPI
ownership check (`internal/validators/registries/pypi.go`) and the live API, which serves `server.json` files with
the `2025-12-11` schema. The registry is in preview: "Breaking changes or data resets may occur before general
availability."

- `server.json` (repository root) names the server `io.github.sarptandoven/agent-testbench`, the PyPI package
  `agent-testbench` 0.2.0 and stdio. It validates against
  <https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json>.
- Ownership: the registry fetches `https://pypi.org/pypi/agent-testbench/0.2.0/json` and looks for
  `mcp-name: io.github.sarptandoven/agent-testbench` in the description, which is the README. The README has it
  as an HTML comment on its last line. The release must be on PyPI before publishing to the registry.
- A GitHub login grants the `io.github.<your user>/*` namespace, so log in as `sarptandoven`.

Steps:

1. Install the publisher: `brew install mcp-publisher`, or the binary from
   <https://github.com/modelcontextprotocol/registry/releases>.
2. In the repository root: `mcp-publisher validate`, then `mcp-publisher login github` (it prints a code to enter
   at <https://github.com/login/device>), then `mcp-publisher publish`.
3. Check: `curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.sarptandoven/agent-testbench"`.

What a client runs from this entry: VS Code builds `uvx [runtimeArguments] <identifier>@<version>
[packageArguments]` (read in its source, `src/vs/platform/mcp/common/mcpManagementService.ts`), which here is
`uvx --with agent-testbench[all]==0.2.0 agent-testbench@0.2.0 mcp`. That command was run against a local build of
0.2.0: the server started and listed its 21 tools. The `--with` brings in the `all` extra, which has the MCP
server; the command is `agent-testbench`, the package's second entry point, because clients put the package name
there. `registryBaseUrl` is left out on purpose: the registry fills in `https://pypi.org` (verified), and VS Code
would pass a set value to uvx as `--index-url https://pypi.org`, which is not a package index (inferred from the
code; not run).

To publish from CI later, the registry documents a workflow that logs in with `mcp-publisher login github-oidc`
(`docs/modelcontextprotocol-io/github-actions.mdx` in its repository).

## 5. Anthropic's plugin directory (Claude Code)

Verified against <https://claude.com/docs/directory/publish> and the plugin pre-submission checklist
(<https://claude.com/docs/plugins/pre-submission-checklist>).

1. Add an icon first: a square PNG, 512 to 2048 px on each side and under 2 MB, at `.claude-plugin/icon.png` (or
   `icon` in `.claude-plugin/plugin.json`). `claude plugin validate .` warns `ICON_MISSING` today, and the icon is
   taken only the first time the plugin is saved or submitted in the portal.
2. Run `claude plugin validate . && claude plugin validate .claude-plugin/plugin.json --strict` (both pass today).
3. From a claude.ai account on Pro, Max, Ultra, Team or Enterprise (on Team and Enterprise, an Owner), open
   <https://claude.ai/directory/manage>, select **Submit new**, choose **Plugin bundle**, enter the repository (the
   plugin folder is the repository root; it must be public before the listing goes live), select **Validate**, fix
   anything marked Blocking, then submit for review.
4. After it is listed, merges to the tracked branch are picked up, scanned and published; raise `version` each
   release.

What the checklist looks at here: a README of at least 40 words and a license (both there); the guard hook runs
`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/guard.py"` and the README describes it, as the security scan wants; the
repository has about 120 files, the largest about 40 KB, well under the limits that hold a version for a reviewer.
The Claude Code plugin starts no MCP server; the Cursor plugin's `uvx` command is pinned to an exact version all
the same, as the checklist asks of launchers.
Install from this repository's own marketplace keeps working as before
(`claude plugin marketplace add sarptandoven/agent-testbench`).

## 6. The Cursor Marketplace

In the repository:

- `.cursor-plugin/plugin.json`: the manifest, with the same name and version as `.claude-plugin/plugin.json`.
- `skills/`: the same two skills.
- `.cursor-plugin/mcp.json`: the MCP server, with the README's command pinned to the release
  (`uvx --from "agent-testbench[all]==0.2.0" testbench mcp`), so the server matches the plugin's skills.
- `.cursor-plugin/hooks.json`: an empty hooks file, on purpose. Cursor reads `hooks/hooks.json` by default, and that
  file is the Claude Code guard hook in Claude Code's format; `hooks/guard.py` reads and answers only Claude Code's
  hook messages. Pointing `hooks` at an empty file keeps Cursor from loading it, so the Cursor plugin has no guard
  hook. Giving Cursor one needs `guard.py` to speak Cursor's hook format.

Verified: the manifest validates against Cursor's published schema (`schemas/plugin.schema.json` in
github.com/cursor/plugins, `$id` <https://cursor.com/schemas/cursor-plugin/plugin.json>); the `mcp.json` and
`hooks.json` shapes and path-valued `skills`, `mcpServers` and `hooks` match Cursor's official plugins there,
which also keep `.claude-plugin` and `.cursor-plugin` manifests side by side with equal versions. Inferred (from
cursor.com's documentation as quoted by search results): that a `hooks` field replaces the default
`hooks/hooks.json`, and the submission route below.

Steps:

1. Try it locally: clone the repository to `~/.cursor/plugins/local/agent-testbench/` (where Cursor's own
   create-plugin skill puts plugins under development), restart Cursor, and check that the two skills and the
   `agent-testbench` MCP server appear and that the plugin shows no hooks.
2. Submit the public repository at <https://cursor.com/marketplace/publish> while signed in. Every plugin is
   reviewed by a person before it is listed; there is no confirmation email, and submitting twice only makes a
   duplicate.

Without the marketplace, the MCP config in the README works in Cursor today.

## 7. Agent Skills (`npx skills add`)

Nothing to submit: `npx skills add sarptandoven/agent-testbench` reads the public repository and finds
`skills/iterate` and `skills/setup` (it searches `skills/`; verified in the skills CLI's documentation and source,
github.com/vercel-labs/skills; the CLI needs Node.js 22.20 or later). The skills need the `testbench` command,
which the iterate skill tells the agent to install.

Both skills' frontmatter fits the Agent Skills specification (github.com/agentskills/agentskills,
`docs/specification.mdx`, and its reference validator `skills-ref`): the name is the folder's name in lowercase,
the description is under 1024 characters, and there are no other fields. `argument-hint` was removed from both:
it is a Claude Code addition that `skills-ref` rejects, and Claude Code's documentation says fields outside the
specification fail an upload to claude.ai or the Skills API. The one loss is the argument hint after
`/agent-testbench:iterate` in Claude Code's menu.
