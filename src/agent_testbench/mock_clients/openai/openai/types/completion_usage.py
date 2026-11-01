"""openai.types.completion_usage in the mock package (agent-testbench; docs/mocks.md)."""
from _testbench_mock import Obj as _Obj

__all__ = ["CompletionUsage"]


class CompletionUsage(_Obj):
    _required = ("completion_tokens", "prompt_tokens", "total_tokens")
    _optional = ("completion_tokens_details", "prompt_tokens_details")
