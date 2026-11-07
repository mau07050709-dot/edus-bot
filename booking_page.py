"""Adaptador de reserva para pantallas que exponen evidencia suficiente."""

import re

from uuid import uuid4

from playwright.sync_api import Error

from booking import ConfirmationNotSent, DetailClickUncertain, Receipt, parse_appointment_time


DETAIL_TITLES = re.compile(r"^(?:Detalles? de la cita|Confirmar reserva|Confirmar cita)$", re.I)
CONFIRM = re.compile(r"^(?:Confirmar|Reservar|Guardar) (?:la )?(?:cita|reserva)$", re.I)
ACTION = 'button, a, [role="button"]'
DETAIL_ICONS = ".pi-plus, .ui-icon-plus, .ui-icon-plusthick, .fa-plus, .fa-plus-circle, .fa-circle-plus"
CONFIRM_ICONS = ".pi-check, .pi-check-circle, .ui-icon-check, .ui-icon-circle-check, .fa-check, .fa-check-circle, .fa-circle-check"
EXTRA_FIELDS = (
    'input:not([type="hidden"]):visible, textarea:visible, select:visible, '
    '[contenteditable="true"]:visible, [role="textbox"]:visible, [role="combobox"]:visible'
)


def compact(value):
    return " ".join(value.split())


def exact(value):
    return re.compile(r"^\s*" + re.escape(value).replace(r"\ ", r"\s+") + r"\s*$", re.I)


def establishment_name(value):
    return re.sub(r"^\d+\s*[-–—]\s*", "", value)


def detail_control(table, offer):
    header = table.locator("thead th:visible").filter(has_text=exact("Ver cita"))
    if header.count() != 1 or header.and_(table.locator("thead th:nth-child(6)")).count() != 1:
        return None
    candidates = table.locator("tbody tr:visible")
    for index, value in enumerate(offer.cells, start=1):
        cell = table.page.locator(f"td:nth-child({index})").filter(has_text=exact(value))
        candidates = candidates.filter(has=cell)
    # Los duplicados visuales son válidos para mostrar disponibilidad, pero no para pulsar una fila.
    if candidates.count() != 1:
        return None
    cell = candidates.locator("td:nth-child(6)")
    button = cell.locator(ACTION).filter(visible=True).and_(
        cell.get_by_role("button", name="Ver cita", exact=True)
        .or_(cell.get_by_role("link", name="Ver cita", exact=True))
        .or_(cell.get_by_title("Ver cita", exact=True))
        .or_(cell.locator(ACTION).filter(has_text=exact("+")))
        .or_(cell.locator(ACTION).filter(has=table.page.locator(f":is({DETAIL_ICONS}):visible")))
        .or_(cell.locator(f":is({ACTION}):is({DETAIL_ICONS})"))
    )
    return button if button.count() == 1 and button.is_enabled(timeout=800) else None


class EdusBookingPage:
    def __init__(self, page, table, offer, identification, allowed, cancelled):
        self.page = page
        self.table = table
        self.offer = offer
        self.identification = identification
        self.allowed = allowed
        self.cancelled = cancelled
        self.appointment = None
        self.submit = None
        self.first_stage = "edusConfirm_" + uuid4().hex
        self.origin_document = "edusDocument_" + uuid4().hex

    def detail_view(self):
        title = self.page.get_by_text(DETAIL_TITLES).filter(visible=True)
        dialogs = self.page.locator(':is([role="dialog"], .ui-dialog):visible').filter(has=title)
        main = self.page.locator(':is(main,[role="main"]):visible').filter(has=title).filter(
            has_not=self.page.locator(':is([role="dialog"], .ui-dialog):visible').filter(has=title)
        )
        return dialogs.or_(main)

    def verified_detail(self, view, appointment):
        if not self.allowed(self.page.url) or view.count() != 1 or view.locator(EXTRA_FIELDS).count():
            return None
        values = (appointment.number, appointment.date, appointment.time)
        if not all(value and view.get_by_text(exact(value)).filter(visible=True).count() == 1 for value in values):
            return None
        # El detalle observado usa el nombre; Solicitar Cita muestra además el código.
        name = establishment_name(appointment.establishment)
        establishment = view.get_by_text(exact(appointment.establishment)).or_(view.get_by_text(exact(name)))
        if not name or establishment.filter(visible=True).count() != 1:
            return None
        same_document = self.page.evaluate("(key) => window[key] === true", self.origin_document)
        if not same_document and (
            not self.identification
            or view.get_by_text(exact(self.identification)).filter(visible=True).count() != 1
            or view.get_by_text(re.compile(r"^(?:N[.°º.º ]* )?Identificación:?$", re.I)).filter(visible=True).count() != 1
        ):
            return None
        labels = ("Fecha", "Hora de Cita", "Establecimiento")
        if not all(view.get_by_text(re.compile(rf"^{re.escape(label)}:?$", re.I)).filter(visible=True).count() == 1 for label in labels):
            return None
        if view.get_by_text(re.compile(r"^N[.°º.º ]* de Cita:?$", re.I)).filter(visible=True).count() != 1:
            return None
        # Si la pantalla declara el servicio o la especialidad, su valor también debe coincidir.
        for label, expected in (("Servicio", appointment.service), ("Especialidad", appointment.specialty)):
            if view.get_by_text(re.compile(rf"^{label}:?$", re.I)).filter(visible=True).count() and (
                view.get_by_text(exact(expected)).filter(visible=True).count() != 1
            ):
                return None
        confirm = view.get_by_role("button", name=CONFIRM).filter(visible=True)
        # La pantalla de EDUS con visto bueno puede no exponer un texto accesible.
        if (
            view.get_by_text("Detalles de la Cita", exact=True).filter(visible=True).count() == 1
            and all(view.get_by_text(exact(label)).filter(visible=True).count() == 1 for label in (
                "Consultorio", "Funcionario", "Medio de Atención", "Ubicación",
            ))
            and all(value and view.get_by_text(exact(value)).filter(visible=True).count() == 1
                    for value in self.offer.cells[3:])
        ):
            check = view.locator(ACTION).filter(visible=True).and_(
                view.locator(ACTION).filter(has_text=re.compile(r"^\s*[✓✔]\s*$"))
                .or_(view.locator(ACTION).filter(has=self.page.locator(f":is({CONFIRM_ICONS}):visible")))
                .or_(view.locator(f":is({ACTION}):is({CONFIRM_ICONS})"))
            )
            confirm = confirm.or_(check)
        return confirm if confirm.count() == 1 and confirm.is_enabled(timeout=800) else None

    def open_details(self, appointment):
        if not appointment.number or not self.allowed(self.page.url) or self.cancelled():
            return None
        self.appointment = appointment
        if self.listed_receipt() is not None:
            return None
        control = detail_control(self.table, self.offer)
        if control is None:
            return None
        self.page.evaluate("(key) => window[key] = true", self.origin_document)
        if self.cancelled():
            return None
        if self.detail_view().count():
            return None
        try:
            control.click(timeout=3_000)
            view = self.detail_view()
            gone = self.page.get_by_text(re.compile(r"^Cupo no disponible\.?$", re.I)).filter(visible=True)
            view.or_(gone).wait_for(state="visible", timeout=5_000)
            if gone.count() == 1:
                return None
            self.submit = self.verified_detail(view, appointment)
        except Error as error:
            raise DetailClickUncertain from error
        if self.submit is None:
            return None
        return appointment

    def ready_to_confirm(self):
        if self.appointment is None or self.cancelled():
            return False
        self.submit = self.verified_detail(self.detail_view(), self.appointment)
        return self.submit is not None

    def confirm(self):
        try:
            self.submit.evaluate(
                r"""(button, key) => {
                    const name = button.getAttribute('aria-label') || button.innerText || '';
                    window[key] = {button, canContinue: /^(Reservar|Guardar)\s/i.test(name.trim())};
                }""", self.first_stage, timeout=800,
            )
        except Error as error:
            raise ConfirmationNotSent from error
        if self.cancelled():
            raise ConfirmationNotSent
        self.submit.click(timeout=3_000)

    def listed_receipt(self):
        if not self.allowed(self.page.url) or not self.identification:
            return None
        if not self.page.get_by_text("Listado de Citas", exact=True).filter(visible=True).count():
            return None
        appointment = self.appointment
        tables = self.page.locator("table:visible").filter(
            has=self.page.get_by_text("Fecha y Hora", exact=True)
        ).filter(has=self.page.get_by_text("Establecimiento", exact=True)).filter(
            has=self.page.get_by_text("Servicio", exact=True)
        )
        if tables.count() != 1:
            return None
        headers = [compact(label).casefold() for label in tables.locator("thead th").all_text_contents()]
        number_column = next((i for i, label in enumerate(headers) if re.fullmatch(r"n[.°º.º ]* de cita", label)), None)
        patient_column = next((i for i, label in enumerate(headers) if re.fullmatch(r"n[.°º.º ]* identificación", label)), None)
        if number_column is None or patient_column is None:
            return None
        indices = (
            number_column, patient_column, headers.index("fecha y hora"), headers.index("establecimiento"),
            headers.index("servicio"),
        )
        matches = tables.locator("tbody tr:visible").evaluate_all(
            """(rows, indices) => rows.map(row => {
                const cells = row.querySelectorAll(':scope > td');
                return indices.map(i => cells[i]?.innerText ?? '');
            })""", indices,
        )
        expected_when = parse_appointment_time(appointment.date, appointment.time)
        expected_places = {appointment.establishment.casefold(), establishment_name(appointment.establishment).casefold()}
        verified = 0
        for row in matches:
            number, patient, when, place, service = map(compact, row)
            if (
                number != appointment.number or patient != self.identification
                or place.casefold() not in expected_places or service.casefold() != appointment.service.casefold()
            ):
                continue
            date, _, hour = when.partition(" ")
            try:
                if parse_appointment_time(date, hour) == expected_when:
                    verified += 1
            except ValueError:
                continue
        if verified == 1:
            return Receipt(appointment, appointment.number)
        return None

    def verify(self):
        second = self.page.locator(':is([role="dialog"], .ui-dialog):visible').filter(
            has=self.page.get_by_text(re.compile(r"^Confirmar (?:reserva|cita)$", re.I))
        )
        try:
            for _ in range(12):
                receipt = self.listed_receipt()
                if receipt is not None:
                    return receipt
                if not self.cancelled() and self.allowed(self.page.url) and second.count() == 1:
                    button = self.verified_detail(second, self.appointment)
                    if button is not None and second.get_by_role("button", name=re.compile(
                        r"^Confirmar (?:la )?(?:reserva|cita)$", re.I
                    )).count() == 1 and button.evaluate(
                        r"""(button, key) => {
                            const first = window[key];
                            return !!first?.canContinue && !first.button.getClientRects().length
                                && first.button !== button;
                        }""", self.first_stage, timeout=800,
                    ):
                        button.click(timeout=3_000)
                        break
                self.page.wait_for_timeout(250)
            for _ in range(12):
                receipt = self.listed_receipt()
                if receipt is not None:
                    return receipt
                self.page.wait_for_timeout(250)
            return None
        finally:
            if self.allowed(self.page.url) and not self.page.is_closed():
                try:
                    self.page.evaluate(
                        "(keys) => { for (const key of keys) delete window[key]; }",
                        [self.first_stage, self.origin_document],
                    )
                except Error:
                    pass

