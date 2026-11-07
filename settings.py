"""Configuración local de EDUS Bot."""

import json
import os
import re
from dataclasses import dataclass, asdict, field
from pathlib import Path
from tempfile import NamedTemporaryFile

from monitoring import MAX_INTERVAL_SECONDS, MAX_QUERIES, MIN_INTERVAL_SECONDS


@dataclass(frozen=True)
class Settings:
    establishment: str = "Establecimiento por verificar"
    preparation: str = "04:50"
    opening: str = "05:00"
    clock_24h: bool = True
    brave_path: str = ''
    accounts: list = field(default_factory=list)
    default_account: str = ''
    mode: str = "scheduled"
    watch_interval: int = 60
    watch_limit: int = 12
    watch_start: str = ""
    watch_end: str = ""

    def __post_init__(self):
        if self.mode not in ("scheduled", "monitor"):
            raise ValueError("El modo de EDUS Bot no es válido.")
        for value, minimum, maximum in (
            (self.watch_interval, MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS),
            (self.watch_limit, 1, MAX_QUERIES),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError("Los límites de vigilancia no son válidos.")
        for value in (self.watch_start, self.watch_end):
            if not isinstance(value, str) or (value and not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value)):
                raise ValueError("Los horarios de vigilancia deben tener el formato HH:mm.")
        if not isinstance(self.brave_path, str):
            raise ValueError('Las preferencias de Brave no son válidas.')
        keys = set()
        if not isinstance(self.accounts, list) or not isinstance(self.default_account, str):
            raise ValueError("El catálogo de cuentas no es válido.")
        for account in self.accounts:
            if (
                not isinstance(account, dict)
                or not {"key", "alias", "kind", "establishment"} <= account.keys()
                or account.keys() - {"key", "alias", "kind", "establishment", "service", "specialty", "booking_preference"}
                or not all(isinstance(value, str) for value in account.values())
                or not account["alias"].strip()
                or account.get("booking_preference", "first") not in ("first", "earliest")
                or any(not account.get(field, "default").strip() for field in ("service", "specialty"))
                or account["kind"] not in ("0", "6", "7")
                or not re.fullmatch(r"(?:[a-f0-9]{32}|Mi cuenta EDUS)", account["key"])
                or account["key"] in keys
            ):
                raise ValueError("Hay una cuenta inválida en la configuración.")
            keys.add(account["key"])
        if self.default_account and self.default_account not in keys:
            raise ValueError("La cuenta predeterminada no existe.")
        for value in (self.preparation, self.opening):
            if not isinstance(value, str) or not re.fullmatch(
                r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value
            ):
                raise ValueError("Los horarios deben tener el formato HH:mm.")
        if not isinstance(self.establishment, str) or not self.establishment.strip():
            raise ValueError("El establecimiento debe tener un nombre.")
        if not isinstance(self.clock_24h, bool):
            raise ValueError("La preferencia del reloj debe ser booleana.")


def data_directory():
    return Path(os.environ["LOCALAPPDATA"]) / "EDUS_Bot_Public"


def load_settings(path):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Settings()
    if not isinstance(raw, dict):
        raise ValueError("La configuración debe ser un objeto JSON.")
    fields = asdict(Settings())
    return Settings(**{key: raw[key] for key in fields if key in raw})


def save_settings(path, settings):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as temporary:
            temp_path = Path(temporary.name)
            json.dump(asdict(settings), temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)



