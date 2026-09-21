"""Create fresh development login keys; never copies the old server's key."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
from pathlib import Path


def initialize(app: Path, check: bool = False) -> str:
    from Cryptodome.PublicKey import RSA

    app = app.resolve()
    private = app / "conf/private.pem"
    public = app / "conf/public.pem"
    frontend = app / "web/src/utils/index.ts"
    text = frontend.read_text(encoding="utf-8")
    pattern = r"-----BEGIN PUBLIC KEY-----[A-Za-z0-9+/=\r\n]+-----END PUBLIC KEY-----"
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise ValueError("Expected exactly one frontend login public key; inspect the current code first.")

    if private.exists() != public.exists():
        raise ValueError("An incomplete key pair exists. No files were overwritten.")
    if private.exists():
        key = RSA.import_key(private.read_bytes(), passphrase="Welcome")
        pub = RSA.import_key(public.read_bytes())
        if key.publickey().export_key() != pub.export_key():
            raise ValueError("Existing public/private keys do not match. No files were overwritten.")
    else:
        if check:
            raise ValueError("Development keys are missing. Run initialization first.")
        key = RSA.generate(2048)
        private.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(key.export_key())
        with public.open("xb") as stream:
            stream.write(key.publickey().export_key())

    pem = key.publickey().export_key().decode("ascii")
    inline = pem.replace("\n", "").replace("\r", "")
    if check:
        if matches[0].group(0).replace("\n", "").replace("\r", "") != inline:
            raise ValueError("Frontend public key differs. Run initialization and rebuild the web app.")
    else:
        frontend.write_text(re.sub(pattern, lambda _: inline, text), encoding="utf-8")
    return hashlib.sha256(key.publickey().export_key(format="DER")).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", type=Path, default=Path(__file__).resolve().parents[2] / "app")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        fingerprint = initialize(args.app_dir, args.check)
    except (ImportError, OSError, ValueError) as error:
        parser.exit(1, f"Login key setup failed: {error}\n")
    print(f"Login key pair verified; public SHA-256: {fingerprint}")
    if not args.check:
        print("Rebuild the frontend before logging in. Keep conf/private.pem out of Git.")


if __name__ == "__main__":
    main()
