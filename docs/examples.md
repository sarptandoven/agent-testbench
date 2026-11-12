# Examples

Four small projects, one for each kind of work people bring to testbench. Each runs here, for free, in about a
minute: `testbench demo NAME` copies it to a new folder (a git repository), runs its free plan, and says what to
try next.

```
testbench demo --list
testbench demo llm-app            # NAME [DIR]; with no NAME, tiny-model
```

| example | what it is | what its free plan proves | needs |
|---|---|---|---|
| [llm-app](#llm-app) | a support bot on the Anthropic SDK with one tool | the tool-use loop works, every request is one the API would take, and a 429 is survived - against a strict mock, with no API key | nothing |
| [web-app](#web-app) | a page with a form and a JSON API | a person can fill the form and get the right answer, with no console errors; screenshots to look at | Playwright and Chromium |
| [test-suite](#test-suite) | a Python package with a pytest suite and a planted bug | the suite fails, and the report names the test and what it got; the same suite in a Docker sandbox with no network | pytest (Docker for the sandbox plan) |
| [tiny-model](#tiny-model) | a Colab-style notebook that trains a small network | it trains, and the accuracy clears the bar; the same notebook on Modal and Colab | scikit-learn, matplotlib |

The examples are in [examples/](../examples) and in the installed package. In the repository, `cd examples/NAME`
and `testbench run PLAN` runs one in place.

## llm-app

[examples/llm-app](../examples/llm-app): `support_bot.py` answers questions about orders with Claude Opus 5.5 and
one tool, `lookup_order`, and runs the tool-use loop itself. The `mocked` plan runs it on five questions with
`mocks:`, so `import anthropic` loads testbench's [strict mock of the SDK](mocks.md): no key, no network, no cost.
The mock's answers come from a replies file, and its answer to a lookup is made from what the tool returned - so
the right answer in the output means the tool really ran and its result really went back.

```yaml
      - name: answer questions
        run: python support_bot.py --questions questions.json --out outputs/answers.json
        mocks:
          anthropic: {replies: mock_replies.yaml}
        expect:
          text:
            stdout: [Order A1002 has shipped with UPS and should arrive on 2026-11-04., ...]
          json:
            outputs/answers.json: {questions: 5, lookups: 4}
          mock_calls:
            anthropic: {calls: 9, tool_calls: 4, input_tokens: "<= 1500", output_tokens: "<= 300"}
```

A second step makes the third request fail with a 429 (`fail_on_call: 3`) and checks every answer still comes:

```
- survives a rate limit: mock anthropic calls 10 - 10 (wanted == 10); beta.messages.create claude-opus-5-5 x10
- survives a rate limit: mock anthropic injected 1 - 1 (wanted == 1); call 3: RateLimitError: fail_on_call: call 3 fails with rate_limit (1 retried by the SDK itself)
```

A broken loop is caught the way the real API would catch it. With the tool result sent back under the wrong id:

```
- expected mock anthropic refused 0: 1 (wanted == 0); call 2: BadRequestError: messages[2]: tool_use ids were found
  without tool_result blocks immediately after: toolu_mock001_0. ...
```

## web-app

[examples/web-app](../examples/web-app): "Split the bill", a page with a form (`static/`) and a JSON API in
`app.py` (standard library only). The `ui` plan starts the app, opens it in headless Chromium, fills in $50, 3
people and a 15% tip, submits, and checks the answer to the cent; then it submits a bill for no one and checks the
page explains why. Both steps check that nothing reached the console as an error, and take screenshots.

```yaml
        web:
          serve: python app.py 8974
          url: http://127.0.0.1:8974/
          actions:
            - screenshot: empty-form
            - fill: {selector: "#total", text: "50"}
            - fill: {selector: "#people", text: "3"}
            - fill: {selector: "#tip", text: "15"}
            - click: "button[type=submit]"
            - wait_for: "text=Each person pays"
            - screenshot: split
        expect:
          web:
            title: Split the bill
            text: ["Each person pays $19.17 (of $57.50 with the tip)."]
            no_console_errors: true
```

Rename the page's `#result` element and the report says what a person would have seen:

```
- expected page shows 'Each person pays $19.17 (of $57.50 with the tip).': not found
- expected no console or page errors: Cannot set properties of null (setting 'className')
- expected every action worked: action 6 {'wait_for': 'text=Each person pays'}: Page.wait_for_selector: Timeout 30000ms exceeded.
```

Needs `pip install "agent-testbench[web]"` and `playwright install chromium`. `testbench report --format html`
shows the screenshots inline.

## test-suite

[examples/test-suite](../examples/test-suite): `textstats`, a small package (word counts, the most common words,
reading time) with five tests and one planted bug. Its plans fail until the code is fixed - that is the point:
the demo ends with the failing report and asks you to hand it to your agent.

- `tests` runs the suite here, judged by pytest's own JUnit report (`junit:`): no failures or errors, and at least
  5 tests, so a suite that silently collects nothing fails too.
- `sandbox` runs it in a fresh [Docker](docker.md) container with `network: none`, after a step that proves the
  container cannot connect anywhere: the way to run code nobody has reviewed. The first run builds the image.

```
- expected outputs/junit.xml: failures 0: 1 (wanted == 0) of 5 tests; failed: tests.test_textstats.test_top_words_are_the_most_common
  (AssertionError: assert [('c', 1), ('b', 2)] == [('a', 3), ('b', 2)] At index 0 diff: ('c', 1) != ('a', 3) ...)
```

Then, in Claude Code with the plugin: "the tests plan fails: find out why, fix the code (not the tests), and
prove it with the plan". [A failing suite fixed in a Docker sandbox](demo-sandbox.md) is a real run of that.

## tiny-model

[examples/tiny-model](../examples/tiny-model): a Colab-style notebook, built from `cells/`, that trains a small
network on scikit-learn's digits. `testbench demo` (no NAME) runs its `smoke` plan: lint, then a quick train with
`accuracy >= 0.85` checked from the metrics file it writes. Its other plans run the same notebook on a Modal CPU,
a Modal T4 and a Colab runtime, each priced before it starts. It is the project of the
[getting started](getting-started.md) walk-through and of the [loop demo](demo-loop.md).

## More in examples/

- [web-ui](../examples/web-ui): a one-page counter, clicked twice in headless Chromium.
- [agent-loop](../examples/agent-loop): an agent of your own in about 60 lines, on the Anthropic SDK and
  testbench's tools ([Python API](python-api.md)).
- [plugins/testbench-check-example](../examples/plugins/testbench-check-example): a check plug-in in one file,
  `max_log_lines: 200` ([Check plug-ins](plugins.md#checks)).
