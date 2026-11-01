# Rescue a notebook

Most notebooks on the internet no longer run: a package is missing or was renamed, cells only worked in an order
nobody wrote down, paths point at Colab or at someone's laptop, a data file was downloaded by hand, outputs came
from state that is gone. `testbench rescue` finds out why in about a minute, and says what to do.

```bash
testbench rescue analysis.ipynb                       # read it, run it top to bottom here, diagnose
testbench rescue analysis.ipynb --fix fixed.ipynb     # also write a copy with the mechanical fixes
testbench rescue analysis.ipynb --json                # the diagnosis as JSON
testbench rescue analysis.ipynb --no-run              # only read it: run nothing
```

It reads an `.ipynb` or a percent-format `.py` notebook (`# %%` cells, as jupytext, VS Code and Spyder write them;
a `.py` without `# %%` markers is refused as a plain script). It needs no `testbench.yaml`. Exit code 0 means it ran
top to bottom; 1 means it failed or ran out of time (with
`--no-run`: something that blocks it was found); 2 means it could not start (no such notebook, or `--fix` pointed at
the original).

## What it says

A notebook whose saved outputs came from cells run out of order, with a package nobody installed, an import that was
deleted, and a data file that never came with it (abridged):

```
$ testbench rescue analysis.ipynb --fix analysis-fixed.ipynb
# Rescue: `analysis.ipynb` fails at cell 1 of 5

**cell 1**: `ModuleNotFoundError`: No module named 'umap'
at cell line 2: import umap

**What to do** (missing package): `umap` is not installed here: `pip install umap-learn`. `--fix OUT.ipynb` adds a
first %pip install cell for every missing package it is sure of.

Ran it from the top in a fresh kernel on this machine: 0 cell(s) ran, then cell 1 failed (1.1 s, limit 120 s).
Reading it found 3 more thing(s) further down that will stop it next (below).

## Found by reading it (5)

- **blocks** cell 3, undefined: nothing in the notebook makes `pd`, which cell 3 uses (line 1: `df =
  pd.read_csv('data/train.csv').sample(N_SAMPLES)`): it came from a cell that was deleted, or changed after it ran
  (hidden state). **Do**: Add `import pandas as pd` to the first cell (--fix does).
- **blocks** cell 3, files: cell 3 reads data/train.csv, which is not there (.../data/train.csv). **Do**: Put it at
  .../data/train.csv: nothing in the notebook makes it, so its author put it there by hand.
- **blocks** cell 4, order: cell 4 uses `embedding` (line 1: `print(embedding.shape)`), which cell 5 makes, further
  down; the saved counts show cell 5 ran first. **Do**: Move cell 5 (or the lines that make `embedding`) above cell
  4. testbench never moves cells.
- **warn** cell 5, hidden state: the saved execution counts are out of order: cell 5 ran (count 7) before cell 4
  (count 9). ...

## Fixed copy

Wrote analysis-fixed.ipynb (the original is unchanged):

- a new first code cell installs umap-learn and adds `import pandas as pd`; the code cells after it are numbered one higher
- left for you: 2 thing(s) above that are not mechanical (cells are never moved)

## A plan that proves it once fixed

  analysis-fixed-runs:
    backend: local
    ...
        params: {EPOCHS: 1, N_SAMPLES: 100}   # its parameters; the ones that count work set small
        expect:
          no_errors: true                    # every cell runs
          files: ["**/embedding.npy"]        # what it writes
```

Cells are code cells counted from 1, as in `testbench lint` and a plan's `cells:`.

## What it reads, without running anything

| | what it finds | what it says to do |
|---|---|---|
| hidden state | saved execution counts out of order, with gaps (cells run again, or run and deleted), or cells never run while later ones were; cells saved with an error | trust the run, not the saved outputs |
| order | a name used in a cell that only a later cell makes | move that cell (or the lines) up - rescue never moves cells |
| undefined | a name nothing in the notebook makes; for the usual ones (`np`, `pd`, `plt`, `torch`, ...) the import that makes it | add the import (`--fix` does) or the code |
| packages | every import and `!pip` / `%pip install` line; the pip name where it differs from the import (`sklearn` is scikit-learn, `cv2` opencv-python, `PIL` pillow, `yaml` pyyaml, `bs4` beautifulsoup4, ... about 70 of them, and 110 well-known packages whose names match); which are not installed in the kernel's Python here; imports inside try/except; installs that come after the import | `pip install` the pip name; `%pip` rather than `!pip` |
| Colab | `google.colab` imports and calls, and which ones testbench's stand-in covers: `drive` (the mount point becomes a local folder), `files` (uploads from the plan's `uploads:`, downloads kept as evidence), `userdata` (secrets from environment variables), `output`, `patches`; not `auth` and the others, `output.eval_js` or `%tensorflow_version` | guard what only Colab has, or prove it on `backend: colab` |
| files | files it reads (`open`, `pd.read_*`, `np.load`, `torch.load`, `Path(...).read_text()` ... with paths built from strings, names, f-strings, `/` and `os.path.join`) that are not there and that nothing in the notebook writes or downloads; `/content` paths; paths on someone else's machine (`/home/...`, `/Users/...`, `C:\...`, `/kaggle/...`) | where to put the file, or which earlier cell (an unzip, a download, an upload) should have made it |
| input | `input()` and `getpass()`, which wait for a person | a form field instead |
| secrets | `userdata.get(NAME)` and `os.environ[NAME]` for names that look like secrets, and whether each is set here | the person exports it; a plan lists it under `secrets:` |
| GPU | `.cuda()` or `"cuda"` with no `is_available()` fallback, on a machine without an NVIDIA GPU | a plan on a GPU backend, or a CPU fallback |
| shell | `apt-get`, `nvidia-smi`, downloads, unzips, conda, and `!cd` (which changes nothing for later cells; `%cd` does) | what each needs from the machine |
| syntax | a cell that is not Python 3, and whether it is Python 2 | port it |

## The run

Then it runs the notebook top to bottom in a fresh kernel on this machine, through the same step runner as a local
plan's notebook step - the same kernel, the colab stand-in, the same evidence - in the notebook's own folder (as
Jupyter does), with no form fields set, no uploads and no secrets given, for at most `--timeout` seconds (default
120). It stops at the first cell that fails and says what kind of failure it is:

| kind | when | what it says |
|---|---|---|
| missing package | `ModuleNotFoundError` | the pip package (`sklearn` -> scikit-learn); if a later cell installs it, that it comes too late; if an earlier one did, that the install failed or went to another Python |
| api changed | `cannot import name`, `module has no attribute` | the installed version has no such name: pin the release the notebook was written for, or use the new name |
| order | `NameError` for a name a later cell makes | which cell to move above which |
| undefined | `NameError` for a name nothing makes | the import to add, or that it came from a deleted cell |
| missing file | `FileNotFoundError` | which file, where the notebook expects it (relative to its folder), and whether an earlier cell should have made it; Drive, `/content` and other machines' paths each get their own advice |
| GPU | CUDA or NVIDIA errors on a machine with no NVIDIA GPU | a plan on `backend: modal` with a T4 (or colab), or a CPU fallback; out of GPU memory: smaller batches |
| timeout | the time limit ran out | which cell was running, its parameters (form fields, papermill's) to set small, or a longer `--timeout` |
| input, upload, secret | `input()`, `files.upload()`, `userdata.get()` with nobody there | a form field, the plan's `uploads:`, the plan's `secrets:` |
| magic, syntax, network, memory, name clash | `%tensorflow_version`, Python 2, a download that failed, the kernel died, a local file named like a package | what to change |
| code | anything else | the notebook's own code fails: work on it live with `testbench session start` and `testbench exec --cells` |

The run is the notebook's own code, as "Run all" would run it: its `!pip install` lines install into this machine's
Python, and its downloads go to the network. For a notebook nobody has reviewed, read it first with `--no-run`, and
prove it in a sandbox (`backend: docker` with `docker: {network: none}`, see [the docker backend](docker.md)).

## What it keeps

Everything goes under `.testbench/rescue/<stamp>-<notebook>/`, next to `testbench.yaml` if the notebook is in a
project, else next to the notebook (and `.testbench/` is kept out of git):

- `diagnosis.md` - what it printed; `diagnosis.json` - the same as `--json`
- `executed.ipynb` - the notebook as far as it ran, with each cell's output: the evidence; `log.txt` - what it
  printed, live; `kernel.log` - what the kernel itself printed (a crash shows here); images it displayed
- `plan.yaml` - the suggested plan

## The plan that proves it

The diagnosis ends with a plan to paste under `plans:` in testbench.yaml: the notebook, run top to bottom, with every
cell expected to run (`no_errors: true`), the files it writes expected, and `params:` for its parameters (Colab form fields,
papermill's `parameters` cell) with the ones that count work set small (epochs, steps and iterations to 1, samples and rows to 100, batch sizes to 8), so the
proof is quick. Secrets it reads are listed under `secrets:`; a `files.upload()` gets an `uploads:` line to fill in.
On a machine without an NVIDIA GPU, a notebook that needs one gets `backend: modal` with a T4 - that costs money, so
`testbench estimate` it first. Once the notebook is fixed, `testbench run <plan>` proves it, with a report that names
the code it ran.

## `--fix`

`--fix OUT.ipynb` writes a copy with fixes that are mechanical and safe, and lists each one:

- `!pip install` becomes `%pip install`, which installs into the kernel's own Python;
- a new first code cell `%pip install`s the packages it is sure of (imported, missing here, and in its table of
  well-known packages; never a guess), unpinned - pin the versions that work once it runs;
- the same cell adds the usual import for a name nothing in the notebook makes (`import numpy as np` for `np`), when
  that package is installed or being installed.

It never moves cells (a cell to move is a decision about what the notebook means), never changes paths or code, and
never writes the original. The code cells after the new one are numbered one higher. Then run
`testbench rescue OUT.ipynb` to see what is next.

For a percent-format `.py` notebook, `--fix OUT.py` makes the same fixes on its text, so its header, comments and
cell titles stay as they were (the new cell's `%pip` is written `# %pip`, as jupytext writes magics); `--fix
OUT.ipynb` writes it as an `.ipynb` instead. An `.ipynb`'s fixed copy is an `.ipynb`.

## From an agent

The MCP tool `rescue_notebook` (notebook, project_dir, timeout_seconds, fix_to, run, as_json) runs the same command.
In Claude Code, the `iterate` skill starts with it when a notebook does not run.
