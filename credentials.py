"""Credenciales de una cuenta EDUS en el almacén nativo de Windows."""

from keyring.backends.Windows import WinVaultKeyring


SERVICE = "EDUS Bot Public"
ACCOUNT = "Mi cuenta EDUS"


class CredentialStore:
    def __init__(self, service=SERVICE):
        self.vault = WinVaultKeyring()
        self.service = service

    def load(self, account=ACCOUNT):
        value = self.vault.get_password(self.service, account)
        if value is None:
            return None
        identification, separator, password = value.partition("\n")
        if not separator or not identification or not password:
            raise ValueError("La entrada de Windows Credential Manager no es válida.")
        return identification, password

    def save(self, identification, password, account=ACCOUNT):
        if not identification or "\n" in identification or not password:
            raise ValueError("Completa la identificación y la contraseña.")
        self.vault.set_password(self.service, account, f"{identification}\n{password}")

    def delete(self, account=ACCOUNT):
        if self.vault.get_password(self.service, account) is not None:
            self.vault.delete_password(self.service, account)

