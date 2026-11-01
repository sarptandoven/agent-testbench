"""testbench rescue: why a notebook does not run, and what to do about it, in about a minute.

First it reads the notebook without running anything: the order its cells last ran in, names used before the cell
that makes them, the packages it needs (and which are missing here), Colab-only parts (and which ones testbench's
colab stand-in covers), files it reads that are not there, input() calls and shell commands. Then it runs it top
to bottom in a fresh kernel on this machine, the way a plan's notebook step runs, and stops at the first cell that
fails: which cell, the error, what kind of failure it is and what to do. It all goes under
.testbench/rescue/<stamp>-<notebook>/ with the executed notebook as evidence, and a plan that proves the notebook
once fixed. `--fix` writes a copy with mechanical fixes only: cells are never moved, the original never written.

Cells are code cells counted from 1, as in `testbench lint` and a plan's `cells:`.
"""
from __future__ import annotations

import ast
import builtins
import copy
import datetime as dt
import json
import math
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import nbformat

from . import config as C, notebook as NB
from .notebook import fields as nb_fields
from .scaffold import _q, _slug


class RescueError(ValueError):
    pass


def _pairs(text: str) -> dict:
    return dict(p.split(":", 1) for p in text.split())


# import name -> pip name, where they differ; SAME: well-known packages whose pip name is their import name
PIP = _pairs("""
sklearn:scikit-learn cv2:opencv-python PIL:pillow yaml:pyyaml bs4:beautifulsoup4 skimage:scikit-image
dateutil:python-dateutil dotenv:python-dotenv jwt:PyJWT serial:pyserial usb:pyusb magic:python-magic
docx:python-docx pptx:python-pptx fitz:PyMuPDF attr:attrs OpenSSL:pyOpenSSL git:GitPython Crypto:pycryptodome
umap:umap-learn faiss:faiss-cpu Bio:biopython telegram:python-telegram-bot zmq:pyzmq MySQLdb:mysqlclient
psycopg2:psycopg2-binary jose:python-jose multipart:python-multipart slugify:python-slugify
googleapiclient:google-api-python-client absl:absl-py gin:gin-config ffmpeg:ffmpeg-python whisper:openai-whisper
speech_recognition:SpeechRecognition pyaudio:PyAudio talib:TA-Lib osgeo:GDAL tabula:tabula-py camelot:camelot-py
mpl_toolkits:matplotlib pkg_resources:setuptools tensorflow_datasets:tensorflow-datasets
tensorflow_hub:tensorflow-hub tensorflow_addons:tensorflow-addons tensorflow_text:tensorflow-text
tensorflow_probability:tensorflow-probability tf_keras:tf-keras pytorch_lightning:pytorch-lightning
sentence_transformers:sentence-transformers huggingface_hub:huggingface-hub ml_collections:ml-collections
face_recognition:face-recognition pandas_datareader:pandas-datareader nest_asyncio:nest-asyncio
Levenshtein:python-Levenshtein cairo:pycairo gi:PyGObject wx:wxPython win32api:pywin32 win32com:pywin32
llama_cpp:llama-cpp-python google_auth_oauthlib:google-auth-oauthlib pydantic_settings:pydantic-settings
IPython:ipython ruamel:ruamel.yaml lightning_fabric:lightning-fabric keras_cv:keras-cv discord:discord.py
""")
SAME = set("""
numpy pandas matplotlib scipy seaborn torch torchvision torchaudio tensorflow keras jax flax optax transformers
datasets tokenizers accelerate peft diffusers bitsandbytes sentencepiece einops timm xgboost lightgbm catboost
statsmodels plotly tqdm requests httpx openai anthropic nltk spacy gensim networkx sympy numba h5py openpyxl xlrd
pyarrow polars sqlalchemy flask fastapi uvicorn gradio streamlit ipywidgets wandb mlflow optuna librosa soundfile
pydub imageio moviepy albumentations kornia shap lime gdown wget kaggle gym gymnasium shapely geopandas folium
pytesseract easyocr pypdf PyPDF2 pdfplumber selenium playwright rich click typer fire joblib dill cloudpickle
tabulate termcolor colorama boto3 onnx onnxruntime mediapipe ultralytics evaluate trl langchain tiktoken
safetensors lxml jinja2 markdown pydantic altair bokeh dash gspread pymongo redis lightning tensorboard igraph
""".split())
ALIASES = {"np": "import numpy as np", "pd": "import pandas as pd", "plt": "import matplotlib.pyplot as plt",
           "sns": "import seaborn as sns", "tf": "import tensorflow as tf", "torch": "import torch",
           "nn": "import torch.nn as nn", "F": "import torch.nn.functional as F", "px": "import plotly.express as px",
           "os": "import os", "sys": "import sys", "re": "import re", "json": "import json", "math": "import math",
           "random": "import random", "time": "import time", "glob": "import glob", "pickle": "import pickle",
           "Path": "from pathlib import Path", "datetime": "import datetime", "tqdm": "from tqdm import tqdm"}
COLAB = {"drive": "drive.mount() makes the mount point an ordinary local folder (empty: copy the files it expects there)",
         "files": "files.upload() takes the files a plan step lists in `uploads:`; files.download() keeps a copy as evidence",
         "userdata": "userdata.get(NAME) reads the environment variable NAME",
         "output": "front-end helpers do nothing headless (output.eval_js still needs a browser)",
         "patches": "cv2_imshow() shows the image as a cell output"}
KNOWN = set(dir(builtins)) | {"display", "get_ipython", "In", "Out", "exit", "quit", "_ih", "_oh", "_dh", "__name__",
                              "__doc__", "__builtins__", "__spec__", "__loader__", "__package__", "__builtin__"}
DATA_EXT = set(""".csv .tsv .txt .json .jsonl .parquet .feather .xlsx .xls .npy .npz .pt .pth .ckpt .pkl .pickle
.joblib .h5 .hdf5 .safetensors .bin .onnx .zip .tar .gz .tgz .jpg .jpeg .png .gif .bmp .tif .tiff .mp4 .avi .mov
.wav .mp3 .flac .yaml .yml .xml .db .sqlite .mat .nc .shp .geojson .pdf .model .pb .tflite""".split())
READ_ANY = {"read_csv", "read_table", "read_json", "read_parquet", "read_excel", "read_pickle", "read_feather",
            "read_hdf", "read_orc", "read_xml", "read_fwf", "read_stata", "read_sas", "read_spss", "loadtxt",
            "genfromtxt", "fromfile", "imread", "read_image", "load_img", "load_workbook", "VideoCapture", "open"}
READ_PATHLIKE = {"load", "load_model", "File", "ZipFile", "from_file", "read", "read_file"}
WRITERS = {"to_csv", "to_json", "to_parquet", "to_excel", "to_pickle", "to_feather", "to_hdf", "savefig", "save",
           "savez", "savez_compressed", "savetxt", "imwrite", "imsave", "dump", "to_file", "save_model", "save_pretrained",
           "save_weights", "urlretrieve", "download", "mkdir", "makedirs", "write_html", "write_image", "copy",
           "copyfile", "move"}
UNPACKS = {"unzip", "tar", "7z", "gunzip", "kaggle", "cp", "mv", "rsync", "ln", "huggingface-cli", "aria2c"}
PIP_VALUE_OPTS = set("""-r --requirement -c --constraint -e --editable -i --index-url --extra-index-url -f
--find-links -t --target --prefix --root --src --upgrade-strategy --python-version --platform --abi
--implementation --only-binary --no-binary --progress-bar --cache-dir --log --timeout --retries --proxy
--trusted-host --cert --client-cert --exists-action --report --config-settings -C""".split())
SECRETISH = re.compile(r"KEY|TOKEN|SECRET|PASSW|AUTH|CREDENTIAL", re.I)
MACHINE_PATH = re.compile(r"^(/(home|Users|mnt|media|data|scratch|root|kaggle|gdrive|drive|workspace)/|~/|[A-Za-z]:[\\/])")
SEVERITY = {"blocks": 0, "warn": 1, "info": 2}


def has_gpu() -> bool:
    return shutil.which("nvidia-smi") is not None


def pip_name(module: str) -> tuple[str | None, bool]:
    """(pip name, sure) for an import: sure when it is in the table, else the import name is a guess."""
    parts = module.split(".")
    if parts[0] == "google" and len(parts) > 1:
        if parts[1] == "colab":
            return None, True
        if parts[1] == "cloud" and len(parts) > 2:
            return f"google-cloud-{parts[2].replace('_', '-')}", True
        known = {"protobuf": "protobuf", "oauth2": "google-auth", "auth": "google-auth", "genai": "google-genai",
                 "generativeai": "google-generativeai"}
        return known.get(parts[1], f"google-{parts[1]}"), parts[1] in known
    top = parts[0]
    if top in PIP:
        return PIP[top], True
    return top.replace("_", "-"), top in SAME


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# ---- reading the notebook ---------------------------------------------------------------------------------

def _fname(node) -> str | None:
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None


def _root(node) -> str | None:
    """The name an attribute chain starts from: PIL for PIL.Image, z for z.open."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _catches(handler, names) -> bool:
    if handler.type is None:
        return True
    return any(_fname(t) in names for t in (handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]))


def _ipy(node):
    """('system'|'getoutput'|'run_line_magic'|'run_cell_magic', [text args]) for IPython's own calls in a cell."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Call) \
            and _fname(node.func.value.func) == "get_ipython":
        return node.func.attr, [a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else "" for a in node.args]
    return None


_TM = []


def _parse(src: str):
    """(tree, SyntaxError): IPython syntax (!cmd, %magic, %%cell magic) is made Python first, as IPython does."""
    try:
        if not _TM:
            from IPython.core.inputtransformer2 import TransformerManager
            _TM.append(TransformerManager())
        code = _TM[0].transform_cell(src)
    except Exception:
        code = re.sub(r"^(\s*)[!%].*$", r"\1pass", src, flags=re.M)
    try:
        return ast.parse(code), None
    except SyntaxError as exc:
        return None, exc


class _Names(ast.NodeVisitor):
    """What a cell's top level uses and defines, in the order Python meets it. Function and lambda bodies run later,
    so they are left out (their defaults and decorators are not)."""

    def __init__(self):
        self.events, self.scopes, self.quiet = [], [], 0

    def use(self, name, line):
        if not self.quiet and not any(name in s for s in self.scopes):
            self.events.append(("use", name, line))

    def define(self, name):
        self.scopes[-1].add(name) if self.scopes else self.events.append(("def", name, 0))

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.use(node.id, node.lineno)
        elif isinstance(node.ctx, ast.Store):
            self.define(node.id)

    def visit_Assign(self, node):
        self.visit(node.value)
        for t in node.targets:
            self.visit(t)

    def visit_AnnAssign(self, node):
        if node.value:
            self.visit(node.value)
        self.visit(node.target)

    def visit_AugAssign(self, node):
        if isinstance(node.target, ast.Name):
            self.use(node.target.id, node.lineno)
        self.visit(node.value)
        self.visit(node.target)

    def visit_NamedExpr(self, node):
        self.visit(node.value)
        self.visit(node.target)

    def visit_For(self, node):
        self.visit(node.iter)
        self.visit(node.target)
        for s in node.body + node.orelse:
            self.visit(s)
    visit_AsyncFor = visit_For

    def visit_FunctionDef(self, node):
        for d in node.decorator_list + node.args.defaults + [d for d in node.args.kw_defaults if d]:
            self.visit(d)
        for g in ast.walk(node):
            if isinstance(g, ast.Global):
                for name in g.names:
                    self.define(name)
        self.define(node.name)
    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        for d in node.args.defaults + [d for d in node.args.kw_defaults if d]:
            self.visit(d)

    def visit_ClassDef(self, node):
        for d in node.decorator_list + node.bases + [k.value for k in node.keywords]:
            self.visit(d)
        self.scopes.append(set())
        for s in node.body:
            self.visit(s)
        self.scopes.pop()
        self.define(node.name)

    def visit_Import(self, node):
        for a in node.names:
            self.define(a.asname or a.name.split(".")[0])

    def visit_ImportFrom(self, node):
        for a in node.names:
            if a.name == "*":
                self.events.append(("opaque", "*", node.lineno))
            else:
                self.define(a.asname or a.name)

    def visit_Try(self, node):
        catches = any(_catches(h, {"NameError", "Exception", "BaseException"}) for h in node.handlers)
        self.quiet += catches
        for s in node.body:
            self.visit(s)
        self.quiet -= catches
        for h in node.handlers:
            if h.type:
                self.visit(h.type)
            if h.name:
                self.define(h.name)
            for s in h.body:
                self.visit(s)
        for s in node.orelse + node.finalbody:
            self.visit(s)
    visit_TryStar = visit_Try

    def _comprehension(self, node, parts):
        self.visit(node.generators[0].iter)
        self.scopes.append(set())
        for i, g in enumerate(node.generators):
            if i:
                self.visit(g.iter)
            self.visit(g.target)
            for c in g.ifs:
                self.visit(c)
        for p in parts:
            self.visit(p)
        self.scopes.pop()

    def visit_ListComp(self, node):
        self._comprehension(node, [node.elt])
    visit_SetComp = visit_GeneratorExp = visit_ListComp

    def visit_DictComp(self, node):
        self._comprehension(node, [node.key, node.value])

    def visit_MatchAs(self, node):
        if node.pattern:
            self.visit(node.pattern)
        if node.name:
            self.define(node.name)

    def visit_MatchStar(self, node):
        if node.name:
            self.define(node.name)

    def visit_MatchMapping(self, node):
        self.generic_visit(node)
        if node.rest:
            self.define(node.rest)

    def visit_Call(self, node):
        magic = _ipy(node)
        if magic and magic[0] == "run_line_magic" and magic[1][:1] and magic[1][0] in ("time", "timeit", "prun"):
            tree, _ = _parse(re.sub(r"^(\s*-\w+(\s+\d+)?)*\s*", "", magic[1][1] if len(magic[1]) > 1 else ""))
            if tree:
                self.visit(tree)
            return
        if (magic and magic[0] == "run_line_magic" and magic[1][:1] and magic[1][0] in ("run", "store", "load", "pylab")) \
                or _fname(node.func) in ("globals", "exec", "execfile"):
            self.events.append(("opaque", _fname(node.func), node.lineno))
        self.generic_visit(node)


def _cell(n: int, cell) -> dict:
    c = {"n": n, "source": cell.source, "count": cell.get("execution_count"), "shell": [], "magics": [],
         "writefile": None, "events": [], "syntax": None}
    tree, err = _parse(cell.source)
    first = tree.body[0].value if tree and len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr) else None
    magic = _ipy(first)
    if magic and magic[0] == "run_cell_magic" and len(magic[1]) == 3:
        name, args, body = magic[1]
        c["magics"].append((name, args))
        words = args.split()
        if name in ("capture", "time", "timeit", "prun"):
            tree, err = _parse(body)
            if name == "capture" and [w for w in words if not w.startswith("-")]:
                c["events"].append(("def", [w for w in words if not w.startswith("-")][0], 0))
        else:
            tree = None
            if name in ("bash", "sh", "shell", "script", "system"):
                c["shell"] += [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")]
            elif name == "writefile" and [w for w in words if not w.startswith("-")]:
                c["writefile"] = [w for w in words if not w.startswith("-")][-1]
    c["tree"] = tree
    if err:
        c["syntax"] = err
        c["events"].append(("opaque", "syntax", 0))
    if tree:
        for node in ast.walk(tree):
            m = _ipy(node)
            if m and m[0] in ("system", "getoutput") and m[1]:
                c["shell"].append(m[1][0])
            elif m and m[0] == "run_line_magic" and m[1]:
                c["magics"].append((m[1][0], m[1][1] if len(m[1]) > 1 else ""))
        w = _Names()
        w.visit(tree)
        c["events"] += w.events
    return c


def _str(node, consts: dict) -> str | None:
    """The text of an expression made of strings, names set to strings, f-strings, Path(), / and os.path.join."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.JoinedStr):
        parts = [v.value if isinstance(v, ast.Constant) else _str(v.value, consts)
                 if isinstance(v, ast.FormattedValue) and not v.format_spec else None for v in node.values]
        return None if None in parts else "".join(map(str, parts))
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add)):
        a, b = _str(node.left, consts), _str(node.right, consts)
        if a is None or b is None:
            return None
        return posixpath.join(a, b) if isinstance(node.op, ast.Div) else a + b
    if isinstance(node, ast.Call):
        fn = _fname(node.func)
        if fn in ("expanduser", "resolve", "absolute") and isinstance(node.func, ast.Attribute):
            return _str(node.func.value, consts)
        is_join = fn == "join" and isinstance(node.func, ast.Attribute) and _fname(node.func.value) == "path"
        if fn in ("Path", "PurePath", "PosixPath") or is_join:
            parts = [_str(a, consts) for a in node.args]
            return None if not parts or None in parts else posixpath.join(*parts)
    return None


def _pathlike(s: str) -> bool:
    return bool(s) and "://" not in s and "\n" not in s and len(s) < 300 and (
        "/" in s or "\\" in s or os.path.splitext(s)[1].lower() in DATA_EXT)


def _commands(line: str) -> list[list[str]]:
    out = []
    for part in re.split(r"&&|\|\||;|\|", line):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        while words and re.fullmatch(r"\w+=\S*|sudo|time|nohup|env", words[0]):
            words = words[1:]
        if words:
            out.append(words)
    return out


def _pip_install(words: list[str]) -> list[tuple[str, str]] | None:
    """[(name, spec)] for a pip install command (pip, pip3, python -m pip, uv pip), or None if it is not one."""
    head = words[:words.index("install")] if "install" in words else None
    if head is None or not (head[-1:] in (["pip"], ["pip3"]) or head[-2:] == ["-m", "pip"]):
        return None
    out, skip = [], False
    for w in words[words.index("install") + 1:]:
        if skip:
            skip = False
            continue
        if w.startswith("-"):
            skip = w in PIP_VALUE_OPTS
            continue
        if "#egg=" in w:
            out.append((w.split("#egg=")[1].split("&")[0], w))
            continue
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?", w)
        if m and "://" not in w and "/" not in w and not w.startswith(("$", "{")):
            out.append((m.group(1), w))
    return out


def _shell_files(words: list[str]) -> tuple[list[str], bool]:
    """Files a download command makes, and whether it makes others it does not name (an archive, a clone)."""
    cmd, urls = words[0], [w for w in words if "://" in w]
    opt = lambda *names: next((words[i + 1] for i, w in enumerate(words[:-1]) if w in names), None)
    base = lambda u: posixpath.basename(u.split("?")[0].rstrip("/"))
    if cmd == "wget":
        out = opt("-O", "--output-document")
        return ([out] if out else [posixpath.join(opt("-P", "--directory-prefix") or "", base(u)) for u in urls]), False
    if cmd == "curl":
        out = opt("-o", "--output")
        return ([out] if out else [base(u) for u in urls] if {"-O", "--remote-name"} & set(words) else []), False
    if cmd == "gdown":
        out = opt("-O", "--output")
        return ([out] if out else []), not out
    return [], cmd in UNPACKS or words[:2] == ["git", "clone"]


def _shell_note(words: list[str]) -> tuple[str, str] | None:
    """(severity, what it means) for a shell command a notebook runs."""
    cmd = words[0]
    if cmd in ("apt", "apt-get", "yum", "dnf", "apk", "brew"):
        here = "found here" if shutil.which(cmd) else "not on this machine"
        return "warn", f"installs system packages with {cmd} ({here}; Colab runs as root with apt-get)"
    if cmd == "nvidia-smi":
        return ("info" if has_gpu() else "warn"), "asks for the NVIDIA GPU" + ("" if has_gpu() else ", which this machine does not have")
    if cmd == "cd":
        return "warn", "`!cd` runs in a shell of its own and changes nothing for later lines or cells; `%cd DIR` does"
    if cmd in ("wget", "curl", "gdown", "kaggle", "aria2c", "huggingface-cli") or words[:2] == ["git", "clone"]:
        return "info", "downloads at run time: it needs the network, and breaks if the source moved or needs a login"
    if cmd in ("unzip", "tar", "7z", "gunzip"):
        return "info", "unpacks files the later cells may read"
    if cmd in ("conda", "mamba") and "install" in words:
        return "warn", "installs with conda, which this machine may not have; `%pip install` works in any Python kernel"
    return None


def read(nb_path: str | Path, workdir: str | Path | None = None) -> dict:
    """Everything rescue learns without running the notebook: findings (each with what to do) and the facts the
    run's diagnosis and the suggested plan use."""
    nb_path = Path(nb_path)
    workdir = Path(workdir or nb_path.parent)
    try:
        nb = NB.read(nb_path)
    except NB.NotebookError as exc:
        raise RescueError(str(exc)) from None
    except Exception as exc:
        raise RescueError(f"{nb_path} cannot be read as a notebook ({str(exc)[:300]}); rescue reads an .ipynb, or a "
                          "percent-format .py (`# %%` cells)") from None
    lang = (nb.metadata.get("kernelspec") or {}).get("language") or (nb.metadata.get("language_info") or {}).get("name") or "python"
    if lang.lower() != "python":
        raise RescueError(f"{nb_path.name} is a {lang} notebook; testbench rescue reads and runs Python notebooks")
    cells = [_cell(n, c) for n, c in enumerate((c for c in nb.cells if c.cell_type == "code"), 1)]
    findings = []

    def add(kind, severity, cell, message, fix=""):
        findings.append({"kind": kind, "severity": severity, "cell": cell, "message": message, "fix": fix})

    imports, pip_lines, reads, writes, unknown, consts = [], [], [], set(), [], {}
    inputs, secrets, env_vars, colab, mounts, uploads, gpu_hints, gpu_guard = [], [], [], {}, [], [], [], False
    abs_paths, content, code_dirs = [], [], ["."]
    imported ={(a.asname or a.name).split(".")[0] for c in cells if c["tree"] for node in ast.walk(c["tree"])
                if isinstance(node, (ast.Import, ast.ImportFrom)) for a in node.names}       # open() of io, gzip, Image
    for c in cells:
        n = c["n"]
        for line in c["shell"]:
            for words in _commands(line):
                pkgs = _pip_install(words)
                if pkgs is not None:
                    pip_lines += [{"cell": n, "name": name, "spec": spec, "bang": True} for name, spec in pkgs]
                    continue
                made, more = _shell_files(words)
                writes.update(made)
                if more:
                    unknown.append((n, " ".join(words)[:80]))
                note = _shell_note(words)
                if note:
                    add("shell", note[0], n, f"`!{' '.join(words)[:100]}` {note[1]}.")
        for name, args in c["magics"]:
            if name == "pip" and _pip_install(["pip", *args.split()]):
                pip_lines += [{"cell": n, "name": p, "spec": s, "bang": False} for p, s in _pip_install(["pip", *args.split()])]
            elif name == "conda":
                add("shell", "warn", n, f"`%conda {args[:80]}` installs with conda, which this machine may not have.",
                    "Use `%pip install` for the same packages.")
            elif name == "cd":
                unknown.append((n, f"%cd {args}"))
            elif name == "tensorflow_version":
                add("colab", "blocks", n, f"`%tensorflow_version {args}` is a magic only Colab ever had; anywhere else the "
                    "cell fails.", "Delete the line and install the TensorFlow version it needs with %pip.")
        if c["writefile"]:
            writes.add(c["writefile"])
        if c["syntax"]:
            e = c["syntax"]
            py2 = re.search(r"^\s*print\s+[^\s(=]|^\s*except\s+\w+\s*,\s*\w+\s*:|^\s*exec\s+[\"']", c["source"], re.M)
            add("syntax", "blocks", n, f"cell {n} is not valid Python 3 (line {e.lineno}: {e.msg})"
                + (": it is Python 2 code" if py2 else "") + ".",
                "Port it to Python 3 (print(...), except E as e)." if py2 else "Fix the line; the run stops there.")
        for out in cell_outputs(nb, n):
            if out.get("output_type") == "error":
                add("saved_error", "warn", n, f"cell {n} was saved with an error ({out.get('ename')}: "
                    f"{str(out.get('evalue'))[:150]}): it failed for its author too, the last time it ran.")
                break
        tree = c["tree"]
        if not tree:
            continue
        parents = {ch: p for p in ast.walk(tree) for ch in ast.iter_child_nodes(p)}

        def guarded(node, names=("ImportError", "ModuleNotFoundError", "Exception", "BaseException")):
            while node in parents:
                node = parents[node]
                if isinstance(node, ast.Try) and any(_catches(h, names) for h in node.handlers):
                    return True
            return False

        for stmt in tree.body:
            for node in ast.walk(stmt):
                if isinstance(node, ast.Import):
                    imports += [(a.name, n, guarded(node)) for a in node.names]
                    for a in node.names:
                        if a.name.startswith("google.colab"):
                            colab.setdefault(".".join(a.name.split(".")[2:3]), []).append((n, guarded(node)))
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    imports.append((node.module, n, guarded(node)))
                    names = [a.name for a in node.names]
                    parts = names if node.module == "google.colab" else [""] if node.module == "google" and "colab" in names \
                        else [node.module.split(".")[2]] if node.module.startswith("google.colab.") else []
                    for part in parts:
                        colab.setdefault(part, []).append((n, guarded(node)))
                elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and _fname(node.value) == "environ":
                    key = _str(node.slice, consts)
                    if key:
                        secrets.append((key, n, "os.environ", guarded(node, ("KeyError", "Exception", "BaseException"))))
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if re.fullmatch(r"cuda(:\d+)?", node.value):
                        gpu_hints.append(n)
                    elif node.value.startswith("/content"):
                        content.append((node.value, n))
                    elif MACHINE_PATH.match(node.value) and "\n" not in node.value:
                        abs_paths.append((node.value, n))
                elif isinstance(node, ast.Call):
                    fn, args = _fname(node.func), node.args
                    owner = node.func.value if isinstance(node.func, ast.Attribute) else None
                    kw = {k.arg: k.value for k in node.keywords if k.arg}
                    via_path = owner is not None and (     # Path("x").open(), (OUT / "x").open(), p.open() for p = Path(..)
                        isinstance(owner, ast.Call) and _fname(owner.func) in ("Path", "PurePath")
                        or isinstance(owner, (ast.BinOp, ast.Name)) and _str(owner, consts) is not None)
                    if fn in ("input", "getpass") and (owner is None or _fname(owner) == "getpass"):
                        inputs.append((n, node.lineno, fn))
                    elif fn == "mount" and _fname(owner) == "drive":
                        mounts.append((n, (_str(args[0], consts) if args else None) or "/content/drive"))
                    elif fn == "upload" and _fname(owner) == "files":
                        uploads.append(n)
                    elif fn == "get" and _fname(owner) == "userdata" and args and _str(args[0], consts):
                        secrets.append((_str(args[0], consts), n, "userdata.get", guarded(node)))
                    elif fn == "authenticate_user" or fn == "eval_js":
                        colab.setdefault("auth" if fn == "authenticate_user" else "output.eval_js", []).append((n, guarded(node)))
                    elif (fn == "getenv" or (fn == "get" and _fname(owner) == "environ")) and len(args) == 1 \
                            and "default" not in kw and _str(args[0], consts):
                        env_vars.append((_str(args[0], consts), n))
                    elif fn in ("append", "insert") and _fname(owner) == "path" and _root(owner) == "sys" and args:
                        arg = args[-1]                     # sys.path.append(os.path.abspath("..")): its code is local
                        while isinstance(arg, ast.Call) and _fname(arg.func) in ("abspath", "realpath", "normpath", "str") \
                                and arg.args:
                            arg = arg.args[0]
                        if _str(arg, consts):
                            code_dirs.append(_str(arg, consts))
                    elif fn == "cuda" and owner is not None:
                        gpu_hints.append(n)
                    elif fn in ("is_available", "device_count", "list_physical_devices", "gpu_device_name"):
                        gpu_guard = True
                    if fn in ("read_text", "read_bytes") and via_path:
                        reads.append((_str(owner, consts), n))
                    elif fn in ("write_text", "write_bytes") and via_path:
                        writes.add(_str(owner, consts))
                    elif fn == "open" and owner is not None and not via_path and _root(owner) not in imported:
                        pass                               # an object's own open(): a member of a zip file, a bucket
                    elif fn == "open":
                        path = _str(owner, consts) if via_path else _str(args[0], consts) if args else _str(kw.get("file"), consts)
                        mode_node = (args[0] if via_path else args[1] if len(args) > 1 else None) if args else None
                        mode = _str(kw.get("mode") or mode_node, consts) or "r"
                        if path and "://" not in path and re.search(r"[wax]", mode):
                            writes.add(path)
                        elif path and "://" not in path:
                            reads.append((path, n))
                    elif fn in READ_ANY or fn in READ_PATHLIKE:
                        first = args[0] if args else next((kw[k] for k in ("filepath_or_buffer", "path", "fname", "file",
                                                                         "filename", "path_or_buf", "io") if k in kw), None)
                        path = _str(first, consts)
                        mode = _str(args[1] if len(args) > 1 else kw.get("mode"), consts) if fn in ("File", "ZipFile") \
                            else None                      # h5py.File(path, "w"), ZipFile(path, "w") make the file
                        if path and mode and re.search(r"[wax]", mode) and _pathlike(path):
                            writes.add(path)
                        elif path and (fn in READ_ANY and "://" not in path or _pathlike(path)):
                            reads.append((path, n))
                    elif fn in WRITERS:
                        for a in list(args) + list(kw.values()):
                            s = _str(a, consts)
                            if s and _pathlike(s) or (s and fn in ("mkdir", "makedirs")):
                                writes.add(s)
                    if fn in ("extractall", "unpack_archive") or (fn == "upload" and _fname(owner) == "files"):
                        unknown.append((n, f"{fn}()"))
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
                for target in getattr(stmt, "targets", None) or [stmt.target]:
                    if isinstance(target, ast.Name) and _str(stmt.value, consts) is not None:
                        consts[target.id] = _str(stmt.value, consts)

    # ---- the order cells ran in, and names used before the cell that makes them
    ran = [(c["n"], c["count"]) for c in cells if c["count"] is not None]
    if ran:
        back = [(a, b) for a, b in zip(ran, ran[1:]) if b[1] < a[1]]
        if back:
            shown = "; ".join(f"cell {b[0]} ran (count {b[1]}) before cell {a[0]} (count {a[1]})" for a, b in back[:3])
            add("hidden_state", "warn", back[0][1][0], f"the saved execution counts are out of order: {shown}. Its outputs "
                "came from a session that ran cells in another order, so they may rest on state a run from the top "
                "never builds.", "Trust the run below, not the saved outputs.")
        counts = sorted(k for _, k in ran)
        missing = counts[-1] - len(counts)
        if missing > 0 and not back:
            jumps = [(a, b) for a, b in zip(ran, ran[1:]) if b[1] - a[1] > 1]
            where = f" (between cells {jumps[0][0][0]} and {jumps[0][1][0]}: {jumps[0][0][1]} to {jumps[0][1][1]})" if jumps \
                else f" (the first saved count is {counts[0]})"
            add("hidden_state", "warn", jumps[0][1][0] if jumps else ran[0][0], f"{missing} execution(s) are not in the "
                f"saved notebook{where}: cells were run again, or run and then deleted or edited, so saved outputs can "
                "come from code that is no longer there.", "Trust the run below, not the saved outputs.")
        skipped = [c["n"] for c in cells if c["count"] is None and c["source"].strip() and c["n"] < ran[-1][0]]
        if skipped:
            add("hidden_state", "info", skipped[0], f"cell(s) {', '.join(map(str, skipped[:8]))} have no saved count "
                "although later cells do: they were not run in that session (or their outputs were cleared).")
    defines: dict[str, list[int]] = {}
    for c in cells:
        for kind, name, _ in c["events"]:
            if kind == "def" and c["n"] not in defines.setdefault(name, []):
                defines[name].append(c["n"])
    known, reported, undefined, opaque = set(KNOWN), set(), [], False
    hidden = [c["n"] for c in cells if any(e[0] == "opaque" and e[1] != "syntax" for e in c["events"])]  # *, %run, exec
    count = {c["n"]: c["count"] for c in cells}
    for c in cells:
        n = c["n"]
        for kind, name, line in c["events"]:
            if kind == "def":
                known.add(name)
            elif kind == "opaque":
                opaque = True
            elif name not in known and name not in reported and not re.fullmatch(r"_+|_i+|_i?\d+", name):
                maker = (defines.get(name) or [None])[0]
                if maker == n:
                    continue                    # made further down the same cell: loops and branches make that fine
                reported.add(name)
                text = _line(c["source"], line)
                if maker:
                    agree = count[maker] is not None and count[n] is not None and count[maker] < count[n]
                    add("order", "blocks", n, f"cell {n} uses `{name}` (line {line}: `{text}`), which cell {maker} makes, "
                        "further down" + (f"; the saved counts show cell {maker} ran first" if agree else "") + ".",
                        f"Move cell {maker} (or the lines that make `{name}`) above cell {n}. testbench never moves cells.")
                elif name == "__file__":
                    add("undefined", "blocks", n, f"cell {n} uses `__file__` (line {line}), which only scripts have, never "
                        "a notebook.", "Use Path.cwd(): a notebook runs in its own folder.")
                elif not opaque:
                    undefined.append(name)
                    later = next((k for k in hidden if k > n), None)
                    add("undefined", "blocks", n, f"nothing in the notebook makes `{name}`, which cell {n} uses (line {line}: "
                        f"`{text}`): it came from a cell that was deleted, or changed after it ran (hidden state)"
                        + (f"; or from cell {later} further down, which rescue cannot read through (a star import, "
                           "%run or exec)" if later else "") + ".",
                        f"Add `{ALIASES[name]}` to the first cell (--fix does)." if name in ALIASES
                        else f"Write the code that makes `{name}` in a cell above it.")

    # ---- packages
    local = {Path(w).stem for w in writes if w.endswith(".py")}
    for d in {workdir / Path(c).expanduser() for c in code_dirs}:     # modules, and folders of them (namespace ones too)
        if d.is_dir():
            local |= {p.stem for p in d.glob("*.py")} | {p.name for p in d.iterdir() if p.is_dir() and (
                any(p.glob("*.py")) or any(p.glob("*/__init__.py")))}
    wanted: dict[str, dict] = {}
    for mod, n, safe in imports:
        top = mod.split(".")[0]
        key = ".".join(mod.split(".")[:3 if mod.startswith("google.cloud") else 2]) if top == "google" else top
        if top in sys.stdlib_module_names or top == "__future__" or key in ("google.colab", "google") or top in local:
            continue
        w = wanted.setdefault(key, {"import": key, "cells": [], "guarded": True})
        w["cells"] += [n] if n not in w["cells"] else []
        w["guarded"] = w["guarded"] and safe
    probe = _probe(sorted(set(wanted) | {ALIASES[u].split()[1].split(".")[0] for u in undefined if u in ALIASES}), workdir)
    packages = []
    for key, w in wanted.items():
        pip, sure = pip_name(key)
        names = {_norm(pip), _norm(key)}
        by = [p for p in pip_lines if _norm(p["name"]) in names or any(_norm(p["name"]).startswith(x + "-") for x in names)]
        here = probe.get(key, {})
        pkg = {**w, "pip": pip, "sure": sure, "installed": bool(here.get("installed")), "version": here.get("version"),
               "notebook_installs": by[0]["cell"] if by else None, "pinned": by[0]["spec"] if by else None}
        packages.append(pkg)
        first = w["cells"][0]
        if pkg["installed"]:
            continue
        if by and by[0]["cell"] <= first:
            add("packages", "info", first, f"`{key}` is not installed here; cell {by[0]['cell']} installs it "
                f"(`{by[0]['spec']}`) before cell {first} imports it.")
        elif w["guarded"]:
            add("packages", "info", first, f"`{key}` is not installed here; cell {first} imports it inside try/except, "
                "so the notebook goes on without it.")
        else:
            add("packages", "blocks", first, f"cell {first} imports `{key}`, which is not installed here"
                + (f"; cell {by[0]['cell']} installs it, but only after that" if by else "") + ".",
                f"`pip install {pip}`" + ("" if sure else f" (a guess: `{key}`'s pip name may differ, check its docs)")
                + (" - --fix adds a first %pip install cell for it." if sure else "."))
    bang = sorted({p["cell"] for p in pip_lines if p["bang"]})
    if bang:
        add("packages", "info", bang[0], f"cell(s) {', '.join(map(str, bang))} use `!pip install`, which can install into "
            "another Python than the kernel's.", "Use `%pip install` (--fix rewrites them).")

    # ---- Colab-only parts
    covered = []
    for part, where in sorted(colab.items()):
        n, safe = where[0][0], all(s for _, s in where)
        sub = part.split(".")[0]
        if not part or sub in COLAB:
            covered.append({"part": f"google.colab{'.' + part if part else ''}", "cells": sorted({w[0] for w in where}),
                            "covered": part != "output.eval_js", "how": COLAB.get(sub, "the stand-in package imports")})
            if part == "output.eval_js":
                add("colab", "blocks", n, f"cell {n} calls output.eval_js(), which needs Colab's browser page.",
                    "Skip it off Colab (try/except, or check `'google.colab' in sys.modules`).")
        else:
            covered.append({"part": f"google.colab.{part}", "cells": sorted({w[0] for w in where}), "covered": False,
                            "how": "not in testbench's stand-in: it exists only on Colab"})
            add("colab", "info" if safe else "blocks", n, f"cell {n} uses google.colab.{part}, which exists only on Colab and "
                "is not in testbench's stand-in.", "Guard it (try/except ImportError) so the notebook runs elsewhere, "
                "or prove it on `backend: colab`.")
    for n in uploads[:1]:
        add("colab", "warn", n, f"cell {n} calls files.upload(), which waits for a person to pick files; a headless run "
            "has nobody to pick them, so this run fails there.", "In a plan, list the files under the step's "
            "`uploads:` (the stand-in hands them over); better still, a path form field that skips the upload.")
    for n, point in mounts[:1]:
        drive = [p for p, _ in content + abs_paths if p.startswith(point.rstrip("/") + "/") and p.count("/") > 3]
        add("colab", "info", n, f"cell {n} mounts Google Drive at {point}: the stand-in makes {point}/MyDrive an "
            "empty local folder" + (f", so {drive[0]} is missing unless you copy it there" if drive else "") + ".",
            "Copy the files the notebook reads from Drive into that folder, or make their paths form fields.")

    # ---- files it reads that are not there, and paths from someone else's machine
    made = {os.path.normpath(w) for w in writes if w} | {posixpath.basename(w) for w in writes if w}
    flagged = set()
    for path, n in reads:
        if not path or path in flagged or os.path.normpath(path) in made or posixpath.basename(path) in made:
            continue
        where = Path(path).expanduser() if Path(path).expanduser().is_absolute() else workdir / path
        if where.exists():
            continue
        flagged.add(path)
        before = [u for u in unknown if u[0] <= n] if not os.path.isabs(os.path.expanduser(path)) else []
        if path.startswith("/content"):
            kind, fix = "colab", ("Copy the file there from Drive, or make the path a form field." if "/drive/" in path
                                  else "/content is Colab's folder: use a path relative to the notebook, or run it on "
                                       "`backend: docker`, where the project is at /content.")
        elif os.path.isabs(os.path.expanduser(path)):
            kind, fix = "files", "It is a path on the author's machine: put the file in the project and read it with a "\
                                 "path relative to the notebook (or a form field)."
        else:
            kind, fix = "files", f"Put it at {where}" + (f", or check that cell {before[-1][0]} (`{before[-1][1]}`) "
                                                          "makes it" if before else ": nothing in the notebook makes it, "
                                                          "so its author put it there by hand") + "."
        add(kind, "warn" if before else "blocks", n, f"cell {n} reads {path}, which is not there ({where}).", fix)
    for path, n in abs_paths:
        if path in flagged or path.startswith("/content"):
            continue
        flagged.add(path)
        there = Path(path).expanduser().exists()
        add("paths", "info" if there else "warn", n, f"cell {n} names {path}, a path on "
            + ("this machine (the notebook will not run elsewhere)" if there else "its author's machine, not on this one")
            + ".", "Use a path relative to the notebook, or a form field (PATH = '...'  #@param).")

    # ---- input(), secrets, GPU
    for n, line, fn in inputs:
        add("input", "blocks", n, f"cell {n} calls {fn}() (line {line}), which waits for someone to type an answer; a "
            "headless run has nobody to answer.", "Make it a form field instead (NAME = 'value'  #@param "
            "{type:'string'}), which a plan sets with params:.")
    for key, n, how, safe in secrets:
        there, secret = key in os.environ, how == "userdata.get" or bool(SECRETISH.search(key))
        if there and not secret:
            continue
        add("secrets", "info" if there or safe else "blocks", n, f"cell {n} reads the "
            f"{'secret' if secret else 'environment variable'} {key} with {how}, and {key} is "
            f"{'set' if there else 'not set'} in the environment here"
            + (" (the colab stand-in reads secrets from there)" if how == "userdata.get" else "") + ".",
            "" if there else (f"The person exports {key} before the run (never written into the notebook); a plan lists "
                              f"it under `secrets: [{key}]`." if secret else f"Export {key} before the run."))
    for key, n in env_vars:
        if key not in os.environ and key not in {s[0] for s in secrets}:
            add("secrets", "info", n, f"cell {n} reads the environment variable {key} (not set here; it gets None).")
    meta_gpu = (nb.metadata.get("colab") or {}).get("gpuType") or nb.metadata.get("accelerator")
    gpu = "needs" if gpu_hints and not gpu_guard else "uses" if gpu_hints else None
    if gpu == "needs" and not has_gpu():
        add("gpu", "blocks", gpu_hints[0], f"cell {gpu_hints[0]} puts work on a CUDA GPU with no fallback, and this "
            "machine has no NVIDIA GPU.", "Prove it on a GPU backend (the plan below uses Modal's T4), or pick the "
            "device with torch.cuda.is_available() so it also runs on a CPU.")
    elif gpu == "uses" and not has_gpu():
        add("gpu", "info", gpu_hints[0], f"cell {gpu_hints[0]} uses a GPU when there is one; here it runs on the CPU, "
            "slower.")
    elif meta_gpu and not has_gpu():
        add("gpu", "info", None, f"it was saved from a Colab {meta_gpu} runtime; this machine has no NVIDIA GPU.")
    findings.sort(key=lambda f: (SEVERITY[f["severity"]], f["cell"] or 0))
    fields = {}
    for f in nb_fields(nb):                             # Colab form fields and papermill parameters, as params: sets
        fields[f["name"]] = None if f["name"] in fields else f["value"]
    lang_info = nb.metadata.get("language_info") or {}
    return {"cells": len(cells), "findings": findings, "packages": packages, "colab": covered, "defines": defines,
            "undefined": undefined, "fields": {k: v for k, v in fields.items() if v is not None},
            "secrets": sorted({k for k, _, how, safe in secrets if not safe and (how == "userdata.get" or SECRETISH.search(k))}),
            "uploads": uploads, "gpu": gpu,
            "writes": sorted(w for w in writes if w and not os.path.isabs(w) and _pathlike(w)), "unknown": unknown,
            "pip_lines": pip_lines, "counts": count, "probe": probe, "saved_python": lang_info.get("version"),
            "content_paths": bool(content)}


def cell_outputs(nb, n: int) -> list:
    code = [c for c in nb.cells if c.cell_type == "code"]
    return code[n - 1].get("outputs", []) if 0 < n <= len(code) else []


def _line(source: str, line: int) -> str:
    lines = source.splitlines()
    return lines[line - 1].strip()[:80] if 0 < line <= len(lines) else ""


PROBE = """import importlib.util as u, importlib.metadata as m, json, sys
try:
    dists = m.packages_distributions()
except Exception:
    dists = {}
out = {}
for name in sys.argv[1:]:
    try:
        found = u.find_spec(name) is not None
    except Exception:
        found = False
    version = None
    for d in (dists.get(name, []) if "." not in name else []):
        try:
            version = m.version(d)
            break
        except Exception:
            pass
    out[name] = {"installed": found, "version": version}
print(json.dumps(out))
"""


def kernel_python() -> str:
    """The Python the notebook's kernel runs: jupyter_client starts a `python` kernelspec with this Python."""
    try:
        from jupyter_client.kernelspec import KernelSpecManager
        exe = KernelSpecManager().get_kernel_spec("python3").argv[0]
        if os.path.isabs(exe) and os.path.exists(exe):
            return exe
    except Exception:
        pass
    return sys.executable


def _probe(names: list[str], workdir: Path) -> dict:
    """{import name: {installed, version}} as the kernel's Python sees them (from a neutral folder, so files beside
    the notebook do not count as installed packages)."""
    if not names:
        return {}
    try:
        out = subprocess.run([kernel_python(), "-c", PROBE, *names], capture_output=True, text=True, timeout=90,
                             cwd=str(Path(sys.executable).parent))
        return json.loads(out.stdout)
    except Exception:
        import importlib.util
        found = {}
        for n in names:
            try:
                found[n] = {"installed": importlib.util.find_spec(n) is not None, "version": None}
            except Exception:
                found[n] = {"installed": False, "version": None}
        return found


# ---- the run and what its failure means ---------------------------------------------------------------------

def _run(nb_path: Path, project: Path, workdir: Path, out_dir: Path, timeout: float) -> dict:
    """The notebook top to bottom in a fresh kernel, through the same step runner as a local plan's notebook step
    (colab stand-in, evidence, a hard time limit), with no params, uploads or secrets given."""
    from .executor import run_step
    step = {**copy.deepcopy(C.STEP_DEFAULTS), "name": "rescue", "kind": "notebook", "max_minutes": timeout / 60,
            "notebook": os.path.relpath(nb_path, workdir)}
    env = {**os.environ, "TESTBENCH_PROJECT_DIR": str(project)}
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    sys.stderr.flush()
    with open(out_dir / "kernel.log", "w") as log:          # the kernel's own stderr is evidence, not terminal noise
        try:
            saved = os.dup(2)
            os.dup2(log.fileno(), 2)
        except OSError:                                     # no stderr to redirect (a windowless process)
            saved = None
        try:
            return run_step(step, project=project, workdir=workdir, step_dir=out_dir, base_env=env)
        finally:
            if saved is not None:
                os.dup2(saved, 2)
                os.close(saved)


def classify(err: dict, info: dict, workdir: Path) -> tuple[str, str]:
    """(kind, what to do) for the error that stopped the run, in plain words."""
    ename, evalue = err.get("ename") or "", str(err.get("evalue") or "")
    text = f"{ename}: {evalue}"
    n = err.get("cell")
    saved = f" (it was saved with Python {info['saved_python']})" if info.get("saved_python") else ""
    if ename == "StepTimeout":
        fields = ", ".join(info["fields"])
        return "timeout", (f"cell {n} was still running when the time limit ran out. If it trains, downloads or loops "
                           "over data, make it do less: " + (f"set its parameters ({fields}) small in the plan's params:"
                                                              if fields else "fewer epochs or a sample of the data, as form "
                                                              "fields (NAME = value  #@param) a plan sets with params:")
                           + "; or give it longer: `testbench rescue NOTEBOOK --timeout 600`.")
    if re.search(r"StdinNotImplementedError|raw_input was called|input requests", text):
        return "input", (f"cell {n} asks for input(), and a headless run has nobody to answer. Make it a form field "
                         "(NAME = 'value'  #@param {type:'string'}), which a plan sets with params:.")
    if "files.upload() needs a person" in text:
        return "upload", (f"cell {n} waits for files.upload(). In a plan, give the step `uploads: [[path/to/file]]` "
                          "(one list per upload() call, in order); the plan below has the line to fill in.")
    m = re.search(r"Secret (\S+) does not exist", evalue)
    key = m.group(1) if m else evalue.strip("'") if ename == "KeyError" and evalue.strip("'") in info["secrets"] else None
    if key:
        return "secret", (f"cell {n} reads the secret {key}, which is not set here. The person exports {key} before the "
                          f"run (never written into the notebook); a plan lists it under `secrets: [{key}]`.")
    m = re.search(r"No module named '([^']+)'", evalue)
    if ename in ("ModuleNotFoundError", "ImportError") and m:
        mod = m.group(1)
        if mod.startswith("google.colab"):
            return "colab", (f"`{mod}` exists only on Colab, and testbench's stand-in has no {mod.split('.')[-1]}. Guard "
                             "the import (try/except ImportError) and skip what needs it elsewhere, or prove it on "
                             "`backend: colab`.")
        top = mod.split(".")[0]
        pip, sure = pip_name(mod)
        by = [p for p in info["pip_lines"] if _norm(p["name"]) in (_norm(pip), _norm(top))]
        what = f"`{top}` is not installed here: `pip install {pip}`" + (
            "" if sure else f" (a guess: `{top}`'s pip name may differ, check its docs)")
        if by and by[0]["cell"] > n:
            what += f". Cell {by[0]['cell']} installs it, but only after cell {n} imports it"
        elif by:
            what += (f". Cell {by[0]['cell']}'s pip install ran first, so it failed (no network, or no such version for "
                     "this Python: see its output in executed.ipynb) or went into another Python than the kernel's "
                     "(`%pip install` cannot)")
        return "missing_package", what + (". `--fix OUT.ipynb` adds a first %pip install cell for every missing package "
                                          "it is sure of." if sure else ".")
    m = re.search(r"cannot import name '([^']+)' from '([^']+)'", evalue) or \
        re.search(r"module '([^']+)' has no attribute '([^']+)'", evalue)
    if m and "partially initialized" not in evalue:
        name, mod = (m.group(1), m.group(2)) if "cannot import" in evalue else (m.group(2), m.group(1))
        if mod.startswith("google.colab"):
            return "colab", (f"google.colab.{name} exists only on Colab, and testbench's stand-in does not have it. Guard "
                             "the import (try/except ImportError) and skip what needs it elsewhere, or prove it on "
                             "`backend: colab`.")
        top = mod.split(".")[0]
        if top in sys.stdlib_module_names:
            return "api_changed", (f"this Python's {mod} has no `{name}`: a typo, or renamed or removed in a newer Python"
                                   f"{saved}. Change the code to the current name.")
        version = (info["probe"].get(top) or {}).get("version")
        return "api_changed", (f"the installed {top}{' ' + version if version else ''} has no `{name}`: renamed or removed "
                               f"after the notebook was written{saved}, or a typo. Pin the {pip_name(top)[0]} release it "
                               "was written for (an older one), or change the code to the new name.")
    if "partially initialized module" in evalue:
        mod = re.search(r"module '([^']+)'", evalue)
        name = mod.group(1).split(".")[0] if mod else "?"
        return "name_clash", (f"a file named {name}.py next to the notebook hides the real {name} package: rename it.")
    m = re.search(r"name '([^']+)' is not defined", evalue)
    if ename in ("NameError", "UnboundLocalError") and m:
        name = m.group(1)
        defs = info["defines"].get(name, [])
        later = [d for d in defs if d > (n or 0)]
        if later:
            return "order", (f"cell {n} uses `{name}`, which cell {later[0]} makes, further down. Move cell {later[0]} (or "
                             f"the lines that make `{name}`) above cell {n}; testbench never moves cells.")
        if name in ALIASES and not defs:
            return "undefined", (f"nothing in the notebook makes `{name}`: add `{ALIASES[name]}` to the first cell (the "
                                 "cell that imported it was deleted, most likely). `--fix OUT.ipynb` adds it.")
        if defs:
            return "undefined", (f"cell(s) {', '.join(map(str, defs))} make `{name}`, but did not this time (inside an if, "
                                 "a loop that did not run, or a try that failed). Look at why in executed.ipynb.")
        return "undefined", (f"nothing in the notebook makes `{name}`: it came from a cell that was deleted, or changed "
                             "after it ran (hidden state). Write the code that makes it in a cell above.")
    gpu_words = re.search(r"CUDA|cuda|NVIDIA|nvidia|cudnn|\bGPU\b", text)
    if gpu_words and re.search(r"out of memory", text, re.I):
        return "gpu", f"cell {n} ran out of GPU memory: make the batch or the model smaller, or prove it on a bigger GPU."
    if gpu_words and not has_gpu():
        return "gpu", (f"cell {n} needs an NVIDIA GPU, and this machine has none. Prove it on a GPU: the plan below uses "
                       "`backend: modal` with a T4 (check its cost first with `testbench estimate`), or `backend: colab`; "
                       "or pick the device with torch.cuda.is_available() so it also runs on a CPU.")
    if ename in ("FileNotFoundError", "IsADirectoryError") or "No such file or directory" in evalue \
            or re.search(r"File \S+ does not exist", evalue):
        m = re.search(r"No such file or directory: '([^']+)'", evalue) or re.search(r"File (\S+) does not exist", evalue) \
            or re.search(r"'([^']+)'", evalue)
        path = m.group(1) if m else None
        return "missing_file", _file_fix(path, n, info, workdir)
    if ename == "PermissionError" and "/content" in evalue:
        return "colab", (f"cell {n} writes under /content, Colab's folder, which this machine does not let it create. "
                         "Use paths relative to the notebook, or prove it on `backend: docker` (the project is at "
                         "/content there).")
    if re.search(r"magic function `?%?(\w+)`? not found|Cell magic `%%(\w+)` not found", text):
        magic = re.search(r"magic (?:function )?`?%*(\w+)", text).group(1)
        return "magic", (f"cell {n} uses %{magic}, which this IPython does not have"
                         + (" (only Colab ever had it): delete the line" if magic == "tensorflow_version"
                            else ": delete the line, or install what provides it") + ".")
    if ename in ("SyntaxError", "IndentationError", "TabError"):
        src = err.get("source_head") or ""
        py2 = re.search(r"^\s*print\s+[^\s(=]", src, re.M) or "Missing parentheses in call to 'print'" in evalue
        return "syntax", (f"cell {n} is not valid Python 3 ({evalue[:200]})"
                          + (": it is Python 2 code; port it (print(...), except E as e)" if py2 else "") + ".")
    if re.search(r"ConnectionError|URLError|HTTPError|Max retries exceeded|Name or service not known|Temporary failure "
                 r"in name resolution|getaddrinfo|SSLError|Connection refused", text):
        return "network", (f"cell {n} downloads something, and that failed ({evalue[:160]}): no network here, or the file "
                           "moved or needs a login. Download it by hand next to the notebook, point the cell at the "
                           "copy, and note where it came from.")
    if ename in ("DeadKernelError", "MemoryError") or "Kernel died" in text or re.search(r"out of memory", text, re.I):
        return "memory", (f"the kernel died in cell {n}: most often it ran out of memory (or native code crashed). Make it "
                          "use less (smaller data, batch or model, through its form fields) or prove it on a bigger "
                          "machine.")
    if not n:
        return "harness", (f"the notebook did not start: {ename}: {evalue[:300]}. `testbench doctor` checks that nbclient "
                           "and ipykernel are installed.")
    return "code", (f"the notebook's own code fails here ({ename}), not its setup. Read the traceback (executed.ipynb, "
                    f"log.txt) and work on it live: `testbench session start`, then `testbench exec --cells NOTEBOOK:1-{n}`.")


def _file_fix(path: str | None, n, info: dict, workdir: Path) -> str:
    if not path:
        return f"cell {n} reads a file that is not there; the traceback in executed.ipynb names it."
    if path.startswith("/content/drive"):
        return (f"cell {n} reads {path}, a file in its author's Google Drive. Here drive.mount() makes an empty local "
                "folder: copy the file to that path on this machine, or make the path a form field and point it at a "
                "local copy.")
    if path.startswith("/content"):
        return (f"cell {n} reads {path}: /content is Colab's folder. Put the file next to the notebook and read it with "
                "a relative path, or prove it on `backend: docker`, where the project is at /content.")
    if os.path.isabs(os.path.expanduser(path)):
        return (f"cell {n} reads {path}, a path on its author's machine. Put the file in the project and read it with a "
                "path relative to the notebook (or a form field).")
    before = [u for u in info["unknown"] if u[0] < (n or 0)]
    return (f"cell {n} reads {path}; the notebook expects it at {workdir / path} (relative to its folder), and it is not "
            "there. " + (f"Cell {before[-1][0]} (`{before[-1][1]}`) should have made it: check its output in "
                         "executed.ipynb." if before else "Nothing in the notebook makes it, so its author put it there "
                         "by hand: get it from where the notebook's text says, put it there, and keep it with the project "
                         "(or add a cell that downloads it)."))


# ---- the plan that proves it, and the fixed copy ---------------------------------------------------------------

WORK = re.compile(r"epoch|iter|step|round|trial|generation|repeat|runs", re.I)
DATA = re.compile(r"sample|example|rows|image|frame|item|subset|limit|n_?train|n_?test|num_?train|max_?(n|items|len)", re.I)


def small_params(fields: dict) -> dict:
    """Form fields with literal values, the ones that count work set small: epochs and steps to 1, samples to 100."""
    out = {}
    for name, text in fields.items():
        try:
            value = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            continue
        if isinstance(value, int) and not isinstance(value, bool) and value > 1:
            value = 1 if WORK.search(name) else min(value, 8) if "batch" in name.lower() else min(value, 100) \
                if DATA.search(name) else value
        out[name] = value
    return out


def plan_yaml(nb_rel: str, info: dict, diag: dict) -> tuple[str, str]:
    """(plan name, YAML to paste under plans:) for a run that proves the notebook once fixed."""
    stem = _slug(Path(nb_rel).stem)
    name = f"{stem}-runs"
    gpu = not has_gpu() and (info["gpu"] == "needs" or (diag.get("failure") or {}).get("kind") == "gpu")
    seconds = diag.get("seconds") or 0
    minutes = max(5, math.ceil(seconds * 3 / 60)) if diag["status"] == "passed" else 20
    lines = [f"  {name}:"]
    lines += ["    backend: modal                         # it needs an NVIDIA GPU, which this machine lacks; this costs",
              f"    gpu: T4                                #   money: `testbench estimate {name}` first (or backend: colab)"] \
        if gpu else ["    backend: local"]
    lines += [f"    description: {_q(nb_rel + ' runs top to bottom (suggested by testbench rescue)')}", "    steps:",
              f"      - name: {stem}", f"        notebook: {_q(nb_rel)}",
              f"        max_minutes: {minutes}" + ("" if diag["status"] == "passed" else "                    # FILL IN: what it really needs")]
    params = small_params(info["fields"])
    if params:
        lines.append("        params: {" + ", ".join(f"{_q(k)}: {json.dumps(v)}" for k, v in params.items()) + "}"
                     + "   # its parameters; the ones that count work set small")
    if info["secrets"]:
        lines.append(f"        secrets: [{', '.join(info['secrets'])}]          # the person exports them; never written here")
    if info["uploads"]:
        lines.append("        # uploads: [[FILL IN: the file files.upload() asks for]]   # one list per upload() call")
    if Path(nb_rel).parent != Path("."):
        lines.append(f"        # it runs with the project folder as its working directory, not {Path(nb_rel).parent.as_posix()}/")
    if info["content_paths"] and not gpu:
        lines.append("        # it uses /content paths: `backend: docker` puts the project at /content")
    lines += ["        expect:", "          no_errors: true                    # every cell runs"]
    if info["writes"]:
        lines.append("          files: [" + ", ".join(_q(f"**/{posixpath.basename(w)}") for w in info["writes"][:6])
                     + "]   # what it writes")
    lines.append('          # json: {"**/metrics.json": {accuracy: ">= 0.9"}}   # FILL IN: the number that means it works')
    return name, "\n".join(lines) + "\n"


PIP_BANG = r"^(\s*)!\s*(?:pip3?|python3? -m pip)\s+install\b"


def _same(a: Path, b: Path) -> bool:
    """One file under both names: the same path, or another name for it (a hard link, or the name in another case
    where the file system ignores case, as macOS and Windows do)."""
    try:
        return Path(a).resolve() == Path(b).resolve() or (Path(a).exists() and os.path.samefile(a, b))
    except OSError:
        return False


def write_fix(nb_path: Path, out: Path, info: dict, failure: dict | None) -> list[str]:
    """A copy with mechanical fixes only: `%pip install` for `!pip install`, and a first cell that installs the missing
    packages rescue is sure of and adds standard imports nothing in the notebook makes. Nothing is moved. A
    percent-format .py notebook's copy can be a .py (the fixes made on its text) or an .ipynb."""
    if _same(out, nb_path):
        raise RescueError(f"--fix {out} is the notebook itself; rescue never writes the original. Give another path.")
    percent = out.suffix.lower() == ".py"
    if percent and nb_path.suffix.lower() != ".py":
        raise RescueError(f"--fix {out}: {nb_path.name} is an .ipynb, and so is its fixed copy; give a path that ends "
                          "in .ipynb")
    nb = NB.read(nb_path)
    changes = []
    for n, cell in enumerate((c for c in nb.cells if c.cell_type == "code"), 1):
        new = re.sub(PIP_BANG, r"\1%pip install", cell.source, flags=re.M)
        if new != cell.source:
            cell.source = new
            changes.append(f"cell {n}: `!pip install` became `%pip install`, which installs into the kernel's own Python")
    missing = [p for p in info["packages"] if not p["installed"] and p["sure"] and not p["guarded"]
               and not (p["notebook_installs"] and p["notebook_installs"] <= p["cells"][0])]
    if failure and failure.get("kind") == "missing_package":
        mod = (re.search(r"No module named '([^']+)'", failure.get("evalue") or "") or [None, None])[1]
        pip, sure = pip_name(mod) if mod else (None, False)
        if sure and pip and pip not in [p["pip"] for p in missing]:
            missing.append({"pip": pip, "import": mod.split(".")[0]})
    pips = list(dict.fromkeys(p["pip"] for p in missing))
    imports = [ALIASES[u] for u in info["undefined"] if u in ALIASES
               and (ALIASES[u].split()[1].split(".")[0] in sys.stdlib_module_names
                    or (info["probe"].get(ALIASES[u].split()[1].split(".")[0]) or {}).get("installed")
                    or pip_name(ALIASES[u].split()[1])[0] in pips)]
    lines = []
    if pips or imports:
        lines = ["# added by testbench rescue: what the notebook needs before its first cell"]
        if pips:
            lines += ["# pin the versions that work once it runs (pkg==x.y.z)", "%pip install -q " + " ".join(pips)]
        lines += imports
        first = next((i for i, c in enumerate(nb.cells) if c.cell_type == "code"), len(nb.cells))
        nb.cells.insert(first, nbformat.v4.new_code_cell("\n".join(lines)))
        what = (["installs " + ", ".join(pips)] if pips else []) + (["adds " + "; ".join(f"`{i}`" for i in imports)] if imports else [])
        changes.append(f"a new first code cell {' and '.join(what)}; the code cells after it are numbered one higher")
    out.parent.mkdir(parents=True, exist_ok=True)
    if percent:
        _write_percent_fix(nb_path, out, lines, [c.source for c in nb.cells if c.cell_type == "code"])
    else:
        nbformat.write(nb, str(out))
    return changes


def _write_percent_fix(src: Path, out: Path, first: list[str], want: list[str]) -> None:
    """The percent-format `src` with the fixes made on its text, so everything else in it (its header, comments,
    titles, blank lines) stays as it was: `%pip install` for `!pip install` in its code cells, and `first` (lines)
    as a new cell before the first code cell, its magics commented out as jupytext writes them. `want`: the code
    cells' sources the copy must read back as; if it does not, nothing is left at `out`."""
    lines = src.read_text(encoding="utf-8").splitlines(keepends=True)
    i = 0                                                # the header, as NB.from_percent skips it
    while i < len(lines) and (lines[i].startswith("#!") or re.match(r"#.*coding[:=]", lines[i])):
        i += 1
    if i < len(lines) and lines[i].rstrip() == "# ---":
        i = next((j + 1 for j in range(i + 1, len(lines)) if lines[j].rstrip() == "# ---"), i)
    chunks, options, start, quote = [], None, i, None
    for k in range(i, len(lines)):
        mark = None if quote else NB.CELL_MARK.match(lines[k].rstrip("\r\n"))
        if mark:
            chunks.append((options, start, k))
            options, start = mark.group(2) or "", k
            continue
        quote = NB._open_string(lines[k], quote)
    chunks.append((options, start, len(lines)))
    code = [(o, s, e) for o, s, e in chunks if (o is not None or "".join(lines[s:e]).strip())
            and NB._cell_options(o or "")[0] == "code"]
    for o, s, e in code:
        for k in range(s + (o is not None), e):
            lines[k] = re.sub(r"^(\s*(?:#\s?)?)!\s*(?:pip3?|python3? -m pip)\s+install\b", r"\1%pip install", lines[k])
    if first:
        block = ["# %%\n"] + [("# " + ln if ln.startswith("%") else ln) + "\n" for ln in first] + ["\n"]
        at = code[0][1] if code else len(lines)
        if code and code[0][0] is None:
            block.append("# %%\n")                       # the code above the first marker becomes a cell of its own
        if at and not lines[at - 1].endswith("\n"):
            lines[at - 1] += "\n"
        lines[at:at] = block
    out.write_text("".join(lines), encoding="utf-8")
    try:
        got = [c.source for c in NB.read(out).cells if c.cell_type == "code"]
    except Exception:
        got = None
    if got != want:
        out.unlink(missing_ok=True)
        raise RescueError(f"--fix {out}: the fixes could not be made on {src.name}'s text without changing how its "
                          "cells read, so nothing was written; give a path that ends in .ipynb for the fixed copy")


# ---- putting it together --------------------------------------------------------------------------------------

def _show(path: Path) -> str:
    try:
        return path.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


def rescue(notebook: str | Path, *, timeout: float = 120, fix: str | Path | None = None, run: bool = True) -> dict:
    """Read the notebook, run it (unless run=False), write the diagnosis; returns it as a dict."""
    nb_path = Path(notebook).expanduser().resolve()
    if not nb_path.is_file():
        raise RescueError(f"{notebook} does not exist")
    if nb_path.suffix.lower() not in (".ipynb", ".py"):
        raise RescueError(f"{notebook} is not a notebook: rescue reads an .ipynb, or a percent-format .py (`# %%` cells)")
    if NB.plain_python(nb_path):
        raise RescueError(NB.plain_python(nb_path))
    if fix and _same(Path(fix).expanduser(), nb_path):
        raise RescueError(f"--fix {fix} is the notebook itself; rescue never writes the original. Give another path.")
    workdir = nb_path.parent
    try:
        project = C.find_root(workdir)
    except C.ConfigError:
        project = workdir
    info = read(nb_path, workdir)
    from .runs import _gitignore
    (project / ".testbench").mkdir(exist_ok=True)
    _gitignore(project)
    base = project / ".testbench" / "rescue" / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{_slug(nb_path.stem)}"
    out_dir, k = base, 1
    while out_dir.exists():
        k += 1
        out_dir = base.with_name(f"{base.name}-{k}")
    out_dir.mkdir(parents=True)
    diag = {"notebook": _show(nb_path), "project": str(project), "workdir": str(workdir), "dir": _show(out_dir),
            "status": "not run", "seconds": None, "timeout_seconds": timeout, "code_cells": info["cells"],
            "cells_ran": 0, "failure": None, "findings": info["findings"], "packages": info["packages"],
            "colab": info["colab"], "saved_with_python": info["saved_python"],
            "machine": {"python": sys.version.split()[0], "kernel_python": kernel_python(), "gpu": has_gpu()}}
    if run:
        began = time.time()
        res = _run(nb_path, project, workdir, out_dir, timeout)
        diag["seconds"] = round(time.time() - began, 1)
        diag["status"] = res["status"] if res["status"] in ("passed", "failed", "timeout") else "error"
        diag["cells_ran"] = sum(1 for c in res.get("cells", []) if c.get("status") == "ok")
        if res.get("error"):
            err = dict(res["error"])
            inner = re.findall(r"^(\w*(?:Error|Exception)):\s*(.*)$", str(err.get("evalue") or ""), re.M)
            if err.get("ename") == "CellExecutionError" and inner:      # no error output (a UsageError): the text names it
                err["ename"], err["evalue"] = inner[-1]
            kind, what = classify(err, info, workdir)
            what = what.replace("NOTEBOOK", diag["notebook"])
            diag["failure"] = {"cell": err.get("cell"), "ename": err.get("ename"), "evalue": str(err.get("evalue") or "")[:1000],
                               "line": err.get("line"), "source_head": err.get("source_head"), "kind": kind, "fix": what}
        diag["evidence"] = sorted(p.name for p in out_dir.iterdir() if p.is_file())
    if fix:
        out = Path(fix).expanduser().resolve()
        diag["fix"] = {"path": _show(out), "changes": write_fix(nb_path, out, info, diag["failure"]),
                       "left": sum(1 for f in info["findings"] if f["severity"] == "blocks" and f["kind"] != "packages"
                                   and "--fix" not in f["fix"])}
    proved = Path(fix).expanduser().resolve() if fix else nb_path
    rel = os.path.relpath(proved, project) if proved.is_relative_to(project) else os.path.relpath(nb_path, project)
    name, text = plan_yaml(Path(rel).as_posix(), info, diag)
    diag["plan"] = {"name": name, "yaml": text}
    (out_dir / "plan.yaml").write_text("plans:\n" + text)
    (out_dir / "diagnosis.json").write_text(json.dumps(diag, indent=1, default=str))
    (out_dir / "diagnosis.md").write_text(render(diag))
    return diag


def render(d: dict) -> str:
    nb, status, f = d["notebook"], d["status"], d.get("failure")
    cell = f"cell {f['cell']}" if f and f.get("cell") else "a cell"
    head = {"passed": f"runs top to bottom here ({C.counted(d['code_cells'], 'code cell')}, {d['seconds']} s)",
            "failed": f"fails at {cell} of {d['code_cells']}",
            "timeout": f"was still running {cell} at the time limit ({d['timeout_seconds']:g} s)",
            "error": "did not start", "not run": "read without running it"}[status]
    lines = [f"# Rescue: `{nb}` {head}", ""]
    if f:
        lines += [f"**cell {f['cell']}**: `{f['ename']}`: {f['evalue'][:300]}" if f.get("cell") else f"`{f['ename']}`: {f['evalue'][:300]}"]
        if f.get("line"):
            lines += [f"at {f['line']}"]
        lines += ["", f"**What to do** ({f['kind'].replace('_', ' ')}): {f['fix']}", ""]
    blocks = [x for x in d["findings"] if x["severity"] == "blocks"]
    after = [x for x in blocks if (x["cell"] or 0) > ((f or {}).get("cell") or 0)]
    if status == "passed":
        lines += ["It ran in a fresh kernel on this machine without an error." + (
            f" Reading it found {len(blocks)} thing(s) that would stop it elsewhere or with other inputs (below)."
            if blocks else ""), ""]
    elif status != "not run":
        stopped = {"failed": f"then {cell} failed", "timeout": f"then the time ran out in {cell}"}.get(status, "then it stopped")
        lines += [f"Ran it from the top in a fresh kernel on this machine: {d['cells_ran']} cell(s) ran, {stopped} "
                  f"({d['seconds']} s, limit {d['timeout_seconds']:g} s)." + (
                      f" Reading it found {len(after)} more thing(s) further down that will stop it next (below)."
                      if after else ""), ""]
    kept = [e for e in ("executed.ipynb", "log.txt") if e in d.get("evidence", [])]
    lines += [f"{'Evidence' if kept else 'Saved'} in {d['dir']}/ "
              f"({', '.join(kept + ['diagnosis.md', 'diagnosis.json', 'plan.yaml'])})", ""]
    if d["findings"]:
        lines += [f"## Found by reading it ({len(d['findings'])})", ""]
        for x in d["findings"]:
            where = f"cell {x['cell']}" if x["cell"] else "notebook"
            lines.append(f"- **{x['severity']}** {where}, {x['kind'].replace('_', ' ')}: {x['message']}"
                         + (f" **Do**: {x['fix']}" if x["fix"] else ""))
        lines.append("")
    if d["packages"]:
        lines += ["## Packages it imports", "", "| import | pip install | here | cells |", "|---|---|---|---|"]
        for p in d["packages"]:
            here = (p["version"] or "installed") if p["installed"] else ("notebook installs it" if p["notebook_installs"]
                                                                        else "**missing**")
            lines.append(f"| `{p['import']}` | {p['pip']}{'' if p['sure'] else ' (guess)'} | {here} | "
                         f"{', '.join(map(str, p['cells']))} |")
        lines.append("")
    if d["colab"]:
        covered = "covered by testbench's stand-in"
        lines += ["## Colab parts", ""]
        lines += [f"- `{c['part']}` (cell {', '.join(map(str, c['cells']))}): "
                  f"{covered if c['covered'] else 'not covered'} - {c['how']}" for c in d["colab"]]
        lines.append("")
    if d.get("fix"):
        fx = d["fix"]
        lines += ["## Fixed copy", "", f"Wrote {fx['path']} (the original is unchanged):", ""]
        lines += [f"- {c}" for c in fx["changes"]] or ["- nothing mechanical to fix; it is an identical copy"]
        if fx["left"]:
            lines += [f"- left for you: {fx['left']} thing(s) above that are not mechanical (cells are never moved)"]
        lines += ["", f"Then `testbench rescue {fx['path']}` shows what is next.", ""]
    p = d["plan"]
    lines += ["## A plan that proves it once fixed", "", "```yaml", p["yaml"].rstrip("\n"), "```", "",
              f"Paste it under `plans:` in testbench.yaml (`testbench init` writes one), fill in the FILL IN lines, then "
              f"`testbench run {p['name']}`.", "",
              "Cells are code cells counted from 1, as in `testbench lint` and a plan's `cells:`."]
    return "\n".join(lines) + "\n"


def main(a) -> int:
    if not a.json and not a.no_run:
        print(f"[testbench] reading {a.notebook}, then running it top to bottom in a fresh kernel here "
              f"(up to {a.timeout:g} s) ...", flush=True)
    try:
        d = rescue(a.notebook, timeout=a.timeout, fix=a.fix, run=not a.no_run)
    except RescueError as exc:
        print(f"testbench: {exc}")
        return 2
    print(json.dumps(d, indent=1, default=str) if a.json else "\n" + render(d))
    if d["status"] == "not run":
        return 1 if any(x["severity"] == "blocks" for x in d["findings"]) else 0
    return 0 if d["status"] == "passed" else 1
