"""Kunci satu instance bot per mesin: file lock eksklusif yang dilepas OS saat proses mati (termasuk crash),
jadi tidak ada file kunci basi. Instance di PC lain tidak terlihat dari sini: itu dideteksi lewat Telegram 409
(lihat Notifier.conflict_active)."""
from __future__ import annotations

import os
from pathlib import Path

EXIT_ALREADY_RUNNING = 3   # run_forever.ps1 tidak me-restart bot dengan kode ini


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.fh = None

    def acquire(self) -> None:
        """Raise AlreadyRunning bila proses lain memegang kunci yang sama."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            _lock(fh)
        except OSError:
            fh.close()
            raise AlreadyRunning(f"Bot lain sudah berjalan di mesin ini (kunci {self.path} dipegang proses lain)")
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self.fh = fh

    def release(self) -> None:
        if self.fh is not None:
            try:
                _unlock(self.fh)
            finally:
                self.fh.close()
                self.fh = None

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


if os.name == "nt":
    import msvcrt

    def _lock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
