"""Discord webhook notifications (optional, stdlib only).

Enabled when DISCORD_WEBHOOK_URL is set; individual event kinds are gated by
NOTIFY_EVENTS (comma list). Sends are fire-and-forget daemon threads —
failures never affect the rip pipeline.

Diagnosing "my notifications don't arrive": fire-and-forget used to mean a
failure existed only as a line on stdout, so a typo'd or revoked webhook, a
kind missing from NOTIFY_EVENTS, or no egress from the container all looked
identical from the UI — silence. So every attempt now records its outcome
(`last`, exposed on /api/status as `notify`), and `send_test()` runs the same
path SYNCHRONOUSLY and returns the real error, which is what the UI's "Test
notification" button and `POST /api/notify/test` call. Failure text comes
straight from urllib, including Discord's own response body: a 401/404 means
the webhook URL is wrong or deleted, a 429 means rate limited.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid

# ⚠️ THIS LINE IS WHY NOTIFICATIONS WORK AT ALL. Discord sits behind
# Cloudflare, which rejects urllib's default "Python-urllib/3.x" agent with
# HTTP 403 "error code: 1010". Measured against a real webhook 2026-08-08:
# default UA -> 403/1010, this UA -> 204. Nothing else about the request
# changed. Fire-and-forget meant that 403 was invisible, so notifications
# looked simply "broken" for the whole life of the tool. Never drop this
# header, and never assume a working curl proves the Python path works —
# curl sends its own UA.
USER_AGENT = "dvd-ripper (+https://github.com/owenb321/dvd_ripper)"

EVENT_EMOJI = {
    "rip_complete": "✅",
    "rip_aborted": "🛑",
    "disc_stalled": "🐌",
    "disc_error": "❌",
    "duplicate": "♻️",
    "disk_space_low": "⚠️",
    "backfill_done": "📚",
}


class Notifier:
    def __init__(self, cfg):
        self.url = cfg.discord_webhook_url
        self.events = cfg.notify_events
        self._lock = threading.Lock()
        self.last = None      # {event, ok, detail, time} of the last attempt

    def configured(self):
        return bool(self.url)

    def status(self):
        """What the UI shows in the notifications row."""
        with self._lock:
            last = dict(self.last) if self.last else None
        return {"configured": self.configured(),
                "events": sorted(self.events), "last": last}

    def notify(self, event, title, message, image_path=None):
        # not-configured / filtered-out are SKIPS, not failures: recording them
        # as failures would paint a deliberate NOTIFY_EVENTS setting red
        if not self.url:
            self._record(event, None, "skipped — no DISCORD_WEBHOOK_URL set")
            return
        if event not in self.events:
            self._record(event, None,
                         "skipped — '%s' not in NOTIFY_EVENTS" % event)
            return
        threading.Thread(target=self._send, daemon=True,
                         args=(event, title, message, image_path)).start()

    def send_test(self):
        """Synchronous end-to-end check. Returns (ok, detail) — never raises."""
        if not self.url:
            detail = "no DISCORD_WEBHOOK_URL set"
            self._record("test", False, detail)
            return False, detail
        return self._send("test", "Test notification",
                          "dvd-ripper is wired up correctly.")

    def _send(self, event, title, message, image_path=None):
        emoji = EVENT_EMOJI.get(event, "")
        payload = {"content": ("%s **%s**\n%s" % (emoji, title, message)).strip()}
        try:
            if image_path and os.path.isfile(image_path):
                req = self._multipart_request(payload, image_path)
            else:
                req = urllib.request.Request(
                    self.url, data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json",
                             "User-Agent": USER_AGENT})
            resp = urllib.request.urlopen(req, timeout=15)
            resp.read()
            return self._record(event, True, "HTTP %d" % resp.status)
        except urllib.error.HTTPError as e:
            # Discord explains itself in the body — 401/404 = bad or deleted
            # webhook URL, 429 = rate limited. Surfacing it saves a log dig.
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:300].strip()
            except Exception:
                pass
            return self._record(event, False, "HTTP %s %s%s"
                                % (e.code, e.reason, " — " + body if body else ""))
        except Exception as e:
            return self._record(event, False, repr(e))

    def _record(self, event, ok, detail):
        """ok: True sent, False failed, None skipped by configuration."""
        if ok is False:
            print("notify: discord send failed (%s): %s" % (event, detail))
        with self._lock:
            self.last = {"event": event, "ok": ok, "detail": detail,
                         "time": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime())}
        return ok, detail

    def _multipart_request(self, payload, image_path):
        boundary = uuid.uuid4().hex
        fname = os.path.basename(image_path)
        with open(image_path, "rb") as fh:
            img = fh.read()
        body = b""
        body += ("--%s\r\nContent-Disposition: form-data; "
                 "name=\"payload_json\"\r\n"
                 "Content-Type: application/json\r\n\r\n" % boundary).encode()
        body += json.dumps(payload).encode() + b"\r\n"
        body += ("--%s\r\nContent-Disposition: form-data; "
                 "name=\"files[0]\"; filename=\"%s\"\r\n"
                 "Content-Type: image/jpeg\r\n\r\n" % (boundary, fname)).encode()
        body += img + b"\r\n"
        body += ("--%s--\r\n" % boundary).encode()
        return urllib.request.Request(
            self.url, data=body,
            headers={"Content-Type":
                     "multipart/form-data; boundary=%s" % boundary,
                     "User-Agent": USER_AGENT})
