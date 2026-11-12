"""agent-testbench: a test bench your coding agent operates - live sessions and proved runs on this machine, in
Docker, over ssh, on Modal GPUs or Colab VMs, inside a budget the agent cannot raise."""
__version__ = "0.3.0"

_API = ("Testbench", "Run", "Session", "ExecResult", "TestbenchError", "Refused", "NeedsApproval")
__all__ = list(_API)


def __getattr__(name):                       # the Python API loads on first use, so `import agent_testbench` stays fast
    if name in _API:
        from . import api
        return getattr(api, name)
    raise AttributeError(f"module 'agent_testbench' has no attribute {name!r}")
