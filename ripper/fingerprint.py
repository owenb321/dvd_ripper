"""Disc fingerprinting for duplicate detection.

The fingerprint is sha1 over the VMG IFO (VIDEO_TS.IFO) content, capped at
1 MiB and at the IFO's own vmgi_last_sector length. The IFO area is never
CSS-scrambled, so it reads identically from the raw /dev/srX at insert time
and from the remastered ISO at backfill time — the volume label is NOT
included because genisoimage does not carry it over to the new image.
"""
import hashlib
import os
import struct
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor"))
from dvd_vm_ref import IsoNav  # noqa: E402

_CAP = 1 << 20  # 1 MiB


def fingerprint(path):
    """path = ISO file or /dev/srX. Returns 'v1:<sha1>' or None."""
    try:
        nav = IsoNav(path)
        if nav.vmgi_lba is None:
            return None
        mat = nav.sec(nav.vmgi_lba)
        vmgi_last = struct.unpack(">I", mat[28:32])[0]
        length = min((vmgi_last + 1) * 2048, _CAP) if vmgi_last else _CAP
        data = nav.rd(nav.vmgi_lba * 2048, length)
        nav.f.close()
        if not data:
            return None
        return "v1:" + hashlib.sha1(data).hexdigest()
    except Exception:
        return None


if __name__ == "__main__":
    for p in sys.argv[1:]:
        print(fingerprint(p), p)
