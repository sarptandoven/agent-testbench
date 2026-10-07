"""files.upload() takes the next group of files from the step's `uploads:`; files.download() keeps a copy as
evidence instead of sending it to a browser."""
import json, os, shutil
from pathlib import Path


def upload(target_dir=None):
    queue_path = os.environ.get("TESTBENCH_UPLOAD_QUEUE")
    queue = json.loads(Path(queue_path).read_text()) if queue_path and Path(queue_path).exists() else []
    if not queue:
        raise RuntimeError("files.upload() needs a person to choose files. In testbench.yaml give this step "
                           "`uploads: [[path/to/file]]` - one list per upload() call, in order.")
    chosen = queue.pop(0)
    Path(queue_path).write_text(json.dumps(queue))
    where = Path(target_dir) if target_dir else Path.cwd()
    where.mkdir(parents=True, exist_ok=True)
    got = {}
    for path in chosen:
        src = Path(path)
        if not src.is_file():
            raise FileNotFoundError(f"upload queued {path}, which does not exist")
        dst = where / src.name
        if src.resolve() != dst.resolve():
            shutil.copyfile(src, dst)
        got[dst.name] = dst.read_bytes()
        print(f"[testbench] uploaded {dst.name} ({dst.stat().st_size / 1e6:.1f} MB)", flush=True)
    return got


def download(filename):
    src = Path(filename)
    if not src.is_file():
        raise FileNotFoundError(f"Cannot find file: {filename}")
    keep = Path(os.environ.get("TESTBENCH_DOWNLOADS_DIR", "testbench_downloads"))
    keep.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, keep / src.name)
    log = keep / "downloads.json"
    entries = json.loads(log.read_text()) if log.exists() else []
    entries.append({"path": str(src.resolve()), "bytes": src.stat().st_size})
    log.write_text(json.dumps(entries, indent=1))
    print(f"[testbench] download offered: {src.name} ({src.stat().st_size / 1e6:.1f} MB), kept as evidence", flush=True)


def view(filename):
    print(f"[testbench] files.view({filename}) has no browser here; skipped", flush=True)
