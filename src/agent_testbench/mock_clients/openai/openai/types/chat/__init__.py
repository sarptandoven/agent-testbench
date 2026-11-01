"""Chat completion types of the mock `openai` package (agent-testbench; docs/mocks.md)."""
from _testbench_mock import Obj as _Obj

__all__ = ["ChatCompletion", "ChatCompletionChunk", "ChatCompletionMessage", "ChatCompletionMessageToolCall",
           "ChatCompletionMessageFunctionToolCall", "ChatCompletionMessageParam", "ChatCompletionToolParam",
           "ChatCompletionToolChoiceOptionParam", "ChatCompletionSystemMessageParam",
           "ChatCompletionUserMessageParam", "ChatCompletionAssistantMessageParam", "ChatCompletionToolMessageParam",
           "ChatCompletionContentPartParam", "CompletionCreateParams"]


class ChatCompletion(_Obj):
    pass


class ChatCompletionChunk(_Obj):
    pass


class ChatCompletionMessage(_Obj):
    pass


class ChatCompletionMessageFunctionToolCall(_Obj):
    pass


ChatCompletionMessageToolCall = ChatCompletionMessageFunctionToolCall


class Choice(_Obj):
    pass


class Function(_Obj):
    pass


class ChunkChoice(_Obj):
    pass


class ChoiceDelta(_Obj):
    pass


class ChoiceDeltaToolCall(_Obj):
    pass


ChatCompletionMessageParam = ChatCompletionToolParam = ChatCompletionSystemMessageParam = dict
ChatCompletionUserMessageParam = ChatCompletionAssistantMessageParam = ChatCompletionToolMessageParam = dict
ChatCompletionContentPartParam = CompletionCreateParams = dict
ChatCompletionToolChoiceOptionParam = object                           # a string or a mapping


def __getattr__(name):
    if name.endswith("Param") or name.endswith("Params"):
        return dict
    raise AttributeError(f"the mock openai.types.chat has no {name!r} (it has {', '.join(__all__)} and every *Param)")
