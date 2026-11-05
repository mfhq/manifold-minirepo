# Copyright (C) 2026 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: MPL-2.0

from functools import cmp_to_key

from manifold_mrepo.db import Image
from manifold_mrepo.bindings import AppStream


def compare_images(a: Image, b: Image) -> int:
    """Order two images of a component by AppStream's version comparison, then by revision."""
    result = AppStream.vercmp_simple(a.version, b.version)
    if result != 0:
        return result
    return (a.revision > b.revision) - (a.revision < b.revision)


def newest_first(images) -> list[Image]:
    """Sort the images of a component, the newest first."""
    return sorted(images, key=cmp_to_key(compare_images), reverse=True)
