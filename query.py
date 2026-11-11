"""Consulta única mediante los controles visibles de EDUS."""

from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.sync_api import Error, TimeoutError

from activity import costa_rica_now
from booking import Offer, Outcome, unique_offers
from booking_page import ACTION, DETAIL_ICONS
from navigation_actions import navigation_click
from appointments import (
    CHOICES, NO_SLOTS, NavigationWait, appointment_close, appointment_dialog, appointment_table,
    assigned_establishment, choices, open_appointment_dialog,
    select_choice,
)


FILTER_RESPONSE_TIMEOUT = 8


@dataclass(frozen=True)
class Result:
    alias: str
    establishment: str
    service: str
    specialty: str
    timestamp: datetime
    requested_at: datetime
    started_at: datetime
    rows: tuple
    timings: tuple
    offers: tuple[Offer, ...] = ()
    booking: Outcome | None = None
    started_tick: float | None = None

    @property
    def duration(self):
        return self.timings[-1][1]


def current_choice(dialog, label):
    select = choices(dialog, label)
    if select is None:
        raise ValueError(f"No se reconoció el selector de {label}.")
    values = select.locator("option:checked").all_text_contents()
    return values[0].strip() if values else ""


def same_portal_request(page, request):
    return (
        request.resource_type in ("xhr", "fetch")
        and urlsplit(request.url).netloc == "edus.ccss.sa.cr"
        and urlsplit(request.url).scheme == "https"
        and request.frame == page.main_frame
    )


def selected_choice(dialog, label):
    if choices(dialog, label) is None:
        return None
    return (current_choice(dialog, label),)


def select_and_wait(dialog, label, value, wait, guard, on_commit=lambda: None):
    page = dialog.page

    def option_ready():
        select = choices(dialog, label)
        if select is None or not select.is_enabled(timeout=500):
            return None
        option = select.locator("option:enabled").and_(page.get_by_text(value, exact=True))
        return option if option.count() == 1 else None

    wait.until(f"Esperar opción de {label}", option_ready)
    started = monotonic()
    committing = False
    requests = set()
    completed = []

    def request_started(request):
        if committing and same_portal_request(page, request):
            requests.add(request)

    def request_completed(request):
        if request in requests:
            completed.append(request)

    def commit():
        nonlocal committing
        guard()
        committing = True
        on_commit()

    page.on("request", request_started)
    page.on("requestfinished", request_completed)
    page.on("requestfailed", request_completed)
    try:
        retried = False
        while True:
            try:
                select_choice(dialog, label, value, before_select=commit)
                break
            except (Error, ValueError) as error:
                if page.is_closed() or not page.context.browser.is_connected():
                    raise
                if committing or not isinstance(error, Error):
                    if isinstance(error, TimeoutError):
                        raise ValueError(f"EDUS no confirmó la actualización de {label}; no se repetirá el envío.") from error
                    raise
                wait.recover(f"Volver a localizar {label}", type(error).__name__)
                retried = True
                wait.until(f"Esperar opción de {label}", option_ready, timeout=1.5)
        # El presupuesto de respuesta es independiente de localizar y accionar el control.
        deadline = monotonic() + FILTER_RESPONSE_TIMEOUT
        while not completed and monotonic() < deadline:
            if wait.cancelled():
                raise ValueError("Consulta detenida.")
            page.wait_for_timeout(75)
        if not completed:
            raise ValueError(f"EDUS no confirmó la actualización de {label}; no se repetirá el envío.")
        request = completed[0]
        response = request.response()
        if request.failure or response is None or not response.ok:
            status = response.status if response else "sin respuesta"
            raise ValueError(f"EDUS devolvió {status} al seleccionar {label}.")
    finally:
        page.remove_listener("request", request_started)
        page.remove_listener("requestfinished", request_completed)
        page.remove_listener("requestfailed", request_completed)

    wait.until(
        f"Verificar selección de {label}",
        lambda: selected_choice(dialog, label) == (value,),
    )
    if retried:
        wait.progress(f"Recuperación exitosa · {label}.")
    wait.progress(f"{label} verificada: {value}.")
    return monotonic() - started


def close_appointment_dialog(dialog, guard, wait):
    close = wait.until(
        "Localizar cierre de Solicitar Cita",
        lambda: control if (control := appointment_close(dialog)).count() == 1 and control.is_enabled(timeout=500) else None,
    )
    guard()
    try:
        navigation_click(close, timeout=2_500)
    except TimeoutError:
        wait.progress("Comprobando si el formulario se cerró después del clic.")
    wait.until("Cerrar el formulario anterior", lambda: dialog.count() == 0)


def reopen_dialog(page, dialog, guard, wait):
    close_appointment_dialog(dialog, guard, wait)
    guard()
    return open_appointment_dialog(page, wait, guard)


def prepared_form(page, account):
    dialog = appointment_dialog(page)
    if dialog.count() != 1 or assigned_establishment(dialog) != account.establishment:
        return False
    close = appointment_close(dialog)
    if close.count() != 1 or not close.is_enabled(timeout=500):
        return False
    table = appointment_table(dialog)
    if table.count() != 1 or table.locator("tbody").count() != 1:
        return False
    return all(
        (select := choices(dialog, label)) is not None and select.is_enabled(timeout=500)
        and current_choice(dialog, label) == value
        for label, value in zip(CHOICES, (account.service, account.specialty))
    )


def run_query(
    page, account, guard, verify_establishment, progress,
    *, preparing=False, cancelled=lambda: False, on_availability_commit=lambda: None,
):
    started_at = costa_rica_now()
    started = monotonic()
    guard()
    timings = [("Sesión", monotonic() - started)]
    wait = NavigationWait(page, progress, cancelled)

    def check_establishment(dialog):
        verify_establishment(wait.until("Leer el establecimiento", lambda: assigned_establishment(dialog)))

    def chosen(dialog, label):
        return wait.until(f"Localizar selector de {label}", lambda: selected_choice(dialog, label))[0]

    stage = monotonic()
    dialog = open_appointment_dialog(page, wait, guard)
    check_establishment(dialog)
    if selected_choice(dialog, "Especialidad") == (account.specialty,):
        dialog = reopen_dialog(page, dialog, guard, wait)
        check_establishment(dialog)
    timings.append(("Solicitud", monotonic() - stage))

    progress(f"Seleccionando Servicio: {account.service}…")
    guard()
    stage = monotonic()
    if chosen(dialog, "Servicio") != account.service:
        select_and_wait(dialog, "Servicio", account.service, wait, guard)
    else:
        progress(f"Servicio verificado: {account.service}.")
    timings.append(("Servicio", monotonic() - stage))
    guard()
    check_establishment(dialog)
    if chosen(dialog, "Especialidad") == account.specialty:
        raise ValueError("EDUS conservó la especialidad anterior. No se pudo iniciar una actualización nueva.")

    progress(f"Seleccionando Especialidad: {account.specialty}…")
    table = wait.until(
        "Localizar la tabla de disponibilidad",
        lambda: current if (current := appointment_table(dialog)).count() == 1
        and current.locator("tbody").count() == 1 else None,
    )
    marker = "edusTable_" + uuid4().hex
    observer = table.evaluate(
        """(table, key) => {
            const body = table.tBodies[0];
            if (!body) return false;
            const data = {changed: false, body};
            data.observer = new MutationObserver(() => data.changed = true);
            data.observer.observe(body, {childList: true});
            window[key] = data;
            return true;
        }""", marker,
    )
    if not observer:
        raise ValueError("La tabla no tiene un cuerpo verificable.")
    try:
        guard()
        requested_at = costa_rica_now()
        timings.append(("Especialidad", select_and_wait(
            dialog, "Especialidad", account.specialty, wait, guard, on_availability_commit,
        )))
        progress("Esperando actualización de disponibilidad…")
        stage = monotonic()
        wait.until(
            "Verificar actualización de disponibilidad",
            lambda: page.evaluate(
                "(key) => !!window[key] && (window[key].changed || !window[key].body.isConnected)", marker
            ),
            timeout=5,
        )
        timings.append(("Actualización", monotonic() - stage))
        guard()
        check_establishment(dialog)
        stage = monotonic()
        selected = tuple(current_choice(dialog, label) for label in ("Servicio", "Especialidad"))
        if selected != (account.service, account.specialty):
            raise ValueError("Los filtros cambiaron antes de leer el resultado.")
        table = appointment_table(dialog)
        if table.count() != 1:
            raise ValueError("La tabla de disponibilidad cambió.")
        offers = read_offers(table)
        rows = tuple(offer.cells for offer in offers)
        empty = table.get_by_text(NO_SLOTS, exact=True).filter(visible=True).count() == 1
        if bool(rows) == empty:
            raise ValueError("La tabla actualizada no contiene horarios ni un aviso válido de ausencia de cupos.")
        if preparing:
            guard()
            wait.until("Validar preparación para una consulta nueva", lambda: prepared_form(page, account))
            progress("Preparación verificada · establecimiento, filtros y formulario listos.")
        timings.extend((("Lectura", monotonic() - stage), ("Total", monotonic() - started)))
        return Result(
            account.alias, account.establishment, account.service, account.specialty,
            costa_rica_now(), requested_at, started_at, rows, tuple(timings), offers, started_tick=started,
        )
    finally:
        try:
            page.evaluate("(key) => { window[key]?.observer.disconnect(); delete window[key]; }", marker)
        except Error:
            # La pestaña puede haberse cerrado o haber navegado.
            pass



def read_offers(table):
    visible_rows = table.locator("tbody tr:visible").evaluate_all(
        r"""(rows, [action, icons]) => rows.flatMap(row => {
            const cells = Array.from(row.querySelectorAll(':scope > td'));
            if (cells.length < 6) return [];
            const fields = cells.slice(0, 5).map(cell => cell.innerText.trim().slice(0, 150));
            if (!/^\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}$/.test(fields[0])
                || !/^\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]\.?\s*m\.?)?$/i.test(fields[1])) return [];
            const controls = Array.from(cells[5].querySelectorAll(action))
                .filter(control => control.getClientRects().length && !control.disabled
                    && control.getAttribute('aria-disabled') !== 'true'
                    && ([control.textContent, control.title, control.getAttribute('aria-label')]
                        .some(label => ['ver cita', 'asignar cita'].includes(label?.trim().toLowerCase()))
                        || control.textContent.trim() === '+'
                        || control.matches(icons)
                        || Array.from(control.querySelectorAll(icons)).some(icon => icon.getClientRects().length)));
            return [[fields, controls.length === 1]];
        })""", [ACTION, DETAIL_ICONS],
    )
    return unique_offers(Offer(tuple(cells), has_details) for cells, has_details in visible_rows)
