"""Response types of the mock `openai` package (agent-testbench; docs/mocks.md). Request types (*Param) are plain
dicts, as the real ones are TypedDicts. The modules app code imports them from (openai.types.completion_usage,
.create_embedding_response, .embedding) are here too. Each model's fields for model_validate follow openai 3.42.0."""
from _testbench_mock import Obj as _Obj

from . import chat
from .completion_usage import CompletionUsage

__all__ = ["chat", "CompletionUsage", "Embedding", "CreateEmbeddingResponse", "EmbeddingModel", "ChatModel",
           "EmbeddingCreateParams"]


class Embedding(_Obj):
    _required = ("embedding", "index", "object")


class _EmbeddingUsage(_Obj):
    _required = ("prompt_tokens", "total_tokens")


_EmbeddingUsage.__name__ = _EmbeddingUsage.__qualname__ = "Usage"


class CreateEmbeddingResponse(_Obj):
    _required = ("data", "model", "object", "usage")
    _nested = {"data": Embedding, "usage": _EmbeddingUsage}


EmbeddingModel = ChatModel = str
EmbeddingCreateParams = dict


def __getattr__(name):
    if name.endswith("Param") or name.endswith("Params"):
        return dict
    raise AttributeError(f"the mock openai.types has no {name!r} (it has {', '.join(__all__)} and every *Param)")
