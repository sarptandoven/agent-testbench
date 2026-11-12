"""testbench.yaml's JSON Schema (src/agent_testbench/schema/testbench.schema.json): what editors use to complete keys
and flag mistakes. Every config that ships or init writes validates; configs testbench refuses for their shape are
refused by the schema too; and a config key without a schema entry fails here, so the two cannot drift apart."""
import json
import re
import tomllib
from pathlib import Path

import jsonschema
import pytest
import yaml

from agent_testbench import backends as B, config as C, expect as X, mocks as M, recipes as RC
from agent_testbench.cli import INIT_TEMPLATE
from conftest import EXAMPLES, REPO, cli, make_notebook

SCHEMA = json.loads(C.SCHEMA.read_text(encoding="utf-8"))
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)
MODELINE = f"# yaml-language-server: $schema={C.SCHEMA_URL}"


class EditorLoader(yaml.SafeLoader):
    """YAML as an editor's language server reads it (YAML 1.2): `on:` and `yes` are words, dates are text."""


EditorLoader.yaml_implicit_resolvers = {
    k: [(tag, r) for tag, r in v if tag not in ("tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp")]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()}
EditorLoader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
                                   list("tTfF"))


def problems(text: str) -> list[str]:
    data = yaml.load(text, Loader=EditorLoader)
    return [f"{'.'.join(map(str, e.absolute_path)) or '(top)'}: {e.message}" for e in VALIDATOR.iter_errors(data)]


def test_it_is_a_draft_2020_12_schema_shipped_in_the_package():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema" and SCHEMA["$id"] == C.SCHEMA_URL
    assert C.SCHEMA.parent == Path(C.__file__).parent / "schema", "next to report-v1.schema.json, in the package"
    project = tomllib.loads((REPO / "pyproject.toml").read_text())
    wheel = project["tool"]["hatch"]["build"]
    assert wheel["targets"]["wheel"]["packages"] == ["src/agent_testbench"], "the wheel holds the whole package"
    assert not any(re.fullmatch(p.replace(".", r"\.").replace("*", ".*"), C.SCHEMA.name) for p in wheel["exclude"])
    assert "jsonschema>=4" in project["project"]["optional-dependencies"]["dev"]


def _described(node, at="", conditional=False):
    """(where, description) for every property the schema names, outside if/then/not (which only add rules)."""
    if isinstance(node, list):
        for i, x in enumerate(node):
            yield from _described(x, f"{at}[{i}]", conditional)
    elif isinstance(node, dict):
        for key, value in node.items():
            inside = conditional or key in ("if", "then", "else", "not")
            if key == "properties" and isinstance(value, dict) and not inside:
                for name, sub in value.items():
                    yield f"{at}.{name}", sub.get("description")
            yield from _described(value, f"{at}.{key}", inside)


def test_every_property_has_a_one_line_description():
    found = list(_described(SCHEMA))
    assert len(found) > 150
    missing = [where for where, text in found if not text or "\n" in text]
    assert not missing, f"give each a one-line plain description (editors show it on hover): {missing}"
    assert all(d.get("description") for d in SCHEMA["$defs"].values())


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*/testbench.yaml")) + sorted(RC.HERE.glob("*.yaml")),
                         ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_shipped_config_validates(path):
    assert problems(path.read_text(encoding="utf-8")) == []


def test_every_recipe_composed_on_init_s_header_validates_and_keeps_the_schema_line_first(tmp_path):
    for name in RC.names():
        text, _ = RC.compose(INIT_TEMPLATE.format(project="recipes", plans=""), [RC.load(name)], fresh=True)
        assert text.splitlines()[0] == MODELINE, name
        assert problems(text) == [], name
    text, _ = RC.compose(INIT_TEMPLATE.format(project="all", plans=""), [RC.load(n) for n in RC.names()], fresh=True)
    assert text.splitlines()[0] == MODELINE and problems(text) == []


def _project(root: Path, kind: str) -> Path:
    root.mkdir(parents=True)
    if kind == "notebook":
        make_notebook(root / "train.ipynb", ['EPOCHS = 5 #@param {type:"integer"}', "import json\n"
                                             "json.dump({'accuracy': 0.9}, open('metrics.json', 'w'))"])
    elif kind == "pytest":
        (root / "tests").mkdir()
        (root / "tests" / "test_math.py").write_text("def test_add():\n    assert 1 + 1 == 2\n")
    elif kind == "train":
        (root / "train.py").write_text("import argparse\nprint('loss=0.5')\n")
    elif kind == "site":
        (root / "index.html").write_text("<!doctype html><title>Hi</title><p>hello</p>\n")
    elif kind == "node":
        (root / "package.json").write_text('{"name": "app", "scripts": {"test": "jest"}}\n')
    return root


@pytest.mark.parametrize("kind", ["empty", "notebook", "pytest", "train", "site", "node"])
def test_what_init_writes_validates_and_points_editors_at_the_schema(tmp_path, kind):
    root = _project(tmp_path / kind, kind)
    r = cli(root, "init")
    assert r.returncode == 0, r.stdout + r.stderr
    text = (root / "testbench.yaml").read_text()
    assert text.splitlines()[0] == MODELINE, "the first line, where the YAML language server looks"
    assert problems(text) == []
    C.load(root)


def test_init_with_a_recipe_keeps_the_schema_line_first(tmp_path):
    root = _project(tmp_path / "goal", "pytest")
    r = cli(root, "init", "--recipe", "data-pipeline", "--recipe", "pytest")
    assert r.returncode == 0, r.stdout + r.stderr
    text = (root / "testbench.yaml").read_text()
    assert text.splitlines()[0] == MODELINE and problems(text) == []


@pytest.mark.parametrize("text", [
    'plans:\n  tests: "pytest -q"\n',
    'project: p\nplans:\n  checks: ["ruff check .", "pytest -q"]\n',
    "plans:\n  checks:\n    - ruff check .\n    - {name: unit, run: pytest -q, max_minutes: 5}\n",
    "plans:\n  gpu:\n    backend: modal\n    gpu: T4\n    steps:\n      - pytest -q\n"
    "      - {name: train, run: python train.py, max_minutes: 30}\n",
    "plans:\n  tests: pytest -q\n  web:\n    steps:\n      - name: page\n        web: {url: 'http://127.0.0.1:8000/', "
    "serve: python -m http.server 8000, actions: [{click: '#go'}, {fill: {selector: '#n', text: Ada}}]}\n",
    "project:\nplans:\n",
    "",
])
def test_the_short_forms_validate(text):
    assert problems(text) == []


def test_a_long_config_with_every_section_validates():
    text = """\
project: everything
budget: {max_run_usd: 3, ask_above_usd: 1, max_day_usd: 20}
sessions: {backend: modal, gpu: T4, idle_minutes: 5, max_minutes: 30, secrets: [HF_TOKEN], cpu: 2, memory_gb: 8}
pricing: {modal_gpu_per_s: {T4: 0.0002}, modal_cpu_core_per_s: 0.00002, colab_usd_per_unit: 0.1, mine: {X: 1}}
guard: {ask: [git push]}
notify: {webhook_env: TESTBENCH_WEBHOOK, on: [run_end, loop_try]}
export: {csv: .testbench/runs.csv, jsonl: true, mlflow: {tracking_uri: "http://127.0.0.1:5000", experiment: e},
         wandb: {project: p, mode: offline, tags: [a]}}
exclude: [.git, data/*.bin]
modal: {python: "3.11", pip: [numpy], apt: [ffmpeg], run_commands: [echo hi], cpu: 4, memory_gb: 16}
colab: {pip: [x], requirements: req.txt, cli: colab, auth: adc}
ssh: {host: box, usd_per_hour: 1.5, setup: none, command: [ssh, -p, "2222"]}
docker: {image: "python:3.12-slim", network: none, gpus: all, cpu: 2, run_args: [-v, /d:/d], command: [podman]}
kubernetes: {namespace: ml, usd_per_hour: {cpu: 0, A100: 2.5}, env_secrets: {HF_TOKEN: hf-token/token},
             volumes: [{name: data, persistentVolumeClaim: {claimName: d}}],
             volume_mounts: [{name: data, mountPath: /data}]}
x-anchors: &step {max_minutes: 3}
plans:
  nb:
    backend: colab
    gpu: A100
    description: the notebook on a GPU
    steps:
      - {name: nb, notebook: nb.ipynb, params: {EPOCHS: 1}, cells: "1-3,5", uploads: [[a.csv]], colab_shim: false}
      - name: check
        run: pytest -q --junitxml=r.xml
        env: {SEED: "1"}
        secrets: [HF_TOKEN]
        pythonpath: [mocks]
        artifacts: ["out/*"]
        continue_on_failure: true
        mocks: {anthropic: {replies: tests/replies.yaml, fail_on_call: [2, 3], fail_with: rate_limit}}
        expect:
          exit_code: 0
          no_errors: true
          files: [out/model.pt]
          json: {metrics.json: {accuracy: ">= 0.9", "losses.-1": "~= 0.2 +- 0.1"}}
          text: {stdout: [epoch 10], log.txt: done}
          media: {out/v.mp4: {frames: ">= 120", audio: true}}
          junit: [r.xml, {path: s.xml, failures: 0, tests: ">= 10"}]
          metrics: {stdout: {"loss=([0-9.]+)": "< 0.3"}}
          gpu: {busy_pct: ">= 50"}
          image: {out/plot.png: {like: golden/plot.png, max_diff: 0.05}}
          duration: {minutes: "<= 5"}
          mock_calls: {anthropic: {calls: "<= 5", output_tokens: "<= 2000"}}
      - name: page
        web: {url: "http://127.0.0.1:9/", ready_timeout: 10, viewport: [800, 600], screenshot: false}
        expect: {web: {status: "between 200 and 299", title: T, text: [x], no_console_errors: true}}
"""
    assert problems(text) == []


# ---- what testbench refuses, the schema refuses ------------------------------------------------------------------

STEP = "plans:\n  p:\n    steps:\n      - "
ON = "plans: {p: {backend: %s, steps: [x]}}\n"          # a backend's section is checked when a plan uses it
REFUSED = [
    "project: -x\n", "project: [a]\n", "budget: [1]\n", "budget: {max_run_usd: lots}\n", "budget: {max_day_usd: -1}\n",
    "sessions: local\n", "sessions: {idle_minutes: 0}\n", "sessions: {secrets: HF_TOKEN}\n", "modal: abc\n",
    "pricing: {modal_cpu_core_per_s: {}}\n" + ON % "modal", "pricing: {modal_gpu_per_s: {T4: x}}\n",
    "modal: {cpu: -1}\n" + ON % "modal", "modal: {pip: numpy}\n" + ON % "modal", "guard: {ask: git push}\n",
    "exclude: .git\n", "notify: {webhook: x, webhook_env: Y}\n", "notify: {}\n", "notify: {webhook_env: 'a b'}\n",
    "notify: {webhook: 'ftp://x'}\n", "notify: {webhook_env: X, on: [never]}\n", "notify: {webhook_env: X, to: y}\n",
    "export: {sheets: {}}\n", "export: {csv: 5}\n", "export: {wandb: {mode: loud}}\n",
    "export: {wandb: {api_key: k}}\n",
    "export: {mlflow: {tracking_uri: 'http://u:p@host'}}\n", "export: {mlflow: {experiment: ''}}\n",
    "docker: {setup: always}\n" + ON % "docker", "docker: {network: 'a b'}\n" + ON % "docker",
    "docker: {usd_per_hour: -1}\n" + ON % "docker", "ssh: {host: h, setup: conda}\n" + ON % "ssh",
    "kubernetes: {image_pull_policy: Sometimes}\n" + ON % "kubernetes",
    "kubernetes: {namespace: My_NS}\n" + ON % "kubernetes",
    "kubernetes: {env_secrets: {HF_TOKEN: the-token-itself}}\n" + ON % "kubernetes",
    "plans: 5\n", "plans: [a]\n", "plans:\n  'bad name': pytest\n", "plans:\n  p: 42\n", "plans:\n  p: ''\n",
    "plans:\n  p: []\n", "plans:\n  p: [5]\n", "plans:\n  p: {steps: []}\n", "plans:\n  p: {backend: local}\n",
    "plans:\n  p: {workdir: here, steps: [x]}\n", "plans:\n  p: {backend: modal, gpu: 'T4:0', steps: [x]}\n",
    "plans:\n  p: {backend: 5, steps: [x]}\n",
    STEP + "{run: x}\n", STEP + "{name: a}\n", STEP + "{name: a, run: x, web: {url: u}}\n",
    STEP + "{name: a, run: x, expectt: {}}\n", STEP + "{name: a, run: 5}\n", STEP + "{name: a, notebook: 5}\n",
    STEP + "{name: a, run: x, max_minutes: 0}\n", STEP + "{name: a, run: x, max_minutes: true}\n",
    STEP + "{name: a, run: x, env: [A]}\n", STEP + "{name: a, run: x, artifacts: [{a: 1}]}\n",
    STEP + "{name: a, run: x, secrets: ['a b']}\n", STEP + "{name: a, run: x, continue_on_failure: 'no'}\n",
    STEP + "{name: a, run: x, params: {EPOCHS: 2}}\n", STEP + "{name: a, run: x, cells: '1-3'}\n",
    STEP + "{name: a, run: x, uploads: [[a.csv]]}\n", STEP + "{name: a, web: {url: 'http://x'}, colab_shim: false}\n",
    STEP + "{name: a, web: {url: 'http://x', viewport: wide}}\n", STEP + "{name: a, web: {serve: x}}\n",
    STEP + "{name: a, web: {url: 'http://x', ready_timeout: 0}}\n", STEP + "{name: a, run: x, mocks: [gemini]}\n",
    STEP + "{name: a, run: x, mocks: {openai: {retries: 2}}}\n",
    STEP + "{name: a, run: x, mocks: {openai: {fail_with: auth}}}\n",
    STEP + "{name: a, run: x, mocks: {openai: {fail_on_call: 0}}}\n",
    STEP + "{name: a, run: x, expect: {exit: 0}}\n", STEP + "{name: a, run: x, expect: {exit_code: '0'}}\n",
    STEP + "{name: a, run: x, expect: {no_errors: 'yes'}}\n", STEP + "{name: a, run: x, expect: {files: ['']}}\n",
    STEP + "{name: a, run: x, expect: {files: out.txt}}\n", STEP + "{name: a, run: x, expect: {json: {m.json: {}}}}\n",
    STEP + "{name: a, run: x, expect: {json: {m.json: {a: '=> 0.9'}}}}\n",
    STEP + "{name: a, run: x, expect: {json: {m.json: {a: [1]}}}}\n",
    STEP + "{name: a, run: x, expect: {text: {stdout: ''}}}\n", STEP + "{name: a, run: x, expect: {text: {log: []}}}\n",
    STEP + "{name: a, run: x, expect: {media: {v.mp4: {bitrate: 5}}}}\n",
    STEP + "{name: a, run: x, expect: {web: {text: [5]}}}\n", STEP + "{name: a, run: x, expect: {web: {title: ''}}}\n",
    STEP + "{name: a, run: x, expect: {web: {headers: 1}}}\n", STEP + "{name: a, run: x, expect: {junit: {}}}\n",
    STEP + "{name: a, run: x, expect: {junit: {path: r.xml, flaky: 0}}}\n",
    STEP + "{name: a, run: x, expect: {metrics: {stdout: {}}}}\n",
    STEP + "{name: a, run: x, expect: {gpu: {util: 1}}}\n", STEP + "{name: a, run: x, expect: {image: {a.png: {}}}}\n",
    STEP + "{name: a, run: x, expect: {image: {a.png: {like: g.png, max_diff: 2}}}}\n",
    STEP + "{name: a, run: x, expect: {duration: {hours: 1}}}\n",
    STEP + "{name: a, run: x, expect: {mock_calls: {gemini: {calls: 1}}}}\n",
    STEP + "{name: a, run: x, expect: {mock_calls: {openai: {cost: 1}}}}\n",
]


@pytest.mark.parametrize("text", REFUSED)
def test_what_testbench_refuses_for_its_shape_the_schema_refuses(tmp_path, text):
    (tmp_path / "testbench.yaml").write_text(text)
    with pytest.raises(C.ConfigError):
        C.load(tmp_path)
    assert problems(text), "an editor would show nothing wrong with a file testbench refuses"


# ---- drift: a config key without a schema entry fails here -------------------------------------------------------

def _props(path: str) -> dict:
    node = SCHEMA
    for part in path.split("/"):
        node = node[part]
    return node["properties"]


def _enum(node) -> set:
    found = set(node.get("enum") or [])
    for key in ("anyOf", "oneOf"):
        for sub in node.get(key) or []:
            found |= _enum(sub) | _enum(sub.get("items") or {})
    return found | _enum(node["items"]) if isinstance(node.get("items"), dict) else found


def test_every_config_key_has_a_schema_entry():
    top = SCHEMA["properties"]
    for key, value in C.DEFAULTS.items():
        assert key in top, f"testbench.yaml's {key}: has no entry in {C.SCHEMA.name}"
        for sub in value if isinstance(value, dict) else ():
            assert sub in top[key]["properties"], f"{key}.{sub} has no entry in {C.SCHEMA.name}"
    for name in B.BUILTIN:                   # not the registry: other tests register plugins of their own there
        for section, keys in B.get(name).config_defaults.items():
            assert section in top, f"backend {name}'s section {section}: has no entry in {C.SCHEMA.name}"
            missing = set(keys) - set(top[section]["properties"])
            assert not missing, f"{section}.{sorted(missing)} (backend {name}) have no entry in {C.SCHEMA.name}"
    backends = _enum(_props("$defs/plan_full")["backend"])
    assert set(B.BUILTIN) <= backends == _enum(top["sessions"]["properties"]["backend"]), "the built-ins, listed"
    for key in ("project", "notify", "export", "plans"):
        assert key in top
    assert set(C.PLAN_DEFAULTS) | {"steps", "cpu", "memory_gb"} == set(_props("$defs/plan_full"))
    step, allowed = set(_props("$defs/step_full")), {"name", *C.STEP_KINDS, *C.STEP_DEFAULTS}
    assert allowed == step, (f"a step takes exactly the keys config._step allows: add {sorted(allowed - step)} to, "
                             f"or remove {sorted(step - allowed)} from, $defs.step_full in {C.SCHEMA.name}")
    assert set(C.WEB_DEFAULTS) | {"url"} == set(_props("$defs/web"))


def test_every_expectation_and_option_has_a_schema_entry():
    expect = _props("$defs/expect")
    assert set(X.KINDS) == set(expect), (f"every expectation kind config._expect allows, and no other: add "
                                         f"{sorted(set(X.KINDS) - set(expect))} to, or remove "
                                         f"{sorted(set(expect) - set(X.KINDS))} from, $defs.expect in {C.SCHEMA.name}")
    keyed = lambda kind: set(expect[kind]["additionalProperties"]["properties"])
    assert keyed("media") == set(X.MEDIA_KEYS) and keyed("mock_calls") == set(X.MOCK_KEYS)
    assert set(expect["web"]["properties"]) == set(X.WEB_KEYS)
    assert set(expect["gpu"]["properties"]) == set(X.GPU_KEYS)
    assert set(expect["duration"]["properties"]) == set(X.DURATION_KEYS)
    assert set(SCHEMA["$defs"]["junit"]["anyOf"][1]["properties"]) == {"path", *X.JUNIT_KEYS}
    assert set(expect["mock_calls"]["propertyNames"]["enum"]) == set(X.MOCK_SDKS) == _enum(SCHEMA["$defs"]["mocks"])
    core = M.client_core()
    assert set(SCHEMA["$defs"]["mock_options"]["properties"]) == set(core.OPTION_KEYS)
    assert set(SCHEMA["$defs"]["mock_options"]["properties"]["fail_with"]["enum"]) == set(core.FAILURES)
    notify = SCHEMA["properties"]["notify"]["properties"]
    assert set(notify) == {"webhook", "webhook_env", "on"} and _enum(notify["on"]) == set(C.NOTIFY_EVENTS)
    export = SCHEMA["properties"]["export"]["properties"]
    assert set(export) == set(C.EXPORT_KEYS)
    for target, keys in C.EXPORT_KEYS.items():
        options = next(s for s in export[target]["anyOf"] if s.get("type") == "object")["properties"]
        assert set(options) == set(keys), f"export.{target}"


def test_testbench_schema_prints_it_anywhere(tmp_path):
    r = cli(tmp_path, "schema")
    assert r.returncode == 0 and json.loads(r.stdout) == SCHEMA, r.stderr
