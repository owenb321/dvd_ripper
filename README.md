# DVD Auto-Rip — headless appliance with web UI

Automates: insert disc → decrypt + rip to unencrypted `.iso` → auto-eject →
repeat, across any number of simultaneously-attached optical drives (SATA or
USB) — plus a phone-friendly **web UI** for monitoring it all from the couch.

For every disc it also produces **sidecar files** next to the ISO:

```
AKIRA_20260808_141201.iso            the rip
AKIRA_20260808_141201.meta.json      rip record (drive, times, sizes, errors, fingerprint,
                                     decryption check)
AKIRA_20260808_141201.census.json    DVD feature census (titles, chapters, angles,
                                     audio codecs, region, VM quirks, ...)
AKIRA_20260808_141201.menu.jpg       menu screenshot   — identify the disc at a glance
AKIRA_20260808_141201.title1..3.jpg  main-title frames @ 15/40/65%
```

Screenshots are grabbed from the **decrypted mirror during the rip** (no ISO
mounting), and the census is the [MiSTer_DVD](https://github.com/owenb321/MiSTer_DVD)
project's census tooling (vendored in `vendor/`) — so the library doubles as a
tracker for novel DVD features/specs. It reads the IFOs *and* samples the video
stream itself, for the two things the IFOs cannot tell you: line-21 closed
captions, and whether the disc is field-coded or film (3:2 pulldown).

## Web UI (`http://<host>:8080`)

- **Per-drive cards**: state, disc label, progress %, speed, ETA
- **Name it while it rips**: each active drive card has a name box — insert the
  disc, then type the real movie title any time before the rip finishes and the
  ISO + every sidecar are written as `Your Title_<timestamp>` instead of
  `VOLUME_LABEL_<timestamp>`. The card shows the exact filename it will use.
  Miss the window and it's just a **rename** away (below). Scriptable:
  `POST /api/name {"device": "/dev/sr0", "name": "The Movie"}`
- **Abort & eject**: a failing disc usually *crawls* (0.0-0.1 MB/s for ages)
  rather than erroring out, so every active drive card has an **Abort & eject**
  button: it kills the rip, deletes the partial ISO, mirror and screenshots,
  and ejects the disc — the library is left exactly as if the disc had never
  gone in. Available from detection until the ISO is built (not during
  finalizing, where the ISO already exists). Scriptable:
  `POST /api/abort {"device": "/dev/sr0"}`
- **Every rip checks its own work**: losing libdvdcss does not fail a rip — it
  produces a full-size, well-formed, silently undecrypted ISO that only reveals
  itself hours later on the player. Each finished ISO is sampled for
  CSS-scrambled packs (using the same test the MiSTer_DVD core applies
  on-board) and a bad one is called out on its card and pushed to Discord as
  `rip_encrypted`. Existing libraries get checked by backfill. "Unverified" is
  reported separately from "encrypted" — an absence of evidence is not a pass.
- **Automatic stall handling**: a rip counts as *stalled* when its average rate
  since the last healthy moment stays under `STALL_MBPS` (0.2 MB/s). After
  `STALL_WARN_MIN` (5) the card flags it and Discord gets one ping; after
  `STALL_ABORT_MIN` (15) it aborts and ejects itself. One good burst of
  progress clears the stall, so a disc that hits a scratch and recovers is not
  punished for it — set `STALL_ABORT_MIN=0` to keep the warning but never
  abort automatically.
- **Runaway guard**: some broken discs read at full speed but never finish —
  they keep writing past their own declared size (overlapping or padded
  extents). Stall detection can't see that, so a second guard flags a rip at
  `OVERRUN_WARN_PCT` (110%) of the disc's declared size and aborts it at
  `OVERRUN_ABORT_PCT` (150%), which also stops it eating unbounded scratch.
- **Notification self-check**: the header shows whether Discord is configured
  and how the *last* send actually went (including Discord's own error text),
  with a **Test notification** button — `POST /api/notify/test`, which returns
  the real result rather than failing silently.
- **Progress that admits what it doesn't know**: the percentage is bytes-so-far
  over an *estimate* (the size the disc declares, then the mirror's size), and
  the real work can exceed it — a mirror can even be bigger than the disc when
  a title's extents overlap or are padded. Once it passes the estimate the card
  stops claiming a percentage and shows bytes written with a striped bar,
  instead of sitting at "100%" with a negative time remaining.
- **Error callouts**: dvdbackup/genisoimage failures with the captured stderr tail
- **Duplicate detection**: discs are fingerprinted at insert (VMG IFO hash); a
  disc you already ripped is called out (and skipped + ejected by default —
  set `DUPLICATE_POLICY=rip` to rip anyway)
- **Rip history**: browsable library with screenshots, census flag chips, and
  **rename** (renames the ISO + all sidecars)
- **Census aggregate**: feature prevalence across the library; single-disc
  ("novel") features highlighted
- **Backfill**: ISOs ripped before this tool existed get census + screenshots
  + fingerprint sidecars generated from the ISO itself (runs at startup and on
  the Backfill button)

No auth — LAN use only.

## Notifications (optional)

Set `DISCORD_WEBHOOK_URL` to get Discord pings. Pick which events with
`NOTIFY_EVENTS` (comma list, default all):
`rip_complete`, `rip_aborted`, `disc_stalled`, `disc_error`, `duplicate`,
`disk_space_low`, `backfill_done`.

> Historical note: notifications silently failed for a long time because
> Cloudflare (in front of Discord) rejects Python's default `urllib`
> User-Agent with `403 error code: 1010`. The sender now sends a real agent —
> if you ever port this to another HTTP client, keep that header.

Not arriving? Press **Test notification** in the header (or
`curl -X POST http://<host>:8080/api/notify/test`) — it sends synchronously and
reports what happened: `HTTP 401 Invalid Webhook Token` means the URL is wrong
or the webhook was deleted, a timeout means the container has no egress, and
`skipped — '<kind>' not in NOTIFY_EVENTS` means it is being filtered on purpose.

## Option A: Docker (recommended)

> **Requires a native Linux Docker host.** Docker Desktop on Mac/Windows runs
> the engine inside a VM and can't see your host's SATA/USB optical drives.

1. Find your drives: `ls /dev/sr*`
2. Edit `docker-compose.yml` (`devices:` + `DVD_DEVICES` to match), then:

```bash
docker compose up -d --build
docker compose logs -f
```

Pop discs into any drive; finished ISOs + sidecars land in `/mnt/rips` on the
host, and the UI is on port 8080.

**How detection works without udev:** the container reads the *host's* udev
database (the `/run/udev:/run/udev:ro` mount) to ask whether each drive has DVD
media, polling every `POLL_INTERVAL` seconds. This deliberately avoids opening
the device to probe — some drive firmwares treat a bare `open()` as a
tray-close cue, which can yank a disc in misaligned mid-insert. Without the
mount it falls back to a `dd` probe.

**If `eject` fails inside the container**, add `cap_add: ["SYS_ADMIN"]` to the
service.

## Option B: bare metal (systemd)

```bash
sudo apt install dvdbackup genisoimage libdvd-pkg util-linux udev eject \
                 python3 python3-flask ffmpeg
sudo dpkg-reconfigure libdvd-pkg        # builds libdvdcss locally
sudo mkdir -p /opt/dvd-ripper /mnt/rips
sudo cp -r ripper vendor /opt/dvd-ripper/
sudo cp dvd-ripper.service /etc/systemd/system/   # edit DVD_DEVICES etc. first
sudo systemctl daemon-reload && sudo systemctl enable --now dvd-ripper
```

(The old udev-rule + shell-script deployment was replaced by this service —
one long-running supervisor handles detection, ripping, and the web UI.)

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `DVD_DEVICES` | `/dev/sr0` | space-separated drive list |
| `OUTPUT_DIR` | `/mnt/rips` | ISOs + sidecars + `.status.json` |
| `WORK_DIR` | `/var/tmp/dvd-autorip` | scratch for the decrypted mirror |
| `POLL_INTERVAL` | `5` | media poll period (s) |
| `PORT` / `HOST` | `8080` / `0.0.0.0` | web UI bind |
| `DUPLICATE_POLICY` | `skip` | `skip` (eject duplicates) or `rip` |
| `DISCORD_WEBHOOK_URL` | *(unset)* | enable Discord notifications |
| `NOTIFY_EVENTS` | all | comma list of event kinds to send |
| `SPACE_LOW_GB` | `25` | low-disk warning threshold |
| `STALL_MBPS` | `0.2` | below this average rate counts as stalled |
| `STALL_WARN_MIN` | `5` | minutes stalled before the UI flag + Discord ping (`0` = off) |
| `STALL_ABORT_MIN` | `15` | minutes stalled before it aborts itself (`0` = off) |
| `OVERRUN_WARN_PCT` | `110` | % of the disc's declared size before it flags a runaway (`0` = off) |
| `OVERRUN_ABORT_PCT` | `150` | % of the declared size before it aborts itself (`0` = off) |
| `ES_SCAN` | `1` | also scan the video stream (captions, picture coding); `0` = IFOs only |
| `MOCK_DRIVES` | *(unset)* | virtual drive names for testing (below) |

## Mock mode (test without a drive)

`MOCK_DRIVES=mock0` creates a virtual drive; "insert" an ISO via the API and
the full real pipeline runs (bsdtar extraction stands in for dvdbackup):

```bash
curl -X POST http://localhost:8080/api/mock/insert \
  -H 'Content-Type: application/json' \
  -d '{"drive": "mock0", "iso": "/isos/SOME_DISC.iso"}'
```

## Notes

- **Legal note (US)**: the DMCA's anti-circumvention provision technically
  covers CSS decryption even for personal backups of discs you own — a
  separate question from copyright/fair use itself. It's essentially never
  enforced against individuals doing personal backups, and the tools here
  (`libdvdcss`, `dvdbackup`) are packaged in most Linux distros, but worth
  knowing if you're outside a jurisdiction with a personal-backup exemption.
- **Disk space**: each rip needs roughly *two* discs' worth of space while
  running — the decrypted mirror (`WORK_DIR`) plus the final ISO — per drive,
  simultaneously. The supervisor checks before starting and refuses (with a
  UI callout + notification) if there isn't room.
- **Damaged/scratched discs**: `dvdbackup` retries reads on its own but isn't
  as tolerant as `ddrescue`. If a disc keeps failing, fall back to a manual
  `ddrescue` copy for that disc and decrypt it separately later.
- **Filenames**: volume label + timestamp (e.g. `MOVIE_TITLE_20260808_143012.iso`),
  or *your* title + that timestamp if you typed one into the drive card while the
  disc was ripping. Either way you can rename from the UI afterwards. The
  timestamp is always kept, so two rips of the same movie never collide;
  allowed characters are letters, digits, space and `._()[]'-`.

## Licence

GPL-3.0. The census and verification tools under `vendor/` are copied verbatim
from [MiSTer_DVD](https://github.com/owenb321/MiSTer_DVD), which is GPL-3.0, so
this project is too.

`dvdbackup` and `libdvdcss` are runtime dependencies, not bundled code. The
Docker image installs `libdvdcss` through Debian's `libdvd-pkg`, which *builds*
it locally at install time — the same route a Debian user takes by hand, and the
reason nothing proprietary or pre-built ships in this repository.
