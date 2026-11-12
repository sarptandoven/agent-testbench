# Recipe plug-ins

A [recipe](recipes.md) is testbench.yaml for a goal: a ladder of plans with their checks and `FILL IN` lines. The 14
that ship are YAML files in `src/agent_testbench/recipes/`; a package adds more, and they work everywhere the built-in
ones do: `testbench recipes` lists them (with the package each comes from), `testbench recipes NAME` prints one,
`testbench init --recipe NAME` and `testbench init --add --recipe NAME` write them, and so does the MCP `recipes` tool.

## A package that ships one

[examples/plugins/testbench-recipe-example](https://github.com/sarptandoven/agent-testbench/tree/main/examples/plugins/testbench-recipe-example)
is the whole of it: a YAML file in the package, and one entry point that names the package.

```toml
# pyproject.toml
[project.entry-points."agent_testbench.recipes"]
cli-smoke = "testbench_recipe_example"      # reads cli-smoke.yaml from the package's folder
```

```
$ pip install ./examples/plugins/testbench-recipe-example
$ testbench recipes
...
cli-smoke (local) - from testbench-recipe-example
  a command-line tool starts, and turns a small sample input into the output it should.
    cli-smoke           local, up to 7 min, free
$ testbench init --recipe cli-smoke
Wrote testbench.yaml from recipe cli-smoke:
  plan cli-smoke: local, up to 7 min, free (recipe cli-smoke, from testbench-recipe-example)
```

An entry point can name any of these, all read the same way:

| the entry point names | read as |
|---|---|
| a package or module (`"my_pkg"`) | the file `NAME.yaml` in its folder, NAME being the entry point's name |
| a `pathlib.Path` (`"my_pkg:RECIPE"`, `RECIPE = Path(__file__).with_name("x.yaml")`) | that YAML file |
| a string with a newline in it | the YAML text itself |
| a mapping | `recipes.from_mapping` (below) |
| an `agent_testbench.recipes.Recipe` | `Recipe(name, text, path)`, as is |

## What a recipe holds

The same as a built-in one, and the same rules hold for it, checked in code when it loads:

- **A header comment.** Its first line is `# Recipe NAME: what it is for` (the line `testbench recipes` shows), then
  `Proves:` (what passing shows), `FILL IN:` (what only the person knows) and `Costs:` (what it can cost).
- **Sections, then plans.** Any of `sessions:`, `modal:`, `colab:`, `docker:`, `ssh:` (each once), then `plans:`
  with at least one plan; each top-level key unquoted at the start of its line and everything under it indented, so
  the recipe can be added to a testbench.yaml as text, comments and all.
- **Never `budget:`, `pricing:` or `guard:`.** They stay the person's. A recipe that sets one does not load:
  `testbench recipes` lists it under "Installed but not loaded" with the reason, and nothing from it is ever written.
- **`# FILL IN` on each value only the person knows**: a command's arguments, a path, the number that means
  working. A recipe with nothing to fill in says `FILL IN: nothing` in its header.

A plan name a recipe shares with the testbench.yaml it is added to (or with another recipe in the same `init`) is
refused with what to do; a built-in recipe's name cannot be taken by a plug-in (it is skipped and reported).

## The contract

`testbench plugin test` checks a recipe against `recipes.contract` before anyone installs it; from Python,
`plugins.check("recipes", recipe)` returns the problems in plain words (`[]` when it keeps the contract):

1. It reads by the rules above (`recipes.parse`).
2. Its header has the first line, `Proves:`, `FILL IN:` and `Costs:`, and its lines to fill in are marked.
3. Its plans load through config under the header `testbench init` writes, as `init --recipe` checks them (the
   notebooks it names stand in for the project's own).
4. Every plan's worst case is a number of dollars within the default `budget.max_run_usd` ($2.00), so nothing in it
   is refused as it ships.

The 14 built-in recipes keep it (tests/test_recipes_plugins.py checks each).

## A recipe made in Python

`recipes.from_mapping` writes a recipe's YAML text from a mapping, for a package that builds recipes in code. A
mapping holds no comments, so the header and the `FILL IN` marks are keys of their own:

```python
RECIPE = {
    "summary": "a command-line tool answers --help",
    "proves": "the tool installs and starts",
    "fill_in": "the tool's command",
    "costs": "nothing; it runs on this machine",
    "fill_in_at": {"plans.cli.steps.0.run": "your tool's command"},     # a `# FILL IN:` comment on that line
    "plans": {"cli": {"backend": "local", "description": "the tool starts", "steps": [
        {"name": "help", "run": "mytool --help", "max_minutes": 2, "expect": {"text": {"stdout": ["usage:"]}}}]}},
}
```

```toml
[project.entry-points."agent_testbench.recipes"]
cli-help = "my_pkg:RECIPE"
```
