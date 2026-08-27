"""The on-disk rip library: sidecar I/O, history cache, rename, dedup index.

Everything is derived by scanning OUTPUT_DIR (*.iso + sidecars) — no database.
The scan is cached in memory and invalidated on rip-complete/rename/backfill.

For NAME.iso the sidecars are:
  NAME.meta.json    rip record (schema 1)
  NAME.census.json  census record (schema 1, features = vendored census dict)
  NAME.menu.jpg / NAME.title1..3.jpg
"""
import datetime
import json
import os
import re
import sys
import threading

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor"))
from dvd_census import PREVALENCE_ROWS  # noqa: E402

META_SCHEMA = 1
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()\[\]'-]*$")
SHOT_SUFFIXES = (".menu.jpg", ".title1.jpg", ".title2.jpg", ".title3.jpg")


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_name(name):
    """Clean + check a user-supplied name. Returns it, or raises ValueError.

    One validator for both naming paths (rename after the fact, and the name
    queued from the UI while a disc is still ripping) so a name that is legal
    in one place can't be rejected in the other.
    """
    name = (name or "").strip()
    if not NAME_RE.match(name) or name.endswith("."):
        raise ValueError(
            "invalid name (allowed: letters digits space ._()[]'-)")
    return name


def census_chips(features):
    """Compact flag chips for the UI history cards.

    These describe the DISC to a person browsing their library -- what is on
    it and whether it is region-locked. The census also records a second class
    of feature (PTL_MAIT, TXTDT, NavTimer, GPRM counter mode, rnd, TMAP,
    un-decoded VM bits) which exists to track DVD-Video spec coverage for the
    MiSTer_DVD core. That vocabulary means nothing to someone ripping a movie,
    so it stays in the census JSON and the aggregate prevalence table rather
    than on the history cards.
    """
    if not features:
        return []
    chips = []
    fmts = features.get("audio_formats") or []
    if fmts:
        # Already spells out AC3/DTS/LPCM, so no separate codec chips.
        chips.append("+".join(fmts))
    if features.get("lpcm_24bit"):
        chips.append("LPCM24")
    if features.get("lpcm_96k"):
        chips.append("LPCM96k")
    n_subp = features.get("max_subp_streams") or 0
    if n_subp:
        chips.append("%d sub%s" % (n_subp, "" if n_subp == 1 else "s"))
    # Captions are a viewer-facing feature and, unlike everything else here,
    # are invisible to the IFOs -- they only exist if the ES scan ran.
    if features.get("cc_present"):
        chips.append("CC")
    if features.get("multi_angle"):
        chips.append("angles×%d" % features.get("max_angles", 0))
    if features.get("region_locked"):
        chips.append("region 0x%02x" % features.get("region_mask", 0))
    if features.get("max_chapters", 0) > 256:
        chips.append("⚠ >256ch")
    return chips


class Library:
    def __init__(self, output_dir):
        self.output_dir = output_dir
        self._lock = threading.Lock()
        self._history = None  # list of entries, newest first

    # ---- paths -------------------------------------------------------------
    def iso_path(self, base):
        for ext in (".iso", ".ISO"):
            p = os.path.join(self.output_dir, base + ext)
            if os.path.isfile(p):
                return p
        return os.path.join(self.output_dir, base + ".iso")

    def meta_path(self, base):
        return os.path.join(self.output_dir, base + ".meta.json")

    def census_path(self, base):
        return os.path.join(self.output_dir, base + ".census.json")

    def shot_paths(self, base):
        return [os.path.join(self.output_dir, base + s) for s in SHOT_SUFFIXES]

    # ---- sidecar I/O -------------------------------------------------------
    def _read_json(self, path):
        try:
            with open(path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def read_meta(self, base):
        """The meta sidecar dict, or None if absent/unreadable."""
        return self._read_json(self.meta_path(base))

    def write_meta(self, base, meta):
        meta = dict(meta, schema=META_SCHEMA)
        tmp = self.meta_path(base) + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(meta, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.meta_path(base))

    # ---- scan / history ----------------------------------------------------
    def invalidate(self):
        with self._lock:
            self._history = None

    def history(self):
        with self._lock:
            if self._history is None:
                self._history = self._scan()
            return self._history

    def _scan(self):
        entries = []
        try:
            names = os.listdir(self.output_dir)
        except OSError:
            return entries
        for n in sorted(names):
            if not n.lower().endswith(".iso"):
                continue
            base = n[:-4]
            iso = os.path.join(self.output_dir, n)
            try:
                st = os.stat(iso)
            except OSError:
                continue
            meta = self._read_json(self.meta_path(base))
            cen = self._read_json(self.census_path(base))
            feats = (cen or {}).get("features")
            shots = [base + s for s in SHOT_SUFFIXES
                     if os.path.isfile(os.path.join(self.output_dir, base + s))]
            entry = {
                "name": base,
                "iso": n,
                "iso_bytes": st.st_size,
                "mtime": st.st_mtime,
                "finished_at": (meta or {}).get("finished_at") or
                    datetime.datetime.fromtimestamp(
                        st.st_mtime, datetime.timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "meta": meta,
                "screenshots": shots,
                "census_error": (cen or {}).get("error") if cen else None,
                "has_census": feats is not None,
                "census_chips": census_chips(feats),
                "census_summary": {
                    "n_vts": feats.get("n_vts"),
                    "n_titles": feats.get("n_titles"),
                    "max_chapters": feats.get("max_chapters"),
                    "max_angles": feats.get("max_angles"),
                    "audio": feats.get("audio_formats"),
                } if feats else None,
            }
            entries.append(entry)
        entries.sort(key=lambda e: e["finished_at"], reverse=True)
        return entries

    # ---- dedup -------------------------------------------------------------
    def fingerprint_index(self):
        """{fingerprint: base-name} from meta sidecars (backfill seeds these)."""
        idx = {}
        for e in self.history():
            fp = (e["meta"] or {}).get("fingerprint")
            if fp:
                idx.setdefault(fp, e["name"])
        return idx

    # ---- aggregate census --------------------------------------------------
    def aggregate(self):
        vectors = []
        for e in self.history():
            cen = self._read_json(self.census_path(e["name"]))
            feats = (cen or {}).get("features")
            if feats:
                vectors.append((e["name"], feats))
        rows = []
        for key, label, gap in PREVALENCE_ROWS:
            discs = [name for name, f in vectors if f.get(key)]
            rows.append({"key": key, "label": label, "note": gap,
                         "count": len(discs), "discs": discs})
        # audio formats as pseudo-rows
        fmt_map = {}
        for name, f in vectors:
            for fmt in f.get("audio_formats") or []:
                fmt_map.setdefault(fmt, []).append(name)
        for fmt, discs in sorted(fmt_map.items()):
            rows.append({"key": "audio_" + fmt, "label": "Audio: " + fmt,
                         "note": "", "count": len(discs), "discs": discs})
        return {"n_discs": len(vectors), "rows": rows}

    # ---- rename ------------------------------------------------------------
    def rename(self, old_base, new_base):
        """Rename ISO + all sidecars; rewrite meta fields. Raises ValueError."""
        new_base = validate_name(new_base)
        old_iso = self.iso_path(old_base)
        if not os.path.isfile(old_iso):
            raise ValueError("no such ISO: %s" % old_base)
        if new_base == old_base:
            return
        new_iso = os.path.join(self.output_dir, new_base + ".iso")
        for ext in (".iso", ".ISO"):
            if os.path.exists(os.path.join(self.output_dir, new_base + ext)):
                raise ValueError("target already exists: %s" % new_base)
        os.rename(old_iso, new_iso)
        renames = [(self.meta_path(old_base), self.meta_path(new_base)),
                   (self.census_path(old_base), self.census_path(new_base))]
        renames += list(zip(self.shot_paths(old_base), self.shot_paths(new_base)))
        for src, dst in renames:
            if os.path.isfile(src):
                os.rename(src, dst)
        meta = self._read_json(self.meta_path(new_base))
        if meta is not None:
            meta["iso"] = new_base + ".iso"
            meta["screenshots"] = [
                new_base + s for s in SHOT_SUFFIXES
                if os.path.isfile(os.path.join(self.output_dir, new_base + s))]
            self.write_meta(new_base, meta)
        self.invalidate()
