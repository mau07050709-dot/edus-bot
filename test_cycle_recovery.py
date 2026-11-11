"""Recuperación de ciclos con controles inertes; todo HTTP se sirve localmente."""

from unittest import TestCase, main
from unittest.mock import patch

from playwright.sync_api import TimeoutError, sync_playwright

from brave import find_brave
from browser import (
    ACCOUNT_PATH, AUTHENTICATED, EXPIRED, PORTAL_URL, has_login_form, refresh_cycle_page,
)
from navigation_actions import navigation_click


ORIGIN = 'https://edus.ccss.sa.cr'
ACCOUNT = 'local-recovery-fixture'
LOGIN = '''
<input id="formInicioSesion:usuario">
<input id="formInicioSesion:clave" type="password">
<input id="formInicioSesion:captchaDigitado">
<button id="formInicioSesion:ejecutarPaso1">Ingresar</button>
'''
PRIVATE = '''
<h1>Listado de Citas</h1><button>Agregar una cita</button>
<a id="formSIAC:btnMenuSalir" href="#" onclick="event.preventDefault(); window.logoutClicks++">Salir</a>
<script>window.logoutClicks=0; window.reentryClicks=0;</script>
'''


class CycleRecoveryTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch(executable_path=str(find_brave()))
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.visits = []
        self.messages = []
        # Un control inerte debe agotar su presupuesto, no consumir segundos por prueba.
        for constant in ('RECOVERY_ACTION_TIMEOUT', 'RECOVERY_LOGIN_TIMEOUT'):
            short = patch('browser.' + constant, 350, create=True)
            short.start()
            self.addCleanup(short.stop)

    def serve(self, private):
        def route(request):
            self.visits.append(request.request.url)
            request.fulfill(
                body=private if request.request.url.endswith(ACCOUNT_PATH) else LOGIN,
                content_type='text/html; charset=utf-8',
            )
        # Incluso la URL oficial se intercepta; ninguna petición llega a EDUS.
        self.page.route('**/*', route)
        self.page.goto(ORIGIN + ACCOUNT_PATH)

    def refresh(self, *, state=AUTHENTICATED, verified=lambda: ACCOUNT, cancelled=lambda: False):
        return refresh_cycle_page(self.page, state, verified, ACCOUNT, cancelled, self.messages.append)

    def assert_login_recovered(self):
        self.assertTrue(has_login_form(self.page))
        self.assertEqual(self.page.url, PORTAL_URL)
        self.assertIn(PORTAL_URL, self.visits)
        self.assertLessEqual(len(self.visits), 5, 'La recuperación debe terminar, no navegar indefinidamente.')

    def freeze_utility_animation_frames(self):
        session = self.context.new_cdp_session(self.page)
        contexts = []
        session.on('Runtime.executionContextCreated', lambda event: contexts.append(event['context']))
        session.send('Runtime.enable')
        utility = next(context for context in contexts
                       if context['name'].startswith('__playwright_utility_world'))
        # Se sustituye RAF antes de inyectar los selectores de Playwright; simula
        # que no llegan cuadros de renderizado, sin apagar monitores reales.
        session.send('Runtime.evaluate', {
            'expression': 'window.requestAnimationFrame = () => 0', 'contextId': utility['id'],
        })

    def test_inert_logout_uses_official_navigation_fallback(self):
        self.serve(PRIVATE)
        self.refresh()
        self.assert_login_recovered()

    def test_close_timeout_and_lost_marker_do_not_trap_next_logout(self):
        dialog = '''<section role="dialog"><h2>Solicitar Cita</h2>
        <button aria-label="Cerrar">Cerrar</button></section>'''
        self.serve(PRIVATE + dialog)
        verified = [ACCOUNT]
        self.page.on('framenavigated', lambda frame: verified.__setitem__(0, '')
                     if frame == self.page.main_frame else None)
        with patch('browser.close_appointment_dialog', side_effect=TimeoutError('Control inerte local')):
            self.refresh(verified=lambda: verified[0])
        self.assert_login_recovered()
        self.assertEqual(verified[0], '')

    def test_inert_expired_dialog_reentry_uses_navigation_fallback(self):
        expired = '''<section role="dialog"><h2>Sesión caducada</h2>
        <p>La sesión ha caducado.</p>
        <button onclick="window.reentryClicks++">VOLVER A ACCEDER A LA APLICACIÓN</button></section>'''
        self.serve(PRIVATE + expired)
        self.refresh(state=EXPIRED, verified=lambda: '')
        self.assert_login_recovered()

    def test_known_other_account_is_never_reset(self):
        self.serve(PRIVATE)
        with self.assertRaisesRegex(ValueError, 'cuenta seleccionada'):
            self.refresh(verified=lambda: 'another-account')
        self.assertEqual(self.visits, [ORIGIN + ACCOUNT_PATH])
        self.assertEqual(self.page.evaluate('window.logoutClicks'), 0)

    def test_cancelled_cycle_does_not_click_or_navigate(self):
        self.serve(PRIVATE)
        with self.assertRaisesRegex(ValueError, 'detuvo'):
            self.refresh(cancelled=lambda: True)
        self.assertEqual(self.visits, [ORIGIN + ACCOUNT_PATH])
        self.assertEqual(self.page.evaluate('window.logoutClicks'), 0)

    def test_navigation_helper_rejects_disabled_hidden_and_duplicate_controls(self):
        self.serve(PRIVATE + '''
        <button id="disabled" disabled onclick="window.logoutClicks++">Cerrar</button>
        <button id="aria-disabled" aria-disabled="true" onclick="window.logoutClicks++">Cerrar</button>
        <button id="hidden" hidden onclick="window.logoutClicks++">Cerrar</button>
        <button class="duplicate" onclick="window.logoutClicks++">Cerrar</button>
        <button class="duplicate" onclick="window.logoutClicks++">Cerrar</button>
        ''')
        for selector in ('#disabled', '#aria-disabled', '#hidden', '.duplicate'):
            with self.subTest(selector=selector), self.assertRaises(ValueError):
                navigation_click(self.page.locator(selector), timeout=350)
        self.assertEqual(self.page.evaluate('window.logoutClicks'), 0)

    def test_navigation_helper_works_when_animation_frames_do_not_advance(self):
        self.serve(PRIVATE + '<button id="neutral" onclick="window.logoutClicks++">Cerrar</button>')
        self.freeze_utility_animation_frames()
        with self.assertRaises(TimeoutError):
            self.page.locator('#neutral').click(timeout=500)
        self.assertEqual(self.page.evaluate('window.logoutClicks'), 0)
        navigation_click(self.page.locator('#neutral'), timeout=500)
        self.assertEqual(self.page.evaluate('window.logoutClicks'), 1)

    def test_full_refresh_closes_dialog_and_logs_out_with_frozen_animation_frames(self):
        private = PRIVATE.replace(
            'href="#" onclick="event.preventDefault(); window.logoutClicks++"',
            'href="/eduscitasweb/"',
        )
        private += '''<section role="dialog"><h2>Solicitar Cita</h2>
        <button aria-label="Cerrar" onclick="this.parentElement.hidden=true">Cerrar</button></section>'''
        self.serve(private)
        self.freeze_utility_animation_frames()
        self.refresh()
        self.assert_login_recovered()

    def test_full_expired_reentry_recovers_with_frozen_animation_frames(self):
        self.serve(PRIVATE + '''<section role="dialog"><h2>Sesión caducada</h2>
        <p>La sesión ha caducado.</p>
        <button onclick="location.href='/eduscitasweb/'">VOLVER A ACCEDER A LA APLICACIÓN</button></section>''')
        self.freeze_utility_animation_frames()
        self.refresh(state=EXPIRED, verified=lambda: '')
        self.assert_login_recovered()

    def test_two_full_queries_with_frozen_animation_frames(self):
        import test_navigation as fixtures
        self.sent = []
        self.progress = self.messages
        fixtures.NavigationTests.fixture(self)
        self.freeze_utility_animation_frames()
        first = fixtures.NavigationTests.query(self)
        second = fixtures.NavigationTests.query(self)
        self.assertFalse(first.rows)
        self.assertFalse(second.rows)
        self.assertEqual(self.sent, ['service', 'availability', 'service', 'availability'])
        self.assertEqual(self.page.evaluate('window.opens'), 2)

    def test_full_query_hidden_select_menus_with_frozen_animation_frames(self):
        import test_navigation as fixtures
        def menu(container, identifier, label, value, callback):
            return f'''<div role="combobox" id="{identifier}" aria-controls="{identifier}_panel">
                <select style="display:none" onchange="{callback}()">
                <option>Seleccione {label}...</option><option>{value}</option></select>
                <span>Seleccione {label}...</span>
                <span class="ui-selectonemenu-trigger" onclick="
                    document.getElementById('{identifier}_panel').hidden=false">Abrir</span></div>
                <div id="{identifier}_panel" hidden><span onclick="
                    const select=document.querySelector('#{container} select'); select.selectedIndex=1;
                    select.dispatchEvent(new Event('change')); this.parentElement.hidden=true;">{value}</span></div>'''
        self.sent = []
        self.progress = self.messages
        with patch('test_navigation.SERVICE', menu('service', 'svc', 'Servicio', 'MEDICINA', 'serviceChanged')), \
                patch('test_navigation.SPECIALTY', menu('specialty', 'spec', 'Especialidad', 'MEDICINA GENERAL', 'specialtyChanged')):
            fixtures.NavigationTests.fixture(self)
        self.freeze_utility_animation_frames()
        result = fixtures.NavigationTests.query(self)
        self.assertFalse(result.rows)
        self.assertEqual(self.sent, ['service', 'availability'])


if __name__ == '__main__':
    main()
