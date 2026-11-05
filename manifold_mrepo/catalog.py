# Copyright (C) 2026 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: MPL-2.0

"""
The AppStream catalog of a repository.

Every image of a bundle comes with a catalog of its own, with one component.
A channel of the repository publishes one catalog with the component of the
newest image of every component ID. We bring each component into the shape
it has in that catalog when we import it, so a catalog is only the
<component> elements of its images, joined together.
"""

import re
from dataclasses import dataclass
from xml.sax.saxutils import quoteattr

from manifold_mrepo.bindings import GLib, AppStream, AppStreamCompose
from manifold_mrepo.utils.hashing import hash_bytes

# the catalog format we write components in, and which the root element of a catalog names
CATALOG_FORMAT_VERSION = AppStream.FormatVersion.V1_2

# the start of the line with the <component> element, which is not the <components> one
_COMPONENT_START_RE = re.compile(r'^[ \t]*<component[\s>]', re.MULTILINE)
_COMPONENT_END = '</component>'


class CatalogError(Exception):
    """The catalog data of an image is not usable."""


@dataclass
class ComponentInfo:
    """What we need to know about the component of an image."""

    component_id: str
    bundle_id: str
    # the screenshots and videos of the component, relative to the media directory
    media_paths: list[str]
    # the <component> element, as the catalog of a repository holds it
    element: str


def _new_metadata() -> AppStream.Metadata:
    mdata = AppStream.Metadata()
    mdata.set_locale('ALL')
    mdata.set_format_style(AppStream.FormatStyle.CATALOG)
    mdata.set_format_version(CATALOG_FORMAT_VERSION)
    # media URLs stay as they are, relative to the media directory
    mdata.set_parse_flags(AppStream.ParseFlags.IGNORE_MEDIABASEURL)
    return mdata


def _media_items(component: AppStream.Component):
    """Every image and video of the screenshots of a component."""
    for screenshot in component.get_screenshots_all():
        yield from screenshot.get_images_all()
        yield from screenshot.get_videos_all()


def read_component(component_xml: str) -> ComponentInfo:
    """Read the catalog data of an image, and write its component the way the catalog of a repository holds it."""
    mdata = _new_metadata()
    try:
        mdata.parse_data(component_xml, -1, AppStream.FormatKind.XML)
    except GLib.Error as e:
        raise CatalogError('Unable to read the catalog data: {}'.format(e.message))
    components = mdata.get_components()
    if components.get_size() != 1:
        raise CatalogError('The catalog data has {} components instead of one'.format(components.get_size()))
    component = components.index_safe(0)

    bundle = component.get_bundle(AppStream.BundleKind.MANIFOLD)
    if bundle is None:
        raise CatalogError('The component {} has no Manifold bundle ID'.format(component.get_id()))

    # libmanifold only opens bundles whose screenshots are their own files, relative to the
    # media directory, which is how the catalog of a repository refers to them as well
    media_paths = []
    for item in _media_items(component):
        if item.get_url() not in media_paths:
            media_paths.append(item.get_url())

    return ComponentInfo(component.get_id(), bundle.get_id(), media_paths, _component_element(mdata))


def _component_element(mdata: AppStream.Metadata) -> str:
    """Write the one component of a catalog, as its <component> element alone."""
    catalog = mdata.components_to_catalog(AppStream.FormatKind.XML)
    start = _COMPONENT_START_RE.search(catalog)
    end = catalog.rfind(_COMPONENT_END)
    if start is None or end < start.start():
        raise CatalogError('AppStream wrote a catalog without a component')
    return catalog[start.start() : end + len(_COMPONENT_END)] + '\n'


def icon_dir(component_id: str, icons: dict[str, str]) -> str:
    """
    The directory of the icons of an image, below the media directory.

    Clients need icons by the names the catalog gives them, so we keep those names and
    put the icons into a directory that changes with them instead. We build it the way
    appstream-compose builds the directories of screenshots, from the component ID and
    a checksum, which is made from the icons here.

    :param icons: the SHA-256 hash of every icon, by its "<size>/<name>"
    """
    listing = ''.join('{} {}\n'.format(path, sha256) for path, sha256 in sorted(icons.items()))
    # like appstream-compose, we take the first 128 bits of a BLAKE3 hash
    checksum = hash_bytes(listing.encode()).blake3[:32]
    try:
        gcid = AppStreamCompose.build_component_global_id(component_id, checksum)
    except GLib.Error as e:
        raise CatalogError('Unable to make a directory for the icons of {}: {}'.format(component_id, e.message))
    return '{}/icons'.format(gcid)


def build_catalog(elements: list[str], origin: str, architecture: str, media_baseurl: str) -> bytes:
    """Write the catalog of a channel from the <component> elements of its newest images."""
    root = '<components version={} origin={} architecture={} media_baseurl={}>'.format(
        quoteattr(AppStream.format_version_to_string(CATALOG_FORMAT_VERSION)),
        quoteattr(origin),
        quoteattr(architecture),
        quoteattr(media_baseurl),
    )
    return ''.join(['<?xml version="1.0" encoding="utf-8"?>\n', root, '\n', *elements, '</components>\n']).encode()
