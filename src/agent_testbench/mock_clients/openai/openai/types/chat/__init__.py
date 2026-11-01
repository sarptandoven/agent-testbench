"""Chat completion types of the mock `openai` package (agent-testbench; docs/mocks.md), and the modules app code
imports them from (openai.types.chat.chat_completion, .chat_completion_message, .chat_completion_chunk, ...)."""
from _testbench_mock import Obj as _Obj

from ..completion_usage import CompletionUsage as _CompletionUsage

__all__ = ["ChatCompletion", "ChatCompletionChunk", "ChatCompletionMessage", "ChatCompletionMessageToolCall",
           "ChatCompletionMessageFunctionToolCall", "ChatCompletionMessageParam", "ChatCompletionToolParam",
           "ChatCompletionToolChoiceOptionParam", "ChatCompletionSystemMessageParam",
           "ChatCompletionUserMessageParam", "ChatCompletionAssistantMessageParam", "ChatCompletionToolMessageParam",
           "ChatCompletionContentPartParam", "CompletionCreateParams"]


class Function(_Obj):
    _required = ("arguments", "name")


class ChatCompletionMessageFunctionToolCall(_Obj):
    _required, _nested = ("id", "function", "type"), {"function": Function}


ChatCompletionMessageToolCall = ChatCompletionMessageFunctionToolCall


class ChatCompletionMessage(_Obj):
    _required = ("role",)
    _optional = ("content", "refusal", "annotations", "audio", "function_call", "tool_calls")
    _nested = {"tool_calls": {"function": ChatCompletionMessageFunctionToolCall}}


class Choice(_Obj):
    _required, _optional = ("finish_reason", "index", "message"), ("logprobs",)
    _nested = {"message": ChatCompletionMessage}


class ChatCompletion(_Obj):
    _required = ("id", "choices", "created", "model", "object")
    _optional = ("metadata", "moderation", "service_tier", "system_fingerprint", "usage")
    _nested = {"choices": Choice, "usage": _CompletionUsage}


class ChoiceDeltaToolCallFunction(_Obj):
    _optional = ("arguments", "name")


class ChoiceDeltaToolCall(_Obj):
    _required, _optional = ("index",), ("id", "function", "type")
    _nested = {"function": ChoiceDeltaToolCallFunction}


class ChoiceDelta(_Obj):
    _optional = ("audio", "content", "function_call", "refusal", "role", "tool_calls")
    _nested = {"tool_calls": ChoiceDeltaToolCall}


class ChunkChoice(_Obj):
    _required, _optional = ("delta", "index"), ("finish_reason", "logprobs")
    _nested = {"delta": ChoiceDelta}


ChunkChoice.__name__ = ChunkChoice.__qualname__ = "Choice"      # as openai.types.chat.chat_completion_chunk names it


class ChatCompletionChunk(_Obj):
    _required = ("id", "choices", "created", "model", "object")
    _optional = ("moderation", "obfuscation", "service_tier", "system_fingerprint", "usage")
    _nested = {"choices": ChunkChoice, "usage": _CompletionUsage}


ChatCompletionMessageParam = ChatCompletionToolParam = ChatCompletionSystemMessageParam = dict
ChatCompletionUserMessageParam = ChatCompletionAssistantMessageParam = ChatCompletionToolMessageParam = dict
ChatCompletionContentPartParam = CompletionCreateParams = dict
ChatCompletionToolChoiceOptionParam = object                           # a string or a mapping


def __getattr__(name):
    if name.endswith("Param") or name.endswith("Params"):
        return dict
    raise AttributeError(f"the mock openai.types.chat has no {name!r} (it has {', '.join(__all__)} and every *Param)")
