# Notebooks

A `notebook:` step runs a notebook top to bottom in a real kernel, one code cell at a time against the step's
`max_minutes`, and keeps the evidence: the executed notebook (`executed.ipynb`, outputs and all), the images its
cells displayed, and for a failure the cell, the error and the line that raised it. This page says what such a
step accepts. The step's fields are in the [reference](reference.md#steps).

| | accepted |
|---|---|
| files | `.ipynb`; percent-format `.py` (`# %%` cells) |
| parameters | Colab form fields (`NAME = value  #@param`); papermill's `parameters` cell |
| kernels | the one the notebook's kernelspec names, when installed; else `python3` for a Python notebook |
| Colab | a headless `google.colab` stand-in, in Python kernels |

```yaml
steps:
  - name: train
    notebook: train.py              # or train.ipynb
    params: {EPOCHS: 2, DATA: small.csv}
    cells: "1-4"                    # optional: code cells counted from 1
    max_minutes: 20
```

## Parameters

`params:` (and `testbench exec --cells NB --params JSON` in a live session) sets two kinds of parameter. A name
may be either; each value goes where the notebook defines that name.

**Colab form fields.** A line `EPOCHS = 5  #@param {type:"integer"}` is a form field. Its value is replaced on
that line, as the Colab form does, so the cell runs with `EPOCHS = 2  #@param {type:"integer"}`. A field must be
on exactly one `#@param` line in the notebook.

**papermill parameters.** A code cell tagged `parameters` holds defaults, one assignment per line:

```python
# a code cell with the tag `parameters`
EPOCHS = 5
DATA: str = "full.csv"
```

The names it assigns at its top level are its parameters. As papermill does, the values from `params:` go into
a new cell right after it, tagged `injected-parameters`:

```python
# Parameters
EPOCHS = 2
DATA = 'small.csv'
```

The defaults stay as they were, so the executed notebook shows both. When the notebook already has a cell
tagged `injected-parameters` (one saved after a papermill run), that cell is replaced instead. The cell testbench
adds is not counted in `cells:`: code cells keep the numbers they have in the file, and the injected cell runs
right after the `parameters` cell whenever that cell runs. Papermill uses the first cell tagged `parameters`;
so does testbench, and `testbench lint` says when there are two. (In a Jupyter editor, add the tag in the cell's
tags. In a percent-format `.py`, write `# %% tags=["parameters"]`.)

**A name that matches neither** stops the step before anything runs, with an error that names what the
notebook does have:

```
ParamError: params: the notebook has no parameter 'EPOCH'. Its Colab form fields (NAME = value  #@param) are:
LR, SEED; its papermill parameters (set in the code cell tagged `parameters`) are: EPOCHS, DATA
```

papermill itself injects any name you give it; testbench refuses, so a typo cannot run the notebook with its
defaults while looking as if it ran with yours. For the same reason, `cells:` (or `exec --cells`) that leave out
the cell a parameter is set in stop the step with a ParamError naming that cell: add it to `cells:`, or drop the
parameter. `params:`, `cells:`, `uploads:` and `colab_shim:` on a `run:` or `web:` step are refused when
testbench.yaml loads, since only notebook steps use them.

**Listing them.** `testbench nb fields NB` lists both kinds with their cells and current values:

```
$ testbench nb fields train.py
cell 1: EPOCHS = 5  (papermill parameter)
cell 1: DATA = "full.csv"  (papermill parameter)
cell 3: LR = 0.01  (Colab form field)
```

`testbench init` lists them too, in a commented `params:` line under each notebook step it writes.

Values are written as Python literals (`repr`): numbers, strings, booleans, `None`, lists and mappings, which is
what YAML and JSON give.

## Percent-format .py notebooks

A `.py` with `# %%` cell markers is a notebook in jupytext's percent format, which VS Code, Spyder, PyCharm
and jupytext all read and write:

```python
# %% [markdown]
# # Training run
# A markdown cell: every line is a comment.

# %% tags=["parameters"]
EPOCHS = 5

# %% Load the data
# !pip install pandas==2.2.3
import pandas as pd
```

A `notebook:` step pointing to such a file runs it as a notebook: it is read into a notebook, parameters are set
as above, and it runs cell by cell like an `.ipynb`. The executed notebook, `executed.ipynb` in the step's
folder, is the evidence; the `.py` itself is never changed. `testbench lint`, `testbench nb fields`,
`testbench nb split` and `testbench exec --cells` read these files too, and `testbench init` finds them (except a
`.py` paired with an `.ipynb` of the same name, where it takes the `.ipynb`).

A `.py` without any `# %%` marker is a script, not a notebook. A `notebook:` step that points to one is refused
when testbench.yaml is loaded, before anything runs, with what to write instead: a `run:` step
(`run: python script.py`).

**jupytext, when installed, reads the file.** Without it, a small built-in reader does. Install jupytext where
the step runs (`pip install jupytext`; on a remote backend, in its image) to get every detail of the format.
The built-in reader handles:

- `# %%` and `#%%` markers, with an optional title and metadata after them: `# %% Load the data`,
  `# %% tags=["parameters"]` (key=JSON pairs) or `# %% {"tags": ["parameters"]}`;
- `# %% [markdown]` (or `[md]`) and `# %% [raw]` cells, whose lines are comments (`# ` is removed);
- jupytext's header (`# ---` ... `# ---` at the top), which gives the notebook's metadata, such as its
  kernelspec;
- magics, which jupytext writes commented out: `# %pip install x`, `# %%time`, `# !ls` and `# files = !ls` run
  as `%pip install x`, `%%time`, `!ls` and `files = !ls`. A comment that has to stay a comment while looking like
  a magic is written with one more `# ` (`# # %d items`), as jupytext does. Magics written without the comment
  run as they are;
- a `# %%` inside a triple-quoted string, which is not a marker.

Its limits, where it differs from jupytext:

- only the percent format: not the light format, `# In[ ]:` or `# <codecell>` markers, or other languages'
  scripts (`.R`, `.jl`);
- markdown written as a triple-quoted string under `# %% [markdown]` stays a string;
- jupytext's per-cell options such as `active="py"` and its other header options are not applied;
- bare shell commands commented out (`# ls data`, `# cd src`) stay comments (jupytext turns them into
  commands), and blank lines at the start and end of a cell are dropped.

## Kernels

A notebook runs on the kernel its kernelspec names (`metadata.kernelspec.name`, or in a `.py` the `kernelspec:`
of the jupytext header), when that kernel is installed where the step runs: `jupyter kernelspec list` shows
what is. A notebook with no kernelspec runs on `python3`, the Python that runs testbench - except from uvx, `uv
tool` or pipx, whose environment holds none of the project's packages: there `python3` runs on the project's own
virtualenv (an activated one, else `.venv` or `venv`) when it has ipykernel, checked by running its Python with
`-c "import ipykernel"`. Without it, the kernel stays on testbench's Python and the step's log says how to put
ipykernel in the virtualenv. The log's first line names the Python the kernel ran on, and report.json records it
(the step's `runtime`).

- **Another Python kernel** (a virtualenv or conda kernel), when installed, is used as it is: its own Python,
  its own packages, and its `env` from kernel.json. When it is not installed - a kernel on the author's machine
  only - the notebook runs on `python3`, and the step's log says so.
- **Another language** (`ir`, `julia-1.10`, ...), when installed, is used. When it is not installed, the step
  stops with an error that names the kernel, says how to install it (IRkernel for R, IJulia for Julia, ...), and
  lists the kernels that are installed:

  ```
  NotebookError: the notebook runs on the 'ir' kernel (R), which is not installed on this machine. Install R,
  then IRkernel - in R: install.packages('IRkernel'); IRkernel::installspec(). Then `jupyter kernelspec list`
  should show ir. Kernels installed here: python3. (For a remote backend, install it in the machine's image or
  setup.)
  ```

  In a notebook in another language, `params:` is refused - form fields and papermill's injected cell are Python
  code. Pass values as environment variables instead: `env: {EPOCHS: "2"}` in the step, read with
  `Sys.getenv("EPOCHS")` in R or `ENV["EPOCHS"]` in Julia. The Colab stand-in is not loaded there either.
  `testbench lint`'s rules are written for Python notebooks.

Live sessions (`testbench session start`) always run an IPython kernel; `testbench exec --cells` sends a
notebook's cells to it, so use it with Python notebooks.

## The Colab stand-in

Colab notebooks often use `google.colab`, which needs a browser. In a Python kernel, a notebook step (and every
live session) gets a headless stand-in that wins over a real `google.colab`, so a Colab notebook runs unchanged:

| call | headless |
|---|---|
| `files.upload()` | takes the next group of files from the step's `uploads:` (`[[data.csv], [a.mp4, b.mp4]]`); with none left, it fails and says to add them |
| `files.download(path)` | keeps a copy in the step's `downloads/` as evidence |
| `userdata.get(NAME)` | reads the environment variable `NAME` (list it in the step's `secrets:`); a missing one raises `SecretNotFoundError` |
| `drive.mount(path)` | makes `path/MyDrive` an ordinary folder |
| `output.*`, `patches.cv2_imshow` | do nothing; `cv2_imshow` displays the image, so it is kept with the other images |

Set `colab_shim: false` on the step to leave it out. On Modal, Colab and Docker the project is also at
`/content` (Colab's working folder); `testbench lint` points out hard-coded `/content` paths, which exist nowhere
else.

## On remote backends

On every backend that runs plans (Modal, Colab, ssh, Docker), the step's executor runs on the remote machine
with this version of testbench shipped alongside the project. The notebook file goes there as it is; reading a
`.py`, setting parameters and choosing the kernel all happen there, the same way as on this machine. So:

- a percent-format `.py` is read by jupytext only if jupytext is installed in the remote image
  (`modal.pip`, `colab.pip`, `docker.pip`, or on the ssh machine); otherwise by the built-in reader;
- a kernel must be installed on the remote machine: a Python notebook whose kernel is missing there falls back to
  `python3`, and a notebook in another language needs its kernel in the image (`modal.run_commands`,
  `docker.image`, the ssh machine's setup);
- live sessions read the notebook on this machine and send its cells, so `exec --cells` works the same on every
  backend.

The test suite runs percent-format notebooks with papermill parameters on the ssh backend (against a stand-in
for ssh that runs here) and locally. On Modal, Colab and Docker the same executor runs the same code; R or Julia
kernels on those machines are untested.
