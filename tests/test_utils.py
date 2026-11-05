# Copyright (C) 2026 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: MPL-2.0

import base64
import subprocess

import pytest

from manifold_mrepo.utils.files import rdns_dir
from manifold_mrepo.utils.sshsig import SignatureError, public_key_of_signature
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


def test_public_key_of_signature(tmp_path):
    key = tmp_path / 'key'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'comment', '-f', str(key)], check=True)
    signature = subprocess.run(
        ['ssh-keygen', '-Y', 'sign', '-f', str(key), '-n', 'test'], input=b'data', check=True, capture_output=True
    ).stdout
    public_key = public_key_of_signature(signature)
    assert public_key == ' '.join(key.with_suffix('.pub').read_text().split()[:2])

    lines = signature.decode().splitlines()
    blob = base64.b64decode(''.join(lines[1:-1]))
    for broken in (
        b'',
        b'not a signature',
        b'\xff' + signature,
        signature.replace(lines[1][:4].encode(), b'!!!!'),
        # another version, and a key that is cut off
        '\n'.join([lines[0], base64.b64encode(blob[:6] + b'\0\0\0\2' + blob[10:]).decode(), lines[-1]]).encode(),
        '\n'.join([lines[0], base64.b64encode(blob[:20]).decode(), lines[-1]]).encode(),
    ):
        with pytest.raises(SignatureError):
            public_key_of_signature(broken)


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
