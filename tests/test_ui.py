"""testbench ui: static pages for the project's runs - an index with a trend line per checked number, and a
self-contained page per run."""
import re

from conftest import cli


def test_ui_builds_an_index_and_a_page_per_run(plain):
    assert cli(plain, "run", "quick", "--wait", "120").returncode == 0
    nb = plain / "nb.ipynb"
    nb.write_text(nb.read_text().replace('N = 3 #@param', 'N = 4 #@param'))
    cli(plain, "run", "quick", "--wait", "120")
    r = cli(plain, "ui")
    assert r.returncode == 0 and "wrote" in r.stdout
    index = (plain / ".testbench" / "ui" / "index.html").read_text()
    links = re.findall(r"href='\.\./runs/([^/']+)/report\.html'", index)
    assert len(set(links)) == 2 and all((plain / ".testbench" / "runs" / x / "report.html").exists() for x in links)
    assert "<svg" in index and "3 &rarr; 4" in index, "a trend line for n"
    assert "quick" in index and "PASS" in index and "FAIL" in index
