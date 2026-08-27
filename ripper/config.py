"""Environment-variable configuration.

Defaults mirror the original shell scripts (dvd-autorip.sh / entrypoint.sh) so
an existing docker-compose deployment keeps working with only the port added.
"""
import os
from dataclasses import dataclass, field


def _split(val):
    return [x for x in (val or "").split() if x]


def _split_csv(val):
    return {x.strip() for x in (val or "").split(",") if x.strip()}


ALL_NOTIFY_EVENTS = {
    "rip_complete", "rip_aborted", "disc_stalled", "disc_error", "duplicate",
    "disk_space_low", "backfill_done",
    # A rip that completes but is still CSS-scrambled looks like a success
    # everywhere else, so it gets its own event rather than riding on
    # rip_complete -- the whole point is that it must not pass unnoticed.
    "rip_encrypted",
}


@dataclass
class Config:
    devices: list = field(default_factory=list)        # /dev/sr0 /dev/sr1 ...
    mock_drives: list = field(default_factory=list)    # mock0 mock1 ...
    output_dir: str = "/mnt/rips"
    work_dir: str = "/var/tmp/dvd-autorip"
    poll_interval: float = 5.0
    port: int = 8080
    host: str = "0.0.0.0"
    duplicate_policy: str = "skip"                     # skip | rip
    discord_webhook_url: str = ""
    notify_events: set = field(default_factory=lambda: set(ALL_NOTIFY_EVENTS))
    space_low_gb: float = 25.0                         # header warning + notify threshold
    # stall detection (see DriveWatcher._run_with_progress): a doomed disc
    # crawls rather than failing, so "average rate since the last healthy point
    # never reached stall_mbps" is what counts as stalled
    stall_mbps: float = 0.2                            # below this = stalled
    stall_warn_min: float = 5.0                        # badge + ping (0 = off)
    stall_abort_min: float = 15.0                      # auto-abort (0 = off)
    # runaway guard: a rip can read FAST and never end (overlapping/padded
    # extents), which stall detection cannot see. Percentages of the size the
    # disc declares for itself.
    overrun_warn_pct: float = 110.0                    # flag it (0 = off)
    overrun_abort_pct: float = 150.0                   # auto-abort (0 = off)
    # Video elementary-stream census (line-21 captions, picture coding /
    # cadence). Seconds per disc against a rip of many minutes, and it reports
    # the disc properties the IFOs cannot -- so it is on unless turned off.
    es_scan: bool = True
    vendor_dir: str = ""

    @classmethod
    def from_env(cls, env=os.environ):
        here = os.path.dirname(os.path.abspath(__file__))
        cfg = cls(
            devices=_split(env.get("DVD_DEVICES", "/dev/sr0")),
            mock_drives=_split(env.get("MOCK_DRIVES", "")),
            output_dir=env.get("OUTPUT_DIR", "/mnt/rips"),
            work_dir=env.get("WORK_DIR", "/var/tmp/dvd-autorip"),
            poll_interval=float(env.get("POLL_INTERVAL", "5")),
            port=int(env.get("PORT", "8080")),
            host=env.get("HOST", "0.0.0.0"),
            duplicate_policy=env.get("DUPLICATE_POLICY", "skip").lower(),
            discord_webhook_url=env.get("DISCORD_WEBHOOK_URL", ""),
            space_low_gb=float(env.get("SPACE_LOW_GB", "25")),
            stall_mbps=float(env.get("STALL_MBPS", "0.2")),
            stall_warn_min=float(env.get("STALL_WARN_MIN", "5")),
            stall_abort_min=float(env.get("STALL_ABORT_MIN", "15")),
            overrun_warn_pct=float(env.get("OVERRUN_WARN_PCT", "110")),
            overrun_abort_pct=float(env.get("OVERRUN_ABORT_PCT", "150")),
            es_scan=env.get("ES_SCAN", "1").strip().lower()
                    not in ("0", "false", "no", "off"),
            vendor_dir=env.get("VENDOR_DIR",
                               os.path.join(os.path.dirname(here), "vendor")),
        )
        if "NOTIFY_EVENTS" in env:
            cfg.notify_events = _split_csv(env["NOTIFY_EVENTS"]) & ALL_NOTIFY_EVENTS
        if cfg.duplicate_policy not in ("skip", "rip"):
            cfg.duplicate_policy = "skip"
        return cfg
