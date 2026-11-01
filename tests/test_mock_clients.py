"""The drop-in mock `anthropic` and `openai` packages (mocks: on a step) and the mock_calls expectation."""
import asyncio
import base64
import importlib
import inspect
import json
import math
import os
import re
import socket
import subprocess
import sys
import textwrap

import pytest
import yaml

from agent_testbench import config as C, expect, mocks as M, report as R
from conftest import cli, make_notebook, write_config

PATHS = M.client_paths(M.SDKS)
NAMES = ("anthropic", "openai", "_testbench_mock")


def _forget():
    for k in [k for k in sys.modules if k.split(".")[0] in NAMES]:
        del sys.modules[k]


@pytest.fixture
def sdk(tmp_path, monkeypatch):
    """load("anthropic", **options) imports a fresh mock package, logging to tmp_path/mock_calls.jsonl."""
    saved = {k: v for k, v in sys.modules.items() if k.split(".")[0] in NAMES}
    for p in reversed(PATHS):
        monkeypatch.syspath_prepend(p)
    monkeypatch.setenv("TESTBENCH_MOCK_CALLS", str(tmp_path / "mock_calls.jsonl"))
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    def load(name, **opts):
        monkeypatch.setenv(f"TESTBENCH_MOCK_{name.upper()}", json.dumps(opts))
        _forget()
        return importlib.import_module(name)
    yield load
    _forget()
    sys.modules.update(saved)


def logged(tmp_path):
    f = tmp_path / "mock_calls.jsonl"
    return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []


USER = [{"role": "user", "content": "What is the weather in Paris today?"}]
WEATHER = {"name": "get_weather", "description": "Weather for a city",
           "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}
OA_WEATHER = {"type": "function", "function": {"name": "get_weather", "parameters": WEATHER["input_schema"]}}


def replies(tmp_path, rules, name="replies.yaml") -> str:
    f = tmp_path / name
    f.write_text(json.dumps(rules) if name.endswith(".json") else yaml.safe_dump(rules))
    return str(f)


# ---- anthropic ----------------------------------------------------------------------------------------------

def test_anthropic_reply_has_the_sdk_shape(sdk, tmp_path, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("the mock opened a connection")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    anthropic = sdk("anthropic")
    client = anthropic.Anthropic()                                  # no key anywhere, and none needed
    r = client.messages.create(model="claude-opus-5-5", max_tokens=256, system="Be brief.", messages=USER)
    assert (r.type, r.role, r.model, r.stop_reason, r.stop_sequence) == ("message", "assistant", "claude-opus-5-5",
                                                                         "end_turn", None)
    assert r.id.startswith("msg_") and r.content[0].type == "text"
    assert r.content[0].text == "mock reply to: What is the weather in Paris today?"
    assert r.usage.input_tokens == math.ceil(9 * 1.3) and r.usage.output_tokens == math.ceil(10 * 1.3)
    assert isinstance(r, anthropic.types.Message) and isinstance(r.content[0], anthropic.types.TextBlock)
    assert json.loads(r.to_json())["content"][0]["text"] == r.content[0].text == r.model_dump()["content"][0]["text"]
    again = anthropic.Anthropic(api_key="anything").messages.create(model="claude-opus-5-5", max_tokens=256,
                                                                    messages=USER)
    assert again.content == r.content, "the same question gets the same reply"
    rec = logged(tmp_path)[-1]
    assert (rec["sdk"], rec["endpoint"], rec["call"], rec["model"], rec["known_model"]) == (
        "anthropic", "messages.create", 2, "claude-opus-5-5", True)
    assert rec["usage"] == {"input_tokens": 10, "output_tokens": 13} and rec["error"] is None
    client.messages.create(model="claude-made-up", max_tokens=10, messages=USER)
    assert "not one this mock knows" in logged(tmp_path)[-1]["notes"][0], "an unknown model is allowed, and noted"


A_BAD = [
    ({"messages": [{"role": "system", "content": "Be brief."}, *USER]}, "top-level system= parameter", False),
    ({"temprature": 0.2}, "unexpected keyword argument 'temprature' (did you mean 'temperature'?)", True),
    ({"max_tokens": None}, "is missing max_tokens", True),
    ({"max_tokens": "256"}, "max_tokens must be an integer, got a string", False),
    ({"messages": [{"content": "hi"}]}, "messages[0] has no role", False),
    ({"messages": [*USER, {"role": "tool", "content": "x"}]}, "a tool result goes in a user message", False),
    ({"messages": [{"role": "assistant", "content": "hi"}]}, "the first message must be from the user", False),
    ({"messages": [{"role": "user", "content": " "}]}, "messages[0].content is empty", False),
    ({"messages": [*USER, {"role": "system", "content": "In French."}, *USER]},
     "a system message must be the last message or be followed by an assistant message", False),
    ({"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "u"}}]}]},
     "'image_url', which is not a content block type (that is OpenAI's shape", False),
    ({"tools": [OA_WEATHER]}, "tools[0] is in OpenAI's shape", False),
    ({"tools": [{"name": "w", "parameters": {"type": "object"}}]}, "the Messages API calls it input_schema", False),
    ({"tools": [WEATHER, WEATHER]}, "two tools are named 'get_weather'", False),
    ({"tools": [WEATHER], "tool_choice": "auto"}, "the string 'auto' is OpenAI's shape", False),
    ({"tool_choice": {"type": "any"}}, "the request has no tools", False),
    ({"tools": [WEATHER], "tool_choice": {"type": "tool", "name": "nope"}}, "'nope' is not one of the request's", False),
    ({"messages": [*USER, {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "get_weather",
                                                             "input": {}}]}, {"role": "user", "content": "and?"}]},
     "tool_use ids were found without tool_result blocks immediately after: t1", False),
    ({"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t9", "content": "x"}]}]},
     "unexpected tool_use_id 't9'", False),
    ({"thinking": {"type": "enabled", "budget_tokens": 100}}, "budget_tokens must be at least 1024", False),
    ({"stop_sequences": [" "]}, "stop_sequences[0] must be a string with non-whitespace text", False),
    ({"metadata": {"user": "u1"}}, "metadata has 'user', which the API does not accept", False),
    ({"temperature": 1.5}, "temperature must be from 0 to 1", False),
    ({"output_config": {"effort": "extreme"}}, "output_config.effort is 'extreme'", False),
    ({"extra_body": {"frobnicate": 1}}, "frobnicate: Extra inputs are not permitted", False),
]


@pytest.mark.parametrize("change,why,type_error", A_BAD, ids=[w[:40] for _, w, _ in A_BAD])
def test_anthropic_refuses_what_the_api_refuses(sdk, tmp_path, change, why, type_error):
    anthropic = sdk("anthropic")
    kwargs = {k: v for k, v in {"model": "claude-opus-5-5", "max_tokens": 256, "messages": USER, **change}.items()
              if v is not None}
    with pytest.raises(anthropic.BadRequestError) as info:
        anthropic.Anthropic().messages.create(**kwargs)
    e = info.value
    assert why in e.body["error"]["message"] and why in str(e)
    assert (e.status_code, e.type, isinstance(e, TypeError)) == (400, "invalid_request_error", type_error)
    rec = logged(tmp_path)[-1]
    assert rec["error"]["kind"] == "refused" and why in rec["error"]["message"] and "usage" not in rec


def test_anthropic_sdk_side_refusals(sdk):
    anthropic = sdk("anthropic")
    c = anthropic.Anthropic()
    with pytest.raises(ValueError, match="Streaming is required"):
        c.messages.create(model="claude-opus-5-5", max_tokens=64000, messages=USER)
    with c.messages.stream(model="claude-opus-5-5", max_tokens=64000, messages=USER) as s:   # streaming: fine
        assert s.get_final_text().startswith("mock reply to:")
    with pytest.raises(TypeError, match="unexpected keyword argument 'stream'"):
        c.messages.stream(model="claude-opus-5-5", max_tokens=10, messages=USER, stream=True).__enter__()
    with pytest.raises(TypeError, match="api_kye"):
        anthropic.Anthropic(api_kye="x")
    with pytest.raises(AttributeError, match="it answers messages.create"):
        c.completions.create(prompt="x")


def test_anthropic_tool_use_from_a_replies_file(sdk, tmp_path):
    path = replies(tmp_path, [{"match": r"weather in (\w+)", "tool": "get_weather", "input": {"city": r"\1"}},
                              {"match": "sunny", "text": "It is sunny in Paris, 21 C."}])
    anthropic = sdk("anthropic", replies=path)
    c = anthropic.Anthropic()
    msgs = list(USER)
    r = c.messages.create(model="claude-opus-5-5", max_tokens=512, tools=[WEATHER], messages=msgs)
    use = r.content[-1]
    assert r.stop_reason == "tool_use" and (use.type, use.name, use.input) == ("tool_use", "get_weather",
                                                                              {"city": "Paris"})
    msgs += [{"role": "assistant", "content": r.content},             # the SDK's own blocks, passed back as they are
             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use.id, "content": "sunny, 21 C"}]}]
    r2 = c.messages.create(model="claude-opus-5-5", max_tokens=512, tools=[WEATHER], messages=msgs)
    assert r2.stop_reason == "end_turn" and r2.content[0].text == "It is sunny in Paris, 21 C."
    assert [x["rule"] for x in logged(tmp_path)] == [1, 2] and logged(tmp_path)[0]["reply"]["tool_calls"] == [
        {"name": "get_weather", "input": {"city": "Paris"}}]
    with pytest.raises(RuntimeError, match="calls the tool 'get_weather', but this request offers no tools"):
        c.messages.create(model="claude-opus-5-5", max_tokens=512, messages=USER)
    forced = c.messages.create(model="claude-opus-5-5", max_tokens=512, tools=[WEATHER],
                               tool_choice={"type": "tool", "name": "get_weather"},
                               messages=[{"role": "user", "content": "anything"}])
    assert forced.content[0].input == {"city": "mock"}, "a forced tool call gets input of its schema's shape"


def test_anthropic_errors_on_the_calls_asked_for(sdk, tmp_path):
    anthropic = sdk("anthropic", fail_on_call=[2])
    c = anthropic.Anthropic(max_retries=0)
    kw = {"model": "claude-opus-5-5", "max_tokens": 64, "messages": USER}
    c.messages.create(**kw)
    with pytest.raises(anthropic.RateLimitError) as info:
        c.messages.create(**kw)
    e = info.value
    assert isinstance(e, anthropic.APIStatusError) and isinstance(e, anthropic.APIError)
    assert (e.status_code, e.response.headers["Retry-After"], e.type) == (429, "0", "rate_limit_error")
    c.messages.create(**kw)
    assert [(x["call"], (x["error"] or {}).get("kind")) for x in logged(tmp_path)] == [(1, None), (2, "injected"),
                                                                                        (3, None)]
    anthropic = sdk("anthropic", fail_on_call=1)                       # the SDK retries a 429 itself (max_retries=2)
    assert anthropic.Anthropic().messages.create(**kw).content
    assert [x["error"]["retried"] for x in logged(tmp_path)[3:] if x["error"]] == [True]
    anthropic = sdk("anthropic", fail_on_call=[1, 2, 3], fail_with="overloaded")
    with pytest.raises(anthropic.OverloadedError):                     # three failures in a row exhaust two retries
        anthropic.Anthropic().messages.create(**kw)
    anthropic = sdk("anthropic", fail_on_call=1, fail_with="timeout")
    with pytest.raises(anthropic.APIConnectionError):
        anthropic.Anthropic(max_retries=0).messages.create(**kw)
    path = replies(tmp_path, [{"match": "Paris", "error": "auth"}])
    anthropic = sdk("anthropic", replies=path)
    with pytest.raises(anthropic.AuthenticationError):
        anthropic.Anthropic().messages.create(**kw)


def test_anthropic_streams_and_async(sdk):
    anthropic = sdk("anthropic")
    c = anthropic.Anthropic()
    kw = {"model": "claude-opus-5-5", "max_tokens": 256, "messages": USER}
    text = c.messages.create(**kw).content[0].text
    events = list(c.messages.create(**kw, stream=True))
    assert [e.type for e in events[:2]] == ["message_start", "content_block_start"]
    assert [e.type for e in events[-3:]] == ["content_block_stop", "message_delta", "message_stop"]
    assert "".join(e.delta.text for e in events if e.type == "content_block_delta") == text
    assert events[-2].delta.stop_reason == "end_turn" and events[0].message.content == []
    with c.messages.stream(**kw) as s:
        assert "".join(s.text_stream) == text and s.get_final_message().content[0].text == text
    with c.messages.stream(**kw, tools=[WEATHER], tool_choice={"type": "any"}) as s:
        seen = [e.type for e in s]
        assert "input_json" in seen and s.get_final_message().content[0].input == {"city": "mock"}

    async def main():
        a = anthropic.AsyncAnthropic()
        r = await a.messages.create(**kw)
        chunks = [e.type async for e in await a.messages.create(**kw, stream=True)]
        async with a.messages.stream(**kw) as s:
            streamed = "".join([t async for t in s.text_stream])
            final = await s.get_final_message()
        return r.content[0].text, chunks[-1], streamed, final.stop_reason
    assert asyncio.run(main()) == (text, "message_stop", text, "end_turn")


def test_anthropic_follows_max_tokens_stop_sequences_thinking_and_formats(sdk):
    anthropic = sdk("anthropic")
    c = anthropic.Anthropic()
    r = c.messages.create(model="claude-opus-5-5", max_tokens=3, messages=USER)
    assert r.stop_reason == "max_tokens" and r.content[0].text == "mock reply"
    r = c.messages.create(model="claude-opus-5-5", max_tokens=100, messages=USER, stop_sequences=["Paris"])
    assert (r.stop_reason, r.stop_sequence, r.content[0].text) == ("stop_sequence", "Paris",
                                                                  "mock reply to: What is the weather in ")
    r = c.messages.create(model="claude-opus-5-5", max_tokens=100, messages=USER, thinking={"type": "adaptive"})
    assert [b.type for b in r.content] == ["thinking", "text"], "code must not assume content[0] is the text"
    r = c.messages.create(model="claude-opus-5-5", max_tokens=100, messages=[*USER, {"role": "system",
                                                                                   "content": "In French."}])
    assert r.stop_reason == "end_turn", "a system message after a user message is a mid-conversation instruction"
    schema = {"type": "object", "properties": {"city": {"type": "string"}, "temp": {"type": "number"},
                                               "tags": {"type": "array", "items": {"type": "string"}}}}
    r = c.messages.create(model="claude-opus-5-5", max_tokens=100, messages=USER,
                          output_config={"format": {"type": "json_schema", "schema": schema}})
    assert json.loads(r.content[0].text) == {"city": "mock", "temp": 0.0, "tags": []}
    assert c.messages.count_tokens(model="claude-opus-5-5", messages=USER).input_tokens == math.ceil(7 * 1.3)


# ---- openai -------------------------------------------------------------------------------------------------

def test_openai_reply_has_the_sdk_shape(sdk, tmp_path):
    openai = sdk("openai")
    client = openai.OpenAI()
    r = client.chat.completions.create(model="gpt-5.5", messages=[{"role": "developer", "content": "Be brief."}, *USER],
                                       n=2, seed=7, temperature=0.2, max_completion_tokens=200, user="u1")
    assert (r.object, r.model, len(r.choices), r.choices[1].index) == ("chat.completion", "gpt-5.5", 2, 1)
    m = r.choices[0].message
    assert (m.role, m.content, m.tool_calls, r.choices[0].finish_reason) == (
        "assistant", "mock reply to: What is the weather in Paris today?", None, "stop")
    assert r.usage.total_tokens == r.usage.prompt_tokens + r.usage.completion_tokens
    assert isinstance(r, openai.types.chat.ChatCompletion) and r.id.startswith("chatcmpl-")
    assert logged(tmp_path)[-1]["usage"] == {"input_tokens": r.usage.prompt_tokens,
                                             "output_tokens": r.usage.completion_tokens}


O_BAD = [
    ({"messages": [{"content": "hi"}]}, "messages[0] has no role", False),
    ({"messages": [{"role": "user"}]}, "messages[0] (user) has no content", False),
    ({"messages": [{"role": "robot", "content": "hi"}]}, "messages[0].role is 'robot'", False),
    ({"messages": [{"role": "user", "content": [{"type": "image", "source": {}}]}]}, "that is Anthropic's shape", False),
    ({"messages": [*USER, {"role": "tool", "tool_call_id": "c1", "content": "x"}]},
     "must answer a tool call of the assistant message before it", False),
    ({"messages": [*USER, {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {
        "name": "get_weather", "arguments": "{}"}}]}, {"role": "user", "content": "and?"}]},
     "must be followed by tool messages answering each tool_call_id; no answer for c1", False),
    ({"messages": [*USER, {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {
        "name": "get_weather", "arguments": {"city": "Paris"}}}]}]}, "arguments must be a JSON string", False),
    ({"tools": [WEATHER]}, "tools[0] is in Anthropic's shape", False),
    ({"tool_choice": "auto"}, "'tool_choice' is only allowed when 'tools' are specified", False),
    ({"tools": [OA_WEATHER], "tool_choice": {"type": "function", "function": {"name": "x"}}},
     "names the tool 'x'", False),
    ({"response_format": {"type": "json_object"}}, "must contain the word 'json'", False),
    ({"response_format": {"type": "json_schema", "json_schema": {"schema": {}}}}, "json_schema.name is required", False),
    ({"stream_options": {"include_usage": True}}, "only allowed when 'stream' is enabled", False),
    ({"temperature": 2.5}, "temperature must be from 0 to 2", False),
    ({"n": 0}, "n must be from 1 to 128", False),
    ({"stop": ["a", "b", "c", "d", "e"]}, "up to 4 strings", False),
    ({"max_tokns": 5}, "unexpected keyword argument 'max_tokns' (did you mean 'max_tokens'?)", True),
    ({"system": "Be brief."}, "the system prompt is the first message", True),
    ({"model": None}, "is missing model", True),
]


@pytest.mark.parametrize("change,why,type_error", O_BAD, ids=[w[:40] for _, w, _ in O_BAD])
def test_openai_refuses_what_the_api_refuses(sdk, tmp_path, change, why, type_error):
    openai = sdk("openai")
    kwargs = {k: v for k, v in {"model": "gpt-5.5", "messages": USER, **change}.items() if v is not None}
    with pytest.raises(openai.BadRequestError) as info:
        openai.OpenAI().chat.completions.create(**kwargs)
    e = info.value
    assert why in e.message and why in e.body["message"] and (e.status_code, isinstance(e, TypeError)) == (
        400, type_error)
    assert logged(tmp_path)[-1]["error"]["kind"] == "refused"


def test_openai_tool_calls_and_structured_output(sdk, tmp_path):
    path = replies(tmp_path, {r"weather in (\w+)": {"tool": "get_weather", "input": {"city": r"\1"}},
                              "sunny": "Sunny, 21 C."}, "replies.json")
    openai = sdk("openai", replies=path)
    c = openai.OpenAI()
    msgs = list(USER)
    r = c.chat.completions.create(model="gpt-5.5", messages=msgs, tools=[OA_WEATHER])
    m = r.choices[0].message
    assert r.choices[0].finish_reason == "tool_calls" and m.content is None
    call = m.tool_calls[0]
    assert (call.type, call.function.name, json.loads(call.function.arguments)) == ("function", "get_weather",
                                                                                    {"city": "Paris"})
    msgs += [m, {"role": "tool", "tool_call_id": call.id, "content": "sunny"}]    # the SDK's message, passed back
    assert c.chat.completions.create(model="gpt-5.5", messages=msgs, tools=[OA_WEATHER]).choices[0].message.content \
        == "Sunny, 21 C."
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}, "n": {"type": "integer", "minimum": 1}}}
    r = c.chat.completions.create(model="gpt-5.5", messages=[{"role": "user", "content": "hi"}], response_format={
        "type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": True}})
    assert json.loads(r.choices[0].message.content) == {"ok": False, "n": 1}
    r = c.chat.completions.create(model="gpt-5.5", messages=[{"role": "user", "content": "Answer in JSON."}],
                                  response_format={"type": "json_object"})
    assert json.loads(r.choices[0].message.content) == {"reply": "mock reply to: Answer in JSON."}
    r = c.chat.completions.create(model="gpt-5.5", messages=[{"role": "user", "content": "go"}], max_completion_tokens=2)
    assert r.choices[0].finish_reason == "length" and r.choices[0].message.content == "mock"


def test_openai_embeddings(sdk, tmp_path):
    openai = sdk("openai")
    c = openai.OpenAI()
    e = c.embeddings.create(model="text-embedding-3-small", input=["the cat sat", "the cat sat down", "stocks fell"])
    v = [d.embedding for d in e.data]
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))
    assert (e.object, e.data[2].index, len(v[0])) == ("list", 2, 1536) and abs(dot(v[0], v[0]) - 1) < 1e-4
    assert dot(v[0], v[1]) > 0.5 > dot(v[0], v[2]), "texts that share words point the same way"
    assert c.embeddings.create(model="text-embedding-3-small", input="the cat sat").data[0].embedding == v[0]
    assert len(c.embeddings.create(model="text-embedding-3-large", input="x", dimensions=256).data[0].embedding) == 256
    b64 = c.embeddings.create(model="text-embedding-3-small", input="x", encoding_format="base64").data[0].embedding
    assert isinstance(b64, str) and len(base64.b64decode(b64)) == 1536 * 4
    assert e.usage.prompt_tokens == e.usage.total_tokens == math.ceil(9 * 1.3)
    for kw, why in [({"input": ""}, "empty string"), ({"input": "x", "model": "text-embedding-ada-002",
                                                       "dimensions": 8}, "does not support specifying dimensions")]:
        with pytest.raises(openai.BadRequestError, match=why):
            c.embeddings.create(**{"model": "text-embedding-3-small", **kw})
    assert logged(tmp_path)[0]["reply"] == {"vectors": 3, "dimensions": 1536}


def test_openai_streams_async_and_errors(sdk, tmp_path):
    openai = sdk("openai", fail_on_call=4)
    c = openai.OpenAI(max_retries=0)
    kw = {"model": "gpt-5.5", "messages": USER}
    text = c.chat.completions.create(**kw).choices[0].message.content
    chunks = list(c.chat.completions.create(**kw, stream=True, stream_options={"include_usage": True}))
    assert chunks[0].choices[0].delta.role == "assistant" and chunks[0].object == "chat.completion.chunk"
    assert "".join(ch.choices[0].delta.content or "" for ch in chunks if ch.choices) == text
    assert chunks[-2].choices[0].finish_reason == "stop" and chunks[-1].choices == [] and chunks[-1].usage.total_tokens
    with c.chat.completions.create(**kw, stream=True, tools=[OA_WEATHER], tool_choice="required") as s:
        args = "".join(ch.choices[0].delta.tool_calls[0].function.arguments for ch in s
                       if ch.choices[0].delta.tool_calls)
    assert json.loads(args) == {"city": "mock"}
    with pytest.raises(openai.RateLimitError) as info:
        c.chat.completions.create(**kw)
    assert isinstance(info.value, openai.APIError) and info.value.status_code == 429 and info.value.code == \
        "rate_limit_exceeded"

    async def main():
        a = openai.AsyncOpenAI()
        r = await a.chat.completions.create(**kw)
        parts = [ch.choices[0].delta.content async for ch in await a.chat.completions.create(**kw, stream=True)]
        e = await a.embeddings.create(model="text-embedding-3-small", input="x")
        return r.choices[0].message.content, "".join(p or "" for p in parts), len(e.data)
    assert asyncio.run(main()) == (text, text, 1)
    with pytest.raises(AttributeError, match="it answers chat.completions.create and embeddings.create"):
        c.responses.create(model="gpt-5.5", input="hi")


def _nones(v) -> list:
    if isinstance(v, dict):
        return [k for k, x in v.items() if x is None] + [n for x in v.values() for n in _nones(x)]
    return [n for x in v for n in _nones(x)] if isinstance(v, list) else []


def test_reply_objects_dump_and_validate_as_the_sdks_models_do(sdk):
    anthropic = sdk("anthropic")
    r = anthropic.Anthropic().messages.create(model="claude-opus-5-5", max_tokens=256, messages=USER)
    assert _nones(r.model_dump()) and not _nones(r.model_dump(exclude_none=True)), "exclude_none drops every None"
    assert not _nones(r.to_dict(exclude_none=True)) and "null" not in r.model_dump_json(exclude_none=True)
    assert r.model_dump(include={"id", "usage"}, exclude={"usage": {"output_tokens"}}).keys() == {"id", "usage"}
    assert "output_tokens" not in r.model_dump(include={"usage"}, exclude={"usage": {"output_tokens"}})["usage"]
    with pytest.raises(TypeError, match="exclude_nones"):
        r.model_dump(exclude_nones=True)
    assert anthropic.types.Message.model_validate(r.model_dump()) == r, "a dumped reply reads back as itself"

    m = anthropic.types.Message.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "stop_reason": "tool_use",
        "content": [{"type": "text", "text": "Checking."}, {"type": "tool_use", "id": "t1", "name": "get_weather",
                                                           "input": {"city": "Paris"}}],
        "usage": {"input_tokens": 3, "output_tokens": 5}})
    assert isinstance(m.content[1], anthropic.types.ToolUseBlock) and m.content[1].input["city"] == "Paris"
    assert isinstance(m.usage, anthropic.types.Usage) and m.stop_sequence is None and m.usage.cache_read_input_tokens is None
    with pytest.raises(ValueError, match="validation errors? for Message") as e:
        anthropic.types.Message.model_validate({"type": "message", "role": "assistant"})
    assert "id\n  Field required" in str(e.value) and "usage\n  Field required" in str(e.value)

    openai = sdk("openai")
    c = openai.types.chat.ChatCompletion.model_validate_json(json.dumps({
        "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "gpt-5.5", "choices": [
            {"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}]}}]}))
    call = c.choices[0].message.tool_calls[0]
    assert isinstance(call, openai.types.chat.ChatCompletionMessageFunctionToolCall) and call.function.name == "get_weather"
    assert c.choices[0].message.content is None and c.usage is None and c.model_copy(update={"id": "x"}).id == "x"
    done = openai.OpenAI().chat.completions.create(model="gpt-5.5", messages=USER)
    assert openai.types.chat.ChatCompletion.model_validate(done.model_dump()) == done


def test_the_modules_app_code_imports_types_from(sdk):
    anthropic = sdk("anthropic")
    from anthropic.types.message import Message
    from anthropic.types.text_block import TextBlock
    from anthropic.types.tool_use_block import ToolUseBlock
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    assert (Message, TextBlock, ToolUseBlock) == (anthropic.types.Message, anthropic.types.TextBlock,
                                                  anthropic.types.ToolUseBlock)
    with pytest.raises(ModuleNotFoundError, match="the mock anthropic package .* does not have it"):
        importlib.import_module("anthropic.types.beta_message")

    openai = sdk("openai")
    from openai.types.chat.chat_completion import ChatCompletion, Choice
    from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall, Function
    from openai.types.chat.chat_completion_chunk import ChatCompletionChunk, ChoiceDelta
    from openai.types.completion_usage import CompletionUsage
    from openai.types.chat.completion_create_params import CompletionCreateParams
    r = openai.OpenAI().chat.completions.create(model="gpt-5.5", messages=USER)
    assert isinstance(r, ChatCompletion) and isinstance(r.choices[0], Choice) and isinstance(r.usage, CompletionUsage)
    assert ChatCompletionMessageToolCall.__name__ and Function and ChatCompletionChunk and ChoiceDelta
    assert MessageCreateParamsNonStreaming is CompletionCreateParams is dict, "request types are plain dicts"


# ---- testbench.yaml and the expectation ---------------------------------------------------------------------

def _step(tmp_path, **step):
    return {"project": "p", "plans": {"p": {"steps": [{"name": "s", "run": "true", **step}]}}}


def test_config_takes_mocks_and_says_what_to_write(tmp_path):
    (tmp_path / "r.yaml").write_text("- {match: hi, text: hello}\n")
    cfg = C.validate(_step(tmp_path, mocks=["openai", "anthropic"]), tmp_path)
    assert cfg["plans"]["p"]["steps"][0]["mocks"] == {"openai": {}, "anthropic": {}}
    cfg = C.validate(_step(tmp_path, mocks={"anthropic": {"replies": "r.yaml", "fail_on_call": 3}}), tmp_path)
    assert cfg["plans"]["p"]["steps"][0]["mocks"] == {"anthropic": {"replies": "r.yaml", "fail_on_call": 3}}
    assert C.validate(_step(tmp_path), tmp_path)["plans"]["p"]["steps"][0]["mocks"] == {}
    (tmp_path / "bad.yaml").write_text("- {match: '(unclosed', text: x}\n")
    (tmp_path / "bad2.yaml").write_text("- {match: hi, tool: get_weather, input: city}\n")
    for mocks, why in [(["gemini"], "names 'gemini'; testbench has mock clients for anthropic, openai"),
                       ({"openai": {"fail_with": "rate_limit"}}, "set fail_on_call too"),
                       ({"openai": {"fail_on_call": 0}}, "calls count from 1"),
                       ({"openai": {"fail_on_call": 2, "fail_with": "boom"}}, "use one of rate_limit"),
                       ({"openai": {"reply": "x"}}, "has no 'reply'; the options are replies"),
                       ({"openai": {"replies": "nope.yaml"}}, "nope.yaml does not exist"),
                       ({"openai": {"replies": "bad.yaml"}}, "not a regular expression"),
                       ({"openai": {"replies": "bad2.yaml"}}, "input: is a mapping"),
                       ([1], "lists the mock clients by name")]:
        with pytest.raises(C.ConfigError, match=f"mocks.*{re.escape(why)}"):
            C.validate(_step(tmp_path, mocks=mocks), tmp_path)
    for spec, why in [({"gemini": {"calls": 1}}, "names 'gemini'"), ({"openai": {"cost": 1}}, "has no 'cost'"),
                      ({"openai": {"calls": "~= 3"}}, "neither a tolerance")]:
        with pytest.raises(C.ConfigError, match=f"mock_calls.*{why}"):
            C.validate(_step(tmp_path, expect={"mock_calls": spec}), tmp_path)


def test_mock_calls_expectation_reads_the_log(tmp_path):
    lines = [{"sdk": "anthropic", "endpoint": "messages.create", "call": 1, "model": "claude-opus-5-5",
              "known_model": True, "error": None, "usage": {"input_tokens": 10, "output_tokens": 30},
              "reply": {"text": None, "tool_calls": [{"name": "w", "input": {}}]}},
             {"sdk": "anthropic", "endpoint": "messages.create", "call": 2, "model": "my-model", "known_model": False,
              "error": None, "usage": {"input_tokens": 12, "output_tokens": 40}, "reply": {"text": "x"}},
             {"sdk": "anthropic", "endpoint": "messages.create", "call": 3, "model": "claude-opus-5-5",
              "error": {"kind": "refused", "type": "BadRequestError", "message": "messages[0] has no role"}},
             {"sdk": "openai", "endpoint": "embeddings.create", "call": 1, "model": "text-embedding-3-small",
              "error": {"kind": "injected", "type": "RateLimitError", "message": "fail_on_call", "retried": True}}]
    (tmp_path / "mock_calls.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines) + "not json\n")
    rules = {"anthropic": {"calls": "<= 5", "output_tokens": "< 50", "tool_calls": 1}, "openai": {"injected": 1}}
    found = {c["check"]: c for c in expect.check({"mock_calls": rules}, workdir=tmp_path, result={
        "kind": "run", "exit_code": 0, "step_dir": str(tmp_path), "mocks": {"anthropic": {}, "openai": {}}})}
    calls = found["mock anthropic calls <= 5"]
    assert calls["ok"] and R.check_number(calls) == 3
    assert "messages.create my-model (not a model the mock knows) x1" in calls["detail"]
    tokens = found["mock anthropic output_tokens < 50"]
    assert not tokens["ok"] and tokens["detail"].startswith("70 (wanted < 50.0); the mock's count (words x 1.3)")
    assert found["mock anthropic tool_calls 1"]["ok"]
    refused = found["mock anthropic refused 0"]                    # on by default for every mocked SDK
    assert not refused["ok"] and "call 3: BadRequestError: messages[0] has no role" in refused["detail"]
    assert found["mock openai injected 1"]["ok"] and "1 retried by the SDK itself" in found["mock openai injected 1"][
        "detail"]
    assert found["mock openai refused 0"]["ok"]
    none = expect.check({"mock_calls": {"openai": {"calls": ">= 1"}}}, workdir=tmp_path / "empty", result={})
    assert not none[0]["ok"] and "no calls were logged (is `mocks: [openai]` on this step?)" in none[0]["detail"]


# ---- end to end: a plan whose app calls both SDKs ---------------------------------------------------------------

APP = '''
import json
import anthropic
import openai

claude = anthropic.Anthropic()
tools = [{"name": "get_weather", "description": "Weather for a city",
          "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}]
messages = [{"role": "user", "content": "What is the weather in Paris?"}]
while True:
    r = claude.messages.create(model="claude-opus-5-5", max_tokens=1024, tools=tools, messages=messages)
    messages.append({"role": "assistant", "content": r.content})
    if r.stop_reason != "tool_use":
        break
    messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": b.id, "content": "sunny, 21 C"}
                                                 for b in r.content if b.type == "tool_use"]})
answer = next(b.text for b in r.content if b.type == "text")
print("claude:", answer)
gpt = openai.OpenAI()
emb = gpt.embeddings.create(model="text-embedding-3-small", input=["paris weather", "tokyo weather"])
summary = gpt.chat.completions.create(model="gpt-5.5", messages=[{"role": "user", "content": "Summarize: " + answer}])
print("gpt:", summary.choices[0].message.content)
json.dump({"answer": answer, "dims": len(emb.data[0].embedding)}, open("out.json", "w"))
'''

SLOPPY = '''
import anthropic
try:
    anthropic.Anthropic().messages.create(model="claude-opus-5-5", max_tokens=100, messages=[
        {"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}])
except anthropic.APIError as e:
    print("ignored", e.status_code)
'''

WHICH = ("python -c \"import importlib.util as u; s = u.find_spec('openai'); "
         "print('MOCK' if s and 'mock_clients' in str(s.origin) else 'NO MOCK')\"")


def test_plan_runs_an_app_on_the_mocks(tmp_path):
    root = tmp_path / "llmapp"
    root.mkdir()
    (root / "app.py").write_text(APP)
    (root / "sloppy.py").write_text(SLOPPY)
    (root / "replies.yaml").write_text(textwrap.dedent(r'''
        - match: "weather in (\\w+)"
          tool: get_weather
          input: {city: "\\1"}
        - match: sunny
          text: "It is sunny in Paris, 21 C."
        '''))
    make_notebook(root / "nb.ipynb", ["import openai\nr = openai.OpenAI().chat.completions.create(model='gpt-5.5', "
                                      "messages=[{'role': 'user', 'content': 'hi'}])\nprint(r.choices[0].message.content)"])
    write_config(root, {"project": "llmapp", "plans": {
        "mocked": {"steps": [
            {"name": "app", "run": "python app.py", "mocks": {"anthropic": {"replies": "replies.yaml"}, "openai": None},
             "expect": {"json": {"out.json": {"answer": "It is sunny in Paris, 21 C.", "dims": 1536}},
                        "text": {"stdout": ["claude: It is sunny in Paris"]},
                        "mock_calls": {"anthropic": {"calls": 2, "tool_calls": 1, "output_tokens": "<= 2000"},
                                       "openai": {"calls": 2}}}},
            {"name": "notebook", "notebook": "nb.ipynb", "mocks": ["openai"],
             "expect": {"text": {"stdout": ["mock reply to: hi"]}, "mock_calls": {"openai": {"calls": 1}}}},
            {"name": "no mocks", "run": WHICH, "expect": {"text": {"stdout": ["NO MOCK"]}}}]},
        "sloppy": {"steps": [{"name": "app", "run": "python sloppy.py", "mocks": ["anthropic"]}]}}})
    r = cli(root, "run", "mocked", "--wait", "180")
    assert r.returncode == 0, r.stdout + r.stderr
    run = sorted((root / ".testbench" / "runs").iterdir())[-1]
    rep = json.loads((run / "report.json").read_text())
    checks = {c["check"]: c for s in rep["steps"] for c in s["expectations"]}
    assert checks["mock anthropic calls 2"]["detail"].startswith("2 (wanted == 2); messages.create claude-opus-5-5 x2")
    assert checks["mock openai calls 2"]["ok"] and checks["mock anthropic refused 0"]["ok"]
    logs = [json.loads(line) for line in (run / "steps" / "01-app" / "mock_calls.jsonl").read_text().splitlines()]
    assert [(x["sdk"], x["endpoint"]) for x in logs] == [("anthropic", "messages.create")] * 2 + [
        ("openai", "embeddings.create"), ("openai", "chat.completions.create")]
    assert any(e["path"] == "steps/01-app/mock_calls.jsonl" for e in rep["evidence"]), "the log is evidence"
    assert (run / "steps" / "02-notebook" / "mock_calls.jsonl").is_file()

    r = cli(root, "run", "sloppy", "--wait", "120")                  # the app swallowed a 400: the step still fails
    assert r.returncode == 1, r.stdout + r.stderr
    assert "mock anthropic refused 0: 1 (wanted == 0)" in r.stdout
    assert "messages[0] has role 'system': put the system prompt in the top-level system= parameter" in r.stdout


# ---- the mock must not drift into names the real SDK lacks -------------------------------------------------------

DEEP = ["anthropic.types." + m for m in (                     # the modules app code imports types from
    "message", "text_block", "tool_use_block", "thinking_block", "redacted_thinking_block", "usage",
    "message_tokens_count", "content_block", "model", "message_param", "text_block_param", "tool_param",
    "tool_result_block_param", "tool_use_block_param")] + ["openai.types." + m for m in (
        "completion_usage", "embedding", "create_embedding_response", "chat.chat_completion",
        "chat.chat_completion_message", "chat.chat_completion_message_tool_call",
        "chat.chat_completion_message_function_tool_call", "chat.chat_completion_chunk",
        "chat.chat_completion_message_param", "chat.chat_completion_tool_param")]
MOCK_NAMES = '''
import importlib, json
import anthropic, openai, openai.types.chat
print(json.dumps({"anthropic": anthropic.__all__, "anthropic.types": anthropic.types.__all__,
                  "openai": openai.__all__, "openai.types": openai.types.__all__,
                  "openai.types.chat": openai.types.chat.__all__,
                  **{m: importlib.import_module(m).__all__ for m in DEEP},
                  "anthropic create": list(anthropic._CREATE), "anthropic count": list(anthropic._COUNT),
                  "openai create": list(openai._CREATE), "openai embed": list(openai._EMBED)}))
'''.replace("DEEP", repr(DEEP))


@pytest.mark.parametrize("name", ["anthropic", "openai"])
def test_mock_names_exist_in_the_real_sdk(name, tmp_path):
    real = pytest.importorskip(name, reason=f"the real {name} package is not installed here")
    if getattr(real, "TESTBENCH_MOCK", False):
        pytest.skip("the mock is on this interpreter's path")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(PATHS)}
    out = subprocess.run([sys.executable, "-c", MOCK_NAMES], capture_output=True, text=True, env=env, cwd=tmp_path)
    mock = json.loads(out.stdout)
    for module in [m for m in mock if m.split(".")[0] == name and " " not in m]:
        target = importlib.import_module(module)
        assert [n for n in mock[module] if not hasattr(target, n)] == [], f"{module} names the real SDK lacks"
    if name == "anthropic":
        from anthropic.resources.messages import Messages
        params = set(inspect.signature(Messages.create).parameters)
        assert set(mock["anthropic create"]) - params <= {"temperature", "top_p", "top_k"}
        assert set(mock["anthropic count"]) <= set(inspect.signature(Messages.count_tokens).parameters)
    else:
        from openai.resources.chat.completions import Completions
        from openai.resources.embeddings import Embeddings
        assert set(mock["openai create"]) <= set(inspect.signature(Completions.create).parameters)
        assert set(mock["openai embed"]) <= set(inspect.signature(Embeddings.create).parameters)
