"""Per-drive watcher threads: media polling, rip pipeline, progress, states.

State machine (per drive):

  IDLE -> DETECTED (settle, label, size, fingerprint)
    -> DUPLICATE (policy=skip)            -> EJECTING
    -> ERROR (no scratch/output space)    -> EJECTING
    -> RIPPING (dvdbackup -M, progress = mirror bytes / disc size)
    -> SCREENSHOTS (from the decrypted mirror, before it's deleted)
    -> BUILDING_ISO (genisoimage, progress = iso bytes / mirror bytes)
    -> FINALIZING (resolve queued name, promote .part -> .iso, sidecars,
                   rm mirror)
    -> EJECTING -> WAITING_REMOVAL -> IDLE

  any of DETECTED/RIPPING/SCREENSHOTS/BUILDING_ISO -> ABORTED -> EJECTING
    (user pressed Abort — see abort()/_abort_step())

Gotchas ported from the original shell scripts: never open the device to poll
(udev DB instead — firmware tray-close quirk), re-arm only after the disc is
removed, sanitize labels, unique timestamped names, mirror cleanup on every
exit path.

Naming (queued name, see queue_name()/_resolve_name()): the workflow is
"insert disc, then go and type the real movie name while it rips", so a name
can be queued from the UI any time between DETECTED and BUILDING_ISO. It is
read exactly once, at FINALIZING, and becomes `<name>_<timestamp>`; until then
every artifact uses the label-derived working base. Reading it late means a
rip that is never named (or that fails) needs nothing undone, and the ISO is
promoted from `.part` straight to its final name — only the small jpgs are
ever renamed.

Aborting (abort(), see also _abort_step()): a dying disc typically reads at
0.0-0.1 MB/s for a very long time rather than failing outright, so the user
needs a way to give up on it. Abort is a *request* (a threading.Event), not a
thread kill: the watcher thread stays in charge, so every abort takes the same
cleanup path as a failure — mirror removed, `.part` removed, screenshots for
the discarded rip removed, disc ejected. It is honoured at phase boundaries,
between screenshot grabs, and within one SAMPLE_S tick of a running
dvdbackup/genisoimage (which are killed by process *group*, since dvdbackup
under libdvdcss can leave children behind). Nothing is written to the library:
an aborted disc leaves no ISO and no sidecar, exactly as if it had never been
inserted.

Stall detection (_run_with_progress + _stall_warn/_stall_abort) automates the
same judgement: a run is "healthy" whenever its average rate SINCE the last
healthy point reaches cfg.stall_mbps, so a single good burst clears the stall
and a disc that hits a bad patch and recovers is never counted against. Past
stall_warn_min the UI flags it and Discord gets one ping; past stall_abort_min
it calls abort() itself. Thresholds are generous on purpose — "never got
going" is obvious within a minute, but "stalled at 60%" may be dvdbackup
retrying a scratch, where aborting would throw away real work.
"""
import collections
import datetime
import errno
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from . import detect
from .census import run_census
from .verify import summary as css_summary, verify_css
from .fingerprint import fingerprint
from .library import validate_name
from .screenshots import capture_from_mirror

SPACE_MARGIN = 2 << 30          # extra bytes required beyond disc size
STDERR_TAIL = 2000              # chars of subprocess stderr kept on failure
SAMPLE_S = 2.0                  # progress sample period
KILL_GRACE_S = 5.0              # SIGTERM -> SIGKILL wait when aborting

# states in which a name may still be queued for the disc in the drive
NAMEABLE_STATES = ("DETECTED", "RIPPING", "SCREENSHOTS", "BUILDING_ISO")

# states a rip may be abandoned from. FINALIZING is deliberately excluded:
# by then the ISO is built and only cheap bookkeeping remains, so aborting
# would throw away a finished deliverable for no gain.
ABORTABLE_STATES = NAMEABLE_STATES

ABORT_RC = -999                 # _run_with_progress sentinel: killed by abort


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


class EventLog:
    def __init__(self, maxlen=50):
        self._events = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, kind, device, message):
        with self._lock:
            self._events.appendleft({
                "time": utcnow(), "kind": kind,
                "device": device, "message": message})

    def list(self):
        with self._lock:
            return list(self._events)


class Board:
    """Shared view of everything the web UI reports; also dumps .status.json."""

    def __init__(self, cfg, library, notifier):
        self.cfg = cfg
        self.library = library
        self.notifier = notifier
        self.events = EventLog()
        self.watchers = []
        self.backfill = {"state": "idle", "done": 0, "total": 0, "current": None}
        self._lock = threading.Lock()
        self._space_notified = 0.0

    def status(self):
        out_free = detect.free_bytes(self.cfg.output_dir)
        work_free = detect.free_bytes(self.cfg.work_dir)
        low = self.cfg.space_low_gb * (1 << 30)
        space_low = out_free < low or work_free < low
        if space_low:
            self._maybe_notify_space(out_free, work_free)
        with self._lock:
            backfill = dict(self.backfill)
        return {
            "drives": [w.status() for w in self.watchers],
            "backfill": backfill,
            "disk": {"output_free_bytes": out_free,
                     "work_free_bytes": work_free,
                     "space_low": space_low},
            "notify": self.notifier.status(),
            "events": self.events.list(),
        }

    def set_backfill(self, **kw):
        with self._lock:
            self.backfill.update(kw)

    def _maybe_notify_space(self, out_free, work_free):
        now = time.time()
        if now - self._space_notified > 6 * 3600:
            self._space_notified = now
            self.notifier.notify(
                "disk_space_low", "Disk space low",
                "output free: %.1f GB, scratch free: %.1f GB"
                % (out_free / (1 << 30), work_free / (1 << 30)))

    def dump(self):
        try:
            path = os.path.join(self.cfg.output_dir, ".status.json")
            import json
            with open(path + ".tmp", "w") as fh:
                json.dump(self.status(), fh, indent=1)
            os.replace(path + ".tmp", path)
        except OSError:
            pass


class DriveWatcher(threading.Thread):
    def __init__(self, cfg, dev, board):
        super().__init__(name="watch-" + os.path.basename(dev), daemon=True)
        self.cfg = cfg
        self.dev = dev
        self.board = board
        self._lock = threading.Lock()
        self._status = {}
        self._abort = threading.Event()
        self._proc = None          # currently running rip subprocess, if any
        self._set(state="IDLE")

    # ---- status ------------------------------------------------------------
    def _set(self, **kw):
        base = {
            "device": self.dev, "state": "IDLE", "label": None, "name": None,
            "disc_bytes": 0, "phase": None, "progress": None,
            "speed_mbps": None, "eta_s": None, "error": None,
            "error_detail": None, "duplicate_of": None, "started_at": None,
            "queued_name": None, "timestamp": None, "abort_requested": False,
            "aborted": False, "abort_reason": None, "stalled_s": None,
            "bytes_done": 0, "overrun": False,
        }
        with self._lock:
            base.update({k: v for k, v in self._status.items()
                         if k in ("label", "name", "disc_bytes", "error",
                                  "error_detail", "duplicate_of", "started_at",
                                  "queued_name", "timestamp",
                                  "abort_requested", "aborted",
                                  "abort_reason")})
            base.update(kw)
            self._status = base
        self.board.dump()

    def _update(self, **kw):
        with self._lock:
            self._status.update(kw)

    def status(self):
        with self._lock:
            st = dict(self._status)
        # derived: what the finished rip will be called, name box included
        st["nameable"] = st.get("state") in NAMEABLE_STATES
        # abortable = the button is live; aborting = requested and still being
        # wound down (once the state leaves ABORTABLE_STATES it is over, so the
        # UI must stop saying "aborting…")
        in_abortable = st.get("state") in ABORTABLE_STATES
        st["aborting"] = bool(in_abortable and st.get("abort_requested"))
        st["abortable"] = in_abortable and not st.get("abort_requested")
        st["planned_name"] = (
            "%s_%s" % (st["queued_name"], st["timestamp"])
            if st.get("queued_name") and st.get("timestamp") else st.get("name"))
        # stall is reported to the UI only once it is past the warn threshold —
        # below that it is just a slow sample, not news
        warn_s = self.cfg.stall_warn_min * 60
        abort_s = self.cfg.stall_abort_min * 60
        stalled_s = st.get("stalled_s") or 0
        st["stalled"] = bool(warn_s and stalled_s >= warn_s)
        st["stall_abort_in_s"] = (int(abort_s - stalled_s)
                                  if st["stalled"] and abort_s else None)
        return st

    def queue_name(self, name):
        """Queue the final name for the disc in this drive (UI, mid-rip).

        Last write wins, right up to FINALIZING. Raises ValueError for a bad
        name and RuntimeError if this drive has nothing being ripped — the
        queue is deliberately NOT accepted while IDLE, and is cleared at the
        start of every cycle, so a name can never be applied to the wrong disc.
        """
        name = validate_name(name)
        with self._lock:
            state = self._status.get("state")
            if state not in NAMEABLE_STATES:
                raise RuntimeError(
                    "no rip in progress in %s (%s)" % (self.dev, state.lower())
                    if state in ("IDLE", "WAITING_REMOVAL") else
                    "too late to name this rip (%s) — rename it in the library"
                    % state.lower())
            self._status["queued_name"] = name
        self._event("name_queued", "name queued: %s" % name)
        self.board.dump()

    def abort(self, reason="aborted by request"):
        """Ask the watcher thread to abandon the disc it is ripping and eject.

        Sets the flag and kills whatever subprocess is running right now; the
        watcher thread does the actual cleanup (see _abort_step()) so there is
        exactly one teardown path — the automatic stall abort comes through
        here too and differs only in `reason`. Raises RuntimeError when this
        drive has nothing abortable — including when an abort is already in
        flight, so a double tap can't queue a second one onto the *next* disc.
        """
        with self._lock:
            state = self._status.get("state")
            if state not in ABORTABLE_STATES:
                raise RuntimeError(
                    "nothing to abort in %s (%s)" % (self.dev, state.lower()))
            if self._status.get("abort_requested"):
                raise RuntimeError("abort already in progress for %s" % self.dev)
            self._status["abort_requested"] = True
            self._status["abort_reason"] = reason
        self._abort.set()
        self._event("abort", "%s — abandoning this disc" % reason)
        self._kill_proc()
        self.board.dump()

    def _aborting(self):
        return self._abort.is_set()

    # ---- stall handling ----------------------------------------------------
    @staticmethod
    def _mins(seconds):
        # thresholds are minutes in production but seconds in tests — don't
        # round a 30 s test threshold down to a meaningless "0 min"
        return "%d min" % (seconds // 60) if seconds >= 60 \
            else "%d s" % seconds

    def _stall_warn(self, stalled_s):
        msg = "no progress for %s (under %.2f MB/s)" \
              % (self._mins(stalled_s), self.cfg.stall_mbps)
        self._event("stalled", "stalled: %s" % msg)
        self.board.notifier.notify(
            "disc_stalled", "Disc stalled: %s"
            % (self._status.get("label") or self.dev),
            "%s on %s%s" % (msg, self.dev,
                            "" if not self.cfg.stall_abort_min else
                            " — auto-abort at %g min" % self.cfg.stall_abort_min))

    def _overrun_warn(self, done, total, pct):
        """Written well past the size the disc claims for itself.

        Not automatically fatal — some discs simply under-report — but it means
        the percentage is meaningless from here on and the rip may be copying
        overlapping or padded extents, so it is worth surfacing early: scratch
        use is now unbounded by the disc's own size.
        """
        msg = "%.0f%% of the declared size (%.2f GB written, disc claims " \
              "%.2f GB)" % (pct, done / (1 << 30), total / (1 << 30))
        self._event("overrun", "writing past the disc size: %s" % msg)
        self.board.notifier.notify(
            "disc_stalled", "Disc over-running: %s"
            % (self._status.get("label") or self.dev),
            "%s on %s%s" % (msg, self.dev,
                            "" if not self.cfg.overrun_abort_pct else
                            " — auto-abort at %g%%" % self.cfg.overrun_abort_pct))

    def _overrun_abort(self, pct):
        """Give up on a rip that has no end in sight (see _overrun_warn)."""
        try:
            self.abort(reason="auto-aborted at %.0f%% of the declared disc size"
                              % pct)
        except RuntimeError:
            pass

    def _stall_abort(self, stalled_s):
        """Give up on a disc that has crawled for stall_abort_min minutes.

        Goes through the ordinary abort request so the teardown, the UI and
        the event log are identical to the user pressing the button — only the
        reason differs. abort() may refuse (an abort is already in flight, or
        the phase moved on); that is fine, it is retried on the next sample.
        """
        try:
            self.abort(reason="auto-aborted after %s under %.2f MB/s"
                              % (self._mins(stalled_s), self.cfg.stall_mbps))
        except RuntimeError:
            pass

    def _kill_proc(self):
        """Terminate the running rip subprocess and its whole process group.

        Process *group*, because dvdbackup/genisoimage can leave children
        behind that would keep writing into the mirror we are about to delete.
        A disc-read hang can leave the process unkillable in D state; the
        SIGKILL is best-effort and the watcher moves on either way (the kernel
        reaps it when the I/O finally errors out).
        """
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError, OSError) as e:
                if getattr(e, "errno", None) != errno.ESRCH:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                return
            try:
                proc.wait(timeout=KILL_GRACE_S)
                return
            except subprocess.TimeoutExpired:
                continue

    def _event(self, kind, msg):
        self.board.events.add(kind, self.dev, msg)

    # ---- media plumbing (overridden by MockDriveWatcher) -------------------
    def media_present(self):
        return detect.media_present(self.dev)

    def read_label(self):
        return detect.read_label(self.dev)

    def disc_size(self):
        return detect.disc_size(self.dev)

    def disc_fingerprint(self):
        return fingerprint(self.dev)

    def eject(self):
        return detect.eject(self.dev)

    def rip_cmd(self, label, mirror_dir):
        return ["dvdbackup", "-i", self.dev, "-M", "-n", label, "-o", mirror_dir]

    # ---- main loop ---------------------------------------------------------
    def run(self):
        while True:
            try:
                if self.media_present():
                    self._rip_cycle()
                    self._set(state="WAITING_REMOVAL")
                    while self.media_present():
                        time.sleep(self.cfg.poll_interval)
                    self._set(state="IDLE", label=None, name=None,
                              duplicate_of=None, started_at=None, disc_bytes=0,
                              queued_name=None, timestamp=None,
                              abort_requested=False)
                    # `aborted`/`error` deliberately survive into IDLE (they
                    # are cleared when the next disc goes in) so the card still
                    # says why nothing was produced after the disc is removed
            except Exception as e:
                self._event("error", "watcher error: %r" % (e,))
                self._set(state="ERROR", error="watcher error: %r" % (e,))
                time.sleep(30)
            time.sleep(self.cfg.poll_interval)

    # ---- one disc ----------------------------------------------------------
    def _rip_cycle(self):
        cfg = self.cfg
        # the timestamp is reserved here, at insert, so the UI can show the
        # exact final filename as soon as a name is typed; queued_name and the
        # abort flag are cleared so neither can leak from the previous disc
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self._abort.clear()
        self._proc = None
        self._set(state="DETECTED", error=None, duplicate_of=None,
                  started_at=utcnow(), queued_name=None, timestamp=timestamp,
                  abort_requested=False, aborted=False, abort_reason=None)
        time.sleep(2)  # let the drive settle after insert
        label = self.read_label()
        disc_bytes = self.disc_size()
        self._update(label=label, disc_bytes=disc_bytes)
        self._event("detected", "disc detected: %s (%.2f GB)"
                    % (label, disc_bytes / (1 << 30)))

        # the fingerprint read is a plain blocking read of the IFO area; an
        # abort asked for during it is honoured as soon as it returns
        fp = self.disc_fingerprint()
        if self._aborting():
            self._abort_step()
            return
        dup_of = self.board.library.fingerprint_index().get(fp) if fp else None
        if dup_of:
            self._update(duplicate_of=dup_of)
            self._event("duplicate", "disc already ripped as %s" % dup_of)
            self.board.notifier.notify(
                "duplicate", "Duplicate disc: %s" % label,
                "already ripped as %s (%s)" % (dup_of, self.dev))
            if cfg.duplicate_policy == "skip":
                self._set(state="DUPLICATE", duplicate_of=dup_of)
                self._eject_step()
                return

        need = disc_bytes + SPACE_MARGIN
        if detect.free_bytes(cfg.work_dir) < need or \
           detect.free_bytes(cfg.output_dir) < need:
            msg = "not enough disk space for a %.1f GB disc" \
                  % (disc_bytes / (1 << 30))
            self._fail("space", msg, notify_event="disk_space_low")
            return

        work_base = base = "%s_%s" % (label, timestamp)   # may be replaced at
        self._update(name=base)               # FINALIZING by _resolve_name()
        mirror_root = tempfile.mkdtemp(
            prefix=os.path.basename(self.dev) + ".", dir=cfg.work_dir)
        started = time.time()
        errors = []
        part = None
        shots = []
        try:
            # -- RIPPING ------------------------------------------------------
            self._set(state="RIPPING", phase="dvdbackup", progress=0.0)
            self._event("rip_start", "ripping %s -> %s.iso" % (label, base))
            rc, tail = self._run_with_progress(
                self.rip_cmd(label, mirror_root),
                lambda: detect.dir_bytes(mirror_root), disc_bytes)
            if rc == ABORT_RC:
                self._abort_step(part, shots)
                return
            if rc != 0:
                errors.append({"phase": "dvdbackup", "exit_code": rc,
                               "stderr_tail": tail})
                self._fail("dvdbackup",
                           "dvdbackup failed (unreadable disc or unsupported "
                           "protection scheme)", tail)
                return

            mirror_dir = os.path.join(mirror_root, label)
            video_ts = os.path.join(mirror_dir, "VIDEO_TS")

            # -- SCREENSHOTS (mirror is decrypted and still on disk) ----------
            self._set(state="SCREENSHOTS", phase="screenshots", progress=None)
            shots = capture_from_mirror(video_ts, cfg.output_dir, base,
                                        should_abort=self._aborting)
            if self._aborting():
                self._abort_step(part, shots)
                return
            if not shots:
                self._event("warn", "no screenshots captured")

            # -- BUILDING_ISO -------------------------------------------------
            mirror_bytes = detect.dir_bytes(mirror_dir)
            part = os.path.join(cfg.output_dir, base + ".iso.part")
            self._set(state="BUILDING_ISO", phase="genisoimage", progress=0.0)
            rc, tail = self._run_with_progress(
                ["genisoimage", "-dvd-video", "-udf", "-o", part, mirror_dir],
                lambda: (os.path.getsize(part) if os.path.exists(part) else 0),
                mirror_bytes)
            if rc == ABORT_RC:
                self._abort_step(part, shots)
                return
            if rc != 0:
                errors.append({"phase": "genisoimage", "exit_code": rc,
                               "stderr_tail": tail})
                if os.path.exists(part):
                    os.unlink(part)
                self._fail("genisoimage", "genisoimage failed while building "
                           "the ISO", tail)
                return

            # -- FINALIZING ---------------------------------------------------
            # last look at the queued name, then the ISO is promoted straight
            # from .part into its final name (no big-file rename)
            self._set(state="FINALIZING", phase="census", progress=None)
            base, shots = self._resolve_name(work_base, timestamp, shots)
            iso_path = os.path.join(cfg.output_dir, base + ".iso")
            os.replace(part, iso_path)
            lib = self.board.library
            run_census(iso_path, lib.census_path(base), cfg.vendor_dir,
                       es_scan=cfg.es_scan)
            # Check our own work before calling this a success: a rip that
            # lost libdvdcss still produces a full-size, well-formed, useless
            # ISO (see ripper/verify.py).
            self._set(state="FINALIZING", phase="verify", progress=None)
            css = verify_css(iso_path, cfg.vendor_dir)
            finished = time.time()
            queued = self._status.get("queued_name")
            lib.write_meta(base, {
                "iso": base + ".iso", "label": label, "device": self.dev,
                # named_by_user reflects what was APPLIED, not what was typed:
                # a name can lose to an existing file (see _resolve_name)
                "queued_name": queued, "named_by_user": base != work_base,
                "started_at": self._status.get("started_at"),
                "finished_at": utcnow(),
                "duration_s": int(finished - started),
                "disc_bytes": disc_bytes,
                "iso_bytes": os.path.getsize(iso_path),
                "fingerprint": fp, "status": "ok", "errors": errors,
                # status stays "ok": the RIP succeeded mechanically. Whether
                # the artifact is playable is a separate question with its own
                # answer, so it gets its own field.
                "css": css,
                "screenshots": shots, "backfilled": False,
            })
            lib.invalidate()
            self._event("rip_done", "rip finished: %s.iso (%d min)"
                        % (base, int((finished - started) / 60)))
            menu_jpg = os.path.join(cfg.output_dir, base + ".menu.jpg")
            self.board.notifier.notify(
                "rip_complete", "Rip complete: %s" % (queued or label),
                "%s.iso — %.2f GB in %d min (%s)"
                % (base, os.path.getsize(iso_path) / (1 << 30),
                   int((finished - started) / 60), self.dev),
                image_path=menu_jpg if os.path.isfile(menu_jpg) else None)
            warn = css_summary(css)
            if warn:
                self._event("css_warn", "%s: %s" % (base, warn))
                self.board.notifier.notify(
                    "rip_encrypted",
                    "Rip may not play: %s" % (queued or label), warn)
        finally:
            shutil.rmtree(mirror_root, ignore_errors=True)
        if self._aborting():
            # the request landed in FINALIZING (not abortable) and the rip beat
            # it — say so rather than leaving the UI's "aborting…" unexplained
            self._update(abort_requested=False)
            self._event("warn", "abort arrived too late — the rip had finished")
        self._eject_step()

    # ---- helpers -----------------------------------------------------------
    def _resolve_name(self, work_base, timestamp, shots):
        """Apply a UI-queued name at FINALIZING. Returns (base, screenshots).

        The queued text is kept verbatim (same character set `rename` allows)
        and still gets the `_<timestamp>` suffix, so two rips of the same movie
        never collide and every name in the library reads the same way. The
        screenshots are already on disk under the working base, so they are the
        only files renamed here; the ISO is still a `.part` and gets promoted
        into the final name by the caller. Any failure degrades to the working
        name rather than to a half-renamed set.
        """
        with self._lock:
            queued = self._status.get("queued_name")
        if not queued:
            return work_base, shots
        final = "%s_%s" % (queued, timestamp)
        if final == work_base:
            return work_base, shots
        for ext in (".iso", ".ISO"):
            if os.path.exists(os.path.join(self.cfg.output_dir, final + ext)):
                self._event("warn", "keeping %s: %s%s already exists"
                            % (work_base, final, ext))
                return work_base, shots
        renamed = []
        for s in shots:
            suffix = s[len(work_base):]     # ".menu.jpg", ".title2.jpg", ...
            try:
                os.rename(os.path.join(self.cfg.output_dir, s),
                          os.path.join(self.cfg.output_dir, final + suffix))
                renamed.append(final + suffix)
            except OSError:
                renamed.append(s)           # meta must list what really exists
        self._update(name=final)
        self._event("named", "%s -> %s" % (work_base, final))
        return final, renamed

    def _run_with_progress(self, cmd, bytes_done_fn, bytes_total):
        """Run cmd, sampling progress every SAMPLE_S. Returns (rc, stderr_tail).

        Returns ABORT_RC when the run was cut short by abort(), so the caller
        takes the abort path instead of reporting a failed disc. The child gets
        its own session (start_new_session) purely so abort can signal the
        whole process group.
        """
        with tempfile.TemporaryFile(mode="w+") as errf:
            try:
                proc = subprocess.Popen(cmd, stdout=errf, stderr=errf,
                                        start_new_session=True)
            except OSError as e:
                return 127, "failed to launch %s: %s" % (cmd[0], e)
            self._proc = proc
            killed = False
            # abort() may have arrived between the check and the spawn
            if self._aborting():
                self._kill_proc()
                killed = True
            speed = None  # bytes/s EMA
            last_b, last_t = 0, time.time()
            # stall watch: (bytes, time) of the last point the run was healthy
            ok_b, ok_t, warned = 0, time.time(), False
            over_warned = False        # runaway watch (see _overrun_abort)
            while proc.poll() is None:
                time.sleep(SAMPLE_S)
                if self._aborting():
                    # kill once; a process wedged in a disc read can sit in
                    # uninterruptible I/O for a while, and re-signalling it
                    # every tick would neither help nor be visible
                    if not killed:
                        self._kill_proc()
                        killed = True
                    continue        # stop sampling; just wait for it to die
                done = bytes_done_fn()
                now = time.time()
                inst = (done - last_b) / max(now - last_t, 0.1)
                speed = inst if speed is None else 0.7 * speed + 0.3 * inst
                last_b, last_t = done, now
                progress = min(done / bytes_total, 1.0) if bytes_total else None
                # Both totals are ESTIMATES, and the numerator can pass them:
                # the RIPPING total is what the disc's ISO9660 descriptor
                # claims (under-reports on bridge-format / protected discs),
                # and the BUILDING_ISO total is the mirror's file bytes, which
                # the finished ISO always exceeds by its UDF/ISO structures and
                # DVD-Video padding. Past that point the ratio is a lie the bar
                # can't show (it clamps at 1.0), so drop the ETA rather than
                # report a negative one, and let the UI fall back to bytes.
                overrun = bool(bytes_total) and done >= bytes_total
                eta = int((bytes_total - done) / speed) \
                    if bytes_total and speed and speed > 1e3 and not overrun \
                    else None
                # a run is "healthy" whenever its average rate SINCE the last
                # healthy point reaches the threshold, so one good burst clears
                # the stall — that is what keeps a disc that hits a bad patch
                # and recovers from being counted as stalled
                if (done - ok_b) / max(now - ok_t, 0.1) >= self.cfg.stall_mbps * 1e6:
                    ok_b, ok_t, warned = done, now, False
                stalled_s = int(now - ok_t)
                self._update(progress=progress, bytes_done=done,
                             overrun=overrun,
                             speed_mbps=round(speed / 1e6, 2) if speed else None,
                             eta_s=eta, stalled_s=stalled_s)
                warn_s = self.cfg.stall_warn_min * 60      # 0 disables either
                abort_s = self.cfg.stall_abort_min * 60
                if warn_s and not warned and stalled_s >= warn_s:
                    self._stall_warn(stalled_s)
                    warned = True
                if abort_s and stalled_s >= abort_s:
                    self._stall_abort(stalled_s)
                # a runaway reads FAST and never ends, so stall detection is
                # blind to it — bound it by size instead
                if overrun and bytes_total:
                    pct = 100.0 * done / bytes_total
                    if self.cfg.overrun_warn_pct and not over_warned \
                            and pct >= self.cfg.overrun_warn_pct:
                        self._overrun_warn(done, bytes_total, pct)
                        over_warned = True
                    if self.cfg.overrun_abort_pct \
                            and pct >= self.cfg.overrun_abort_pct:
                        self._overrun_abort(pct)
            self._proc = None
            errf.seek(0)
            tail = errf.read()[-STDERR_TAIL:]
            if self._aborting():
                return ABORT_RC, tail
            return proc.returncode, tail

    def _fail(self, phase, message, stderr_tail="", notify_event="disc_error"):
        self._event("error", "[%s] %s" % (phase, message))
        self._set(state="ERROR", error=message, phase=phase,
                  error_detail=stderr_tail)
        self.board.notifier.notify(
            notify_event, "Rip failed: %s" % (self._status.get("label") or self.dev),
            "%s: %s\n%s" % (phase, message, stderr_tail[-500:]))
        self._eject_step(final_state="ERROR")

    def _abort_step(self, part=None, shots=()):
        """Tear down an aborted rip and eject. The mirror is removed by the
        caller's `finally`; the partial ISO and any screenshots already written
        into OUTPUT_DIR are removed here, because an aborted disc must leave
        nothing behind for the library to pick up (a `.part` is invisible to
        the library, but the jpgs would otherwise be orphaned — the same wart
        the genisoimage failure path is known to have).
        """
        label = self._status.get("label") or self.dev
        reason = self._status.get("abort_reason") or "aborted"
        for path in ([part] if part else []) + \
                    [os.path.join(self.cfg.output_dir, s) for s in shots]:
            try:
                os.unlink(path)
            except OSError:
                pass
        # `aborted` (not the state, which becomes IDLE once the disc is out)
        # is what tells the UI to report this as a deliberate act, not a failure
        self._set(state="ABORTED", phase=None, progress=None, speed_mbps=None,
                  eta_s=None, stalled_s=None, aborted=True,
                  error="%s — no ISO was written" % reason)
        self._event("aborted", "rip aborted: %s (%s)" % (label, reason))
        self.board.notifier.notify(
            "rip_aborted", "Rip aborted: %s" % label,
            "%s on %s — nothing was written" % (reason, self.dev))
        self._eject_step(final_state="ABORTED")

    def _eject_step(self, final_state=None):
        self._set(state="EJECTING", error=self._status.get("error"),
                  duplicate_of=self._status.get("duplicate_of"))
        if not self.eject():
            self._event("warn", "eject failed for %s" % self.dev)
        if final_state:
            self._update(state=final_state)
        # run() takes over: waits for media removal, then back to IDLE


class MockDriveWatcher(DriveWatcher):
    """Test double: 'insert' an ISO via the API; dvdbackup becomes bsdtar."""

    def __init__(self, cfg, name, board):
        super().__init__(cfg, name, board)
        self.mock_iso = None

    def media_present(self):
        return self.mock_iso is not None and os.path.isfile(self.mock_iso)

    def read_label(self):
        label = ""
        try:
            label = subprocess.run(
                ["blkid", "-o", "value", "-s", "LABEL", self.mock_iso],
                capture_output=True, text=True, timeout=15).stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            pass
        if not label:
            label = os.path.splitext(os.path.basename(self.mock_iso))[0]
        return detect.sanitize_label(label)

    def disc_size(self):
        try:
            return os.path.getsize(self.mock_iso)
        except OSError:
            return 0

    def disc_fingerprint(self):
        return fingerprint(self.mock_iso)

    def eject(self):
        self.mock_iso = None
        return True

    def rip_cmd(self, label, mirror_dir):
        dest = os.path.join(mirror_dir, label)
        os.makedirs(dest, exist_ok=True)
        return ["bsdtar", "-x", "-f", self.mock_iso, "-C", dest]
