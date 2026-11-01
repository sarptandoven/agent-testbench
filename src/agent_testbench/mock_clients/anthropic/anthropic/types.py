"""Response types of the mock `anthropic` package (agent-testbench; docs/mocks.md). Request types (MessageParam,
ToolParam and every other *Param) are plain dicts, as the real ones are TypedDicts."""
from _testbench_mock import Obj as _Obj

__all__ = ["Message", "TextBlock", "ToolUseBlock", "ThinkingBlock", "Usage", "MessageTokensCount", "ContentBlock",
           "RawMessageStreamEvent", "MessageStreamEvent", "RawMessageStartEvent", "RawMessageDeltaEvent",
           "RawMessageStopEvent", "RawContentBlockStartEvent", "RawContentBlockDeltaEvent", "RawContentBlockStopEvent",
           "TextDelta", "InputJSONDelta", "ThinkingDelta", "SignatureDelta", "MessageDeltaUsage", "Model",
           "MessageParam", "TextBlockParam", "ToolParam", "ToolUseBlockParam", "ToolResultBlockParam",
           "ImageBlockParam", "ToolChoiceParam", "MessageCreateParams", "ModelParam"]


class Message(_Obj):
    pass


class TextBlock(_Obj):
    pass


class ToolUseBlock(_Obj):
    pass


class ThinkingBlock(_Obj):
    pass


class Usage(_Obj):
    pass


class MessageTokensCount(_Obj):
    pass


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
