"""Stable local answer pseudonyms shared by reports and archives."""

import hashlib
import hmac
import os
from pathlib import Path
import secrets


def read_salt(directory):
    path = Path(directory) / "privacy-salt"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(secrets.token_bytes(32))
    return path.read_bytes()


def answer_key(value, labels, salt):
    if value is None or value in labels:
        return value
    return "answer#" + hmac.new(salt, value.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
