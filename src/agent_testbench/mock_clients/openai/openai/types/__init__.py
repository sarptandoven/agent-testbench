"""Response types of the mock `openai` package (agent-testbench; docs/mocks.md). Request types (*Param) are plain
dicts, as the real ones are TypedDicts."""
from _testbench_mock import Obj as _Obj

from . import chat

__all__ = ["chat", "CompletionUsage", "Embedding", "CreateEmbeddingResponse", "EmbeddingModel", "ChatModel",
           "EmbeddingCreateParams"]


class CompletionUsage(_Obj):
    pass


class Embedding(_Obj):
    pass


class CreateEmbeddingResponse(_Obj):
    pass


EmbeddingModel = ChatModel = str
EmbeddingCreateParams = dict


def __getattr__(name):
    if name.endswith("Param") or name.endswith("Params"):
        return dict
    raise AttributeError(f"the mock openai.types has no {name!r} (it has {', '.join(__all__)} and every *Param)")
