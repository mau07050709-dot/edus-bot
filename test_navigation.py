"""Pruebas de navegación con un portal simulado y sin solicitudes reales."""

from dataclasses import replace
from time import monotonic
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest import TestCase, main
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright
from PySide6.QtCore import QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import QApplication

from accounts import Account
from appointments import NO_SLOTS, appointment_dialog
from brave import find_brave, resolve_endpoint
from browser import (
    ACCOUNT_PATH, AUTHENTICATED, BAD_CAPTCHA, EXPIRED, PORTAL_URL, WAITING, BrowserThread, ConnectionLost,
    arm_login_proof, captcha_retry_reason, inspect_session, mark_authenticated_document, refresh_cycle_page, submit_captcha,
    submitted_login_identity,
)
from captcha import read_captcha
from playwright.sync_api import Error
from query import prepared_form, run_query
from session_link import SessionLink
from settings import Settings


ORIGIN = "https://edus.ccss.sa.cr"
ACCOUNT = Account("test", "Cuenta de prueba", "0", "123456 - EBAIS PRUEBA")
DIALOG = f"""
<section role="dialog" id="appointment" hidden>
<h2>Solicitar Cita</h2><button aria-label="Cerrar" onclick="closeDialog()">Cerrar</button>
<label>Establecimiento de Salud:</label><p>123456 - EBAIS PRUEBA</p>
<div id="service"></div><div id="specialty"></div>
<table><thead><tr><th>Fecha</th><th>Hora de Cita</th><th>N° de Cita</th>
<th>Consultorio</th><th>Funcionario</th><th>Ver cita</th></tr></thead>
<tbody><tr><td colspan="6">{NO_SLOTS}</td></tr></tbody></table>
</section>
"""
SERVICE = '<select onchange="serviceChanged()"><option>Seleccione Servicio...</option><option>MEDICINA</option></select>'
SPECIALTY = '<select onchange="specialtyChanged()"><option>Seleccione Especialidad...</option><option>MEDICINA GENERAL</option></select>'


class NavigationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.driver = sync_playwright().start()
        cls.addClassCleanup(cls.driver.stop)
        cls.browser = cls.driver.chromium.launch(executable_path=str(find_brave()))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()

    def setUp(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        self.page = context.new_page()
        self.progress = []
        self.sent = []

    def fixture(self, *, button=0, dialog=0, service=0, specialty=0, opened=False, mode="normal", closable=True):
        import json
        form = DIALOG if closable else DIALOG.replace('aria-label="Cerrar"', 'aria-label="Otra acción"')
        html = f"""
        <h1>Listado de Citas</h1><label>Estado Citas:</label>
        <div id="button"></div>{form}
        <script>
        window.opens=0;
        const available = document.querySelector('#appointment');
        let service = document.querySelector('#service');
        const specialty = document.querySelector('#specialty');
        function openDialog() {{
            window.opens++;
            setTimeout(() => {{
                available.hidden=false;
                setTimeout(() => service.innerHTML={json.dumps(SERVICE)}, {service});
            }}, {dialog});
        }}
        function closeDialog() {{
            available.hidden=true;
            service.innerHTML=specialty.innerHTML='';
        }}
        function serviceChanged() {{
            fetch('/service', {{method:'POST'}}).then(() =>
                setTimeout(() => {{
                    const copy=service.cloneNode(true);
                    copy.querySelector('select').selectedIndex=1;
                    service.replaceWith(copy); service=copy;
                    specialty.innerHTML={json.dumps(SPECIALTY)};
                }}, {specialty})
            );
        }}
        function specialtyChanged() {{
            if ({json.dumps(mode)} === 'unrelated') {{
                document.querySelector('tbody').innerHTML='<tr><td colspan="6">{NO_SLOTS}</td></tr>';
                return;
            }}
            fetch('/availability', {{method:'POST'}}).then(response => {{
                if (response.ok && {json.dumps(mode)} !== 'stale')
                    document.querySelector('tbody').innerHTML='<tr><td colspan="6">{NO_SLOTS}</td></tr>';
            }});
        }}
        setTimeout(() => document.querySelector('#button').innerHTML=
            '<button onclick="openDialog()">Agregar una cita</button>', {button});
        if ({str(opened).lower()}) {{
            available.hidden=false;
            service.innerHTML={json.dumps(SERVICE)};
        }}
        if ({json.dumps(mode)} === 'unrelated') fetch('/older');
        </script>
        """

        def respond(route):
            path = route.request.url
            if path.endswith(("/service", "/availability", "/older")):
                self.sent.append(path.rsplit("/", 1)[-1])
                if path.endswith("/older"):
                    self.older = route
                    return
                status = 503 if mode == "error" and path.endswith("/availability") else 200
                return route.fulfill(status=status, body="{}", content_type="application/json")
            route.fulfill(body=html, content_type="text/html; charset=utf-8")

        self.page.route(ORIGIN + "/**", respond)
        self.page.goto(ORIGIN + ACCOUNT_PATH)

    def query(self, **values):
        def guard():
            if inspect_session(self.page, False) != AUTHENTICATED:
                raise ValueError("Sesión caducada o cuenta no verificable.")
        def establishment(observed):
            self.assertEqual(observed, ACCOUNT.establishment)
        return run_query(self.page, ACCOUNT, guard, establishment, self.progress.append, **values)

    def test_delayed_button_dialog_service_and_replaced_dom(self):
        self.fixture(button=3100, dialog=450, service=450, specialty=500)
        result = self.query(preparing=True)
        self.assertFalse(result.rows)
        self.assertTrue(prepared_form(self.page, ACCOUNT))
        self.assertEqual(self.sent, ["service", "availability"])
        self.assertEqual(self.page.evaluate("window.opens"), 1)
        self.assertTrue(any("Recuperación exitosa" in event for event in self.progress))

    def test_reuse_open_dialog_and_refresh_after_close(self):
        self.fixture(opened=True, specialty=150)
        self.query(preparing=True)
        self.assertEqual(self.page.evaluate("window.opens"), 0)
        self.query()
        self.assertEqual(self.page.evaluate("window.opens"), 1)
        self.assertEqual(self.sent, ["service", "availability", "service", "availability"])

    def test_existing_form_with_no_safe_refresh_cannot_be_prepared(self):
        self.fixture(opened=True, closable=False)
        with self.assertRaisesRegex(ValueError, "Validar preparación"):
            self.query(preparing=True)
        self.assertFalse(prepared_form(self.page, ACCOUNT))
        self.assertEqual(self.sent.count("availability"), 1)

    def test_session_loss_and_stop_do_not_send(self):
        self.fixture(button=2000)
        self.page.evaluate("document.body.insertAdjacentHTML('beforeend','<div role=alert>Sesión caducada</div>')")
        with self.assertRaisesRegex(ValueError, "Sesión caducada"):
            self.query()
        self.page.locator("[role=alert]").evaluate("(node) => node.remove()")
        with self.assertRaisesRegex(ValueError, "detenida"):
            self.query(cancelled=lambda: True)
        self.assertEqual(self.sent, [])

    def test_unconfirmed_or_failed_availability_is_never_resent(self):
        self.fixture(mode="error")
        with self.assertRaisesRegex(ValueError, "503"):
            self.query()
        self.assertEqual(self.sent.count("availability"), 1)

    def test_response_window_begins_after_control_selection(self):
        self.fixture()
        from query import select_choice

        def selecting(dialog, label, text, before_select):
            self.page.wait_for_timeout(250)
            select_choice(dialog, label, text, before_select)

        with patch("query.FILTER_RESPONSE_TIMEOUT", 0.15), patch("query.select_choice", side_effect=selecting):
            self.assertFalse(self.query().rows)
        self.assertEqual(self.sent, ["service", "availability"])

    def test_older_response_cannot_certify_new_availability(self):
        self.fixture(mode="unrelated")
        from query import select_choice
        def selecting(dialog, label, text, before_select):
            select_choice(dialog, label, text, before_select)
            if label == "Especialidad":
                self.older.fulfill(body="{}", content_type="application/json")
        with patch("query.select_choice", side_effect=selecting):
            with self.assertRaisesRegex(ValueError, "no confirmó la actualización"):
                self.query()
        self.assertEqual(self.sent, ["older", "service"])

    def test_readiness_invalidated_when_form_closes(self):
        self.fixture(opened=True)
        self.query(preparing=True)
        other = self.page.context.new_page()
        other.bring_to_front()
        self.assertTrue(prepared_form(self.page, ACCOUNT))
        self.page.locator("[aria-label=Cerrar]").click()
        self.assertEqual(appointment_dialog(self.page).count(), 0)
        self.assertFalse(prepared_form(self.page, ACCOUNT))
        self.query()
        self.assertEqual(self.sent.count("availability"), 2)


    def login_fixture(self):
        login = f"""
        <input id="formInicioSesion:usuario" value="000000004">
        <input id="formInicioSesion:clave" type="password">
        <input id="formInicioSesion:captchaDigitado">
        <select id="formInicioSesion:tipIdentificacion_input"><option value="0">Cédula</option>
        <option value="7">CCSS</option></select>
        <button id="formInicioSesion:ejecutarPaso1" onclick="location.href='{ACCOUNT_PATH}'">Ingresar</button>
        """
        def route(request):
            body = login if request.request.url == PORTAL_URL else '<h1>Listado de Citas</h1>'
            request.fulfill(body=body, content_type="text/html; charset=utf-8")
        self.page.route(ORIGIN + "/**", route)
        self.page.goto(PORTAL_URL)
        self.assertTrue(arm_login_proof(self.page, "000000004", "0", "test-submission", "attempt"))

    def test_visual_captcha_uses_normal_submit_and_preserves_manual_input(self):
        app = QApplication.instance() or QApplication([])
        image = QImage(270, 70, QImage.Format.Format_RGB32)
        image.fill(QColor("#0e2239"))
        painter = QPainter(image)
        font = QFont("Arial")
        font.setPixelSize(34)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("white"))
        code = "T7HXF4"
        for index, char in enumerate(code):
            painter.drawText(31 + index * 36, 38 + index % 3 * 5, char)
        painter.end()
        padded = QImage(271, 71, QImage.Format.Format_RGB32)
        padded.fill(QColor("white"))
        painter = QPainter(padded)
        painter.drawImage(0, 1, image)
        painter.end()
        border_data = QByteArray()
        border_buffer = QBuffer(border_data)
        border_buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(padded.save(border_buffer, "PNG"))
        self.assertEqual(read_captcha(bytes(border_data)), code)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(image.save(buffer, "PNG"))
        html = """
        <form id="formInicioSesion">
            <input id="formInicioSesion:usuario" value="000000004">
            <input id="formInicioSesion:clave" type="password" value="fake">
            <select id="formInicioSesion:tipIdentificacion_input"><option value="0">CEDULA</option></select>
            <img src="/CitasWebPF/captcha;fixture?version=1" width="324" height="84">
            <input id="formInicioSesion:captchaDigitado">
            <button id="formInicioSesion:ejecutarPaso1" type="button">Ingresar</button>
        </form>
        <script>
        window.submissions = [];
        document.getElementById('formInicioSesion:ejecutarPaso1').onclick = event => {
            window.submissions.push([event.isTrusted, document.getElementById('formInicioSesion:captchaDigitado').value]);
        };
        </script>
        """
        def route(request):
            if "/captcha;" in request.request.url:
                request.fulfill(body=bytes(data), content_type="image/png")
            else:
                request.fulfill(body=html, content_type="text/html; charset=utf-8")

        self.page.route(ORIGIN + "/**", route)
        self.page.goto(PORTAL_URL)
        field = self.page.locator('input[id="formInicioSesion:captchaDigitado"]')
        field.fill("MANUAL")
        self.assertFalse(submit_captcha(self.page, "000000004", "fake", "0", lambda: False))
        self.assertEqual(field.input_value(), "MANUAL")
        self.assertEqual(self.page.evaluate("window.submissions"), [])
        field.clear()
        self.assertFalse(submit_captcha(self.page, "000000005", "fake", "0", lambda: False))
        self.assertEqual(self.page.evaluate("window.submissions"), [])
        attempt = submit_captcha(self.page, "000000004", "fake", "0", lambda: False)
        self.assertTrue(attempt)
        self.assertEqual(self.page.evaluate("window.submissions"), [[True, code]])
        self.assertFalse(submit_captcha(self.page, "000000004", "fake", "0", lambda: False))
        self.assertEqual(captcha_retry_reason(self.page, WAITING, attempt, True, settled=True), "reset")
        field.fill("MANUAL")
        self.assertIsNone(captcha_retry_reason(self.page, WAITING, attempt, True, settled=True))
        field.clear()
        self.assertIsNone(captcha_retry_reason(self.page, WAITING, attempt, False, settled=True))
        self.assertIsNone(captcha_retry_reason(self.page, WAITING, attempt, True))
        self.assertEqual(captcha_retry_reason(self.page, WAITING, attempt, True, settled=True), "reset")
        self.assertIsNotNone(app)

    def test_periodic_refresh_logs_out_only_the_verified_account_and_reloads_login(self):
        login = """
        <input id="formInicioSesion:usuario">
        <input id="formInicioSesion:clave" type="password">
        <input id="formInicioSesion:captchaDigitado">
        <button id="formInicioSesion:ejecutarPaso1">Ingresar</button>
        """
        private = """
        <h1>Listado de Citas</h1><button>Agregar una cita</button>
        <a id="formSIAC:btnMenuSalir" href="/eduscitasweb/"><i class="mdi-exit-to-app">Salir</i></a>
        <section role="dialog" style="position:fixed; inset:0; background:white">
            <h2>Solicitar Cita</h2><button aria-label="Cerrar" onclick="this.parentElement.hidden=true">Cerrar</button>
        </section>
        """
        visits = []

        def route(request):
            url = request.request.url
            visits.append(url)
            request.fulfill(body=private if url.endswith(ACCOUNT_PATH) else login,
                            content_type="text/html; charset=utf-8")

        self.page.route(ORIGIN + "/**", route)
        self.page.goto(ORIGIN + ACCOUNT_PATH)
        with self.assertRaisesRegex(ValueError, "cuenta seleccionada"):
            refresh_cycle_page(self.page, AUTHENTICATED, lambda: "another-account", ACCOUNT.key, lambda: False)
        self.assertEqual(visits, [ORIGIN + ACCOUNT_PATH])
        refresh_cycle_page(self.page, AUTHENTICATED, lambda: ACCOUNT.key, ACCOUNT.key, lambda: False)
        self.assertEqual(visits, [ORIGIN + ACCOUNT_PATH, PORTAL_URL, PORTAL_URL])
        self.assertEqual(self.page.url, PORTAL_URL)

    def test_stuck_appointment_close_recovers_by_reload_then_logout(self):
        visits = []
        login = '''<input id="formInicioSesion:usuario"><input id="formInicioSesion:clave" type="password">
        <input id="formInicioSesion:captchaDigitado"><button id="formInicioSesion:ejecutarPaso1">Ingresar</button>'''
        private = '''<h1>Listado de Citas</h1><button>Agregar una cita</button>
        <a id="formSIAC:btnMenuSalir" href="/eduscitasweb/">Salir</a>'''
        def route(request):
            visits.append(request.request.url)
            html = private if request.request.url.endswith(ACCOUNT_PATH) else login
            if len(visits) == 1:
                html += '<section role="dialog"><h2>Solicitar Cita</h2><button>Cerrar</button></section>'
            request.fulfill(body=html, content_type='text/html; charset=utf-8')
        self.page.route(ORIGIN + '/**', route)
        self.page.goto(ORIGIN + ACCOUNT_PATH)
        verified = [ACCOUNT.key]
        self.page.on('framenavigated', lambda frame: verified.__setitem__(0, '')
                     if frame == self.page.main_frame else None)
        messages = []
        with patch('browser.close_appointment_dialog', side_effect=ValueError('Cerrar el formulario anterior')):
            refresh_cycle_page(self.page, AUTHENTICATED, lambda: verified[0], ACCOUNT.key,
                               lambda: False, messages.append)
        self.assertEqual(visits, [ORIGIN + ACCOUNT_PATH, ORIGIN + ACCOUNT_PATH, PORTAL_URL, PORTAL_URL])
        self.assertTrue(any('Formulario atascado' in message for message in messages))
        self.assertEqual(self.page.url, PORTAL_URL)
        self.assertEqual(verified[0], '')

    def test_unverified_private_page_is_logged_out_without_trusting_identity(self):
        visits = []
        login = '''<input id="formInicioSesion:usuario"><input id="formInicioSesion:clave" type="password">
        <input id="formInicioSesion:captchaDigitado"><button id="formInicioSesion:ejecutarPaso1">Ingresar</button>'''
        private = '''<h1>Listado de Citas</h1><button>Agregar una cita</button>
        <a id="formSIAC:btnMenuSalir" href="/eduscitasweb/">Salir</a>'''
        def route(request):
            visits.append(request.request.url)
            request.fulfill(body=private if request.request.url.endswith(ACCOUNT_PATH) else login,
                            content_type='text/html; charset=utf-8')
        self.page.route(ORIGIN + '/**', route)
        self.page.goto(ORIGIN + ACCOUNT_PATH)
        worker = BrowserThread(Settings(), 'probe')
        messages = []
        self.assertFalse(worker.has_proof(self.page))
        refresh_cycle_page(self.page, AUTHENTICATED, lambda: '', ACCOUNT.key, lambda: False, messages.append)
        self.assertEqual(visits, [ORIGIN + ACCOUNT_PATH, PORTAL_URL, PORTAL_URL])
        self.assertTrue(any('Marca de cuenta perdida' in message for message in messages))
        self.assertFalse(worker.has_proof(self.page))

    def test_expired_dialog_uses_reentry_even_with_stale_authenticated_state(self):
        visits = []
        login = '''<input id="formInicioSesion:usuario"><input id="formInicioSesion:clave" type="password">
        <input id="formInicioSesion:captchaDigitado"><button id="formInicioSesion:ejecutarPaso1">Ingresar</button>'''
        private = '''<h1>Listado de Citas</h1><button>Agregar una cita</button>
        <section role="dialog"><h2>Sesión caducada</h2>
        <p>La sesión ha caducado, presione el siguiente botón para volver a acceder a la aplicación</p>
        <button onclick="location.href='/eduscitasweb/'">VOLVER A ACCEDER A LA APLICACIÓN</button></section>'''
        def route(request):
            visits.append(request.request.url)
            request.fulfill(body=private if request.request.url.endswith(ACCOUNT_PATH) else login,
                            content_type='text/html; charset=utf-8')
        self.page.route(ORIGIN + '/**', route)
        self.page.goto(ORIGIN + ACCOUNT_PATH)
        messages = []
        refresh_cycle_page(self.page, AUTHENTICATED, lambda: '', ACCOUNT.key, lambda: False, messages.append)
        self.assertEqual(visits, [ORIGIN + ACCOUNT_PATH, PORTAL_URL])
        self.assertTrue(any('Pulsando Volver' in message for message in messages))

    def test_expired_page_without_alert_role_is_recognized(self):
        self.page.route(ORIGIN + '/**', lambda r: r.fulfill(
            body='<h1>La sesión ha expirado.</h1>', content_type='text/html; charset=utf-8'))
        self.page.goto(PORTAL_URL)
        self.assertEqual(inspect_session(self.page, False), EXPIRED)

    def test_automatic_login_retries_more_than_once_and_stops_on_authentication(self):
        app = QApplication.instance() or QApplication([])
        images = {}
        for number in range(5):
            image = QImage(270, 70, QImage.Format.Format_RGB32)
            image.fill(QColor(number, 10, 20))
            data = QByteArray()
            buffer = QBuffer(data)
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            self.assertTrue(image.save(buffer, "PNG"))
            images[number] = bytes(data)

        html = """
        <form id="formInicioSesion">
            <input id="formInicioSesion:usuario">
            <input id="formInicioSesion:clave" type="password">
            <select id="formInicioSesion:tipIdentificacion_input"><option value="0">CEDULA</option></select>
            <img id="formInicioSesion:captcha" src="/CitasWebPF/captcha;fixture?v=0">
            <a href="#" onclick="event.preventDefault(); renew()">
                <img id="formInicioSesion:imagenRefrescar" width="12" height="12" alt="Renovar">
            </a>
            <input id="formInicioSesion:captchaDigitado">
            <button id="formInicioSesion:ejecutarPaso1" type="button">Ingresar</button>
            <div id="notices"></div>
        </form>
        <script>
        const field = name => document.getElementById('formInicioSesion:' + name);
        const rotate = version => field('captcha').src = '/CitasWebPF/captcha;fixture?v=' + version;
        const renew = () => rotate(Number(new URL(field('captcha').src).searchParams.get('v')) + 1);
        const reject = () => document.querySelector('#notices').innerHTML =
            '<div role="alert"><span>Código de seguridad incorrecto</span></div>';
        field('ejecutarPaso1').onclick = async () => {
            const data = new URLSearchParams({
                'javax.faces.source': 'formInicioSesion:ejecutarPaso1',
                'formInicioSesion:usuario': field('usuario').value,
                'formInicioSesion:tipIdentificacion_input': field('tipIdentificacion_input').value,
                'formInicioSesion:captchaDigitado': field('captchaDigitado').value,
            });
            const count = Number(await (await fetch(location.href, {method: 'POST', body: data})).text());
            if (count === 1) reject();
            if (count === 2) {
                document.querySelector('#notices').replaceChildren();
                rotate(3);
            }
            if (count === 3) { reject(); rotate(4); }
            if (count === 4) location.href = '/CitasWebPF/faces/xhtml/comun/Inicio.xhtml';
        };
        </script>
        """

        def route(request):
            url = request.request.url
            if "/captcha;" in url:
                number = int(url.rsplit("=", 1)[-1])
                request.fulfill(body=images[number], content_type="image/png")
            elif request.request.method == "POST":
                self.sent.append(request.request.post_data)
                request.fulfill(body=str(len(self.sent)))
            elif url.endswith(ACCOUNT_PATH):
                request.fulfill(body="<h1>Listado de Citas</h1><button>Agregar una cita</button>")
            else:
                request.fulfill(body=html, content_type="text/html; charset=utf-8")

        self.page.route(ORIGIN + "/**", route)
        self.page.goto(PORTAL_URL)
        worker = BrowserThread(Settings(), "probe", (ACCOUNT.key, "0", "000000004", "fake"))
        authenticated = []
        worker.verified.connect(authenticated.append)
        deadline = monotonic() + 12
        with (
            patch("browser.read_captcha", side_effect=(None, "AAAA11", "BBBB22", "CCCC33", "DDDD44")),
            patch("browser.CAPTCHA_RETRY_DELAY", 0),
            patch.object(worker, "isInterruptionRequested", side_effect=lambda: ACCOUNT.key in authenticated or monotonic() > deadline),
        ):
            worker.watch(self.page, self.browser)
        self.assertIn(ACCOUNT.key, authenticated)
        self.assertEqual(len(self.sent), 4)
        for sent, code in zip(self.sent, ("AAAA11", "BBBB22", "CCCC33", "DDDD44")):
            self.assertIn("formInicioSesion%3AcaptchaDigitado=" + code, sent)
        self.assertIsNotNone(app)

    def test_login_proof_survives_immediate_same_origin_redirect(self):
        self.login_fixture()
        self.assertEqual(mark_authenticated_document(self.page, "marker", "test-submission", "attempt"), "pending")
        self.page.get_by_role("button", name="Ingresar").click()
        self.page.wait_for_url(ORIGIN + ACCOUNT_PATH)
        self.assertEqual(mark_authenticated_document(self.page, "marker", "test-submission", "attempt"), "verified")
        self.assertTrue(self.page.evaluate("window.marker === true"))
        self.assertIsNone(self.page.evaluate("sessionStorage.getItem('test-submission')"))

    def test_wrong_identification_cannot_produce_proof(self):
        self.login_fixture()
        self.page.locator('[id="formInicioSesion:usuario"]').fill("000000999")
        self.page.get_by_role("button", name="Ingresar").click()
        self.page.wait_for_url(ORIGIN + ACCOUNT_PATH)
        self.assertEqual(mark_authenticated_document(self.page, "marker", "test-submission", "attempt"), "unverified")
        self.assertFalse(self.page.evaluate("window.marker === true"))

    def test_private_page_alone_does_not_verify_account(self):
        self.login_fixture()
        self.page.goto(ORIGIN + ACCOUNT_PATH)
        self.assertEqual(mark_authenticated_document(self.page, "marker", "test-submission", "attempt"), "unverified")


    def test_captcha_rejection_and_expiration_are_separate_states(self):
        self.login_fixture()
        self.assertEqual(inspect_session(self.page, True), WAITING)
        self.page.evaluate("""() => document.body.insertAdjacentHTML(
            'beforeend', '<div role="alert">Código de seguridad incorrecto</div>'
        )""")
        self.assertEqual(inspect_session(self.page, True), BAD_CAPTCHA)
        self.page.locator("[role=alert]").evaluate("(alert) => alert.textContent = 'Su sesión ha vencido'")
        self.assertEqual(inspect_session(self.page, False), EXPIRED)
        self.assertEqual(inspect_session(self.page, True), WAITING)

    def test_session_link_rejects_other_account_and_other_document(self):
        self.fixture()
        link = SessionLink.new("test")
        self.page.evaluate("([key, proof]) => window[key] = proof", [link.marker, link.proof("0", "000000004")])
        worker = BrowserThread(Settings(), "probe")
        worker.session_proof = (link.account, "000000004")
        worker.document_marker = link.marker
        worker.document_value = link.proof("0", "000000004")
        self.assertTrue(worker.has_proof(self.page))
        worker.document_value = link.proof("0", "000000009")
        self.assertFalse(worker.has_proof(self.page))
        worker.document_value = link.proof("0", "000000004")
        self.page.reload()
        self.assertFalse(worker.has_proof(self.page))


class ConnectionTests(TestCase):
    def test_repeated_queries_never_send_when_booking_journal_is_pending(self):
        worker = BrowserThread(Settings(), 'probe')
        worker.journal = Mock()
        worker.journal.state.return_value = 'pending'
        with patch('browser.inspect_session', return_value=AUTHENTICATED), patch('browser.run_query') as query:
            for _ in range(25):
                outcome, _ = worker.query(Mock(), ACCOUNT, lambda: ACCOUNT.key, '000000004', object(), False)
                self.assertEqual(outcome, 'error')
            query.assert_not_called()
            worker.journal.claim.assert_not_called()
            worker.journal.state.return_value = 'confirmed'
            self.assertEqual(worker.query(Mock(), ACCOUNT, lambda: ACCOUNT.key, '000000004', object(), False)[0], 'complete')

    def test_personal_cdp_uses_announced_websocket(self):
        with (
            patch("brave.personal_debug_endpoint", return_value=(9422, "/devtools/browser/test")),
            patch("brave.verify_endpoint") as verify,
        ):
            self.assertEqual(resolve_endpoint("brave.exe"), "ws://127.0.0.1:9422/devtools/browser/test")
        verify.assert_called_once_with("brave.exe", 9422)
        with patch("brave.personal_debug_endpoint", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Brave personal"):
                resolve_endpoint("brave.exe")

    def test_primefaces_observer_only_accepts_login_from_expected_frame(self):
        frame = object()
        fields = {
            "javax.faces.source": "formInicioSesion:ejecutarPaso1",
            "formInicioSesion:usuario": "000000004",
            "formInicioSesion:tipIdentificacion_input": "0",
            "formInicioSesion:clave": "secret-fixture",
            "formInicioSesion:captchaDigitado": "manual-fixture",
        }
        request = SimpleNamespace(method="POST", resource_type="xhr", frame=frame,
                                  url=PORTAL_URL, post_data=urlencode(fields))
        self.assertEqual(submitted_login_identity(request, frame), ("000000004", "0"))
        self.assertIsNone(submitted_login_identity(request, object()))
        request.url = "https://example.test/eduscitasweb/"
        self.assertIsNone(submitted_login_identity(request, frame))

    def test_destroyed_document_is_treated_as_pending(self):
        page = type("Page", (), {})()
        with patch.object(page, "evaluate", create=True, side_effect=Error(
            "Execution context was destroyed, most likely because of a navigation"
        )):
            self.assertEqual(mark_authenticated_document(page, "marker", "key", "token"), "pending")

    def test_reconnect_after_temporary_endpoint_failure(self):
        worker = BrowserThread(Settings(), "probe")
        with (
            patch("browser.find_brave", return_value="brave.exe"),
            patch("browser.resolve_endpoint", side_effect=[None, RuntimeError(), None]) as endpoint,
            patch.object(worker, "observe_portal", side_effect=[ConnectionLost(), None]) as observe,
            patch.object(worker, "msleep"),
            patch.object(worker, "report_error") as failure,
        ):
            worker.connect_brave()
        self.assertEqual((endpoint.call_count, observe.call_count), (3, 2))
        failure.assert_not_called()

    def test_never_reconnect_more_than_twice(self):
        worker = BrowserThread(Settings(), "probe")
        with (
            patch("browser.find_brave", return_value="brave.exe"),
            patch("browser.resolve_endpoint"),
            patch.object(worker, "observe_portal", side_effect=ConnectionLost()) as observe,
            patch.object(worker, "msleep"),
            patch.object(worker, "report_error") as failure,
        ):
            worker.connect_brave()
        self.assertEqual(observe.call_count, 3)
        failure.assert_called_once()


if __name__ == "__main__":
    main()


