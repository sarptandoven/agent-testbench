# testbench-mock-example

A strict mock of `weatherkit` - a small, fictional weather SDK - packaged as an agent-testbench mock plug-in. It is
the pattern for a mock of `stripe`, a vendor's SDK or an internal API: any client a step must not call for real.

```bash
pip install ./examples/plugins/testbench-mock-example
testbench plugins                     # lists weatherkit among the mocks
```

```yaml
# testbench.yaml
plans:
  weather:
    steps:
      - name: app
        run: python app.py Paris London
        mocks: {weatherkit: {forecasts: tests/forecasts.yaml}}     # or [weatherkit]: the forecasts shipped with it
        expect:
          mock_calls: {weatherkit: {calls: 2}}                     # refused: 0 is checked unless said otherwise
```

The step's `import weatherkit` loads the mock. `Client(api_key).forecast(city, days=1)` answers from the forecasts
file (YAML or JSON: `{Paris: [{summary: sunny, high_c: 21, low_c: 12}, ...]}`); a city the file lacks, `days`
outside 1..7 and every other method (`client.radar()`) are refused, logged as refused, and fail the step. Nothing
touches the network.

| file | what it is |
|---|---|
| `pyproject.toml` | the entry point that makes it a plug-in: `[project.entry-points."agent_testbench.mocks"]` |
| `testbench_mock_example/__init__.py` | the `Mock` testbench loads: its name, path, imports and options |
| `testbench_mock_example/sdk/weatherkit/__init__.py` | the stand-in for `import weatherkit`, built on `StrictAPI` |
| `testbench_mock_example/forecasts.json` | the forecasts it answers when a step gives none |

[Mock plug-ins](../../../docs/plugins.md#mocks) explains each part and the contract `testbench plugin test` checks.
