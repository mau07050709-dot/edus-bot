"""Vinculación local de una cuenta con el documento autenticado de Brave."""

import hmac
import json
import re
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class SessionLink:
    account: str
    nonce: str

    @classmethod
    def new(cls, account):
        return cls(account, uuid4().hex)

    @property
    def marker(self):
        return "edusVerified_" + self.nonce

    def proof(self, kind, identification):
        return hmac.digest(bytes.fromhex(self.nonce), f"{kind}:{identification}".encode(), "sha256").hex()


def load_link(path):
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        link = SessionLink(**raw)
        if isinstance(link.account, str) and re.fullmatch(r"[0-9a-f]{32}", link.nonce):
            return link
    except (OSError, ValueError, TypeError):
        pass
    return None
