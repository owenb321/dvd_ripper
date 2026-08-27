"""Post-rip verification: is the ISO actually decrypted?

The whole point of this appliance is producing ISOs a player can read, and the
failure mode that matters most is SILENT. libdvdcss can be absent, half-built,
or purged by an apt hook (see the Dockerfile's install notes -- every one of
those has produced a CSS-less image here before), and when it happens
dvdbackup still exits 0 and still writes a full-size, perfectly well-formed
ISO. Nothing in the rip complains. The disc simply does not play: the
MiSTer_DVD core green-screens the video, shows CSS ENCRYPTED and mutes the
audio, hours later and on different hardware.

So the rip checks its own work. vendor/css_scan.py samples the image for packs
whose PES_scrambling_control is non-zero, using exactly the test the core's
ps_demux applies on-board -- so a clean verdict here means the core will not
raise that flag either.

Three outcomes, and the distinction between the last two matters:
  clean=True   sampled packs, none scrambled
  clean=False  scrambled packs found -- the rip is bad, say so loudly
  clean=None   nothing to judge (no MPEG packs found, or the scan failed).
               UNKNOWN is not CLEAN, and must never be reported as success.
"""
import os
import sys

# Sampled mode reads ~20k sectors (~40 MB) spread across the image: seconds,
# against a rip of many minutes. A full scan is minutes on an 8 GB image and
# buys nothing here -- a rip that loses its keys loses them for ~19% of packs
# (the FAIRYTOPIA case), not for three sectors in the middle.
FULL_SCAN = False


def verify_css(iso_path, vendor_dir):
    """Returns a verdict dict; never raises."""
    out = {
        "scanned": False, "clean": None, "scrambled_pct": None,
        "packs_checked": 0, "scrambled": 0, "sectors": 0, "stride": 0,
        "first_hits": [], "error": None,
    }
    try:
        if vendor_dir and vendor_dir not in sys.path:
            sys.path.insert(0, vendor_dir)
        import css_scan

        nsec, stride, packs, checked, scrambled, hits = \
            css_scan.scan(iso_path, full=FULL_SCAN)
        out.update(sectors=nsec, stride=stride, packs_checked=checked,
                   scrambled=scrambled,
                   first_hits=[{"sector": s, "stream_id": sid}
                               for s, sid in hits])
        if checked == 0:
            # No PES packets to test. Could be a non-DVD-Video image or a
            # pathological rip; either way there is no evidence of cleanliness.
            out["error"] = ("no MPEG packs found in %d sectors -- "
                            "not a DVD-Video image?" % nsec)
            return out
        out["scanned"] = True
        out["clean"] = scrambled == 0
        out["scrambled_pct"] = round(100.0 * scrambled / checked, 2)
    except Exception as e:                       # noqa: BLE001 - never fatal
        out["error"] = "css scan failed: %r" % (e,)
    return out


def summary(css):
    """One-line human summary, or None if there is nothing worth saying."""
    if not css:
        return None
    if css.get("clean") is True:
        return None                              # success is the quiet case
    if css.get("clean") is False:
        return ("CSS-encrypted: %.1f%% of %d sampled packs are scrambled -- "
                "this ISO will not play (check libdvdcss)"
                % (css["scrambled_pct"], css["packs_checked"]))
    return "decryption unverified: %s" % (css.get("error") or "scan did not run")
