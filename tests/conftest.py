import base64
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import nbformat
import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"
PNG_1PX = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def make_notebook(path: Path, cells: list[str]) -> Path:
    """Code cells from strings; a string starting with '# md:' becomes a markdown cell."""
    nb = nbformat.v4.new_notebook()
    for c in cells:
        if c.startswith("# md:"):
            nb.cells.append(nbformat.v4.new_markdown_cell(c[5:].strip()))
        else:
            nb.cells.append(nbformat.v4.new_code_cell(textwrap.dedent(c).strip()))
    nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
    path.parent.mkdir(parents=True, exist_ok=True)
    nbformat.write(nb, str(path))
    return path


def write_config(root: Path, cfg: dict) -> Path:
    (root / "testbench.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    return root / "testbench.yaml"


def cli(root: Path, *args, timeout=600, env=None) -> subprocess.CompletedProcess:
    """The CLI exactly as a person or agent runs it, from the project folder."""
    return subprocess.run([sys.executable, "-m", "agent_testbench", *args], cwd=root, capture_output=True, text=True,
                          timeout=timeout, env={**os.environ, **(env or {})})


POSIX_ONLY = {"test_ssh.py": "the ssh backend's scripts run on the Linux or macOS machine it reaches",
              "test_watchdog.py": "the watchdog runs on remote Linux and macOS machines (Colab, ssh, containers)",
              "test_docker.py": "the docker backend runs Linux containers"}


def pytest_collection_modifyitems(config, items):
    if os.name != "nt":
        return
    for item in items:
        why = POSIX_ONLY.get(item.path.name)
        if why or item.get_closest_marker("posix"):
            item.add_marker(pytest.mark.skip(reason=f"not on Windows: {why or 'uses POSIX tools'}"))


@pytest.fixture(autouse=True)
def one_serial_test_at_a_time(request, tmp_path_factory):
    """Tests marked serial use something the whole machine shares (the Docker daemon, a kind cluster). Under pytest -n
    they hold a lock all the run's workers share, so they run one at a time on whichever worker gets them (an
    xdist_group would need --dist loadgroup, which pytest-xdist 3.8 cannot honour on pytest 9.2)."""
    if not (os.environ.get("PYTEST_XDIST_WORKER") and request.node.get_closest_marker("serial")):
        yield
        return
    from agent_testbench.procs import exclusive
    with exclusive(tmp_path_factory.getbasetemp().parent / "serial.lock"):     # the run's own basetemp
        yield


@pytest.fixture(autouse=True)
def plugin_registries_restored():
    """A test that registers a plug-in (plugins.register, backends.register) leaves it behind for every later test on
    the same worker, so results depended on test order. Each loaded kind's table is put back as it was."""
    from agent_testbench import plugins as P
    def tables():
        out = {}
        for kind, module in P.KINDS.items():
            k = getattr(sys.modules.get(module), "KIND", None)
            if isinstance(k, P.Kind):
                out[kind] = (k, None if k.loaded is None else dict(k.loaded), dict(k.broken))
        return out
    before = tables()
    yield
    for kind, (k, _, _) in tables().items():
        if kind in before:
            _, loaded, broken = before[kind]
            k.loaded, k.broken = loaded, broken
        else:
            k.loaded, k.broken = None, {}


@pytest.fixture(autouse=True)
def machine_limits(tmp_path, monkeypatch) -> Path:
    """Every test gets its own ~/.testbench (limits for all projects, the machine's ledger), never the real one."""
    d = tmp_path / "user-testbench"
    monkeypatch.setenv("TESTBENCH_USER_DIR", str(d))
    return d


@pytest.fixture
def tiny(tmp_path) -> Path:
    """A fresh copy of the tiny-model example."""
    dest = tmp_path / "tiny-model"
    shutil.copytree(EXAMPLES / "tiny-model", dest, ignore=shutil.ignore_patterns(".testbench", "outputs", "__pycache__"))
    return dest


@pytest.fixture
def plain(tmp_path) -> Path:
    """A small project with a dependency-free notebook (no scikit-learn needed)."""
    root = tmp_path / "plain"
    make_notebook(root / "nb.ipynb", [
        "# md: A plain notebook",
        'N = 3 #@param {type:"integer"}\nNAME = "ada" #@param {type:"string"}',
        "import json, pathlib\npathlib.Path('out').mkdir(exist_ok=True)\nprint('hello', NAME)",
        "json.dump({'n': N, 'squares': [i * i for i in range(N)]}, open('out/result.json', 'w'))\nprint('wrote', N)",
    ])
    write_config(root, {"project": "plain", "plans": {
        "quick": {"backend": "local", "steps": [
            {"name": "notebook", "notebook": "nb.ipynb", "max_minutes": 3, "artifacts": ["out/*"],
             "expect": {"json": {"out/result.json": {"n": ">= 3", "squares.-1": "== 4"}}}}]}}})
    return root


def has_sklearn() -> bool:
    try:
        import sklearn, matplotlib
        return bool(sklearn and matplotlib)
    except ImportError:
        return False


def chromium_ready() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


def own_port(config: Path, port: int) -> None:
    """A copied example's fixed port in its testbench.yaml swapped for a free one, so tests running at the same time
    (pytest -n) or a server already on this machine never answer for it."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    text = config.read_text()
    assert str(port) in text, f"{config} no longer uses port {port}: update this test"
    config.write_text(text.replace(str(port), str(free)))


PLUGIN = '''
from agent_testbench.backends import Backend
from agent_testbench.backends.local import LocalBackend
from agent_testbench.session_backends import LocalSession


class FakeRunner(LocalBackend):
    def run(self):
        rep, _ = super().run()
        return rep, self.cost(rep)

    def cost(self, rep):
        return backend.rate_per_minute(self.cfg, self.plan) * rep.get("minutes", 0)


class Fake(Backend):
    """This machine, priced as if it were a rented GPU box."""
    gpus = ("FAKE1", "FAKE2")
    remote = False
    startup_minutes = 1.0
    config_defaults = {"fake": {"usd_per_hour": 6.0}}

    def validate(self, cfg):
        if cfg["fake"]["usd_per_hour"] <= 0:
            raise ValueError("fake.usd_per_hour must be positive")

    def rate_per_minute(self, cfg, hardware):
        return cfg["fake"]["usd_per_hour"] / 60

    def plan_runner(self):
        return FakeRunner

    def session_class(self):
        return LocalSession

    def doctor(self):
        return [("fake box reachable", True, "yes")]


backend = Fake()
'''


@pytest.fixture
def plugin(tmp_path) -> dict:
    """fakebench installed the way pip would: a module plus a dist-info with entry points, on PYTHONPATH."""
    site = tmp_path / "site"
    (site / "fakebench-0.1.dist-info").mkdir(parents=True)
    (site / "fakebench.py").write_text(PLUGIN)
    (site / "fakebench-0.1.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: fakebench\nVersion: 0.1\n")
    (site / "fakebench-0.1.dist-info" / "entry_points.txt").write_text(textwrap.dedent("""\
        [agent_testbench.backends]
        fake = fakebench:backend
        broken = fakebench:no_such_thing
        """))
    return {"PYTHONPATH": os.pathsep.join(p for p in (str(site), os.environ.get("PYTHONPATH")) if p)}


SCORER = """\
import json
from settings import SCORE
assert SCORE >= 0, "a negative score is a bug"
json.dump({"score": SCORE}, open("metrics.json", "w"))
print("score", SCORE)
"""


def git(root: Path, *args) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    """A small project in a git repository: a plan that writes a score and checks it (for loops)."""
    root = tmp_path / "scoring"
    root.mkdir()
    (root / "score.py").write_text(SCORER)
    (root / "settings.py").write_text("SCORE = 1\n")
    (root / ".gitignore").write_text("metrics.json\n__pycache__/\n")
    write_config(root, {"project": "scoring", "plans": {"score": {"steps": [
        {"name": "score", "run": "python score.py", "max_minutes": 1,
         "expect": {"json": {"metrics.json": {"score": ">= 0"}}}}]}}})
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "start")
    return root


def score(root: Path, value) -> None:
    (root / "settings.py").write_text(f"SCORE = {value}\n")


class Webhook:
    """A local stand-in for a webhook (Slack's, or any that takes a JSON POST): it keeps what it was sent and
    answers with `status`; `hang` keeps it from answering at all."""
    SECRET = "/services/T0SECRET/B0SECRET/xoxSECRETKEY"

    def __init__(self):
        import http.server
        import threading
        hook = self
        self.bodies, self.status, self.hang = [], 200, False

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                hook.bodies.append({"path": self.path, **json.loads(data)})
                if hook.hang:
                    threading.Event().wait(30)
                body = b"ok" if hook.status < 300 else b"no_service"
                self.send_response(hook.status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}{self.SECRET}"

    def events(self, name=None) -> list[dict]:
        return [b for b in self.bodies if name is None or b["event"] == name]


@pytest.fixture
def webhook():
    hook = Webhook()
    yield hook
    hook.server.shutdown()
