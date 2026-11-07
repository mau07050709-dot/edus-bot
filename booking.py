"""Selección de cupos y coordinación de una confirmación verificable."""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from sqlite3 import Error as DatabaseError, connect
from typing import Protocol
from uuid import uuid4


PREFERENCES = {
    "first": "Primera cita disponible",
    "earliest": "Fecha y hora más tempranas",
}


@dataclass(frozen=True)
class Offer:
    cells: tuple[str, ...]
    has_details: bool

    @property
    def key(self):
        date, time, number, room, clinician = self.cells
        return ("number", number) if number else ("slot", date, time, room, clinician)


@dataclass(frozen=True)
class Appointment:
    account: str
    establishment: str
    service: str
    specialty: str
    date: str
    time: str
    number: str


@dataclass(frozen=True)
class Receipt:
    appointment: Appointment
    reference: str


@dataclass(frozen=True)
class Outcome:
    state: str
    message: str
    receipt: Receipt | None = None


class ConfirmationNotSent(Exception):
    """La acción se detuvo de forma comprobable antes de pulsar el control."""


class DetailClickUncertain(Exception):
    """No se pudo comprobar el resultado de pulsar el control de la fila."""


class BookingPortal(Protocol):
    """Contrato para una pantalla de reserva y un listado ya verificados."""

    def open_details(self, appointment: Appointment) -> Appointment | None: ...
    def ready_to_confirm(self) -> bool: ...
    def confirm(self) -> None: ...
    def verify(self) -> Receipt | None: ...


def unique_offers(offers):
    unique = {}
    for offer in offers:
        earlier = unique.get(offer.key)
        if earlier is not None and earlier.cells != offer.cells:
            raise ValueError("EDUS muestra datos incompatibles para el mismo número de cita.")
        if earlier is None or offer.has_details:
            unique[offer.key] = offer
    return tuple(unique.values())


def appointment_time(offer):
    return parse_appointment_time(*offer.cells[:2])


def parse_appointment_time(date, hour):
    date = date.replace("-", "/").replace(".", "/")
    hour = hour.replace(".", "").upper()
    hour = " ".join(hour.split()).replace("A M", "AM").replace("P M", "PM")
    for date_format in ("%d/%m/%Y", "%Y/%m/%d"):
        for hour_format in ("%H:%M", "%I:%M %p", "%H:%M:%S"):
            try:
                return datetime.strptime(f"{date} {hour}", f"{date_format} {hour_format}")
            except ValueError:
                continue
    raise ValueError("No se pudo ordenar la fecha y hora que muestra EDUS.")


def select_offer(offers, preference):
    if not offers:
        return None
    return min(offers, key=appointment_time) if preference == "earliest" else offers[0]


class BookingJournal:
    """Reserva una única tentativa persistente antes de pulsar Confirmar."""

    def __init__(self, path: Path):
        self.path = path

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = connect(self.path, timeout=3)
        db.execute("""
            CREATE TABLE IF NOT EXISTS attempt (
                account TEXT PRIMARY KEY, token TEXT NOT NULL, status TEXT NOT NULL,
                date TEXT, time TEXT, reference TEXT
            )
        """)
        return db

    def state(self, account):
        if not self.path.exists():
            return None
        with closing(self.connect()) as db:
            row = db.execute("SELECT status FROM attempt WHERE account = ?", (account,)).fetchone()
            return row[0] if row else None

    def claim(self, account):
        token = uuid4().hex
        with closing(self.connect()) as db, db:
            result = db.execute(
                "INSERT OR IGNORE INTO attempt(account, token, status) VALUES (?, ?, 'pending')",
                (account, token),
            )
            return token if result.rowcount == 1 else None

    def discard_unsent(self, account, token):
        with closing(self.connect()) as db, db:
            db.execute(
                "DELETE FROM attempt WHERE account = ? AND token = ? AND status = 'pending'",
                (account, token),
            )

    def confirm(self, account, token, receipt):
        with closing(self.connect()) as db, db:
            result = db.execute(
                """UPDATE attempt SET status = 'confirmed', date = ?, time = ?, reference = ?
                   WHERE account = ? AND token = ? AND status = 'pending'""",
                (receipt.appointment.date, receipt.appointment.time, receipt.reference, account, token),
            )
            if result.rowcount != 1:
                raise RuntimeError("No se pudo registrar la verificación de la cita.")


def stage_booking(appointment, control_verified, portal, journal, guard, progress):
    """La falta de un adaptador verificado mantiene el flujo en intervención."""

    if portal is None or not control_verified:
        return Outcome(
            "Intervención necesaria",
            f"Cupo sugerido para {appointment.date} a las {appointment.time}. Revisa la cita en Brave.",
        )
    token = None
    sent = False
    detail_uncertain = False
    try:
        guard()
        token = journal.claim(appointment.account)
        if token is None:
            return Outcome("Intervención necesaria", "Existe un intento anterior. Verifica Listado de Citas.")
        progress("Seleccionando cita")
        progress("Abriendo detalle de cita")
        if portal.open_details(appointment) != appointment:
            return Outcome("Intervención necesaria", "El detalle no coincide o el cupo ya no está disponible.")
        guard()
        if not portal.ready_to_confirm():
            return Outcome("Intervención necesaria", "La pantalla cambió o requiere intervención en Brave.")
        guard()
        progress("Confirmando reserva")
        sent = True
        try:
            portal.confirm()
        except ConfirmationNotSent:
            sent = False
            return Outcome("Error", "La confirmación se detuvo antes del envío.")
        except Exception:
            # Un error de transporte no permite saber si se procesó el envío.
            pass
        receipt = portal.verify()
        if receipt is not None and receipt.reference and receipt.appointment == appointment:
            journal.confirm(appointment.account, token, receipt)
            return Outcome(
                "Reserva confirmada",
                f"Cita confirmada para {appointment.date} a las {appointment.time}.",
                receipt,
            )
    except DetailClickUncertain:
        detail_uncertain = True
        return Outcome("Intervención necesaria", "No se pudo verificar qué ocurrió al abrir la cita. Revisa Brave y Listado de Citas.")
    except Exception:
        if not sent:
            return Outcome("Error", "La selección se detuvo antes de enviar la confirmación.")
    finally:
        if token is not None and not sent and not detail_uncertain:
            try:
                journal.discard_unsent(appointment.account, token)
            except (DatabaseError, OSError):
                # Si no es posible retirar el intento, el bloqueo se conserva.
                pass
    return Outcome("Intervención necesaria", "Resultado sin verificar. Revisa Listado de Citas antes de intentar otra reserva.")
