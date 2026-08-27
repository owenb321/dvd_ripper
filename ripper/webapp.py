"""Flask app: status/history API + static phone-first page. LAN only, no auth."""
import os

from flask import Flask, jsonify, request, send_from_directory


def create_app(cfg, board, backfill_worker):
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "static")
    app = Flask(__name__, static_folder=static_dir, static_url_path="/static")

    @app.get("/")
    def index():
        return send_from_directory(static_dir, "index.html")

    @app.get("/api/status")
    def status():
        return jsonify(board.status())

    @app.get("/api/history")
    def history():
        entries = []
        for e in board.library.history():
            entries.append({k: v for k, v in e.items() if k != "mtime"})
        return jsonify({"rips": entries})

    @app.get("/api/census/aggregate")
    def aggregate():
        return jsonify(board.library.aggregate())

    @app.post("/api/rename")
    def rename():
        data = request.get_json(silent=True) or {}
        old, new = data.get("name", ""), data.get("new_name", "")
        active = {w.status().get("name") for w in board.watchers}
        if old in active:
            return jsonify({"error": "a rip is in progress for that name"}), 409
        try:
            board.library.rename(old, new)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except OSError as e:
            return jsonify({"error": "rename failed: %s" % e}), 500
        board.events.add("rename", "web", "renamed %s -> %s" % (old, new))
        return jsonify({"ok": True})

    @app.post("/api/name")
    def queue_name():
        """Queue the real movie name for a disc that is still ripping."""
        data = request.get_json(silent=True) or {}
        dev, name = data.get("device", ""), data.get("name", "")
        for w in board.watchers:
            if w.dev != dev:
                continue
            try:
                w.queue_name(name)
            except ValueError as e:          # bad characters / empty
                return jsonify({"error": str(e)}), 400
            except RuntimeError as e:        # nothing rippable in that drive
                return jsonify({"error": str(e)}), 409
            return jsonify({"ok": True,
                            "planned_name": w.status().get("planned_name")})
        return jsonify({"error": "no such drive: %s" % dev}), 404

    @app.post("/api/abort")
    def abort_rip():
        """Give up on the disc in a drive: kill the rip, clean up, eject."""
        data = request.get_json(silent=True) or {}
        dev = data.get("device", "")
        for w in board.watchers:
            if w.dev != dev:
                continue
            try:
                w.abort()
            except RuntimeError as e:    # nothing abortable in that drive
                return jsonify({"error": str(e)}), 409
            return jsonify({"ok": True})
        return jsonify({"error": "no such drive: %s" % dev}), 404

    @app.post("/api/notify/test")
    def notify_test():
        """Send a test Discord message and report what actually happened."""
        ok, detail = board.notifier.send_test()
        board.events.add("notify_test" if ok else "error", "web",
                         "test notification: %s" % detail)
        return jsonify({"ok": ok, "detail": detail}), (200 if ok else 502)

    @app.post("/api/backfill")
    def backfill():
        backfill_worker.trigger()
        return jsonify({"ok": True})

    @app.get("/media/<path:name>")
    def media(name):
        if not name.endswith(".jpg"):
            return jsonify({"error": "not found"}), 404
        return send_from_directory(cfg.output_dir, name)

    if cfg.mock_drives:
        @app.post("/api/mock/insert")
        def mock_insert():
            data = request.get_json(silent=True) or {}
            drive, iso = data.get("drive", ""), data.get("iso", "")
            for w in board.watchers:
                if w.dev == drive and hasattr(w, "mock_iso"):
                    if not os.path.isfile(iso):
                        return jsonify({"error": "no such ISO: %s" % iso}), 400
                    w.mock_iso = iso
                    board.events.add("mock", drive, "mock disc inserted: %s" % iso)
                    return jsonify({"ok": True})
            return jsonify({"error": "no such mock drive: %s" % drive}), 400

    return app
