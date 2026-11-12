"""testbench-mock-example: a strict mock of weatherkit (a small, fictional weather SDK) as an agent-testbench plug-in.
Installed, a step can list it like a built-in mock:

    mocks: {weatherkit: {forecasts: tests/forecasts.yaml}}
    expect: {mock_calls: {weatherkit: {calls: "<= 3"}}}
"""
from pathlib import Path

import yaml

from agent_testbench.config import ConfigError
from agent_testbench.mocks import Mock


class WeatherKit(Mock):
    def validate(self, where, opts, root):
        opts = super().validate(where, opts, root)       # only the options below; forecasts: a file that exists
        if opts.get("forecasts"):
            try:
                data = yaml.safe_load((Path(root) / opts["forecasts"]).read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise ConfigError(f"`{where}.forecasts` ({opts['forecasts']}) is not YAML: {exc}") from None
            if not isinstance(data, dict) or not all(isinstance(days, list) for days in data.values()):
                raise ConfigError(f"`{where}.forecasts`: {opts['forecasts']} maps each city to its days, e.g. "
                                  "{Paris: [{summary: sunny, high_c: 21, low_c: 12}]}")
        return opts


mock = WeatherKit(
    name="weatherkit",
    path=Path(__file__).parent / "sdk",                  # sdk/weatherkit/ goes first on the step's PYTHONPATH
    imports=["weatherkit"],
    files=["forecasts"],                                 # relative to the project; the step gets the full path
    options_schema={"type": ["object", "null"], "additionalProperties": False, "properties": {
        "forecasts": {"description": "A YAML file of canned forecasts: {city: [{summary, high_c, low_c}, ...]}",
                      "type": "string"}}},
    example='import weatherkit\nclient = weatherkit.Client("any-key")\nclient.forecast("Paris")\n',
)
