# Copyright (C) 2026 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: MPL-2.0

"""
Hashes of the files a repository lists.

Every file is listed with its size, its SHA-256 and its BLAKE3 hash.
"""

import hashlib
from pathlib import Path
from dataclasses import dataclass

import blake3

READ_BUFFER_SIZE = 1024 * 1024


@dataclass(frozen=True)
class FileHashes:
    """The size and hashes of a file, as a repository index lists them."""

    size: int
    sha256: str
    blake3: str

    def to_entry(self) -> dict:
        return {'size': self.size, 'sha256': self.sha256, 'blake3': self.blake3}


def hash_bytes(data: bytes) -> FileHashes:
    """Compute the size and hashes of some data."""
    return FileHashes(len(data), hashlib.sha256(data).hexdigest(), blake3.blake3(data).hexdigest())


def hash_file(path: Path) -> FileHashes:
    """Compute the size and hashes of a file, without reading all of it into memory."""
    sha256 = hashlib.sha256()
    b3 = blake3.blake3()
    size = 0
    with open(path, 'rb') as f:
        while chunk := f.read(READ_BUFFER_SIZE):
            sha256.update(chunk)
            b3.update(chunk)
            size += len(chunk)
    return FileHashes(size, sha256.hexdigest(), b3.hexdigest())
