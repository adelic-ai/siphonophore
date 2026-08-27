#!/usr/bin/env python3
"""The fixed bootstrap runtime siphonophore-spawn exec's (contracts/spawn_helper.md, SH-24).

Runs already privilege-dropped, already cgroup-member, already environment-sanitized -- none of
that is this script's job. This script's only job is: read source/payload/nonce from the three
fixed fd numbers SH-24 pins, then execute the source. It never discovers fd layout, never reads
argv (SH-22), never touches a broker-named path.

Must be installed root-owned, not writable by the broker's own user, at the fixed absolute path
`siphonophore-spawn.c`'s BOOTSTRAP_SCRIPT constant names -- SH-26's hardening applies to this file
exactly as it applies to the helper binary itself: broker-influenced PYTHONPATH or cwd must never
reach the import/exec path here. See spawn_helper/README.md for install instructions.
"""
from __future__ import annotations

import json
import os

SOURCE_FD = 3
PAYLOAD_FD = 4
NONCE_FD = 5


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _nonce_fd_present() -> bool:
    try:
        os.fstat(NONCE_FD)
        return True
    except OSError:
        return False


def main() -> None:
    source = _read_all(SOURCE_FD).decode()
    os.close(SOURCE_FD)

    payload_bytes = _read_all(PAYLOAD_FD)
    os.close(PAYLOAD_FD)
    payload = json.loads(payload_bytes) if payload_bytes else {}

    # NONCE_FD, if the helper attached it, is deliberately left open and unread here -- reading it
    # is the artifact's own job (siphonophore_core.identity.read_nonce_from_fd(NONCE_FD)) if and
    # when it performs a check-in; consuming it in this bootstrap would make it unavailable for
    # that later step.
    nonce_fd = NONCE_FD if _nonce_fd_present() else None

    exec(compile(source, "<siphonophore-artifact>", "exec"), {"payload": payload, "NONCE_FD": nonce_fd})


if __name__ == "__main__":
    main()
