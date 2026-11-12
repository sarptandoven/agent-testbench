# Plug-ins

A plug-in is a part of testbench that another package adds: a machine to run on, a check `expect:` can make, a
place `export:` sends a run, and so on. Each kind sits behind one small interface around the core
([Architecture](architecture.md)), and built-ins and plug-ins share one table per kind, so a plug-in can do anything
a built-in does. `testbench plugins` lists every kind, its built-ins, what is installed and from which package, and
what is broken and why.

## Kinds

| kind | entry point group | what one adds | interface |
|---|---|---|---|
| backends | `agent_testbench.backends` | a machine plans and sessions run on (`backend:`) | `agent_testbench.backends.Backend` |
| checks | `agent_testbench.checks` | a check a step's `expect:` can make | `agent_testbench.expect.Check` |
| mocks | `agent_testbench.mocks` | a stand-in SDK a step's `mocks:` loads | `agent_testbench.mocks.Mock` |
| sinks | `agent_testbench.sinks` | a place `export:` sends a run | `agent_testbench.export.Sink` |
| notifiers | `agent_testbench.notifiers` | a place `notify:` tells people | `agent_testbench.notify.Notifier` |
| recipes | `agent_testbench.recipes` | a ready plan ladder (`testbench init --recipe`) | `agent_testbench.recipes.Recipe` |
| metrics | `agent_testbench.metrics` | numbers read from a step's files | `agent_testbench.metrics.Reader` |

`agent_testbench.plugins.KINDS` is this table in code. A kind takes plug-ins once its module defines `KIND`
(`plugins.Kind(...)`: its built-ins, how a loaded object becomes one, and its contract); until then `testbench
plugins` lists it as "not yet open to plug-ins", and a plug-in installed for it is listed as waiting.

## How a package adds one

A package declares an entry point in the kind's group, in its `pyproject.toml`. The entry point's name is the name
testbench uses (in testbench.yaml, on the command line); its value is the object, as `module:attr`:

```toml
[project.entry-points."agent_testbench.backends"]
nice = "testbench_nice:backend"
```

Installed next to agent-testbench (`pip install .` in the package's folder, or `pip install -e .` while you work
on it), it is found the next time testbench loads that kind. Nothing is imported until a kind is used, so a
plug-in costs nothing to the commands that do not need it.

Three rules hold for every kind:

- **A built-in cannot be replaced.** A plug-in that takes a built-in's name (`local`, `modal`, ...) is skipped and
  reported; give it a name of its own.
- **A broken plug-in breaks only what uses it.** One that does not load is left out and reported - by `testbench
  plugins`, by `testbench doctor` (a `no` line with why), and by the message a plan that names it gets - and every
  other plan, kind and command goes on working.
- **Every kind keeps its guardrails.** A backend prices its hardware and stops itself on the remote side; a sink
  or notifier never raises into a run or holds it up; a mock never touches the network. Each kind's contract
  checks its part.

## Testing a plug-in: the conformance kit

`testbench plugin test` loads plug-ins and checks each against its kind's contract, one line per plug-in and one
per problem, in plain words:

```bash
testbench plugin test testbench-backend-example        # every plug-in an installed package declares
testbench plugin test testbench_nice:backend --kind backends    # one object, before it is packaged
testbench plugin test --installed                      # every installed plug-in, of every kind
```

```text
ok   backends nice (testbench_nice:backend, testbench-backend-example 0.1.0): keeps its kind's contract
FAIL backends rack (testbench_rack:backend): 2 problems
     - a remote backend stops its own work on the remote side, ... say what does it in hard_stop ...
     - a one-step plan on it without a GPU costs $0 at worst, and it has no price setting of its own: ...
```

Exit codes: 0 every plug-in keeps its contract, 1 one does not, 2 nothing could be tested (no such object or
package, a package that declares no plug-ins, a kind it cannot tell: `--kind` says which). `--json` gives the same
as JSON. A `module:attr` without `--kind` is tested as the kind an installed entry point declares it under, or the
only kind that takes it. The kit also reports an entry point that takes a built-in's name, one whose object calls
itself by another name, and one in a group that is not a kind.

In the plug-in's own tests, the same check (standard library only):

```python
from agent_testbench.plugins import assert_conforms
import testbench_nice

def test_nice_keeps_the_backend_contract():
    assert_conforms("backends", testbench_nice.backend, "nice")    # AssertionError listing every problem
```

[examples/plugins/testbench-backend-example](../examples/plugins/testbench-backend-example) is a whole plug-in
package: a backend in one file, its `pyproject.toml` with the entry point, and its own test.

## Backends

A backend is a machine for the core to run plans and sessions on. The interface, from the reference:

<!-- From docs/reference.md, "Backends". Edit it there. -->
--8<-- "docs/reference.md#backends"

### The backend contract

What `testbench plugin test` checks for a backend (`agent_testbench.backends.contract`):

- its name can be written as `backend:` in testbench.yaml (letters, digits, `-` and `_`), and is the name its
  entry point gives it;
- `config_defaults` maps sections of its own to their settings: not one of testbench.yaml's own (`budget:`,
  `sessions:`, `pricing:`, ...), so a plug-in cannot change a budget, and not another backend's;
- it prices its CPU time and every GPU it offers: the real budget code (config's plan reader and
  `budget.worst_case`, on a throwaway config, no network) gives a one-step plan on it, without a GPU and with each
  of its GPUs, a finite worst case of $0 or more;
- a remote backend costs more than $0 there, at its defaults or once its own price settings (a number under a key
  with `usd` or `price` in its name, e.g. `usd_per_hour: 0` for a machine you own) are set;
- a remote backend takes start-up minutes above 0 (added to every worst case), and says what stops its work on the
  remote side at the time limit (`hard_stop`) or how long a session may run there (`max_session_minutes`);
- `plan_runner()` is a class with `run`, `cancel` and `cost`, `session_class()` one with the `Session` methods
  (either may raise `ConfigError`: the backend does not do that), and it does at least one of the two.

All six built-in backends keep it (a test checks).

## Checks

What a step's `expect:` can check, from another package. (Coming: this section says how to write one.)

## Mocks

A stand-in SDK a step's `mocks:` puts first on PYTHONPATH, never touching the network. (Coming: this section says
how to write one.)

## Sinks

A place `export:` sends a run after it ends, never failing or holding up the run. (Coming: this section says how to
write one.)

## Notifiers

A place `notify:` tells people a run or loop ended, never failing or holding up the run. (Coming: this section says
how to write one.)

## Recipes

A ready plan ladder for `testbench recipes` and `testbench init --recipe`; a recipe never sets `budget:`,
`pricing:` or `guard:`. (Coming: this section says how to write one.)

## Metrics

Numbers read from a step's files, for checks, loops and history. (Coming: this section says how to write one.)
