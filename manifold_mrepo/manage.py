# Copyright (C) 2026 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: MPL-2.0

"""
Looking at and changing what the repository holds.
"""

import os

from sqlalchemy import select

from manifold_mrepo.db import Image, utcnow
from manifold_mrepo.repo import Repository
from manifold_mrepo.versions import newest_first
from manifold_mrepo.utils.hashing import hash_file


def list_images(repo: Repository, component_id: str | None = None, arch: str | None = None) -> list[Image]:
    """Find the images the index lists, by component, newest first."""
    with repo.db.session() as session:
        query = select(Image).where(Image.removed_at.is_(None))
        if component_id is not None:
            query = query.where(Image.component_id == component_id)
        if arch is not None:
            query = query.where(Image.architecture == arch)
        images = session.scalars(query).all()

    groups = {}
    for image in images:
        groups.setdefault((image.component_id, image.architecture), []).append(image)
    return [image for key in sorted(groups) for image in newest_first(groups[key])]


def remove_images(
    repo: Repository, component_id: str, version: str | None = None, arch: str | None = None
) -> list[Image]:
    """
    Remove the images of a component from the index, or only those of one
    version. Their files stay published until the grace period is over.
    """
    now = utcnow()
    with repo.db.session() as session:
        query = select(Image).where(Image.component_id == component_id, Image.removed_at.is_(None))
        if version is not None:
            query = query.where(Image.version == version)
        if arch is not None:
            query = query.where(Image.architecture == arch)
        images = session.scalars(query).all()
        for image in images:
            image.removed_at = now
            repo.db.mark_dirty(session)
    return images


def check_files(repo: Repository) -> list[str]:
    """Check that the public tree has every file the database records, with the right content."""
    problems = []
    with repo.db.session() as session:
        for image in session.scalars(select(Image)):
            name = image.describe()
            image_path = repo.public_path(image.zck_location)
            if image_path.exists():
                if os.path.getsize(image_path) != image.zck_size:
                    problems.append('{}: {} has the wrong size'.format(name, image.zck_location))
            elif image.external_url is None:
                problems.append('{}: {} is missing'.format(name, image.zck_location))

            manifest_path = repo.public_path(image.manifest_location)
            if not manifest_path.exists():
                problems.append('{}: {} is missing'.format(name, image.manifest_location))
            elif hash_file(manifest_path).sha256 != image.manifest_sha256:
                problems.append('{}: {} is damaged'.format(name, image.manifest_location))

            for catalog_file in image.catalog_files:
                path = repo.public_path(catalog_file.location)
                if not path.exists():
                    problems.append('{}: {} is missing'.format(name, catalog_file.location))
                elif hash_file(path).sha256 != catalog_file.sha256:
                    problems.append('{}: {} is damaged'.format(name, catalog_file.location))
    return problems
