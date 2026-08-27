"""Identifying screenshots for a disc.

One code path for both sources: a disc is reduced to byte *segments*
(path, base_offset, length) — plain VOB files in the decrypted mirror during
a rip, or VOB extents inside an ISO (located via the vendored IsoNav) for
backfill. MPEG program streams are self-synchronizing, so seeking to any
2048-aligned offset and piping bytes into `ffmpeg -f mpeg -i pipe:0` decodes.

Output: BASE.menu.jpg (menu VOB @5%) + BASE.title1..3.jpg (main title VOB set
@15/40/65%).
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor"))
from dvd_vm_ref import IsoNav  # noqa: E402

TITLE_FRACTIONS = (0.15, 0.40, 0.65)
MENU_FRACTION = 0.05
_FEED_CAP = 64 << 20  # stop feeding ffmpeg after 64 MiB even if no frame found


def grab_frame(path, offset, out_jpg, timeout=60):
    """Decode one frame from an MPEG-PS byte stream starting at offset."""
    offset -= offset % 2048
    try:
        f = open(path, "rb")
    except OSError:
        return False
    with f:
        f.seek(offset)
        proc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-f", "mpeg", "-i", "pipe:0",
             "-frames:v", "1", "-q:v", "4", "-vf", "scale=640:-2",
             "-y", out_jpg],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        fed = 0
        try:
            while fed < _FEED_CAP:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                proc.stdin.write(chunk)
                fed += len(chunk)
        except (BrokenPipeError, OSError):
            pass  # ffmpeg got its frame and exited
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    ok = os.path.isfile(out_jpg) and os.path.getsize(out_jpg) > 0
    if not ok and os.path.isfile(out_jpg):
        os.unlink(out_jpg)
    return ok


def _capture(segments, fraction, out_jpg):
    """segments = ordered [(path, base_offset, length)]; fraction of the total."""
    total = sum(s[2] for s in segments)
    if total == 0:
        return False
    target = int(total * fraction)
    for path, base, length in segments:
        if target < length:
            return grab_frame(path, base + target, out_jpg)
        target -= length
    return False


def _capture_set(menu_segs, title_segs, out_dir, base_name, should_abort=None):
    """should_abort: optional predicate checked between grabs, so a user abort
    isn't held up for the length of a whole (worst case, timing-out) set."""
    made = []
    if menu_segs and not (should_abort and should_abort()):
        out = os.path.join(out_dir, base_name + ".menu.jpg")
        if _capture(menu_segs, MENU_FRACTION, out):
            made.append(os.path.basename(out))
    for i, frac in enumerate(TITLE_FRACTIONS, 1):
        if should_abort and should_abort():
            break
        out = os.path.join(out_dir, "%s.title%d.jpg" % (base_name, i))
        if _capture(title_segs, frac, out):
            made.append(os.path.basename(out))
    return made


def capture_from_mirror(video_ts_dir, out_dir, base_name, should_abort=None):
    """During a rip: VOB files in the decrypted VIDEO_TS mirror directory."""
    try:
        names = sorted(os.listdir(video_ts_dir))
    except OSError:
        return []

    def segs(filenames):
        out = []
        for n in filenames:
            p = os.path.join(video_ts_dir, n)
            try:
                out.append((p, 0, os.path.getsize(p)))
            except OSError:
                pass
        return out

    menu_files = [n for n in names if n.upper() == "VIDEO_TS.VOB"]
    if not menu_files:
        m0 = [n for n in names if re.match(r"VTS_\d\d_0\.VOB$", n.upper())]
        if m0:
            menu_files = [max(m0, key=lambda n: os.path.getsize(
                os.path.join(video_ts_dir, n)))]

    groups = {}
    for n in names:
        m = re.match(r"VTS_(\d\d)_([1-9])\.VOB$", n.upper())
        if m:
            groups.setdefault(m.group(1), []).append(n)
    title_files = []
    if groups:
        best = max(groups.values(), key=lambda fs: sum(
            os.path.getsize(os.path.join(video_ts_dir, f)) for f in fs))
        title_files = sorted(best)

    return _capture_set(segs(menu_files), segs(title_files), out_dir, base_name,
                        should_abort=should_abort)


def capture_from_iso(iso_path, out_dir, base_name):
    """Backfill: VOB extents located inside the ISO by IsoNav."""
    try:
        nav = IsoNav(iso_path)
    except Exception:
        return []
    menu_segs = []
    if nav.best_menu_vts:
        lba, length = nav.menu_vob[nav.best_menu_vts]
        menu_segs = [(iso_path, lba * 2048, length)]
    title_segs = []
    if nav.best_vts:
        title_segs = [(iso_path, lba * 2048, length)
                      for lba, length in nav.groups[nav.best_vts]]
    nav.f.close()
    return _capture_set(menu_segs, title_segs, out_dir, base_name)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python3 -m ripper.screenshots <disc.iso> <out_dir>")
    iso, out_dir = sys.argv[1], sys.argv[2]
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(iso))[0]
    made = capture_from_iso(iso, out_dir, base)
    print("captured:", made if made else "nothing")
