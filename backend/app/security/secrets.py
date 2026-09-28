from cryptography.fernet import Fernet, InvalidToken
from app.core.config import settings
from app.core.errors import DomainError


def cipher():
    if not settings().encryption_key:
        raise DomainError("Set DFB_ENCRYPTION_KEY before storing platform credentials")
    try:
        return Fernet(settings().encryption_key.encode())
    except ValueError:
        raise DomainError("DFB_ENCRYPTION_KEY must be a Fernet key")


def encrypt(value):
    return cipher().encrypt(value.encode()).decode()


def decrypt(value):
    try:
        return cipher().decrypt(value.encode()).decode()
    except InvalidToken:
        raise DomainError("Platform token cannot be decrypted with this installation key")
