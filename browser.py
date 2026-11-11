"""Conexión a Brave y observación del formulario público de EDUS."""

import logging
import re
import subprocess
from base64 import b64decode
from dataclasses import dataclass, replace
from queue import Empty, SimpleQueue
from sqlite3 import Error as DatabaseError
from time import time
from time import monotonic
from uuid import uuid4
from urllib.parse import unquote, unquote_plus, urlsplit

from PySide6.QtCore import QThread, Signal
from playwright.sync_api import Error, TimeoutError, sync_playwright

from accounts import PORTAL_TYPE_LABELS
from appointments import (
    CHOICES, NavigationWait, appointment_dialog, appointment_table, assigned_establishment, open_appointment_dialog,
    read_availability, select_choice,
)
from brave import find_brave, resolve_endpoint
from booking import Appointment, BookingJournal, Outcome, select_offer, stage_booking
from booking_page import EdusBookingPage
from captcha import read_captcha
from query import close_appointment_dialog, current_choice, prepared_form, read_offers, run_query
from session_link import SessionLink, load_link
from navigation_actions import navigation_click
from settings import save_settings


PORTAL_URL = "https://edus.ccss.sa.cr/eduscitasweb/"
USER = 'input[id="formInicioSesion:usuario"]'
PASSWORD = 'input[type="password"][id="formInicioSesion:clave"]'
CAPTCHA = 'input[id="formInicioSesion:captchaDigitado"]'
CAPTCHA_IMAGE = 'form[id="formInicioSesion"] img[src*="captcha" i]'
CAPTCHA_REFRESH = 'form[id="formInicioSesion"] a:has(img[id="formInicioSesion:imagenRefrescar"])'
SUBMIT = 'button[id="formInicioSesion:ejecutarPaso1"]'
LOGOUT = 'a[id="formSIAC:btnMenuSalir"], button[id="formSIAC:btnMenuSalir"]'
TYPE_SELECT = 'select[id="formInicioSesion:tipIdentificacion_input"]'
TYPE_COMBO = '[id="formInicioSesion:tipIdentificacion"]'
TYPE_PANEL = '[id="formInicioSesion:tipIdentificacion_panel"]'
FORM_FIELDS = (USER, PASSWORD, CAPTCHA, SUBMIT)
ALERTS = '[id="formInicioSesion:growl_container"] .ui-growl-message, [role="alert"]'
WAITING = "CAPTCHA pendiente"
SOLVING_CAPTCHA = "Leyendo CAPTCHA"
BAD_CAPTCHA = "CAPTCHA incorrecto"
EXPIRED = "Sesión caducada"
BAD_CREDENTIALS = "Credenciales incorrectas"
SIGNED_OUT = "No autenticado"
UNKNOWN = "Estado desconocido"
AUTHENTICATED = "Autenticado"
ACCOUNT_PATH = "/CitasWebPF/faces/xhtml/comun/Inicio.xhtml"
CAPTCHA_RETRY_DELAY = 3
RECOVERY_ACTION_TIMEOUT = 2_000
RECOVERY_LOGIN_TIMEOUT = 4_000


@dataclass(frozen=True)
class CaptchaAttempt:
    code: str
    pixels: bytes
    source: str
    rejection: tuple


def is_captcha_rejection(text):
    text = text.casefold()
    return any(name in text for name in ("código de seguridad", "captcha")) and any(
        word in text for word in ("incorrect", "inválid", "no coincide", "erróneo", "no es válido")
    )


def captcha_rejection(page):
    notices = page.locator(f":is({ALERTS}):visible").evaluate_all(
        """nodes => {
            const state = window.edusCaptchaNotices ??= {ids: new WeakMap(), next: 0, prefix: crypto.randomUUID()};
            return nodes.map(node => {
                const content = node.firstElementChild || node.firstChild || node;
                if (!state.ids.has(content)) state.ids.set(content, ++state.next);
                return [state.prefix, state.ids.get(content), node.textContent];
            });
        }"""
    )
    return tuple(tuple(notice) for notice in notices if is_captcha_rejection(notice[2]))


def captcha_retry_reason(page, state, attempt, login_response_ok, *, settled=False):
    if state == BAD_CAPTCHA and set(captcha_rejection(page)) - set(attempt.rejection):
        return "rejected"
    # EDUS también puede volver al formulario con otro reto y sin un aviso de error visible.
    if login_response_ok and state in (WAITING, BAD_CAPTCHA):
        if page.get_by_text("Control de Sesiones", exact=True).filter(visible=True).count():
            return None
        image = page.locator(CAPTCHA_IMAGE).filter(visible=True)
        if image.count() == 1 and image.evaluate(
            "img => img.complete && img.naturalWidth ? img.currentSrc : ''", timeout=1_200,
        ) not in ("", attempt.source):
            return "rotated"
        if settled and page.locator(CAPTCHA).input_value(timeout=1_200) in ("", attempt.code):
            return "reset"
    return None


def is_portal(url):
    origin = urlsplit(url)
    return (
        origin.scheme == "https" and origin.netloc == "edus.ccss.sa.cr"
        and origin.path.startswith(("/eduscitasweb/", "/CitasWebPF/"))
    )


def has_login_form(page):
    return all(page.locator(selector).is_visible() for selector in FORM_FIELDS)


def is_authenticated(page):
    if not is_portal(page.url) or urlsplit(page.url).path != ACCOUNT_PATH:
        return False
    if not page.get_by_text("Listado de Citas", exact=True).filter(visible=True).count():
        return False
    return (
        page.get_by_text("Agregar una cita", exact=True).filter(visible=True).count() > 0
        or page.get_by_text("Estado Citas:", exact=True).filter(visible=True).count() > 0
        or appointment_dialog(page).count() == 1
    )


def refresh_cycle_page(page, state, verified, account, cancelled, progress=lambda _: None):
    def guard():
        if cancelled():
            raise ValueError("Se detuvo la recarga.")
        # Esta operación solo cierra una sesión. Una marca perdida tras recargar
        # no autoriza consultas, pero tampoco debe impedir volver al ingreso.
        if verified() not in ("", account) or not is_portal(page.url):
            raise ValueError("Cierra la sesión actual en EDUS para ingresar con la cuenta seleccionada.")

    def login_arrived():
        deadline = monotonic() + RECOVERY_LOGIN_TIMEOUT / 1000
        while monotonic() < deadline:
            guard()
            if has_login_form(page):
                return True
            # Sondeo por tiempo real: no depende de requestAnimationFrame.
            page.wait_for_timeout(100)
        return False

    def navigate_to_login():
        guard()
        progress("Recuperación de sesión · Navegando al ingreso oficial como respaldo.")
        response = page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=15_000)
        guard()
        if response is None or not response.ok or not login_arrived():
            raise ValueError("La navegación de respaldo no devolvió el formulario de ingreso de EDUS.")

    def activate_and_wait(control, name):
        guard()
        try:
            navigation_click(control, timeout=RECOVERY_ACTION_TIMEOUT)
        except (Error, ValueError) as error:
            progress(f"{name} · Activación sin confirmar ({type(error).__name__}).")
        try:
            arrived = login_arrived()
        except Error:
            arrived = False
        if not arrived:
            progress(f"{name} no abrió el ingreso; se descarta repetir el mismo control.")
            navigate_to_login()

    if cancelled():
        raise ValueError("Se detuvo la recarga.")
    if not is_portal(page.url):
        raise ValueError("La pestaña ya no muestra el portal oficial de EDUS.")
    if inspect_session(page, False) == EXPIRED:
        state = EXPIRED
    if state == AUTHENTICATED:
        guard()
        if not verified():
            progress("Marca de cuenta perdida · Cerrando la sesión sin reutilizar su identidad.")
        dialog = appointment_dialog(page)
        if dialog.count():
            progress("Cerrando Solicitar Cita antes de salir de EDUS.")
            try:
                close_appointment_dialog(dialog, guard, NavigationWait(page, progress, cancelled))
            except (ValueError, Error):
                guard()
                progress("Formulario atascado · Recargando la página antes de recuperar la sesión.")
                page.reload(wait_until="domcontentloaded", timeout=15_000)
                guard()
                if has_login_form(page):
                    return
                if inspect_session(page, False) == EXPIRED:
                    navigate_to_login()
                    return
        logout = page.locator(LOGOUT).filter(visible=True)
        if logout.count() != 1 or not logout.is_enabled(timeout=1_200):
            progress("Control de salida no disponible · Recuperando el documento de ingreso.")
            navigate_to_login()
            return
        guard()
        progress("Cerrando la sesión de EDUS mediante su control oficial.")
        activate_and_wait(logout, "Cierre de sesión")
    elif state in (EXPIRED, UNKNOWN) and not has_login_form(page):
        guard()
        return_name = re.compile(r"^Volver a acceder a la aplicaci[oó]n$", re.I)
        reentry = page.get_by_role('button', name=return_name).or_(
            page.get_by_role('link', name=return_name)
        ).filter(visible=True)
        if state == EXPIRED and reentry.count() == 1 and reentry.is_enabled(timeout=800):
            progress("Sesión caducada · Pulsando Volver a acceder a la aplicación.")
            activate_and_wait(reentry, "Retorno desde sesión caducada")
        else:
            navigate_to_login()
        if not is_portal(page.url) or not has_login_form(page):
            raise ValueError("EDUS no mostró el formulario de ingreso después de caducar la sesión.")
        return
    elif state not in (SIGNED_OUT, EXPIRED, WAITING, BAD_CAPTCHA) or not has_login_form(page):
        raise ValueError("No se pudo comprobar el estado de la sesión antes de recargar.")
    if cancelled():
        raise ValueError("Se detuvo la recarga.")
    if not is_portal(page.url) or not has_login_form(page):
        raise ValueError("EDUS no mostró el formulario de ingreso tras cerrar la sesión.")
    progress("Recargando el formulario de ingreso de EDUS.")
    response = page.reload(wait_until="domcontentloaded", timeout=15_000)
    if not response or not response.ok or not is_portal(page.url):
        raise ValueError("EDUS no devolvió una página válida al recargar.")
    page.locator(CAPTCHA).wait_for(state="visible", timeout=5_000)
    if not has_login_form(page):
        raise ValueError("El formulario de ingreso de EDUS cambió después de recargar.")


def transient_document_error(error):
    message = str(error).casefold()
    return any(signature in message for signature in (
        "execution context was destroyed", "cannot find context", "context is not available",
        "frame was detached", "because of a navigation", "inspected target navigated",
    ))


def mark_authenticated_document(page, marker, submission_key, attempt, observed_submit=False, marker_value=True):
    """Consume una señal de envío desde Brave, solo en el documento privado."""
    try:
        return page.evaluate(
            r"""([path, marker, key, attempt, observedSubmit, markerValue]) => {
                const visible = node => node && node.getClientRects().length &&
                    getComputedStyle(node).visibility !== 'hidden';
                if (location.origin !== 'https://edus.ccss.sa.cr' || location.pathname !== path
                    || document.readyState === 'loading'
                    || visible(document.getElementById('formInicioSesion:usuario'))) return 'pending';
                const heading = [...document.querySelectorAll('a, h1, h2, h3, span, div, label')]
                    .some(node => visible(node) &&
                        node.textContent.trim().split(/\s+/).join(' ') === 'Listado de Citas');
                if (!heading) return 'pending';
                const [token, timestamp] = (sessionStorage.getItem(key) || '').split(':');
                const age = Date.now() - Number(timestamp);
                if (!observedSubmit && (token !== attempt || !Number.isFinite(age) || age < 0 || age >= 60000)) {
                    return 'unverified';
                }
                sessionStorage.removeItem(key);
                window[marker] = markerValue;
                return 'verified';
            }""", [ACCOUNT_PATH, marker, submission_key, attempt, observed_submit, marker_value],
        )
    except Error as error:
        if transient_document_error(error):
            return "pending"
        raise


def inspect_session(page, awaiting_code):
    if not is_portal(page.url):
        return UNKNOWN
    alerts = " ".join(page.locator(f":is({ALERTS}):visible").all_text_contents()).casefold()
    expired_page = page.get_by_text(re.compile(
        r"^(?:(?:su|la)\s+)?sesi[oó]n\s+(?:(?:ha|ya)\s+)?(?:expirad[ao]|expir[oó]|caducad[ao]|vencid[ao]|venci[oó])(?:[.!\s]|$)",
        re.I,
    )).filter(visible=True).count() > 0
    if not (awaiting_code and has_login_form(page)) and (expired_page or any(message in alerts for message in (
        "sesión caducada", "sesión expirada", "sesión ha expirado", "sesión ha caducado",
        "sesión vencida", "sesión ha vencido", "sesión expiró",
    ))):
        return EXPIRED
    if any(message in alerts for message in (
        "credenciales incorrectas", "contraseña incorrecta", "clave incorrecta",
        "usuario o contraseña incorrectos", "usuario o clave incorrectos",
    )):
        return BAD_CREDENTIALS
    busy = page.locator('[id^="formInicioSesion:"] .ui-dialog-title').filter(
        has_text="Espere por favor"
    )
    if busy.first.is_visible():
        return "Autenticando"
    if is_authenticated(page) and not has_login_form(page):
        return AUTHENTICATED
    if not has_login_form(page):
        return UNKNOWN
    if is_captcha_rejection(alerts):
        return BAD_CAPTCHA
    if awaiting_code:
        return WAITING
    return SIGNED_OUT


def prepare_identity_type(page, kind):
    if not is_portal(page.url) or not has_login_form(page):
        raise ValueError("El formulario de EDUS no coincide con el verificado.")
    select = page.locator(TYPE_SELECT)
    if select.count() != 1:
        raise ValueError("No se reconoció el selector de identificación.")
    if select.input_value(timeout=2_000) == kind:
        return
    # Si el usuario ya escribió algo, primero debe revisar los campos en Brave.
    if page.locator(USER).input_value(timeout=2_000):
        raise ValueError("El formulario ya tiene otra selección. Revísala en Brave.")
    page.locator(TYPE_COMBO).click(timeout=2_000)
    option = page.locator(TYPE_PANEL).get_by_text(PORTAL_TYPE_LABELS[kind], exact=True)
    option.filter(visible=True).click(timeout=2_000)
    if select.input_value(timeout=2_000) != kind:
        raise ValueError("No se pudo comprobar el tipo de identificación.")


def arm_login_proof(page, identification, kind, submission_key, attempt):
    return page.evaluate(
        """([user, kindSelect, submit, identification, kind, key, attempt]) => {
            if (location.origin !== 'https://edus.ccss.sa.cr') return false;
            if (![user, kindSelect, submit].every(selector => document.querySelector(selector))) return false;
            sessionStorage.removeItem(key);
            window[key]?.abort();
            const controller = new AbortController();
            window[key] = controller;
            const record = event => {
                if (!event.isTrusted) return;
                const target = event.type === 'submit' ? event.submitter : event.target;
                if (!target?.closest(submit)) return;
                const matches = document.querySelector(user)?.value === identification
                    && document.querySelector(kindSelect)?.value === kind;
                if (matches) sessionStorage.setItem(key, attempt + ':' + Date.now());
                else sessionStorage.removeItem(key);
            };
            const options = {capture: true, signal: controller.signal};
            document.addEventListener('click', record, options);
            document.addEventListener('submit', record, options);
            return true;
        }""", [USER, TYPE_SELECT, SUBMIT, identification, kind, submission_key, attempt],
    )


def submitted_login_identity(request, frame):
    """Observa el envío normal de PrimeFaces y extrae únicamente el tipo y el ID."""
    if request.method != "POST" or request.resource_type not in ("xhr", "fetch", "document"):
        return None
    if not is_portal(request.url) or request.frame != frame:
        return None
    fields = {}
    wanted = {"javax.faces.source", "jakarta.faces.source", "formInicioSesion:ejecutarPaso1",
              "formInicioSesion:usuario", "formInicioSesion:tipIdentificacion_input"}
    for pair in (request.post_data or "").split("&"):
        raw_key, _, raw_value = pair.partition("=")
        key = unquote_plus(raw_key)
        if key in wanted:
            if key in fields:
                return None
            fields[key] = unquote_plus(raw_value)
    source = fields.get("javax.faces.source", fields.get("jakarta.faces.source", ""))
    if source != "formInicioSesion:ejecutarPaso1" and "formInicioSesion:ejecutarPaso1" not in fields:
        return None
    return (
        fields.get("formInicioSesion:usuario", "").strip(),
        fields.get("formInicioSesion:tipIdentificacion_input", "").strip(),
    )


def fill_login(page, identification, password, *, retry=False):
    if not is_portal(page.url) or not has_login_form(page):
        raise ValueError("El formulario de EDUS no coincide con el verificado.")
    # Comprobar origen y campos en la misma ejecución que el autocompletado.
    result = page.evaluate(
        r"""([userSelector, passwordSelector, captchaSelector, identification, password, retry]) => {
            if (location.origin !== 'https://edus.ccss.sa.cr' ||
                !/^\/(eduscitasweb|CitasWebPF)\//.test(location.pathname)) return 'changed';
            const fields = [userSelector, passwordSelector, captchaSelector]
                .map(selector => document.querySelector(selector));
            if (!fields.every(field => field && !field.disabled && !field.readOnly
                && field.getClientRects().length)) return 'changed';
            if (retry ? fields[0].value !== identification : fields.some(field => field.value)) return 'occupied';
            const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            for (const [field, value] of [[fields[0], identification], [fields[1], password]]) {
                if (field.value) continue;
                setValue.call(field, value);
                field.dispatchEvent(new Event('input', { bubbles: true }));
                field.dispatchEvent(new Event('change', { bubbles: true }));
            }
            return 'filled';
        }""",
        [USER, PASSWORD, CAPTCHA, identification, password, retry],
    )
    if result == "changed":
        raise ValueError("El formulario cambió antes de completar los campos.")
    return result


def captcha_snapshot(image):
    # Captura el recurso cargado antes del escalado CSS, sin hacer otra solicitud a EDUS.
    raw = image.evaluate(
        """img => {
            if (!img.complete || !img.naturalWidth || img.naturalWidth > 1080 || img.naturalHeight > 300) return '';
            const canvas = document.createElement('canvas');
            canvas.width = img.naturalWidth;
            canvas.height = img.naturalHeight;
            canvas.getContext('2d').drawImage(img, 0, 0);
            try { return canvas.toDataURL('image/png'); } catch { return ''; }
        }""", timeout=2_000,
    )
    if raw.startswith("data:image/png;base64,"):
        return b64decode(raw.partition(",")[2])
    return image.screenshot(timeout=2_500, scale="css")


def submit_captcha(page, identification, password, kind, cancelled, progress=lambda _: None, *,
                   rejected=None, refresh=False):
    if cancelled() or not is_portal(page.url) or not has_login_form(page):
        return False
    old_code = rejected.code if rejected else ""
    if page.locator(CAPTCHA).input_value(timeout=1_200) not in ("", old_code):
        progress("CAPTCHA · Se conservó el código que ya estaba escrito.")
        return False
    image = page.locator(CAPTCHA_IMAGE).filter(visible=True)
    if image.count() != 1:
        progress("CAPTCHA · No se identificó una única imagen visible.")
        return False
    image.wait_for(state="visible", timeout=2_000)
    page.wait_for_function(
        """selector => {
            const image = document.querySelector(selector);
            return image?.complete && image.naturalWidth > 0;
        }""", arg=CAPTCHA_IMAGE, timeout=2_500,
    )
    def image_source():
        source = image.evaluate("img => img.complete && img.naturalWidth ? img.currentSrc : ''", timeout=1_200)
        url = urlsplit(source)
        if (url.scheme, url.netloc, unquote(url.path).split(";", 1)[0]) == (
            "https", "edus.ccss.sa.cr", "/CitasWebPF/captcha",
        ):
            return source
        progress("CAPTCHA · El origen de la imagen no coincide con el formulario verificado.")
        return ""

    source = image_source()
    if not source:
        return False
    progress("CAPTCHA · Capturando la imagen y leyendo los caracteres.")
    png = captcha_snapshot(image)
    code = None if rejected and png == rejected.pixels else read_captcha(png)
    while not code:
        if cancelled() or page.locator(CAPTCHA).input_value(timeout=1_200) not in ("", old_code):
            return False
        control = page.locator(CAPTCHA_REFRESH).filter(visible=True)
        if (rejected and png == rejected.pixels and not refresh) or control.count() != 1 or not control.is_enabled(timeout=1_200):
            progress("CAPTCHA · No se pudo renovar la imagen. Complétalo en Brave.")
            return False
        if inspect_session(page, True) not in (WAITING, BAD_CAPTCHA):
            return False
        progress("CAPTCHA · Solicitando otra imagen a EDUS.")
        deadline = monotonic() + CAPTCHA_RETRY_DELAY
        while monotonic() < deadline:
            if cancelled() or page.locator(CAPTCHA).input_value(timeout=1_200) not in ("", old_code):
                return False
            page.wait_for_timeout(150)
        previous_png = png
        control.click(timeout=2_500)
        try:
            page.wait_for_function(
                """([selector, source]) => {
                    const image = document.querySelector(selector);
                    return image?.complete && image.naturalWidth > 0 && image.currentSrc !== source;
                }""", arg=[CAPTCHA_IMAGE, source], timeout=3_000,
            )
        except TimeoutError:
            pass
        source = image_source()
        if not source:
            return False
        png = captcha_snapshot(image)
        if png == previous_png:
            progress("CAPTCHA · EDUS todavía muestra la misma imagen.")
            return False
        code = read_captcha(png)
    if cancelled():
        return False
    if captcha_snapshot(image) != png:
        progress("CAPTCHA · La imagen cambió durante la lectura. Revisa Brave.")
        return False
    rejection = captcha_rejection(page)
    ready = page.evaluate(
        r"""([selectors, identity, password, kind, source, code, oldCode]) => {
            if (location.origin !== 'https://edus.ccss.sa.cr' ||
                !/^\/(eduscitasweb|CitasWebPF)\//.test(location.pathname)) return false;
            const nodes = selectors.map(selector => [...document.querySelectorAll(selector)]);
            if (nodes.some(matches => matches.length !== 1)) return false;
            const [user, secret, type, field, image, submit] = nodes.map(matches => matches[0]);
            if (user.value !== identity || secret.value !== password || type.value !== kind ||
                image.currentSrc !== source || (field.value && field.value !== oldCode) || field.disabled || field.readOnly ||
                submit.disabled || !submit.getClientRects().length || !field.getClientRects().length) return false;
            field.value = code;
            field.dispatchEvent(new Event('input', {bubbles: true}));
            field.dispatchEvent(new Event('change', {bubbles: true}));
            return field.value === code;
        }""",
        [[USER, PASSWORD, TYPE_SELECT, CAPTCHA, CAPTCHA_IMAGE, SUBMIT], identification, password, kind, source, code, old_code],
    )
    if not ready:
        progress("CAPTCHA · El formulario o la cuenta cambiaron antes del envío.")
        return False
    if cancelled():
        return False
    page.locator(SUBMIT).click(timeout=2_500)
    return CaptchaAttempt(code, png, source, rejection)


class BrowserThread(QThread):
    status = Signal(str)
    authentication = Signal(str)
    activity = Signal(int, str)
    verified = Signal(str)
    availability = Signal(object)
    query_progress = Signal(str)
    query_finished = Signal(str, object)
    cycle_refreshed = Signal(str)
    preparation_status = Signal(bool)
    slot_found = Signal(str, str, str, str)

    def __init__(self, settings, action, credentials=None, parent=None, booking_path=None, *, session_path=None, load_credentials=None):
        super().__init__(parent)
        self.settings = settings
        self.action = action
        self.requests = SimpleQueue()
        self.journal = BookingJournal(booking_path) if booking_path is not None else None
        self.wake_at = 0
        self.cdp_endpoint = ""
        self.session_path = session_path
        self.load_credentials = load_credentials
        self.document_marker = "edusVerified_" + uuid4().hex
        self.document_value = True
        self.submission_key = "edusSubmitted_" + uuid4().hex
        self.session_proof = ("", "")
        self.prepared_account = None
        self.recover_query = None
        if credentials is not None:
            self.request_login(credentials)

    def request_login(self, credentials):
        self.requests.put(("login", credentials))

    def request_appointment(self, account, establishment):
        self.requests.put(("appointment", (account, establishment)))

    def confirm_establishment(self, account, establishment):
        self.requests.put(("establishment", (account, establishment)))

    def request_choice(self, account, index, value):
        self.requests.put(("choice", (account, index, value)))

    def request_query(self, account, booking_after=None, *, preparing=False):
        self.requests.put(("query", (account, booking_after, preparing)))

    def request_cycle_refresh(self, account):
        self.requests.put(("refresh_cycle", (account,)))

    def request_focus(self):
        self.requests.put(("focus", ()))

    def run(self):
        try:
            self.connect_brave()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            self.report_error(str(error))
        except TimeoutError:
            self.report_error("Se agotó el tiempo de conexión con Brave o con EDUS.")
        except Error:
            self.report_error("Se interrumpió la conexión con Brave o no se pudo cargar EDUS.")
        except Exception:
            self.report_error("Ocurrió un error al conectar con Brave.")
        finally:
            while not self.requests.empty():
                self.requests.get_nowait()

    def report_error(self, message):
        if not self.isInterruptionRequested():
            self.status.emit("Brave desconectado")
            self.authentication.emit("Error de conexión")
            self.activity.emit(logging.ERROR, message)

    def connect_brave(self):
        brave = find_brave(self.settings.brave_path)
        if brave is None:
            raise RuntimeError("No se encontró Brave. Selecciona brave.exe en Configuración.")
        try:
            self.cdp_endpoint = resolve_endpoint(brave)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            self.status.emit("Requiere configuración")
            self.activity.emit(logging.WARNING, str(error))
            if self.action == "open" and not self.isInterruptionRequested():
                subprocess.Popen([str(brave), PORTAL_URL], close_fds=True)
                self.activity.emit(logging.INFO, "EDUS se abrió en Brave para acceso manual.")
            return
        for attempt in range(3):
            if self.isInterruptionRequested():
                return
            if attempt:
                self.status.emit("Reconectando Brave…")
                self.activity.emit(logging.WARNING, f"Recuperación CDP · intento {attempt} de 2.")
                self.msleep(350 * attempt)
                if self.isInterruptionRequested():
                    return
            try:
                if attempt:
                    try:
                        self.cdp_endpoint = resolve_endpoint(brave)
                    except RuntimeError as error:
                        raise ConnectionLost from error
                self.observe_portal()
                return
            except (ConnectionLost, Error, OSError, subprocess.SubprocessError) as error:
                category = (
                    "contexto del documento en transición" if isinstance(error, Error)
                    and transient_document_error(error) else type(error).__name__
                )
                self.activity.emit(logging.WARNING, f"Brave · conexión o pestaña interrumpida ({category}).")
                if attempt == 2:
                    self.report_error(
                        "No se pudo conectar con Brave personal. Revisa si Brave mostró el aviso Permitir."
                    )

    def observe_portal(self):
        with sync_playwright() as playwright:
            needs_permission = self.cdp_endpoint.startswith("ws:")
            if needs_permission:
                self.status.emit("Esperando autorización en Brave…")
                self.activity.emit(logging.INFO, "Si Brave pide permitir la conexión de depuración, pulsa Permitir.")
            browser = playwright.chromium.connect_over_cdp(
                self.cdp_endpoint, timeout=30_000 if needs_permission else 5_000,
                is_local=True, no_defaults=True,
            )
            if self.isInterruptionRequested():
                return
            while not self.isInterruptionRequested() and browser.is_connected():
                try:
                    page = self.portal_page(browser)
                    if self.action != "probe" and not self.isInterruptionRequested():
                        page.bring_to_front()
                    self.status.emit("Brave conectado")
                    self.watch(page, browser)
                except (Error, OSError, RuntimeError, ValueError) as error:
                    if not browser.is_connected():
                        raise ConnectionLost from error
                    if self.isInterruptionRequested():
                        break
                    self.activity.emit(logging.WARNING,
                        f"Recuperación de página ({type(error).__name__}) · Se conserva la conexión autorizada.")
                    self.authentication.emit(UNKNOWN)
                    self.status.emit("Brave conectado")
                    # Espera interrumpible, sin crear otra sesión de depuración.
                    for _ in range(4):
                        if self.isInterruptionRequested():
                            break
                        self.msleep(250)
            if not self.isInterruptionRequested() and not browser.is_connected():
                raise ConnectionLost()
            # Al salir, Playwright suelta su conexión. Brave y sus pestañas quedan abiertos.

    def portal_page(self, browser):
        tabs = [page for context in browser.contexts for page in context.pages if is_portal(page.url)]
        restored = [tab for tab in tabs if self.has_proof(tab)]
        if not restored and self.restore_session_link():
            restored = [tab for tab in tabs if self.has_proof(tab)]
        if len(restored) != 1:
            self.session_proof = ("", "")
        if len(restored) == 1:
            tabs = restored
        if len(tabs) > 1:
            signed_in = [tab for tab in tabs if inspect_session(tab, False) == AUTHENTICATED]
            if len(signed_in) != 1:
                raise RuntimeError("Hay varias pestañas de EDUS. Deja una sola para conectarla.")
            tabs = signed_in
        if tabs:
            return tabs[0]
        if len(browser.contexts) != 1:
            raise RuntimeError("Abre EDUS en el perfil de Brave que quieres utilizar.")
        page = browser.contexts[0].new_page()
        response = page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=15_000)
        if response is None or not response.ok or not is_portal(page.url):
            raise RuntimeError("EDUS no devolvió una página válida del portal oficial.")
        return page

    def restore_session_link(self):
        link = load_link(self.session_path)
        if link is None or self.load_credentials is None:
            return False
        account = next((entry for entry in self.settings.accounts if entry["key"] == link.account), None)
        if account is None:
            return False
        try:
            credentials = self.load_credentials(link.account)
        except Exception:
            return False
        if not credentials:
            return False
        identification = credentials[0].strip()
        self.document_marker = link.marker
        self.document_value = link.proof(account["kind"], identification)
        self.session_proof = (link.account, identification)
        return True

    def forget_session_link(self):
        if self.session_path:
            try:
                self.session_path.unlink(missing_ok=True)
            except OSError:
                self.activity.emit(logging.WARNING, "No se pudo retirar la verificación local anterior.")

    def has_proof(self, page):
        if not self.session_proof[0] or not is_portal(page.url):
            return False
        try:
            return inspect_session(page, False) == AUTHENTICATED and page.evaluate(
                "([key, value]) => window[key] === value", [self.document_marker, self.document_value],
            )
        except Error:
            return False

    def watch(self, page, browser):
        previous = None
        lost_session = False
        verified, verified_id = self.session_proof if self.has_proof(page) else ("", "")
        self.session_proof = (verified, verified_id)
        self.verified.emit(verified)
        if verified:
            self.activity.emit(logging.INFO, "Sesión verificada reutilizada en la misma pestaña de Brave.")
        check_ready_at = 0
        last_ready = None
        checking_login_document = False
        armed_id = ""
        armed_kind = ""
        submitted = None
        login_request = None
        login_response_ok = False
        armed_account = ""
        attempt = ""
        automatic_attempt = None
        automatic_password = None
        automatic_submit_at = None
        appointment_account = ""
        expected_establishment = ""
        pending_requests = set()
        last_response = 0
        response_error = False
        previous_availability = None
        previous_selection = ()
        selection_changed = 0
        def navigation(frame):
            nonlocal verified, verified_id, appointment_account, lost_session
            if frame == page.main_frame and verified:
                lost_session = True
                self.forget_session_link()
                verified = verified_id = ""
                self.session_proof = ("", "")
                appointment_account = ""
                self.verified.emit("")
                self.availability.emit(None)

        def requested(request):
            nonlocal submitted, login_request, login_response_ok
            if armed_account:
                try:
                    identity = submitted_login_identity(request, page.main_frame)
                except Error:
                    identity = None
                if identity is not None:
                    submitted = (monotonic(), identity == (armed_id, armed_kind))
                    login_request, login_response_ok = request, False
            if request.resource_type in ("xhr", "fetch"):
                pending_requests.add(request)

        def completed(request):
            nonlocal last_response, response_error, login_response_ok
            pending = request in pending_requests
            if not pending and request is not login_request:
                return
            try:
                response = request.response()
                failed = bool(request.failure) or response is None or response.status >= 400
            except Error:
                failed, response = True, None
            if request is login_request:
                login_response_ok = bool(not failed and response.ok and submitted and submitted[1])
            if pending:
                pending_requests.discard(request)
                response_error = failed
                last_response = monotonic()

        page.on("framenavigated", navigation)
        page.on("request", requested)
        page.on("requestfinished", completed)
        page.on("requestfailed", completed)
        try:
            while not self.isInterruptionRequested() and browser.is_connected() and not page.is_closed():
                try:
                    state = inspect_session(page, bool(armed_account))
                    if state == AUTHENTICATED and armed_account:
                        recent = submitted if submitted and monotonic() - submitted[0] < 60 else None
                        if recent and not recent[1]:
                            proof = "unverified"
                        else:
                            link = SessionLink.new(armed_account)
                            marker_value = link.proof(armed_kind, armed_id)
                            proof = mark_authenticated_document(
                                page, link.marker, self.submission_key, attempt, bool(recent), marker_value,
                            )
                        if proof == "pending":
                            if not checking_login_document:
                                self.activity.emit(logging.INFO, "EDUS está cambiando de página después del ingreso. Esperando el documento privado.")
                                checking_login_document = True
                            self.msleep(80)
                            continue
                        checking_login_document = False
                        if proof == "verified":
                            verified, verified_id = armed_account, armed_id
                            self.session_proof = (verified, verified_id)
                            self.document_marker, self.document_value = link.marker, marker_value
                            lost_session = False
                            if self.session_path and verified != "temporary":
                                try:
                                    save_settings(self.session_path, link)
                                except OSError:
                                    self.activity.emit(logging.WARNING, "No se pudo guardar la verificación para el próximo inicio.")
                            self.verified.emit(verified)
                            self.activity.emit(logging.INFO, "Ingreso verificado en el documento privado de EDUS.")
                        else:
                            self.activity.emit(logging.WARNING, "El ingreso no se pudo vincular con la cuenta seleccionada. Revisa la identificación en Brave.")
                except Error as error:
                    if not browser.is_connected() or page.is_closed():
                        break
                    if not transient_document_error(error):
                        raise
                    if not checking_login_document:
                        self.activity.emit(logging.INFO, "EDUS está cambiando de documento. Revalidando la sesión.")
                        checking_login_document = True
                    self.msleep(80)
                    continue
                if state in (AUTHENTICATED, EXPIRED):
                    armed_account, attempt = "", ""
                    armed_id = armed_kind = ""
                    submitted = None
                    checking_login_document = False
                if state in (AUTHENTICATED, EXPIRED, BAD_CREDENTIALS):
                    automatic_attempt = automatic_password = automatic_submit_at = None
                if automatic_attempt and state in (WAITING, BAD_CAPTCHA):
                    elapsed = monotonic() - automatic_submit_at
                    try:
                        reason = (
                            captcha_retry_reason(page, state, automatic_attempt, login_response_ok, settled=elapsed >= 8)
                            if elapsed >= CAPTCHA_RETRY_DELAY else None
                        )
                        if reason:
                            rejected, automatic_attempt = automatic_attempt, None
                            login_response_ok = False
                            self.authentication.emit(SOLVING_CAPTCHA)
                            self.activity.emit(logging.INFO, "CAPTCHA · EDUS pidió otro código. Volviendo a leer la imagen.")
                            previous = None
                            if fill_login(page, armed_id, automatic_password, retry=True) != "occupied":
                                automatic_attempt = submit_captcha(
                                    page, armed_id, automatic_password, armed_kind, self.isInterruptionRequested,
                                    lambda message: self.activity.emit(logging.INFO, message),
                                    rejected=rejected, refresh=reason != "rotated",
                                )
                            if automatic_attempt:
                                automatic_submit_at = monotonic()
                                self.activity.emit(logging.INFO, "Código reconocido · Nuevo ingreso enviado a EDUS.")
                                continue
                            automatic_password = automatic_submit_at = None
                        elif elapsed < 8:
                            state = "Autenticando"
                    except (Error, ValueError):
                        automatic_attempt = automatic_password = automatic_submit_at = None
                        self.activity.emit(logging.WARNING, "No se pudo renovar el CAPTCHA. Revisa Brave.")
                if verified and state not in (AUTHENTICATED, UNKNOWN, "Autenticando"):
                    lost_session = True
                    self.forget_session_link()
                    verified = verified_id = ""
                    self.session_proof = ("", "")
                    appointment_account = ""
                    self.verified.emit("")
                    self.availability.emit(None)
                if state == SIGNED_OUT and lost_session:
                    self.activity.emit(logging.INFO, "La sesión terminó y EDUS volvió a pedir ingreso.")
                    lost_session = False
                if state != previous:
                    if state in (BAD_CAPTCHA, BAD_CREDENTIALS):
                        submitted = None
                    if state in (WAITING, BAD_CAPTCHA) and armed_account:
                        if state == BAD_CAPTCHA and self.load_credentials and armed_account != "temporary":
                            try:
                                credentials = self.load_credentials(armed_account)
                                if credentials and credentials[0].strip() == armed_id:
                                    fill_login(page, *credentials, retry=True)
                            except Exception:
                                self.activity.emit(logging.WARNING, "Revisa los campos de ingreso antes de reintentar el CAPTCHA.")
                            finally:
                                credentials = None
                        try:
                            page.bring_to_front()
                            page.locator(CAPTCHA).focus(timeout=800)
                        except Error:
                            pass
                    self.authentication.emit(state)
                    self.activity.emit(logging.INFO, f"Sesión EDUS · {state}")
                    previous = state
                if self.prepared_account is not None and monotonic() >= check_ready_at:
                    check_ready_at = monotonic() + 1
                    try:
                        ready = (
                            state == AUTHENTICATED and verified == self.prepared_account.key
                            and prepared_form(page, self.prepared_account)
                        )
                    except Error:
                        ready = False
                    if ready != last_ready:
                        self.preparation_status.emit(ready)
                        last_ready = ready
                if self.recover_query is not None:
                    action, args = "query", self.recover_query
                    self.recover_query = None
                else:
                    try:
                        action, args = self.requests.get_nowait()
                    except Empty:
                        action, args = "", ()
                try:
                    if action == "focus" and is_portal(page.url):
                        page.bring_to_front()
                    elif action == "login":
                        if state == AUTHENTICATED:
                            raise ValueError("Cierra la sesión actual en EDUS antes de iniciar con otra cuenta.")
                        account, kind, identification, password = args
                        page.bring_to_front()
                        if state == EXPIRED and not has_login_form(page):
                            page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=10_000)
                            page.locator(CAPTCHA).wait_for(state="visible", timeout=5_000)
                        armed_account, attempt = "", uuid4().hex
                        submitted = None
                        login_request, login_response_ok = None, False
                        automatic_attempt = automatic_password = automatic_submit_at = None
                        prepare_identity_type(page, kind)
                        if not arm_login_proof(page, identification, kind, self.submission_key, attempt):
                            raise ValueError("El formulario cambió antes de preparar el acceso.")
                        filled = fill_login(page, identification, password)
                        armed_account = account
                        armed_id, armed_kind = identification.strip(), kind
                        self.authentication.emit(SOLVING_CAPTCHA)
                        previous = None
                        try:
                            automatic_attempt = submit_captcha(
                                page, identification, password, kind, self.isInterruptionRequested,
                                lambda message: self.activity.emit(logging.INFO, message),
                            )
                            if automatic_attempt:
                                automatic_password = password
                                automatic_submit_at = monotonic()
                                self.activity.emit(logging.INFO, "Código reconocido · Ingreso enviado a EDUS.")
                            elif filled == "occupied":
                                self.activity.emit(logging.WARNING, "Revisa los datos ya presentes en Brave.")
                        except Error:
                            self.activity.emit(logging.WARNING, "No se pudo verificar el ingreso automático. Revisa Brave.")
                    elif action == "refresh_cycle":
                        try:
                            refresh_cycle_page(
                                page, state, lambda: verified, args[0], self.isInterruptionRequested,
                                lambda message: self.activity.emit(logging.INFO, message),
                            )
                        except ValueError as error:
                            self.cycle_refreshed.emit(str(error))
                        except Error:
                            self.cycle_refreshed.emit("No se pudo verificar el cierre de sesión o la recarga en Brave.")
                        else:
                            armed_account, armed_id, armed_kind, attempt = "", "", "", ""
                            submitted, login_request, login_response_ok = None, None, False
                            automatic_attempt = automatic_password = automatic_submit_at = None
                            appointment_account = ""
                            previous = None
                            self.activity.emit(logging.INFO, "Página de EDUS recargada. Preparando el ingreso.")
                            self.cycle_refreshed.emit("")
                    elif action == "query":
                        account, booking_after, preparing = args
                        appointment_account = ""
                        self.prepared_account = None
                        last_ready = None
                        if verified != account.key and state in (UNKNOWN, WAITING, BAD_CAPTCHA, SIGNED_OUT, EXPIRED):
                            self.authentication.emit(state)
                            self.query_finished.emit("session", "La cuenta necesita autenticación en Brave.")
                            continue
                        try:
                            outcome = self.query(page, account, lambda: verified, verified_id, booking_after, preparing)
                        except SafeConnectionLost:
                            self.recover_query = args
                            break
                        if outcome[0] == "session":
                            try:
                                self.authentication.emit(inspect_session(page, bool(armed_account)))
                            except Error as error:
                                if not transient_document_error(error):
                                    raise
                        if outcome[0] == "ok" and preparing:
                            self.prepared_account = account
                            self.preparation_status.emit(True)
                        if outcome[0] == "establishment":
                            appointment_account = account.key
                            expected_establishment = account.establishment
                            observed = outcome[1]
                            self.availability.emit((account.key, observed, "Confirma el establecimiento mostrado por EDUS.", (), ()))
                            previous_availability = None
                        self.query_finished.emit(*outcome)
                    elif action == "appointment":
                        account, expected_establishment = args
                        if account != verified or state != AUTHENTICATED:
                            raise ValueError("Comprueba la sesión de la cuenta seleccionada antes de consultar.")
                        page.bring_to_front()
                        last_response, response_error = 0, False
                        open_appointment_dialog(page)
                        appointment_account = account
                        previous_selection = ()
                        self.activity.emit(logging.INFO, "Vista Solicitar Cita abierta.")
                    elif action == "establishment":
                        account, establishment = args
                        if account == verified == appointment_account:
                            expected_establishment = establishment
                    elif action == "choice":
                        account, index, value = args
                        dialog = appointment_dialog(page)
                        if account != verified or account != appointment_account or dialog.count() != 1:
                            raise ValueError("La consulta ya no está disponible para esta cuenta.")
                        observed = assigned_establishment(dialog)
                        if not observed or observed != expected_establishment:
                            raise ValueError("Confirma el establecimiento observado antes de consultar.")
                        last_response, response_error = 0, False
                        select_choice(dialog, CHOICES[index], value)
                except ValueError as error:
                    self.activity.emit(logging.WARNING, str(error))
                except Error:
                    self.activity.emit(logging.ERROR, "La acción no se completó. Revisa la pantalla de EDUS en Brave.")
                finally:
                    args = ()
                    if action == "login":
                        identification = password = None

                view = None
                if appointment_account and appointment_account == verified:
                    dialog = appointment_dialog(page)
                    if dialog.count() == 1:
                        observed = assigned_establishment(dialog)
                        result, options, selection = read_availability(dialog)
                        if selection != previous_selection:
                            selection_changed = monotonic()
                            previous_selection = selection
                        if observed != expected_establishment or not observed:
                            result = "Confirma el establecimiento mostrado por EDUS."
                            options = ()
                        elif selection and all(value in items for value, items in zip(selection, options)):
                            elapsed = monotonic() - selection_changed
                            if pending_requests or elapsed < 1.2:
                                result = (
                                    "EDUS tarda en actualizar los cupos. Revisa Brave." if elapsed > 12
                                    else "Esperando la actualización de EDUS…"
                                )
                            elif last_response < selection_changed - 0.65:
                                result = "Actualización sin verificar. Revisa la tabla en Brave."
                            elif response_error:
                                result = "EDUS devolvió un error al actualizar los cupos."
                        view = (appointment_account, observed, result, options, selection)
                    else:
                        appointment_account = ""
                if view != previous_availability:
                    self.availability.emit(view)
                    previous_availability = view
                try:
                    page.wait_for_timeout(40 if abs(self.wake_at - time()) < 1.25 else 400)
                except Error as error:
                    if not transient_document_error(error):
                        raise
        finally:
            if not page.is_closed() and browser.is_connected() and is_portal(page.url):
                try:
                    page.evaluate(
                        """([key, marker]) => {
                            window[key]?.abort();
                            delete window[key];
                            sessionStorage.removeItem(key);
                            if (marker) delete window[marker];
                        }""",
                        [self.submission_key, self.document_marker if self.isInterruptionRequested() and not self.session_path else ""],
                    )
                except Error:
                    pass
    def query(self, page, account, verified, identification, booking_after, preparing):
        committed = False

        def progress(message):
            self.activity.emit(logging.INFO, message)
            self.query_progress.emit(message)

        def mark_commit():
            nonlocal committed
            committed = True

        guard_wait = NavigationWait(page, progress, self.isInterruptionRequested)

        def guard():
            if self.isInterruptionRequested():
                raise ValueError("Consulta detenida.")
            if verified() != account.key:
                raise SessionRequired("La cuenta seleccionada ya no se puede verificar.")
            state = inspect_session(page, False)
            if state == UNKNOWN and self.has_proof(page):
                state = guard_wait.until(
                    "Revalidar sesión de EDUS",
                    lambda: observed if (observed := inspect_session(page, False)) != UNKNOWN else None,
                    timeout=0.8,
                )
            if state != AUTHENTICATED:
                raise SessionRequired("La sesión de la cuenta seleccionada ya no se puede verificar.")

        def verify_establishment(observed):
            if not observed:
                raise ValueError("No se encontró el establecimiento en Solicitar Cita.")
            if observed != account.establishment:
                raise EstablishmentChanged(observed)

        try:
            guard()
            if booking_after is not None and self.journal:
                previous_booking = self.journal.state(account.key)
                if previous_booking == 'confirmed':
                    return "complete", "La reserva ya figura confirmada en el registro local."
                if previous_booking:
                    return "error", "Existe un envío pendiente. Se conserva el bloqueo para no duplicar la reserva. Verifica Listado de Citas."
            result = run_query(
                page, account, guard, verify_establishment, progress,
                preparing=preparing, cancelled=self.isInterruptionRequested, on_availability_commit=mark_commit,
            )
            if result.offers:
                try:
                    guard()
                    offer = select_offer(result.offers, account.booking_preference)
                    dialog = appointment_dialog(page)
                    verify_establishment(assigned_establishment(dialog))
                    table = appointment_table(dialog)
                    filters = tuple(current_choice(dialog, label) for label in CHOICES)
                    if (
                        table.count() != 1 or filters != (account.service, account.specialty)
                        or offer not in read_offers(table)
                    ):
                        raise ValueError("El cupo o los filtros cambiaron.")
                    candidate = Appointment(
                        account.key, account.establishment, account.service, account.specialty, *offer.cells[:3]
                    )
                    portal = None
                    may_send = booking_after is not None and result.requested_at >= booking_after and bool(identification and self.journal)
                    self.slot_found.emit(
                        candidate.date, candidate.time, candidate.establishment,
                        "Verificando y enviando si EDUS lo permite…" if may_send else "Cupo encontrado en EDUS.",
                    )
                    if may_send:
                        portal = EdusBookingPage(page, table, offer, identification, is_portal, self.isInterruptionRequested)

                    def booking_guard():
                        if self.isInterruptionRequested() or not is_portal(page.url) or has_login_form(page):
                            raise ValueError("Se detuvo la reserva o se perdió la sesión.")
                        if inspect_session(page, False) in ("Sesión caducada", "Credenciales incorrectas"):
                            raise ValueError("La sesión de EDUS dejó de ser válida.")
                        if verified() != account.key and (portal is None or not portal.ready_to_confirm()):
                            raise ValueError("La cuenta ya no se puede verificar.")

                    outcome = stage_booking(
                        candidate, offer.has_details, portal, self.journal, booking_guard, self.query_progress.emit,
                    )
                except (Error, ValueError):
                    outcome = Outcome("Intervención necesaria", "El cupo cambió o no se pudo verificar. Revisa Brave.")
                result = replace(result, booking=outcome)
            return "ok", result
        except (DatabaseError, OSError):
            return "error", "No se pudo comprobar el registro local de intentos de reserva."
        except SessionRequired as error:
            if committed:
                return "retry", "La sesión caducó durante la consulta de cupos; no se envió ninguna reserva."
            return "session", str(error)
        except EstablishmentChanged as change:
            return "establishment", str(change)
        except (Error, ValueError) as error:
            connected = not page.is_closed() and page.context.browser.is_connected()
            if not connected and not committed and not self.isInterruptionRequested():
                progress("Se perdió CDP antes de consultar. Se recuperará la conexión sin reenviar.")
                raise SafeConnectionLost from error
            if not connected and committed:
                return "retry", "Se perdió CDP durante la consulta de cupos; no se envió ninguna reserva."
            if isinstance(error, Error):
                return "retry", f"Navegación interrumpida ({type(error).__name__}) antes de reservar."
            return "retry", str(error)



class EstablishmentChanged(ValueError):
    pass



class ConnectionLost(Exception):
    pass


class SafeConnectionLost(ConnectionLost):
    pass


class SessionRequired(ValueError):
    pass

