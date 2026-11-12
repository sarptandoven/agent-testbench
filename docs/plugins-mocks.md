# Mock plug-ins

A step's `mocks:` loads strict stand-ins for paid SDKs, so the code around a paid call is tested without making it
([Mock clients](mocks.md)). Five are built in: `anthropic`, `openai`, `alpaca`, `ccxt` and `ib_async`. A package
can add more - for `stripe`, a vendor's SDK, an internal API - and a step then lists them the same way:

```yaml
steps:
  - name: weather app
    run: python app.py Paris London
    mocks: {weatherkit: {forecasts: tests/forecasts.yaml}}
    expect:
      mock_calls: {weatherkit: {calls: 2}}       # refused: 0 is checked unless said otherwise
```

The built-ins are plug-ins of the same kind (`agent_testbench.mocks.KIND`), so a plug-in can do anything they do,
but it cannot take a built-in's name. Like every mock, a plug-in never touches the network: `testbench plugin test`
checks that before anyone installs it.

## The interface

A mock plug-in is one `agent_testbench.mocks.Mock`, named by an entry point in the group `agent_testbench.mocks`:

```toml
# pyproject.toml
[project.entry-points."agent_testbench.mocks"]
weatherkit = "testbench_mock_example:mock"
```

| attribute | what it is |
|---|---|
| `name` | what a step's `mocks:` lists; the entry point's name |
| `path` | the folder (or a list of folders) whose packages go first on the step's `PYTHONPATH` |
| `imports` | the module names it stands in for: `["weatherkit"]` (the built-in `ib_async` has `["ib_async", "ib_insync"]`) |
| `validate(where, opts, root)` | the step's options for it, normalized, or a `ConfigError` naming the field; the default takes the options `options_schema` lists and checks that each file option exists |
| `files` | options that name a file in the project: checked to exist, and the step gets the full path |
| `call_keys` | the `mock_calls:` rule keys its calls are counted by: `calls` and `refused` (also `injected`, `input_tokens`, `output_tokens`, `tool_calls` if its log lines carry them, as the LLM clients' do) |
| `options_schema`, `calls_schema` | JSON Schema fragments for its options and its `mock_calls:` rules |
| `example` | a few lines a step would run that make one call it answers, with the client bound to `client` |

`mocks.call_keys(name)` gives a mock's rule keys, and `mocks.names()` every mock a step can list.

## Writing one

[examples/plugins/testbench-mock-example](../examples/plugins/testbench-mock-example) is a complete one in about 85
lines, docstrings included: a strict mock of `weatherkit`, a small fictional weather SDK, whose
`Client(api_key).forecast(city)` answers from a YAML file of canned forecasts and refuses anything else.

The package has two parts. What testbench loads, the `Mock`:

```python
mock = WeatherKit(                                   # a Mock subclass, to check the forecasts file's shape too
    name="weatherkit",
    path=Path(__file__).parent / "sdk",              # sdk/weatherkit/ goes first on the step's PYTHONPATH
    imports=["weatherkit"],
    files=["forecasts"],                             # relative to the project; the step gets the full path
    options_schema={"type": ["object", "null"], "additionalProperties": False,
                    "properties": {"forecasts": {"description": "A YAML file of canned forecasts", "type": "string"}}},
    example='import weatherkit\nclient = weatherkit.Client("any-key")\nclient.forecast("Paris")\n',
)
```

And what the step imports instead of the real SDK, `sdk/weatherkit/__init__.py`, built on `StrictAPI`:

```python
from _testbench_strict import Endpoint, StrictAPI, UnexpectedCall, step_options

FORECASTS = load(step_options("weatherkit").get("forecasts") or DEFAULT)
api = StrictAPI({"forecast": Endpoint(required={"city": tuple(FORECASTS)}, optional={"days": int},
                                      ranges={"days": (1, 7)})}, sdk="weatherkit")

class Client:
    def forecast(self, city, days=1):
        api.check("forecast", {"city": city, "days": days})      # logged, answered or refused
        return {"city": city, "days": FORECASTS[city][:days]}

    def __getattr__(self, name):                                 # radar(), alerts(): not modelled, so refused
        raise api.not_modelled(name, "Client.")
```

- `StrictAPI(..., sdk="weatherkit")` writes every call, answered or refused, as one line of the step's
  `mock_calls.jsonl` under the mock's name: the log `mock_calls:` rules count and the run keeps as evidence.
- `step_options(name)` reads the step's options for the mock (`TESTBENCH_MOCK_WEATHERKIT`, JSON, file options as
  full paths).
- `api.not_modelled(name)` is the error for a method the mock does not have: it names the method, is an
  `AttributeError`, and is logged as a refused call - so code that catches it and carries on still fails the step.
- The step's own code imports StrictAPI from `_testbench_strict`, a standard-library module in the folder every
  mocked step has on its `PYTHONPATH` (`agent_testbench.mocks` hands out the same objects). Run from uvx or pipx,
  testbench gives a step the project's Python, which may not have agent-testbench; the mock must still import there.
  For the same reason, read data files as JSON where you can (a YAML file needs PyYAML in the step's Python).

## Installing it

`pip install` the package where testbench runs (with uvx: `uvx --with testbench-mock-example agent-testbench ...`).
For a plan on another machine (docker, ssh, kubernetes, modal), install it there too, the way the plan's other
packages are. `testbench plugins` lists it; a plug-in that fails to load is listed with the reason, and a step that
names it says so.

Editors accept a plug-in mock's name and options in testbench.yaml (the schema cannot know its options, so it does
not complete them); testbench itself checks them with the mock's `validate` before anything runs.

## The contract

`testbench plugin test` (and `plugins.check("mocks", mock)` from Python) checks a mock before anyone installs it.
Each problem is said in plain words:

- its `path` exists and holds a package for each name in `imports`;
- importing them and running its `example` in a fresh Python, with the network cut and no `agent_testbench` to
  import, opens no connection;
- `validate` refuses an option it does not have with a `ConfigError` naming the field, and takes no options at all;
- the example's call is a line of `mock_calls.jsonl` under the mock's name;
- calling something it does not model (`client.testbench_unmodelled_call()`) raises an error naming the method,
  never a silent `None`;
- its `call_keys` include `calls` and `refused`, and only keys testbench counts.

The five built-in mocks keep the same contract (`tests/test_mocks_plugins.py`).
