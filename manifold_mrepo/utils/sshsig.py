# Copyright (C) 2026 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: MPL-2.0

"""
Reading SSHSig signatures, as ssh-keygen -Y sign writes them.

The format is described in PROTOCOL.sshsig of OpenSSH.
"""

import base64
import struct
import binascii

_ARMOR_BEGIN = '-----BEGIN SSH SIGNATURE-----'
_ARMOR_END = '-----END SSH SIGNATURE-----'
_MAGIC = b'SSHSIG'
_VERSION = 1


class SignatureError(ValueError):
    """The data is not an SSHSig signature that we can read."""


def _read_string(blob: bytes, offset: int) -> tuple[bytes, int]:
    """Read a string of the SSH wire format, and return it with the offset after it."""
    if offset + 4 > len(blob):
        raise SignatureError('The signature is truncated')
    (length,) = struct.unpack_from('>I', blob, offset)
    offset += 4
    if offset + length > len(blob):
        raise SignatureError('The signature is truncated')
    return blob[offset : offset + length], offset + length


def public_key_of_signature(signature: bytes) -> str:
    """
    The public key that made an armored SSHSig signature, in the OpenSSH
    format of a .pub file, without a comment.

    We do not check the signature itself; it only has to be readable.
    """
    try:
        text = signature.decode('ascii').strip()
    except UnicodeDecodeError:
        raise SignatureError('This is not an armored SSH signature')
    if not text.startswith(_ARMOR_BEGIN) or not text.endswith(_ARMOR_END):
        raise SignatureError('This is not an armored SSH signature')
    body = text[len(_ARMOR_BEGIN) : -len(_ARMOR_END)]
    try:
        blob = base64.b64decode(''.join(body.split()), validate=True)
    except binascii.Error:
        raise SignatureError('The signature is not valid Base64')

    if not blob.startswith(_MAGIC) or len(blob) < len(_MAGIC) + 4:
        raise SignatureError('This is not an SSHSig signature')
    (version,) = struct.unpack_from('>I', blob, len(_MAGIC))
    if version != _VERSION:
        raise SignatureError('Version {} of SSHSig is not supported'.format(version))

    # the public key comes first, as a blob of the wire format that starts with the type of the key
    key_blob, _ = _read_string(blob, len(_MAGIC) + 4)
    key_type, _ = _read_string(key_blob, 0)
    try:
        key_type_name = key_type.decode('ascii')
    except UnicodeDecodeError:
        raise SignatureError('The key of the signature has no valid type')
    if not key_type_name or not key_type_name.isprintable() or ' ' in key_type_name:
        raise SignatureError('The key of the signature has no valid type')

    return '{} {}'.format(key_type_name, base64.b64encode(key_blob).decode('ascii'))
