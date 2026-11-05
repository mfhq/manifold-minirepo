# Manifold Minirepo Tool

`manifold-mrepo` builds repositories of [Manifold](https://github.com/mfhq/manifold) images.

It takes bundles from an incoming directory, keeps track of them in a small
database, and publishes a static tree of files that any web server can serve.
The tree holds the images as zchunk files, so clients only download what
changed, together with their manifests and an AppStream catalog.

## Requirements

* Python 3.12 or newer, with SQLAlchemy 2, click, zstandard, blake3 and PyGObject
* libmanifold with its GObject Introspection data, and AppStream 1.2.2 or newer with
  its compose library
* `ssh-keygen`


## Installing

```console
$ pip install .
```

To run it from the source tree instead, use `python3 -m manifold_mrepo`.

## Setting up a repository

A repository is a directory with a configuration file, `mrepo.toml`.
`data/mrepo.toml.example` lists all settings. A minimal one:

```toml
[repository]
id = "example"
summary = "Example applications and their runtimes"
url = "https://example.org/manifold"
signing-key = "keys/repo"
```

The root index is signed with `signing-key`, which can be a key file, the name of a key
of `manifold key new`, or the fingerprint of a key in ssh-agent:

```console
$ ssh-keygen -t ed25519 -f keys/repo
```

Bundles have to be signed by a key that the repository trusts. Copy the public keys
of the uploaders to `trusted-keys/`:

```console
$ cp ~/.config/manifold/keys/default.pub trusted-keys/uploader.pub
```


## Adding bundles

Put bundles into `incoming/`, then import and publish them:

```console
$ manifold-mrepo import
$ manifold-mrepo publish
```

`manifold-mrepo update` does both. An imported bundle is removed from `incoming/`. A
bundle that is rejected is moved to `incoming/rejected/`, next to a file that says why.

A new revision of a version replaces the previous revision. The index lists the
newest `keep-versions` versions of every component, and keeps any older image that
a listed image builds on. Removed images stay downloadable for `grace-days`, for
clients that still have an older index.

`publish` only writes a new tree if something changed, or if the root index has
used up half of its validity. Run `manifold-mrepo update` from a timer, daily for
example, so the root index never expires.
