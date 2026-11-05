# Copyright (C) 2026 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: MPL-2.0

import os
import re
import json
import shutil
import hashlib
import subprocess
from datetime import timedelta

import pytest
from conftest import ARCH, JXL_DATA, TestImage

from manifold_mrepo.db import PUBLISH_STATE_ID, Image, PublishState
from manifold_mrepo.manage import check_files, remove_images
from manifold_mrepo.publish import publish
from manifold_mrepo.bindings import GLib, Manifold, AppStream
from manifold_mrepo.importer import import_bundles
from manifold_mrepo.utils.files import zstd_decompress


def _versions(index: dict, component_id: str) -> list[str]:
    return ['{}-{}'.format(v['version'], v['revision']) for v in index['components'][component_id]['versions']]


def _check_file(path, entry: dict) -> None:
    data = path.read_bytes()
    assert len(data) == entry['size']
    assert hashlib.sha256(data).hexdigest() == entry['sha256']


def test_import_and_publish(env):
    core = TestImage('org.example.Core', kind='core')
    app = TestImage('org.example.App', kind='app', base=core)
    env.bundle('app.manifold', [core, app])
    repo = env.repo()

    result = import_bundles(repo)
    assert result.rejected == {}
    assert sorted(result.added) == ['org.example.App 1-1 (x86_64)', 'org.example.Core 1-1 (x86_64)']
    # the queue was consumed
    assert not (env.incoming_dir / 'app.manifold').exists()

    assert publish(repo).published
    assert not (env.dir / 'public.new').exists()
    root = env.read_root_index()
    assert root['id'] == 'test'
    assert root['summary'] == 'Images for the tests'
    channel = root['channels']['main']
    assert channel['origin'] == 'test-main'
    assert 'image-url' not in channel
    assert list(channel['architectures']) == [ARCH]

    # only the root index is signed, by the repository key, and it pins the files of the channel
    allowed = env.dir / 'allowed'
    allowed.write_text('repo ' + ' '.join(env.repo_key.with_suffix('.pub').read_text().split()[:2]) + '\n')
    with open(env.public_dir / 'repo-index.json.zst', 'rb') as f:
        subprocess.run(
            ['ssh-keygen', '-Y', 'verify', '-f', str(allowed), '-I', 'repo', '-n', 'manifold.repo', '-s']
            + [str(env.public_dir / 'repo-index.sig')],
            stdin=f,
            check=True,
            capture_output=True,
        )
    arch_dir = env.public_dir / 'main' / ARCH
    assert sorted(channel['architectures'][ARCH]) == ['components.xml.zst', 'icons.json.zst', 'index.json.zst']
    for name, file_entry in channel['architectures'][ARCH].items():
        _check_file(arch_dir / name, file_entry)
    index = env.read_index()
    assert sorted(index['components']) == ['org.example.App', 'org.example.Core']

    # every image is next to its manifest in the pool, which is the one of the bundle, and holds the image it describes
    entry = index['components']['org.example.App']['versions'][0]
    assert entry['bundle'] == 'x86_64/org.example.App/1'
    assert entry['manifest']['location'].startswith('pool/org/example/App/')
    manifest_path = env.public_dir / entry['manifest']['location']
    _check_file(manifest_path, entry['manifest'])
    manifest_data = zstd_decompress(manifest_path.read_bytes())
    assert manifest_data == app.manifest(app.data())
    manifest = Manifold.Manifest.new_from_bytes(GLib.Bytes.new(manifest_data))
    zck_path = manifest_path.with_name(manifest_path.name.replace('.json.zst', '.raw.zck'))
    fd = os.open(zck_path, os.O_RDONLY)
    try:
        Manifold.zchunk_check_image(fd, manifest)
        _, header_size, header_sha256 = Manifold.zchunk_read_header(fd)
    finally:
        os.close(fd)
    assert entry['zck-header'] == {'size': header_size, 'sha256': header_sha256}

    # the catalog has the newest version of every component, and the real media URL
    catalog = zstd_decompress((arch_dir / 'components.xml.zst').read_bytes()).decode()
    assert '<image type="source">org.example.App/screenshot.jxl</image>' in catalog
    assert (arch_dir / 'media' / 'org.example.App' / 'screenshot.jxl').exists()
    # it is joined from the components of the images, and AppStream reads it as one catalog
    mdata = AppStream.Metadata()
    mdata.set_format_style(AppStream.FormatStyle.CATALOG)
    mdata.parse_data(catalog, -1, AppStream.FormatKind.XML)
    assert mdata.get_origin() == 'test-main'
    components = {component.get_id(): component for component in mdata.get_components().as_array()}
    assert sorted(components) == ['org.example.App', 'org.example.Core']
    screenshot = components['org.example.App'].get_screenshots_all()[0].get_images_all()[0]
    assert screenshot.get_url() == 'https://example.org/repo/main/x86_64/media/org.example.App/screenshot.jxl'

    icons = json.loads(zstd_decompress((arch_dir / 'icons.json.zst').read_bytes()))
    assert list(icons) == ['64x64/org.example.App.jxl']
    _check_file(env.public_dir / icons['64x64/org.example.App.jxl']['location'], icons['64x64/org.example.App.jxl'])
    assert check_files(repo) == []


def test_rejected_bundles(env):
    core = TestImage('org.example.Core')
    env.bundle('unsigned.manifold', [core], signed=False)
    env.bundle('other.manifold', [TestImage('org.example.Other')], key=env.other_key)
    repo = env.repo()

    result = import_bundles(repo)
    assert sorted(result.rejected) == ['other.manifold', 'unsigned.manifold']
    assert result.added == []
    rejected_dir = env.incoming_dir / 'rejected'
    assert (rejected_dir / 'unsigned.manifold').exists()
    assert 'not signed' in (rejected_dir / 'unsigned.manifold.reason').read_text()
    assert 'not made by a trusted key' in (rejected_dir / 'other.manifold.reason').read_text()

    # without signature checks, any bundle is fine
    env.write_config('[trust]\nverify-signatures = false\n')
    os.replace(rejected_dir / 'unsigned.manifold', env.incoming_dir / 'unsigned.manifold')
    assert import_bundles(env.repo()).added == ['org.example.Core 1-1 (x86_64)']


def test_bases(env):
    platform = TestImage('org.example.Platform', kind='platform')
    kit = TestImage('org.example.Kit', kind='kit', base=platform)
    lost_kit = TestImage('org.example.LostKit', kind='kit', base=platform)
    # bundles are imported in the order of their names, so the app comes before its kit
    env.bundle('a-app.manifold', [TestImage('org.example.App', kind='app', base=kit)])
    env.bundle('b-kit.manifold', [kit])
    env.bundle('c-other.manifold', [TestImage('org.example.Other', kind='app', base=lost_kit)])
    repo = env.repo()

    # the app waits for its kit, while the platform below the kit may come from another repository
    result = import_bundles(repo)
    assert result.imported == ['b-kit.manifold', 'a-app.manifold']
    assert list(result.rejected) == ['c-other.manifold']
    reason = (env.incoming_dir / 'rejected' / 'c-other.manifold.reason').read_text()
    assert 'builds on the kit org.example.LostKit 1, which is neither in the bundle nor in the repository' in reason


def _icon_location(env, name: str) -> str:
    icons = json.loads(zstd_decompress((env.public_dir / 'main' / ARCH / 'icons.json.zst').read_bytes()))
    entry = icons[name]
    _check_file(env.public_dir / entry['location'], entry)
    return entry['location']


def test_catalog_files(env):
    core = TestImage('org.example.Core')
    env.bundle('app1.manifold', [core, TestImage('org.example.App', kind='app', version='1', base=core, seed='one')])
    env.bundle('app2.manifold', [TestImage('org.example.App', kind='app', version='2', base=core, seed='two')])
    repo = env.repo()
    import_bundles(repo)
    publish(repo)

    # icons keep the name the catalog gives them, in a directory that changes with them
    location2 = _icon_location(env, '64x64/org.example.App.jxl')
    assert re.fullmatch(r'main/x86_64/media/org/example/App/[0-9a-f]{32}/icons/64x64/org\.example\.App\.jxl', location2)

    # a published file never changes, so the first screenshot at a path stays there
    screenshot = env.public_dir / 'main' / ARCH / 'media' / 'org.example.App' / 'screenshot.jxl'
    assert screenshot.read_bytes() == JXL_DATA + b'one'
    assert check_files(repo) == []

    # the files of an older version stay as well, so it can become the newest version again
    remove_images(repo, 'org.example.App', '2')
    publish(repo)
    location1 = _icon_location(env, '64x64/org.example.App.jxl')
    assert location1 != location2


def test_revisions(env):
    repo = env.repo()
    env.bundle('core1.manifold', [TestImage('org.example.Core', revision=1)])
    import_bundles(repo)
    publish(repo)
    old_files = sorted(p.name for p in (env.public_dir / 'pool' / 'org/example/Core').iterdir())

    # a new revision of the version replaces the old one in the index
    env.bundle('core2.manifold', [TestImage('org.example.Core', revision=2)])
    assert import_bundles(repo).added == ['org.example.Core 1-2 (x86_64)']
    publish(repo)
    assert _versions(env.read_index(), 'org.example.Core') == ['1-2']
    # its files stay downloadable for clients that still have the older index
    files = {p.name for p in (env.public_dir / 'pool' / 'org/example/Core').iterdir()}
    assert set(old_files) < files

    # the same revision as a different image, or an older revision, are refused
    env.bundle('core2b.manifold', [TestImage('org.example.Core', revision=2, seed='b')])
    env.bundle('core1b.manifold', [TestImage('org.example.Core', revision=1, seed='b')])
    result = import_bundles(repo)
    assert 'as a different image' in result.rejected['core2b.manifold']
    assert 'newer revision 2' in result.rejected['core1b.manifold']


def test_retention_keeps_bases(env):
    env.write_config(repository='keep-versions = 1\n')
    repo = env.repo()
    core1 = TestImage('org.example.Core', version='1')
    core2 = TestImage('org.example.Core', version='2')
    env.bundle('app1.manifold', [core1, TestImage('org.example.App', kind='app', version='1', base=core1)])
    env.bundle('core2.manifold', [core2])
    import_bundles(repo)

    # the app still builds on the first core, so both cores stay, the newest first
    result = publish(repo)
    assert result.retired == []
    index = env.read_index()
    assert _versions(index, 'org.example.Core') == ['2-1', '1-1']
    assert _versions(index, 'org.example.App') == ['1-1']

    # once the app moves on, the old core and the old app leave the index
    env.bundle('app2.manifold', [TestImage('org.example.App', kind='app', version='2', base=core2)])
    import_bundles(repo)
    result = publish(repo)
    assert sorted(result.retired) == ['org.example.App 1-1 (x86_64)', 'org.example.Core 1-1 (x86_64)']
    index = env.read_index()
    assert _versions(index, 'org.example.Core') == ['2-1']
    assert _versions(index, 'org.example.App') == ['2-1']


def test_republish_and_expiry(env):
    repo = env.repo()
    env.bundle('core.manifold', [TestImage('org.example.Core')])
    import_bundles(repo)
    publish(repo)
    serial = env.read_root_index()['serial']
    catalog_path = env.public_dir / 'main' / ARCH / 'components.xml.zst'
    catalog = catalog_path.read_bytes()

    # nothing changed, so nothing is published, unless we are asked to
    assert not publish(repo).published
    assert publish(repo, force=True).published
    assert env.read_root_index()['serial'] > serial
    assert catalog_path.read_bytes() == catalog

    # an index is published again before half of its validity has passed
    with repo.db.session() as session:
        state = session.get(PublishState, PUBLISH_STATE_ID)
        state.date -= timedelta(days=4)
        state.valid_until -= timedelta(days=4)
    assert publish(repo).published

    # a removed image is deleted once its grace period is over
    assert len(remove_images(repo, 'org.example.Core')) == 1
    with repo.db.session() as session:
        for image in session.query(Image):
            image.removed_at -= timedelta(days=repo.config.grace_days + 1)
    result = publish(repo)
    assert result.deleted == ['org.example.Core 1-1 (x86_64)']
    # a channel without images has no architectures
    assert env.read_root_index()['channels']['main']['architectures'] == {}
    assert not (env.public_dir / 'pool' / 'org').exists()


def test_external_images(env, http_dir):
    served_dir, base_url = http_dir
    env.write_config(
        '[channels.main]\nimage-url = "{}/{{component}}/{{file}}"\nomit-local-images = true\n'.format(base_url)
    )
    repo = env.repo()
    env.bundle('core.manifold', [TestImage('org.example.Core')])
    import_bundles(repo)

    from manifold_mrepo.external import find_pending_uploads

    # an image that is not at its URL yet is published from the repository
    with repo.db.session() as session:
        pending = find_pending_uploads(repo, session)
        assert len(pending) == 1
        upload = pending[0]
        assert upload.url == '{}/org.example.Core/{}'.format(base_url, upload.asset_name)
    publish(repo)
    assert len(list(env.public_dir.rglob('*.raw.zck'))) == 1

    # a different file at the URL does not count
    (served_dir / 'org.example.Core').mkdir()
    (served_dir / 'org.example.Core' / upload.asset_name).write_bytes(b'not the image')
    publish(repo, force=True)
    assert len(list(env.public_dir.rglob('*.raw.zck'))) == 1

    # once the image is there, clients get it from there
    shutil.copy(upload.path, served_dir / 'org.example.Core' / upload.asset_name)
    publish(repo, force=True)
    assert list(env.public_dir.rglob('*.raw.zck')) == []
    assert env.read_root_index()['channels']['main']['image-url'] == base_url + '/{component}/{file}'
    with repo.db.session() as session:
        assert find_pending_uploads(repo, session) == []


def test_config_errors(env):
    from manifold_mrepo.config import ConfigError, load_config

    for extra in (
        '[channels.beta]\n',
        '[channels.main]\nimage-url = "https://example.org/images/"\n',
        '[channels.main]\nimage-url = "https://example.org/{arch}/{file}"\n',
    ):
        path = env.write_config(extra)
        with pytest.raises(ConfigError):
            load_config(path)

    path = env.dir / 'ftp.toml'
    path.write_text('[repository]\nid = "test"\nsummary = "Test"\nurl = "ftp://example.org"\nsigning-key = "key"\n')
    with pytest.raises(ConfigError):
        load_config(path)

    # a key path is relative to the configuration file
    config = load_config(env.write_config())
    assert config.url == 'https://example.org/repo'
    assert config.signing_key == str(env.dir / 'keys' / 'repo')
