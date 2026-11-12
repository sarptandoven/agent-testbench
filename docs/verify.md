# Check it yourself (5 minutes)

Enough to see that 0.3.0 works on your machine before it is released. Everything here is local and free.

## 1. Install the branch (1 minute)

Until 0.3.0 is on PyPI, install it from `main`, in a fresh virtualenv:

```bash
python -m venv tb && . tb/bin/activate          # Windows: tb\Scripts\activate
pip install "agent-testbench[all] @ git+https://github.com/sarptandoven/agent-testbench@main"
testbench --version                             # agent-testbench 0.3.0
```

## 2. The self-test (1 minute)

```bash
testbench selftest
```

It builds a small project in a temporary folder and checks eleven things, one line each. Your `~/.testbench` is not
touched: it uses empty limits and an empty ledger of its own. What you should see:

```
  ok       a plan runs on this machine and passes
  ok       a broken change fails, and the report says which check and why
  ok       evidence is fingerprinted: verify passes, then catches an edited log
  ok       costs are checked before anything starts: refused / needs approval
  ok       a budget that is not a number (NaN) is refused
  ok       a live kernel keeps running and answers
  ok       an LLM app runs against the mock SDK, with no key and no network
  ok       a strategy that decides with tomorrow's data is caught
  ok       a loop keeps a better try and reverts a worse one
  ok       an HTML report is written
  ok       the MCP server answers

11 checks: 11 ok, 0 failed, 0 skipped, in 27 s.
```

The strategy check runs a ten-line strategy that reads tomorrow's close by an off-by-one slice through a
[look-ahead plan](trading-data.md#look-ahead) - the plan must fail - then the fixed one, which must pass.

A failed line prints what went wrong and keeps the folder (`testbench selftest --keep` keeps it anyway).

## 3. See it (1 minute)

```bash
pip install "scikit-learn>=1.5" "matplotlib>=3.9"     # what the tiny-model example trains and plots with
testbench demo                    # the tiny-model example in ./testbench-demo, its free plan run
cd testbench-demo && testbench ui --serve --open
```

The dashboard shows the run, the plan's trend line and today's spend against the limits.

## 4. An agent uses it (2 minutes, optional)

With Claude Code, from a clone of the repository:

```bash
git clone https://github.com/sarptandoven/agent-testbench
testbench demo test-suite          # a pytest suite with a planted bug: its plan fails
cd test-suite && claude --plugin-dir ../agent-testbench
```

Ask: *"The tests plan fails. Use agent-testbench to find out why, fix it, and prove the fix."* It should run the
plan, read the report, change one line in `textstats`, run the plan again until it passes, and name the run that
proves it (`testbench verify` checks that run's evidence).

If all of that holds, 0.3.0 is ready to publish; `docs/publishing.md` lists the release steps.
