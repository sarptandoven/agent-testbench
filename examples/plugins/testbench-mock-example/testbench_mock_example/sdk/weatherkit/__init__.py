"""weatherkit, as a strict mock. The real weatherkit (a small, fictional weather SDK) is used like this:

    import weatherkit
    client = weatherkit.Client(api_key="...")
    client.forecast("Paris", days=3)    # {"city": "Paris", "days": [{"summary", "high_c", "low_c"}, ...]}

This stand-in answers forecast() from a YAML (or JSON) file of canned forecasts - the step's `mocks: {weatherkit:
{forecasts: FILE}}`, else the one shipped with it - refuses a city that file lacks and every other method, and logs
each call to the step's mock_calls.jsonl. It never touches the network.
"""
import json
from pathlib import Path

# StrictAPI, from the folder testbench puts on every mocked step's PYTHONPATH (agent_testbench.mocks hands out the
# same): the step's Python may not have agent-testbench, from uvx or pipx it is the project's.
from _testbench_strict import Endpoint, StrictAPI, UnexpectedCall, step_options

TESTBENCH_MOCK = True
FILE = Path(step_options("weatherkit").get("forecasts") or Path(__file__).parents[2] / "forecasts.json")
try:
    FORECASTS = json.loads(FILE.read_text(encoding="utf-8"))
except ValueError:
    import yaml                                          # a YAML file needs PyYAML where the step runs
    FORECASTS = yaml.safe_load(FILE.read_text(encoding="utf-8"))
api = StrictAPI({"forecast": Endpoint(required={"city": tuple(FORECASTS)}, optional={"days": int},
                                      ranges={"days": (1, 7)})}, sdk="weatherkit")


class WeatherKitError(Exception):
    """What the real SDK raises when the API refuses a request (an unknown city, a bad argument)."""


class Client:
    def __init__(self, api_key: str | None = None):
        if not api_key:                                  # the real client needs one; any text works here
            raise WeatherKitError("weatherkit.Client needs an api_key")
        self.api_key = api_key

    def forecast(self, city: str, days: int = 1) -> dict:
        try:
            api.check("forecast", {"city": city, "days": days})     # logged, answered or refused
        except UnexpectedCall as exc:
            raise WeatherKitError(str(exc)) from None
        return {"city": city, "days": [dict(day) for day in FORECASTS[city][:days]]}

    def __getattr__(self, name):                         # radar(), alerts(), ...: not modelled, so refused
        raise api.not_modelled(name, "Client.")
