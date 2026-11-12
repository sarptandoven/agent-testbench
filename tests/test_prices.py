"""The price catalog: it can raise a price above the built-in table or add hardware, never lower a price; a project's
`pricing:` still wins; and the catalog in the repository matches the built-in tables."""
import json
from pathlib import Path

from agent_testbench import budget, config as C
from agent_testbench.backends import colab_backend, modal_backend, runpod_backend
from conftest import REPO, cli, write_config


def test_the_repository_catalog_matches_the_built_in_tables():
    cat = json.loads((REPO / "pricing" / "catalog.json").read_text())
    assert cat["modal"]["gpu_per_s"] == modal_backend.GPU_PER_S
    assert cat["modal"]["cpu_core_per_s"] == modal_backend.CPU_CORE_PER_S
    assert cat["modal"]["mem_gib_per_s"] == modal_backend.MEM_GIB_PER_S
    assert cat["colab"]["units_per_hour"] == colab_backend.UNITS_PER_HOUR
    assert cat["colab"]["usd_per_unit"] == colab_backend.USD_PER_UNIT
    assert cat["runpod"]["usd_per_hour"] == runpod_backend.USD_PER_HOUR
    assert cat["runpod"]["cpu_usd_per_vcpu_hour"] == runpod_backend.CPU_USD_PER_VCPU_HOUR
    assert cat["runpod"]["taken"] in runpod_backend.PRICES_TAKEN, "the date the built-in prices were taken"
    assert set(runpod_backend.USD_PER_HOUR) == set(runpod_backend.GPU_IDS), "every GPU it offers has a price"


def _catalog(tmp_path: Path, **modal) -> str:
    gpu = {**modal_backend.GPU_PER_S, **modal}
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps({"version": "2027-01-01", "modal": {"gpu_per_s": gpu}}))
    return p.as_uri()


def test_a_catalog_raises_and_adds_but_never_lowers(tmp_path):
    write_config(tmp_path, {"project": "p", "budget": {"max_run_usd": 100, "ask_above_usd": 100, "max_day_usd": 100},
                            "plans": {"h": {"backend": "modal", "gpu": "H100", "steps": [{"name": "a", "run": "true", "max_minutes": 60}]},
                                      "t": {"backend": "modal", "gpu": "T4", "steps": [{"name": "a", "run": "true", "max_minutes": 60}]}}})
    before = {k: budget.worst_case(C.load(tmp_path), k)["worst_case_usd"] for k in ("h", "t")}
    url = _catalog(tmp_path, H100=0.002, T4=0.00001, X1=0.003)            # H100 up, T4 down, a GPU it does not know
    r = cli(tmp_path, "prices", "--update", "--url", url)
    assert r.returncode == 0 and "H100: 0.001097 -> 0.002" in r.stdout and "X1: 0.003 (new)" in r.stdout
    assert "T4" not in r.stdout.split("\n")[0], "a lower price is not taken"
    assert "H100          $0.002000/s" in r.stdout and "(catalog 2027-01-01)" in r.stdout
    after = {k: budget.worst_case(C.load(tmp_path), k)["worst_case_usd"] for k in ("h", "t")}
    assert after["h"] > before["h"] and after["t"] == before["t"]
    write_config(tmp_path, {"project": "p", "pricing": {"modal_gpu_per_s": {"H100": 0.0009}},
                            "plans": {"x": {"backend": "modal", "gpu": "X1", "steps": [{"name": "a", "run": "true", "max_minutes": 10}]},
                                      "h": {"backend": "modal", "gpu": "H100", "steps": [{"name": "a", "run": "true", "max_minutes": 60}]}}})
    cfg = C.load(tmp_path)                                                 # X1 is allowed now, and priced
    assert budget.worst_case(cfg, "x")["usd_per_minute"] > 0.003 * 60
    assert abs(budget.worst_case(cfg, "h")["usd_per_minute"] - (0.0009 + 2 * 0.0000131 + 8 * 0.00000222) * 60) < 1e-9, \
        "the project's own pricing: still wins"


def test_a_bad_catalog_changes_nothing(tmp_path):
    write_config(tmp_path, {"project": "p", "plans": {"a": {"steps": [{"name": "a", "run": "true"}]}}})
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2, 3]")
    r = cli(tmp_path, "prices", "--update", "--url", bad.as_uri())
    assert r.returncode == 2 and "not a price catalog" in r.stdout and "unchanged" in r.stdout
    r = cli(tmp_path, "prices", "--update", "--url", (tmp_path / "missing.json").as_uri())
    assert r.returncode == 2 and "could not fetch" in r.stdout
    assert "none fetched" in cli(tmp_path, "prices").stdout
