r"""MkDocs hook: builds the site from the same Markdown files people read on GitHub and PyPI.

Three things, all at build time; no file in the repository is changed:

- A line `--8<-- "PATH"` in a page (the notation of pymdownx.snippets) is replaced by the file at PATH, relative
  to the repository root. `--8<-- "PATH#anchor"` takes one section of it: the part under the heading whose anchor
  is `anchor`, up to the next heading of the same or a higher level (the heading line itself is left out, so the
  page gives the section its own heading). This is how README.md becomes the home page without being copied.
- Links are written for GitHub: relative to the file they are in, which for README.md is the repository root.
  On the site, a link to a file that is a page of the site goes to that page; a link to any other file or
  folder of the repository goes to it on GitHub; web links are kept. So README.md's `docs/docker.md` becomes
  `docker.md`, and `src/agent_testbench/schema/report-v1.schema.json` a link to that file on GitHub.
- In a table, GitHub needs `\|` for a pipe inside code (`html\|junit`); Python-Markdown shows the backslash, so it
  is dropped there (Python-Markdown already keeps a pipe inside code from splitting the cell).

A section or file that is not there stops the build with a message that names it.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from markdown.extensions.toc import slugify
from mkdocs.exceptions import PluginError

BRANCH = "main"                                                       # what GitHub links point at
INCLUDE = re.compile(r'^[ \t]*--8<--[ \t]+"([^"#]+)(?:#([^"]+))?"[ \t]*$', re.M)
FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
CODE = re.compile(r"(`+)(.+?)\1")
LINK = re.compile(r"(!?\[[^\]\n]*\])\(([^)\s]+)((?:[ \t]+\"[^\"\n]*\")?)\)")
SCHEME = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|//)")

_site: dict = {}                      # filled by on_files: repo root, docs folder, pages, files included whole


def on_files(files, config):
    root = Path(config.config_file_path).resolve().parent
    docs = Path(config.docs_dir).resolve()
    pages = {f.src_uri for f in files.documentation_pages()}
    whole = {}                                                        # repo path of a file included whole -> page
    for uri in pages:
        for m in INCLUDE.finditer((docs / uri).read_text(encoding="utf-8")):
            if not m.group(2):
                whole.setdefault(m.group(1), uri)
    _site.update(root=root, docs=docs.relative_to(root).as_posix(), pages=pages, whole=whole,
                 github=config.repo_url.rstrip("/"))
    return files


def on_page_markdown(markdown, page, config, files):
    uri = page.file.src_uri
    own = f"{_site['docs']}/{uri}"

    def include(m):
        path, anchor = m.group(1).strip(), m.group(2)
        src = _site["root"] / path
        if not src.is_file():
            raise PluginError(f"{own} includes {path}, which is not a file in the repository (include paths are "
                              "relative to the repository root)")
        text = src.read_text(encoding="utf-8")
        if anchor:
            text = _section(text, anchor, f"{own}: {path}#{anchor}")
        return _for_site(text, path, uri)

    return INCLUDE.sub(include, _for_site(markdown, own, uri))


def _fence(line: str, fence: str | None) -> tuple[bool, str | None]:
    """Whether `line` opens or closes a code fence, and the fence open after it (None outside code)."""
    m = FENCE.match(line)
    if not m:
        return False, fence
    if fence is None:
        return True, m.group(1)
    closes = m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence)
    return True, None if closes else fence


def _section(text: str, anchor: str, where: str) -> str:
    lines, start, level, fence = text.splitlines(), None, 0, None
    for i, line in enumerate(lines):
        is_fence, fence = _fence(line, fence)
        h = None if is_fence or fence else HEADING.match(line)
        if not h:
            continue
        if start is None and slugify(h.group(2), "-") == anchor:
            start, level = i + 1, len(h.group(1))
        elif start is not None and len(h.group(1)) <= level:
            return "\n".join(lines[start:i]).strip("\n") + "\n"
    if start is None:
        raise PluginError(f"{where}: no heading with the anchor #{anchor} in that file")
    return "\n".join(lines[start:]).strip("\n") + "\n"


def _for_site(text: str, source: str, page_uri: str) -> str:
    """`text`, written for GitHub in the repository file `source`, as it should read in the site's page `page_uri`."""
    out, fence = [], None
    for line in text.splitlines(keepends=True):
        is_fence, fence = _fence(line, fence)
        if not is_fence and not fence:
            line = LINK.sub(lambda m: f"{m.group(1)}({_target(m.group(2), source, page_uri)}{m.group(3)})", line)
            if line.lstrip().startswith("|"):
                line = CODE.sub(lambda m: m.group(1) + m.group(2).replace("\\|", "|") + m.group(1), line)
        out.append(line)
    return "".join(out)


def _target(target: str, source: str, page_uri: str) -> str:
    if SCHEME.match(target):
        return target
    path, _, anchor = target.partition("#")
    anchor = f"#{anchor}" if anchor else ""
    repo_path = posixpath.normpath(posixpath.join(posixpath.dirname(source), path)) if path else source
    if repo_path.startswith("../"):
        return target                                                 # outside the repository: leave it to MkDocs
    docs, here = _site["docs"] + "/", posixpath.dirname(page_uri) or "."
    site_page = _site["whole"].get(repo_path)
    if site_page is None and repo_path.startswith(docs) and repo_path[len(docs):] in _site["pages"]:
        site_page = repo_path[len(docs):]
    if site_page is not None:
        if site_page == page_uri:
            return anchor or posixpath.basename(page_uri)
        return posixpath.relpath(site_page, here) + anchor
    if repo_path.startswith(docs) and not (_site["root"] / repo_path).is_dir():   # an image or file of the site
        return posixpath.relpath(repo_path[len(docs):], here) + anchor
    kind = "tree" if (_site["root"] / repo_path).is_dir() else "blob"
    return f"{_site['github']}/{kind}/{BRANCH}/{repo_path}{anchor}"
