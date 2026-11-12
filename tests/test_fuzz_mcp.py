"""Every MCP tool whose arguments reach the command line, called with generated values (names that start with '-',
hold '=', newlines or any unicode, numbers that are negative, huge, NaN or infinite): no value ever becomes an option
of the command it runs. Each command line is also read by testbench's own parser, which must never see --approve
unless the call said approved_by_person=true. TESTBENCH_FUZZ_EXAMPLES=N runs N examples (tests/trust_helpers.py)."""
import asyncio
import contextlib
import inspect
import io
import typing

import pytest

pytest.importorskip("hypothesis")
pytest.importorskip("mcp")
from hypothesis import given, strategies as st  # noqa: E402

from agent_testbench import mcp_server as M  # noqa: E402
from agent_testbench.cli import build_parser  # noqa: E402
from trust_helpers import fuzz  # noqa: E402

TOOLS = {name: fn for name, fn in vars(M).items() if inspect.iscoroutinefunction(fn) and not name.startswith("_")
         and "project_dir" in inspect.signature(fn).parameters}
OUR_OPTIONS = {"--detach", "--approve", "--timeout", "--json", "--plan", "-s", "--backend", "--gpu", "--idle-minutes",
               "--max-minutes", "--cells", "--params", "--fix", "--no-run", "--metric", "--budget-usd", "--goal",
               "--max-hours", "--min-delta", "--target", "--patience", "--max-tries", "--wait", "--new-direction",
               "--to", "--all", "-n"}
WORDS = ["--approve", "-", "--", "-s", "--all", "--json", "=", "--to=x", "\n", "\n--approve", " ", "\x1b", "é", "😀", "‮",
         "main", "last~1", "a:b", "../x", "-1", "1e5", "nan", ".", "--help", "-h", "$(id)", "'", '"', ";"]
text = st.lists(st.one_of(st.sampled_from(WORDS), st.text(max_size=4)), max_size=4).map("".join)
numbers = st.one_of(st.integers(-10 ** 6, 10 ** 6), st.floats(), st.sampled_from([-1e-05, -0.5, 1e300, 10 ** 30]))


def strategy(annotation):
    """Values for one argument of a tool, by its type."""
    if annotation is bool:
        return st.booleans()
    if annotation is int:
        return st.one_of(st.integers(-10 ** 6, 10 ** 6), st.sampled_from([-1, 0, 2 ** 63]))
    if annotation is float:
        return numbers
    if annotation is str:
        return text
    args = typing.get_args(annotation)
    if float in args:
        return st.one_of(st.none(), numbers)
    return st.one_of(st.none(), st.lists(text, max_size=3))          # list[str] | None


def calls():
    @st.composite
    def one(draw):
        name = draw(st.sampled_from(sorted(TOOLS)))
        params = inspect.signature(TOOLS[name]).parameters
        hints = typing.get_type_hints(TOOLS[name])
        kwargs = {p: draw(strategy(hints[p])) for p in params if p not in ("ctx", "project_dir")}
        return name, kwargs
    return one()


PARSER = []


def options(argv: list[str]) -> list[str]:
    """What testbench's parser reads as options: the words before `--` that start with -."""
    return [a for a in (argv[:argv.index("--")] if "--" in argv else argv) if a.startswith("-")]


def check_argv(argv: list[str], plain: list[str], approved: bool) -> None:
    """argv: the command line from the generated values; plain: from the same call with each value replaced by x."""
    assert options(argv).count("--approve") <= approved, f"--approve that approved_by_person did not ask for: {argv}"
    for a in options(argv):
        option, has_value, _ = a.partition("=")
        assert option in OUR_OPTIONS, f"{a!r} would be read as an option: {argv}"
        assert has_value or options(argv).count(a) <= options(plain).count(a), f"a value became {a!r}: {argv}"
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        try:
            PARSER[:] = PARSER or [build_parser()]
            ns = PARSER[0].parse_args(argv)
        except SystemExit:
            return                                       # a value testbench refuses: nothing would run
    assert approved or not getattr(ns, "approve", False), f"--approve without approved_by_person: {argv}"


@fuzz(400)
@given(calls())
def test_no_tool_argument_becomes_an_option(tmp_path, monkeypatch, call):
    name, kwargs = call
    seen = []

    async def fake_cli(project_dir, argv, on_line=None, env=None):
        seen.append(list(argv))
        return 0, "ok"
    monkeypatch.setattr(M, "_cli", fake_cli)
    try:
        asyncio.run(TOOLS[name](project_dir=str(tmp_path), **kwargs))
    except (ValueError, M.ToolError):
        assert not seen, "a refused name runs nothing"
        return
    got, seen[:] = list(seen), []
    plain = {k: ("x" if v else "") if isinstance(v, str) else ["x"] * len(v) if isinstance(v, list) else v
             for k, v in kwargs.items()}
    asyncio.run(TOOLS[name](project_dir=str(tmp_path), **plain))
    for argv, same in zip(got, seen):
        check_argv(argv, same, bool(kwargs.get("approved_by_person")))


class Person:
    """A client that can ask the person, who says no."""

    def __init__(self):
        import types
        self.client_capabilities = types.SimpleNamespace(elicitation=object())
        self.asked = []

    async def elicit(self, message, schema):
        import types
        self.asked.append(message)
        return types.SimpleNamespace(action="accept", data=schema(approve=False))


@fuzz(60)
@given(st.sampled_from(["run_plan", "session_start", "loop_start"]))
def test_approved_by_person_never_skips_asking_a_person_who_can_be_asked(tmp_path, monkeypatch, tool):
    seen = []

    async def fake_cli(project_dir, argv, on_line=None, env=None):
        seen.append(list(argv))
        return 2, "Needs approval: plan x could cost up to $9.00 (above ask_above_usd). Ask a person; only they..."
    monkeypatch.setattr(M, "_cli", fake_cli)
    person = Person()
    args = {"run_plan": {"plan": "x"}, "session_start": {"name": "s"},
            "loop_start": {"plan": "x", "metric": "m.json:k", "budget_usd": 9.0}}[tool]
    out = asyncio.run(TOOLS[tool](project_dir=str(tmp_path), approved_by_person=True, ctx=person, **args))
    assert len(person.asked) == 1, "the person is asked, whatever the agent said they approved"
    assert all("--approve" not in argv for argv in seen), seen
    assert "said no" in str(out)
