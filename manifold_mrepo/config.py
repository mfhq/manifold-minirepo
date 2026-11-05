# Copyright (C) 2026 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: MPL-2.0

"""
The configuration of a repository, read from a TOML file.

Paths in the configuration are relative to the directory of the file.
"""

import re
import tomllib
from pathlib import Path
from dataclasses import field, dataclass

DEFAULT_CONFIG_NAME = 'manifold-repo.toml'

# the only channel there is for now
MAIN_CHANNEL = 'main'

# the environment variable with a private key to sign the root index with, instead of signing-key
SIGNING_KEY_ENV = 'MREPO_SIGNING_KEY'

_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')


class ConfigError(Exception):
    """The configuration is missing or not valid."""


@dataclass
class ChannelConfig:
    """How a channel of the repository is published."""

    name: str
    # where clients try to download images first, without a "/" at its end, see the repository format
    image_url_prefix: str | None = None
    # leave images that are confirmed at their external URL out of the public tree
    omit_local_images: bool = False


@dataclass
class Config:
    """The configuration of a repository."""

    config_file: Path
    id: str
    summary: str
    url: str
    signing_key: str | None  # None: the key is in $MREPO_SIGNING_KEY
    publisher_name: str | None
    publisher_email: str | None
    incoming_dir: Path
    processed_dir: Path | None  # None: imported bundles are deleted
    public_dir: Path
    database: Path
    trusted_keys_dir: Path
    verify_signatures: bool = True
    valid_days: int = 7
    keep_versions: int = 3
    grace_days: int = 3
    channels: dict[str, ChannelConfig] = field(default_factory=dict)

    def origin(self, channel: str) -> str:
        """The AppStream origin of the catalogs (one per arch) of a channel."""
        return '{}-{}'.format(self.id, channel)

    def media_baseurl(self, channel: str, arch: str) -> str:
        """The public URL of the media directory of a catalog."""
        return '{}/{}/{}/media'.format(self.url, channel, arch)


def _get(table: dict, key: str, kind: type, section: str, default=None, required: bool = False):
    value = table.get(key, default)
    if value is None:
        if required:
            raise ConfigError('[{}] {} is required'.format(section, key))
        return None
    if kind is int and isinstance(value, bool) or not isinstance(value, kind):
        raise ConfigError('[{}] {} must be a {}'.format(section, key, kind.__name__))
    return value


def _get_line(table: dict, key: str, section: str, required: bool = False) -> str | None:
    value = _get(table, key, str, section, required=required)
    if value is None:
        return None
    value = value.strip()
    if not value or '\n' in value:
        raise ConfigError('[{}] {} must be one line of text'.format(section, key))
    return value


def _check_url(value: str, what: str) -> str:
    # clients only download over https
    if not value.startswith('https://'):
        raise ConfigError('{} must be an https URL: {}'.format(what, value))
    return value


def load_config(path: Path) -> Config:
    """Read the configuration of a repository."""

    path = Path(path).absolute()
    try:
        with open(path, 'rb') as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError('The configuration file {} does not exist'.format(path))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError('{}: {}'.format(path, e))

    base_dir = path.parent

    def resolve(value: str) -> Path:
        return (base_dir / value).absolute()

    repo = data.get('repository', {})
    paths = data.get('paths', {})
    trust = data.get('trust', {})

    repo_id = _get(repo, 'id', str, 'repository', required=True)
    if not _ID_RE.match(repo_id):
        raise ConfigError('[repository] id may only contain letters, digits and ".", "_" or "-"')
    summary = _get_line(repo, 'summary', 'repository', required=True)
    publisher_name = _get_line(repo, 'publisher-name', 'repository')
    publisher_email = _get_line(repo, 'publisher-email', 'repository')
    if publisher_email is not None:
        if publisher_name is None:
            raise ConfigError('[repository] publisher-email needs a publisher-name')
        if '@' not in publisher_email or any(c.isspace() for c in publisher_email):
            raise ConfigError('[repository] publisher-email is not an email address: {}'.format(publisher_email))
    url = _check_url(_get(repo, 'url', str, 'repository', required=True), '[repository] url').rstrip('/')

    # a key path is relative to the configuration, names and fingerprints are not paths
    signing_key = _get(repo, 'signing-key', str, 'repository')
    if signing_key is not None and '/' in signing_key and not signing_key.startswith('SHA256:'):
        signing_key = str(resolve(signing_key))

    processed = _get(paths, 'processed', str, 'paths', default='delete')
    config = Config(
        config_file=path,
        id=repo_id,
        summary=summary,
        url=url,
        signing_key=signing_key,
        publisher_name=publisher_name,
        publisher_email=publisher_email,
        incoming_dir=resolve(_get(paths, 'incoming', str, 'paths', default='incoming')),
        processed_dir=None if processed == 'delete' else resolve(processed),
        public_dir=resolve(_get(paths, 'public', str, 'paths', default='public')),
        database=resolve(_get(paths, 'database', str, 'paths', default='mrepo.sqlite')),
        trusted_keys_dir=resolve(_get(trust, 'keys', str, 'trust', default='trusted-keys')),
        verify_signatures=_get(trust, 'verify-signatures', bool, 'trust', default=True),
        valid_days=_get(repo, 'valid-days', int, 'repository', default=7),
        keep_versions=_get(repo, 'keep-versions', int, 'repository', default=3),
        grace_days=_get(repo, 'grace-days', int, 'repository', default=3),
    )
    if config.valid_days < 1 or config.keep_versions < 1 or config.grace_days < 0:
        raise ConfigError('[repository] valid-days and keep-versions must be at least 1, grace-days at least 0')

    channels = data.get('channels', {})
    for channel_name in channels:
        if channel_name != MAIN_CHANNEL:
            raise ConfigError('Only the "{}" channel is supported, not "{}"'.format(MAIN_CHANNEL, channel_name))
    channel_data = channels.get(MAIN_CHANNEL, {})
    section = 'channels.' + MAIN_CHANNEL
    image_url_prefix = _get(channel_data, 'image-url-prefix', str, section)
    if image_url_prefix is not None:
        _check_url(image_url_prefix, '[{}] image-url-prefix'.format(section))
        # clients append "/" and the file name, which would end up in a query or fragment
        if '?' in image_url_prefix or '#' in image_url_prefix:
            raise ConfigError('[{}] image-url-prefix can not have a query or a fragment'.format(section))
        image_url_prefix = image_url_prefix.rstrip('/')
    config.channels[MAIN_CHANNEL] = ChannelConfig(
        name=MAIN_CHANNEL,
        image_url_prefix=image_url_prefix,
        omit_local_images=_get(channel_data, 'omit-local-images', bool, section, default=False),
    )

    return config
