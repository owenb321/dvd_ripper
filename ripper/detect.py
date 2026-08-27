"""Drive/media detection helpers.

media_present() reads the host's udev database (bind-mounted /run/udev in
Docker) instead of opening the device: some drive firmwares treat a bare
open() as a tray-close cue, which can yank a disc in misaligned mid-insert.
Only fall back to a dd probe when no udev DB is visible.
"""
import os
import re
import subprocess

_udev_db_available = None


def udev_db_available():
    global _udev_db_available
    if _udev_db_available is None:
        _udev_db_available = os.path.isdir("/run/udev/data")
    return _udev_db_available


def media_present(dev):
    if udev_db_available():
        try:
            out = subprocess.run(
                ["udevadm", "info", "--query=property", "--name", dev],
                capture_output=True, text=True, timeout=10).stdout
        except (subprocess.TimeoutExpired, OSError):
            return False
        return "ID_CDROM_MEDIA_DVD=1" in out.splitlines()
    # fallback: try reading one sector (may trigger tray-close on some firmware)
    try:
        rc = subprocess.run(
            ["dd", "if=" + dev, "of=/dev/null", "bs=2048", "count=1"],
            capture_output=True, timeout=15).returncode
    except (subprocess.TimeoutExpired, OSError):
        return False
    return rc == 0


def read_label(dev):
    try:
        out = subprocess.run(["blkid", "-o", "value", "-s", "LABEL", dev],
                             capture_output=True, text=True, timeout=15).stdout
    except (subprocess.TimeoutExpired, OSError):
        out = ""
    return sanitize_label(out.strip())


def sanitize_label(label):
    label = label.replace(" ", "_")
    label = re.sub(r"[^A-Za-z0-9_-]", "", label)
    return label or "disc"


def disc_size(dev):
    """Media size in bytes (isosize; 0 if unknown)."""
    try:
        out = subprocess.run(["isosize", dev], capture_output=True, text=True,
                             timeout=15).stdout.strip()
        return int(out)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return 0


def eject(dev):
    try:
        return subprocess.run(["eject", dev], capture_output=True,
                              timeout=30).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def free_bytes(path):
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    except OSError:
        return 0


def dir_bytes(path):
    """Recursive byte sum of a directory tree (progress sampling)."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total
