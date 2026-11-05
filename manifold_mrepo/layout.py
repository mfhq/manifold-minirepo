# Copyright (C) 2026 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: MPL-2.0

"""
The layout of the public tree, see Manifold's repository format.

A location is a path from the root of the tree, the way the indexes give it.
"""

from manifold_mrepo.utils.files import rdns_dir

# the names that clients rely on
ROOT_INDEX_NAME = 'repo-index.json.zst'
ROOT_SIGNATURE_NAME = 'repo-index.sig'
COMPONENT_INDEX_NAME = 'index.json.zst'
CATALOG_NAME = 'components.xml.zst'
ICON_INDEX_NAME = 'icons.json.zst'

# the directory of the images and manifests of all channels and architectures
POOL_DIR = 'pool'


def pool_location(component_id: str, name: str) -> str:
    """The location of a file of an image in the pool."""
    return '{}/{}/{}'.format(POOL_DIR, rdns_dir(component_id), name)


def arch_location(channel: str, arch: str) -> str:
    """The location of the files of a channel for one architecture."""
    return '{}/{}'.format(channel, arch)


def media_location(channel: str, arch: str, path: str = '') -> str:
    """The location of a file below the media directory of a catalog."""
    location = '{}/media'.format(arch_location(channel, arch))
    return '{}/{}'.format(location, path) if path else location
