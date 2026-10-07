"""Google Drive cannot be mounted without a browser sign-in; the mount point becomes an ordinary folder."""
import os


def mount(mountpoint="/content/drive", force_remount=False, timeout_ms=120000, readonly=False):
    os.makedirs(os.path.join(mountpoint, "MyDrive"), exist_ok=True)
    print(f"[testbench] drive.mount({mountpoint!r}) skipped headless: {mountpoint}/MyDrive is a local folder", flush=True)


def flush_and_unmount(timeout_ms=None):
    pass
