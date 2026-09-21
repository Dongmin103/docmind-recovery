"""Decrypt an authenticated DocMind recovery archive. Requires cryptography.

Example: python restore_backup.py backup.enc --key /private/recovery.key --output recovery-private.tar
The key is a separate binary file containing exactly 32 bytes. Never commit it.
This command decrypts only; it does not change a checkout or running service.
"""
import argparse
import hashlib
import os
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"DOCMIND-RECOVERY-AES256GCM-V1\n"

def decrypt(source, key_path, destination):
    key = key_path.read_bytes()
    if len(key) != 32:
        raise ValueError("Recovery key must contain exactly 32 bytes")
    if destination.exists():
        raise FileExistsError("Refusing to overwrite the destination")
    temporary = destination.with_name(destination.name + ".unverified")
    if temporary.exists():
        raise FileExistsError("Temporary output already exists")
    total = source.stat().st_size
    if total < len(MAGIC) + 12 + 16:
        raise ValueError("Truncated recovery archive")
    digest = hashlib.sha256()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with source.open("rb") as inp:
            if inp.read(len(MAGIC)) != MAGIC:
                raise ValueError("Unknown recovery archive format")
            nonce = inp.read(12)
            inp.seek(-16, os.SEEK_END)
            tag = inp.read(16)
            inp.seek(len(MAGIC) + 12)
            decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
            decryptor.authenticate_additional_data(MAGIC)
            remaining = total - len(MAGIC) - 12 - 16
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                while remaining:
                    block = inp.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError("Truncated recovery archive")
                    remaining -= len(block)
                    plain = decryptor.update(block)
                    digest.update(plain)
                    out.write(plain)
                final = decryptor.finalize()
                digest.update(final)
                out.write(final)
        os.rename(temporary, destination)
        return digest.hexdigest()
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("source", type=Path)
    p.add_argument("--key", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    a = p.parse_args()
    print("Decrypted archive SHA-256:", decrypt(a.source, a.key, a.output))
