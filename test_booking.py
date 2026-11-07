"""Pruebas locales. El portal de prueba solo devuelve objetos ficticios."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main

from booking import Appointment, BookingJournal, Offer, Receipt, appointment_time, select_offer, stage_booking, unique_offers


APPOINTMENT = Appointment("test-account", "123 - Centro de prueba", "MEDICINA", "MEDICINA GENERAL",
                          "04/11/2026", "08:15", "12345")


class Portal:
    def __init__(self, *, detail=APPOINTMENT, receipt=None, confirm_error=False):
        self.detail = detail
        self.receipt = receipt
        self.confirm_error = confirm_error
        self.sent = 0
        self.verified = 0

    def open_details(self, appointment):
        return self.detail

    def ready_to_confirm(self):
        return True

    def confirm(self):
        self.sent += 1
        if self.confirm_error:
            raise ConnectionError("Simulated")

    def verify(self):
        self.verified += 1
        return self.receipt


class BookingTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "attempts.sqlite3"
        self.journal = BookingJournal(self.path)

    def attempt(self, portal, guard=lambda: None, progress=lambda _: None, control=True):
        return stage_booking(APPOINTMENT, control, portal, self.journal, guard, progress)

    def test_preferences_and_duplicates(self):
        first = Offer(("04/11/2026", "08:15", "1", "", ""), False)
        earlier = Offer(("04/11/2026", "7:30 a. m.", "2", "", ""), True)
        actionable = Offer(first.cells, True)
        available = unique_offers((first, earlier, actionable))
        self.assertEqual(len(available), 2)
        self.assertEqual(select_offer(available, "first"), actionable)
        self.assertEqual(select_offer(available, "earliest"), earlier)
        self.assertEqual(appointment_time(earlier), datetime(2026, 11, 4, 7, 30))
        self.assertIsNone(select_offer((), "first"))
        with self.assertRaises(ValueError):
            unique_offers((first, Offer(("04/11/2026", "07:15", "1", "", ""), True)))

    def test_live_confirmation_has_no_adapter(self):
        self.assertEqual(self.attempt(None).state, "Intervención necesaria")
        self.assertFalse(self.path.exists())
        portal = Portal()
        self.assertEqual(self.attempt(portal, control=False).state, "Intervención necesaria")
        self.assertEqual(portal.sent, 0)

    def test_disappeared_or_mismatched_offer(self):
        for detail in (None, replace(APPOINTMENT, account="other-account")):
            portal = Portal(detail=detail)
            self.assertEqual(self.attempt(portal).state, "Intervención necesaria")
            self.assertEqual(portal.sent, 0)

    def test_confirmed_with_evidence_and_duplicate_after_restart(self):
        portal = Portal(receipt=Receipt(APPOINTMENT, "receipt-1"))
        self.assertEqual(self.attempt(portal).state, "Reserva confirmada")
        self.journal = BookingJournal(self.path)
        second = Portal(receipt=Receipt(APPOINTMENT, "receipt-2"))
        self.assertEqual(self.attempt(second).state, "Intervención necesaria")
        self.assertEqual((portal.sent, portal.verified, second.sent), (1, 1, 0))

    def test_failed_or_ambiguous_confirmation_is_reconciled_and_locked(self):
        portal = Portal(confirm_error=True)
        self.assertEqual(self.attempt(portal).state, "Intervención necesaria")
        self.assertEqual((portal.sent, portal.verified), (1, 1))
        second = Portal()
        self.assertEqual(self.attempt(second).state, "Intervención necesaria")
        self.assertEqual(second.sent, 0)

    def test_wrong_receipt_is_not_success(self):
        wrong = Appointment("different", APPOINTMENT.establishment, APPOINTMENT.service,
                            APPOINTMENT.specialty, APPOINTMENT.date, APPOINTMENT.time, APPOINTMENT.number)
        portal = Portal(receipt=Receipt(wrong, "receipt-1"))
        self.assertEqual(self.attempt(portal).state, "Intervención necesaria")
        self.assertIsNone(self.journal.claim(APPOINTMENT.account))

    def test_stop_or_session_loss_before_send(self):
        checks = iter((True, True, False))
        def guard():
            if not next(checks):
                raise ValueError("Stopped or session lost")
        portal = Portal()
        self.assertEqual(self.attempt(portal, guard=guard).state, "Error")
        self.assertEqual(portal.sent, 0)
        self.assertIsNotNone(self.journal.claim(APPOINTMENT.account))

    def test_transport_error_can_still_be_confirmed_by_portal_evidence(self):
        portal = Portal(receipt=Receipt(APPOINTMENT, "receipt-1"), confirm_error=True)
        self.assertEqual(self.attempt(portal).state, "Reserva confirmada")


if __name__ == "__main__":
    main()

