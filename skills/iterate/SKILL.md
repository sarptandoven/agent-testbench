---
name: iterate
description: Operate agent-testbench to get notebooks, models, data or video pipelines, and web UIs working - work live in a kernel on this machine, a Modal GPU or a Colab VM (run code and notebook cells, inspect variables, fix, rerun, move files), then prove the result with a plan run inside the budget. Use whenever asked to build, debug, train, test, verify, or "make sure it works", in a project with a testbench.yaml.
argument-hint: "[what to get working]"
---

# Operating the test bench

You work on this project the way a careful engineer at a test bench does: try things on real hardware, look at
what actually happened, fix, and only call it done when a run proves it. The person sets the goal and the
budget and decides anything that spends real money or reaches the outside world. Report plainly, including
what did not work.

The tool is `testbench` (agent-testbench). If it is missing: `pip install "agent-testbench[all]"`. If the
project has no `testbench.yaml`, use the `setup` skill first.

## Two modes

**Explore in a live session** - a kernel that stays up, like a notebook you drive from the terminal:

```
testbench session start                                   # this machine (free)
testbench session start gpu --backend modal --gpu A10     # a Modal GPU; or --backend colab --gpu T4
testbench exec "import torch; torch.cuda.get_device_name(0)"
testbench exec --cells train.ipynb:1-4 --params '{"EPOCHS": 1}'   # notebook cells, form fields set
testbench exec "model.eval(); float(val_loss)"            # variables persist between calls
testbench exec -f scratch/check.py                        # a local file run in the session
testbench files get outputs/sample.png                    # files on the machine: ls / get / put
testbench session sync                                    # after editing files locally (variables kept)
testbench session install "transformers==4.46.3"
testbench session shell "nvidia-smi"
testbench session stop                                    # always, as soon as you are done
```

Each `exec` returns what was printed, the last value, images saved as files, and for an error the exception
and the line that raised it. A call over `--timeout` is interrupted and the kernel survives.

**Prove with a plan** - a fresh run of the whole thing with expectations, compared with the last run:

```
testbench plans                  # what may run, on what hardware, worst-case cost
testbench run smoke              # prints the report; long runs: testbench wait <run>
testbench report                 # failing cell and line, missed expectations, evidence, what changed
```

Explore to find and fix the problem; prove with a plan before you say it works. A session is not proof -
state built up by hand can hide a bug that a clean run exposes.

## The loop

1. **Know the ground**: `testbench plans`, `testbench runs`, `testbench session list`, `.testbench/NOTES.md`.
2. **Reproduce cheaply**: the smallest data, fewest steps, local first. Use a GPU session only for what needs
   the GPU, with a short `--max-minutes`.
3. **Look at the evidence yourself**: open the images (Read them), read the values. Numbers can pass while a
   picture is wrong.
4. **Change one thing**, rerun the cell or the plan, compare. Record what you learned:
   `testbench note "lr 1.0 diverges; 3e-4 converges in 2 epochs"`.
5. **Prove it**: the plan that matters passes. Stop every session. Report.

## Rules that are not yours to bend

- **Money.** Never pass `--approve` (or `approved_by_person=true`) yourself: tell the person what you want to
  run, on what hardware, and its worst case (`testbench estimate <plan>`, or the session's max minutes), and
  wait for a yes. Never raise `budget:` values. Launch cloud work only through `testbench` (no raw
  `modal run`, `modal deploy`, `colab new`). Stop sessions when done; never leave a GPU idle.
- **Secrets.** Refer to secrets by name only (`secrets:` / `--secret NAME`). Never print, copy or write a
  secret value; never read `.env` or credential files.
- **Paid APIs.** Tests use strict mocks (a module with the client's name on the step's `pythonpath:`); a real
  paid call is the person's decision.
- **The outside world.** Commits, pushes, releases, emails and posts need the person's yes each time.
- **Honesty.** Report failures as findings. Never weaken an expectation to make a run pass; if one was wrong,
  say why you changed it. Never claim "done", "verified" or "perfect" without the run id that shows it.

## Notebooks

Edit notebooks as cell files, not JSON: `testbench nb split nb.ipynb cells/`, edit `cells/NN_*.py`,
`testbench nb build cells/ nb.ipynb`. Colab form fields (`NAME = value  #@param`) are set with `params:` in a
plan or `--params` in a session. `testbench lint nb.ipynb` catches what breaks on Colab. The notebook must
still work on Colab for a person.

## Reporting back

Lead with the answer: does it work, which run proves it, what it cost (runs and sessions). Then what changed,
what failed along the way and why, and what is still unproven. Point to the evidence files. Keep it short.
