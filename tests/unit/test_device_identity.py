"""utils/device_identity.py: hardware serial + trust-chain identity readers.

The fingerprint must equal what Noktura computes (SHA-256 of the DER SPKI)
and what `openssl pkey -pubin -outform DER | sha256sum` prints, or the app's
"identity matches record" check fails on every unit.
"""

import base64
import hashlib

import pytest

from utils import device_identity
from utils.device_identity import pem_fingerprint, read_identity

# A P-256 SPKI DER: the fixed 26-byte algorithm header, then 0x04 + 64 bytes
# of uncompressed point. The point bytes are synthetic (the fingerprint is a
# hash of the DER, so it does not need to be a valid curve point).
P256_SPKI_HEADER = bytes.fromhex('3059301306072a8648ce3d020106082a8648ce3d030107034200')
P256_DER = P256_SPKI_HEADER + b'\x04' + bytes(range(64))
assert len(P256_DER) == 91
P256_DER_B64 = base64.b64encode(P256_DER).decode()


def pem(b64=P256_DER_B64, kind='PUBLIC KEY'):
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return f'-----BEGIN {kind}-----\n' + '\n'.join(lines) + f'\n-----END {kind}-----\n'


@pytest.mark.unit
class TestPemFingerprint:
    def test_matches_sha256_of_der(self):
        der = base64.b64decode(P256_DER_B64)
        assert pem_fingerprint(pem()) == hashlib.sha256(der).hexdigest()

    def test_whitespace_and_crlf_do_not_change_it(self):
        assert pem_fingerprint(pem().replace('\n', '\r\n')) == pem_fingerprint(pem())
        assert pem_fingerprint(pem() + '\n\n') == pem_fingerprint(pem())

    def test_rejects_private_keys_and_junk(self):
        assert pem_fingerprint(pem(kind='PRIVATE KEY')) is None
        assert pem_fingerprint(pem(kind='EC PRIVATE KEY')) is None
        assert pem_fingerprint('not a key') is None
        assert pem_fingerprint('') is None
        assert pem_fingerprint(None) is None

    def test_rejects_invalid_base64(self):
        assert pem_fingerprint('-----BEGIN PUBLIC KEY-----\n!!!!\n-----END PUBLIC KEY-----') is None
        assert pem_fingerprint('-----BEGIN PUBLIC KEY-----\n-----END PUBLIC KEY-----') is None


@pytest.mark.unit
class TestReadIdentity:
    @pytest.fixture(autouse=True)
    def serial(self, monkeypatch):
        monkeypatch.setattr(device_identity, '_cached', True)
        monkeypatch.setattr(device_identity, '_serial', '10000000a3f1c29b')

    def test_no_identity_dir_is_tier_none_with_hw_id(self, tmp_path):
        ident = read_identity(tmp_path / 'missing')
        assert ident == {'hw_id': '10000000a3f1c29b', 'tier': 'none',
                         'fingerprint': None, 'noktura_serial': None}

    def test_otp_identity(self, tmp_path):
        (tmp_path / 'tier').write_text('otp\n')
        (tmp_path / 'device.pub').write_text(pem())
        (tmp_path / 'noktura_serial').write_text('OWL-2026-000001\n')
        ident = read_identity(tmp_path, include_pem=True)
        assert ident['tier'] == 'otp'
        assert ident['fingerprint'] == pem_fingerprint(pem())
        assert ident['noktura_serial'] == 'OWL-2026-000001'
        assert ident['public_key_pem'].startswith('-----BEGIN PUBLIC KEY-----')

    def test_pem_not_included_by_default(self, tmp_path):
        (tmp_path / 'tier').write_text('disk')
        (tmp_path / 'device.pub').write_text(pem())
        assert 'public_key_pem' not in read_identity(tmp_path)

    def test_tier_without_a_readable_key_degrades_to_none(self, tmp_path):
        (tmp_path / 'tier').write_text('otp')
        ident = read_identity(tmp_path)
        assert ident['tier'] == 'none'
        assert ident['fingerprint'] is None

    def test_private_key_in_device_pub_is_not_reported(self, tmp_path):
        # A mis-run of owl_identity.sh must never leak a private key as identity
        (tmp_path / 'tier').write_text('disk')
        (tmp_path / 'device.pub').write_text(pem(kind='EC PRIVATE KEY'))
        ident = read_identity(tmp_path, include_pem=True)
        assert ident['tier'] == 'none'
        assert ident['public_key_pem'] is None

    def test_unknown_tier_and_bad_serial_are_ignored(self, tmp_path):
        (tmp_path / 'tier').write_text('quantum')
        (tmp_path / 'device.pub').write_text(pem())
        (tmp_path / 'noktura_serial').write_text('OWL-1')
        ident = read_identity(tmp_path)
        assert ident['tier'] == 'none'
        assert ident['noktura_serial'] is None

    def test_serial_shape(self, tmp_path):
        (tmp_path / 'noktura_serial').write_text('OWL-2026-000042')
        assert read_identity(tmp_path)['noktura_serial'] == 'OWL-2026-000042'
