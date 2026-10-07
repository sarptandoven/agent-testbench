"""A web UI checked the way a person would: open, click, read, look - and a broken UI caught."""
import json
import shutil

import pytest

from conftest import EXAMPLES, chromium_ready, cli

pytestmark = [pytest.mark.web, pytest.mark.skipif(not chromium_ready(), reason="needs `playwright install chromium`")]


@pytest.fixture
def ui(tmp_path):
    dest = tmp_path / "web-ui"
    shutil.copytree(EXAMPLES / "web-ui", dest, ignore=shutil.ignore_patterns(".testbench"))
    return dest


def test_ui_check_passes_and_keeps_screenshots(ui):
    r = cli(ui, "run", "ui-check", "--wait", "120")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "page shows 'Count: 2'" in r.stdout and "no console or page errors" in r.stdout
    run = sorted((ui / ".testbench" / "runs").iterdir())[-1]
    shots = {p.name for p in (run / "steps" / "01-counter-works").glob("*.png")}
    assert shots == {"after-two-clicks.png", "page.png"}


def test_a_broken_ui_is_caught_with_the_reason(ui):
    js = ui / "site" / "app.js"
    js.write_text(js.read_text().replace("count += 1;", "count += 1; undefinedHelper();"))
    r = cli(ui, "run", "ui-check", "--wait", "120")
    assert r.returncode == 1
    assert "page shows 'Count: 2'" in r.stdout and "not found" in r.stdout
    assert "undefinedHelper is not defined" in r.stdout, "the page error must reach the report"


def test_a_missing_button_fails_the_action(ui):
    html = ui / "site" / "index.html"
    html.write_text(html.read_text().replace('id="increment"', 'id="add"'))
    r = cli(ui, "run", "ui-check", "--wait", "180")
    assert r.returncode == 1 and "action 1" in r.stdout and "#increment" in r.stdout
