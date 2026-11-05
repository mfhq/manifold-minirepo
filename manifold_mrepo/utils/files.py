# Copyright (C) 2026 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: MPL-2.0

import os
import errno
import shutil
import logging
from pathlib import Path

import zstandard

log = logging.getLogger(__name__)

# a repository compresses everything once and serves it many times, so we compress well
ZSTD_LEVEL = 19

_cross_device_warned = False


def zstd_compress(data: bytes) -> bytes:
    """Compress data with zstd. The same data always gives the same bytes, with the same zstd."""
    return zstandard.ZstdCompressor(level=ZSTD_LEVEL, write_checksum=True, write_content_size=True).compress(data)


def zstd_decompress(data: bytes, max_size: int = 64 * 1024 * 1024) -> bytes:
    """Decompress zstd data, refusing to produce more than max_size bytes."""
    return zstandard.ZstdDecompressor().decompress(data, max_output_size=max_size)


def link_or_copy(src: Path, dest: Path) -> None:
    """
    Hardlink a file to a new location, or copy it if the two are on different filesystems.
    """
    global _cross_device_warned

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dest)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        if not _cross_device_warned:
            log.warning(
                '%s is on a different filesystem than the files linked to it, so they are copied!',
                dest.parent,
            )
            _cross_device_warned = True
        shutil.copy2(src, dest)


def write_file(path: Path, data: bytes) -> None:
    """Write a file below a directory that is not public yet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def move_into_place(tmp_path: Path, path: Path) -> None:
    """Move a finished file to its name, which replaces a file of that name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.rename(tmp_path, path)


def rdns_dir(component_id: str) -> str:
    """
    The directory of the images of a component: its ID, split at the first three dots.
    `com.example.myapp.really.long` is stored in `com/example/myapp/really.long/`.
    """
    return '/'.join(component_id.split('.', 3))
