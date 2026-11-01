"""Recipes: testbench.yaml for a goal. Each one loads as a config under init's header, init writes it (with what it
detects), --add appends without changing anything already in the file, and the plans that run on this machine pass on
a tiny project made to each recipe's contract."""
import asyncio
import json
import os
import re
import shutil
import socket
import subprocess

import pytest
import yaml

from agent_testbench import budget, config as C, recipes as RC
from agent_testbench.cli import INIT_TEMPLATE
from conftest import chromium_ready, cli, make_notebook

NAMES = ["cargo-test", "data-pipeline", "docker-sandbox", "go-test", "hf-finetune", "jest", "llm-eval", "notebook-colab",
         "playwright-ts", "pytest", "pytorch-train", "ssh-gpu-box", "vitest", "web-app"]


def fresh(project="recipes") -> str:
    """What `testbench init` writes before any plan goes in."""
    return INIT_TEMPLATE.format(project=project, plans="")


def notebooks_of(recipe: dict) -> set:
    return {s["notebook"] for p in recipe["plans"].values() for s in p["steps"] if "notebook" in s}


def in_order(old: str, new: str) -> bool:
    """Every line of old is in new, in the same order: nothing was changed or removed, only added."""
    lines = iter(new.splitlines())
    return all(any(line == n for n in lines) for line in old.splitlines())


def test_the_recipes_that_ship_are_well_formed():
    assert RC.names() == NAMES
    for name in NAMES:
        r = RC.load(name)
        head = "\n".join(r["header"])
        assert r["header"][0].startswith(f"# Recipe {name}: ") and r["summary"], name
        for part in ("Proves:", "FILL IN:", "Costs:"):
            assert part in head, f"{name}: its header says what it proves, what to fill in and what it costs ({part})"
        assert set(r["sections"]) <= set(RC.SECTIONS), f"{name} never sets budget:, pricing: or guard:"
        assert all(not ln.strip() or ln.startswith("  ") for ln in r["plan_lines"]), f"{name}: plans indented by 2"
        assert [ln[2:].split(":")[0] for ln in r["plan_lines"] if re.match(r"  \S", ln) and not ln.strip().startswith("#")] \
            == list(r["plans"])
        for plan_name, plan in r["plans"].items():
            where = f"{name}.{plan_name}"
            assert plan.get("description"), f"{where} says what it proves"
            assert all("max_minutes" in s for s in plan["steps"]), f"{where}: every step bounds its time (and cost)"
            assert any(set(s.get("expect") or {}) - {"exit_code"} for s in plan["steps"]), \
                f"{where} checks a result, not only that something ran"
            if plan.get("gpu"):
                assert any("gpu" in (s.get("expect") or {}) for s in plan["steps"]), f"{where} pays for a GPU: checks it was busy"


@pytest.mark.parametrize("name", NAMES)
def test_each_recipe_loads_as_a_config_under_init_s_header(tmp_path, name):
    r = RC.load(name)
    for nb in notebooks_of(r):
        make_notebook(tmp_path / nb, ["x = 1"])         # the notebooks a recipe names stand for the project's own
    text, _ = RC.compose(fresh(), [r], fresh=True)
    cfg = C.validate(yaml.safe_load(text), tmp_path)
    assert list(cfg["plans"]) == list(r["plans"])
    assert "FILL IN" in text and r["header"][0] in text, "the recipe's comments come along"
    assert cfg["budget"] == {"max_run_usd": 2.0, "ask_above_usd": 0.5, "max_day_usd": 10.0}, "init's budget is kept"
    costs = {f"{budget.worst_case(cfg, p)['worst_case_usd']:.2f}" for p in cfg["plans"]}
    for p in cfg["plans"]:
        assert budget.worst_case(cfg, p)["worst_case_usd"] <= cfg["budget"]["max_run_usd"], \
            f"{name}.{p} fits the default budget, so nothing in a recipe is refused as it ships"
    claimed = set(re.findall(r"\$(\d+\.\d\d)", "\n".join(r["header"]))) - {"0.50", "2.00", "10.00"}
    assert claimed <= costs, f"{name}: the costs its header states ({claimed}) are its worst cases ({costs})"


def test_all_recipes_combine_into_one_file(tmp_path):
    make_notebook(tmp_path / "notebook.ipynb", ["x = 1"])
    chosen = [RC.load(n, tmp_path) for n in NAMES]
    text, notes = RC.compose(fresh(), chosen, fresh=True)
    cfg = C.validate(yaml.safe_load(text), tmp_path)
    assert list(cfg["plans"]) == [p for r in chosen for p in r["plans"]]
    assert cfg["sessions"]["backend"] == "docker", "the first recipe with sessions: replaces init's default"
    assert cfg["modal"]["memory_gb"] == 32, "the first recipe with modal: (hf-finetune) sets it"
    assert "# modal:" not in text, "init's commented-out modal: example goes once a real one is there"
    kept = [n for n in notes if n.startswith("Kept")]
    assert any("`modal:`" in n and "pytorch-train" in n and "torch==2.14.1" in n for n in kept), notes
    assert any("`sessions:`" in n and "ssh-gpu-box" in n for n in kept), notes


def test_the_project_s_own_notebook_and_script_replace_the_recipe_s(tmp_path):
    make_notebook(tmp_path / "my notebooks" / "an analysis.ipynb", ['EPOCHS = 3 #@param {type:"integer"}'])
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "train.py").write_text("print('loss=1')\n")
    for name in NAMES:                                  # a path that needs quoting still makes valid YAML
        r = RC.load(name, tmp_path)
        cfg = C.validate(yaml.safe_load(RC.compose(fresh(), [r], fresh=True)[0]), tmp_path)
        steps = [s for p in cfg["plans"].values() for s in p["steps"]]
        if name == "notebook-colab":
            assert r["swapped"] == {"notebook.ipynb": '"my notebooks/an analysis.ipynb"'}
            assert {s.get("notebook") for s in steps} == {None, "my notebooks/an analysis.ipynb"}
            assert steps[0]["run"] == 'testbench lint "my notebooks/an analysis.ipynb" --strict'
        if name in ("pytorch-train", "hf-finetune", "ssh-gpu-box"):
            assert all("scripts/train.py" in s["run"] for s in steps if "--device" in s.get("run", "") or "--model" in s.get("run", "")), name


def test_recipes_lists_every_recipe_with_its_costs(tmp_path):
    r = cli(tmp_path, "recipes")
    assert r.returncode == 0, r.stdout + r.stderr
    for name in NAMES:
        assert f"\n{name} (" in r.stdout and RC.load(name)["summary"] in r.stdout
    assert "Worst cases at the built-in prices and the default budget." in r.stdout
    assert re.search(r"gpu-check +modal T4, up to 10 min, worst case \$0\.23\n", r.stdout), r.stdout
    assert re.search(r"full +modal A10, up to 60 min, worst case \$1\.54 - above budget.ask_above_usd \$0\.50: a "
                     r"person approves it", r.stdout), r.stdout
    assert re.search(r"notebook-colab +colab, up to 30 min, worst case \$0\.01", r.stdout)
    assert re.search(r"box-full +ssh RTX4090, up to 240 min, free", r.stdout)
    (tmp_path / "testbench.yaml").write_text("project: priced\nbudget: {max_run_usd: 1.0}\n"
                                             "pricing: {modal_gpu_per_s: {T4: 0.0005}}\nplans: {}\n")
    r = cli(tmp_path, "recipes")
    assert "Worst cases at project priced's prices and budget." in r.stdout
    assert re.search(r"gpu-check +modal T4, up to 10 min, worst case \$0\.53 - above budget.ask_above_usd", r.stdout)
    assert re.search(r"full +modal A10, up to 60 min, worst case \$1\.54 - over budget.max_run_usd \$1\.00: refused "
                     r"until a person raises it", r.stdout), r.stdout


def test_recipes_name_prints_it_as_it_ships(tmp_path):
    r = cli(tmp_path, "recipes", "pytorch-train")
    assert r.returncode == 0 and r.stdout == (RC.HERE / "pytorch-train.yaml").read_text()
    r = cli(tmp_path, "recipes", "pytorch")
    assert r.returncode == 2 and ("no recipe 'pytorch' (did you mean pytorch-train?); the recipes are cargo-test, "
                                  "data-pipeline, docker-sandbox") in r.stdout


def test_init_with_recipes_writes_a_config_that_loads(tmp_path):
    root = tmp_path / "goal"
    make_notebook(root / "notebooks" / "analysis.ipynb", ['EPOCHS = 5 #@param {type:"integer"}', "x = EPOCHS"])
    (root / "tests").mkdir()
    (root / "tests" / "test_math.py").write_text("def test_add():\n    assert 1 + 1 == 2\n")
    (root / "src").mkdir()
    (root / "src" / "train.py").write_text("print('loss=1.0')\n")
    r = cli(root, "init", "--recipe", "pytorch-train", "--recipe", "notebook-colab")
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert out.startswith("Wrote testbench.yaml from recipes pytorch-train, notebook-colab:")
    assert "plan gpu-check: modal T4, up to 10 min, worst case $0.23 (recipe pytorch-train)" in out
    assert "runs src/train.py, found here, where the recipe says train.py" in out
    assert "runs notebooks/analysis.ipynb, found here, where the recipe says notebook.ipynb" in out
    assert "plan tests: a pytest suite (tests/test_math.py, ...), found here" in out, "what no recipe covers is kept"
    assert "found notebook notebooks/analysis.ipynb (1 form field(s)): the recipe's plans already cover it" in out
    assert "found a training script (src/train.py): the recipe's plans already cover it" in out
    assert "`testbench run smoke`" in out and (root / "AGENTS.md").is_file()
    cfg = C.load(root)
    assert list(cfg["plans"]) == ["tests", "smoke", "gpu-check", "full", "notebook-smoke", "notebook-colab"]
    assert cfg["plans"]["smoke"]["steps"][0]["run"].startswith("python src/train.py --epochs 1")
    assert cfg["plans"]["notebook-colab"]["steps"][0]["notebook"] == "notebooks/analysis.ipynb"
    assert cfg["modal"]["pip"] == ["torch==2.14.1"] and cfg["colab"]["pip"] == []
    r = cli(root, "init", "--recipe", "pytest")
    assert r.returncode == 2 and "already exists (use --force to replace it, or --add to add" in r.stdout


def test_init_without_a_recipe_is_unchanged(tmp_path):
    (tmp_path / "train.py").write_text("print('trained')\n")
    r = cli(tmp_path, "init")
    assert r.returncode == 0 and "plan train-smoke: a training script (train.py)" in r.stdout
    assert "# modal:" in (tmp_path / "testbench.yaml").read_text() and "Recipe" not in r.stdout
    r = cli(tmp_path, "init")
    assert r.returncode == 2 and r.stdout.strip() == f"{tmp_path / 'testbench.yaml'} already exists (use --force to replace it)"
    r = cli(tmp_path, "init", "--add")
    assert r.returncode == 2 and "--add adds a recipe's plans" in r.stdout


def test_a_recipe_naming_a_notebook_the_project_lacks_is_refused(tmp_path):
    r = cli(tmp_path, "init", "--recipe", "notebook-colab")
    assert r.returncode == 2 and "runs the notebook notebook.ipynb, which is not in this project" in r.stdout
    assert not (tmp_path / "testbench.yaml").exists(), "nothing that would not load is written"


def test_add_appends_and_changes_nothing_already_there(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_math.py").write_text("def test_add():\n    assert 1 + 1 == 2\n")
    assert cli(tmp_path, "init").returncode == 0
    yml = tmp_path / "testbench.yaml"
    yml.write_text(yml.read_text().replace("max_run_usd: 2.00", "max_run_usd: 0.40")
                   + "# a note of mine at the end\n")
    before, cfg_before = yml.read_text(), C.load(tmp_path)

    r = cli(tmp_path, "init", "--add", "--recipe", "llm-eval", "--recipe", "pytorch-train")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("Added to testbench.yaml (the plans, budget and sections already there are unchanged):")
    assert "plan full: modal A10, up to 60 min, worst case $1.54 - over budget.max_run_usd $0.40: refused until a " \
           "person raises it" in r.stdout, "the costs are judged by this project's own budget"
    after = yml.read_text()
    assert in_order(before, after), "every line already there is still there, in order"
    cfg = C.load(tmp_path)
    assert cfg["budget"] == cfg_before["budget"] and cfg["plans"]["tests"] == cfg_before["plans"]["tests"]
    assert list(cfg["plans"]) == ["tests", "eval-quick", "eval-full", "smoke", "gpu-check", "full"]
    assert cfg["modal"]["pip"] == ["torch==2.14.1"], "a section the file lacked is added"

    for again in (["llm-eval"], ["pytest"]):            # a plan name that is taken: refused, nothing written
        r = cli(tmp_path, "init", "--add", "--recipe", *again)
        assert r.returncode == 2 and "already has one, so nothing was written" in r.stdout, r.stdout
        assert yml.read_text() == after

    r = cli(tmp_path, "init", "--add", "--recipe", "hf-finetune")      # a section the file has: kept, and said so
    assert r.returncode == 0 and "Kept the `modal:` section already in testbench.yaml (recipe hf-finetune's" in r.stdout
    assert "transformers==5.20.2" in r.stdout and C.load(tmp_path)["modal"]["pip"] == ["torch==2.14.1"]
    assert in_order(after, yml.read_text())


def test_add_follows_the_file_s_own_indentation_and_refuses_what_it_cannot_extend(tmp_path):
    yml = tmp_path / "testbench.yaml"
    yml.write_text("project: four\nplans:\n    mine:\n        steps:\n            - {name: a, run: \"true\"}\n")
    r = cli(tmp_path, "init", "--add", "--recipe", "data-pipeline")
    assert r.returncode == 0, r.stdout
    assert "\n    pipeline-sample:\n      backend: local\n" in yml.read_text()
    assert list(C.load(tmp_path)["plans"]) == ["mine", "pipeline-sample", "pipeline-full"]
    yml.write_text("project: inline\nplans: {}\n")
    r = cli(tmp_path, "init", "--add", "--recipe", "pytest")
    assert r.returncode == 2 and "`plans:` in testbench.yaml is written on one line" in r.stdout
    yml.write_text("project: broken\nplans: {p: {steps: []}}\n")
    r = cli(tmp_path, "init", "--add", "--recipe", "pytest")
    assert r.returncode == 2 and "A recipe is added only to a testbench.yaml that loads" in r.stdout


def test_the_mcp_tool_lists_and_shows_recipes(tmp_path):
    from agent_testbench import mcp_server

    async def go():
        tools = {t.name for t in await mcp_server.server.list_tools()}
        listing = await mcp_server.server.call_tool("recipes", {"project_dir": str(tmp_path)})
        one = await mcp_server.server.call_tool("recipes", {"name": "llm-eval", "project_dir": str(tmp_path)})
        return tools, *(json.dumps(o, default=lambda x: getattr(x, "text", str(x))) for o in (listing, one))

    tools, listing, one = asyncio.run(go())
    assert "recipes" in tools
    assert "pytorch-train (local, modal)" in listing and "[exit 0]" in listing
    assert "Recipe llm-eval:" in one and "eval-quick:" in one


# ---- the plans that run on this machine pass, on a tiny project made to each recipe's contract ----------------

TRAIN = """\
import argparse, json, pathlib
p = argparse.ArgumentParser()
p.add_argument("--out", required=True)
args, _ = p.parse_known_args()
out = pathlib.Path(args.out)
out.mkdir(parents=True, exist_ok=True)
for step, loss in enumerate([2.31, 1.7, 1.2, 0.9]):
    print(f"step {step} loss={loss}")
print("val_loss=3.5")                     # the \\b in the recipe's pattern keeps this one from counting
(out / "model.pt").write_bytes(b"weights")
json.dump({"accuracy": 0.61}, open(out / "metrics.json", "w"))
"""

FINETUNE = """\
import argparse, json, pathlib
p = argparse.ArgumentParser()
p.add_argument("--output-dir", required=True)
args, _ = p.parse_known_args()
adapter = pathlib.Path(args.output_dir) / "adapter"
adapter.mkdir(parents=True, exist_ok=True)
(adapter / "adapter_config.json").write_text('{"r": 4}')
(adapter / "adapter_model.safetensors").write_bytes(b"lora")
json.dump({"eval_loss": 10.37, "train_loss": 10.4}, open(adapter.parent / "metrics.json", "w"))
"""

EVAL = """\
import argparse, json, pathlib
from settings import SCORE
p = argparse.ArgumentParser()
p.add_argument("--limit", type=int, default=200)
p.add_argument("--seed", type=int)
p.add_argument("--out", required=True)
a = p.parse_args()
print(f"scored {a.limit} cases")
print(f"score={SCORE}")
pathlib.Path(a.out).parent.mkdir(parents=True, exist_ok=True)
json.dump({"score": SCORE, "n": a.limit}, open(a.out, "w"))
"""

PIPELINE = """\
import argparse, csv, json, pathlib
p = argparse.ArgumentParser()
p.add_argument("--input", required=True)
p.add_argument("--out", required=True)
a = p.parse_args()
rows = list(csv.DictReader(open(a.input)))
good = [r for r in rows if r["value"].strip()]
out = pathlib.Path(a.out)
out.mkdir(parents=True, exist_ok=True)
with open(out / "clean.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["id", "value"])
    w.writeheader()
    w.writerows(good)
json.dump({"rows_in": len(rows), "rows_out": len(good), "dropped_pct": 100 * (len(rows) - len(good)) / len(rows)},
          open(out / "summary.json", "w"))
"""

SERVER = """\
const http = require("http");
const arg = (name) => process.argv[process.argv.indexOf(name) + 1];
const page = `<!doctype html><title>My App</title><h1>Welcome</h1>
<form><input name="email"><button type="submit">Join</button></form><p id="done"></p>
<script>document.querySelector("form").addEventListener("submit", (e) => { e.preventDefault();
  document.getElementById("done").textContent = "Thanks, " + document.querySelector("input").value; });</script>`;
http.createServer((req, res) => {
  if (req.url === "/") { res.writeHead(200, {"Content-Type": "text/html"}); res.end(page); }
  else { res.writeHead(204); res.end(); }
}).listen(Number(arg("--port")), arg("--host"));
"""


def _pytest_project(root):
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_math.py").write_text("def test_add():\n    assert 1 + 1 == 2\n\n\n"
                                                 "def test_mul():\n    assert 2 * 3 == 6\n")


def _notebook_project(root):
    make_notebook(root / "notebooks" / "analysis.ipynb", [
        'EPOCHS = 5 #@param {type:"integer"}\nSAMPLE_SIZE = 10000 #@param {type:"integer"}',
        "import json, pathlib\npathlib.Path('outputs').mkdir(exist_ok=True)\n"
        "json.dump({'accuracy': min(0.99, 0.5 + 0.1 * EPOCHS), 'rows': SAMPLE_SIZE}, open('outputs/metrics.json', 'w'))",
    ])


def _web_project(root):
    root.mkdir(parents=True)
    (root / "server.js").write_text(SERVER)
    (root / "package.json").write_text(json.dumps({"name": "stub", "private": True, "scripts": {"dev": "node server.js"}}))


def _data_project(root):
    (root / "data").mkdir(parents=True)
    (root / "pipeline.py").write_text(PIPELINE)
    (root / "data" / "sample.csv").write_text("id,value\n" + "".join(f"{i},{i * 2}\n" for i in range(40)))


def _eval_project(root):
    root.mkdir(parents=True)
    (root / "eval.py").write_text(EVAL)
    (root / "settings.py").write_text("SCORE = 0.8\n")


RUNNABLE = {
    "pytest": (_pytest_project, ["tests"]),
    "notebook-colab": (_notebook_project, ["notebook-smoke"]),
    "pytorch-train": (lambda root: (root.mkdir(parents=True), (root / "train.py").write_text(TRAIN)), ["smoke"]),
    "ssh-gpu-box": (lambda root: (root.mkdir(parents=True), (root / "train.py").write_text(TRAIN)), ["box-smoke"]),
    "hf-finetune": (lambda root: (root.mkdir(parents=True), (root / "finetune.py").write_text(FINETUNE)),
                    ["finetune-smoke"]),
    "llm-eval": (_eval_project, ["eval-quick", "eval-full"]),
    "data-pipeline": (_data_project, ["pipeline-sample"]),
    "web-app": (_web_project, ["ui"]),
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("name", list(RUNNABLE))
def test_the_local_plans_pass_on_a_project_made_to_the_recipe(tmp_path, name):
    if name == "web-app" and not (chromium_ready() and shutil.which("npm")):
        pytest.skip("needs `playwright install chromium` and npm")
    if name == "web-app" and os.name == "nt":
        pytest.skip("a web step starts `serve:` without a shell, and on Windows npm is npm.cmd")
    make, plans = RUNNABLE[name]
    root = tmp_path / name
    make(root)
    r = cli(root, "init", "--recipe", name)
    assert r.returncode == 0, r.stdout + r.stderr
    if name == "web-app":                               # a free port, so a dev server running here does not interfere
        yml = root / "testbench.yaml"
        yml.write_text(yml.read_text().replace("5173", str(_free_port())))
    for plan in plans:
        r = cli(root, "run", plan, "--wait", "300")
        assert r.returncode == 0 and f"# PASS - plan `{plan}` on local" in r.stdout, f"{name}.{plan}:\n{r.stdout}{r.stderr}"
    if name == "pytorch-train":
        assert "stdout /\\bloss=([0-9.eE+-]+)/ < 2.30 - 0.9 (wanted < 2.3); the last of 4 matches" in r.stdout, \
            "the last training loss counts, not val_loss"
    if name == "pytest":
        assert "junit.xml: tests >= 1 - 2 (wanted >= 1.0)" in r.stdout


@pytest.mark.posix                                      # Linux containers
def test_the_sandbox_recipe_runs_offline_in_a_container(tmp_path):
    import test_docker as TD                            # its image and build settings, so the image is built the same way
    if not TD._docker_answers():
        pytest.skip("needs a Docker daemon that answers `docker info`")
    root = tmp_path / "unreviewed"
    _pytest_project(root)
    (root / "main.py").write_text("import json, os\nos.makedirs('outputs', exist_ok=True)\n"
                                  "json.dump({'ok': True}, open('outputs/result.json', 'w'))\n")
    assert cli(root, "init", "--recipe", "docker-sandbox").returncode == 0
    yml = root / "testbench.yaml"
    here = "".join(f"  {k}: {json.dumps(v)}\n" for k, v in TD._build_settings().items())     # this machine's proxy
    yml.write_text(yml.read_text().replace("  image: python:3.12-slim\n", f"  image: {TD.IMAGE}\n{here}"))
    for plan in ("sandbox-tests", "sandbox-run"):
        r = cli(root, "run", plan, "--wait", "900", timeout=1200)
        assert r.returncode == 0 and f"# PASS - plan `{plan}` on docker" in r.stdout, f"{plan}:\n{r.stdout}{r.stderr}"
    assert "stdout contains 'Network is unreachable' - found" in r.stdout


def test_the_eval_metric_drives_a_loop(tmp_path):
    root = tmp_path / "evals"
    _eval_project(root)
    assert cli(root, "init", "--recipe", "llm-eval").returncode == 0
    (root / ".gitignore").write_text("outputs/\n__pycache__/\n")

    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout

    git("init", "-q")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "start")
    r = cli(root, "loop", "start", "eval-quick", "--metric", "outputs/eval-quick.json:score", "--budget-usd", "0")
    assert r.returncode == 0 and "BASELINE: score 0.8" in r.stdout, r.stdout
    (root / "settings.py").write_text("SCORE = 0.86\n")
    r = cli(root, "loop", "try", "a clearer prompt")
    assert r.returncode == 0 and "KEPT try 1: score 0.86" in r.stdout, r.stdout
    assert cli(root, "loop", "stop").returncode == 0
