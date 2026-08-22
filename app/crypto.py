"""Server-signed track records (Ed25519). Public key served at /api/key so
records are portable and verifiable without client crypto."""
import base64, json, os
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from . import config

_key = None

def _key_path():
    return os.path.join(config.DATA_DIR, "signing_key.bin")

def get_key() -> Ed25519PrivateKey:
    global _key
    if _key is None:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        p = _key_path()
        if os.path.exists(p):
            raw = open(p, "rb").read()
            _key = Ed25519PrivateKey.from_private_bytes(raw)
        else:
            _key = Ed25519PrivateKey.generate()
            raw = _key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption())
            with open(p, "wb") as f:
                f.write(raw)
            os.chmod(p, 0o600)
    return _key

def pubkey_b64() -> str:
    pub = get_key().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.urlsafe_b64encode(pub).decode().rstrip("=")

def sign_record(record: dict) -> str:
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    sig = get_key().sign(canonical)
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")
