# Mock LLM clients

Code that calls a language model is usually tested in one of two ways: with real API calls, which cost money and
give a different answer each time, or not at all. Agents are no better - they spend the money or skip the test.
agent-testbench ships strict stand-ins for the two most used SDKs, `openai` and `anthropic`, so the code around a
model call (the prompt it builds, the tool loop, the retry, the parsing of the answer) can be tested for free, the
same way every time, and with the mistakes a real API would refuse caught before anyone pays for them.

```yaml
steps:
  - name: tests
    run: python -m pytest -q
    mocks: [openai, anthropic]
```

That is the whole setup. The step's `import openai` and `import anthropic` load the mocks instead of the real
packages (even where those are installed), no API key is needed, and nothing opens a network connection.

## What `mocks:` does

`mocks:` puts the mock packages first on the step's `PYTHONPATH`, the same way notebook steps get the headless
`google.colab`. It works for `run:` steps, for the kernel of `notebook:` steps, and for the `serve:` command of
`web:` steps. Each mock reads its options from the environment and appends every call to
`mock_calls.jsonl` in the step's folder, which is part of the run's evidence.

With options:

```yaml
mocks:
  anthropic: {replies: tests/replies.yaml, fail_on_call: 3, fail_with: rate_limit}
  openai: {}                       # or `openai:` with nothing after it
```

| option | what it does |
|---|---|
| `replies` | a YAML or JSON file of replies, relative to the project folder ([replies](#replies)) |
| `fail_on_call` | a call number (`3`) or a list (`[3, 4, 5]`) that fails instead of answering, counted from 1 in each process |
| `fail_with` | how those calls fail: `rate_limit` (default), `overloaded`, `server`, `timeout`, `connection`, `auth`, `permission`, `not_found`, `bad_request` |

`testbench.yaml` is checked when it loads: an SDK other than these two, an unknown option, a replies file that
does not exist or has a bad pattern all stop there with what to write instead.

## What the mocks answer

| | `anthropic` | `openai` |
|---|---|---|
| clients | `Anthropic`, `AsyncAnthropic` (`with_options`, context managers) | `OpenAI`, `AsyncOpenAI` (`with_options`, context managers) |
| calls | `messages.create` (also `stream=True`), `messages.stream` (`text_stream`, `get_final_message`, `get_final_text`, `until_done`), `messages.count_tokens`; the same under `beta.messages` | `chat.completions.create` (also `stream=True` with `stream_options`), `embeddings.create` |
| replies | `Message` with `id`, `type`, `role`, `model`, `content` (`text`, `tool_use` and, with `thinking`, `thinking` blocks), `stop_reason`, `stop_sequence`, `usage` | `ChatCompletion` with `id`, `object`, `created`, `model`, `choices[i].message.content` / `.tool_calls`, `finish_reason`, `usage`; `CreateEmbeddingResponse` |
| errors | `APIError`, `APIStatusError`, `APIConnectionError`, `APITimeoutError`, `BadRequestError`, `AuthenticationError`, `PermissionDeniedError`, `NotFoundError`, `RateLimitError`, `InternalServerError`, `OverloadedError`, ... | `OpenAIError`, `APIError`, `APIStatusError`, `APIConnectionError`, `APITimeoutError`, `BadRequestError`, `AuthenticationError`, `PermissionDeniedError`, `NotFoundError`, `RateLimitError`, `InternalServerError`, ... |
| types | `anthropic.types` (`Message`, `TextBlock`, `ToolUseBlock`, stream events; every `*Param` is a dict) | `openai.types`, `openai.types.chat` (`ChatCompletion`, `ChatCompletionMessage`, ...; every `*Param` is a dict) |

Reply objects take attribute access like the SDKs' models, and `to_dict()`, `model_dump()`, `to_json()`. They can
be passed back as they are (`messages.append({"role": "assistant", "content": response.content})`, or OpenAI's
`messages.append(completion.choices[0].message)`). Errors carry what code reads from them: `status_code`, `body`,
`response.headers` (`retry-after: 0` on a 429), `request_id`, and OpenAI's `code` and `param`. Any `api_key` works, and
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` are read like the real clients read them, but never required.

The parameter lists follow the published SDKs: anthropic 2.3.0 (and the Messages API reference on
platform.claude.com, read on 2026-11-01) and openai 3.42.0 (its `chat.completions.create` and `embeddings.create`
signatures and types, read on 2026-11-01; OpenAI's API reference pages could not be reached when this was written).
When the real packages are installed where the tests run, a test checks that every public name and parameter of
the mocks exists in them, so the mocks do not drift into names the SDKs lack.

## What is checked

A request the real API would answer with a 400 raises the SDK's `BadRequestError`, whose message says what is wrong
and what to write instead. A wrong keyword argument, which the real SDK refuses with a `TypeError` before sending
anything, raises an error that is both a `TypeError` and a `BadRequestError`.

```
anthropic.BadRequestError: Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message':
"messages[0] has role 'system': put the system prompt in the top-level system= parameter (a system message inside
messages must come after a user message)"}, ...}
```

| | refused |
|---|---|
| both | an unknown keyword (with "did you mean" for typos, and a pointer when it is the other SDK's name: `stop_sequences` for OpenAI, `max_completion_tokens` for Anthropic), a missing `model` / `messages` / `max_tokens`, a value of the wrong type or out of range (`temperature`, `top_p`, `n`, ...), unknown keys inside messages, tools, `metadata` and `extra_body`, a tool in the other SDK's shape, `tool_choice` without tools or naming a tool that is not there, an assistant's tool calls without results right after, or results that answer no call |
| `anthropic` | a `system` message first (it belongs in `system=`), a first message from the assistant, a `tool` / `function` / `developer` role, empty content, OpenAI-style content blocks (`image_url`), `tool_result` outside user messages, two tools of one name, `thinking.budget_tokens` under 1024 or not below `max_tokens`, whitespace-only stop sequences, a string `tool_choice`, and the SDK's own refusal of a non-streaming `max_tokens` that may take over 10 minutes (a `ValueError`, as in the SDK) |
| `openai` | a message without `role` or without `content`, a role that does not exist, Anthropic-style content blocks, tool call `arguments` that are not a JSON string, a `tool` message that answers no call, `response_format: json_object` without the word "json" in the messages, a `json_schema` without a name, `stream_options` without `stream`, `parallel_tool_calls` without tools, more than 4 stop sequences, `top_logprobs` without `logprobs`, a pydantic class passed to `create()`; for embeddings, an empty input, more than 2048 inputs, `dimensions` on `text-embedding-ada-002` |

A model name the mock does not know is accepted, since no mock can list every model, and noted in the call's log
line. So are things that depend on the model (some models refuse `temperature`, or `max_tokens` in favor of
`max_completion_tokens`): those are noted, not refused.

## Replies

Without a replies file, every answer is `mock reply to: ` and the first 60 characters of the last user message, so
the same input always gets the same output. The last user message is the last `user` message's text, or the tool
results when they come last (OpenAI `tool` messages, Anthropic `tool_result` blocks): after a tool call, the mock
answers the tool's result, not the question again.

A replies file maps a regular expression, searched in that text, to an answer. The first rule that matches answers.

```yaml
# tests/replies.yaml
- match: "weather in (\\w+)"
  tool: get_weather              # the model calls a tool of the request
  input: {city: "\\1"}           # \1 is the first group of the match
- match: "sunny|rain"
  text: "Pack for the weather: \\g<0>."
- match: "(?i)classify"
  json: {label: positive, score: 0.9}     # written out as the reply text
- match: "trigger an outage"
  error: overloaded              # this question always fails (retried by the SDK like a real one)
```

The short form `{"pattern": "reply text"}` (a mapping) works too. A rule takes `text`, `json`, `tool` with
`input`, or `error`; `text` and `tool` can go together (the model says something, then calls the tool). A tool the
request does not offer, or an `input` without a key the tool's schema requires, fails the call with a message that
says so: that is the test setup, not the code, being wrong.

Without a matching rule:

- **structured output** (Anthropic `output_config={"format": {"type": "json_schema", ...}}`, OpenAI
  `response_format` `json_schema`) is the smallest value of the schema's shape: `{"city": "mock", "temp": 0.0}`;
  OpenAI's `json_object` is `{"reply": "mock reply to: ..."}`;
- **a forced tool** (`tool_choice` `{type: any}` / `{type: tool}` / `"required"` / a named function) is called with
  input of its schema's shape;
- **`max_tokens`** and **stop sequences** end the text as the model would, with `stop_reason` `max_tokens` /
  `stop_sequence` (`finish_reason` `length` / `stop`);
- **`thinking`** (`adaptive` or `enabled`) puts a `thinking` block before the text, so code that reads
  `content[0].text` fails here as it would against the real API;
- **embeddings** are unit vectors made from the text's words: the same text always gets the same vector, and texts
  that share words point the same way. Dimensions are the model's (1536, or 3072 for `text-embedding-3-large`)
  or `dimensions=`.

Token counts are the words times 1.3, rounded up: stable, not what a real tokenizer says.

## Tool calls

The tool loop of an app can be tested end to end:

```python
r = client.messages.create(model="claude-opus-5-5", max_tokens=1024, tools=[weather_tool], messages=messages)
while r.stop_reason == "tool_use":
    messages.append({"role": "assistant", "content": r.content})
    messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": b.id, "content": run(b)}
                                                 for b in r.content if b.type == "tool_use"]})
    r = client.messages.create(model="claude-opus-5-5", max_tokens=1024, tools=[weather_tool], messages=messages)
```

With the replies above, "What is the weather in Paris?" gets a `tool_use` block calling `get_weather` with
`{"city": "Paris"}`; the tool's result "sunny, 21 C" then gets "Pack for the weather: sunny.". A loop that forgot to
send the `tool_result` back, or sent it with the wrong id, gets the 400 the real API sends.

## Errors on purpose

`fail_on_call: 3` makes the third call of each process fail with `fail_with` (a 429 `RateLimitError` unless said
otherwise). Calls are HTTP requests, so the SDK's own retries count: like the real clients, the mocks retry a
`rate_limit`, `overloaded`, `server`, `timeout` or `connection` failure up to `max_retries` times (2 unless the
client says otherwise), without waiting. So with a default client, a single failure is absorbed by the SDK, as it
would be in production. To test the app's own retry code, make the client `max_retries=0`, or fail calls in a row:
`fail_on_call: [3, 4, 5]` exhausts two retries.

| `fail_with` | anthropic | openai |
|---|---|---|
| `rate_limit` | `RateLimitError` (429) | `RateLimitError` (429) |
| `overloaded` | `OverloadedError` (529) | `InternalServerError` (503) |
| `server` | `InternalServerError` (500) | `InternalServerError` (500) |
| `timeout` | `APITimeoutError` | `APITimeoutError` |
| `connection` | `APIConnectionError` | `APIConnectionError` |
| `auth`, `permission`, `not_found`, `bad_request` | `AuthenticationError`, `PermissionDeniedError`, `NotFoundError`, `BadRequestError` | the same |

## The log

Every call, answered or not, is one JSON line of `mock_calls.jsonl` in the step's folder
(`.testbench/runs/RUN/steps/01-NAME/`):

```json
{"sdk": "anthropic", "endpoint": "messages.create", "call": 2, "model": "claude-opus-5-5", "known_model": true,
 "arguments": {"model": "claude-opus-5-5", "max_tokens": 100, "messages": [{"role": "user", "content": "question 0"}]},
 "error": null, "notes": [], "reply": {"text": "mock reply to: question 0", "tool_calls": []}, "rule": null,
 "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 7}}
```

`error` is `{"kind": "refused", "type": "BadRequestError", "message": ..., "param": ...}` for a request the mock
refused, or `{"kind": "injected", "type": "RateLimitError", "retried": true, ...}` for a failure asked for. Strings
over 2000 characters and lists over 100 items are cut. `rule` is the replies rule that answered.

## The expectation

```yaml
expect:
  mock_calls:
    anthropic: {calls: "<= 5", output_tokens: "<= 2000", tool_calls: ">= 1"}
    openai: {calls: ">= 1"}
```

Keys: `calls` (every call, the SDK's retries included), `refused`, `injected`, `input_tokens`, `output_tokens`
(the mock's counts) and `tool_calls`, each with any [rule](expectations.md#rules). `refused: 0` is checked for every
SDK a step lists under `mocks:` unless said otherwise, so a 400 that the code caught and ignored still fails the
step:

```
- expected mock anthropic refused 0: 1 (wanted == 0); call 1: BadRequestError: messages[0] has role 'system': put
  the system prompt in the top-level system= parameter (a system message inside messages must come after a user message)
- expected mock anthropic calls <= 3: 5 (wanted <= 3.0); messages.create claude-opus-5-5 x5
- expected mock openai calls >= 1: 0 (wanted >= 1.0); no calls were logged
```

Like every check that measures a number, these reach the comparison with the previous run, `testbench history`
and `testbench loop --metric "check:mock anthropic output_tokens"`.

## Outside testbench

The mocks are plain packages; `pytest` or a script can use them directly:

```bash
export PYTHONPATH="$(python -c 'from agent_testbench.mocks import client_paths; import os; print(os.pathsep.join(client_paths(["openai", "anthropic"])))')"
export TESTBENCH_MOCK_CALLS=mock_calls.jsonl                 # where the log goes (default: the step folder, or ./)
export TESTBENCH_MOCK_ANTHROPIC='{"replies": "tests/replies.yaml", "fail_on_call": 3}'
python -m pytest -q
```

## Limits

- **Not a model.** Answers are canned: the mocks test the code around a model call, not what a model would say or
  how good it is. Prompts are checked for shape, never for meaning.
- **Not the service.** No real rate limits, latency, timeouts or outages, except the failures asked for; no
  model-specific rules beyond notes; token counts are approximate.
- **Not every endpoint.** Anthropic: message batches, files, models, `messages.parse`, `with_raw_response` and the
  Bedrock, Vertex and Foundry clients are not there. OpenAI: the Responses API, `chat.completions.parse` and
  `.stream`, images, audio, files, models and `AzureOpenAI` are not there. Using one raises an `AttributeError`
  that says what the mock answers.
- **Not every library.** Code that calls the SDKs directly works; libraries that reach into the SDKs' internals
  (LangChain, LiteLLM and the like) may not import against the mocks.
- **Plans only.** Live sessions (`testbench session`, `exec`) do not get `mocks:`; set `PYTHONPATH` as above.
- **Python only.** The TypeScript, Go and other SDKs are not mocked.

For any other paid API, write a strict mock with `agent_testbench.mocks.StrictAPI` and put its folder on the step's
`pythonpath:`.
