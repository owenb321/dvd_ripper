# Vendored MiSTer_DVD census tools

`dvd_census.py`, `dvd_vm_ref.py`, `iso_nav_check.py`, `nav_extract.py`,
`cc_scan.py`, `video_cadence_census.py`, and `css_scan.py` are **verbatim
copies** from the MiSTer_DVD project's `tools/` directory
(https://github.com/owenb321/MiSTer_DVD).

**Do not edit them here** — fix upstream and re-copy. They are pure-stdlib
Python 3 and only import each other (sibling `sys.path` imports), so copying
the seven files together is sufficient. `css_scan.py` imports nothing at all
and is used by `ripper/verify.py` rather than by the census.

`cc_scan.py` (line-21 captions) and `video_cadence_census.py` (picture coding
and 3:2 cadence) open the video elementary stream. They report the disc
properties that are **not in the IFOs at all**, so no libdvdread-shaped tool
can see them; `dvd_census.py --captions` calls into the former.

`census_run.py` is the only file owned by this repo: a wrapper that runs
`census_iso()` + `_derived()` on a single ISO and writes one JSON object,
invoked as a subprocess by `ripper/census.py`. With `--es` it also runs the
two ES scans and merges their results in. Its one piece of real logic is
sweeping cadence across **several VTSs** rather than just the largest one —
see the comment above `_cadence()` for the disc that makes that necessary.

The ripper also imports `IsoNav` from `dvd_vm_ref.py` directly (screenshot VOB
extents, disc fingerprinting).
