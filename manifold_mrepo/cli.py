# Copyright (C) 2026 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: MPL-2.0

import sys
import logging
from pathlib import Path

import click

from manifold_mrepo import __version__
from manifold_mrepo.config import DEFAULT_CONFIG_NAME, ConfigError, load_config


class _CmdContext:
    """What the commands share: where the configuration is, and the repository once it is open."""

    def __init__(self, config_path: Path):
        self.config_path = config_path
        self._repo = None

    @property
    def repo(self):
        if self._repo is None:
            # GObject Introspection is only loaded once a command needs the repository
            try:
                from manifold_mrepo.repo import Repository
            except ImportError as e:
                raise click.ClickException('libmanifold is not available through GObject Introspection: {}'.format(e))

            try:
                self._repo = Repository(load_config(self.config_path))
            except ConfigError as e:
                raise click.ClickException(str(e))
        return self._repo


def _run(func, *args, **kwargs):
    """Run a command on the repository, turning our errors into messages."""
    from manifold_mrepo.repo import RepositoryError
    from manifold_mrepo.bindings import GLib

    try:
        return func(*args, **kwargs)
    except (RepositoryError, ConfigError) as e:
        raise click.ClickException(str(e))
    except GLib.Error as e:
        raise click.ClickException(e.message)


@click.group()
@click.option(
    '-c',
    '--config',
    'config_path',
    type=click.Path(path_type=Path, dir_okay=False),
    default=DEFAULT_CONFIG_NAME,
    show_default=True,
    help='The configuration file of the repository.',
)
@click.option('-v', '--verbose', is_flag=True, help='Show debug messages.')
@click.version_option(__version__, prog_name='manifold-mrepo')
@click.pass_context
def cli(ctx, config_path: Path, verbose: bool):
    """Build a repository of Manifold images."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format='%(levelname)s: %(message)s')
    ctx.obj = _CmdContext(config_path)


def _do_import(repo, bundles: list[Path]) -> bool:
    from manifold_mrepo.importer import import_bundles

    result = _run(import_bundles, repo, bundles or None)
    for name in result.added:
        click.echo('Added {}'.format(name))
    for name, reason in result.rejected.items():
        click.echo('Rejected {}: {}'.format(name, reason), err=True)
    return not result.rejected


def _do_publish(repo, force: bool, recheck_external: bool) -> None:
    from manifold_mrepo.publish import publish

    result = _run(publish, repo, force=force, recheck_external=recheck_external)
    for name in result.retired:
        click.echo('Retired {}'.format(name))
    if result.published:
        click.echo('Published {}'.format(', '.join(result.architectures) or 'an empty repository'))
    else:
        click.echo('Nothing to publish')
    for name in result.deleted:
        click.echo('Deleted {}'.format(name))


@cli.command('import')
@click.argument('bundles', nargs=-1, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_obj
def import_cmd(state: _CmdContext, bundles: tuple[Path, ...]):
    """Import bundles"""
    repo = state.repo
    with repo.lock():
        ok = _do_import(repo, list(bundles))
    if not ok:
        sys.exit(1)


@cli.command()
@click.option('--force', is_flag=True, help='Publish even if nothing changed.')
@click.option('--recheck-external', is_flag=True, help='Check again which images are found at their external URL.')
@click.pass_obj
def publish(state: _CmdContext, force: bool, recheck_external: bool):
    """Publish the repository"""
    repo = state.repo
    with repo.lock():
        _do_publish(repo, force, recheck_external)


@cli.command()
@click.option('--force', is_flag=True, help='Publish even if nothing changed.')
@click.pass_obj
def update(state: _CmdContext, force: bool):
    """Import incoming bundles, then publish."""
    repo = state.repo
    with repo.lock():
        ok = _do_import(repo, [])
        _do_publish(repo, force, False)
    if not ok:
        sys.exit(1)


@cli.command('list')
@click.argument('component', required=False)
@click.option('--arch', help='Only list images of this architecture.')
@click.pass_obj
def list_cmd(state: _CmdContext, component: str | None, arch: str | None):
    """List the images the index lists, newest first."""
    repo = state.repo
    from manifold_mrepo.manage import list_images

    for image in _run(list_images, repo, component, arch):
        click.echo(
            '{}\t{}-{}\t{}\t{}'.format(
                image.component_id, image.version, image.revision, image.architecture, image.verity_hash
            )
        )


@cli.command()
@click.argument('component')
@click.argument('version', required=False)
@click.option('--arch', help='Only remove images of this architecture.')
@click.pass_obj
def remove(state: _CmdContext, component: str, version: str | None, arch: str | None):
    """Remove a component from the index, or only one of its versions."""
    repo = state.repo
    from manifold_mrepo.manage import remove_images

    with repo.lock():
        images = _run(remove_images, repo, component, version, arch)
    if not images:
        raise click.ClickException('Nothing matched')
    for image in images:
        click.echo('Removed {}'.format(image.describe()))


@cli.command('pending-uploads')
@click.option(
    '--link-to',
    type=click.Path(path_type=Path, file_okay=False),
    help='Link the images to this directory, under the names they have to be uploaded as.',
)
@click.pass_obj
def pending_uploads(state: _CmdContext, link_to: Path | None):
    """List the images that are not found at their external URL yet."""
    repo = state.repo
    from manifold_mrepo.external import find_pending_uploads
    from manifold_mrepo.utils.files import link_or_copy

    with repo.lock(), repo.db.session() as session:
        pending = _run(find_pending_uploads, repo, session)
        for upload in pending:
            click.echo('{}\t{}'.format(upload.image.zck_name, upload.url))
            if link_to is not None:
                link_or_copy(upload.path, link_to / upload.image.zck_name)


@cli.command('external-files')
@click.pass_obj
def external_files(state: _CmdContext):
    """List the images that have to stay at their external URL, so the others can be removed from there."""
    repo = state.repo
    from manifold_mrepo.external import find_external_files

    with repo.lock(), repo.db.session() as session:
        for external in _run(find_external_files, repo, session):
            click.echo('{}\t{}'.format(external.image.zck_name, external.url))


@cli.command()
@click.pass_obj
def check(state: _CmdContext):
    """Check that the public tree holds every file of the repository, undamaged."""
    repo = state.repo
    from manifold_mrepo.manage import check_files

    problems = _run(check_files, repo)
    for problem in problems:
        click.echo(problem, err=True)
    if problems:
        sys.exit(1)
    click.echo('All files are fine')


def main():
    cli(prog_name='manifold-mrepo')
