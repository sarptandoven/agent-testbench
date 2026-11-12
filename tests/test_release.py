"""What a release needs: one version everywhere it is written, a PyPI description whose links all resolve, the MCP
Registry entry matching the package, and a release workflow that publishes only from a published GitHub release.

Standard library, pytest and pyyaml only, and no import of agent_testbench: the release workflow runs this file on
its own (`pytest --noconftest tests/test_release.py`) with TESTBENCH_RELEASE_TAG set to the release's tag, which
also requires the tag to name the version and the changelog to give the release a date."""

import json
import os
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
GITHUB = "https://github.com/sarptandoven/agent-testbench"
SERVER_NAME = "io.github.sarptandoven/agent-testbench"


def _toml():
    try:
        import tomllib
    except ImportError:                                    # Python 3.10
        tomllib = pytest.importorskip("tomli")
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))


def _version() -> str:
    m = re.search(r'^version = "([^"]+)"$', (REPO / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    assert m, "pyproject.toml has no `version = \"...\"` line under [project]"
    return m.group(1)


def _json(rel: str):
    return json.loads((REPO / rel).read_text(encoding="utf-8"))


def _pin(spec: str) -> str:
    """The version an `agent-testbench[all]==X` spec pins."""
    m = re.fullmatch(r"agent-testbench(?:\[[\w,]+\])?==(\S+)", spec)
    assert m, f"{spec!r} does not pin agent-testbench to one version (agent-testbench[all]==X)"
    return m.group(1)


def test_one_version_everywhere():
    v = _version()
    init = (REPO / "src/agent_testbench/__init__.py").read_text(encoding="utf-8")
    server = _json("server.json")
    market = [p for p in _json(".claude-plugin/marketplace.json")["plugins"] if p["name"] == "agent-testbench"]
    cursor_args = _json(".cursor-plugin/mcp.json")["mcpServers"]["agent-testbench"]["args"]
    places = {
        "src/agent_testbench/__init__.py __version__": re.search(r'^__version__ = "([^"]+)"', init, re.M).group(1),
        ".claude-plugin/plugin.json version": _json(".claude-plugin/plugin.json")["version"],
        ".claude-plugin/marketplace.json plugins[agent-testbench].version": market[0]["version"],
        ".cursor-plugin/plugin.json version": _json(".cursor-plugin/plugin.json")["version"],
        ".cursor-plugin/mcp.json pin in args": _pin(next(a for a in cursor_args if a.startswith("agent-testbench"))),
        "server.json version": server["version"],
        "server.json packages[0].version": server["packages"][0]["version"],
        "server.json packages[0].runtimeArguments pin": _pin(next(
            a["value"] for a in server["packages"][0].get("runtimeArguments", []) if a.get("name") == "--with")),
    }
    wrong = {where: found for where, found in places.items() if found != v}
    assert not wrong, (f"pyproject.toml says {v}, but these say otherwise - set each to {v} "
                       f"(docs/publishing.md lists every place): {wrong}")
    heading = re.search(r"^## (.+)$", (REPO / "CHANGELOG.md").read_text(encoding="utf-8"), re.M).group(1)
    assert heading.split(" - ")[0] == v, f"CHANGELOG.md's first release heading is {heading!r}; it should be for {v}"


def test_release_tag_names_the_version_and_the_changelog_has_its_date():
    tag = os.environ.get("TESTBENCH_RELEASE_TAG")
    if not tag:
        pytest.skip("only in the release workflow (TESTBENCH_RELEASE_TAG is the release's tag)")
    v = _version()
    assert tag == f"v{v}", f"the release's tag is {tag!r} but the version is {v}: tag the release v{v}, or fix the version"
    heading = re.search(r"^## (.+)$", (REPO / "CHANGELOG.md").read_text(encoding="utf-8"), re.M).group(1)
    assert re.fullmatch(rf"{re.escape(v)} - \d{{4}}-\d{{2}}-\d{{2}}", heading), (
        f"CHANGELOG.md's heading for {v} is {heading!r}: give it the release date (`## {v} - YYYY-MM-DD`)")


def _pypi_description() -> str:
    """README.md as PyPI will show it: the fragments and substitutions of pyproject.toml's fancy-pypi-readme hook."""
    hook = _toml()["tool"]["hatch"]["metadata"]["hooks"]["fancy-pypi-readme"]
    text = "".join((REPO / f["path"]).read_text(encoding="utf-8") for f in hook["fragments"])
    for sub in hook.get("substitutions", []):
        text = re.sub(sub["pattern"], sub["replacement"], text)
    return text


def _github_anchor(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def test_pypi_description_links_all_resolve():
    text = _pypi_description()
    links = re.findall(r"\]\(([^)\s]+)\)", text) + re.findall(r'<img[^>]*\ssrc="([^"]+)"', text)
    relative = [x for x in links if not re.match(r"(https?:|mailto:|#)", x)]
    assert not relative, f"these links would point at pypi.org on PyPI; add a substitution in pyproject.toml: {relative}"
    for link in links:
        m = re.match(rf"{re.escape(GITHUB)}/(blob|tree)/main/([^#]+)(?:#(.+))?$", link)
        if not m:
            continue
        kind, path, anchor = m.groups()
        target = REPO / path
        assert (target.is_file() if kind == "blob" else target.is_dir()), f"{link}: there is no such {kind} in the repository"
        if anchor and target.suffix == ".md":
            headings = re.findall(r"^#{1,6} (.+?)\s*$", target.read_text(encoding="utf-8"), re.M)
            assert anchor in {_github_anchor(h) for h in headings}, f"{link}: {path} has no heading #{anchor}"
    images = re.findall(r'<img[^>]*\ssrc="([^"]+)"', text) + re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", text)
    for src in images:                                     # raw files on main: each must be in the repository
        m = re.match(r"https://raw\.githubusercontent\.com/sarptandoven/agent-testbench/main/(.+)$", src)
        if m:
            assert (REPO / m.group(1)).is_file(), f"README.md shows {src}, which is not a file in the repository"


def test_pypi_description_keeps_the_mcp_registry_token():
    """The MCP Registry proves the PyPI package is this server's by finding `mcp-name: NAME` in its description."""
    server = _json("server.json")
    assert server["name"] == SERVER_NAME
    assert re.search(rf"mcp-name: {re.escape(SERVER_NAME)}(\s|-->|<|$)", _pypi_description()), (
        f"the PyPI description (README.md) lost `<!-- mcp-name: {SERVER_NAME} -->`; the registry refuses to publish without it")


def test_server_json_matches_the_package():
    server, project = _json("server.json"), _toml()["project"]
    pkg = server["packages"][0]
    assert pkg["registryType"] == "pypi" and pkg["identifier"] == project["name"]
    assert pkg["transport"] == {"type": "stdio"} and [a["value"] for a in pkg["packageArguments"]] == ["mcp"]
    assert "registryBaseUrl" not in pkg, "leave registryBaseUrl out: the registry fills in https://pypi.org"
    assert len(server["description"]) <= 100, "the registry's schema allows at most 100 characters of description"
    assert "agent-testbench" in project["scripts"], "clients run the package's name as the command (uvx agent-testbench@X)"


def test_release_workflow_publishes_only_a_published_release_with_pinned_actions():
    wf = yaml.safe_load((REPO / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    on = wf.get("on", wf.get(True))                        # YAML 1.1 reads a bare `on` key as True
    assert on == {"release": {"types": ["published"]}}, f"release.yml must run only on a published release, not {on}"
    assert wf.get("permissions") == {"contents": "read"}
    uses = [s["uses"] for job in wf["jobs"].values() for s in job.get("steps", []) if "uses" in s]
    loose = [u for u in uses if not re.fullmatch(r"[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}", u)]
    assert uses and not loose, f"pin every action to a full commit SHA (with the tag in a comment): {loose}"
    publish = [job for job in wf["jobs"].values() if any("gh-action-pypi-publish" in s.get("uses", "")
                                                         for s in job.get("steps", []))]
    assert len(publish) == 1, "one job publishes"
    job = publish[0]
    # PyPI's trusted publisher for this project names workflow release.yml and environment pypi (docs/publishing.md)
    env = job["environment"]["name"] if isinstance(job["environment"], dict) else job["environment"]
    assert env == "pypi" and job["permissions"] == {"id-token": "write"}
    assert "github.event_name == 'release'" in job.get("if", "")
