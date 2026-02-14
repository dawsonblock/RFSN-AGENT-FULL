"""HMAC-SHA256 signing and verification of replay bundles.

Signs the canonical manifest content so that any tampering can be
detected. The signing key is read from RFSN_REPLAY_SIGNING_KEY env var
or falls back to a deterministic key derived from the policy hash.
"""

import hashlib
import hmac
import json
import os
from typing import Optional


_DEFAULT_KEY = b"rfsn-replay-default-key-change-me"


def _get_signing_key() -> bytes:
    """Resolve the signing key from environment or fallback."""
    env_key = os.getenv("RFSN_REPLAY_SIGNING_KEY")
    if env_key:
        return env_key.encode()
    # Derive from policy hash if available
    policy_hash = os.getenv("RFSN_POLICY_HASH", "")
    if policy_hash:
        return hashlib.sha256(f"rfsn-sign-{policy_hash}".encode()).digest()
    return _DEFAULT_KEY


def sign_manifest(manifest: dict, key: Optional[bytes] = None) -> str:
    """Compute HMAC-SHA256 signature of a manifest dict.

    The manifest is serialized with sorted keys and no whitespace
    to produce a canonical representation.

    Returns:
        hex-encoded HMAC signature
    """
    key = key or _get_signing_key()
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()


def verify_manifest(
    manifest: dict,
    expected_signature: str,
    key: Optional[bytes] = None,
) -> bool:
    """Verify a manifest's HMAC-SHA256 signature.

    Returns True if the signature matches.
    """
    actual = sign_manifest(manifest, key)
    return hmac.compare_digest(actual, expected_signature)


def sign_bundle(bundle_dir: str, key: Optional[bytes] = None) -> str:
    """Sign a replay bundle's manifest.json and write the signature file.

    Returns the hex signature.
    """
    manifest_path = os.path.join(bundle_dir, "manifest.json")
    sig_path = os.path.join(bundle_dir, "manifest.sig")

    with open(manifest_path) as f:
        manifest = json.load(f)

    sig = sign_manifest(manifest, key)

    with open(sig_path, "w") as f:
        f.write(sig)

    return sig


def verify_bundle(bundle_dir: str, key: Optional[bytes] = None) -> bool:
    """Verify a replay bundle's signature.

    Returns True if valid, False if invalid or missing.
    """
    manifest_path = os.path.join(bundle_dir, "manifest.json")
    sig_path = os.path.join(bundle_dir, "manifest.sig")

    if not os.path.exists(sig_path):
        return False

    with open(manifest_path) as f:
        manifest = json.load(f)

    with open(sig_path) as f:
        expected_sig = f.read().strip()

    return verify_manifest(manifest, expected_sig, key)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage:")
        print("  sign   <bundle_dir>")
        print("  verify <bundle_dir>")
        sys.exit(1)

    cmd = sys.argv[1]
    bundle = sys.argv[2]

    if cmd == "sign":
        sig = sign_bundle(bundle)
        print(f"SIGNED: {sig}")
    elif cmd == "verify":
        ok = verify_bundle(bundle)
        print("VERIFY_OK" if ok else "VERIFY_FAIL")
        sys.exit(0 if ok else 1)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
