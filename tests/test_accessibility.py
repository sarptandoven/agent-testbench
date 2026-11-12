"""Everything testbench renders as HTML reads without sight and without a mouse: the report page (with its tear
sheet), `testbench ui`'s pages, the live dashboard and a shared page. Headings in order with landmarks, tables with
headers and their scope, alt text naming each evidence image's step and file, a visible focus, text at 4.5:1 or more
in light and dark (computed from the CSS variables), results in words as well as colours, and a run list a keyboard
can walk while the page updates."""
import base64
import html.parser
import re
import sys
import threading

import pytest

from agent_testbench import config as C, dashboard, report as R, share as S
from agent_testbench.trading import tearsheet as T
from conftest import PNG_1PX, chromium_ready, cli, write_config
from test_tearsheet import backtest, contrast

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
STATUS = {"passed", "failed", "error", "timeout", "cancelled", "running", "queued", "lost", "PASS", "FAIL", "ERROR",
          "TIMEOUT", "CANCELLED"}
STEP = """\
import base64, pathlib
out = pathlib.Path("out")
out.mkdir(exist_ok=True)
(out / "plot.png").write_bytes(base64.b64decode({png!r}))
for name, text in {files!r}.items():
    (out / name).write_text(text)
(out / "m.json").write_text('{{"score": {score}}}')
print("done")
"""


class Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.children = tag, attrs, parent, []

    def text(self) -> str:
        if self.tag in ("style", "script"):
            return ""
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)

    def walk(self):
        for c in self.children:
            if isinstance(c, Node):
                yield c
                yield from c.walk()


class Doc(html.parser.HTMLParser):
    """A small DOM: enough to ask what a screen reader or a keyboard meets."""

    def __init__(self, page: str):
        super().__init__(convert_charrefs=True)
        self.root = self.cur = Node("#document", {}, None)
        self.feed(page)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs), self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, dict(attrs), self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)

    def all(self, tag=None):
        return [n for n in self.root.walk() if tag is None or n.tag == tag]


def check_page(name: str, page: str, *, whole: bool = True) -> Doc:
    """What every page testbench renders keeps to; whole=False for a fragment the dashboard swaps in."""
    doc = Doc(page)
    if whole:
        assert re.search(r"<html lang=\w+", page), f"{name}: no language"
        assert doc.all("title") and doc.all("title")[0].text().strip(), f"{name}: no title"
        assert len(doc.all("main")) == 1, f"{name}: one main landmark"
    heads = [int(n.tag[1]) for n in doc.all() if re.fullmatch(r"h[1-6]", n.tag)]
    assert heads.count(1) == 1 and heads[0] == 1, f"{name}: one h1, first: {heads}"
    for before, after in zip(heads, heads[1:]):
        assert after <= before + 1, f"{name}: the headings skip a level (h{before} then h{after}): {heads}"
    for th in doc.all("th"):
        assert th.attrs.get("scope") in ("col", "row"), f"{name}: a th without scope: {th.text()!r}"
        assert th.text().strip(), f"{name}: an empty table header"
    for img in doc.all("img"):
        assert (img.attrs.get("alt") or "").strip(), f"{name}: an image without alt text: {img.attrs.get('src')!r:.60}"
    for svg in doc.all("svg"):
        if svg.attrs.get("role") == "img":
            assert (svg.attrs.get("aria-label") or "").strip(), f"{name}: a chart without a label"
    for a in doc.all("a"):
        assert a.attrs.get("href"), f"{name}: a link with nowhere to go"
        assert a.text().strip() or a.attrs.get("aria-label") or any(i.attrs.get("alt") for i in a.walk()), \
            f"{name}: a link with no words: {a.attrs['href']}"
    for n in doc.all():
        assert int(n.attrs.get("tabindex") or 0) <= 0, f"{name}: tabindex above 0 breaks the reading order"
        classes = set((n.attrs.get("class") or "").split())
        if classes & STATUS and n.tag not in ("tr", "table"):
            assert n.text().strip(), f"{name}: a result shown by colour alone ({classes})"
    assert "<form" not in page and "<button" not in page if name.startswith("dashboard") else True
    return doc


def css_vars(css: str) -> tuple[dict, dict]:
    """The light and the dark values of the CSS variables of report.CSS, as #rrggbb."""
    def colors(block):
        found = re.findall(r"--([\w-]+):#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b", block)
        return {k: "#" + (v if len(v) == 6 else "".join(c * 2 for c in v)) for k, v in found}
    light = colors(css.split("@media")[0])
    dark = colors(re.search(r"@media \(prefers-color-scheme:dark\)\{:root\{([^}]*)\}", css).group(1))
    assert {"fg", "bg", "card", "muted", "code"} <= set(light) and set(light) == set(dark), "every colour in both"
    return light, dark


def test_text_is_readable_in_light_and_dark():
    for mode, v in zip(("light", "dark"), css_vars(R.CSS)):
        for fg in ("fg", "muted", "pass", "fail", "warn", "link"):
            for bg in ("bg", "card"):
                ratio = contrast(v[fg], v[bg])
                assert ratio >= 4.5, f"{mode}: --{fg} {v[fg]} on --{bg} {v[bg]} is {ratio:.2f}:1, under 4.5:1"
        assert contrast(v["fg"], v["code"]) >= 4.5, f"{mode}: code blocks"
        ink, page = T.INK[mode], v["bg"]
        text = T.COLORS[mode]["text"]
        for fg, bg in ((text, ink["surface"]), (text, ink["caveat"]), (text, page), (ink["muted"], ink["surface"]),
                       (ink["muted"], page)):
            assert contrast(fg, bg) >= 4.5, f"{mode} tear sheet: {fg} on {bg} is {contrast(fg, bg):.2f}:1"
    for css in (R.CSS, dashboard.CSS, T.css()):
        assert not re.search(r"outline:\s*(none|0)\b", css), "the focus ring is never taken away"
    assert re.search(r":focus-visible\{outline:2px solid var\(--link\)", R.CSS), "a focus ring to see"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "a11y"
    root.mkdir()
    data = backtest(tmp_path / "data")
    files = {p.name: p.read_text() for p in data.iterdir()}
    for name, score in (("good", 3), ("bad", 1)):
        (root / f"{name}.py").write_text(STEP.format(png=base64.b64encode(PNG_1PX).decode(), files=files,
                                                     score=score))
    py = f'"{sys.executable}"'
    write_config(root, {"project": "a11y", "plans": {
        name: {"steps": [{"name": f"backtest <{name}>", "run": f"{py} {name}.py", "max_minutes": 2,
                          "artifacts": ["out/*"], "expect": {"json": {"out/m.json": {"score": ">= 2"}}}}]}
        for name in ("good", "bad")}})
    assert cli(root, "run", "good", "--wait", "120").returncode == 0
    assert cli(root, "run", "bad", "--wait", "120").returncode == 1
    return root


def test_every_page_has_headings_landmarks_table_headers_and_alt_text(project, tmp_path):
    runs = sorted((project / ".testbench" / "runs").iterdir())
    for run in runs:
        rep = R.load(run)
        doc = check_page(f"report {rep['status']}", R.render_html(rep, run))
        alts = [i.attrs["alt"] for i in doc.all("img")]
        assert f"{rep['steps'][0]['name']}: artifacts/out/plot.png" in alts, "the step and the file"
        assert "<section class=ts>" in R.render_html(rep, run), "with its tear sheet"
        page, _ = T.run_page(rep, run)
        check_page("tear sheet", page)
    assert cli(project, "ui").returncode == 0
    check_page("ui index", (project / ".testbench" / "ui" / "index.html").read_text())

    d = dashboard.Dashboard(project)
    for path in ("/", f"/runs/{runs[0].name}/", f"/runs/{runs[1].name}/"):
        code, body, _, _ = dashboard.route(d, path)
        assert code == 200, path
        doc = check_page(f"dashboard {path}", body)
        nav = doc.all("nav")
        assert len(nav) == 1 and nav[0].attrs.get("aria-label"), "the dashboard's own links are a named landmark"
    check_page("dashboard update", "<main>" + dashboard.overview(d) + "</main>", whole=False)

    shared = tmp_path / "shared"
    shared.mkdir()
    S.build(project, C.load(project), next(r for r in runs if R.load(r)["status"] == "failed"), shared)
    doc = check_page("shared page", (shared / "index.html").read_text())
    assert "testbench: 1 of 2 checks failed" in [i.attrs["alt"] for i in doc.all("img")], "the badge in words"


def test_the_dashboard_run_list_is_walked_with_a_keyboard(project):
    d = dashboard.Dashboard(project)
    doc = Doc("<main>" + dashboard.overview(d) + "</main>")
    recent = next(t for t in doc.all("table") if any(th.text() == "started" for th in t.walk() if th.tag == "th"))
    links = [a.attrs["href"] for a in recent.walk() if a.tag == "a"]
    names = sorted((p.name for p in (project / ".testbench" / "runs").iterdir()), reverse=True)
    assert links == [f"/runs/{n}/" for n in names], "each run is a link, newest first, in reading order"
    assert not any(n.attrs.get("onclick") for n in doc.all()), "nothing only a mouse can reach"
    assert "swap(d)" in dashboard.SCRIPT and "swap(d.head)" in dashboard.SCRIPT and ".focus(" in dashboard.SCRIPT, \
        "an update keeps the keyboard's place"


@pytest.mark.skipif(not chromium_ready(), reason="needs Chromium for Playwright (playwright install chromium)")
def test_focus_stays_on_a_run_link_while_the_overview_updates(project):
    from playwright.sync_api import sync_playwright
    server = dashboard.make_server(project, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
            for _ in range(40):
                page.keyboard.press("Tab")
                href = page.evaluate("document.activeElement.getAttribute('href') || ''")
                if href.startswith("/runs/"):
                    break
            assert href.startswith("/runs/"), "Tab reaches a run"
            page.evaluate("window.__old = document.querySelector('#live h1')")
            page.wait_for_function("() => !window.__old.isConnected", timeout=15000)
            assert page.evaluate("document.activeElement.getAttribute('href')") == href, "focus kept"
            page.keyboard.press("Enter")
            page.wait_for_url(f"**{href}", timeout=15000)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
