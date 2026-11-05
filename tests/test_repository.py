# Copyright (C) 2026 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: MPL-2.0

import os
import re
import json
import shutil
import hashlib
import logging
import subprocess
from datetime import timedelta

import pytest
from conftest import ARCH, JXL_DATA, TestImage

from manifold_mrepo.db import PUBLISH_STATE_ID, Image, PublishState
from manifold_mrepo.repo import RepositoryError
from manifold_mrepo.manage import check_files, remove_images
from manifold_mrepo.publish import publish
from manifold_mrepo.bindings import GLib, Manifold, AppStream
from manifold_mrepo.importer import import_bundles
from manifold_mrepo.utils.files import zstd_decompress


def _versions(index: dict, component_id: str) -> list[str]:
    return ['{}-{}'.format(v['version'], v['revision']) for v in index['components'][component_id]['versions']]


def _check_root_signature(env, key) -> None:
    """Check that the root index is signed by key, and that the published public key is that key."""
    public_key = ' '.join(key.with_suffix('.pub').read_text().split()[:2])
    assert (env.public_dir / 'test_key.pub').read_text() == public_key + '\n'

    allowed = env.dir / 'allowed'
    allowed.write_text('repo ' + public_key + '\n')
    with open(env.public_dir / 'repo-index.json.zst', 'rb') as f:
        subprocess.run(
            ['ssh-keygen', '-Y', 'verify', '-f', str(allowed), '-I', 'repo', '-n', 'manifold.repo', '-s']
            + [str(env.public_dir / 'repo-index.sig')],
            stdin=f,
            check=True,
            capture_output=True,
        )


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
    assert 'publisher' not in root
    # the origin of the channel is always test-main, so the root index doesn't repeat it
    channel = root['channels']['main']
    assert list(channel) == ['architectures']
    assert list(channel['architectures']) == [ARCH]

    # only the root index is signed, by the repository key, and it pins the files of the channel
    _check_root_signature(env, env.repo_key)
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
    env.write_config(repository='publisher-name = "Example Software"\npublisher-email = "repo@example.org"\n')
    repo = env.repo()
    assert publish(repo, force=True).published
    root = env.read_root_index()
    assert root['serial'] > serial
    assert root['publisher'] == {'name': 'Example Software', 'email': 'repo@example.org'}
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


def test_external_images(env, https_dir, caplog):
    served_dir, base_url = https_dir.path, https_dir.url
    # the prefix may end with a "/", which is not published
    env.write_config('[channels.main]\nimage-url-prefix = "{}/images/"\nomit-local-images = true\n'.format(base_url))
    repo = env.repo()
    env.bundle('core.manifold', [TestImage('org.example.Core')])
    import_bundles(repo)

    from manifold_mrepo.external import find_pending_uploads

    # an image that is not at its URL yet is published from the repository
    with repo.db.session() as session:
        pending = find_pending_uploads(repo, session)
        assert len(pending) == 1
        upload = pending[0]
        file_name = upload.image.zck_name
        assert upload.url == '{}/images/{}'.format(base_url, file_name)
    publish(repo)
    assert len(list(env.public_dir.rglob('*.raw.zck'))) == 1

    # a different file at the URL does not count
    (served_dir / 'images').mkdir()
    (served_dir / 'images' / file_name).write_bytes(b'not the image')
    publish(repo, force=True)
    assert len(list(env.public_dir.rglob('*.raw.zck'))) == 1

    # hosts like GitHub redirect to another location, which has to be https as well
    from manifold_mrepo.external import find_external_image

    shutil.copy(upload.path, served_dir / 'images' / file_name)
    with repo.db.session() as session:
        image = session.query(Image).one()
        https_dir.redirects['/moved/' + file_name] = '{}/images/{}'.format(base_url, file_name)
        assert find_external_image(repo, '{}/moved/{}'.format(base_url, file_name), image)
        https_dir.redirects['/moved/' + file_name] = '{}/images/{}'.format(base_url.replace('https', 'http'), file_name)
        caplog.set_level(logging.DEBUG, logger='manifold_mrepo.external')
        assert not find_external_image(repo, '{}/moved/{}'.format(base_url, file_name), image)
        assert 'not an https URL' in caplog.text

    # once the image is there, clients get it from there
    publish(repo, force=True)
    assert list(env.public_dir.rglob('*.raw.zck')) == []
    assert env.read_root_index()['channels']['main']['image-url-prefix'] == base_url + '/images'
    with repo.db.session() as session:
        assert find_pending_uploads(repo, session) == []


def test_external_files(env, https_dir):
    base_url = https_dir.url
    env.write_config('[channels.main]\nimage-url-prefix = "{}"\n'.format(base_url))
    repo = env.repo()
    env.bundle('core.manifold', [TestImage('org.example.Core')])
    import_bundles(repo)
    publish(repo)

    from manifold_mrepo.external import find_external_files

    def external_files() -> list[str]:
        with repo.db.session() as session:
            return [external.image.zck_name for external in find_external_files(repo, session)]

    # an image that is not at its URL yet still belongs there
    first = external_files()
    assert len(first) == 1 and first[0].endswith('.raw.zck')

    # one that left the index is still needed during its grace period, for clients with an older index
    env.bundle('core2.manifold', [TestImage('org.example.Core', version='2')])
    import_bundles(repo)
    assert len(remove_images(repo, 'org.example.Core', '1')) == 1
    publish(repo)
    assert len(external_files()) == 2 and first[0] in external_files()

    # once that is over, only the images of the index are left
    with repo.db.session() as session:
        for image in session.query(Image).where(Image.removed_at.is_not(None)):
            image.removed_at -= timedelta(days=repo.config.grace_days + 1)
    publish(repo)
    remaining = external_files()
    assert len(remaining) == 1 and remaining[0] != first[0]


def test_signing_key_from_environment(env, monkeypatch):
    env.bundle('core.manifold', [TestImage('org.example.Core')])
    env.write_config(signing_key=None)
    repo = env.repo()
    assert repo.config.signing_key is None
    import_bundles(repo)

    monkeypatch.delenv('MREPO_SIGNING_KEY', raising=False)
    with pytest.raises(RepositoryError, match='no key to sign'):
        publish(repo)
    assert not (env.public_dir / 'repo-index.json.zst').exists()

    # the key in the environment is used instead of signing-key, even without the newline at its end
    env.write_config()
    repo = env.repo()
    monkeypatch.setenv('MREPO_SIGNING_KEY', env.other_key.read_text().strip())
    assert publish(repo).published
    _check_root_signature(env, env.other_key)

    # the public key is part of the tree, so it is written again when it is missing
    (env.public_dir / 'test_key.pub').unlink()
    assert publish(repo).published
    _check_root_signature(env, env.other_key)


def test_config_errors(env):
    from manifold_mrepo.config import ConfigError, load_config

    for extra in (
        '[channels.beta]\n',
        '[channels.main]\nimage-url-prefix = "ftp://example.org/images"\n',
        '[channels.main]\nimage-url-prefix = "http://example.org/images"\n',
        '[channels.main]\nimage-url-prefix = "https://example.org/images?tag=1"\n',
        '[channels.main]\nimage-url-prefix = "https://example.org/images#top"\n',
    ):
        path = env.write_config(extra)
        with pytest.raises(ConfigError):
            load_config(path)

    # a publisher email needs a name, has to look like an address, and both are one line
    for repository in (
        'publisher-email = "repo@example.org"\n',
        'publisher-name = "Example"\npublisher-email = "not an address"\n',
        'publisher-name = "Two\\nlines"\n',
    ):
        path = env.write_config(repository=repository)
        with pytest.raises(ConfigError):
            load_config(path)

    # clients only download over https
    for url in ('ftp://example.org', 'http://example.org'):
        path = env.dir / 'plain.toml'
        path.write_text('[repository]\nid = "test"\nsummary = "Test"\nurl = "{}"\nsigning-key = "key"\n'.format(url))
        with pytest.raises(ConfigError):
            load_config(path)

    # a key path is relative to the configuration file
    config = load_config(env.write_config())
    assert config.url == 'https://example.org/repo'
    assert config.signing_key == str(env.dir / 'keys' / 'repo')
