# DVD Auto-Rip (`dvd_ripper`) — Claude Code Context

## Project Overview

A **headless DVD ripping appliance**: insert disc → decrypt + rip to an unencrypted
`.iso` → generate identifying sidecars → auto-eject → repeat, across any number of
simultaneously-attached optical drives, with a phone-friendly **Flask web UI** for
monitoring from the couch.

It is a companion to the [MiSTer_DVD](https://github.com/owenb321/MiSTer_DVD)
player core: that core plays **decrypted** ISOs (CSS decryption is deliberately a
PC-side rip step), and this tool is that rip step. It also vendors MiSTer_DVD's
`dvd_census.py` so the resulting library doubles as a **DVD feature/spec tracker**
feeding the core's conformance work.

User-facing docs live in `README.md` (install, config table, mock mode, notes).
**This file is for the agent: process, invariants, and the *why* behind decisions.**

---

## Documentation Discipline (read before starting work)

Every non-trivial design decision **must** be written down. Code without recorded
rationale is treated as incomplete.

- **Where to put it:**
  - `CLAUDE.md` (here) — durable, project-wide rules, conventions, invariants, and
    high-level decisions an agent needs *before* touching code.
  - **Module docstrings** — this project's primary detail-level documentation. Every
    `ripper/*.py` module opens with a docstring stating what it does and the
    non-obvious *why* (see `detect.py`'s udev rationale, `fingerprint.py`'s
    label-exclusion rationale). Keep that convention: a new module gets a real
    docstring, and an existing one gets updated when its behaviour changes.
  - `README.md` — anything the *user* needs to run/configure the thing (new env var,
    new endpoint, new deployment step). A feature that adds a knob but not its
    README row is unfinished.
  - `docs/` — create it only when a subject genuinely outgrows a docstring
    (e.g. a multi-module data flow or an FSM worth diagramming). Don't
    pre-create empty structure.

- **When to write it:** at the same time as the code, not "later." Commit docs
  together with the change that motivated them.

- **What to record:** the *why* behind each decision, known limitations / TODOs,
  design alternatives that were rejected (and why), and anything that surprised you
  or wasn't obvious from reading the code. **Traps deserve a comment at the trap**,
  not just in a commit message — see the libdvdcss block in the `Dockerfile` for the
  model (it explains three separate ways the build has silently produced a
  CSS-less image).

### Leave a trail to resume work in a new session

Sessions are stateless — the next session starts cold with only committed markdown
and docstrings to go on. Before finishing any feature:

- Update the relevant **status wording** here and in `README.md` (what's implemented,
  what's wired in, what isn't, what's deployed).
- Record the **next concrete step** so the following session knows where to start.
- List **known limitations** explicitly so they aren't rediscovered the hard way.
- Name the relevant files/modules so they're easy to locate.

### ★ Update status markers when a feature completes (mandatory — a stale marker is a bug)

Status wording written at branch-creation time and never updated when the PR merged
has misdirected whole sessions on the sibling MiSTer_DVD project. To prevent it here:

- **When you complete or merge a feature, update its status markers in the SAME change.**
- Flip the marker to reality: `🔧`/`❌`/"pending" → `✅ MERGED (PR #N)`, and once it's
  been run against real discs on the real host, `✅ HW-CONFIRMED`. If merged but not
  yet exercised on real hardware, say exactly that (`⏳ HW-confirm pending`).
- **Retire dead branch names.** A merged feature must not still point at a live
  `feature/*` branch in prose — replace it with the PR number.
- Treat a lingering "pending"/`feature/*` reference on shipped work as a documentation
  bug: fix it on sight. When in doubt, reconcile against
  `gh pr list --state merged` (what actually merged), not the branch name.

**"HW-confirmed" here means: real discs, in real drives, on the actual host** — not
mock mode, not a unit test. Mock mode proves the pipeline, not the drives, not
libdvdcss, not eject.

---

## Repository Structure

```
dvd_ripper/
├── CLAUDE.md              ← you are here (agent process + invariants)
├── README.md              ← user-facing: install, config, mock mode
├── Dockerfile             ← Debian + dvdbackup/libdvd-pkg/genisoimage/ffmpeg/flask
├── docker-compose.yml     ← the recommended deployment (edit devices: + DVD_DEVICES)
├── dvd-ripper.service     ← bare-metal systemd alternative
├── ripper/                ← THE APPLICATION (this repo owns all of it)
│   ├── main.py            ← entrypoint: `python3 -m ripper.main`
│   ├── config.py          ← env-var config (single source of defaults)
│   ├── detect.py          ← media detection via the udev DB (NOT open())
│   ├── drive.py           ← per-drive watcher thread + rip state machine
│   ├── fingerprint.py     ← VMG-IFO sha1 for duplicate detection
│   ├── screenshots.py     ← menu/title jpgs from VOB byte segments via ffmpeg
│   ├── census.py          ← runs the vendored census as a subprocess
│   ├── library.py         ← sidecar I/O, history cache, rename, dedup index
│   ├── backfill.py        ← sidecars for ISOs that predate this tool
│   ├── notify.py          ← optional Discord webhooks
│   ├── webapp.py          ← Flask API + static file serving
│   └── static/            ← phone-first UI (index.html, app.js, style.css)
└── vendor/                ← ⚠️ VERBATIM copies from MiSTer_DVD — do not edit
    ├── dvd_census.py      ← } copied from MiSTer_DVD tools/ @ e4a119f
    ├── dvd_vm_ref.py      ← }
    ├── iso_nav_check.py   ← }
    ├── nav_extract.py     ← }
    ├── census_run.py      ← the ONLY vendor/ file this repo owns (subprocess wrapper)
    └── README.md          ← the copy provenance + rule
```

---

## Toolchain

- **Language:** Python 3 (Debian bookworm's `python3`). The supervisor is
  **stdlib-only except Flask** — keep it that way; `notify.py` uses
  `urllib.request` rather than `requests` on purpose. `vendor/*` is pure stdlib.
- **Web:** Flask (`python3-flask` from apt, not pip) + hand-written HTML/CSS/JS.
  No build step, no npm, no framework.
- **External binaries** (all installed by the `Dockerfile`, all invoked as
  subprocesses): `dvdbackup` (CSS-decrypting mirror), `genisoimage` (mirror → ISO),
  `libdvdcss2` via `libdvd-pkg` (built at image-build time), `ffmpeg`
  (screenshots), `eject`, `bsdtar` from `libarchive-tools` (mock mode only).
- **Deployment:** Docker Compose on a **native Linux host** (Docker Desktop's VM
  can't see host optical drives). `dvd-ripper.service` is the bare-metal fallback.
- **Dev host note:** the dev machine (CachyOS) has no system Flask — host-side
  testing needs a venv (`python3 -m venv .venv && .venv/bin/pip install flask`).

---

## Key Architectural Decisions & Invariants

These are load-bearing. Breaking one produces a *silent* wrong result, not a crash.

### Never `open()` a drive to poll for media
`detect.py` reads the **host's udev database** (`/run/udev` bind-mounted read-only)
to ask whether a drive has DVD media. Some drive firmwares treat a bare `open()` as a
tray-close cue and can yank a misaligned disc mid-insert. The `dd` probe is only a
fallback for when no udev DB is visible.

### Fingerprint = sha1 of VMG IFO content, **label excluded**
`genisoimage` does not carry the volume label into the remastered ISO, so a
label-in-hash would break "disc at insert" vs "backfilled ISO" dedup. Capped at
`vmgi_last_sector` (VMGI_MAT@28) so trailing layout differences can't leak in. The
IFO area is never CSS-scrambled, so it reads identically from raw `/dev/srX` and from
the finished ISO. Round-trip (source ISO vs bsdtar + genisoimage remaster) verified
matching — re-verify it if you touch this.

### Screenshots never mount anything
A disc is reduced to byte **segments** (path, base_offset, length): plain VOB files in
the decrypted mirror during a rip, or VOB extents located inside the ISO (via the
vendored `IsoNav`) for backfill. MPEG program streams self-synchronize from any
2048-aligned offset, so bytes pipe straight into `ffmpeg -f mpeg -i pipe:0`. A raw
(still-scrambled) rip just yields fewer jpgs — that's expected, not a bug.

### `vendor/` is verbatim — fix upstream and re-copy
`dvd_census.py`, `dvd_vm_ref.py`, `iso_nav_check.py`, `nav_extract.py` are byte-copies
of MiSTer_DVD `tools/` @ commit `e4a119f`. **Never edit them in place** — a local
patch silently diverges the census schema from the core project that consumes it.
Fix in MiSTer_DVD, re-copy all four together (they sibling-import each other), and
update the commit hash in `vendor/README.md`. `census_run.py` is the only file here
this repo owns.

### The library is the filesystem — no database
Everything is derived by scanning `OUTPUT_DIR` for `*.iso` + sidecars, cached in
memory and invalidated on rip-complete/rename/backfill. Sidecar JSON carries an
explicit `schema` field; bump it rather than changing a field's meaning in place.
Rename must move the ISO **and every sidecar** together.

### ⚠️ Dockerfile: no `purge`/`autoremove` after the libdvd-pkg build
This trap has bitten **twice**. `libdvd-pkg` depends on `wget` + `build-essential`;
purging them cascades into removing `libdvd-pkg`, whose removal hook purges the
`libdvdcss2` it just built. Symptom: `libdvdread: Encrypted DVD support unavailable /
No css library available` — unencrypted discs rip fine, CSS discs silently produce
a scrambled image. Required shape of that `RUN` chain (all present today):
`dpkg-reconfigure libdvd-pkg` → `apt-get install -y --reinstall libdvd-pkg` (forces
the "postponed till after next APT operation" build hook) → `apt-mark manual
libdvdcss2` → **`ldconfig -p | grep -q libdvdcss` as the FINAL step** so a regression
fails the build loudly.

**Generalized lesson: check-then-clean ordering lies.** A mid-chain verification
passes and the very next command undoes it. Put verification last.

### A queued name is read once, as late as possible
The UI can queue the real movie name while a disc rips (`DriveWatcher.queue_name`,
`POST /api/name`). It is consumed at **FINALIZING** only (`_resolve_name`), never
earlier, because the rip is still running when the user types: every intermediate
artifact keeps the label-derived working base, the ISO is promoted from `.part`
directly into its final name (no multi-GB rename), and the only files renamed are
the jpgs. So a rip that is never named — or that fails — needs nothing undone, and
a name that loses a filename collision degrades to the working base instead of
leaving a half-renamed set. Two guards keep a name off the wrong disc: it is
**rejected unless the drive is in `NAMEABLE_STATES`** (never while IDLE) and it is
**cleared at the start of every cycle**. The `_<timestamp>` suffix is always
appended, so the queued name is *not* a free-form filename — uniqueness is
structural, not hoped for. `meta.named_by_user` records what was actually applied,
not what was typed.

### Abort is a request the watcher thread honours, never a thread kill
The abort button (`DriveWatcher.abort`, `POST /api/abort`) sets a
`threading.Event` and signals the running subprocess; the *watcher thread*
still owns the teardown (`_abort_step`), so an abort takes the same shape as a
failure — mirror removed, `.part` removed, screenshots removed, disc ejected,
nothing written to the library. Reasons this shape and not another:

- **Kill by process *group*** (`start_new_session=True` + `killpg`, SIGTERM
  then SIGKILL): dvdbackup/libdvdcss can leave children that would keep writing
  into the mirror we are about to delete. A disc read wedged in uninterruptible
  I/O may ignore both signals — it is signalled **once**, the UI keeps saying
  "aborting…", and the watcher completes when the kernel finally lets go.
- **`ABORTABLE_STATES` excludes FINALIZING**: past genisoimage the ISO exists
  and only cheap bookkeeping remains, so aborting there would destroy a
  finished deliverable. If the request loses that race the rip completes and an
  "abort arrived too late" event says so — it is never carried to the next disc
  (the flag, like the queued name, is cleared at the start of every cycle).
- **Screenshots are removed on abort** even though the ISO never existed: they
  are written into `OUTPUT_DIR` under the working base, and leaving them is the
  same orphan wart the genisoimage failure path is known for.
- **`aborted` is a separate status flag from the `ABORTED` state** because the
  state only lasts until the disc is removed, while the "no ISO was written"
  message must stay on the card (styled as a deliberate act, not a failure)
  until the next disc goes in.

### Both progress denominators are estimates the numerator can exceed
Neither progress bar is a real fraction, so `min(…, 1.0)` clamping means
**"100% and still working" is a normal state, not a hang**:

- **RIPPING** = mirror bytes / `isosize /dev/srX`, i.e. what the disc's own
  ISO9660 descriptor *claims*. Even on a healthy disc the mirror tops out
  around **99.98%** (dvdbackup copies files, not filesystem padding — measured
  3,375,609,856 of 3,376,183,296 on ELMOPALOOZA), which `Math.round` shows as
  100% for the last moments.
- **BUILDING_ISO** = ISO bytes / mirror file bytes, and the finished ISO is
  **always larger** than the files that went in (UDF + ISO9660 structures +
  `-dvd-video` padding — measured **+884,736 bytes** on the same disc), so this
  phase ends pinned at 100% by construction.

A *large, sustained* overrun means the disc under-reported itself. Observed
2026-08-08 on a real disc ("FLIGHT", /dev/sr2): 100% with **`-385s left`** at
3.7 MB/s ⇒ ~1.4 GB written past the declared 7.4 GiB, putting the mirror at
~8.7 GiB — **more than a DVD-9 physically holds**. That means overlapping or
padded VOB extents (an ARccOS/RipGuard-style layout) or dvdbackup reading into
garbage: the sum of file sizes legitimately exceeds the medium.

Consequences: the negative ETA was a real bug (now suppressed via the
`overrun` flag, with the UI switching to bytes written and a striped bar), and
**`SPACE_MARGIN` = disc size + 2 GB under-provisions scratch for such a disc**.

**That disc never finished** — which is a failure mode stall detection is blind
to: it read at full speed, it just had no end. Hence the second guard
(`OVERRUN_WARN_PCT` 110 / `OVERRUN_ABORT_PCT` 150): bound the rip by SIZE as
well as by rate. The two guards catch opposite shapes — "slow forever" and
"fast forever" — and both funnel into the same `abort()`. The abort threshold
also caps scratch use at 1.5x the disc, which is what keeps a runaway from
filling the work volume and taking the other drives down with it.

### A stall is "no healthy burst since X", not "the last sample was slow"
Auto-abort (`STALL_ABORT_MIN`, default 15 min) rides on the same `abort()` as
the button, so its teardown is identical — only the `reason` differs. What
matters is the *measurement*: `_run_with_progress` keeps a "last healthy point"
`(bytes, time)` and resets it whenever the average rate **since that point**
reaches `STALL_MBPS`. Stall duration is then just `now - ok_t`. Rejected
alternatives: the EMA speed (noisy, and a single 0-byte sample would trip it)
and a fixed byte delta per window (a knob nobody can reason about).

The thresholds are deliberately generous because two different situations look
identical on the meter: **"never got going"** (0.0-0.1 MB/s from the start —
doomed, aborting costs minutes) and **"stalled at 60%"** (dvdbackup retrying a
scratch, where the kernel can burn 10-30 s per bad sector and then recover —
aborting throws away real work). One rule covers both only if it is slow enough
that the second case survives; the first is obvious long before 15 min. The
"one good burst clears it" reset is what makes a recovering disc safe.

### ⚠️ Discord + urllib's default User-Agent = HTTP 403 "error code: 1010"
**This was the real reason Discord notifications never arrived** (diagnosed
2026-08-08 against a live webhook): Cloudflare, in front of Discord, blocks
requests carrying `Python-urllib/3.x`. Same URL, same payload — default agent
→ `403 error code: 1010`, `notify.USER_AGENT` → `204`. Because sends were
fire-and-forget, that 403 was invisible, so the feature looked "broken" rather
than "rejected". Never drop the `User-Agent` header from either request
builder (JSON *and* multipart), and **never conclude the webhook is fine
because `curl` works** — curl sends its own agent and sails through. The same
trap applies to any other stdlib HTTP client added here later.

### A silent notification is a bug — record every attempt
`Notifier` sends fire-and-forget, which used to mean a dead webhook, a kind
filtered out by `NOTIFY_EVENTS`, and a container with no egress were all
indistinguishable from "nothing happened yet". Every attempt now records
`{event, ok, detail}` (`/api/status` → `notify`), where **`ok=None` means
skipped by configuration** — a deliberate `NOTIFY_EVENTS` filter must not be
painted as a failure. `send_test()` / `POST /api/notify/test` run the same path
synchronously and return the real error, including Discord's response body
(401/404 = wrong or deleted webhook, 429 = rate limited). Keep that: the whole
point is that the UI can answer "did it send?" without a log dig.

### Rip pipeline never blocks on an optional step
Census runs as a **subprocess with a timeout** so a pathological image can't hang or
crash the supervisor; the sidecar is always written, with failures in its `error`
field. Notifications are fire-and-forget daemon threads. Screenshot failure loses
jpgs, not the rip. Keep that posture for anything new: the ISO is the deliverable.

---

## Testing

There is no unit-test suite; **mock mode is the test harness** and it runs the real
pipeline (bsdtar extraction stands in for `dvdbackup`):

```bash
MOCK_DRIVES=mock0 OUTPUT_DIR=/tmp/rips WORK_DIR=/tmp/rip-work python3 -m ripper.main

curl -X POST http://localhost:8080/api/mock/insert \
  -H 'Content-Type: application/json' \
  -d '{"drive": "mock0", "iso": "/path/to/SOME_DISC.iso"}'
```

Test ISOs live in **`$DVD_ISO_DIR/`** (decrypted DVD-Video rips shared with
the MiSTer_DVD project) — `MEN_IN_BLACK.iso`,
`THE_MATRIX_16X9LB_N_AMERICA.ISO`, `ULTIMATE_T2.iso`, `PAW_PATROL_MEET_EVEREST.iso`,
`SCENEIT_HP.iso` / `SCENEIT_JR.iso` / `Scene_It.iso`. `FAIRYTOPIA.iso` is the known
**still-CSS-scrambled** sample — useful for checking degraded-path behaviour.

What mock mode does **not** cover, and therefore what a real-disc test must confirm:
media detection, `dvdbackup`/libdvdcss on real CSS, eject, multi-drive concurrency,
disk-space refusal, and **abort against a drive that is actually stuck** (mock
mode kills a cooperative `bsdtar`; a real drive retrying a bad sector can sit in
uninterruptible I/O).

---

## Deploying / verifying after a feature

**Always deploy after completing a requested change** — don't leave the user to
trigger it:

```bash
docker compose up -d --build     # build AND restart
docker compose logs -f
```

⚠️ **`docker compose build` alone does not restart the running container.** A build
that "succeeds" while the old container keeps serving is a classic false pass — always
`up -d`. Then confirm the UI answers on `http://<host>:8080` before reporting done.

---

## Source control

**This repository is PUBLIC.** Everything pushed — code, comments, docs, commit
messages, PR titles and descriptions — is visible to anyone, permanently, and is
not meaningfully undone by a later commit.

### ★ Never publish personal or workstation-specific information

Applies to everything that lands in the repository or on the remote: code,
scripts, docs, `.gitignore`, commit messages, and PR text alike.

Never write:

- **Absolute paths from a development machine** — `/home/<user>/...`,
  `C:\Users\...`, `/Users/...`, or any path that resolves on one workstation.
- **Host, user, or account identifiers** — usernames, hostnames, e-mail
  addresses, self-hosted service URLs, LAN addresses, share names.
- **Secrets of any kind** — webhook URLs, tokens, API keys. `DISCORD_WEBHOOK_URL`
  is configuration, never a committed value; the example in `docker-compose.yml`
  stays commented and elided.

Write instead: paths relative to the repository root; environment variables with
generic fallbacks for anything outside it (`${DVD_ISO_DIR:-~/dvd-isos}`, not one
person's media library); generic placeholders in examples (`/dev/sr0`,
`<disc>.iso`, `<user>/<repo>`).

Two failure modes worth naming, because both were found here at publication
time and both had been sitting in the tree for months:

1. A self-hosted forge hostname baked into `ripper/notify.py`'s `USER_AGENT` —
   not merely documented, but **transmitted to Discord on every notification**.
   A string that looks like documentation can still be a network payload.
2. A hardcoded media path as `DEFAULT_DIR` in a vendored tool, which had
   already been scrubbed upstream — the stale copy silently reintroduced it.
   **Re-vendor from upstream rather than carrying an old snapshot.**

- **Never push a branch or open a PR until explicitly asked.** Work locally and
  commit freely; a feature branch is a private workspace until its author decides
  otherwise. Finishing the work, a green run, or a clean branch is not permission.
- **Never commit directly to `main`.** If `main` is checked out when a feature is
  requested, automatically create a feature branch (e.g. `feature/<short-description>`)
  before writing any code.
- Use PRs to merge feature branches into `main`.
- Commit often — after each logical, self-contained change (not just at the end).
- Write clear, descriptive commit messages that explain *what* changed and *why*.
- Keep `__pycache__/` out of commits (it's gitignored; it was committed once by
  mistake).

### Opening a PR after every feature branch

**Only when explicitly asked.** This repository is public: finishing the work, a
green test run, or a clean branch is not permission to publish it. Push the branch
first, then open the PR — the remote is **GitHub**, so use the `gh` CLI:

```bash
gh pr create --title "<title>" --base main --head <branch> --body-file /tmp/pr_body.md
```

Include a short summary and a markdown test plan checklist in the body. Write the
body to a file rather than passing `--body` inline — long markdown with backticks
and checklists does not survive shell quoting reliably. Present the returned PR URL
to the user.

### Merging a PR

```bash
gh pr merge <number> --merge        # or --squash / --rebase
```

### Updating a PR description

```bash
gh pr edit <number> --body-file /tmp/pr_body.md
```

To read a PR's current body back (e.g. to tick test-plan checkboxes):

```bash
gh pr view <number> --json body -q .body
```

---

## Status

- ✅ **Python supervisor + web UI rewrite — MERGED + HW-CONFIRMED 2026-08-08 (PR #1).**
  Replaced the previous two bash scripts (udev rule + `dvd-autorip.sh`) with one
  long-running supervisor. Confirmed on real hardware: multiple simultaneous SATA
  drives ripping CSS discs, after the libdvdcss purge fix above.
- ✅ **Queue a name while the disc rips — MERGED + HW-CONFIRMED 2026-08-08 (PR #3).**
  Name box on every active drive card → `POST /api/name` → applied at FINALIZING as
  `<name>_<timestamp>` (see the invariant above). Confirmed on the ripping machine
  (real disc, real drive, name typed mid-rip); also verified end-to-end in mock mode:
  named / renamed-mid-rip (last write wins) / no-name / collision-guard / IDLE
  rejection / bad-character rejection, plus the library `rename` path that now
  shares `library.validate_name`.
  UI note: the drives section is re-rendered every poll, so unsaved text lives in
  the JS `pendingNames` map and the re-render is *skipped* while a name box has
  focus (otherwise the caret — and the phone keyboard — is dropped every 2 s).
- ✅ **Abort & eject a doomed rip — MERGED + HW-CONFIRMED 2026-08-08 (PR #5).**
  Per-drive **Abort & eject** button → `POST /api/abort` → `DriveWatcher.abort`
  (see the invariant above), for the common case of a disc that reads at
  0.0-0.1 MB/s forever instead of failing. Verified end-to-end in mock mode:
  abort during DETECTED / RIPPING / BUILDING_ISO (partial ISO + all 4 jpgs +
  mirror removed, nothing in the library, drive re-armed), rejection while IDLE
  and on a double tap (409), unknown drive (404), a second drive ripping
  through its neighbour's abort untouched, and a normal rip completing
  unchanged. Then confirmed on the real host, which is what mock mode could not
  prove: killing `dvdbackup` mid-read releases the drive and `eject` opens the
  tray.
- ✅ **Discord notifications FIXED (root cause: Cloudflare 403/1010 on urllib's
  default User-Agent — see the invariant above). CONFIRMED against a live
  webhook 2026-08-08 (PR #5):** test message, `rip_complete` *with the menu jpg
  attached* (multipart path), `disc_stalled` and `rip_aborted` all delivered.
- ✅ **Automatic stall abort, runaway guard + notification self-check — MERGED +
  HW-CONFIRMED 2026-08-08 (PR #5).** Same branch. `STALL_MBPS`/`STALL_WARN_MIN`/`STALL_ABORT_MIN`
  (0.2 MB/s / 5 min / 15 min; see the two invariants above) plus a Discord
  status row + **Test notification** button (`POST /api/notify/test`) added
  because notification failures were previously invisible. Verified with two
  throwaway harnesses (not committed — recreate them if needed): a mock watcher
  whose `rip_cmd` is `sleep` (zero bytes forever ⇒ warn → ping → auto-abort →
  cleanup → eject, with sub-minute thresholds), and a local HTTP webhook
  stand-in serving `/ok` (204) and `/bad` (401 + a Discord-shaped error body)
  to prove both the success and failure surfacing. Plus the **runaway guard**
  (`OVERRUN_WARN_PCT` 110 / `OVERRUN_ABORT_PCT` 150), added because a real disc
  ("FLIGHT") read at full speed and never ended — see the progress-estimate
  invariant above for that story and the numbers.
- Known limitations / open items:
  - No auth on the web UI — **LAN only**, by design.
  - Damaged discs: `dvdbackup` retries but is less tolerant than `ddrescue`; the
    documented fallback is a manual `ddrescue` copy, decrypted separately.
  - Each concurrent rip needs ~2 discs' worth of scratch (mirror + ISO); the
    supervisor refuses to start a rip without room.
  - `vendor/` is pinned at MiSTer_DVD `e4a119f` — re-copy when the upstream census
    gains features worth tracking.
  - **Some discs make `genisoimage -dvd-video` refuse the mirror**: observed
    2026-08-08 in mock mode on `Harry Potter Interactive DVD Game (HOGWARTS
    CHALLENGE).iso` — *"Implementation botch. Video pad for file VTS_02_0.BUP is
    -34118 / Either the *.IFO file is bad or you found a mkisofs bug"*. The rip
    fails at BUILDING_ISO (state ERROR, stderr tail in the UI) and the screenshots
    already written are left orphaned next to no ISO. Not investigated; if it shows
    up on real discs the direction is to drop `-dvd-video` for the retry (it only
    controls DVD-Video sector padding/ordering) rather than to patch the mirror.

---

## References

- Sibling project (consumes these ISOs, source of `vendor/`):
  https://github.com/owenb321/MiSTer_DVD
- Local checkouts of `libdvdnav` / `libdvdread` for spec questions:
  keep them checked out beside this repo — grep them rather than guessing at
  DVD-Video structure semantics.
