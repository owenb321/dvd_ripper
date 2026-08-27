"""Entrypoint: python3 -m ripper.main

Wires everything together: config from env, stale-mirror sweep, one watcher
thread per drive (real or mock), the backfill worker, and the Flask web UI.
"""
import os
import shutil
import sys

from .backfill import BackfillWorker
from .config import Config
from .drive import Board, DriveWatcher, MockDriveWatcher
from .library import Library
from .notify import Notifier
from .webapp import create_app


def main():
    cfg = Config.from_env()
    os.makedirs(cfg.output_dir, exist_ok=True)
    os.makedirs(cfg.work_dir, exist_ok=True)

    # sweep stale scratch mirrors left by a crash / power loss
    for entry in os.listdir(cfg.work_dir):
        path = os.path.join(cfg.work_dir, entry)
        if os.path.isdir(path):
            print("sweeping stale scratch dir:", path)
            shutil.rmtree(path, ignore_errors=True)
    # and a partial ISO from a crash mid-genisoimage
    for entry in os.listdir(cfg.output_dir):
        if entry.endswith(".iso.part"):
            path = os.path.join(cfg.output_dir, entry)
            print("removing partial ISO:", path)
            os.unlink(path)

    library = Library(cfg.output_dir)
    notifier = Notifier(cfg)
    board = Board(cfg, library, notifier)

    for dev in cfg.devices:
        if not os.path.exists(dev):
            print("warning: %s does not exist — check --device passthrough"
                  % dev, file=sys.stderr)
            continue
        board.watchers.append(DriveWatcher(cfg, dev, board))
    for name in cfg.mock_drives:
        board.watchers.append(MockDriveWatcher(cfg, name, board))

    if not board.watchers:
        print("warning: no drives configured (DVD_DEVICES/MOCK_DRIVES) — "
              "web UI + backfill only", file=sys.stderr)

    backfill_worker = BackfillWorker(cfg, board)
    backfill_worker.start()
    for w in board.watchers:
        w.start()

    print("dvd-ripper: %d drive(s), output %s, web UI on http://%s:%d"
          % (len(board.watchers), cfg.output_dir, cfg.host, cfg.port))
    app = create_app(cfg, board, backfill_worker)
    app.run(host=cfg.host, port=cfg.port, threaded=True)


if __name__ == "__main__":
    main()
