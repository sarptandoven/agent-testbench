"""Mock `openai` package from agent-testbench (docs/mocks.md): chat completions and embeddings answered here,
deterministically, with no network and no key. It is strict like the real API: a request the API or the SDK would
refuse raises the SDK's own error, saying what is wrong. Every call is one line of mock_calls.jsonl.

Follows the openai Python SDK 3.42.0 (its chat.completions.create and embeddings.create signatures and types).
"""
from __future__ import annotations

import base64
import json
import os
import re
import struct
import time

import _testbench_mock as core
from _testbench_mock import NOT_GIVEN, NotGiven, Omit, not_given, omit

from . import types
from .types import CompletionUsage, CreateEmbeddingResponse, Embedding, _EmbeddingUsage
from .types.chat import (ChatCompletion, ChatCompletionChunk, ChatCompletionMessage,
                         ChatCompletionMessageFunctionToolCall, ChoiceDelta, ChoiceDeltaToolCall, ChunkChoice, Function)
from .types.chat import Choice as _Choice

__version__ = "3.42.0+testbench.mock"
TESTBENCH_MOCK = True
__all__ = ["types", "OpenAI", "AsyncOpenAI", "Client", "AsyncClient", "Stream", "AsyncStream", "OpenAIError",
           "APIError", "APIStatusError", "APIConnectionError", "APITimeoutError", "BadRequestError",
           "AuthenticationError", "PermissionDeniedError", "NotFoundError", "ConflictError", "UnprocessableEntityError",
           "RateLimitError", "InternalServerError", "NOT_GIVEN", "NotGiven", "not_given", "Omit", "omit",
           "DEFAULT_MAX_RETRIES"]
DEFAULT_MAX_RETRIES = 2
_MOCK = core.Mock("openai")
core.absent_modules("openai", "openai.types (and its completion_usage, embedding and create_embedding_response "
                    "modules) and openai.types.chat (and its chat_completion, chat_completion_message, "
                    "chat_completion_message_tool_call, chat_completion_message_function_tool_call and "
                    "chat_completion_chunk modules)")

# the SDK's models (openai.types.ChatModel and EmbeddingModel); any other name is accepted and noted
KNOWN_MODELS = (
    "gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra",
    "gpt-5.6-luna", "gpt-5.5", "gpt-5.5-2026-04-23", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.4-mini-2026-03-17",
    "gpt-5.4-nano-2026-03-17", "gpt-5.3-chat-latest", "gpt-5.2", "gpt-5.2-2025-12-11", "gpt-5.2-chat-latest",
    "gpt-5.2-pro", "gpt-5.2-pro-2025-12-11", "gpt-5.1", "gpt-5.1-2025-11-13", "gpt-5.1-codex", "gpt-5.1-mini",
    "gpt-5.1-chat-latest", "gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-5-2025-08-07", "gpt-5-mini-2025-08-07",
    "gpt-5-nano-2025-08-07", "gpt-5-chat-latest", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-4.1-2025-04-14",
    "gpt-4.1-mini-2025-04-14", "gpt-4.1-nano-2025-04-14", "o4-mini", "o4-mini-2025-04-16", "o3", "o3-2025-04-16",
    "o3-mini", "o3-mini-2025-01-31", "o1", "o1-2024-12-17", "o1-preview", "o1-preview-2024-09-12", "o1-mini",
    "o1-mini-2024-09-12", "gpt-4o", "gpt-4o-2024-11-20", "gpt-4o-2024-08-06", "gpt-4o-2024-05-13", "gpt-audio-mini",
    "gpt-audio-mini-2025-12-15", "gpt-4o-audio-preview", "gpt-4o-audio-preview-2024-10-01",
    "gpt-4o-audio-preview-2024-12-17", "gpt-4o-audio-preview-2025-06-03", "gpt-4o-mini-audio-preview",
    "gpt-4o-mini-audio-preview-2024-12-17", "gpt-4o-search-preview", "gpt-4o-mini-search-preview",
    "gpt-4o-search-preview-2025-03-11", "gpt-4o-mini-search-preview-2025-03-11", "chatgpt-4o-latest",
    "codex-mini-latest", "gpt-4o-mini", "gpt-4o-mini-2024-07-18", "gpt-4-turbo", "gpt-4-turbo-2024-04-09",
    "gpt-4-0125-preview", "gpt-4-turbo-preview", "gpt-4-1106-preview", "gpt-4-vision-preview", "gpt-4", "gpt-4-0314",
    "gpt-4-0613", "gpt-4-32k", "gpt-4-32k-0314", "gpt-4-32k-0613", "gpt-3.5-turbo", "gpt-3.5-turbo-16k",
    "gpt-3.5-turbo-0301", "gpt-3.5-turbo-0613", "gpt-3.5-turbo-1106", "gpt-3.5-turbo-0125", "gpt-3.5-turbo-16k-0613")
EMBEDDING_MODELS = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072, "text-embedding-ada-002": 1536}
_REQUEST = ("extra_headers", "extra_query", "extra_body", "timeout")
_CREATE = ("messages", "model", "audio", "frequency_penalty", "function_call", "functions", "logit_bias", "logprobs",
           "max_completion_tokens", "max_tokens", "metadata", "modalities", "moderation", "n", "parallel_tool_calls",
           "prediction", "presence_penalty", "prompt_cache_key", "prompt_cache_options", "prompt_cache_retention",
           "reasoning_effort", "response_format", "safety_identifier", "seed", "service_tier", "stop", "store",
           "stream", "stream_options", "temperature", "tool_choice", "tools", "top_logprobs", "top_p", "user",
           "verbosity", "web_search_options") + _REQUEST
_EMBED = ("input", "model", "dimensions", "encoding_format", "user") + _REQUEST
_HINTS = {"max_tokens_to_sample": " (the old Anthropic name; here max_completion_tokens)",
          "stop_sequences": " (that is Anthropic's; here stop)",
          "system": " (that is Anthropic's; here the system prompt is the first message: {'role': 'system', "
                    "'content': ...})",
          "top_k": " (that is Anthropic's; chat completions have no top_k)"}
_ROLE_KEYS = {"system": ("role", "content", "name"), "developer": ("role", "content", "name"),
              "user": ("role", "content", "name"),
              "assistant": ("role", "content", "name", "refusal", "tool_calls", "function_call", "audio",
                            "annotations"),
              "tool": ("role", "content", "tool_call_id"), "function": ("role", "content", "name")}
_PART_KEYS = {"text": ("type", "text", "prompt_cache_breakpoint"),
              "image_url": ("type", "image_url", "prompt_cache_breakpoint"),
              "input_audio": ("type", "input_audio"), "file": ("type", "file", "prompt_cache_breakpoint"),
              "refusal": ("type", "refusal")}
_PARTS = {"user": ("text", "image_url", "input_audio", "file"), "assistant": ("text", "refusal")}
_NAME = re.compile(r"[a-zA-Z0-9_-]{1,64}")


# ---- the SDK's errors -----------------------------------------------------------------------------------------

class OpenAIError(Exception):
    pass


class APIError(OpenAIError):
    def __init__(self, message, request, *, body):
        super().__init__(message)
        self.message, self.request, self.body = message, request, body
        info = body if isinstance(body, dict) else {}
        self.code = None if info.get("code") is None else str(info["code"])
        self.param, self.type = info.get("param"), info.get("type")


class APIStatusError(APIError):
    def __init__(self, message, *, response, body):
        super().__init__(message, response.request, body=body)
        self.response, self.status_code = response, response.status_code
        self.request_id = response.headers.get("x-request-id")


class APIConnectionError(APIError):
    def __init__(self, *, message="Connection error.", request):
        super().__init__(message, request, body=None)


class APITimeoutError(APIConnectionError):
    def __init__(self, request):
        super().__init__(message="Request timed out.", request=request)


class BadRequestError(APIStatusError):
    status_code = 400


class AuthenticationError(APIStatusError):
    status_code = 401


class PermissionDeniedError(APIStatusError):
    status_code = 403


class NotFoundError(APIStatusError):
    status_code = 404


class ConflictError(APIStatusError):
    status_code = 409


class UnprocessableEntityError(APIStatusError):
    status_code = 422


class RateLimitError(APIStatusError):
    status_code = 429


class InternalServerError(APIStatusError):
    pass


# a wrong keyword argument: the real SDK raises TypeError before sending anything; the mock's error is both
_ArgumentError = type("BadRequestError", (BadRequestError, TypeError), {})
_STATUS = {"bad_request": (400, BadRequestError, "invalid_request_error", None),
           "argument": (400, _ArgumentError, "invalid_request_error", None),
           "auth": (401, AuthenticationError, "invalid_request_error", "invalid_api_key"),
           "permission": (403, PermissionDeniedError, "invalid_request_error", None),
           "not_found": (404, NotFoundError, "invalid_request_error", "model_not_found"),
           "rate_limit": (429, RateLimitError, "requests", "rate_limit_exceeded"),
           "server": (500, InternalServerError, "server_error", None),
           "overloaded": (503, InternalServerError, "server_error", "service_unavailable")}


def _error(kind, message, param, n):
    request = core.FakeRequest("https://api.openai.com/v1/chat/completions")
    if kind == "timeout":
        return APITimeoutError(request)
    if kind == "connection":
        return APIConnectionError(message=f"Connection error. ({message})", request=request)
    status, cls, etype, code = _STATUS[kind]
    body = {"message": message, "type": etype, "param": param, "code": code}
    headers = {"x-request-id": f"req_mock_{n}", **({"retry-after": "0"} if status == 429 else {})}
    response = core.FakeResponse(status, {"error": body}, request, headers)
    return cls(f"Error code: {status} - {{'error': {body}}}", response=response, body=body)


# ---- checking a request ---------------------------------------------------------------------------------------

def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text", "") for p in content or [] if isinstance(p, dict) and p.get("type") == "text")


def _check_content(at, content, role):
    if isinstance(content, str):
        return content
    content = core.as_list(f"{at}.content", content)
    allowed = _PARTS.get(role, ("text",))
    out = []
    for j, p in enumerate(content):
        where = f"{at}.content[{j}]"
        p = core.plain(p, drop_none=True)
        if not isinstance(p, dict) or "type" not in p:
            core.refuse(f"{where} needs a type: {', '.join(allowed)}", where)
        if p["type"] not in allowed:
            hint = {"image": " (that is Anthropic's shape; here an image is {'type': 'image_url', 'image_url': "
                             "{'url': ...}})",
                    "tool_result": " (that is Anthropic's; here a tool result is a message of its own: {'role': "
                                   "'tool', 'tool_call_id': ..., 'content': ...})",
                    "tool_use": " (that is Anthropic's; here tool calls are the assistant message's tool_calls)"}
            core.refuse(f"{where}.type is {p['type']!r}; a {role} message takes {', '.join(allowed)}"
                        f"{hint.get(p['type'], '')}", f"{where}.type")
        core.keys(where, p, _PART_KEYS[p["type"]])
        if p["type"] == "text":
            core.need(isinstance(p.get("text"), str), f"{where}.text", "a string", p.get("text"))
        elif p["type"] == "image_url":
            img = p.get("image_url")
            core.need(isinstance(img, dict) and isinstance(img.get("url"), str), f"{where}.image_url",
                      "{'url': 'https://...' or 'data:image/png;base64,...'}", img)
            if "detail" in img:
                core.choice(f"{where}.image_url.detail", img["detail"], ("auto", "low", "high", "original"))
        elif p["type"] == "input_audio":
            audio = p.get("input_audio")
            core.need(isinstance(audio, dict) and isinstance(audio.get("data"), str), f"{where}.input_audio",
                      "{'data': base64 audio, 'format': 'wav' or 'mp3'}", audio)
            core.choice(f"{where}.input_audio.format", audio.get("format"), ("wav", "mp3"))
        out.append(p)
    return out


def _check_tool_calls(at, calls) -> list:
    out = []
    for k, c in enumerate(core.as_list(f"{at}.tool_calls", calls)):
        where = f"{at}.tool_calls[{k}]"
        c = core.plain(c, drop_none=True)
        core.need(isinstance(c, dict), where, "a mapping {'id', 'type': 'function', 'function': {'name', 'arguments'}}",
                  c)
        core.need(isinstance(c.get("id"), str), f"{where}.id", "the call's id, a string", c.get("id"))
        if c.get("type") == "custom":
            out.append(c)
            continue
        core.choice(f"{where}.type", c.get("type"), ("function", "custom"))
        fn = c.get("function")
        core.need(isinstance(fn, dict) and isinstance(fn.get("name"), str), f"{where}.function",
                  "{'name': ..., 'arguments': '<JSON text>'}", fn)
        if not isinstance(fn.get("arguments"), str):
            core.refuse(f"{where}.function.arguments must be a JSON string (json.dumps(...)), got "
                        f"{core.kind(fn.get('arguments'))}", f"{where}.function.arguments")
        out.append(c)
    return out


def _check_messages(msgs) -> list:
    out = []
    for i, m in enumerate(core.as_list("messages", msgs)):
        at = f"messages[{i}]"
        m = core.plain(m, drop_none=True)
        if not isinstance(m, dict):
            core.refuse(f"{at} must be a mapping {{'role': ..., 'content': ...}}, got {core.kind(m)}", at)
        role = m.get("role")
        if role is None:
            core.refuse(f"{at} has no role; a message is {{'role': 'system', 'developer', 'user', 'assistant' or "
                        "'tool', 'content': ...}", f"{at}.role")
        if role not in _ROLE_KEYS:
            core.refuse(f"{at}.role is {role!r}; it takes {', '.join(_ROLE_KEYS)}", f"{at}.role")
        core.keys(at, m, _ROLE_KEYS[role], {"tool_use_id": " (that is Anthropic's; a tool message takes "
                                                           "tool_call_id)"})
        if m.get("content") is None and role != "assistant":
            core.refuse(f"{at} ({role}) has no content", f"{at}.content")
        if role == "assistant" and m.get("content") is None and not (m.get("tool_calls") or m.get("function_call")
                                                                      or m.get("audio")):
            core.refuse(f"{at} (assistant) needs content or tool_calls", f"{at}.content")
        if m.get("content") is not None:
            m["content"] = _check_content(at, m["content"], role)
        if "tool_calls" in m:
            m["tool_calls"] = _check_tool_calls(at, m["tool_calls"])
        if role == "tool":
            core.need(isinstance(m.get("tool_call_id"), str), f"{at}.tool_call_id",
                      "the id of the tool call it answers", m.get("tool_call_id"))
        if role == "function":
            core.need(isinstance(m.get("name"), str), f"{at}.name", "the function's name", m.get("name"))
        out.append(m)
    open_calls, asked_at = [], None
    for i, m in enumerate(out):
        if m["role"] == "tool":
            if m["tool_call_id"] not in open_calls:
                core.refuse(f"messages[{i}]: a message with role 'tool' must answer a tool call of the assistant "
                            f"message before it; {m['tool_call_id']!r} is not one" +
                            (f" (still unanswered: {', '.join(open_calls)})" if open_calls else ""),
                            f"messages[{i}].tool_call_id")
            open_calls.remove(m["tool_call_id"])
            continue
        if open_calls:
            break
        if m["role"] == "assistant":
            open_calls, asked_at = [c["id"] for c in m.get("tool_calls") or []], i
    if open_calls:
        core.refuse(f"messages[{asked_at}]: an assistant message with tool_calls must be followed by tool messages "
                    f"answering each tool_call_id; no answer for {', '.join(open_calls)}", f"messages[{asked_at}]")
    return out


def _check_tools(tools) -> list:
    out = []
    for i, t in enumerate(core.as_list("tools", tools, empty_ok=True)):
        at = f"tools[{i}]"
        t = core.plain(t, drop_none=True)
        if not isinstance(t, dict):
            core.refuse(f"{at} must be a mapping {{'type': 'function', 'function': {{...}}}}, got {core.kind(t)}", at)
        if "input_schema" in t or ("name" in t and "type" not in t):
            core.refuse(f"{at} is in Anthropic's shape ({{'name', 'input_schema'}}); a tool here is {{'type': "
                        "'function', 'function': {'name': ..., 'description': ..., 'parameters': {...}}}", at)
        core.choice(f"{at}.type", t.get("type"), ("function", "custom"))
        core.keys(at, t, ("type", t["type"]))
        spec = t.get(t["type"])
        core.need(isinstance(spec, dict), f"{at}.{t['type']}", "a mapping with name", spec)
        core.keys(f"{at}.{t['type']}", spec, ("name", "description", "parameters", "strict") if t["type"] == "function"
                  else ("name", "description", "format"))
        if not (isinstance(spec.get("name"), str) and _NAME.fullmatch(spec["name"])):
            core.refuse(f"{at}.{t['type']}.name must be 1 to 64 letters, digits, _ or -, got {spec.get('name')!r}",
                        f"{at}.{t['type']}.name")
        if "parameters" in spec:
            core.need(isinstance(spec["parameters"], dict), f"{at}.function.parameters", "a JSON schema mapping",
                      spec["parameters"])
        out.append(t)
    return out


def _check_chat(kw):
    core.arguments("chat.completions.create", kw, _CREATE, ("messages", "model"), _HINTS)
    if isinstance(kw.get("response_format"), type):
        raise core.Refusal("You tried to pass a `BaseModel` class to `chat.completions.create()`; you must use "
                           "`chat.completions.parse()` instead (the mock does not answer parse(): pass "
                           "{'type': 'json_schema', 'json_schema': {'name': ..., 'schema': ...}})", "response_format",
                           "argument")
    extra = kw.get("extra_body") or {}
    core.need(isinstance(extra, dict), "extra_body", "a mapping", extra)
    for k in extra:
        if k not in _CREATE:
            core.refuse(f"Unrecognized request argument supplied: {k} (it came in extra_body)", k)
    req = {k: v for k, v in {**kw, **extra}.items() if k not in _REQUEST and (v is not None or k in ("messages", "model"))}
    notes = []
    core.need(isinstance(req["model"], str) and req["model"].strip(), "model", "a model name such as gpt-5.5",
              req["model"])
    if req["model"] not in KNOWN_MODELS:
        notes.append(f"model {req['model']!r} is not one this mock knows (a model that does not exist is a 404 "
                     "model_not_found from the real API)")
    req["messages"] = _check_messages(req["messages"])
    req["tools"] = _check_tools(req.get("tools", []))
    names = [t[t["type"]]["name"] for t in req["tools"]]
    if "tool_choice" in req:
        tc = core.plain(req["tool_choice"], drop_none=True)
        if isinstance(tc, str):
            core.choice("tool_choice", tc, ("none", "auto", "required"))
        else:
            core.need(isinstance(tc, dict), "tool_choice", "'none', 'auto', 'required' or {'type': 'function', "
                                                           "'function': {'name': ...}}", tc)
            core.choice("tool_choice.type", tc.get("type"), ("function", "custom", "allowed_tools"))
            if tc["type"] in ("function", "custom"):
                name = (tc.get(tc["type"]) or {}).get("name")
                if name not in names:
                    core.refuse(f"tool_choice names the tool {name!r}, which is not one of the request's tools "
                                f"({', '.join(names) or 'it has none'})", "tool_choice")
        if tc != "none" and not req["tools"]:
            core.refuse("Invalid value for 'tool_choice': 'tool_choice' is only allowed when 'tools' are specified",
                        "tool_choice")
        req["tool_choice"] = tc
    if "parallel_tool_calls" in req:
        core.need(isinstance(req["parallel_tool_calls"], bool), "parallel_tool_calls", "true or false",
                  req["parallel_tool_calls"])
        if not req["tools"]:
            core.refuse("Invalid value for 'parallel_tool_calls': 'parallel_tool_calls' is only allowed when 'tools' "
                        "are specified", "parallel_tool_calls")
    if "response_format" in req:
        rf = core.plain(req["response_format"], drop_none=True)
        core.need(isinstance(rf, dict), "response_format", "{'type': 'text'}, {'type': 'json_object'} or "
                                                           "{'type': 'json_schema', 'json_schema': {...}}", rf)
        core.choice("response_format.type", rf.get("type"), ("text", "json_object", "json_schema"))
        core.keys("response_format", rf, ("type", "json_schema") if rf["type"] == "json_schema" else ("type",))
        if rf["type"] == "json_schema":
            js = rf.get("json_schema")
            core.need(isinstance(js, dict), "response_format.json_schema", "{'name': ..., 'schema': {...}}", js)
            core.keys("response_format.json_schema", js, ("name", "description", "schema", "strict"))
            if not (isinstance(js.get("name"), str) and _NAME.fullmatch(js["name"])):
                core.refuse("response_format.json_schema.name is required: 1 to 64 letters, digits, _ or -",
                            "response_format.json_schema.name")
        if rf["type"] == "json_object" and not any("json" in _text_of(m.get("content")).lower() for m in req["messages"]):
            core.refuse("'messages' must contain the word 'json' in some form, to use 'response_format' of type "
                        "'json_object' (say so in the prompt, e.g. 'Answer in JSON.')", "messages")
        req["response_format"] = rf
    for key, lo, hi, integer in (("temperature", 0, 2, False), ("top_p", 0, 1, False), ("n", 1, 128, True),
                                 ("frequency_penalty", -2, 2, False), ("presence_penalty", -2, 2, False),
                                 ("max_tokens", 1, None, True), ("max_completion_tokens", 1, None, True),
                                 ("seed", None, None, True), ("top_logprobs", 0, 20, True)):
        if key in req:
            core.number(key, req[key], lo, hi, integer)
    for key in ("logprobs", "store", "stream"):
        if key in req:
            core.need(isinstance(req[key], bool), key, "true or false", req[key])
    for key in ("user", "safety_identifier", "prompt_cache_key"):
        if key in req:
            core.need(isinstance(req[key], str), key, "a string", req[key])
    for key, options in (("reasoning_effort", ("none", "minimal", "low", "medium", "high", "xhigh", "max")),
                         ("service_tier", ("auto", "default", "flex", "scale", "priority", "fast")),
                         ("verbosity", ("low", "medium", "high")), ("prompt_cache_retention", ("in_memory", "24h"))):
        if key in req:
            core.choice(key, req[key], options)
    for key in ("audio", "prediction", "web_search_options", "moderation", "prompt_cache_options", "logit_bias"):
        if key in req:
            core.need(isinstance(req[key], dict), key, "a mapping", req[key])
    if "top_logprobs" in req and not req.get("logprobs"):
        core.refuse("top_logprobs needs logprobs: true", "top_logprobs")
    if "stop" in req:
        stops = [req["stop"]] if isinstance(req["stop"], str) else core.as_list("stop", req["stop"], empty_ok=True)
        if len(stops) > 4 or not all(isinstance(s, str) for s in stops):
            core.refuse("stop is a string or a list of up to 4 strings", "stop")
        req["stop"] = stops
    if "stream_options" in req:
        so = req["stream_options"]
        core.need(isinstance(so, dict), "stream_options", "a mapping such as {'include_usage': True}", so)
        core.keys("stream_options", so, ("include_usage", "include_obfuscation"))
        if not req.get("stream"):
            core.refuse("The 'stream_options' parameter is only allowed when 'stream' is enabled", "stream_options")
    if "metadata" in req:
        md = req["metadata"]
        core.need(isinstance(md, dict) and len(md) <= 16 and all(isinstance(k, str) and isinstance(v, str)
                                                                 for k, v in md.items()),
                  "metadata", "a mapping of up to 16 string keys to string values", md)
    if "modalities" in req:
        for j, x in enumerate(core.as_list("modalities", req["modalities"])):
            core.choice(f"modalities[{j}]", x, ("text", "audio"))
    if "functions" in req or "function_call" in req:
        notes.append("functions and function_call are deprecated in favor of tools and tool_choice")
    if "max_tokens" in req:
        notes.append("max_tokens is deprecated in favor of max_completion_tokens, and reasoning models (o-series, "
                     "gpt-5 and later) refuse it")
    req.update(known_model=req["model"] in KNOWN_MODELS, notes=notes)
    return req


def _check_embeddings(kw):
    core.arguments("embeddings.create", kw, _EMBED, ("input", "model"))
    req = {k: v for k, v in kw.items() if k not in _REQUEST}
    inp, notes = req["input"], []
    if isinstance(inp, str):
        if not inp:
            core.refuse("input is an empty string, which the API refuses; embed non-empty text", "input")
        texts = [inp]
    else:
        items = core.as_list("input", inp)
        if len(items) > 2048:
            core.refuse(f"input holds {len(items)} items; one request takes at most 2048", "input")
        if all(isinstance(x, str) for x in items):
            if not all(items):
                core.refuse("input holds an empty string, which the API refuses", "input")
            texts = items
        elif all(core.is_int(x) for x in items):
            texts = [" ".join(map(str, items))]
        elif all(isinstance(x, list) and x and all(core.is_int(t) for t in x) for x in items):
            texts = [" ".join(map(str, x)) for x in items]
        else:
            core.refuse("input is a string, a list of strings, a list of token ids or a list of lists of token ids",
                        "input")
    core.need(isinstance(req["model"], str) and req["model"].strip(), "model", "a model name such as "
                                                                             "text-embedding-3-small", req["model"])
    if req["model"] not in EMBEDDING_MODELS:
        notes.append(f"model {req['model']!r} is not an embedding model this mock knows")
    if "dimensions" in req:
        core.number("dimensions", req["dimensions"], 1, None, integer=True)
        if req["model"] == "text-embedding-ada-002":
            core.refuse("This model does not support specifying dimensions.", "dimensions")
    if "encoding_format" in req:
        core.choice("encoding_format", req["encoding_format"], ("float", "base64"))
    if "user" in req:
        core.need(isinstance(req["user"], str), "user", "a string", req["user"])
    return {**req, "texts": texts, "known_model": req["model"] in EMBEDDING_MODELS, "notes": notes}


# ---- answering ------------------------------------------------------------------------------------------------

def _last_text(messages) -> str:
    """What the replies are matched against: the tool results when they come last, else the last user message."""
    results = []
    for m in reversed(messages):
        if m["role"] not in ("tool", "function"):
            break
        results.insert(0, _text_of(m.get("content")))
    if results:
        return "\n".join(results)
    return next((_text_of(m.get("content")) for m in reversed(messages) if m["role"] == "user"), "")


def _input_tokens(req) -> int:
    parts = [_text_of(m.get("content")) for m in req["messages"]]
    parts += [c["function"]["arguments"] for m in req["messages"] for c in m.get("tool_calls") or []
              if c.get("type") == "function"]
    parts.append(json.dumps(req["tools"]) if req["tools"] else "")
    return max(1, core.tokens(" ".join(parts)))


def _completion(req, n):
    tc = req.get("tool_choice")
    tools = {} if tc == "none" else {t[t["type"]]["name"]: t[t["type"]].get("parameters") for t in req["tools"]}
    forced = "any" if tc == "required" else (tc.get(tc["type"]) or {}).get("name") if isinstance(tc, dict) and \
        tc.get("type") in ("function", "custom") else None
    rf = req.get("response_format") or {}
    structured = (rf.get("json_schema") or {}).get("schema", {"type": "object"}) if rf.get("type") == "json_schema" \
        else True if rf.get("type") == "json_object" else None
    r = _MOCK.reply(_last_text(req["messages"]), tools=tools, forced=forced, structured=structured)
    calls = r["tool_calls"][:1] if req.get("parallel_tool_calls") is False else r["tool_calls"]
    text, finish = r["text"], "stop"
    if text is not None:
        text, stop, _ = core.cut(text, req.get("max_completion_tokens", req.get("max_tokens")), req.get("stop"))
        finish = "length" if stop == "max_tokens" else "stop"
    calls = calls if finish != "length" else []
    if calls:
        finish = "tool_calls"
    tool_calls = [ChatCompletionMessageFunctionToolCall(id=f"call_mock{n:03d}_{i}", type="function", function=Function(
        name=c["name"], arguments=json.dumps(c["input"]))) for i, c in enumerate(calls)] or None
    count = req.get("n", 1)
    out_tokens = max(1, core.tokens(text) + sum(core.tokens(json.dumps(c["input"])) for c in calls)) * count
    in_tokens = _input_tokens(req)
    choices = [_Choice(index=k, finish_reason=finish, logprobs=None, message=ChatCompletionMessage(
        role="assistant", content=text, refusal=None, annotations=[], audio=None, function_call=None,
        tool_calls=tool_calls)) for k in range(count)]
    comp = ChatCompletion(id=f"chatcmpl-mock{core.digest('openai', n, req['model'], r)}", object="chat.completion",
                          created=int(time.time()), model=req["model"], choices=choices, service_tier=req.get(
                              "service_tier"), system_fingerprint="fp_testbench_mock", usage=CompletionUsage(
                              prompt_tokens=in_tokens, completion_tokens=out_tokens,
                              total_tokens=in_tokens + out_tokens, completion_tokens_details=None,
                              prompt_tokens_details=None))
    comp._request_id = f"req_mock_{n}"
    info = {"reply": {"text": text, "tool_calls": calls}, "rule": r["rule"], "stop_reason": finish,
            "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens}}
    return comp, info


def _chunks(comp, include_usage) -> list:
    """The chunks a streamed completion arrives in: the role, the text a word at a time, each tool call's name
    then its arguments, the finish reason; then the usage, when stream_options asks for it."""
    out = []
    base = {"id": comp.id, "object": "chat.completion.chunk", "created": comp.created, "model": comp.model,
            "service_tier": comp.service_tier, "system_fingerprint": comp.system_fingerprint}

    def chunk(index, finish=None, **delta):
        fields = {"role": None, "content": None, "tool_calls": None, "refusal": None, "function_call": None, **delta}
        out.append(ChatCompletionChunk(**base, usage=None, choices=[ChunkChoice(
            index=index, delta=ChoiceDelta(**fields), finish_reason=finish, logprobs=None)]))

    for ch in comp.choices:
        m = ch.message
        chunk(ch.index, role="assistant", content="" if m.content is not None else None)
        for p in core.pieces(m.content or ""):
            chunk(ch.index, content=p)
        for i, call in enumerate(m.tool_calls or []):
            chunk(ch.index, tool_calls=[ChoiceDeltaToolCall(index=i, id=call.id, type="function", function=Function(
                name=call.function.name, arguments=""))])
            chunk(ch.index, tool_calls=[ChoiceDeltaToolCall(index=i, id=None, type=None, function=Function(
                name=None, arguments=call.function.arguments))])
        chunk(ch.index, finish=ch.finish_reason)
    if include_usage:
        out.append(ChatCompletionChunk(**base, choices=[], usage=comp.usage))
    return out




def _embeddings(req, n):
    dims = req.get("dimensions") or EMBEDDING_MODELS.get(req["model"], 1536)
    data = []
    for i, text in enumerate(req["texts"]):
        vec = core.embed(text, dims)
        if req.get("encoding_format") == "base64":
            vec = base64.b64encode(struct.pack(f"<{dims}f", *vec)).decode()
        data.append(Embedding(object="embedding", index=i, embedding=vec))
    count = max(1, core.tokens(" ".join(req["texts"])))
    resp = CreateEmbeddingResponse(object="list", data=data, model=req["model"],
                                   usage=_EmbeddingUsage(prompt_tokens=count, total_tokens=count))
    resp._request_id = f"req_mock_{n}"
    return resp, {"reply": {"vectors": len(data), "dimensions": dims}, "usage": {"input_tokens": count,
                                                                                 "output_tokens": 0}}


Stream, AsyncStream = core.Stream, core.AsyncStream


# ---- the client -----------------------------------------------------------------------------------------------

class _Resource:
    _path = ""

    def __init__(self, client):
        self._client = client

    def _call(self, endpoint, kwargs, check, answer):
        return core.call(_MOCK, endpoint, core.given(kwargs), retries=self._client.max_retries, check=check,
                         answer=answer, error=_error)

    def __getattr__(self, name):
        raise AttributeError(f"the mock openai client has no {self._path}.{name}; it answers "
                             "chat.completions.create and embeddings.create (docs/mocks.md)")


class Completions(_Resource):
    _path = "chat.completions"

    def _create(self, kwargs):
        comp = self._call("chat.completions.create", kwargs, _check_chat, _completion)
        if not kwargs.get("stream"):
            return comp
        chunks = _chunks(comp, (kwargs.get("stream_options") or {}).get("include_usage"))
        return (AsyncStream if self._client._async else Stream)(chunks)

    def create(self, **kwargs):
        return self._create(kwargs)


class AsyncCompletions(Completions):
    async def create(self, **kwargs):
        return self._create(kwargs)


class Chat(_Resource):
    _path = "chat"

    def __init__(self, client):
        super().__init__(client)
        self.completions = (AsyncCompletions if client._async else Completions)(client)


class Embeddings(_Resource):
    _path = "embeddings"

    def create(self, **kwargs):
        return self._call("embeddings.create", kwargs, _check_embeddings, _embeddings)


class AsyncEmbeddings(Embeddings):
    async def create(self, **kwargs):
        return Embeddings.create(self, **kwargs)


_CLIENT_OPTIONS = ("api_key", "admin_api_key", "workload_identity", "organization", "project", "webhook_secret",
                   "provider", "base_url", "data_residency", "websocket_base_url", "timeout", "max_retries",
                   "default_headers", "default_query", "http_client", "_strict_response_validation",
                   "_enforce_credentials")


class _Client:
    _async = False

    def __init__(self, **options):
        options = core.given(options)
        for name in options:
            if name not in _CLIENT_OPTIONS:
                raise TypeError(f"{type(self).__name__}.__init__() got an unexpected keyword argument {name!r}")
        self._options = options
        self.api_key = options.get("api_key") or os.environ.get("OPENAI_API_KEY")        # any key, or none, works
        self.organization = options.get("organization") or os.environ.get("OPENAI_ORG_ID")
        self.project = options.get("project") or os.environ.get("OPENAI_PROJECT_ID")
        self.base_url = str(options.get("base_url") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1")
        self.max_retries = options.get("max_retries", DEFAULT_MAX_RETRIES)
        self.timeout = options.get("timeout", 600.0)
        self.chat = Chat(self)
        self.embeddings = (AsyncEmbeddings if self._async else Embeddings)(self)

    def with_options(self, **options):
        return type(self)(**{**self._options, **options})

    copy = with_options

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        raise AttributeError(f"the mock openai client has no {name!r}; it answers chat.completions.create and "
                             "embeddings.create (docs/mocks.md)")


class OpenAI(_Client):
    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class AsyncOpenAI(_Client):
    _async = True

    async def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()


Client, AsyncClient = OpenAI, AsyncOpenAI
