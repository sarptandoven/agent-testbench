"""The core stays small: `import agent_testbench.core` loads no layer of the package (CLI, MCP server, notebooks,
sessions, remote backends, exports, ...) and no optional or heavy package; no core module imports one at module level
(imports inside a function are how the core reaches a layer when it needs one); and a plan of plain `run:` steps is
priced, launched, driven, checked and verified through core's functions without the notebook machinery."""
import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import agent_testbench
from agent_testbench import core
from conftest import write_config

PACKAGE = Path(agent_testbench.__file__).parent
# optional extras, and what only notebook steps, live sessions, web steps or trackers need
HEAVY = ("mcp", "modal", "playwright", "mlflow", "wandb", "kubernetes", "papermill", "numpy", "pandas", "nbclient",
         "ipykernel", "jupyter_client", "nbformat", "jupytext", "IPython", "PIL", "pydantic")
# ceilings for `import agent_testbench.core` and the core modules in a fresh interpreter. Measured on Linux: 121
# modules and about 40 ms on Python 3.13, up to about 160 modules on 3.10 to 3.14. The count is the real bound
# (nbformat alone adds about 150); the time only catches something gone badly wrong, with room for slow CI machines.
MAX_NEW_MODULES = 200
MAX_IMPORT_SECONDS = 3.0

PROBE = """
import importlib, json, sys, time
before = set(sys.modules)
started = time.perf_counter()
import agent_testbench.core as core
seconds = time.perf_counter() - started
for name in core.CORE_MODULES:
    importlib.import_module(name)
print(json.dumps({"seconds": seconds, "loaded": sorted(set(sys.modules) - before)}))
"""


def package_modules() -> list[str]:
    """Every module of the package, by name (folders without __init__.py, such as mock_clients, are not part of it)."""
    out = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE.parent)
        if all((PACKAGE.parent / Path(*rel.parts[:i]) / "__init__.py").exists() for i in range(1, len(rel.parts))):
            out.append(".".join(rel.with_suffix("").parts).removesuffix(".__init__"))
    return out


def is_layer(name: str) -> bool:
    return name.split(".")[0] == "agent_testbench" and name not in core.CORE_MODULES


def third_party(top: str) -> bool:
    return top not in sys.stdlib_module_names and top not in ("__future__", "agent_testbench", *core.CORE_REQUIRES)


def pyyaml_c_loader(top: str) -> bool:
    """What PyYAML's C loader (built with Cython) adds to sys.modules, when it is installed."""
    return top in ("_yaml", "cython_runtime") or top.startswith("_cython")


def top_level_imports(path: Path, module: str) -> list[tuple[int, str]]:
    """(line, module) for each import that runs when the module is imported: anything not inside a function. For
    `from X import name`, X.name when that is a module of the package, else X."""
    known = set(package_modules())
    package = (module if path.name == "__init__.py" else module.rpartition(".")[0]).split(".")
    found = []

    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue                                         # runs when called, not on import
            if isinstance(child, ast.Import):
                found.extend((child.lineno, a.name) for a in child.names)
            elif isinstance(child, ast.ImportFrom):
                base = child.module or ""
                if child.level:                                  # from . import x, from ..config import y
                    base = ".".join(package[:len(package) - child.level + 1] + ([base] if base else []))
                for a in child.names:
                    sub = f"{base}.{a.name}"
                    found.append((child.lineno, sub if sub in known else base))
            visit(child)

    visit(ast.parse(path.read_text(encoding="utf-8")))
    return found


def test_the_lists_name_modules_that_exist_and_do_not_overlap():
    modules = set(package_modules())
    assert set(core.CORE_MODULES) <= modules, f"core.CORE_MODULES: no module {set(core.CORE_MODULES) - modules}"
    for name in core.LAYER_MODULES:
        assert any(m == name or m.startswith(name + ".") for m in modules), f"core.LAYER_MODULES: no module {name}"
        assert not any(c == name or c.startswith(name + ".") for c in core.CORE_MODULES), f"{name} is core and a layer"


def test_no_core_module_imports_a_layer_or_a_third_party_package_at_module_level():
    wrong = []
    for name in core.CORE_MODULES:
        path = Path(importlib.util.find_spec(name).origin)
        for line, imported in top_level_imports(path, name):
            if is_layer(imported) or third_party(imported.split(".")[0]):
                wrong.append(f"{path.relative_to(PACKAGE.parent)}:{line} imports {imported}")
    assert not wrong, ("a core module imports a layer or a third-party package when it is imported. Move each import "
                       "into the function that uses it (core.py says why):\n  " + "\n  ".join(wrong))


def test_importing_the_core_loads_no_layer_and_no_optional_package():
    out = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    probe = json.loads(out.stdout)
    loaded = probe["loaded"]
    layers = [m for m in loaded if is_layer(m)]
    assert not layers, (f"importing the core loaded layers of the package: {layers}. Find the core module that "
                        "imports one at module level (test above) and move the import into the function that uses it")
    tops = {m.split(".")[0] for m in loaded}
    assert not tops & set(HEAVY), f"importing the core loaded optional or heavy packages: {sorted(tops & set(HEAVY))}"
    others = sorted(t for t in tops if third_party(t) and not pyyaml_c_loader(t))
    assert not others, f"importing the core loaded packages beyond the standard library and PyYAML: {others}"
    assert len(loaded) <= MAX_NEW_MODULES, (f"importing the core loaded {len(loaded)} modules (ceiling "
                                            f"{MAX_NEW_MODULES}); something it imports pulls in a lot: {loaded}")
    assert probe["seconds"] <= MAX_IMPORT_SECONDS, f"import agent_testbench.core took {probe['seconds']:.2f} s"


RUN = """
import json, sys
from agent_testbench import core
root = core.find_root(sys.argv[1])
cfg = core.load(root)
est = core.estimate(cfg, "plain")
run_dir = core.launch(root, cfg, "plain", foreground=True)
state = core.wait(run_dir, timeout=60)
print(json.dumps({"plans": core.plans(cfg), "estimate": est, "run_dir": str(run_dir), "state": state,
                  "report": core.report(run_dir), "problems": core.verify(core.find_run(root, "last")),
                  "loaded": sorted(sys.modules)}))
"""


def test_a_plan_of_run_steps_goes_through_the_core_without_the_notebook_machinery(tmp_path):
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    write_config(root, {"project": "core", "plans": {"plain": {"backend": "local", "steps": [
        {"name": "loss", "run": "python -c \"print('val_loss=0.25')\"", "max_minutes": 1,
         "expect": {"metrics": {"stdout": {"val_loss=([0-9.]+)": "< 0.3"}}}}]}}})
    out = subprocess.run([sys.executable, "-c", RUN, str(root / "sub")], capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["plans"]["plain"]["worst_case_usd"] == 0 and got["estimate"]["allowed"], got["estimate"]
    assert got["state"]["status"] == "passed" and got["report"]["status"] == "passed", got["report"]
    assert got["report"]["evidence"] and got["problems"] == []
    run_dir = Path(got["run_dir"])
    loaded = set(got["loaded"])
    tops = {m.split(".")[0] for m in loaded}
    assert not tops & set(HEAVY), f"a plan of run steps loaded {sorted(tops & set(HEAVY))}"
    for layer in ("notebook", "mocks", "web", "sessions", "session_backends", "kernelkit", "cli", "mcp_server", "api"):
        assert f"agent_testbench.{layer}" not in loaded, f"a plan of run steps loaded agent_testbench.{layer}"

    log = next((run_dir / "steps").glob("*/log.txt"))           # evidence changed after the run no longer verifies
    log.write_text(log.read_text() + "edited\n")
    assert core.verify(run_dir) == [f"changed: {log.relative_to(run_dir).as_posix()}"]
    (run_dir / "report.json").unlink()
    assert "no recorded evidence" in core.verify(run_dir)[0]
