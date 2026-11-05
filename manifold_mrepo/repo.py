# Copyright (C) 2026 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: MPL-2.0

import os
import shutil
import logging
from pathlib import Path
from contextlib import contextmanager

from manifold_mrepo.db import Database
from manifold_mrepo.config import SIGNING_KEY_ENV, Config
from manifold_mrepo.bindings import GLib, Manifold
from manifold_mrepo.utils.lock import process_file_lock

log = logging.getLogger(__name__)


class RepositoryError(Exception):
    """Something about the repository itself is wrong, so we can not continue."""


class Repository:
    """
    A repository: its configuration, its database and its files.

    The files are kept in the public tree itself: images and manifests in its
    pool, icons and screenshots in the media directories of the catalogs. A
    publication builds a new tree from links to the files of the current one,
    and leaves out what is no longer needed. Only temporary files are kept
    elsewhere, next to the tree.
    """

    def __init__(self, config: Config):
        self.config = config
        config.incoming_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

        # zchunk keeps an image in a temporary file while it writes it, so the temporary
        # files go next to the public tree, where there is room for images
        os.environ['TMPDIR'] = str(self.tmp_dir)

        self.db = Database(config.database)
        self._trusted_keys = None

    @property
    def tmp_dir(self) -> Path:
        """Where we prepare files, on the same filesystem as the public tree so they can be renamed into it."""
        public_dir = self.config.public_dir
        return public_dir.with_name(public_dir.name + '.tmp')

    def public_path(self, location: str) -> Path:
        """The path of a file of the public tree."""
        return self.config.public_dir / location

    def check_public_dir(self) -> None:
        """Check that we can rename files into the public tree, and replace the tree as a whole."""
        if os.path.ismount(self.config.public_dir):
            raise RepositoryError(
                '{} is a mount point, which we can not replace. Use a directory below it.'.format(
                    self.config.public_dir
                )
            )

    @contextmanager
    def lock(self):
        """Hold the lock of the repository, so no other process changes it at the same time."""
        database = self.config.database
        with process_file_lock(database.with_name(database.name + '.lock')):
            yield

    def clear_tmp(self) -> None:
        """Remove what an earlier run that was interrupted left behind."""
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def trusted_keys(self) -> Manifold.TrustedKeys:
        """The keys that may sign the bundles we import."""
        if self._trusted_keys is None:
            keys = Manifold.TrustedKeys.new()
            try:
                keys.add_dir(str(self.config.trusted_keys_dir))
            except GLib.Error as e:
                raise RepositoryError('Unable to load the trusted keys: {}'.format(e.message))
            if keys.is_empty():
                raise RepositoryError(
                    'No key is trusted to sign bundles: {} holds no *.pub files. Add the public keys of the '
                    'uploaders there, or set verify-signatures = false in [trust].'.format(self.config.trusted_keys_dir)
                )
            self._trusted_keys = keys
        return self._trusted_keys

    def signer(self) -> Manifold.Signer:
        """The signer of the root index: the private key in $MREPO_SIGNING_KEY if it is set, or else signing-key."""
        key_data = os.environ.get(SIGNING_KEY_ENV)
        if not key_data and self.config.signing_key is None:
            raise RepositoryError(
                'There is no key to sign the repository with. Set signing-key in [repository], '
                'or put the private key into ${}.'.format(SIGNING_KEY_ENV)
            )
        try:
            if key_data:
                signer = Manifold.Signer.new_for_key_data(key_data)
            else:
                signer = Manifold.Signer.new_for_key(self.config.signing_key)
            signer.check_key()
        except GLib.Error as e:
            raise RepositoryError('Unable to sign the repository: {}'.format(e.message))
        return signer

    def close(self) -> None:
        self.db.dispose()
