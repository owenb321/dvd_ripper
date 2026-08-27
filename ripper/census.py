"""Run the vendored MiSTer_DVD census on an ISO -> NAME.census.json sidecar.

Runs as a subprocess (vendor/census_run.py) with a timeout so a pathological
image can't hang or crash the supervisor. Always writes the sidecar; parse
failures land in its "error" field with features = null.
"""
import datetime
import json
import os
import subprocess
import sys

# The IFO census is near-instant; the opt-in elementary-stream scans read up to
# ~60 MB of VOB across several VTSs, which is seconds locally but can be far
# slower on a network share, so the ceiling is generous rather than tight.
TIMEOUT_S = 600
# 2: added the video elementary-stream fields (cc_*, cadence_*, pic_*,
#    field_coded). A schema-1 sidecar is complete for what it knew about, so
#    the only consequence is that it lacks those keys -- but a library ripped
#    before this change would otherwise never gain them, so backfill treats an
#    older schema as work to redo.
SCHEMA = 2


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def run_census(iso_path, census_json_path, vendor_dir, es_scan=True):
    """Returns the sidecar dict (also written to census_json_path).

    es_scan adds the video elementary-stream scans (line-21 captions, picture
    coding / 3:2 cadence). They are the only way to see properties the IFOs do
    not record, and cost seconds against a rip that costs minutes.
    """
    tmp = census_json_path + ".tmp"
    runner = os.path.join(vendor_dir, "census_run.py")
    error = None
    features = None
    try:
        proc = subprocess.run(
            [sys.executable, runner, iso_path, tmp]
            + (["--es"] if es_scan else []),
            capture_output=True, text=True, timeout=TIMEOUT_S)
        if proc.returncode == 0:
            with open(tmp) as fh:
                features = json.load(fh)
        else:
            error = (proc.stderr or "census exited %d" % proc.returncode).strip()
    except subprocess.TimeoutExpired:
        error = "census timed out after %ds" % TIMEOUT_S
    except (OSError, ValueError) as e:
        error = "census runner failed: %r" % (e,)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

    sidecar = {
        "schema": SCHEMA,
        "generated_at": _utcnow(),
        "error": error,
        "features": features,
    }
    with open(census_json_path, "w") as fh:
        json.dump(sidecar, fh, indent=1, sort_keys=True)
        fh.write("\n")
    return sidecar


def is_stale(census_json_path):
    """True if the sidecar is missing, unreadable, or predates SCHEMA.

    An unreadable sidecar counts as stale: regenerating costs seconds and is
    strictly better than carrying a corrupt one forever.
    """
    try:
        with open(census_json_path) as fh:
            return int(json.load(fh).get("schema", 0)) < SCHEMA
    except (OSError, ValueError, TypeError):
        return True
