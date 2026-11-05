# Copyright (C) 2026 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: MPL-2.0

"""
Images that are served from somewhere else, like the release assets of a
GitHub project.

A channel can name a URL where clients look for images first. We only rely on
such a copy once we found the same file there: of the same size, and with the
same zchunk header, whose checksum covers the digest of every chunk.
"""

import os
import logging
import urllib.parse
import urllib.request
from pathlib import Path
from dataclasses import dataclass

from sqlalchemy import select

from manifold_mrepo import __version__
from manifold_mrepo.db import Image, utcnow
from manifold_mrepo.repo import Repository
from manifold_mrepo.bindings import GLib, Manifold

log = logging.getLogger(__name__)

USER_AGENT = 'manifold-mrepo/{}'.format(__version__)
TIMEOUT_SECONDS = 30


def expand_image_url(template: str, image: Image) -> str:
    """The URL of an image, made from the image URL template of its channel."""
    return (
        template.replace('{component}', urllib.parse.quote(image.component_id, safe=''))
        .replace('{version}', urllib.parse.quote(image.version, safe=''))
        .replace('{revision}', str(image.revision))
        .replace('{file}', image.zck_name)
    )


def asset_name(url: str) -> str:
    """The name of the file at a URL, which is what it has to be uploaded as."""
    return urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit('/', 1)[-1])


def _request(url: str, method: str = 'GET', headers: dict | None = None) -> urllib.request.Request:
    return urllib.request.Request(url, method=method, headers={'User-Agent': USER_AGENT, **(headers or {})})


def find_external_image(repo: Repository, url: str, image: Image) -> bool:
    """Check whether a URL serves the zchunk file of an image."""
    try:
        # hosts like GitHub redirect to a temporary location, which we send the range request to
        with urllib.request.urlopen(_request(url, 'HEAD'), timeout=TIMEOUT_SECONDS) as response:
            length = response.headers.get('Content-Length')
            location = response.geturl()
        if length is None or int(length) != image.zck_size:
            log.debug('%s does not have the size of %s', url, image.zck_name)
            return False

        headers = {'Range': 'bytes=0-{}'.format(image.zck_header_size - 1)}
        with urllib.request.urlopen(_request(location, headers=headers), timeout=TIMEOUT_SECONDS) as response:
            header = response.read(image.zck_header_size)
    except (OSError, ValueError) as e:
        log.debug('Unable to check %s: %s', url, e)
        return False
    if len(header) != image.zck_header_size:
        return False

    # zchunk reads the header itself, and computes its checksum the way it records it
    header_path = repo.tmp_dir / ('header-' + image.verity_hash)
    header_path.write_bytes(header)
    fd = os.open(header_path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        _, header_size, header_sha256 = Manifold.zchunk_read_header(fd)
    except GLib.Error as e:
        log.debug('%s does not start with a valid zchunk header: %s', url, e.message)
        return False
    finally:
        os.close(fd)
        header_path.unlink(missing_ok=True)

    return header_size == image.zck_header_size and header_sha256 == image.zck_header_sha256


def check_external_images(repo: Repository, session, *, recheck: bool = False) -> None:
    """
    Look for the images of every channel with an image URL at that URL. An image
    that was found once is not checked again, unless we are asked to.
    """
    now = utcnow()
    for channel in repo.config.channels.values():
        if channel.image_url is None:
            continue
        images = session.scalars(select(Image).where(Image.channel == channel.name, Image.removed_at.is_(None)))
        for image in images:
            url = expand_image_url(channel.image_url, image)
            if not recheck and image.external_url == url:
                continue
            found = find_external_image(repo, url, image)
            if found:
                log.info('Found %s at %s', image.zck_name, url)
            image.external_url = url if found else None
            image.external_checked_at = now


@dataclass
class ExternalImage:
    """An image of a channel with an image URL, and where it is located there."""

    image: Image
    url: str
    asset_name: str
    # the zchunk file in the public tree, which is gone once the image was found at its URL
    path: Path


def _external_images(repo: Repository, session, *, pending: bool) -> list[ExternalImage]:
    found = []
    for channel in repo.config.channels.values():
        if channel.image_url is None:
            continue
        query = select(Image).where(Image.channel == channel.name)
        if pending:
            query = query.where(Image.removed_at.is_(None), Image.external_url.is_(None))
        for image in session.scalars(query.order_by(Image.component_id, Image.version, Image.revision)):
            url = expand_image_url(channel.image_url, image)
            found.append(ExternalImage(image, url, asset_name(url), repo.public_path(image.zck_location)))
    return found


def find_pending_uploads(repo: Repository, session) -> list[ExternalImage]:
    """Find the images of channels with an image URL that are not found at that URL yet."""
    check_external_images(repo, session)
    return _external_images(repo, session, pending=True)


def find_external_files(repo: Repository, session) -> list[ExternalImage]:
    """
    Find the images that have to stay at the image URL of their channel: those the
    index lists, and those that left it but stay downloadable for their grace period.
    Any other file at that location is not needed by the repository.
    """
    return _external_images(repo, session, pending=False)
