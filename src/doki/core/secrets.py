"""Encrypts customer credentials (e.g. SFTP passwords) before they are stored."""
import os

from cryptography.fernet import Fernet, InvalidToken


class SecretError(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = os.environ.get("DOKI_SECRET_KEY")
    if not key:
        raise SecretError("DOKI_SECRET_KEY is not set (generate with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')")
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise SecretError("stored secret cannot be decrypted with the current key") from e
