# Copyright (C) 2026 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: MPL-2.0

"""
Importing bundles into the repository.

Bundles arrive in the incoming directory, which works as a queue: a bundle
that was imported is removed from it, and one that was rejected is moved to
`rejected/`, with a file that says why.
"""

import os
import hashlib
import logging
from pathlib import Path
from dataclasses import field, dataclass

from sqlalchemy import select

from manifold_mrepo.db import Image, Bundle, CatalogFile, utcnow
from manifold_mrepo.repo import Repository
from manifold_mrepo.config import MAIN_CHANNEL
from manifold_mrepo.layout import pool_location, media_location
from manifold_mrepo.catalog import CatalogError, ComponentInfo, icon_dir, read_component
from manifold_mrepo.bindings import GLib, Manifold, component_kind_name
from manifold_mrepo.utils.files import write_file, zstd_compress, move_into_place
from manifold_mrepo.utils.hashing import FileHashes, hash_file, hash_bytes

log = logging.getLogger(__name__)

BUNDLE_SUFFIX = '.manifold'
REJECTED_DIR = 'rejected'
REASON_SUFFIX = '.reason'


class ImportRejected(Exception):
    """A bundle can not be imported, for a reason we tell whoever uploaded it."""


class MissingBase(ImportRejected):
    """A bundle needs a base that the repository does not have, which another bundle may still bring."""


@dataclass
class ImportResult:
    """What an import did."""

    # the bundles that were imported
    imported: list[str] = field(default_factory=list)
    # the bundles that were rejected, with the reason
    rejected: dict[str, str] = field(default_factory=dict)
    # the images that were added, as "<component> <version>-<revision> (<architecture>)"
    added: list[str] = field(default_factory=list)


@dataclass
class _IncomingImage:
    """An image of a bundle, with what we read from its manifest and catalog data."""

    image: Manifold.BundleImage
    manifest: Manifold.Manifest
    component: ComponentInfo

    @property
    def verity_hash(self) -> str:
        return self.manifest.get_verity_hash()

    @property
    def architecture(self) -> str:
        return self.manifest.get_architecture()

    @property
    def component_id(self) -> str:
        return self.manifest.get_component_id()

    @property
    def identity(self) -> str:
        return self.manifest.get_identity()

    @property
    def version(self) -> str:
        return self.manifest.get_version()

    @property
    def revision(self) -> int:
        return self.manifest.get_revision()

    @property
    def kind(self) -> str:
        return component_kind_name(self.manifest.get_kind())

    def describe(self) -> str:
        return '{} {}-{} ({})'.format(self.component_id, self.version, self.revision, self.architecture)


def find_incoming_bundles(repo: Repository) -> list[Path]:
    """
    Find the bundles waiting in the incoming directory. Files whose name starts
    with a dot are uploads that are not complete yet.
    """
    return sorted(
        p
        for p in repo.config.incoming_dir.iterdir()
        if p.is_file() and p.name.endswith(BUNDLE_SUFFIX) and not p.name.startswith('.')
    )


def _sha256_of_file(path: Path) -> str:
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def _read_image(image: Manifold.BundleImage) -> _IncomingImage:
    manifest = image.get_manifest()
    try:
        component = read_component(image.get_catalog().get_data().decode('utf-8'))
    except CatalogError as e:
        raise ImportRejected('{}: {}'.format(manifest.get_component_id(), e))

    incoming = _IncomingImage(image, manifest, component)
    expected_bundle_id = '{}/{}/{}'.format(incoming.architecture, incoming.component_id, incoming.version)
    if component.bundle_id != expected_bundle_id:
        raise ImportRejected(
            'The catalog data of {} names the bundle {} instead of {}'.format(
                incoming.describe(), component.bundle_id, expected_bundle_id
            )
        )
    return incoming


def _check_bases(session, incoming_images: list[_IncomingImage]) -> None:
    """
    Check that we have the base of every image, in the bundle or in the repository,
    found by its identity like Manifold does. We only check kits and applications,
    as cores and platforms may as well come from another repository.
    """
    in_bundle = {incoming.identity for incoming in incoming_images}
    for incoming in incoming_images:
        manifest = incoming.manifest
        base_kind = manifest.get_base_kind()
        base_identity = manifest.get_base_identity()
        if base_identity is None or base_kind in (Manifold.ComponentKind.CORE, Manifold.ComponentKind.PLATFORM):
            continue
        if base_identity in in_bundle:
            continue
        provider = session.scalars(
            select(Image).where(
                Image.channel == MAIN_CHANNEL,
                Image.architecture == incoming.architecture,
                Image.identity == base_identity,
                Image.removed_at.is_(None),
            )
        ).first()
        if provider is None:
            raise MissingBase(
                '{} builds on the {} {} {}, which is neither in the bundle nor in the repository'.format(
                    incoming.describe(),
                    component_kind_name(base_kind),
                    manifest.get_base_id(),
                    manifest.get_base_version(),
                )
            )


def _check_against_repository(session, incoming: _IncomingImage) -> list[Image]:
    """
    Check that an image fits into the repository.

    :return: the images of older revisions of the same version, which the image replaces
    """

    # an ID names exactly one kind of component, everywhere in the repository
    other_kind = session.scalars(
        select(Image).where(
            Image.component_id == incoming.component_id,
            Image.removed_at.is_(None),
            (Image.kind != incoming.kind) | (Image.sdk != incoming.manifest.is_sdk()),
        )
    ).first()
    if other_kind is not None:
        raise ImportRejected(
            '{} is in the repository as a different kind of component already'.format(incoming.component_id)
        )

    replaced = []
    same_version = session.scalars(
        select(Image).where(
            Image.channel == MAIN_CHANNEL,
            Image.architecture == incoming.architecture,
            Image.component_id == incoming.component_id,
            Image.version == incoming.version,
            Image.removed_at.is_(None),
            Image.verity_hash != incoming.verity_hash,
        )
    ).all()
    for other in same_version:
        if other.revision == incoming.revision:
            raise ImportRejected('{} is in the repository already, as a different image'.format(incoming.describe()))
        if other.revision > incoming.revision:
            raise ImportRejected(
                'The repository has the newer revision {} of {} {} already'.format(
                    other.revision, incoming.component_id, incoming.version
                )
            )
        replaced.append(other)

    return replaced


def _store_file(repo: Repository, tmp_path: Path, location: str) -> FileHashes:
    """
    Move a file of a catalog to its location in the public tree. A file that is
    there already stays, as a published file never changes, so we return the
    hashes of the file that is there.
    """
    path = repo.public_path(location)
    if path.exists():
        tmp_path.unlink()
        return hash_file(path)
    hashes = hash_file(tmp_path)
    move_into_place(tmp_path, path)
    return hashes


def _store_image(repo: Repository, bundle: Manifold.Bundle, incoming: _IncomingImage) -> dict:
    """
    Put the files of an image into the public tree: the image as a zchunk file
    and its manifest into the pool, its icons and its screenshots into the media
    directory of its catalog. Nothing refers to them before the next publication.

    :return: what the database records about the image
    """
    verity_hash = incoming.verity_hash
    component_id = incoming.component_id
    arch = incoming.architecture
    raw_path = repo.tmp_dir / (verity_hash + '.raw')
    zck_tmp_path = repo.tmp_dir / (verity_hash + '.raw.zck')
    image_file = incoming.manifest.get_image_file()

    # copy the image out of the bundle, which verifies it, and write it as a zchunk file
    log.info('Writing %s as a zchunk file', image_file)
    raw_fd = os.open(raw_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
    try:
        bundle.copy_image(incoming.image, raw_fd, None, None)
        zck_fd = os.open(zck_tmp_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o644)
        try:
            Manifold.zchunk_write_image(raw_fd, zck_fd)
            # what clients download has to decompress to exactly the image of the manifest
            Manifold.zchunk_check_image(zck_fd, incoming.manifest)
            _, header_size, header_sha256 = Manifold.zchunk_read_header(zck_fd)
            os.fsync(zck_fd)
            zck_size = os.fstat(zck_fd).st_size
        finally:
            os.close(zck_fd)
    except GLib.Error as e:
        zck_tmp_path.unlink(missing_ok=True)
        raise ImportRejected('{}: {}'.format(image_file, e.message))
    finally:
        os.close(raw_fd)
        raw_path.unlink(missing_ok=True)
    move_into_place(zck_tmp_path, repo.public_path(pool_location(component_id, verity_hash + '.raw.zck')))

    # the manifest stays the same file as in the bundle, only compressed
    manifest_data = zstd_compress(incoming.manifest.get_data().get_data())
    manifest_tmp_path = repo.tmp_dir / incoming.verity_hash
    write_file(manifest_tmp_path, manifest_data)
    move_into_place(manifest_tmp_path, repo.public_path(pool_location(component_id, verity_hash + '.json.zst')))
    manifest_hashes = hash_bytes(manifest_data)

    catalog_files = []
    icons = {path: data.get_data() for path, data in incoming.image.get_icons().items()}
    if icons:
        try:
            icons_dir = icon_dir(component_id, {path: hash_bytes(data).sha256 for path, data in icons.items()})
        except CatalogError as e:
            raise ImportRejected(str(e))
    for path, data in sorted(icons.items()):
        icon_tmp_path = repo.tmp_dir / ('icon-' + verity_hash)
        write_file(icon_tmp_path, data)
        location = media_location(MAIN_CHANNEL, arch, '{}/{}'.format(icons_dir, path))
        hashes = _store_file(repo, icon_tmp_path, location)
        catalog_files.append(CatalogFile(kind='icon', path=path, location=location, **hashes.to_entry()))

    for path in incoming.component.media_paths:
        media_tmp_path = repo.tmp_dir / ('media-' + verity_hash)
        fd = os.open(media_tmp_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o644)
        try:
            bundle.copy_media_file(path, fd)
            os.fsync(fd)
        except GLib.Error as e:
            raise ImportRejected('{}: {}'.format(path, e.message))
        finally:
            os.close(fd)
        location = media_location(MAIN_CHANNEL, arch, path)
        hashes = _store_file(repo, media_tmp_path, location)
        catalog_files.append(CatalogFile(kind='media', path=path, location=location, **hashes.to_entry()))

    return dict(
        manifest_size=manifest_hashes.size,
        manifest_sha256=manifest_hashes.sha256,
        manifest_blake3=manifest_hashes.blake3,
        zck_size=zck_size,
        zck_header_size=header_size,
        zck_header_sha256=header_sha256,
        catalog_files=catalog_files,
    )


def _import_bundle(repo: Repository, path: Path, keys: Manifold.TrustedKeys | None, result: ImportResult) -> None:
    """Import the images of a bundle, all of them or none."""

    try:
        bundle = Manifold.Bundle.new_from_path(str(path))
    except GLib.Error as e:
        raise ImportRejected(e.message)

    signer = None
    if keys is not None:
        try:
            _, signer = bundle.verify_signature(keys)
        except GLib.Error as e:
            raise ImportRejected(e.message)

    incoming_images = [_read_image(image) for image in bundle.get_images()]
    now = utcnow()
    added = []

    with repo.db.session() as session:
        _check_bases(session, incoming_images)
        bundle_row = Bundle(file_name=path.name, sha256=_sha256_of_file(path), signer=signer, imported_at=now)
        session.add(bundle_row)

        for incoming in incoming_images:
            replaced = _check_against_repository(session, incoming)
            existing = session.get(Image, incoming.verity_hash)
            if existing is not None and existing.is_active:
                log.info('%s is in the repository already', incoming.describe())
                continue

            if existing is not None:
                # an image that was removed comes back, and its files are still in the pool
                existing.removed_at = None
                existing.bundle = bundle_row
            else:
                stored = _store_image(repo, bundle, incoming)
                session.add(
                    Image(
                        verity_hash=incoming.verity_hash,
                        channel=MAIN_CHANNEL,
                        architecture=incoming.architecture,
                        component_id=incoming.component_id,
                        kind=incoming.kind,
                        sdk=incoming.manifest.is_sdk(),
                        version=incoming.version,
                        revision=incoming.revision,
                        identity=incoming.identity,
                        content_hash=incoming.manifest.get_content_hash(),
                        base_identity=incoming.manifest.get_base_identity(),
                        base_content_hash=incoming.manifest.get_base_content_hash(),
                        bundle_id=incoming.component.bundle_id,
                        component_xml=incoming.component.element,
                        added_at=now,
                        bundle=bundle_row,
                        **stored,
                    )
                )

            for old in replaced:
                log.info('%s replaces revision %s', incoming.describe(), old.revision)
                old.removed_at = now
            repo.db.mark_dirty(session)
            added.append(incoming.describe())

    result.added.extend(added)


def _consume(repo: Repository, path: Path) -> None:
    processed_dir = repo.config.processed_dir
    if processed_dir is None:
        path.unlink()
        return
    processed_dir.mkdir(parents=True, exist_ok=True)
    os.replace(path, processed_dir / path.name)


def _reject(repo: Repository, path: Path, reason: str) -> None:
    rejected_dir = repo.config.incoming_dir / REJECTED_DIR
    rejected_dir.mkdir(parents=True, exist_ok=True)
    os.replace(path, rejected_dir / path.name)
    (rejected_dir / (path.name + REASON_SUFFIX)).write_text(reason + '\n')


def import_bundles(repo: Repository, paths: list[Path] | None = None) -> ImportResult:
    """
    Import bundles. Without paths, we import the bundles of the incoming
    directory, and remove them from it.
    """
    from_incoming = paths is None
    if paths is None:
        paths = find_incoming_bundles(repo)

    repo.check_public_dir()
    keys = repo.trusted_keys() if repo.config.verify_signatures else None
    result = ImportResult()

    def reject(path: Path, reason: str) -> None:
        log.warning('Rejected %s: %s', path.name, reason)
        result.rejected[path.name] = reason
        if from_incoming:
            _reject(repo, path, reason)

    # a bundle whose base is missing waits for the others, which may bring it
    pending = list(paths)
    while pending:
        waiting = {}
        for path in pending:
            log.info('Importing %s', path.name)
            try:
                _import_bundle(repo, path, keys, result)
            except MissingBase as e:
                log.info('Trying %s again later: %s', path.name, e)
                waiting[path] = str(e)
                continue
            except ImportRejected as e:
                reject(path, str(e))
                continue
            finally:
                repo.clear_tmp()

            result.imported.append(path.name)
            if from_incoming:
                _consume(repo, path)

        # once a round imports nothing, no other bundle will bring the missing bases
        if len(waiting) == len(pending):
            for path, reason in waiting.items():
                reject(path, reason)
            break
        pending = list(waiting)

    return result
