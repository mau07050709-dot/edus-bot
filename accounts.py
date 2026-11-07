"""Catálogo de cuentas y credenciales protegidas de EDUS."""

from dataclasses import asdict, dataclass, replace
from uuid import uuid4

from credentials import ACCOUNT
from settings import save_settings


IDENTIFICATION_TYPES = {
    "0": "Cédula de identidad en Registro Civil",
    "6": "Identificación temporal/interno",
    "7": "Extranjero con identificación CCSS",
}
PORTAL_TYPE_LABELS = {
    "0": "CEDULA DE IDENTIDAD EN REGISTRO CIVIL",
    "6": "IDENTIFICACION TEMPORAL/INTERNO",
    "7": "EXTRANJERO CON IDENTIFICACION CCSS",
}


@dataclass(frozen=True)
class Account:
    key: str
    alias: str
    kind: str
    establishment: str = ""
    service: str = "MEDICINA"
    specialty: str = "MEDICINA GENERAL"
    booking_preference: str = "first"


class Accounts:
    def __init__(self, path, vault):
        self.path = path
        self.vault = vault

    def migrate(self, settings):
        if settings.accounts or self.vault.load() is None:
            return settings
        account = Account(ACCOUNT, "Cuenta personal", "0")
        return self.persist(settings, [account], account.key)

    def persist(self, settings, accounts, default):
        updated = replace(
            settings, accounts=[asdict(account) for account in accounts], default_account=default,
        )
        save_settings(self.path, updated)
        return updated

    def save(self, settings, key, alias, kind, identification, password):
        if not alias.strip() or kind not in IDENTIFICATION_TYPES:
            raise ValueError("Completa el alias y el tipo de identificación.")
        catalog = [Account(**item) for item in settings.accounts]
        old = next((account for account in catalog if account.key == key), None)
        previous = self.vault.load(old.key) if old else None
        same_identity = old and previous and previous[0] == identification and old.kind == kind
        key = old.key if same_identity else uuid4().hex
        account = (
            replace(old, alias=alias.strip()) if same_identity else Account(key, alias.strip(), kind)
        )
        updated = [account if old and item.key == old.key else item for item in catalog]
        if old is None:
            updated.append(account)
        if not identification or (not password and previous is None):
            raise ValueError("Completa la identificación y la contraseña.")
        password = password or previous[1]
        self.vault.save(identification, password, key)
        default = settings.default_account
        if not default or (old and default == old.key):
            default = key
        try:
            saved = self.persist(settings, updated, default)
        except OSError:
            if same_identity:
                self.vault.save(*previous, key)
            else:
                self.vault.delete(key)
            raise
        if old and not same_identity:
            try:
                self.vault.delete(old.key)
            except Exception:
                pass
        return saved, key

    def delete(self, settings, key):
        catalog = [Account(**item) for item in settings.accounts if item["key"] != key]
        default = settings.default_account
        if default == key:
            default = catalog[0].key if catalog else ""
        updated = self.persist(settings, catalog, default)
        try:
            self.vault.delete(key)
        except Exception:
            save_settings(self.path, settings)
            raise
        return updated

    def set_establishment(self, settings, key, establishment):
        return self.update_preferences(settings, key, establishment=establishment)

    def update_preferences(self, settings, key, **preferences):
        catalog = [
            replace(Account(**item), **preferences)
            if item["key"] == key else Account(**item)
            for item in settings.accounts
        ]
        return self.persist(settings, catalog, settings.default_account)
