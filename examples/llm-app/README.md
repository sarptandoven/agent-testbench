# llm-app

`support_bot.py`: a small customer-support bot on the Anthropic SDK (Claude Opus 5.5) with one tool,
`lookup_order`, that reads `orders.json`. It runs the tool-use loop itself: ask, run the tool, send the result back,
answer.

The `mocked` plan runs it on five questions with `mocks: [anthropic]`, so `import anthropic` loads testbench's strict
mock: no API key, no network, no cost. `mock_replies.yaml` says what the mock model answers, and its answers to a
lookup are made from what the tool returned. The plan checks the answers, the bot's JSON output, and the requests
themselves (`mock_calls`: 9 calls, 4 tool calls, token counts that stay small, none the real API would refuse); its
second step makes the third request fail with a 429 and checks the bot still answers everything.

```bash
testbench demo llm-app          # or, in this folder: testbench run mocked
```

The report lists every check; `mock_calls.jsonl` in the step's folder holds each request as the API would get it.
Break the loop (send a `tool_result` with the wrong `tool_use_id`, or put `SYSTEM` first in `messages` as a system
message) and the report names the 400 the real API would have answered. With `pip install anthropic` and a key,
`python support_bot.py "Where is my order A1002?"` asks the real model.
