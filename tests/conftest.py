# Copyright (C) 2026 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: MPL-2.0

"""
Fixtures that make up bundles and repositories.
"""

import json
import random
import hashlib
import threading
import subprocess
import http.server
from pathlib import Path
from functools import partial
from dataclasses import dataclass

import pytest

pytest.importorskip('manifold_mrepo.bindings', reason='libmanifold is not available through GObject Introspection')

from manifold_mrepo.repo import Repository  # noqa: E402
from manifold_mrepo.config import load_config  # noqa: E402
from manifold_mrepo.bindings import GLib, Manifold  # noqa: E402
from manifold_mrepo.utils.hashing import hash_bytes  # noqa: E402

ARCH = 'x86_64'
BLOCK_SIZE = 4096
# the start of a JPEG XL codestream, which is all libmanifold checks icons for
JXL_DATA = b'\xff\x0a' + b'icon' * 16


def fake_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class TestImage:
    """An image to make up, with what its manifest says about it."""

    __test__ = False

    component_id: str
    kind: str = 'core'
    version: str = '1'
    revision: int = 1
    base: 'TestImage | None' = None
    # a different seed makes a different image with the same metadata
    seed: str = ''

    @property
    def content_hash(self) -> str:
        return fake_hash('content:{}:{}:{}'.format(self.component_id, self.version, self.seed))

    @property
    def file_name(self) -> str:
        return '{}_{}-{}_x86-64.raw'.format(self.component_id, self.version, self.revision)

    def data(self) -> bytes:
        """Some empty blocks, some random data and a short end, like a GPT backup header."""
        rng = random.Random('{}:{}:{}:{}'.format(self.component_id, self.version, self.revision, self.seed))
        return bytes(4 * BLOCK_SIZE) + rng.randbytes(12 * BLOCK_SIZE) + b'g' * 512

    def manifest(self, data: bytes) -> bytes:
        hashes = hash_bytes(data)
        manifest = {
            'format': 1,
            'component': self.component_id,
            'kind': self.kind,
            'version': self.version,
            'revision': self.revision,
            'architecture': ARCH,
            'sdk': False,
            'identity': fake_hash('identity:' + self.component_id),
            'content-hash': self.content_hash,
            'base': None,
            'image': {
                'file': self.file_name,
                'size': hashes.size,
                'sha256': hashes.sha256,
                'blake3': hashes.blake3,
                'verity-root-hash': fake_hash('verity:' + hashes.sha256),
                'data-offset': 0,
                'hash-offset': 8 * BLOCK_SIZE,
            },
        }
        if self.base is not None:
            manifest['base'] = {
                'component': self.base.component_id,
                'kind': self.base.kind,
                'version': self.base.version,
                'revision': self.base.revision,
                'identity': fake_hash('identity:' + self.base.component_id),
                'content-hash': self.base.content_hash,
            }
        if self.kind == 'app':
            manifest['app'] = {'command': 'app'}
        return (json.dumps(manifest, indent=2) + '\n').encode()

    def catalog(self) -> bytes:
        extra = ''
        component_type = 'runtime'
        if self.kind == 'app':
            component_type = 'desktop-application'
            extra = (
                '<icon type="cached" width="64" height="64">{0}.jxl</icon>'
                '<screenshots><screenshot type="default">'
                '<image type="source">{0}/screenshot.jxl</image>'
                '</screenshot></screenshots>'
            ).format(self.component_id)
        return (
            (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<components version="1.2" origin="manifold-local">'
                '<component type="{}"><id>{}</id><name>{}</name><summary>A test component</summary>'
                '<bundle type="manifold">{}/{}/{}</bundle>{}</component></components>\n'
            )
            .format(component_type, self.component_id, self.component_id, ARCH, self.component_id, self.version, extra)
            .encode()
        )


class Env:
    """A repository in a temporary directory, with keys and a way to make bundles for it."""

    def __init__(self, tmp_path: Path):
        self.dir = tmp_path
        self.keys_dir = tmp_path / 'keys'
        self.keys_dir.mkdir()
        self.uploader_key = self._make_key('uploader')
        self.other_key = self._make_key('other')
        self.repo_key = self._make_key('repo')
        (tmp_path / 'trusted-keys').mkdir()
        (tmp_path / 'trusted-keys' / 'uploader.pub').write_text(Path(str(self.uploader_key) + '.pub').read_text())
        self.incoming_dir = tmp_path / 'incoming'
        self.public_dir = tmp_path / 'public'
        self._repos = []
        self.write_config()

    def _make_key(self, name: str) -> Path:
        path = self.keys_dir / name
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', name, '-f', str(path)], check=True)
        return path

    def write_config(self, extra: str = '', repository: str = '', signing_key: str | None = 'keys/repo') -> Path:
        path = self.dir / 'mrepo.toml'
        path.write_text(
            '[repository]\n'
            'id = "test"\n'
            'summary = "Images for the tests"\n'
            'url = "https://example.org/repo"\n'
            + ('signing-key = "{}"\n'.format(signing_key) if signing_key is not None else '')
            + repository
            + '\n'
            + extra
        )
        return path

    def repo(self) -> Repository:
        repo = Repository(load_config(self.dir / 'mrepo.toml'))
        self._repos.append(repo)
        return repo

    def bundle(self, name: str, images: list[TestImage], key: Path | None = None, signed: bool = True) -> Path:
        """Make up a bundle in the incoming directory."""
        work = self.dir / 'work' / name
        icons_dir = work / 'icons' / '64x64'
        media_dir = work / 'media'
        icons_dir.mkdir(parents=True)
        media_dir.mkdir()

        self.incoming_dir.mkdir(exist_ok=True)
        path = self.incoming_dir / name
        writer = Manifold.BundleWriter.new(str(path))
        for image in images:
            data = image.data()
            raw_path = work / image.file_name
            raw_path.write_bytes(data)
            writer.add_image(str(raw_path), GLib.Bytes.new(image.manifest(data)), GLib.Bytes.new(image.catalog()))
            if image.kind == 'app':
                (icons_dir / (image.component_id + '.jxl')).write_bytes(JXL_DATA + image.seed.encode())
                (media_dir / image.component_id).mkdir()
                (media_dir / image.component_id / 'screenshot.jxl').write_bytes(JXL_DATA + image.seed.encode())
        writer.add_catalog_assets(str(work / 'icons'), str(media_dir))
        signer = Manifold.Signer.new_for_key_file(str(key or self.uploader_key)) if signed else None
        writer.finish(signer)
        return path

    def read_root_index(self) -> dict:
        from manifold_mrepo.utils.files import zstd_decompress

        return json.loads(zstd_decompress((self.public_dir / 'repo-index.json.zst').read_bytes()))

    def read_index(self, channel: str = 'main') -> dict:
        """The component index of a channel, for our architecture."""
        from manifold_mrepo.utils.files import zstd_decompress

        return json.loads(zstd_decompress((self.public_dir / channel / ARCH / 'index.json.zst').read_bytes()))

    def close(self) -> None:
        for repo in self._repos:
            repo.close()


@pytest.fixture
def env(tmp_path):
    env = Env(tmp_path)
    yield env
    env.close()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


@pytest.fixture
def http_dir(tmp_path):
    """A directory served over HTTP, like release assets on a web host."""
    served = tmp_path / 'served'
    served.mkdir()
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), partial(_QuietHandler, directory=str(served)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield served, 'http://127.0.0.1:{}'.format(server.server_address[1])
    server.shutdown()
    server.server_close()
