"""Backfill worker: sidecars for ISOs that predate this tool.

Runs once at startup and on POST /api/backfill. For every *.iso in OUTPUT_DIR
missing a sidecar it generates: census JSON, screenshots (from VOB extents
inside the ISO — no mounting), and a minimal meta record (backfilled=true,
file mtime as finished_at, fingerprint included so the dedup index recognizes
the disc on re-insert).
"""
import datetime
import os
import threading

from .census import is_stale, run_census
from .verify import verify_css
from .fingerprint import fingerprint
from .screenshots import capture_from_iso


def _needs_work(lib, base):
    meta = lib.read_meta(base)
    return (meta is None
            or is_stale(lib.census_path(base))
            # A library ripped before verification existed has never been
            # checked for the silent-encryption failure, and those ISOs are
            # exactly the ones worth checking: they have had time to be copied
            # to an SD card and disappoint someone.
            or "css" not in meta)


class BackfillWorker(threading.Thread):
    def __init__(self, cfg, board):
        super().__init__(name="backfill", daemon=True)
        self.cfg = cfg
        self.board = board
        self._wake = threading.Event()
        self._wake.set()  # run once at startup

    def trigger(self):
        self._wake.set()

    def run(self):
        while True:
            self._wake.wait()
            self._wake.clear()
            try:
                self._pass()
            except Exception as e:
                self.board.events.add("error", "backfill",
                                      "backfill pass failed: %r" % (e,))
                self.board.set_backfill(state="error", current=None)

    def _pass(self):
        lib = self.board.library
        try:
            names = sorted(os.listdir(self.cfg.output_dir))
        except OSError:
            return
        todo = [n[:-4] for n in names
                if n.lower().endswith(".iso") and _needs_work(lib, n[:-4])]
        if not todo:
            self.board.set_backfill(state="idle", done=0, total=0, current=None)
            return
        self.board.set_backfill(state="running", done=0, total=len(todo))
        self.board.events.add("backfill", "backfill",
                              "backfilling sidecars for %d ISO(s)" % len(todo))
        done = 0
        for base in todo:
            self.board.set_backfill(current=base)
            iso = lib.iso_path(base)
            try:
                self._one(lib, base, iso)
            except Exception as e:
                self.board.events.add("error", "backfill",
                                      "%s: backfill failed: %r" % (base, e))
            done += 1
            self.board.set_backfill(done=done)
            lib.invalidate()
        self.board.set_backfill(state="idle", current=None)
        self.board.events.add("backfill", "backfill",
                              "backfill finished: %d ISO(s)" % done)
        self.board.notifier.notify(
            "backfill_done", "Backfill finished",
            "sidecars generated for %d existing ISO(s)" % done)

    def _one(self, lib, base, iso):
        if is_stale(lib.census_path(base)):
            run_census(iso, lib.census_path(base), self.cfg.vendor_dir,
                       es_scan=self.cfg.es_scan)
        have_shots = [p for p in lib.shot_paths(base) if os.path.isfile(p)]
        if not have_shots:
            capture_from_iso(iso, self.cfg.output_dir, base)
        meta = lib.read_meta(base)
        if meta is not None and "css" not in meta:
            lib.write_meta(base, dict(meta, css=verify_css(
                iso, self.cfg.vendor_dir)))
        if meta is None:
            st = os.stat(iso)
            lib.write_meta(base, {
                "iso": os.path.basename(iso),
                "label": base,
                "device": None,
                "started_at": None,
                "finished_at": datetime.datetime.fromtimestamp(
                    st.st_mtime, datetime.timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "duration_s": None,
                "disc_bytes": st.st_size,
                "iso_bytes": st.st_size,
                "fingerprint": fingerprint(iso),
                "status": "ok",
                "errors": [],
                "screenshots": [os.path.basename(p)
                                for p in lib.shot_paths(base)
                                if os.path.isfile(p)],
                "backfilled": True,
                "css": verify_css(iso, self.cfg.vendor_dir),
            })
