# Copyright (C) 2026 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: MPL-2.0

"""
The libraries we use through GObject Introspection.

libmanifold reads and verifies bundles, signs, checks signatures and writes
zchunk files, so mrepo follows the same rules as the Manifold client.
"""

import os
import sysconfig

import gi


def _add_local_typelib_dirs() -> None:
    """
    Find typelibs in /usr/local too.

    GObject Introspection only looks for typelibs below its own prefix and in GI_TYPELIB_PATH,
    while the dynamic linker finds libraries in /usr/local. So a libmanifold that was installed
    to /usr/local loads fine, but its typelib is never found.
    """
    repo = gi.Repository.get_default()
    # NOTE: we can not be sure that older versions of PyGObject have this
    if not hasattr(repo, 'prepend_search_path'):
        return

    libdirs = ['lib64', 'lib']
    multiarch = sysconfig.get_config_var('MULTIARCH')
    if multiarch:
        libdirs.insert(0, 'lib/' + multiarch)
    local_dirs = [os.path.join('/usr/local', libdir, 'girepository-1.0') for libdir in libdirs]

    # GI_TYPELIB_PATH, as `meson devenv` sets it for example, still comes first
    env_dirs = [d for d in os.environ.get('GI_TYPELIB_PATH', '').split(os.pathsep) if d]

    # every directory we prepend comes before the previous one, so we go backwards
    for path in reversed(env_dirs + [d for d in local_dirs if os.path.isdir(d)]):
        repo.prepend_search_path(path)


_add_local_typelib_dirs()

try:
    gi.require_version('GLib', '2.0')
    gi.require_version('Manifold', '1.0')
    gi.require_version('AppStream', '1.0')
    gi.require_version('AppStreamCompose', '1.0')
except ValueError as e:
    # a missing typelib is a missing module to Python
    raise ImportError(str(e)) from e

from gi.repository import GLib, Manifold, AppStream, AppStreamCompose  # noqa: E402

__all__ = ['GLib', 'AppStream', 'AppStreamCompose', 'Manifold', 'component_kind_name']


def component_kind_name(kind: Manifold.ComponentKind) -> str:
    """The name of a component kind, as manifests spell it."""
    return kind.name.lower()
