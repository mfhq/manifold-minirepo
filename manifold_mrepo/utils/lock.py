# Copyright (C) 2020-2026 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: MPL-2.0

import os
import time
import fcntl
import logging
from pathlib import Path
from contextlib import contextmanager

log = logging.getLogger(__name__)


class LockError(Exception):
    """An error happened while acquiring a file lock."""


class ProcessFileLock:
    """
    Prevent several processes from working on the same repository at once, with a file lock.
    """

    def __init__(self, path: Path):
        self._path = path
        self._fd = -1

    def acquire(self, raise_error: bool = True) -> bool:
        """
        Try to acquire the lock.
        :param raise_error: True if we should raise an error, instead of just returning False if the lock is taken.
        :return: True if the lock was acquired.
        """
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, BlockingIOError):
            os.close(fd)
            if raise_error:
                raise LockError('Unable to acquire lock "{}": another process holds it.'.format(self._path))
            return False
        self._fd = fd
        return True

    def acquire_wait(self) -> None:
        if self.acquire(raise_error=False):
            return

        log.info('Waiting for another process to finish its work on this repository.')
        while not self.acquire(raise_error=False):
            time.sleep(2)

    def release(self) -> None:
        """Release an acquired lock. Does nothing if no lock was taken."""
        if self._fd < 0:
            return
        fcntl.lockf(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = -1


@contextmanager
def process_file_lock(path: Path, *, wait: bool = True):
    flock = ProcessFileLock(path)
    if wait:
        flock.acquire_wait()
    else:
        flock.acquire()
    try:
        yield flock
    finally:
        flock.release()
