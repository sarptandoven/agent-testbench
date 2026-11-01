"""Response types of the mock `anthropic` package (agent-testbench; docs/mocks.md). Request types (MessageParam,
ToolParam and every other *Param) are plain dicts, as the real ones are TypedDicts. The modules app code imports
them from (anthropic.types.message, .text_block, .tool_use_block, ...) are here too. Each model's fields for
model_validate follow anthropic 2.3.0."""
from _testbench_mock import Obj as _Obj

__all__ = ["Message", "TextBlock", "ToolUseBlock", "ThinkingBlock", "RedactedThinkingBlock", "Usage",
           "MessageTokensCount", "ContentBlock",
           "RawMessageStreamEvent", "MessageStreamEvent", "RawMessageStartEvent", "RawMessageDeltaEvent",
           "RawMessageStopEvent", "RawContentBlockStartEvent", "RawContentBlockDeltaEvent", "RawContentBlockStopEvent",
           "TextDelta", "InputJSONDelta", "ThinkingDelta", "SignatureDelta", "MessageDeltaUsage", "Model",
           "MessageParam", "TextBlockParam", "ToolParam", "ToolUseBlockParam", "ToolResultBlockParam",
           "ImageBlockParam", "ToolChoiceParam", "MessageCreateParams", "ModelParam"]


class TextBlock(_Obj):
    _required, _optional = ("text", "type"), ("citations",)


class ToolUseBlock(_Obj):
    _required, _optional = ("id", "input", "name", "type"), ("caller", "toolset_name")


class ThinkingBlock(_Obj):
    _required = ("signature", "thinking", "type")


class RedactedThinkingBlock(_Obj):
    _required = ("data", "type")


class Usage(_Obj):
    _required = ("input_tokens", "output_tokens")
    _optional = ("cache_creation", "cache_creation_input_tokens", "cache_read_input_tokens", "inference_geo",
                 "output_tokens_details", "server_tool_use", "service_tier", "speed")


class Message(_Obj):
    _required = ("id", "content", "model", "role", "type", "usage")
    _optional = ("container", "diagnostics", "stop_details", "stop_reason", "stop_sequence")
    _nested = {"content": {"text": TextBlock, "tool_use": ToolUseBlock, "thinking": ThinkingBlock,
                           "redacted_thinking": RedactedThinkingBlock}, "usage": Usage}


class MessageTokensCount(_Obj):
    _required = ("input_tokens",)


class RawMessageStartEvent(_Obj):
    pass


class RawMessageDeltaEvent(_Obj):
    pass


class RawMessageStopEvent(_Obj):
    pass


class RawContentBlockStartEvent(_Obj):
    pass


class RawContentBlockDeltaEvent(_Obj):
    pass


class RawContentBlockStopEvent(_Obj):
    pass


class TextDelta(_Obj):
    pass


class InputJSONDelta(_Obj):
    pass


class ThinkingDelta(_Obj):
    pass


class SignatureDelta(_Obj):
    pass


class MessageDeltaUsage(_Obj):
    pass


ContentBlock = RawMessageStreamEvent = MessageStreamEvent = _Obj       # unions in the real SDK, for type hints
Model = ModelParam = str
MessageParam = TextBlockParam = ToolParam = ToolUseBlockParam = ToolResultBlockParam = ImageBlockParam = dict
ToolChoiceParam = MessageCreateParams = dict


def __getattr__(name):
    if name.endswith("Param") or name.endswith("Params"):
        return dict
    raise AttributeError(f"the mock anthropic.types has no {name!r} (it has {', '.join(__all__)} and every *Param)")
