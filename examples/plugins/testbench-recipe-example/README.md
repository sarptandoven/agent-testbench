# testbench-recipe-example

A package that adds one recipe to agent-testbench: `cli-smoke`, a command-line tool that starts and turns a small
sample input into the output it should. The recipe is a YAML file in the package; one entry point names the package.

```
pip install ./examples/plugins/testbench-recipe-example
testbench recipes                       # lists cli-smoke, "from testbench-recipe-example"
testbench recipes cli-smoke             # prints it, FILL IN lines and all
testbench init --recipe cli-smoke       # a new testbench.yaml from it
testbench init --add --recipe cli-smoke # its plan added to the testbench.yaml you have
```

Before you publish one, `testbench plugin test` checks it against the recipe contract (from Python:
`agent_testbench.plugins.check("recipes", recipe)`, `[]` when it keeps it).

How it is wired (`pyproject.toml`):

```toml
[project.entry-points."agent_testbench.recipes"]
cli-smoke = "testbench_recipe_example"      # reads cli-smoke.yaml from the package's folder
```

A recipe from a package is read by the rules agent-testbench's own recipes are read by: sections among `sessions:`,
`modal:`, `colab:`, `docker:`, `ssh:`, then `plans:`; never `budget:`, `pricing:` or `guard:` (a recipe that sets one
does not load, and `testbench recipes` says why). Its header says what it proves, what to FILL IN and what it costs,
and each value only the person knows is marked `# FILL IN`. See [docs/plugins-recipes.md](../../../docs/plugins-recipes.md).
