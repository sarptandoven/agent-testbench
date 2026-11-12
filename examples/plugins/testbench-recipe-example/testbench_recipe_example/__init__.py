"""testbench-recipe-example: a package that adds one recipe, cli-smoke, to agent-testbench.

The entry point in pyproject.toml names this package; agent-testbench reads the YAML file named like the entry point
(cli-smoke.yaml) from the package's folder, by the rules its own recipes are read by. No code is needed: a recipe is
its YAML text. An entry point can also name a pathlib.Path, YAML text, a mapping or an agent_testbench.recipes.Recipe.
"""
