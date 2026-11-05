# Copyright (C) 2026 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: MPL-2.0

"""
Publishing the repository.

We build the whole public tree next to the published one, from links to the
files of the published one, and then swap the two directories with one
rename. Readers see either the old tree or the new one, never a mix of both,
and the files that nothing needs any more go away with the old tree.
"""

import os
import json
import shutil
import logging
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from dataclasses import field, dataclass

from sqlalchemy import select

from manifold_mrepo.db import PUBLISH_STATE_ID, Image, PublishState, utcnow
from manifold_mrepo.repo import Repository, RepositoryError
from manifold_mrepo.config import ChannelConfig
from manifold_mrepo.layout import (
    CATALOG_NAME,
    ICON_INDEX_NAME,
    ROOT_INDEX_NAME,
    ROOT_SIGNATURE_NAME,
    COMPONENT_INDEX_NAME,
    arch_location,
    public_key_name,
)
from manifold_mrepo.catalog import build_catalog
from manifold_mrepo.bindings import GLib, Manifold
from manifold_mrepo.external import check_external_images
from manifold_mrepo.versions import newest_first
from manifold_mrepo.utils.files import (
    write_file,
    link_or_copy,
    zstd_compress,
    zstd_decompress,
)
from manifold_mrepo.utils.sshsig import SignatureError, public_key_of_signature
from manifold_mrepo.utils.hashing import hash_bytes
from manifold_mrepo.utils.renameat2 import exchange_paths

log = logging.getLogger(__name__)

STAGING_SUFFIX = '.new'


def rfc3339(time: datetime) -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ')


def _json_bytes(data: dict) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode()


@dataclass
class PublishResult:
    """What a publication did."""

    published: bool = False
    architectures: list[str] = field(default_factory=list)
    # images that left the index because only the newest versions are kept
    retired: list[str] = field(default_factory=list)
    # images whose files were deleted, once their grace period was over
    deleted: list[str] = field(default_factory=list)


def apply_retention(repo: Repository, session, now: datetime) -> list[str]:
    """
    Remove all but the newest versions of every component from the index.
    We never remove an image that a kept image builds on.
    """
    keep_versions = repo.config.keep_versions
    groups = defaultdict(list)
    for image in session.scalars(select(Image).where(Image.removed_at.is_(None))):
        groups[(image.channel, image.architecture, image.component_id)].append(image)

    kept = set()
    candidates = []
    for images in groups.values():
        ordered = newest_first(images)
        kept.update(image.verity_hash for image in ordered[:keep_versions])
        candidates.extend(ordered[keep_versions:])

    # an image that a kept image builds on is kept too, and so is its own base: the exact
    # image it was built on if we have that, or else the newest image that provides its identity
    active = [image for images in groups.values() for image in images]
    by_content = {(image.architecture, image.content_hash): image for image in active}
    by_identity = defaultdict(list)
    for image in active:
        by_identity[(image.architecture, image.identity)].append(image)
    pending = [image for image in active if image.verity_hash in kept]
    while pending:
        image = pending.pop()
        if image.base_identity is None:
            continue
        base = by_content.get((image.architecture, image.base_content_hash))
        if base is None:
            providers = by_identity.get((image.architecture, image.base_identity))
            base = newest_first(providers)[0] if providers else None
        if base is not None and base.verity_hash not in kept:
            kept.add(base.verity_hash)
            pending.append(base)

    retired = []
    for image in candidates:
        if image.verity_hash in kept:
            continue
        image.removed_at = now
        repo.db.mark_dirty(session)
        retired.append(image.describe())
    return retired


def _expired_before(repo: Repository, now: datetime) -> datetime:
    return now - timedelta(days=repo.config.grace_days)


def _publish_needed(repo: Repository, session, now: datetime) -> bool:
    for name in (ROOT_INDEX_NAME, public_key_name(repo.config.id)):
        if not repo.public_path(name).exists():
            return True
    state = session.get(PublishState, PUBLISH_STATE_ID)
    if state is None or state.dirty:
        return True
    # a new root index goes out before half of the validity of the current one has passed
    if now >= state.date + (state.valid_until - state.date) / 2:
        return True
    # files whose grace period is over leave the tree
    expired = session.scalars(select(Image).where(Image.removed_at <= _expired_before(repo, now))).first()
    return expired is not None


def _published_serial(public_dir: Path) -> int:
    """The serial of the root index that is published now, which may be newer than our records."""
    path = public_dir / ROOT_INDEX_NAME
    if not path.exists():
        return 0
    try:
        return int(json.loads(zstd_decompress(path.read_bytes()))['serial'])
    except Exception as e:
        log.warning('Unable to read the serial of %s: %s', path, e)
        return 0


class _TreeBuilder:
    """Builds the public tree of the repository in a staging directory."""

    def __init__(self, repo: Repository, session, staging_dir: Path, now: datetime):
        self.repo = repo
        self.config = repo.config
        self.session = session
        self.staging_dir = staging_dir
        self.now = now
        # the architectures that some channel has images for
        self.architectures = set()

    def _link(self, location: str) -> None:
        """Take a file of the published tree over into the new one."""
        dest = self.staging_dir / location
        if dest.exists():
            # images can share icons and screenshots
            return
        src = self.repo.public_path(location)
        if not src.exists():
            raise RepositoryError('{} is missing from the public tree'.format(src))
        link_or_copy(src, dest)

    def _write_files(self, directory: Path, files: dict[str, bytes]) -> dict:
        """Write files, and return their file entries by name."""
        entries = {}
        for name, data in files.items():
            write_file(directory / name, data)
            entries[name] = hash_bytes(data).to_entry()
        return entries

    def _version_entry(self, image: Image) -> dict:
        return {
            'bundle': image.bundle_id,
            'version': image.version,
            'revision': image.revision,
            'manifest': {
                'location': image.manifest_location,
                'size': image.manifest_size,
                'sha256': image.manifest_sha256,
                'blake3': image.manifest_blake3,
            },
            'zck-header': {'size': image.zck_header_size, 'sha256': image.zck_header_sha256},
        }

    def _take_over_files(self, images: list[Image]) -> None:
        """
        Take the files of images over into the new tree: their images and manifests,
        and their icons and screenshots, as the media directories keep those of every
        version so an older version can become the newest again.
        """
        for image in images:
            self._link(image.manifest_location)
            for catalog_file in image.catalog_files:
                self._link(catalog_file.location)

            channel = self.config.channels.get(image.channel)
            if channel is not None and channel.omit_local_images and image.external_url is not None:
                continue
            if not self.repo.public_path(image.zck_location).exists():
                raise RepositoryError(
                    'The image of {} is missing from the public tree, and was not found at its external URL'.format(
                        image.describe()
                    )
                )
            self._link(image.zck_location)

    def _build_catalog(self, channel: ChannelConfig, arch: str, newest: list[Image]) -> dict[str, bytes]:
        """Make the catalog files of the newest images."""
        catalog = build_catalog(
            [image.component_xml for image in newest],
            self.config.origin(channel.name),
            arch,
            self.config.media_baseurl(channel.name, arch),
        )

        # clients store an icon by the name it is listed by, which is the name the catalog gives it
        icons = {}
        for image in newest:
            for catalog_file in image.catalog_files:
                if catalog_file.kind != 'icon':
                    continue
                icons[catalog_file.path] = {
                    'location': catalog_file.location,
                    'size': catalog_file.size,
                    'sha256': catalog_file.sha256,
                    'blake3': catalog_file.blake3,
                }

        return {
            CATALOG_NAME: zstd_compress(catalog),
            ICON_INDEX_NAME: zstd_compress(_json_bytes(dict(sorted(icons.items())))),
        }

    def _build_architecture(self, channel: ChannelConfig, arch: str, images: list[Image]) -> dict:
        """Write the files of a channel for one architecture, and return their file entries."""
        groups = defaultdict(list)
        for image in images:
            groups[image.component_id].append(image)
        ordered = {cid: newest_first(groups[cid]) for cid in sorted(groups)}

        components = {
            cid: {'versions': [self._version_entry(image) for image in versions]} for cid, versions in ordered.items()
        }
        files = {COMPONENT_INDEX_NAME: zstd_compress(_json_bytes({'components': components}))}
        files.update(self._build_catalog(channel, arch, [versions[0] for versions in ordered.values()]))
        self.architectures.add(arch)
        return self._write_files(self.staging_dir / arch_location(channel.name, arch), files)

    def build_root_index(self, serial: int, valid_until: datetime) -> bytes:
        """Build the whole tree, and return its root index."""
        images = self.session.scalars(select(Image)).all()
        active = [image for image in images if image.is_active]
        # removed images stay downloadable for a while, for clients that have an older index
        expired_before = _expired_before(self.repo, self.now)
        lingering = [image for image in images if not image.is_active and image.removed_at > expired_before]
        self._take_over_files(active + lingering)

        channels = {}
        for channel in self.config.channels.values():
            by_arch = defaultdict(list)
            for image in active:
                if image.channel == channel.name:
                    by_arch[image.architecture].append(image)

            channel_entry = {}
            if channel.image_url_prefix is not None:
                channel_entry['image-url-prefix'] = channel.image_url_prefix
            channel_entry['architectures'] = {
                arch: self._build_architecture(channel, arch, by_arch[arch]) for arch in sorted(by_arch)
            }
            channels[channel.name] = channel_entry

        root = {'id': self.config.id, 'summary': self.config.summary}
        if self.config.publisher_name is not None:
            publisher = {'name': self.config.publisher_name}
            if self.config.publisher_email is not None:
                publisher['email'] = self.config.publisher_email
            root['publisher'] = publisher
        root['serial'] = serial
        root['date'] = rfc3339(self.now)
        root['valid-until'] = rfc3339(valid_until)
        root['channels'] = channels
        return _json_bytes(root)


def _swap_into_place(staging_dir: Path, public_dir: Path) -> None:
    if public_dir.exists():
        exchange_paths(staging_dir, public_dir)
        # the staging directory holds the old tree now
        shutil.rmtree(staging_dir)
    else:
        public_dir.parent.mkdir(parents=True, exist_ok=True)
        os.rename(staging_dir, public_dir)


def forget_expired_images(repo: Repository, now: datetime) -> list[str]:
    """
    Forget the images whose grace period is over. A publication already left
    their files out of the tree.
    """
    deleted = []
    with repo.db.session() as session:
        for image in session.scalars(select(Image).where(Image.removed_at <= _expired_before(repo, now))):
            deleted.append(image.describe())
            session.delete(image)
    return deleted


def publish(repo: Repository, *, force: bool = False, recheck_external: bool = False) -> PublishResult:
    """
    Publish the repository, if anything changed, or if the published indexes
    are about to expire.
    """
    config = repo.config
    public_dir = config.public_dir
    staging_dir = public_dir.parent / (public_dir.name + STAGING_SUFFIX)
    result = PublishResult()
    now = utcnow()

    repo.check_public_dir()

    with repo.db.session() as session:
        result.retired = apply_retention(repo, session, now)
        if not force and not _publish_needed(repo, session, now):
            return result

        check_external_images(repo, session, recheck=recheck_external)

        signer = repo.signer()
        state = session.get(PublishState, PUBLISH_STATE_ID)
        previous = max(state.serial if state is not None else 0, _published_serial(public_dir))
        serial = max(previous + 1, int(now.timestamp()))
        valid_until = now + timedelta(days=config.valid_days)
        shutil.rmtree(staging_dir, ignore_errors=True)
        staging_dir.mkdir(parents=True)
        builder = _TreeBuilder(repo, session, staging_dir, now)

        try:
            root_index = zstd_compress(builder.build_root_index(serial, valid_until))
            try:
                signature = signer.sign(Manifold.SignatureScope.REPO, GLib.Bytes.new(root_index))
            except GLib.Error as e:
                raise RepositoryError('Unable to sign the root index: {}'.format(e.message))
            # we take the public key from the signature, as we do not know it for every kind of key
            try:
                public_key = public_key_of_signature(signature.get_data())
            except SignatureError as e:
                raise RepositoryError('Unable to read the key from the signature of the root index: {}'.format(e))
            write_file(staging_dir / ROOT_INDEX_NAME, root_index)
            write_file(staging_dir / ROOT_SIGNATURE_NAME, signature.get_data())
            write_file(staging_dir / public_key_name(config.id), (public_key + '\n').encode('ascii'))
            _swap_into_place(staging_dir, public_dir)
        except BaseException:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

        if state is None:
            state = PublishState(id=PUBLISH_STATE_ID)
            session.add(state)
        state.serial = serial
        state.date = now
        state.valid_until = valid_until
        state.dirty = False

        result.published = True
        result.architectures = sorted(builder.architectures)

    result.deleted = forget_expired_images(repo, now)
    return result
