"""Sesiones CDP reales contra Brave headless temporal; todo HTTP es local.

No se accede al perfil personal, a credenciales ni a EDUS de producción.
"""

from contextlib import nullcontext
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from unittest import TestCase, main
from unittest.mock import patch

from playwright.sync_api import Error, TimeoutError, sync_playwright

from brave import find_brave
from browser import ACCOUNT_PATH, AUTHENTICATED, PORTAL_URL, BrowserThread, has_login_form, inspect_session
from settings import Settings


ORIGIN = 'https://edus.ccss.sa.cr'
PRIVATE_URL = ORIGIN + ACCOUNT_PATH
LOGIN = '''<input id="formInicioSesion:usuario"><input id="formInicioSesion:clave" type="password">
<input id="formInicioSesion:captchaDigitado"><button id="formInicioSesion:ejecutarPaso1">Ingresar</button>'''
PRIVATE = '<h1>Listado de Citas</h1><button>Agregar una cita</button>'


class CdpPersistenceTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)

    def setUp(self):
        temporary = TemporaryDirectory(prefix='edus-cdp-local-')
        self.profile = Path(temporary.name)
        self.addCleanup(temporary.cleanup)
        self.process = subprocess.Popen(
            [str(find_brave()), '--headless=new', '--remote-debugging-address=127.0.0.1',
             '--remote-debugging-port=0', '--user-data-dir=' + str(self.profile),
             '--no-first-run', '--no-default-browser-check', 'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        self.addCleanup(self.stop_process)
        descriptor = self.profile / 'DevToolsActivePort'
        deadline = monotonic() + 15
        lines = []
        while monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail('Brave headless local terminó antes de ofrecer CDP.')
            try:
                lines = descriptor.read_text(encoding='ascii').splitlines()
            except (FileNotFoundError, PermissionError):
                pass
            if len(lines) >= 2:
                break
            sleep(0.05)
        self.assertGreaterEqual(len(lines), 2, 'No se encontró el endpoint CDP temporal.')
        self.endpoint = f'ws://127.0.0.1:{int(lines[0])}{lines[1]}'
        self.worker = BrowserThread(Settings(), 'probe')
        self.worker.cdp_endpoint = self.endpoint
        self.requests = []
        self.connect_count = 0
        self.connected = []
        self.finished = False

    def stop_process(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def connect(self, *args, **kwargs):
        self.connect_count += 1
        client = self.actual_connect(*args, **kwargs)
        self.connected.append(client)
        self.addCleanup(lambda: client.close() if client.is_connected() else None)
        for context in client.contexts:
            context.route('**/*', self.route)
        return client

    def route(self, route):
        request = route.request
        self.requests.append((request.method, request.url))
        if request.method == 'POST':
            route.fulfill(body='{"slots":0}', content_type='application/json')
        else:
            route.fulfill(body=PRIVATE if request.url.endswith(ACCOUNT_PATH) else LOGIN,
                          content_type='text/html; charset=utf-8')

    def observe(self, watch):
        self.actual_connect = self.playwright.chromium.connect_over_cdp
        with patch('browser.sync_playwright', return_value=nullcontext(self.playwright)), \
                patch.object(self.playwright.chromium, 'connect_over_cdp', side_effect=self.connect), \
                patch.object(self.worker, 'watch', side_effect=watch), \
                patch.object(self.worker, 'isInterruptionRequested', side_effect=lambda: self.finished), \
                patch.object(self.worker, 'msleep'):
            self.worker.observe_portal()

    def test_100_real_cdp_cycles_and_19_errors_keep_one_connection(self):
        attempts = []
        failures = []

        def watch(page, browser):
            while len(attempts) < 100:
                page.goto(PORTAL_URL, wait_until='domcontentloaded')
                self.assertTrue(has_login_form(page))
                page.goto(PRIVATE_URL, wait_until='domcontentloaded')
                self.assertEqual(inspect_session(page, False), AUTHENTICATED)
                self.assertEqual(page.evaluate("fetch('/CitasWebPF/availability', {method:'POST'}).then(r=>r.json())"),
                                 {'slots': 0})
                attempts.append(page.url)
                self.assertTrue(browser.is_connected())
                if len(attempts) == 100:
                    self.finished = True
                    return
                if len(attempts) % 5 == 0:
                    failures.append(len(attempts))
                    error_type = TimeoutError if len(failures) % 2 else Error
                    raise error_type('Fallo local simulado después de consultar; conexión sigue activa.')

        self.observe(watch)
        self.assertEqual((len(attempts), len(failures), self.connect_count), (100, 19, 1))
        self.assertEqual(sum(method == 'POST' for method, _ in self.requests), 100)
        self.assertTrue(all(url.startswith(ORIGIN + '/') for _, url in self.requests))
        self.assertTrue(self.connected[0].is_connected())

    def test_12_closed_targets_are_replaced_without_reconnecting_cdp(self):
        targets = []

        def watch(page, browser):
            targets.append(page)
            page.goto(PRIVATE_URL, wait_until='domcontentloaded')
            self.assertEqual(inspect_session(page, False), AUTHENTICATED)
            if len(targets) == 12:
                self.finished = True
                return
            page.close()
            self.assertTrue(browser.is_connected())
            raise Error('La pestaña local fue cerrada; el navegador sigue conectado.')

        self.observe(watch)
        self.assertEqual(len(targets), 12)
        self.assertEqual(len({id(page) for page in targets}), 12)
        self.assertEqual(self.connect_count, 1)


if __name__ == '__main__':
    main()
