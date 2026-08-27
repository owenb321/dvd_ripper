#!/usr/bin/env python3
"""Run the vendored MiSTer_DVD census on one ISO and dump a single JSON object.

Usage: census_run.py <disc.iso> <out.json> [--es]

Exit codes: 0 = ok, 2 = not ISO9660 / parse failure (message on stderr).
Run as a subprocess so a pathological ISO can't hang or crash the supervisor.

--es adds the two ELEMENTARY-STREAM scans. Everything dvd_census.py reports
comes from the IFOs, and the disc properties that actually predict how a
player copes with a disc are not in the IFOs at all:

  * line-21 closed captions live in MPEG-2 user_data inside the video ES
    (cc_scan.py), so libdvdread-shaped tools cannot see them;
  * picture coding -- field-coded vs frame-coded, and the 3:2 cadence --
    lives in the picture coding extension (video_cadence_census.py).

Both open the VOBs and sample deep windows across the title rather than the
head, because a disc's lead-ins routinely disagree with its body. They cost a
few seconds each; a rip costs many minutes, so the scan rides along for free.

An ES scan failing must never cost us the IFO census, so each is wrapped
independently and records its own error field.
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dvd_census import census_iso, _derived  # noqa: E402


# A disc is not one cadence. Thayer's Quest is the worked example: its LARGEST
# VTS (9) is frame-coded, while VTS 2/5/7/8 are 100% field-coded and VTS 11 is
# 40%. video_cadence_census defaults to nav.best_vts, so scanning only the
# largest VTS reports that disc as 0% field-coded -- the exact opposite of the
# property that broke frame-granular drop handling in the core. The tool's own
# header warns about sampling heads instead of the body; VTS SELECTION is the
# same trap on a second axis, and a game or TV box set is where it bites.
#
# So: full-resolution scan of the main VTS (its verdict is "what is this
# disc"), plus a cheaper sweep of the other large ones purely to answer "does
# any content path on this disc use field coding".
MAIN_WINDOWS, MAIN_SECTORS = 12, 1200
EXTRA_WINDOWS, EXTRA_SECTORS = 6, 600
MAX_VTS = 8
# A VTS well below half field-coded still sends the core down the field path
# (Thayer's VTS 11 measures ~40%), so flag on "clearly present", not "dominant".
# Real discs read 0.0-0.5% from noise, so 10% is comfortably above the floor.
FIELD_VTS_PCT = 10.0


def _cadence(path, f):
    """Merge video_cadence_census results into the feature dict, in place."""
    f["cadence_scanned"] = False
    f["cadence_error"] = None
    try:
        import video_cadence_census as vcc
        from dvd_vm_ref import IsoNav

        nav = IsoNav(path)
        sizes = {v: sum(dl for _, dl in g) for v, g in nav.groups.items()}
        if not sizes:
            f["cadence_error"] = "no title VOBs"
            return
        main = nav.best_vts if nav.best_vts in sizes else \
            max(sizes, key=lambda v: sizes[v])
        order = [main] + [v for v in sorted(sizes, key=lambda v: -sizes[v])
                          if v != main][:MAX_VTS - 1]

        totals = {"n": 0, "prog": 0, "rff": 0, "field_pic": 0}
        field_vts, scanned, main_acc = [], 0, None
        for vts in order:
            win, sec = ((MAIN_WINDOWS, MAIN_SECTORS) if vts == main
                        else (EXTRA_WINDOWS, EXTRA_SECTORS))
            res, err = vcc.census_iso(path, vts_sel=vts,
                                      windows=win, win_sectors=sec)
            if err or not res:
                continue
            _v, _total, acc, _per_window = res
            n = acc.get("n") or 0
            if not n:
                continue
            scanned += 1
            if vts == main:
                main_acc = acc
            for key in totals:
                totals[key] += acc[key]
            if 100.0 * acc["field_pic"] / n > FIELD_VTS_PCT:
                field_vts.append(vts)

        if not main_acc and not scanned:
            f["cadence_error"] = "sampled no pictures"
            return

        # The headline verdict describes the MAIN title, because that is the
        # question "what is this disc" actually asks.
        label, why = vcc.verdict(main_acc or totals)
        n = totals["n"] or 1
        f["cadence_scanned"] = True
        f["cadence_verdict"] = label
        f["cadence_why"] = why
        f["cadence_main_vts"] = main
        f["cadence_vts_scanned"] = scanned
        # Never let a cap read as full coverage.
        f["cadence_vts_total"] = len(sizes)
        f["pic_sampled"] = totals["n"]
        # Percentages, not raw counts: the sample size is a tool parameter and
        # would otherwise leak into a value meant to be compared across discs.
        f["pic_progressive_pct"] = round(100.0 * totals["prog"] / n, 1)
        f["pic_field_pct"] = round(100.0 * totals["field_pic"] / n, 1)
        f["pic_rff_pct"] = round(100.0 * totals["rff"] / n, 1)
        # ANY field-coded VTS matters, not the disc-wide average: the core hits
        # the field path as soon as playback reaches that content.
        f["field_coded_vts"] = sorted(field_vts)
        f["field_coded"] = bool(field_vts)
    except Exception as e:                       # noqa: BLE001 - never fatal
        f["cadence_error"] = "cadence scan failed: %r" % (e,)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    es = "--es" in sys.argv[1:]
    if len(args) != 2:
        sys.exit("usage: census_run.py <disc.iso> <out.json> [--es]")
    iso, out = args
    try:
        features = _derived(census_iso(iso, captions=es))
    except AssertionError:
        sys.stderr.write("not ISO9660 (UDF-only image?)\n")
        sys.exit(2)
    except TypeError:
        # Older vendored dvd_census.py with no captions parameter: the IFO
        # census still matters, so fall back rather than fail the sidecar.
        features = _derived(census_iso(iso))
        features["cc_error"] = "vendored dvd_census.py predates --captions"
    except Exception as e:
        sys.stderr.write("census failed: %r\n" % (e,))
        sys.exit(2)
    if es:
        _cadence(iso, features)
    with open(out, "w") as fh:
        json.dump(features, fh, indent=1, sort_keys=True)
        fh.write("\n")


if __name__ == "__main__":
    main()
