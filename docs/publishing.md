# Publishing

For the maintainer: how agent-testbench reaches PyPI, the GitHub Action, the documentation site, the MCP Registry,
Anthropic's plugin directory, the Cursor Marketplace and the Agent Skills CLI. Do the steps in this order: the MCP
Registry reads the README of the release on PyPI, so PyPI comes first.

Each section says what was **verified** against the live documentation or code on 2026-10-31, and what is
**inferred** (not checked, or only checked through search results). agentskills.io and cursor.com could not be
reached from the session that wrote this page; their specifications were read from their GitHub repositories
instead, as each section says. Sections 1 to 4 were checked again for 0.2.0 on 2026-11-01; the
[release checklist](#release-checklist) at the end says what that covered and lists the owner's steps in order.

## Every release: the version, in every place

A release changes the version in all of these, to the same number. `tests/test_release.py` fails when one of them
differs from `pyproject.toml`, and the release workflow runs it with the release's tag:

| file | field |
|---|---|
| `pyproject.toml` | `version` |
| `src/agent_testbench/__init__.py` | `__version__` |
| `.claude-plugin/plugin.json` | `version` |
| `.claude-plugin/marketplace.json` | `plugins[0].version` |
| `.cursor-plugin/plugin.json` | `version` |
| `server.json` | `version`, `packages[0].version`, and the `==` pin in `packages[0].runtimeArguments[0].value` |
| `.cursor-plugin/mcp.json` | the `==` pin in `args` |
| `CHANGELOG.md` | the heading of the release |

## 1. PyPI

`.github/workflows/release.yml` runs when a GitHub release is published, and on nothing else: no push, no tag on
its own, no run by hand (0.1.x's workflow could also be started by hand, from any branch). Its `build` job checks
out the release's tag, runs `tests/test_release.py` with that tag (the tag must be `v` and the version, every file
in the table above must name that version, and the changelog heading must have a date), builds the sdist and the
wheel, checks them with `twine check --strict`, and checks that the wheel's description still has the `mcp-name`
line. Its `publish` job, in the GitHub environment `pypi` and with no permission but `id-token: write`, uploads
them with PyPI trusted publishing (no API token anywhere) and the attestations PyPI shows. Every action is pinned
to a full commit SHA, with its tag in a comment.

Set up once, before the first release from this workflow:

1. On pypi.org, project `agent-testbench`, Manage, Publishing: a GitHub trusted publisher with owner
   `sarptandoven`, repository `agent-testbench`, workflow `release.yml` and environment `pypi`. It is there today:
   PyPI's provenance for 0.1.2
   (`https://pypi.org/integrity/agent-testbench/0.1.2/agent_testbench-0.1.2-py3-none-any.whl/provenance`) names
   exactly that publisher (verified 2026-11-01). Renaming the workflow file or the environment breaks it until the
   publisher on PyPI is changed to match.
2. In the repository, Settings, Environments, `pypi` (GitHub made it for 0.1.x): add yourself as a required
   reviewer, so an upload waits for your yes. PyPI's documentation strongly recommends such an environment
   (inferred: the repository's environments could not be read from the session that checked this).

Each release:

1. Merge the release into `main` first. The PyPI page's links and its image point at files on `main` (below), and
   until 0.2.0 is merged, `main` has no docs/tour.md and the other new pages (checked 2026-11-01: 404).
2. In `CHANGELOG.md`, replace "0.2.0 - not yet released" with the date, as `## 0.2.0 - YYYY-MM-DD`; the workflow
   refuses a release whose heading has no date.
3. Tag that commit of `main` and publish a release from the tag; publishing it starts the workflow:

   ```bash
   git tag v0.2.0 && git push origin v0.2.0
   gh release create v0.2.0 --verify-tag --title 0.2.0 --notes "See CHANGELOG.md"
   ```

   To list the action on the GitHub Marketplace in the same step, draft the release on github.com instead
   (section 2): `gh release create` has no Marketplace option.
4. In the run (Actions, release), approve the `pypi` deployment if the environment has a reviewer.
5. Check it: `uvx --from "agent-testbench[all]==0.2.0" testbench --version` prints `agent-testbench 0.2.0`.

If the `build` job fails, nothing reached PyPI: fix it on `main`, delete the release and the tag
(`gh release delete v0.2.0 --cleanup-tag`), and publish again. Once a file is on PyPI, that version can never be
uploaded again, even after deleting it there; the next try needs a new version.

**The description on PyPI.** README.md's links are relative (`docs/tour.md`), which works on GitHub and on the
documentation site but would point at pypi.org. `pyproject.toml` builds the package description with
hatch-fancy-pypi-readme, whose two substitutions turn each relative link into one to that file (`blob/main/...`)
or folder (`tree/main/...`) on GitHub; README.md itself stays as it is. `tests/test_release.py` checks that no
relative link is left, that each one names a file or folder (and heading) of the repository, and that the
`mcp-name` line is still there. The README's image is already an absolute `raw.githubusercontent.com` URL.

Verified 2026-11-01: a build of this tree (sdist and wheel, hatchling 1.33.0) passes `twine check --strict`; its
description has 54 links, all absolute, and the `mcp-name` line. PyPI accepts the wheel's Metadata-Version 2.5
(0.1.2's wheel on PyPI has it too). The workflow's test step was run as the runner does it (a fresh Python 3.12 with
only pytest and pyyaml, `TESTBENCH_RELEASE_TAG=v0.2.0`): it passes with a dated changelog, and fails, naming the
file, when one manifest is left at another version. Not run: the workflow itself on GitHub, and actionlint or
zizmor on it.

## 2. The GitHub Action: tag `v1`

`uses: sarptandoven/agent-testbench@v1` needs a tag named `v1`. Point it at the release, and move it with each
release that stays compatible:

```bash
git tag -f v1 "v0.2.0^{commit}" && git push -f origin v1
```

There is no `v1` tag yet (checked 2026-11-01: the tags are v0.1.0, v0.1.1 and v0.1.2), so every
`uses: sarptandoven/agent-testbench@v1` in README.md and docs/github-action.md fails until it is pushed. The action
is new in 0.2.0, so `v1` starts at `v0.2.0`.

To list the action on the GitHub Marketplace, draft the release on github.com (the banner on `action.yml`, or
Releases, Draft a new release) and tick "Publish this Action to the GitHub Marketplace"; the first time, the account
must accept the Marketplace Developer Agreement, and publishing needs two-factor authentication. The listing needs
a public repository with one `action.yml` at its root and a `name` no other listing, GitHub user or organization
has; `action.yml` has `name: agent-testbench`, a description and `branding`, and the form says whether the name is
free (verified against GitHub's documentation source, github.com/github/docs,
`content/actions/how-tos/create-and-publish-actions/publish-in-github-marketplace.md`).

## 3. The documentation site

`.github/workflows/docs.yml` runs `mkdocs build --strict` on every push and pull request (a broken link, a missing
anchor or a page left out of the nav fails it) and, on pushes to `main`, deploys the site to GitHub Pages with
`actions/upload-pages-artifact` and `actions/deploy-pages`.

1. In the repository: Settings, Pages, Build and deployment, Source: **GitHub Actions**. Until then the deploy job
   fails (inferred). Pages is not on yet (checked 2026-11-01), and the package's Documentation link on PyPI
   points at the site, so do this before the release.
2. Push to `main` (or re-run the docs workflow). The site is at <https://sarptandoven.github.io/agent-testbench/>
   (`site_url` in `mkdocs.yml`; change it there for a custom domain).
3. To read it locally: `pip install -r docs/requirements.txt && mkdocs serve`.

The pages are the repository's own Markdown: `docs/index.md` is a landing screen of its own followed by README.md's
sections, and the Loops, Proof records, ssh and Plugins pages take their sections from README.md and
docs/reference.md. `docs/hooks.py` does this at build time and points GitHub links at the site's pages (a link to
README.md goes to the home page), so edit README.md and docs/reference.md, never a copy. A new section of README.md
needs two lines in `docs/index.md` to appear on the site.

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
0.2.0: the server started and listed its 25 tools. The `--with` brings in the `all` extra, which has the MCP
server; the command is `agent-testbench`, the package's second entry point, because clients put the package name
there. `registryBaseUrl` is left out on purpose: the registry fills in `https://pypi.org` (verified), and VS Code
would pass a set value to uvx as `--index-url https://pypi.org`, which is not a package index (inferred from the
code; not run).

To publish from CI later, the registry documents a workflow that logs in with `mcp-publisher login github-oidc`
(`docs/modelcontextprotocol-io/github-actions.mdx` in its repository).

Checked again on 2026-11-01: `server.json` validates against the 2025-12-11 schema (jsonschema, Draft 7, no
errors); that is still the newest released schema (the schema changelog has only a draft after it) and the one
every entry the live API listed uses; the registry has no entry for `io.github.sarptandoven/agent-testbench` yet;
the publisher's commands (`init`, `login`, `logout`, `publish`, `status`, `validate`) and its install routes are as
the registry's quickstart gives them. The four ways a client starts this server - the `server.json` command above,
the Cursor plugin's `.cursor-plugin/mcp.json`, the README's unpinned `uvx --from "agent-testbench[all]" testbench mcp`
and `uv tool install "agent-testbench[all]==0.2.0"` - each answered an MCP `initialize` and listed 25 tools from a
fresh HOME, with uv finding 0.2.0 in a local build (`UV_FIND_LINKS`) instead of on PyPI. `mcp-publisher` itself was
not run.

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

## Release checklist

### Verified for 0.2.0 on 2026-11-01

From commit `59372e6` of the release branch plus this page's commit, on Linux, without publishing anything:

- **Build.** `python -m build` (hatchling 1.33.0) makes `agent_testbench-0.2.0.tar.gz` (about 320 KB) and
  `agent_testbench-0.2.0-py3-none-any.whl` (about 370 KB, 1.4 MB unpacked); `twine check --strict` passes both.
  The wheel holds exactly the git-tracked files of `src/agent_testbench` (recipes, mock_clients, schema,
  kernelkit, colab_shim) and of the four examples `testbench demo` copies (tiny-model, llm-app, web-app,
  test-suite); the sdist adds README.md, LICENSE, pyproject.toml and .gitignore, and no tests, docs or caches.
  `pricing/catalog.json` ships in neither: the prices are built into the code, and `testbench prices --update`
  fetches the catalog from GitHub. A build from a folder with `.env` files, `.testbench/`, `outputs/` and
  caches in it ships none of them (`.env` files did ship before `[tool.hatch.build] exclude` in pyproject.toml).
- **Install,** with pip (its resolver, then `pip check`) in a fresh venv and a fresh HOME for each Python and
  extra: Python 3.10, 3.11, 3.12, 3.13 and 3.14, each bare and with `[all]`, `[mcp]`, `[web]`, `[mlflow]`,
  `[wandb]`, `[modal]` and `[dev]` (pip 23.0 to 26.2, as each Python's venv comes): every install resolves with
  no conflict (mcp 2.3.0, modal 1.6.3, playwright 1.64.1, mlflow 3.17.2, wandb 0.31.0), and `testbench
  --version` and `agent-testbench --version` print 0.2.0, and `from agent_testbench import Testbench, tools`
  imports, in every one. Bare: `doctor`, `recipes`, `demo --list` and `demo llm-app` (passes) work; `demo`,
  `demo test-suite` and `demo web-app` say which packages to install, and do their job once they are there
  (tiny-model passes, test-suite fails on purpose). `[all]`, `[mcp]` and `[dev]`: `testbench mcp` answers
  `initialize` and lists 25 tools, and `tools.anthropic_tools()` gives 25. `[web]` and `[all]`: `demo web-app`
  passes (with playwright 1.63.0, the release whose Chromium this machine had; 1.64.1 asks for
  `playwright install chromium`, as it should). `[mlflow]` and `[wandb]`: `export --to mlflow` (sqlite store)
  and `--to wandb` (offline) work. `[modal]`: `doctor` finds modal (no Modal call was made). Found, not fixed
  here: on Python 3.14, `testbench demo` asks for `scikit-learn==1.5.2 matplotlib==3.9.2`, which have no 3.14
  wheels, so pip builds them from source (matplotlib's build failed on this machine); with today's releases
  (scikit-learn 1.9.1, matplotlib 3.11.2) the demo passes on 3.14. And without the `mcp` extra, `testbench mcp`
  stops with a `ModuleNotFoundError` traceback instead of saying `pip install "agent-testbench[mcp]"`.
- **MCP.** The four ways a client starts the server (section 4) answer `initialize` and list 25 tools.
- **Versions.** 0.2.0 in all the places in the table at the top; `tests/test_release.py` keeps them equal.
- **PyPI description.** No relative link left, each target a file or folder of the repository, the `mcp-name`
  line kept (section 1).
- **Docs.** `mkdocs build --strict` passes with the versions pinned in docs/requirements.txt (mkdocs 1.6.1,
  mkdocs-material 9.8.0, pymdown-extensions 12.3, Markdown 3.11.2).
- **Plugin.** `claude plugin validate .` and `claude plugin validate .claude-plugin/plugin.json --strict` pass
  (Claude Code 2.1.316), with the one warning ICON_MISSING. The Claude Code plugin starts no MCP server; its
  guard hook, run as Claude Code runs it from a fresh HOME, refuses `modal run` in a project and turns
  `testbench run --approve` into a question with the plan's worst case.
- **Registry.** `server.json` validates against its schema (section 4).
- **Release workflow.** Runs only on a published release, actions pinned to commit SHAs, its test step tried as
  the runner runs it (section 1). PyPI's trusted publisher for this workflow and environment exists (0.1.2's
  provenance).

### The owner's steps, in order

Only the owner can do these; nothing here was published.

1. Turn on GitHub Pages: Settings, Pages, Source **GitHub Actions** (section 3), so the docs deploy that runs on
   the merge works and the package's Documentation link has a site behind it.
2. Optional: Settings, Environments, `pypi`, add yourself as a required reviewer (section 1).
3. Merge the release branch into `main`, then on `main` date the changelog (`## 0.2.0 - YYYY-MM-DD`) and push.
   Wait for the tests, action and docs workflows to pass on that commit.
4. Tag and release: `git tag v0.2.0 && git push origin v0.2.0`, then publish the release - on github.com with
   "Publish this Action to the GitHub Marketplace" ticked to list the action (section 2), or
   `gh release create v0.2.0 --verify-tag --title 0.2.0 --notes "See CHANGELOG.md"`.
5. In the release workflow's run, approve the `pypi` deployment (if step 2), and see it finish. Then
   `uvx --from "agent-testbench[all]==0.2.0" testbench --version` prints `agent-testbench 0.2.0`, and
   <https://pypi.org/project/agent-testbench/> shows the README with working links and its image.
6. Point the action's major tag at it: `git tag v1 "v0.2.0^{commit}" && git push origin v1` (later releases:
   `git tag -f` and `git push -f`, section 2).
7. MCP Registry (section 4): `mcp-publisher validate`, `mcp-publisher login github` as `sarptandoven`,
   `mcp-publisher publish`, then the `curl` check.
8. Anthropic's plugin directory (section 5): add `.claude-plugin/icon.png` first (it is taken only once), then
   submit at <https://claude.ai/directory/manage>.
9. Cursor Marketplace (section 6): try the plugin locally, then submit at <https://cursor.com/marketplace/publish>.
10. Agent Skills (section 7): nothing to do once the repository is public.
