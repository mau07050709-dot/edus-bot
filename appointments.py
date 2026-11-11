"""Lectura de la vista Solicitar Cita, sin seleccionar ni confirmar citas."""

import re
from time import monotonic

from playwright.sync_api import Error, TimeoutError
from navigation_actions import navigation_click


ADD_APPOINTMENT = "Agregar una cita"
NO_SLOTS = "No se encontraron cupos disponibles."
ESTABLISHMENT = re.compile(r"^\s*\d+\s*[-–—]\s*\S[^\r\n]*?\s*$")
HEADERS = ("Fecha", "Hora de Cita", "N° de Cita", "Ver cita")
CHOICES = ("Servicio", "Especialidad")


def appointment_dialog(page):
    title = page.get_by_text("Solicitar Cita", exact=True)
    return page.locator(':is([role="dialog"], .ui-dialog):visible').filter(has=title)


class NavigationWait:
    """Reobserva el DOM con un presupuesto compartido. Nunca repite un clic."""

    def __init__(self, page, progress=lambda _: None, cancelled=lambda: False):
        self.page = page
        self.progress = progress
        self.cancelled = cancelled
        self.remaining = 2

    def recover(self, stage, reason):
        if not self.remaining:
            raise ValueError(f"{stage}: {reason} tras agotar la recuperación local.")
        self.remaining -= 1
        self.progress(f"Recuperación iniciada · {stage}: {reason}.")

    def until(self, stage, predicate, timeout=3):
        recovery = False
        while True:
            deadline = monotonic() + timeout
            last = "el elemento aún no está listo"
            while monotonic() < deadline:
                if self.cancelled():
                    raise ValueError("Consulta detenida.")
                try:
                    result = predicate()
                    if result:
                        if recovery:
                            self.progress(f"Recuperación exitosa · {stage}.")
                        return result
                except Error as error:
                    if self.page.is_closed() or not self.page.context.browser.is_connected():
                        raise
                    last = type(error).__name__ + " durante la lectura del DOM"
                self.page.wait_for_timeout(75)
            self.recover(stage, last)
            recovery = True
            timeout = min(timeout, 1.5)


def add_appointment_control(page):
    label = page.get_by_text(ADD_APPOINTMENT, exact=True).filter(visible=True)
    control = label.locator('xpath=ancestor-or-self::*[self::button or self::a or @role="button"][1]')
    return control if control.count() else label


def open_appointment_dialog(page, wait=None, guard=lambda: None):
    wait = wait or NavigationWait(page)
    dialog = appointment_dialog(page)
    wait.progress("Buscando Agregar una cita o Solicitar Cita…")

    def opening_target():
        if dialog.count() == 1:
            return "dialog", dialog
        control = add_appointment_control(page)
        if not dialog.count() and control.count() == 1 and control.is_enabled(timeout=500):
            return "button", control
        return None

    kind, control = wait.until("Localizar Agregar una cita", opening_target)
    if kind == "button":
        wait.progress("Botón localizado · Abriendo Solicitar Cita…")
        guard()
        # La interfaz puede haber abierto el formulario mientras se validaba la sesión.
        if not dialog.count():
            try:
                navigation_click(control, timeout=2_500)
            except TimeoutError:
                wait.progress("El clic no se pudo verificar. Comprobando el estado del formulario.")
        wait.until("Apertura de Solicitar Cita", lambda: dialog if dialog.count() == 1 else None, timeout=4)
    wait.progress("Formulario Solicitar Cita abierto y verificado.")
    return dialog


def appointment_close(dialog):
    return dialog.locator(
        ':is(.ui-dialog-titlebar-close, button[aria-label="Cerrar"], button[aria-label="Close"]):visible'
    )


def appointment_table(dialog):
    tables = dialog.locator("table:visible")
    for header in HEADERS:
        tables = tables.filter(has=dialog.page.get_by_text(header, exact=True))
    return tables


def assigned_establishment(dialog):
    label = dialog.get_by_text("Establecimiento de Salud:", exact=True).filter(visible=True)
    establishment = dialog.get_by_text(ESTABLISHMENT).filter(visible=True)
    if label.count() == 1 and establishment.count() == 1:
        return " ".join(establishment.inner_text(timeout=800).split())
    return ""


def choices(dialog, label):
    placeholder = re.compile(rf"^\s*Seleccione\s+{label}\b", re.IGNORECASE)
    matching_option = dialog.page.locator("option").filter(has_text=placeholder)
    select = dialog.locator("select").filter(has=matching_option)
    return select if select.count() == 1 else None


def select_choice(dialog, label, text, before_select=lambda: None):
    select = choices(dialog, label)
    if select is None or not select.is_enabled(timeout=800):
        raise ValueError(f"No se pudo identificar el selector de {label}. Selecciónalo en Brave.")
    verified = select.locator("option:enabled").and_(
        dialog.page.get_by_text(text, exact=True)
    )
    if verified.count() != 1:
        raise ValueError(f"La opción de {label} ya no está disponible.")
    if select.is_visible():
        before_select()
        select.select_option(label=text, timeout=3_000)
        return
    combo = select.locator('xpath=ancestor::*[@role="combobox"][1]')
    if combo.count() != 1:
        raise ValueError(f"No se reconoció el control de {label}. Selecciónalo en Brave.")
    popup = combo.get_attribute("aria-controls", timeout=1_000)
    if not popup and (combo_id := combo.get_attribute("id", timeout=1_000)):
        popup = combo_id + "_panel"
    if not popup or not re.fullmatch(r"[\w:.-]+", popup):
        raise ValueError(f"No se reconoció el menú de {label}. Selecciónalo en Brave.")
    option = dialog.page.locator(f'[id="{popup}"]').get_by_text(text, exact=True).filter(visible=True)
    if option.count() != 1:
        trigger = combo.locator('.ui-selectonemenu-trigger:visible')
        navigation_click(trigger if trigger.count() == 1 else combo, timeout=3_000)
    option.wait_for(state="visible", timeout=3_000)
    before_select()
    navigation_click(option, timeout=3_000)


def read_availability(dialog):
    selected = []
    options = []
    for label in CHOICES:
        select = choices(dialog, label)
        if select is None:
            return "Selecciona Servicio y Especialidad en Brave. Selectores sin verificar.", (), ()
        values = select.locator("option:enabled").all_text_contents() if select.is_enabled(timeout=800) else []
        options.append(tuple(
            value.strip() for value in values
            if value.strip() and not value.strip().casefold().startswith("seleccione")
        ))
        current = select.locator("option:checked").all_text_contents()
        selected.append(current[0].strip() if current else "")
    options = tuple(options)
    selection = tuple(selected)
    if any(value not in items for value, items in zip(selection, options)):
        return "Elige Servicio y Especialidad para consultar.", options, selection
    table = appointment_table(dialog)
    if table.count() != 1:
        return "Estado de cupos desconocido · tabla modificada.", options, selection
    if table.get_by_text(NO_SLOTS, exact=True).filter(visible=True).count() == 1:
        return "No hay cupos para la selección actual.", options, selection
    rows = table.locator("tbody tr:visible").evaluate_all(
        r"""rows => rows.filter(row => {
            const cells = row.querySelectorAll(':scope > td');
            return cells.length >= 6 && /\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}/.test(cells[0].innerText)
                && /\b\d{1,2}:\d{2}\b/.test(cells[1].innerText);
        }).length"""
    )
    result = f"Horarios visibles en EDUS: {rows}." if rows else "Estado de cupos desconocido."
    return result, options, selection

