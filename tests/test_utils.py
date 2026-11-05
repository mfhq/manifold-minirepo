# Copyright (C) 2026 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: MPL-2.0

from manifold_mrepo.utils.files import rdns_dir
from manifold_mrepo.utils.hashing import hash_bytes
from manifold_mrepo.utils.renameat2 import exchange_paths


def test_rdns_dir():
    assert rdns_dir('org.example') == 'org/example'
    assert rdns_dir('com.example.myapp') == 'com/example/myapp'
    assert rdns_dir('com.example.myapp.really.long') == 'com/example/myapp/really.long'


def test_hashes():
    hashes = hash_bytes(b'abc')
    assert hashes.size == 3
    assert hashes.sha256 == 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
    assert hashes.blake3 == '6437b3ac38465133ffb63b75273a8db548c558465d79db03fd359c6cd5bd9d85'


def test_exchange_paths(tmp_path):
    a = tmp_path / 'a'
    b = tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    (a / 'from-a').touch()
    (b / 'from-b').touch()
    exchange_paths(a, b)
    assert (a / 'from-b').exists()
    assert (b / 'from-a').exists()
