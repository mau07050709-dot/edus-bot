"""Escenarios de reserva en HTML local, sin acceso al portal de producción."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main

from playwright.sync_api import sync_playwright

from appointments import appointment_dialog, appointment_table
from booking import Appointment, BookingJournal, stage_booking
from booking_page import EdusBookingPage
from brave import find_brave
from query import read_offers


ORIGIN = "https://edus-fixture.invalid"
APPOINTMENT = Appointment(
    "fixture-account", "123 - EBAIS PRUEBA", "MEDICINA", "MEDICINA GENERAL",
    "04/11/2026", "08:15", "12345",
)
IDENTIFICATION = "000000009"
ROW = """
<tr><td>04/11/2026</td><td>08:15</td><td>12345</td><td>2</td><td>Ficticio</td>
<td><button title="Ver cita" onclick="openDetails()">Ver cita</button></td></tr>
"""
DETAILS = """
<p>N° de Cita:</p><p>12345</p><p>Fecha:</p><p>04/11/2026</p>
<p>Hora de Cita:</p><p>08:15</p><p>Establecimiento:</p><p>123 - EBAIS PRUEBA</p>
<p>Servicio</p><p>MEDICINA</p><p>Especialidad</p><p>MEDICINA GENERAL</p>
"""
ICON_DETAILS = """
<p>N° de Cita</p><p>12345</p><p>Fecha</p><p>04/11/2026</p>
<p>Hora de Cita</p><p>02:00 P.M.</p><p>Establecimiento</p><p>EBAIS PRUEBA</p>
<p>Consultorio</p><p>2</p><p>Funcionario</p><p>Ficticio</p>
<p>Medio de Atención</p><p>NO ESPECIFICADA</p><p>Ubicación</p><p>NO ESPECIFICADA</p>
"""
LISTING = f"""
<h1>Listado de Citas</h1><table><thead><tr><th>N° de Cita</th><th>N° Identificación</th>
<th>Fecha y Hora</th><th>Establecimiento</th><th>Servicio</th></tr></thead>
<tbody><tr><td>12345</td><td>{IDENTIFICATION}</td><td>04/11/2026 08:15</td>
<td>123 - EBAIS PRUEBA</td><td>MEDICINA</td></tr></tbody></table>
"""


class PortalFixtures(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        brave = find_brave()
        cls.browser = cls.playwright.chromium.launch(executable_path=str(brave))
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        temp = TemporaryDirectory(dir=os.environ.get("EDUS_TEST_TMP"))
        self.addCleanup(temp.cleanup)
        self.journal = BookingJournal(Path(temp.name) / "booking.sqlite3")
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.actions = 0
        self.stopped = False

    def load(self, detail, *, extra=False, gone=False, server_error=False, ambiguous=False, new_page=False,
             duplicate=False, icon=False, ambiguous_detail=False):
        listing = LISTING.replace("08:15", "14:00").replace("123 - EBAIS PRUEBA", "EBAIS PRUEBA") if icon else LISTING
        after = "document.body.innerHTML = " + repr(listing) if not ambiguous else "document.body.innerHTML = '<p>Solicitud recibida</p>'"
        extra_step = (
            "document.querySelector('#initial').hidden=true; document.querySelector('#second').hidden=false"
            if extra else after
        )
        rows = ROW * (2 if duplicate else 1)
        submit = "Reservar cita"
        if icon:
            rows = ROW.replace("12345", "98765").replace("openDetails()", "window.wrongRow = true")
            rows += ROW.replace("08:15", "02:00 P.M.").replace(
                '<button title="Ver cita" onclick="openDetails()">Ver cita</button>',
                '<button title="Asignar Cita" onclick="openDetails()"><span class="ui-icon mdi mdi-plus"></span>'
                '<span style="font-size:0">Asignar Cita</span></button>',
            )
            rows = rows.replace("<td>", '<td><span class="ui-column-title" style="display:none">Etiqueta</span>')
            submit = '<span class="ui-icon mdi mdi-check-circle" aria-hidden="true"></span>'
        html = f"""
        <style>.mdi {{ display: inline-block; width: 16px; height: 16px; }}</style>
        <section role="dialog" id="available"><h2>Solicitar Cita</h2>
        <table><thead><tr><th>Fecha</th><th>Hora de Cita</th><th>N° de Cita</th><th>Consultorio</th>
        <th>Funcionario</th><th>Ver cita</th></tr></thead><tbody>{rows}</tbody></table></section>
        <section role="dialog" id="initial" hidden><h2>Detalles de la Cita</h2>{detail}
        <button onclick="confirmFirst()">{submit}</button></section>
        <section role="dialog" id="second" hidden><h2>Confirmar reserva</h2>{DETAILS}
        <button onclick="confirmSecond()">Confirmar reserva</button></section>
        <script>
        function openDetails() {{
            window.detailClicks = (window.detailClicks || 0) + 1;
            document.querySelector('#available').hidden = true;
            if ({str(new_page).lower()}) location.href='/details';
            else if ({str(gone).lower()}) document.body.innerHTML = '<p>Cupo no disponible</p>';
            else if ({str(ambiguous_detail).lower()}) document.body.innerHTML = '<p>Procesando…</p>';
            else document.querySelector('#initial').hidden = false;
        }}
        async function confirmFirst() {{
            const response = await fetch('/commit', {{method:'POST'}});
            if (response.ok) {{ {extra_step}; }}
        }}
        async function confirmSecond() {{
            await fetch('/final', {{method:'POST'}});
            {after};
        }}
        </script>
        """
        # JSON lleva el HTML al navegador sin depender del escape de cadenas de Python.
        import json
        literal = json.dumps(listing)
        if not ambiguous:
            html = html.replace(repr(listing), literal)
        new_screen = f"""
        <main><h1>Detalles de la Cita</h1>{detail}<p>Identificación:</p><p>{IDENTIFICATION}</p>
        <button onclick="fetch('/commit',{{method:'POST'}}).then(() => location.href='/listed')">Reservar cita</button></main>
        """

        def route(request):
            path = request.request.url
            if path in (ORIGIN + "/commit", ORIGIN + "/final"):
                self.actions += 1
                return request.fulfill(status=503 if server_error else 200, body="")
            content = new_screen if path.endswith("/details") else listing if path.endswith("/listed") else html
            return request.fulfill(body=content, content_type="text/html; charset=utf-8")

        self.page.route(ORIGIN + "/**", route)
        self.page.goto(ORIGIN + "/")
        table = appointment_table(appointment_dialog(self.page))
        offers = read_offers(table)
        self.assertEqual(len(offers), 2 if icon else 1)
        offer = next(offer for offer in offers if offer.cells[2] == APPOINTMENT.number)
        self.assertTrue(offer.has_details)
        return EdusBookingPage(
            self.page, table, offer, IDENTIFICATION,
            lambda url: url.startswith(ORIGIN + "/"), lambda: self.stopped,
        )

    def reserve(self, portal, appointment=APPOINTMENT):
        return stage_booking(appointment, portal.offer.has_details, portal, self.journal, lambda: None, lambda _: None)

    def test_a_dialog_and_receipt(self):
        self.assertEqual(self.reserve(self.load(DETAILS)).state, "Reserva confirmada")
        self.assertEqual(self.actions, 1)
        self.assertEqual(self.journal.state(APPOINTMENT.account), "confirmed")

    def test_b_new_screen_with_verified_identification(self):
        self.assertEqual(self.reserve(self.load(DETAILS, new_page=True)).state, "Reserva confirmada")
        self.assertEqual(self.actions, 1)

    def test_c_separate_confirmation(self):
        self.assertEqual(self.reserve(self.load(DETAILS, extra=True)).state, "Reserva confirmada")
        self.assertEqual(self.actions, 2)

    def test_d_unknown_required_field(self):
        self.assertEqual(
            self.reserve(self.load(DETAILS + '<label>Motivo<input required></label>')).state,
            "Intervención necesaria",
        )
        self.assertEqual(self.actions, 0)
        self.assertIsNone(self.journal.state(APPOINTMENT.account))

    def test_e_disappearing_slot(self):
        self.assertEqual(self.reserve(self.load(DETAILS, gone=True)).state, "Intervención necesaria")
        self.assertEqual(self.actions, 0)

    def test_f_ambiguous_response_blocks_new_send(self):
        portal = self.load(DETAILS, ambiguous=True)
        self.assertEqual(self.reserve(portal).state, "Intervención necesaria")
        self.assertEqual(self.reserve(portal).state, "Intervención necesaria")
        self.assertEqual(self.actions, 1)
        self.assertEqual(self.journal.state(APPOINTMENT.account), "pending")

    def test_f_rejected_response_stays_pending(self):
        self.assertEqual(self.reserve(self.load(DETAILS, server_error=True)).state, "Intervención necesaria")
        self.assertEqual(self.actions, 1)
        self.assertEqual(self.journal.state(APPOINTMENT.account), "pending")

    def test_duplicate_visual_row_is_not_clicked(self):
        self.assertEqual(self.reserve(self.load(DETAILS, duplicate=True)).state, "Intervención necesaria")
        self.assertEqual(self.actions, 0)

    def test_cancel_before_click(self):
        portal = self.load(DETAILS)
        self.stopped = True
        self.assertEqual(self.reserve(portal).state, "Intervención necesaria")
        self.assertEqual(self.actions, 0)

    def test_plus_row_and_separate_check_control(self):
        from dataclasses import replace

        portal = self.load(ICON_DETAILS, icon=True)
        appointment = replace(APPOINTMENT, time="02:00 P.M.")
        self.assertEqual(self.reserve(portal, appointment).state, "Reserva confirmada")
        self.assertIsNone(self.page.evaluate("window.wrongRow"))
        self.assertEqual(self.page.evaluate("window.detailClicks"), 1)
        self.assertEqual(self.actions, 1)

    def test_wrapped_clinician_matches_single_line_detail(self):
        from dataclasses import replace

        portal = self.load(ICON_DETAILS, icon=True)
        self.page.locator('#available tbody tr').nth(1).locator('td').nth(4).evaluate(
            "cell => cell.innerHTML = 'Julio Alonso<br>Calderon Calderon'"
        )
        self.page.locator('#initial p').filter(has_text='Ficticio').evaluate(
            "node => node.textContent = 'Julio Alonso Calderon Calderon'"
        )
        portal.offer = next(offer for offer in read_offers(portal.table) if offer.cells[2] == APPOINTMENT.number)
        self.assertIn('\n', portal.offer.cells[4])
        self.assertEqual(self.reserve(portal, replace(APPOINTMENT, time='02:00 P.M.')).state, 'Reserva confirmada')
        self.assertEqual(self.actions, 1)

    def structured_detail(self, wrong_date=False):
        import re
        detail = re.sub(r'<p>(.*?)</p><p>(.*?)</p>',
                        r'<div class="ed-input-label"><label>\1</label>\2\n</div>', ICON_DETAILS)
        if wrong_date:
            detail = detail.replace('</label>04/11/2026', '</label>05/11/2026')
        return detail

    def test_real_label_and_bare_text_detail(self):
        from dataclasses import replace
        portal = self.load(self.structured_detail(), icon=True)
        self.assertEqual(self.reserve(portal, replace(APPOINTMENT, time='02:00 P.M.')).state, 'Reserva confirmada')
        self.assertEqual(self.actions, 1)

    def test_real_label_wrong_date_blocks_confirmation(self):
        from dataclasses import replace
        portal = self.load(self.structured_detail(wrong_date=True), icon=True)
        self.assertEqual(self.reserve(portal, replace(APPOINTMENT, time='02:00 P.M.')).state, 'Intervención necesaria')
        self.assertEqual(self.actions, 0)

    def test_ambiguous_detail_click_is_not_repeated(self):
        portal = self.load(DETAILS, ambiguous_detail=True)
        self.assertEqual(self.reserve(portal).state, "Intervención necesaria")
        self.assertEqual(self.reserve(portal).state, "Intervención necesaria")
        self.assertEqual(self.page.evaluate("window.detailClicks"), 1)
        self.assertEqual(self.actions, 0)
        self.assertEqual(self.journal.state(APPOINTMENT.account), "pending")

    def test_action_column_moved_after_read_blocks_click(self):
        portal = self.load(DETAILS)
        self.page.locator("#available th:last-child").evaluate(
            "header => header.parentElement.insertBefore(header, header.previousElementSibling)"
        )
        self.assertEqual(self.reserve(portal).state, "Intervención necesaria")
        self.assertIsNone(self.page.evaluate("window.detailClicks"))
        self.assertEqual(self.actions, 0)


if __name__ == "__main__":
    main()
