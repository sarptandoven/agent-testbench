"""Mock `ib_insync` package from agent-testbench: the mock ib_async under its older name (ib_async is the maintained
successor of ib_insync, with the same API). Calls are logged as ib_async's, and the step's options are mocks:
{ib_async: {...}}."""
import sys as _sys

import ib_async as _ib_async
from ib_async import *  # noqa: F401,F403
from ib_async import __all__, util

__version__ = "0.9.86+testbench.mock"
TESTBENCH_MOCK = True
_sys.modules.setdefault("ib_insync.util", util)
for _name in ("ib", "contract", "order", "objects", "ticker"):
    _sys.modules.setdefault(f"ib_insync.{_name}", getattr(_ib_async, _name))


def __getattr__(name):
    return getattr(_ib_async, name)
