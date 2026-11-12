"""A plug-in's own test: the same contract `testbench plugin test` checks, in the package's test suite."""
from agent_testbench.plugins import assert_conforms

import testbench_nice


def test_nice_keeps_the_backend_contract():
    assert_conforms("backends", testbench_nice.backend, "nice")
