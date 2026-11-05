# Copyright (C) 2026 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: MPL-2.0

"""
The database of a repository.

It records what the repository holds: every image with what the index lists
about it, and the catalog files that belong to it. The files themselves are
kept in the pool.
"""

from pathlib import Path
from datetime import UTC, datetime
from contextlib import AbstractContextManager

from sqlalchemy import (
    String,
    DateTime,
    ForeignKey,
    TypeDecorator,
    event,
    create_engine,
)
from sqlalchemy.orm import (
    Mapped,
    Session,
    DeclarativeBase,
    relationship,
    sessionmaker,
    mapped_column,
)

from manifold_mrepo.layout import pool_location


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class UtcDateTime(TypeDecorator):
    """A point in time, always in UTC. SQLite keeps no time zones, so we store UTC times without one."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError('Times must have a time zone')
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UtcDateTime}


class Bundle(Base):
    """A bundle that was imported."""

    __tablename__ = 'bundles'

    id: Mapped[int] = mapped_column(primary_key=True)
    file_name: Mapped[str]
    sha256: Mapped[str] = mapped_column(String(64))
    # the fingerprint of the key that signed the bundle, if signatures were checked
    signer: Mapped[str | None]
    imported_at: Mapped[datetime]

    images: Mapped[list['Image']] = relationship(back_populates='bundle')


class Image(Base):
    """An image, and what the repository lists about it."""

    __tablename__ = 'images'

    verity_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel: Mapped[str]
    architecture: Mapped[str]
    component_id: Mapped[str]
    kind: Mapped[str]
    sdk: Mapped[bool]
    version: Mapped[str]
    revision: Mapped[int]
    # what images that build on this one ask for, which other builds may provide as well
    identity: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    # the identity and the content hash of the image this one builds on, if any
    base_identity: Mapped[str | None] = mapped_column(String(64))
    base_content_hash: Mapped[str | None] = mapped_column(String(64))
    # the ID of the image in the <bundle type="manifold"/> tag of its catalog data
    bundle_id: Mapped[str]

    # the manifest, as it is stored in the pool, compressed
    manifest_size: Mapped[int]
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    manifest_blake3: Mapped[str] = mapped_column(String(64))

    # the image as a zchunk file
    zck_size: Mapped[int]
    zck_header_size: Mapped[int]
    zck_header_sha256: Mapped[str] = mapped_column(String(64))

    # the <component> element of the image, as the catalog of its channel holds it
    component_xml: Mapped[str]

    # the URL at which we found the same zchunk file, and when
    external_url: Mapped[str | None]
    external_checked_at: Mapped[datetime | None]

    added_at: Mapped[datetime]
    # when the image was removed from the index; its files stay published for a while
    removed_at: Mapped[datetime | None]

    bundle_ref: Mapped[int | None] = mapped_column(ForeignKey('bundles.id', ondelete='SET NULL'))
    bundle: Mapped[Bundle | None] = relationship(back_populates='images')

    catalog_files: Mapped[list['CatalogFile']] = relationship(
        back_populates='image', cascade='all, delete-orphan', passive_deletes=True
    )

    @property
    def is_active(self) -> bool:
        return self.removed_at is None

    @property
    def zck_name(self) -> str:
        return self.verity_hash + '.raw.zck'

    @property
    def manifest_name(self) -> str:
        return self.verity_hash + '.json.zst'

    @property
    def zck_location(self) -> str:
        return pool_location(self.component_id, self.zck_name)

    @property
    def manifest_location(self) -> str:
        return pool_location(self.component_id, self.manifest_name)

    def describe(self) -> str:
        return '{} {}-{} ({})'.format(self.component_id, self.version, self.revision, self.architecture)


class CatalogFile(Base):
    """A cached icon or a screenshot of an image, in the media directory of its catalog."""

    __tablename__ = 'catalog_files'

    id: Mapped[int] = mapped_column(primary_key=True)
    image_verity_hash: Mapped[str] = mapped_column(ForeignKey('images.verity_hash', ondelete='CASCADE'))
    # "icon" or "media"
    kind: Mapped[str]
    # icons: "<size>/<name>" as the catalog names them, media: the path the catalog refers to
    path: Mapped[str]
    # where the file is in the public tree; other images may share it
    location: Mapped[str]
    size: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64))
    blake3: Mapped[str] = mapped_column(String(64))

    image: Mapped[Image] = relationship(back_populates='catalog_files')


# the one row of the publish state, as the whole repository has a single root index
PUBLISH_STATE_ID = 1


class PublishState(Base):
    """What was published last."""

    __tablename__ = 'publish_state'

    id: Mapped[int] = mapped_column(primary_key=True)
    serial: Mapped[int]
    date: Mapped[datetime]
    valid_until: Mapped[datetime]
    # something changed since the last publication
    dirty: Mapped[bool]


def _set_sqlite_pragmas(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute('PRAGMA foreign_keys=ON')
    cursor.execute('PRAGMA journal_mode=WAL')
    cursor.close()


class Database:
    """The SQLite database of a repository."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine('sqlite:///{}'.format(path))
        event.listen(self._engine, 'connect', _set_sqlite_pragmas)
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False)

    def session(self) -> AbstractContextManager[Session]:
        """A session that commits when the block ends, and rolls back on an error."""
        return self._session_factory.begin()

    def mark_dirty(self, session: Session) -> None:
        """Record that the repository has to be published again."""
        state = session.get(PublishState, PUBLISH_STATE_ID)
        if state is not None:
            state.dirty = True

    def dispose(self) -> None:
        self._engine.dispose()
