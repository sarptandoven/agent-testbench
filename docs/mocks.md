# Mock clients: LLMs and brokers

Code that calls a language model is usually tested in one of two ways: with real API calls, which cost money and
give a different answer each time, or not at all. Agents are no better - they spend the money or skip the test.
agent-testbench ships strict stand-ins for the two most used SDKs, `openai` and `anthropic`, so the code around a
model call (the prompt it builds, the tool loop, the retry, the parsing of the answer) can be tested for free, the
same way every time, and with the mistakes a real API would refuse caught before anyone pays for them.

Trading code gets the same treatment: [mock broker SDKs](#brokers) (`alpaca`, `ccxt`, `ib_async`) fill its orders
against a prices file on a paper account, and refuse a live one.

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

`testbench.yaml` is checked when it loads: an SDK testbench has no mock for, an unknown option, a replies file that
does not exist or has a bad pattern all stop there with what to write instead.

## What the mocks answer

| | `anthropic` | `openai` |
|---|---|---|
| clients | `Anthropic`, `AsyncAnthropic` (`with_options`, context managers) | `OpenAI`, `AsyncOpenAI` (`with_options`, context managers) |
| calls | `messages.create` (also `stream=True`), `messages.stream` (`text_stream`, `get_final_message`, `get_final_text`, `until_done`), `messages.count_tokens`; the same under `beta.messages` | `chat.completions.create` (also `stream=True` with `stream_options`), `embeddings.create` |
| replies | `Message` with `id`, `type`, `role`, `model`, `content` (`text`, `tool_use` and, with `thinking`, `thinking` blocks), `stop_reason`, `stop_sequence`, `usage` | `ChatCompletion` with `id`, `object`, `created`, `model`, `choices[i].message.content` / `.tool_calls`, `finish_reason`, `usage`; `CreateEmbeddingResponse` |
| errors | `APIError`, `APIStatusError`, `APIConnectionError`, `APITimeoutError`, `BadRequestError`, `AuthenticationError`, `PermissionDeniedError`, `NotFoundError`, `RateLimitError`, `InternalServerError`, `OverloadedError`, ... | `OpenAIError`, `APIError`, `APIStatusError`, `APIConnectionError`, `APITimeoutError`, `BadRequestError`, `AuthenticationError`, `PermissionDeniedError`, `NotFoundError`, `RateLimitError`, `InternalServerError`, ... |
| types | `anthropic.types` (`Message`, `TextBlock`, `ToolUseBlock`, stream events; every `*Param` is a dict), and the modules they are imported from: `anthropic.types.message`, `.text_block`, `.tool_use_block`, `.thinking_block`, `.usage`, `.content_block`, ... | `openai.types`, `openai.types.chat` (`ChatCompletion`, `ChatCompletionMessage`, ...; every `*Param` is a dict), and `openai.types.chat.chat_completion` (`Choice`), `.chat_completion_message`, `.chat_completion_message_tool_call` (`Function`), `.chat_completion_chunk`, `openai.types.completion_usage`, ... |

Reply objects take attribute access like the SDKs' models, and `to_dict()`, `to_json()`, `model_dump()` and
`model_dump_json()` with `exclude_none=` (and `include=` / `exclude=` for `model_dump`), `model_copy(update=...)`;
a misspelt keyword is a `TypeError`, as in pydantic. `Message.model_validate(data)` and `model_validate_json(text)`
(and the same on `ChatCompletion`, `Choice`, `TextBlock`, ...) build one from a dict, a saved reply for instance,
nested blocks included, and raise pydantic's `ValidationError` (a `ValueError`) when a field the real model requires
is missing; types are not checked beyond that. A module of request types (`*_param`, `*_params`) holds plain dicts
whatever it is called; importing a response module the mock does not have says so. They can
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

## Brokers

Trading code can be tested end to end the same way, against strict stand-ins for the Python SDKs agents use most:
`alpaca` (alpaca-py), `ccxt`, and `ib_async` (which also answers `import ib_insync`). Orders fill against a prices
file the step names, on a paper account the mock keeps; nothing reaches a broker, and the code under test does not
change.

```yaml
steps:
  - name: bot on a paper account
    run: python bot.py --once
    mocks:
      alpaca: {prices: data/prices.csv, cash: 100000, fee_usd: 0, slippage_bps: 5}
    expect:
      mock_calls:
        alpaca: {orders: "<= 20", orders_without_limit: 0, max_order_value: "<= 10000", live: 0}
```

### What the broker mocks answer

| | `alpaca` | `ccxt` | `ib_async` |
|---|---|---|---|
| clients | `TradingClient(api_key, secret_key, paper=True)`, `StockHistoricalDataClient`, `CryptoHistoricalDataClient` (also `raw_data=True`) | `ccxt.binance()`, `binanceus`, `bybit`, `okx`, `kucoin`, `coinbase`, `coinbaseexchange`, `kraken`, `krakenfutures`, `bitstamp`, `gemini`; snake_case and camelCase names | `IB()` with `connect`, `disconnect`, `isConnected`, `managedAccounts`; `import ib_insync` is the same mock |
| account | `get_account`, `get_all_positions`, `get_open_position`, `get_clock` | `fetch_balance` | `accountValues`, `accountSummary`, `reqAccountSummary`, `positions`, `reqPositions`, `portfolio` |
| orders | `submit_order` with `MarketOrderRequest`, `LimitOrderRequest`, `StopOrderRequest`, `StopLimitOrderRequest` (`qty` or `notional`); `get_orders`, `get_order_by_id`, `get_order_by_client_id`, `cancel_orders`, `cancel_order_by_id`, `close_all_positions`, `close_position` | `create_order` (`market`, `limit`; params `timeInForce`, `postOnly`, `clientOrderId`) and `create_market_buy_order` and the like, `cancel_order`, `cancel_all_orders`, `fetch_order`, `fetch_orders`, `fetch_open_orders`, `fetch_closed_orders`, `fetch_my_trades` | `placeOrder` with `MarketOrder`, `LimitOrder`, `StopOrder`, `StopLimitOrder`, giving a `Trade` kept up to date (status, fills, commission reports, its events and IB's); `cancelOrder`, `reqGlobalCancel`, `trades`, `openTrades`, `orders`, `openOrders`, `fills`, `executions`, `reqOpenOrders`, `reqExecutions` |
| market data | `get_stock_bars`, `get_crypto_bars` with `StockBarsRequest` / `CryptoBarsRequest` and `TimeFrame`, giving a `BarSet` (`bars["AAPL"]`, `bars.df`) | `load_markets`, `fetch_markets`, `market`, `fetch_ticker`, `fetch_tickers`, `fetch_ohlcv`, `fetch_time` | `qualifyContracts`, `reqHistoricalData` (a `BarDataList`; `util.df`), `reqMktData`, `reqTickers` |
| errors | `APIError` with the API's status and code (403 insufficient buying power or qty, 404, 422); pydantic's `ValidationError` from a request model | ccxt's own: `InsufficientFunds`, `InvalidOrder`, `OrderNotFound`, `ArgumentsRequired`, `BadSymbol`, `BadRequest`, `NotSupported`, `AuthenticationError`, ... | as IB reports them: an order IB would refuse comes back `Inactive`, with an `errorEvent` (201 rejected, 321 a bad request); raised as `RequestError` only with `IB.RaiseRequestErrors = True` |

Names, arguments and reply shapes follow alpaca-py 0.45.0, ccxt 4.5.92 and ib_async 2.1.0 (read from the installed
packages on 2026-11-12). When the real packages are installed where the tests run, a test checks that every public
name and method parameter of the mocks exists in them.

They are strict where the real SDKs are: alpaca's request models refuse a limit order without `limit_price` or a
market order with one (pydantic's errors, the same messages), `TradingClient()` without keys is a `ValueError`,
crypto orders take `gtc` or `ioc` and fractional stock orders must be `day`; ccxt's account calls need `apiKey` and
`secret` (and `password` on okx, kucoin and coinbaseexchange), binance's `cancel_order` and `fetch_my_trades` need a
symbol, timeframes are the exchange's own; IB's orders need `BUY` or `SELL` and the price their type takes. And
stricter in one place: an alpaca request model refuses a field it does not have (`limit_prce=`), which alpaca-py
would drop without a word.

### Prices and the clock

`prices:` is a CSV file with the columns `time,symbol,close`, and `open`, `high`, `low`, `volume` when you have
them:

```
time,symbol,close
2024-01-02,AAPL,185.64
2024-01-02,BTC/USD,45000
2024-01-02,ETH/USDT,2350
2024-01-03,AAPL,184.25
```

Times are dates, ISO times (`2024-01-02 15:30`, `2024-01-02T15:30:00Z`) or seconds or milliseconds since 1970;
without a zone they are UTC. Symbols are named as the SDK names them: `AAPL`, alpaca's `BTC/USD`, ccxt's unified
`ETH/USDT`; for IB a contract's `localSymbol` or `symbol` (or `symbol` and `currency`, as `EURUSD`). `price:
{AAPL: 190.5}` gives a fixed price instead, and wins over the file for its symbols. A symbol in neither stops the
call with a `SetupError` that lists the symbols there are: the test's data, not the code, is missing something.

The mock's clock is a bar of the file: `now:` (by default its last bar). Data calls answer bars up to now and never
after, so a strategy cannot peek ahead. `advance_every: N` moves the clock one bar every N requests, so a loop that
polls the broker walks through the file: with `advance_every: 1` the first request sees `now`, the next one the bar
after, and so on to the last bar. Making a client, `connect()` and `set_sandbox_mode()` are not requests, nor are
ib_async's reads of what IB already sent (`positions()`, `accountValues()`, `openTrades()`, ...).
`get_clock`, `fetch_time`, `exchange.milliseconds()` and `reqCurrentTime` read this clock, so a `since=` worked out
from them lines up with the file; `ib.sleep()` and `exchange.sleep()` return at once. Bars are answered as the file
has them: an hourly request on daily bars gets daily bars, and a note in its log line says so.

### Fills, fees and the account

- A market order fills at once, at the current close moved against the trader by `slippage_bps`.
- A limit order fills when the close crosses its limit, at the close with slippage but never past the limit. Until
  then it is open, and the cash it may cost (or the shares it sells) are held for it.
- A stop order becomes a market order once the close reaches its stop; a stop-limit becomes a limit order.
- An IOC or FOK order that cannot fill at once is cancelled.
- Each fill pays `fee_usd` plus `fee_bps` of its value, in the currency the order is paid in (for ccxt, the pair's
  quote currency).
- No short selling and no margin: a buy needs free cash (cash less what open orders hold), a sell free shares; else
  the broker's refusal (alpaca's 403 "insufficient buying power", ccxt's `InsufficientFunds`, IB's 201 rejection).

Positions keep their cost, so cash, positions, `avg_entry_price`, `unrealized_pl` (alpaca), `avgCost`,
`realizedPNL` (IB) and equity add up at every bar.

| option | what it does |
|---|---|
| `prices` | the CSV file of prices, relative to the project folder |
| `price` | fixed prices: `{AAPL: 190.5, BTC/USDT: 65000}` |
| `now` | the current bar: a time in the file (by default its last bar) |
| `advance_every` | move the clock one bar every N requests (by default it stays at `now`) |
| `cash` | the starting cash (100000) |
| `currency` | the cash currency (USD; for ccxt USDT on binance, bybit, okx and kucoin) |
| `positions` | held at the start, at the price of `now`: `{AAPL: 10}`; for ccxt by currency, `{BTC: 0.5}` |
| `fee_usd` | a fixed fee per filled order, in the cash currency (0) |
| `fee_bps` | a fee in basis points of each fill's value (0) |
| `slippage_bps` | how far market fills move against the trader, in basis points (0) |
| `allow_live` | let a live account through, for code only exercised offline (false) |

### Live versus paper

The mock never pretends a live order happened. A live account is refused:

- alpaca: `TradingClient(..., paper=False)`;
- ccxt: an account call (balance, orders, trades) on an exchange without `set_sandbox_mode(True)`. kraken, coinbase,
  kucoin and bitstamp have no sandbox in ccxt, so there `set_sandbox_mode(True)` raises `NotSupported` as the real
  ccxt does: every account call of such code would reach a real account. Market data (markets, tickers, candles)
  touches no account and is answered either way;
- ib_async: `connect()` on port 7496 (TWS) or 4001 (IB Gateway). 7497 and 4002 are the paper ports; any other port
  is refused, since the mock cannot tell which account it would reach.

```
LiveAccountRefused: mock alpaca: this test asked for a live account (TradingClient(paper=False)); trading code
under test must use paper/sandbox, see docs/mocks.md#brokers. Code that is only exercised offline can be let
through with allow_live: true in the step's mocks: options (nothing reaches a broker either way).
```

The refusal is a line of the log with `"live": true`, so the step fails even when the code catches it. For code that
is only exercised offline, `allow_live: true` lets it through; every call on that client is still logged as live, and
`mock_calls: {alpaca: {live: 0}}` forbids it again.

### What is not modelled

Anything else - streaming (`TradingStream`, `keepUpToDate=True`, `ccxt.pro`), ccxt's `async_support`, options,
bracket and OCO orders, trailing stops, changing an order (`replace_order_by_id`, `placeOrder` on a placed order),
margin and short selling, asset lists, IB's scanners and news - raises `NotModelled` (an `AttributeError`) naming the
method and what the mock answers, and is logged as refused:

```
mock alpaca: TradingClient.get_portfolio_history is not modelled; the mock answers get_account, get_all_positions, ...
```

A module the mock lacks says so too: `No module named 'alpaca.trading.stream': the mock alpaca package
(agent-testbench) does not have it. ...`

### The log and the expectation

Each call is a line of `mock_calls.jsonl`, as for the LLM mocks, with what the broker did:

```json
{"sdk": "alpaca", "endpoint": "submit_order", "call": 3, "live": false, "time": "2024-01-03T00:00:00Z",
 "arguments": {"order_data": {"class": "MarketOrderRequest", "symbol": "AAPL", "qty": 10.0, "side": "buy", ...}},
 "error": null, "orders": [{"id": "...", "symbol": "AAPL", "side": "buy", "type": "market", "qty": 10.0,
 "limit": null, "stop": null, "value": 1020.0}], "fills": [{"order": "...", "qty": 10.0, "price": 102.051,
 "fee": 1.0, "value": 1020.51, ...}], "account": "alpaca", "cash": 98978.49, "equity": 99998.49}
```

`mock_calls:` takes these keys for a broker, each with any [rule](expectations.md#rules):

| key | what it counts |
|---|---|
| `calls` | every call, refused or not |
| `refused` | calls refused: an API error, a live account, something not modelled (`0` unless said otherwise) |
| `orders` | orders the broker took (an order it rejected is a refused call) |
| `fills` | fills, whenever they happened |
| `cancels` | orders cancelled (also IOC and FOK orders that could not fill) |
| `orders_without_limit` | orders taken with no limit price (market and stop orders) |
| `max_order_value` | the largest order: its quantity times its limit (or stop, or the price when it was placed) |
| `fees` | the fees paid, in the cash currency |
| `live` | calls on a live account (refused, or let through by `allow_live`) |

```yaml
expect:
  mock_calls:
    alpaca: {orders: 0}                          # this signal must not trade today
    ccxt: {orders: "<= 20", fees: "< 50"}
    ib_async: {orders_without_limit: 0, max_order_value: "<= 10000", live: 0}
```

```
- expected mock ib_async orders_without_limit 0: 1 (wanted == 0); call 4: market buy 10 AAPL, value 1,050.00
- expected mock alpaca refused 0: 1 (wanted == 0); call 1: LiveAccountRefused: mock alpaca: this test asked for a
  live account (TradingClient(paper=False)); ...
```

Outside testbench, put the broker's folder first on `PYTHONPATH` (`client_paths(["alpaca"])`, as above) and its
options in `TESTBENCH_MOCK_ALPACA`, `TESTBENCH_MOCK_CCXT` or `TESTBENCH_MOCK_IB_ASYNC`, with `prices` as an absolute
path.

Limits: fills are deterministic and whole (no partial fills, no queue, no order book, no market hours: the mock's
market is open at every bar); one account per broker in each process, starting afresh in each; and a strategy's
results here say that its code does what it means, not that it makes money.
