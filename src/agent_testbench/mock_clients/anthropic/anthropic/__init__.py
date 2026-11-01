"""Mock `anthropic` package from agent-testbench (docs/mocks.md): the Messages API answered here, deterministically,
with no network and no key. It is strict like the real API: a request the API or the SDK would refuse raises the
SDK's own error, saying what is wrong. Every call is one line of mock_calls.jsonl.

Follows the anthropic Python SDK 2.3.0 and the Messages API reference (platform.claude.com, 2026-11-01).
"""
from __future__ import annotations

import json
import os
import re

import _testbench_mock as core
from _testbench_mock import NOT_GIVEN, NotGiven, Omit, not_given, omit

from . import types
from .types import (InputJSONDelta, Message, MessageDeltaUsage, MessageTokensCount, RawContentBlockDeltaEvent,
                    RawContentBlockStartEvent, RawContentBlockStopEvent, RawMessageDeltaEvent, RawMessageStartEvent,
                    RawMessageStopEvent, SignatureDelta, TextBlock, TextDelta, ThinkingBlock, ThinkingDelta,
                    ToolUseBlock, Usage)

__version__ = "2.3.0+testbench.mock"
TESTBENCH_MOCK = True
__all__ = ["types", "Anthropic", "AsyncAnthropic", "Client", "AsyncClient", "Stream", "AsyncStream", "AnthropicError",
           "APIError", "APIStatusError", "APIConnectionError", "APITimeoutError", "BadRequestError",
           "AuthenticationError", "PermissionDeniedError", "NotFoundError", "ConflictError", "RequestTooLargeError",
           "UnprocessableEntityError", "RateLimitError", "InternalServerError", "ServiceUnavailableError",
           "OverloadedError", "DeadlineExceededError", "NOT_GIVEN", "NotGiven", "not_given", "Omit", "omit",
           "DEFAULT_MAX_RETRIES"]
DEFAULT_MAX_RETRIES = 2
_MOCK = core.Mock("anthropic")

# the SDK's models (anthropic.types.Model); any other name is accepted and noted
KNOWN_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-opus-5-5", "claude-mythos-5-1",
                "claude-sonnet-5", "claude-fable-5", "claude-mythos-5", "claude-opus-5", "claude-opus-4-8",
                "claude-opus-4-7", "claude-mythos-preview", "claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5",
                "claude-haiku-4-5-20251001", "claude-opus-4-5", "claude-opus-4-5-20251101", "claude-sonnet-4-5",
                "claude-sonnet-4-5-20250929")
_REQUEST = ("extra_headers", "extra_query", "extra_body", "timeout")
_LEGACY = ("temperature", "top_p", "top_k")           # anthropic SDK 1.x and API version 2023-06-01 only
_CREATE = ("model", "max_tokens", "messages", "cache_control", "container", "diagnostics", "inference_geo", "metadata",
           "output_config", "service_tier", "speed", "stop_sequences", "stream", "system", "thinking", "tool_choice",
           "tools", "user_profile_id", "workspace_id") + _LEGACY + _REQUEST
_STREAM = tuple(p for p in _CREATE if p != "stream") + ("output_format",)
_COUNT = ("model", "messages", "cache_control", "output_config", "output_format", "speed", "system", "thinking",
          "tool_choice", "tools", "user_profile_id", "workspace_id") + _REQUEST
_BETA = ("betas", "compaction", "context_management", "fallback_credit_token", "fallbacks", "mcp_servers", "thread")
_HINTS = {"max_completion_tokens": " (that is OpenAI's; the Messages API takes max_tokens)",
          "stop": " (that is OpenAI's; the Messages API takes stop_sequences)",
          "response_format": " (that is OpenAI's; the Messages API takes output_config={'format': {'type': "
                             "'json_schema', 'schema': ...}})",
          "user": " (that is OpenAI's; the Messages API takes metadata={'user_id': ...})",
          "betas": " (betas go to client.beta.messages)", "fallbacks": " (fallbacks go to client.beta.messages)"}
_ROLES = ("user", "assistant", "system")
_BLOCKS = ("text", "image", "document", "search_result", "thinking", "redacted_thinking", "tool_use", "tool_result",
           "server_tool_use", "web_search_tool_result", "web_fetch_tool_result", "code_execution_tool_result",
           "bash_code_execution_tool_result", "text_editor_code_execution_tool_result", "tool_search_tool_result",
           "container_upload")
_FROM_ASSISTANT = {"tool_use", "thinking", "redacted_thinking", "server_tool_use"}
_BLOCK_KEYS = {"text": ("type", "text", "cache_control", "citations"), "image": ("type", "source", "cache_control"),
               "tool_use": ("type", "id", "name", "input", "cache_control", "caller"),
               "tool_result": ("type", "tool_use_id", "content", "is_error", "cache_control"),
               "thinking": ("type", "thinking", "signature"), "redacted_thinking": ("type", "data")}
_TOOL_KEYS = ("name", "description", "input_schema", "cache_control", "strict", "type", "defer_loading",
              "allowed_callers", "eager_input_streaming", "input_examples")
_MEDIA = ("image/jpeg", "image/png", "image/gif", "image/webp")


# ---- the SDK's errors -----------------------------------------------------------------------------------------

class AnthropicError(Exception):
    pass


class APIError(AnthropicError):
    def __init__(self, message, request, *, body):
        super().__init__(message)
        self.message, self.request, self.body = message, request, body


class APIStatusError(APIError):
    def __init__(self, message, *, response, body):
        super().__init__(message, response.request, body=body)
        self.response, self.status_code = response, response.status_code
        self.request_id = response.headers.get("request-id")
        self.workspace_id = response.headers.get("anthropic-workspace-id")
        error = body.get("error") if isinstance(body, dict) else None
        self.type = error.get("type") if isinstance(error, dict) else None


class APIConnectionError(APIError):
    def __init__(self, *, message="Connection error.", request):
        super().__init__(message, request, body=None)


class APITimeoutError(APIConnectionError):
    def __init__(self, request):
        super().__init__(message="Request timed out or interrupted.", request=request)


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


class RequestTooLargeError(APIStatusError):
    status_code = 413


class UnprocessableEntityError(APIStatusError):
    status_code = 422


class RateLimitError(APIStatusError):
    status_code = 429


class InternalServerError(APIStatusError):
    pass


class ServiceUnavailableError(APIStatusError):
    status_code = 503


class DeadlineExceededError(APIStatusError):
    status_code = 504


class OverloadedError(APIStatusError):
    status_code = 529


# a wrong keyword argument: the real SDK raises TypeError before sending anything; the mock's error is both
_ArgumentError = type("BadRequestError", (BadRequestError, TypeError), {})


_STATUS = {"bad_request": (400, BadRequestError, "invalid_request_error"),
           "argument": (400, _ArgumentError, "invalid_request_error"),
           "auth": (401, AuthenticationError, "authentication_error"),
           "permission": (403, PermissionDeniedError, "permission_error"),
           "not_found": (404, NotFoundError, "not_found_error"), "rate_limit": (429, RateLimitError, "rate_limit_error"),
           "server": (500, InternalServerError, "api_error"), "overloaded": (529, OverloadedError, "overloaded_error")}


def _error(kind, message, param, n):
    request = core.FakeRequest("https://api.anthropic.com/v1/messages")
    if kind == "value":
        return ValueError(message)
    if kind == "timeout":
        return APITimeoutError(request)
    if kind == "connection":
        return APIConnectionError(message=f"Connection error. ({message})", request=request)
    status, cls, etype = _STATUS[kind]
    body = {"type": "error", "error": {"type": etype, "message": message}, "request_id": f"req_mock_{n}"}
    headers = {"request-id": f"req_mock_{n}", **({"retry-after": "0"} if status == 429 else {})}
    response = core.FakeResponse(status, body, request, headers)
    return cls(f"Error code: {status} - {body}", response=response, body=body)


# ---- checking a request ---------------------------------------------------------------------------------------

def _blocks(m: dict) -> list:
    return m["content"] if isinstance(m.get("content"), list) else []


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def _check_block(at, b, role):
    b = core.plain(b, drop_none=True)
    if isinstance(b, str):
        core.refuse(f"{at} is a bare string; a content block is {{'type': 'text', 'text': ...}}", at)
    if not isinstance(b, dict) or "type" not in b:
        core.refuse(f"{at} needs a type: one of {', '.join(_BLOCKS)}", at)
    t = b["type"]
    if t not in _BLOCKS:
        hint = {"image_url": " (that is OpenAI's shape; here an image is {'type': 'image', 'source': {'type': 'url', "
                             "'url': ...}} or {'type': 'image', 'source': {'type': 'base64', 'media_type': "
                             "'image/png', 'data': ...}})",
                "function": " (a tool call is a tool_use block)"}.get(t, "")
        core.refuse(f"{at}.type is {t!r}, which is not a content block type{hint}; types are {', '.join(_BLOCKS)}",
                    f"{at}.type")
    if t == "tool_result" and role != "user":
        core.refuse(f"{at}: tool_result blocks go in user messages (the reply to the assistant's tool_use)", at)
    if t in _FROM_ASSISTANT and role != "assistant":
        core.refuse(f"{at}: {t} blocks only come from the assistant", at)
    if t in _BLOCK_KEYS:
        core.keys(at, b, _BLOCK_KEYS[t])
    if t == "text":
        core.need(isinstance(b.get("text"), str), f"{at}.text", "a string", b.get("text"))
        if not b["text"].strip():
            core.refuse(f"{at}: text content blocks must contain non-whitespace text", f"{at}.text")
    elif t == "image":
        src = b.get("source")
        core.need(isinstance(src, dict) and src.get("type") in ("base64", "url", "file"), f"{at}.source",
                  "{'type': 'base64', 'media_type': ..., 'data': ...}, {'type': 'url', 'url': ...} or "
                  "{'type': 'file', 'file_id': ...}", src)
        if src["type"] == "base64":
            core.choice(f"{at}.source.media_type", src.get("media_type"), _MEDIA)
            core.need(isinstance(src.get("data"), str), f"{at}.source.data", "base64 text", src.get("data"))
        else:
            field = "url" if src["type"] == "url" else "file_id"
            core.need(isinstance(src.get(field), str), f"{at}.source.{field}", "a string", src.get(field))
    elif t == "tool_use":
        for field, ok, what in (("id", str, "a string"), ("name", str, "a string"), ("input", dict, "a mapping")):
            core.need(isinstance(b.get(field), ok), f"{at}.{field}", what, b.get(field))
    elif t == "tool_result":
        core.need(isinstance(b.get("tool_use_id"), str), f"{at}.tool_use_id", "the id of the tool_use it answers",
                  b.get("tool_use_id"))
        content = b.get("content", "")
        if not isinstance(content, str):
            for k, inner in enumerate(core.as_list(f"{at}.content", content, empty_ok=True)):
                inner = core.plain(inner, drop_none=True)
                if not isinstance(inner, dict) or inner.get("type") not in ("text", "image", "document",
                                                                            "search_result"):
                    core.refuse(f"{at}.content[{k}]: a tool result holds text, image, document or search_result "
                                "blocks, or is a string", f"{at}.content[{k}]")
        if "is_error" in b:
            core.need(isinstance(b["is_error"], bool), f"{at}.is_error", "true or false", b["is_error"])
    return b


def _check_messages(msgs) -> list:
    out = []
    msgs = core.as_list("messages", msgs)
    for i, m in enumerate(msgs):
        at = f"messages[{i}]"
        m = core.plain(m, drop_none=True)
        if not isinstance(m, dict):
            core.refuse(f"{at} must be a mapping {{'role': ..., 'content': ...}}, got {core.kind(m)}", at)
        if "role" not in m:
            core.refuse(f"{at} has no role; a message is {{'role': 'user' or 'assistant', 'content': ...}}",
                        f"{at}.role")
        role = m["role"]
        if role not in _ROLES:
            hint = {"tool": "; a tool result goes in a user message: {'role': 'user', 'content': [{'type': "
                            "'tool_result', 'tool_use_id': ..., 'content': ...}]}",
                    "function": "; a tool result goes in a user message as a tool_result block",
                    "developer": "; instructions go in the top-level system= parameter"}.get(role, "")
            core.refuse(f"{at}.role is {role!r}; the Messages API takes user, assistant or system{hint}", f"{at}.role")
        if "content" not in m:
            core.refuse(f"{at} has no content", f"{at}.content")
        core.keys(at, m, ("role", "content", "clear_at", "output_config") if role == "system" else ("role", "content"),
                  {"tool_calls": " (tool_calls is OpenAI's shape: here a tool call is a tool_use block in the "
                                 "assistant's content)", "tool_call_id": " (that is OpenAI's: here a tool result is a "
                                                                         "tool_result block in a user message)",
                   "name": " (that is OpenAI's)"})
        content, last = m["content"], i == len(msgs) - 1
        may_be_empty = (role == "assistant" and last) or (role == "system" and "output_config" in m)
        if isinstance(content, str):
            if not content.strip() and not may_be_empty:
                core.refuse(f"{at}.content is empty; every message needs content, except a final assistant message",
                            f"{at}.content")
        elif isinstance(content, (dict, bytes)) or not hasattr(content, "__iter__"):
            core.refuse(f"{at}.content must be a string or a list of content blocks, got {core.kind(content)}",
                        f"{at}.content")
        else:
            content = [_check_block(f"{at}.content[{j}]", b, role) for j, b in enumerate(content)]
            if not content and not may_be_empty:
                core.refuse(f"{at}.content is an empty list; every message needs content, except a final assistant "
                            "message", f"{at}.content")
            if role == "system" and any(b["type"] != "text" for b in content):
                core.refuse(f"{at}: a system message holds text blocks only", f"{at}.content")
        out.append({**m, "content": content})
    if out[0]["role"] == "system":
        core.refuse("messages[0] has role 'system': put the system prompt in the top-level system= parameter (a system "
                    "message inside messages must come after a user message)", "messages[0].role")
    if out[0]["role"] == "assistant":
        core.refuse("messages[0] is from the assistant; the first message must be from the user", "messages[0].role")
    for i, m in enumerate(out):
        if m["role"] == "system" and not (m["content"] == [] and "output_config" in m):
            prev = out[i - 1]
            after_server_tool = prev["role"] == "assistant" and [b.get("type") for b in _blocks(prev)][-1:] == [
                "server_tool_use"]
            if prev["role"] != "user" and not after_server_tool:
                core.refuse(f"messages[{i}]: a system message must follow a user message, not {out[i - 1]['role']}",
                            f"messages[{i}]")
            if i + 1 < len(out) and out[i + 1]["role"] != "assistant":
                core.refuse(f"messages[{i}]: a system message must be the last message or be followed by an assistant "
                            "message", f"messages[{i}]")
        uses = [b.get("id") for b in _blocks(m) if b.get("type") == "tool_use"]
        if m["role"] == "assistant" and uses and i + 1 < len(out):
            nxt = out[i + 1]
            answered = {b.get("tool_use_id") for b in _blocks(nxt) if b.get("type") == "tool_result"} \
                if nxt["role"] == "user" else set()
            missing = [u for u in uses if u not in answered]
            if missing:
                core.refuse(f"messages[{i + 1}]: tool_use ids were found without tool_result blocks immediately after: "
                            f"{', '.join(missing)}. Each tool_use block of messages[{i}] needs a tool_result block "
                            "with its id in the next message, a user message", f"messages[{i + 1}]")
        if m["role"] == "user":
            asked = {b.get("id") for b in _blocks(out[i - 1]) if b.get("type") == "tool_use"} \
                if i and out[i - 1]["role"] == "assistant" else set()
            for j, b in enumerate(_blocks(m)):
                if b.get("type") == "tool_result" and b.get("tool_use_id") not in asked:
                    core.refuse(f"messages[{i}].content[{j}]: unexpected tool_use_id {b.get('tool_use_id')!r} in a "
                                "tool_result block; each tool_result must answer a tool_use block of the previous "
                                f"message{f' (it has {sorted(asked)})' if asked else ', which has none'}",
                                f"messages[{i}].content[{j}].tool_use_id")
    return out


def _check_tools(tools) -> list:
    out, names = [], set()
    for i, t in enumerate(core.as_list("tools", tools, empty_ok=True)):
        at = f"tools[{i}]"
        t = core.plain(t, drop_none=True)
        if not isinstance(t, dict):
            core.refuse(f"{at} must be a mapping {{'name', 'description', 'input_schema'}}, got {core.kind(t)}", at)
        if t.get("type") == "function" or "function" in t:
            core.refuse(f"{at} is in OpenAI's shape ({{'type': 'function', 'function': {{...}}}}); a tool here is "
                        "{'name': ..., 'description': ..., 'input_schema': {'type': 'object', 'properties': ...}}", at)
        if t.get("type") not in (None, "custom"):         # a server or Anthropic-defined tool: web_search_..., bash_...
            core.need(isinstance(t["type"], str), f"{at}.type", "a tool type such as web_search_20260209", t["type"])
            out.append(t)
            continue
        if "parameters" in t and "input_schema" not in t:
            core.refuse(f"{at} has parameters, OpenAI's name for it; the Messages API calls it input_schema",
                        f"{at}.parameters")
        core.keys(at, t, _TOOL_KEYS)
        name = t.get("name")
        if not (isinstance(name, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", name)):
            core.refuse(f"{at}.name must be 1 to 128 letters, digits, _ or -, got {name!r}", f"{at}.name")
        schema = t.get("input_schema")
        if not (isinstance(schema, dict) and schema.get("type") == "object"):
            core.refuse(f"{at}.input_schema must be a JSON schema with 'type': 'object', e.g. {{'type': 'object', "
                        "'properties': {'city': {'type': 'string'}}, 'required': ['city']}", f"{at}.input_schema")
        if name in names:
            core.refuse(f"tools: two tools are named {name!r}; tool names must be unique", f"{at}.name")
        names.add(name)
        out.append(t)
    return out


def _check(endpoint, kw, beta, timeout_set=False):
    allowed = {"messages.create": _CREATE, "messages.stream": _STREAM, "messages.count_tokens": _COUNT}[endpoint]
    allowed = allowed + (_BETA if beta else ())
    name = ("beta." if beta else "") + endpoint
    core.arguments(name, kw, allowed, ("model", "messages") if endpoint.endswith("count_tokens")
                   else ("max_tokens", "messages", "model"), _HINTS)
    extra = kw.get("extra_body") or {}
    core.need(isinstance(extra, dict), "extra_body", "a mapping", extra)
    for k in extra:
        if k not in allowed:
            core.refuse(f"{k}: Extra inputs are not permitted (it came in extra_body)", k)
    req = {**{k: v for k, v in kw.items() if k not in _REQUEST}, **extra}
    notes = []
    model = req["model"]
    core.need(isinstance(model, str) and model.strip(), "model", "a model name such as claude-opus-5-5", model)
    if model not in KNOWN_MODELS:
        notes.append(f"model {model!r} is not one this mock knows (a model that does not exist is a 404 "
                     "not_found_error from the real API)")
    if "max_tokens" in req:
        core.number("max_tokens", req["max_tokens"], 0, integer=True)
    req["messages"] = _check_messages(req["messages"])
    if "system" in req:
        system = core.plain(req["system"], drop_none=True)
        if not isinstance(system, str):
            for j, b in enumerate(core.as_list("system", system, empty_ok=True)):
                if not (isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)):
                    core.refuse(f"system[{j}] must be a text block {{'type': 'text', 'text': ...}}; system is a string "
                                "or a list of those", f"system[{j}]")
                core.keys(f"system[{j}]", b, _BLOCK_KEYS["text"])
        req["system"] = system
    req["tools"] = _check_tools(req.get("tools", []))
    names = [t["name"] for t in req["tools"] if t.get("type") in (None, "custom")]
    if "tool_choice" in req:
        tc = core.plain(req["tool_choice"], drop_none=True)
        if isinstance(tc, str):
            core.refuse(f"tool_choice must be a mapping such as {{'type': 'auto'}}, {{'type': 'any'}}, {{'type': 'tool', "
                        f"'name': ...}} or {{'type': 'none'}}; the string {tc!r} is OpenAI's shape", "tool_choice")
        core.need(isinstance(tc, dict), "tool_choice", "a mapping such as {'type': 'auto'}", tc)
        core.choice("tool_choice.type", tc.get("type"), ("auto", "any", "tool", "none"))
        core.keys("tool_choice", tc, ("type", "disable_parallel_tool_use") + (("name",) if tc["type"] == "tool" else ()))
        if tc["type"] == "tool" and tc.get("name") not in names:
            core.refuse(f"tool_choice.name {tc.get('name')!r} is not one of the request's tools "
                        f"({', '.join(names) or 'it has none'})", "tool_choice.name")
        if tc["type"] != "none" and not req["tools"]:
            core.refuse("tool_choice is set, but the request has no tools: pass tools= or drop tool_choice", "tool_choice")
        req["tool_choice"] = tc
    if "thinking" in req:
        th = core.plain(req["thinking"], drop_none=True)
        core.need(isinstance(th, dict), "thinking", "a mapping such as {'type': 'adaptive'}", th)
        core.choice("thinking.type", th.get("type"), ("adaptive", "enabled", "disabled", "between_tools"))
        core.keys("thinking", th, ("type", "budget_tokens", "display", "block_binding"))
        if th["type"] == "enabled":
            budget = th.get("budget_tokens")
            if not (core.is_int(budget) and 1024 <= budget < req.get("max_tokens", budget + 1)):
                core.refuse("thinking.budget_tokens must be at least 1024 and less than max_tokens "
                            f"({req.get('max_tokens')}), got {budget!r}", "thinking.budget_tokens")
        elif "budget_tokens" in th:
            core.refuse(f"thinking.budget_tokens goes with type 'enabled', not {th['type']!r}", "thinking.budget_tokens")
        if "display" in th:
            core.choice("thinking.display", th["display"], ("summarized", "omitted", "updates"))
        req["thinking"] = th
    if "metadata" in req:
        core.need(isinstance(req["metadata"], dict), "metadata", "a mapping {'user_id': ...}", req["metadata"])
        core.keys("metadata", req["metadata"], ("user_id",))
    if "stop_sequences" in req:
        stops = core.as_list("stop_sequences", req["stop_sequences"], empty_ok=True)
        for j, s in enumerate(stops):
            core.need(isinstance(s, str) and s.strip(), f"stop_sequences[{j}]", "a string with non-whitespace text", s)
        req["stop_sequences"] = stops
    if "output_config" in req:
        oc = core.plain(req["output_config"], drop_none=True)
        core.need(isinstance(oc, dict), "output_config", "a mapping such as {'effort': 'high'}", oc)
        core.keys("output_config", oc, ("effort", "format", "task_budget"))
        if "effort" in oc:
            core.choice("output_config.effort", oc["effort"], ("low", "medium", "high", "xhigh", "max"))
        if "format" in oc:
            f = oc["format"]
            core.need(isinstance(f, dict) and f.get("type") == "json_schema" and isinstance(f.get("schema"), dict),
                      "output_config.format", "{'type': 'json_schema', 'schema': {...}}", f)
    if "output_format" in req and isinstance(req["output_format"], type):
        core.refuse("output_format here is a JSON schema mapping; a pydantic model class goes to messages.parse()",
                    "output_format")
    for key, lo, hi in (("temperature", 0, 1), ("top_p", 0, 1), ("top_k", 0, None)):
        if key in req:
            core.number(key, req[key], lo, hi, integer=key == "top_k")
            notes.append(f"{key} is refused (400) by API version 2026-10-21 on every model, and is not a parameter "
                         "of the anthropic SDK 2.x")
    if "service_tier" in req:
        core.choice("service_tier", req["service_tier"], ("auto", "standard_only"))
    if req.get("speed") is not None:
        core.choice("speed", req["speed"], ("standard", "fast"))
    if "stream" in req:
        core.need(isinstance(req["stream"], bool), "stream", "true or false", req["stream"])
    if "cache_control" in req and req["cache_control"] is not None:
        cc = req["cache_control"]
        core.need(isinstance(cc, dict) and cc.get("type") == "ephemeral", "cache_control", "{'type': 'ephemeral'}", cc)
    if endpoint == "messages.create" and not req.get("stream") and "timeout" not in kw and not timeout_set \
            and 3600 * req["max_tokens"] / 128000 > 600:
        raise core.Refusal(f"Streaming is required for operations that may take longer than 10 minutes "
                           f"(max_tokens={req['max_tokens']} without stream=True; the real SDK refuses it before "
                           "sending). Use messages.stream(...) or stream=True, or pass timeout=.", "max_tokens", "value")
    req.update(known_model=model in KNOWN_MODELS, notes=notes)
    return req


# ---- answering ------------------------------------------------------------------------------------------------

def _last_user_text(messages) -> str:
    """What the replies are matched against: the last user message's text, or its tool results when it has no text."""
    for m in reversed(messages):
        if m["role"] != "user":
            continue
        if isinstance(m["content"], str):
            return m["content"]
        text = _text_of(m["content"])
        return text or "\n".join(_text_of(b.get("content")) for b in m["content"] if b.get("type") == "tool_result")
    return ""


def _input_tokens(req) -> int:
    parts = [_text_of(req.get("system") or "")]
    for m in req["messages"]:
        parts.append(_text_of(m["content"]))
        for b in _blocks(m):
            if b.get("type") == "tool_result":
                parts.append(_text_of(b.get("content")))
            elif b.get("type") == "tool_use":
                parts.append(json.dumps(b.get("input")))
    parts.append(json.dumps(req.get("tools") or []) if req.get("tools") else "")
    return max(1, core.tokens(" ".join(parts)))


def _message(req, n) -> tuple:
    tc = req.get("tool_choice") or {}
    tools = {} if tc.get("type") == "none" else {t["name"]: t.get("input_schema") for t in req["tools"]
                                                    if t.get("type") in (None, "custom")}
    forced = tc.get("name") if tc.get("type") == "tool" else "any" if tc.get("type") == "any" else None
    fmt = (req.get("output_config") or {}).get("format") or req.get("output_format") or {}
    r = _MOCK.reply(_last_user_text(req["messages"]), tools=tools, forced=forced, structured=fmt.get("schema"))
    calls = r["tool_calls"][:1] if tc.get("disable_parallel_tool_use") else r["tool_calls"]
    key = core.digest("anthropic", n, req["model"], r)
    content, stop, stop_seq, text = [], "end_turn", None, r["text"]
    thinking = req.get("thinking") or {}
    if thinking.get("type") in ("enabled", "adaptive"):
        content.append(ThinkingBlock(type="thinking", signature=f"mock-signature-{key}",
                                     thinking="mock thinking" if thinking.get("display") == "summarized" else ""))
    if text is not None:
        text, stop, stop_seq = core.cut(text, req["max_tokens"], req.get("stop_sequences"))
        if text:
            content.append(TextBlock(type="text", text=text, citations=None))
    if calls and stop == "end_turn":
        content += [ToolUseBlock(type="tool_use", id=f"toolu_mock{n:03d}_{i}", name=c["name"], input=c["input"])
                    for i, c in enumerate(calls)]
        stop = "tool_use"
    else:
        calls = []
    usage = {"input_tokens": _input_tokens(req), "output_tokens": max(1, core.tokens(text) + sum(
        core.tokens(json.dumps(c["input"])) for c in calls))}
    msg = Message(id=f"msg_mock_{key}", type="message", role="assistant", model=req["model"], content=content,
                  stop_reason=stop, stop_sequence=stop_seq, stop_details=None, container=None,
                  usage=Usage(**usage, cache_creation_input_tokens=0, cache_read_input_tokens=0, cache_creation=None,
                              server_tool_use=None, service_tier="standard", inference_geo=None))
    msg._request_id = f"req_mock_{n}"
    info = {"reply": {"text": text, "tool_calls": calls}, "rule": r["rule"], "stop_reason": stop, "usage": usage}
    return msg, info


def _events(msg) -> list:
    """The raw stream of a message: message_start, each block's start / deltas / stop, message_delta, message_stop."""
    usage = msg.usage
    start = Message(**{**vars(msg), "content": [], "stop_reason": None, "stop_sequence": None,
                       "usage": Usage(**{**vars(usage), "output_tokens": 1})})
    out = [RawMessageStartEvent(type="message_start", message=start)]
    for i, b in enumerate(msg.content):
        if b.type == "text":
            first, deltas = TextBlock(type="text", text="", citations=None), [
                TextDelta(type="text_delta", text=p) for p in core.pieces(b.text)]
        elif b.type == "tool_use":
            js = json.dumps(b.input)
            first = ToolUseBlock(type="tool_use", id=b.id, name=b.name, input={})
            deltas = [InputJSONDelta(type="input_json_delta", partial_json=p) for p in (js[:len(js) // 2],
                                                                                        js[len(js) // 2:])]
        else:
            first = ThinkingBlock(type="thinking", thinking="", signature="")
            deltas = [ThinkingDelta(type="thinking_delta", thinking=p) for p in core.pieces(b.thinking)] + [
                SignatureDelta(type="signature_delta", signature=b.signature)]
        out.append(RawContentBlockStartEvent(type="content_block_start", index=i, content_block=first))
        out += [RawContentBlockDeltaEvent(type="content_block_delta", index=i, delta=d) for d in deltas]
        out.append(RawContentBlockStopEvent(type="content_block_stop", index=i))
    out.append(RawMessageDeltaEvent(type="message_delta", delta=core.Obj(
        stop_reason=msg.stop_reason, stop_sequence=msg.stop_sequence, stop_details=None, container=None),
        usage=MessageDeltaUsage(output_tokens=usage.output_tokens, input_tokens=usage.input_tokens,
                                cache_creation_input_tokens=None, cache_read_input_tokens=None, server_tool_use=None)))
    out.append(RawMessageStopEvent(type="message_stop"))
    return out


class _TextEvent(core.Obj):
    pass


class _InputJsonEvent(core.Obj):
    pass


class _ThinkingEvent(core.Obj):
    pass


class _SignatureEvent(core.Obj):
    pass


def _helper_events(msg) -> list:
    """What messages.stream() yields: the raw events, plus text / input_json / thinking / signature events after
    each delta, and content_block_stop and message_stop carrying the finished block and message."""
    out, snap = [], {}
    for ev in _events(msg):
        if ev.type == "content_block_stop":
            out.append(RawContentBlockStopEvent(type="content_block_stop", index=ev.index,
                                                content_block=msg.content[ev.index]))
            continue
        if ev.type == "message_stop":
            out.append(RawMessageStopEvent(type="message_stop", message=msg))
            continue
        out.append(ev)
        if ev.type == "content_block_delta":
            d = ev.delta
            snap[ev.index] = snap.get(ev.index, "") + getattr(d, "text", getattr(d, "partial_json", getattr(
                d, "thinking", "")))
            if d.type == "text_delta":
                out.append(_TextEvent(type="text", text=d.text, snapshot=snap[ev.index]))
            elif d.type == "input_json_delta":
                done = snap[ev.index] == json.dumps(msg.content[ev.index].input)
                out.append(_InputJsonEvent(type="input_json", partial_json=d.partial_json,
                                           snapshot=msg.content[ev.index].input if done else {}))
            elif d.type == "thinking_delta":
                out.append(_ThinkingEvent(type="thinking", thinking=d.thinking, snapshot=snap[ev.index]))
            else:
                out.append(_SignatureEvent(type="signature", signature=d.signature))
    return out


class MessageStream:
    """messages.stream(...) inside its `with`: iterate the events, or text_stream, or get_final_message()."""

    def __init__(self, msg):
        self._msg, self._it = msg, iter(_helper_events(msg))
        self.text_stream = self._texts()
        self.response = None
        self.request_id = msg._request_id

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def _texts(self):
        for ev in self:
            if ev.type == "content_block_delta" and ev.delta.type == "text_delta":
                yield ev.delta.text

    def until_done(self):
        for _ in self:
            pass

    def get_final_message(self):
        self.until_done()
        return self._msg

    def get_final_text(self):
        return "".join(b.text for b in self.get_final_message().content if b.type == "text")

    @property
    def current_message_snapshot(self):
        return self._msg

    def close(self):
        self._it = iter(())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class AsyncMessageStream:
    def __init__(self, msg):
        self._sync = MessageStream(msg)
        self.text_stream = self._texts()
        self.response, self.request_id = None, msg._request_id

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._sync)
        except StopIteration:
            raise StopAsyncIteration from None

    async def _texts(self):
        async for ev in self:
            if ev.type == "content_block_delta" and ev.delta.type == "text_delta":
                yield ev.delta.text

    async def until_done(self):
        self._sync.until_done()

    async def get_final_message(self):
        return self._sync.get_final_message()

    async def get_final_text(self):
        return self._sync.get_final_text()

    @property
    def current_message_snapshot(self):
        return self._sync._msg

    async def close(self):
        self._sync.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()


class MessageStreamManager:
    """The request is made when the `with` starts, as with the real SDK."""

    def __init__(self, make, stream_class=MessageStream):
        self._make, self._class, self._stream = make, stream_class, None

    def __enter__(self):
        self._stream = self._class(self._make())
        return self._stream

    def __exit__(self, *exc):
        self._stream.close()

    async def __aenter__(self):
        self._stream = self._class(self._make())
        return self._stream

    async def __aexit__(self, *exc):
        await self._stream.close()


Stream, AsyncStream = core.Stream, core.AsyncStream


# ---- the client -----------------------------------------------------------------------------------------------

class Messages:
    _beta = False

    def __init__(self, client):
        self._client = client

    def _call(self, endpoint, kwargs, answer):
        name = ("beta." if self._beta else "") + endpoint
        return core.call(_MOCK, name, core.given(kwargs), retries=self._client.max_retries,
                         check=lambda kw: _check(endpoint, kw, self._beta, "timeout" in self._client._options),
                         answer=answer, error=_error)

    def _create(self, kwargs):
        msg = self._call("messages.create", kwargs, _message)
        if not kwargs.get("stream"):
            return msg
        return (AsyncStream if self._client._async else Stream)(_events(msg))

    def _count(self, kwargs):
        def answer(req, n):
            count = _input_tokens(req)
            return MessageTokensCount(input_tokens=count), {"reply": None, "usage": {"input_tokens": count,
                                                                                     "output_tokens": 0}}
        return self._call("messages.count_tokens", kwargs, answer)

    def create(self, **kwargs):
        return self._create(kwargs)

    def stream(self, **kwargs):
        return MessageStreamManager(lambda: self._call("messages.stream", kwargs, _message),
                                    AsyncMessageStream if self._client._async else MessageStream)

    def count_tokens(self, **kwargs):
        return self._count(kwargs)

    def __getattr__(self, name):
        raise AttributeError(f"the mock anthropic client has no messages.{name}; it answers messages.create, "
                             "messages.stream and messages.count_tokens (docs/mocks.md)")


class AsyncMessages(Messages):
    async def create(self, **kwargs):
        return self._create(kwargs)

    async def count_tokens(self, **kwargs):
        return self._count(kwargs)


class _BetaMessages(Messages):
    _beta = True


class _AsyncBetaMessages(AsyncMessages):
    _beta = True


class _Beta:
    def __init__(self, client):
        self.messages = (_AsyncBetaMessages if client._async else _BetaMessages)(client)

    def __getattr__(self, name):
        raise AttributeError(f"the mock anthropic client has no beta.{name}; it answers beta.messages.create, "
                             ".stream and .count_tokens (docs/mocks.md)")


_CLIENT_OPTIONS = ("api_key", "auth_token", "credentials", "config", "profile", "webhook_key", "base_url", "timeout",
                   "max_retries", "default_headers", "default_query", "open_telemetry", "http_client", "middleware",
                   "_strict_response_validation", "_token_cache")


class _Client:
    _async = False

    def __init__(self, **options):
        options = core.given(options)
        for name in options:
            if name not in _CLIENT_OPTIONS:
                raise TypeError(f"{type(self).__name__}.__init__() got an unexpected keyword argument {name!r}")
        self._options = options
        self.api_key = options.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")     # any key, or none, works
        self.auth_token = options.get("auth_token") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        self.base_url = str(options.get("base_url") or os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com")
        self.max_retries = options.get("max_retries", DEFAULT_MAX_RETRIES)
        self.timeout = options.get("timeout", 600.0)
        self.messages = (AsyncMessages if self._async else Messages)(self)
        self.beta = _Beta(self)

    def with_options(self, **options):
        return type(self)(**{**self._options, **options})

    copy = with_options

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        raise AttributeError(f"the mock anthropic client has no {name!r}; it answers messages.create, messages.stream "
                             "and messages.count_tokens (and the same under beta.messages); see docs/mocks.md")


class Anthropic(_Client):
    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class AsyncAnthropic(_Client):
    _async = True

    async def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()


Client, AsyncClient = Anthropic, AsyncAnthropic
