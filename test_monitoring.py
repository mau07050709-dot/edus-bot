"""Pruebas de vigilancia con reloj simulado y sin abrir perfiles de Brave."""

from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from PySide6.QtWidgets import QApplication

from accounts import Account
from booking import Outcome
from browser import AUTHENTICATED, EXPIRED, WAITING
from monitoring import WatchCycles, watch_window
from query import Result
from settings import Settings, load_settings, save_settings
from window import MainWindow


NOW = datetime(2031, 6, 1, 4, 45, tzinfo=ZoneInfo("America/Costa_Rica"))
ACCOUNT = Account("a" * 32, "Cuenta ficticia", "0", "123 - EBAIS PRUEBA")
OTHER = Account("b" * 32, "Otra cuenta", "0", "456 - OTRO EBAIS")


class CycleTests(TestCase):
    def test_no_overlap_no_catch_up_and_actual_start_interval(self):
        watch = WatchCycles(60, 10, NOW + timedelta(hours=1), 10)
        self.assertTrue(watch.begin(NOW, 10))
        self.assertFalse(watch.begin(NOW, 95))
        watch.finish(NOW, 0, actual_start=20)
        self.assertFalse(watch.begin(NOW, 79))
        self.assertTrue(watch.begin(NOW, 95))
        self.assertFalse(watch.begin(NOW, 96))
        watch.finish(NOW, 1)
        self.assertFalse(watch.begin(NOW, 500))
        self.assertEqual((watch.count, watch.slots), (2, 1))

    def test_pause_and_stop_and_limit(self):
        watch = WatchCycles(60, 2, NOW + timedelta(hours=1), 0)
        self.assertTrue(watch.begin(NOW, 0))
        watch.pause()
        self.assertEqual(watch.count, 0)
        self.assertFalse(watch.begin(NOW, 59))
        self.assertTrue(watch.begin(NOW, 60))
        watch.finish(NOW, 0)
        self.assertTrue(watch.begin(NOW, 120))
        watch.finish(NOW, 0)
        self.assertTrue(watch.stopped)
        watch.finish(NOW, 0)
        self.assertEqual(watch.count, 2)

    def test_time_window_crosses_midnight_and_has_hard_end(self):
        start, end = watch_window(NOW, "23:55", "00:15")
        self.assertEqual((start.day, start.hour, end.day, end.minute), (1, 23, 2, 15))
        start, end = watch_window(NOW, "", "")
        self.assertEqual(end - start, timedelta(hours=24))
        watch = WatchCycles(60, 10, end, 0)
        self.assertFalse(watch.begin(end, 0))


class WindowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        settings = Settings(accounts=[asdict(ACCOUNT), asdict(OTHER)], default_account=ACCOUNT.key)
        save_settings(self.directory / "settings.json", settings)
        self.vault = Mock()
        self.vault.load.return_value = ("000000001", "test-only")
        self.window = MainWindow(self.directory, self.vault)
        self.addCleanup(self.close_window)
        self.window.timer.stop()
        self.window.notify = Mock()
        sound = patch("window.winsound.PlaySound")
        self.sound = sound.start()
        self.addCleanup(sound.stop)
        self.now, self.tick = NOW, 10
        for variable, getter in (
            ("costa_rica_now", lambda: self.now),
            ("monotonic", lambda: self.tick),
        ):
            patcher = patch(f"window.{variable}", side_effect=getter)
            patcher.start()
            self.addCleanup(patcher.stop)

    def simulate(self):
        self.window.watch_environment.setCurrentIndex(self.window.watch_environment.findData("simulation"))

    def close_window(self):
        self.window.browser = None
        self.window.close()
        for handler in self.window.logger.handlers:
            handler.close()

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.tick += seconds
        self.window.advance_consultation()
        self.app.processEvents()

    def test_offline_three_cycles_popup_and_simulated_send(self):
        self.simulate()
        with patch.object(self.window, "start_browser") as browser:
            self.window.start_edus()
            self.app.processEvents()
            watch = self.window.consultation.watch
            self.assertEqual(watch.count, 1)
            self.assertIn("VIGILANCIA ACTIVA", self.window.query_state.text())
            self.assertIn("60 s", self.window.watch_rate.text())
            self.advance(59)
            self.assertEqual(watch.count, 1)
            self.advance(1)
            self.assertEqual(watch.count, 2)
            self.advance(60)
            self.assertIsNone(self.window.consultation)
            self.assertEqual(watch.count, 3)
            self.assertEqual(self.window.query_state.text(), "Reserva simulada")
            self.assertTrue(self.window.slot_dialog.isVisible())
            self.assertIn("ficticio", self.window.slot_status.text())
            self.assertEqual(self.window.slot_establishment.text(), "Establecimiento ficticio")
            self.sound.assert_called_once()
            self.window.silence_alarm.click()
            self.sound.assert_called_with(None, 0)
            self.advance(360)
            self.assertEqual(watch.count, 3)
            browser.assert_not_called()
        self.assertEqual(load_settings(self.directory / "settings.json").watch_interval, 60)
        self.vault.load.assert_not_called()

    def test_stop_cancels_queued_simulation(self):
        self.simulate()
        self.window.start_edus()
        watch = self.window.consultation.watch
        self.window.stop_browser()
        self.app.processEvents()
        self.advance(60)
        self.assertTrue(watch.stopped)
        self.assertEqual(watch.count, 0)

    def test_periodic_path_refreshes_before_reauthentication_and_retries_error(self):
        self.window.watch_environment.setCurrentIndex(self.window.watch_environment.findData("periodic"))
        worker = Mock()
        self.window.browser = worker
        self.window.browser_status.setText("Brave conectado")
        self.window.auth_status.setText(AUTHENTICATED)
        self.window.verified_account = ACCOUNT.key
        self.window.start_edus()
        watch = self.window.consultation.watch
        worker.request_query.assert_called_once_with(ACCOUNT, NOW)
        result = Result(ACCOUNT.alias, ACCOUNT.establishment, ACCOUNT.service, ACCOUNT.specialty,
                        NOW, NOW, NOW, (), (("Total", 1.0),), started_tick=11)
        self.window.finish_query("ok", result)
        self.assertEqual(len(self.window.history.recent(ACCOUNT.key)), 1)
        self.assertEqual(self.window.history.recent(OTHER.key), [])
        self.window.verified_account = ""
        self.window.update_authentication(EXPIRED)
        self.advance(119)
        worker.request_cycle_refresh.assert_not_called()
        worker.request_login.assert_not_called()
        self.advance(1)
        worker.request_cycle_refresh.assert_called_once_with(ACCOUNT.key)
        self.advance(90)
        worker.request_cycle_refresh.assert_called_once()
        self.window.finish_cycle_refresh("")
        worker.request_login.assert_called_once()
        self.window.update_authentication(WAITING)
        self.advance(100)
        self.assertEqual(worker.request_query.call_count, 1)
        self.window.verified_account = ACCOUNT.key
        self.window.update_authentication(AUTHENTICATED)
        self.advance(1)
        self.assertEqual(worker.request_query.call_count, 2)
        self.window.finish_query("error", "EDUS devolvió HTTP 429.")
        self.assertFalse(watch.stopped)
        self.advance(240)
        self.assertEqual(worker.request_query.call_count, 2)
        self.assertIsNotNone(self.window.consultation)

    def test_main_button_connects_immediately_even_with_legacy_schedule(self):
        self.assertEqual(self.window.settings.mode, "scheduled")
        with patch.object(self.window, "start_browser") as browser:
            self.window.start_edus()
        self.assertEqual(self.window.consultation.starts, NOW)
        self.assertIsNone(self.window.consultation.watch.interval)
        self.assertTrue(self.window.consultation.auto_booking)
        self.assertFalse(self.window.account_selector.isEnabled())
        browser.assert_called_once_with("probe")

    def test_default_real_mode_queries_once(self):
        worker = Mock()
        self.window.browser = worker
        self.window.browser_status.setText("Brave conectado")
        self.window.auth_status.setText(AUTHENTICATED)
        self.window.verified_account = ACCOUNT.key
        self.window.start_edus()
        worker.request_query.assert_called_once_with(ACCOUNT, NOW)
        self.assertIn("Consulta única", self.window.watch_rate.text())
        self.assertIn("Consulta única", self.window.query_state.text())
        result = Result(ACCOUNT.alias, ACCOUNT.establishment, ACCOUNT.service, ACCOUNT.specialty,
                        NOW, NOW, NOW, (), (("Total", 1.0),), started_tick=10)
        self.window.finish_query("ok", result)
        self.advance(120)
        worker.request_query.assert_called_once()
        self.assertIsNone(self.window.consultation)
        self.assertEqual(self.window.query_state.text(), "Sin cupos")
        self.assertIn("Próxima: —", self.window.watch_stats.text())

    def test_real_periodic_mode_alerts_and_retries_unconfirmed_slot(self):
        worker = Mock()
        self.window.browser = worker
        self.window.browser_status.setText("Brave conectado")
        self.window.auth_status.setText(AUTHENTICATED)
        self.window.verified_account = ACCOUNT.key
        self.window.watch_interval.setValue(120)
        self.window.watch_environment.setCurrentIndex(self.window.watch_environment.findData("periodic"))
        self.window.start_edus()
        self.assertFalse(self.window.consultation.simulation)
        watch = self.window.consultation.watch
        self.assertEqual(watch.interval, 120)
        self.assertIsNone(watch.limit)
        self.assertIsNone(watch.end)
        worker.request_query.assert_called_once_with(ACCOUNT, NOW)
        self.advance(4)
        first = Result(ACCOUNT.alias, ACCOUNT.establishment, ACCOUNT.service, ACCOUNT.specialty,
                       self.now, NOW, NOW, (), (("Total", 1.0),), started_tick=10)
        self.window.finish_query("ok", first)
        self.assertIn("Próxima: 01/06 04:47:04", self.window.watch_stats.text())
        self.advance(119)
        worker.request_query.assert_called_once()
        worker.request_cycle_refresh.assert_not_called()
        self.advance(1)
        worker.request_cycle_refresh.assert_called_once_with(ACCOUNT.key)
        worker.request_query.assert_called_once()
        self.window.finish_cycle_refresh("")
        self.window.verified_account = ACCOUNT.key
        self.window.update_authentication(AUTHENTICATED)
        self.advance(1)
        self.assertEqual(worker.request_query.call_count, 2)

        date, hour = "01/06/2031", "08:15"
        self.window.show_slot_window(date, hour, ACCOUNT.establishment, "Verificando cita…")
        self.assertTrue(self.window.slot_dialog.isVisible())
        self.assertEqual(self.window.slot_establishment.text(), ACCOUNT.establishment)
        self.sound.assert_called_once()
        self.assertIn("Próxima: —", self.window.watch_stats.text())
        self.advance(10)
        self.assertEqual(worker.request_query.call_count, 2)
        second = Result(ACCOUNT.alias, ACCOUNT.establishment, ACCOUNT.service, ACCOUNT.specialty,
                        self.now, self.now, self.now, ((date, hour, "A1", "1", "Prueba"),),
                        (("Total", 1.0),), booking=Outcome("Intervención necesaria", "Revisa EDUS."))
        self.window.finish_query("ok", second)
        self.advance(120)
        self.assertIsNotNone(self.window.consultation)
        self.assertFalse(watch.stopped)
        self.assertEqual(worker.request_query.call_count, 2)

    def test_stop_during_refresh_ignores_late_completion(self):
        worker = Mock()
        self.window.browser = worker
        self.window.browser_status.setText("Brave conectado")
        self.window.auth_status.setText(AUTHENTICATED)
        self.window.verified_account = ACCOUNT.key
        self.window.watch_environment.setCurrentIndex(self.window.watch_environment.findData("periodic"))
        self.window.start_edus()
        self.window.finish_query("ok", Result(ACCOUNT.alias, ACCOUNT.establishment, ACCOUNT.service,
                                              ACCOUNT.specialty, NOW, NOW, NOW, (), (("Total", 1.0),)))
        self.advance(120)
        worker.request_cycle_refresh.assert_called_once_with(ACCOUNT.key)
        self.window.stop_browser()
        worker.requestInterruption.assert_called_once()
        self.window.finish_cycle_refresh("")
        worker.request_login.assert_not_called()
        worker.request_query.assert_called_once()
        self.assertIsNone(self.window.consultation)

    def test_switch_account_drops_authorization_and_manual_query_cannot_send(self):
        with patch.object(self.window, "start_browser"):
            self.window.start_edus()
            self.assertTrue(self.window.consultation.auto_booking)
            self.window.account_selector.setCurrentIndex(self.window.account_selector.findData(OTHER.key))
            self.assertIsNone(self.window.consultation)
            self.window.consult_now()
            self.assertEqual(self.window.consultation.account, OTHER.key)
            self.assertFalse(self.window.consultation.auto_booking)

    def periodic_worker(self):
        worker = Mock()
        self.window.browser = worker
        self.window.browser_status.setText('Brave conectado')
        self.window.auth_status.setText(AUTHENTICATED)
        self.window.verified_account = ACCOUNT.key
        self.window.watch_environment.setCurrentIndex(self.window.watch_environment.findData('periodic'))
        self.window.start_edus()
        return worker

    def test_100_periodic_cycles_continue_after_refresh_and_session_failures(self):
        worker = self.periodic_worker()
        plan = self.window.consultation
        for index in range(100):
            result = Result(ACCOUNT.alias, ACCOUNT.establishment, ACCOUNT.service, ACCOUNT.specialty,
                            self.now, self.now, self.now, (), (('Total', 1.0),), started_tick=self.tick)
            self.window.finish_query('ok', result)
            self.advance(120)
            if index in (30, 40):
                self.window.finish_cycle_refresh('Cerrar el formulario anterior: tiempo agotado')
                self.assertIs(self.window.consultation, plan)
                self.assertFalse(plan.watch.stopped)
                self.advance(119)
                self.assertEqual(plan.phase, 'monitor_idle')
                self.advance(1)
            self.window.finish_cycle_refresh('')
            self.window.verified_account = ACCOUNT.key
            self.window.update_authentication(AUTHENTICATED)
            self.advance(1)
        self.assertEqual(plan.watch.count, 100)
        self.assertEqual(plan.recoveries, 2)
        self.window.finish_query('retry', 'La sesión caducó durante la consulta de cupos')
        self.assertIs(self.window.consultation, plan)
        self.advance(120)
        worker.request_cycle_refresh.assert_called_with(ACCOUNT.key)
        self.window.stop_browser()
        self.assertIsNone(self.window.consultation)

    def test_cdp_loss_reconnects_with_delay_also_after_slot_detection(self):
        self.periodic_worker()
        plan = self.window.consultation
        plan.phase = 'monitor_idle'
        self.window.browser_finished()
        self.assertIs(self.window.consultation, plan)
        with patch.object(self.window, 'start_browser') as reconnect:
            self.advance(119)
            reconnect.assert_not_called()
            self.advance(1)
            reconnect.assert_called_once_with('probe')
        self.window.browser = Mock()
        plan.slot_found = True
        self.window.browser_finished()
        self.assertIs(self.window.consultation, plan)
        self.assertFalse(plan.watch.stopped)

    def test_25_consecutive_failures_then_a_successful_query(self):
        worker = self.periodic_worker()
        plan = self.window.consultation
        self.window.finish_query('error', 'EDUS no responde')
        for _ in range(24):
            self.advance(119)
            self.assertEqual(plan.phase, 'monitor_idle')
            self.advance(1)
            self.assertEqual(plan.phase, 'refreshing')
            self.window.finish_cycle_refresh('EDUS sigue sin responder')
            self.assertIs(self.window.consultation, plan)
        self.assertEqual(plan.recoveries, 25)
        self.assertEqual(plan.watch.count, 0)
        self.advance(120)
        self.window.finish_cycle_refresh('')
        self.window.verified_account = ACCOUNT.key
        self.window.update_authentication(AUTHENTICATED)
        self.advance(1)
        result = Result(ACCOUNT.alias, ACCOUNT.establishment, ACCOUNT.service, ACCOUNT.specialty,
                        self.now, self.now, self.now, (), (('Total', 1.0),), started_tick=self.tick)
        self.window.finish_query('ok', result)
        self.assertEqual(plan.watch.count, 1)
        self.assertFalse(plan.watch.stopped)
        self.window.stop_browser()
        self.assertIsNone(self.window.consultation)

    def test_watchdog_keeps_authorized_worker_and_ignores_late_results(self):
        worker = self.periodic_worker()
        plan = self.window.consultation
        self.advance(120)
        self.assertEqual(plan.phase, 'monitor_idle')
        worker.requestInterruption.assert_not_called()
        self.window.finish_query('error', 'Resultado tardío')
        self.assertEqual(plan.phase, 'monitor_idle')
        with patch.object(self.window, 'start_browser') as start:
            self.advance(120)
            start.assert_not_called()
            worker.request_cycle_refresh.assert_called_once_with(ACCOUNT.key)
        self.assertIs(self.window.consultation, plan)
        self.assertIs(self.window.browser, worker)

    def test_missing_credentials_keeps_next_cycle_and_success_ends_watch(self):
        self.periodic_worker()
        plan = self.window.consultation
        plan.phase = 'ready'
        self.window.auth_status.setText(EXPIRED)
        self.window.verified_account = ''
        self.vault.load.return_value = None
        self.advance(1)
        self.assertEqual(plan.phase, 'monitor_idle')
        self.assertIs(self.window.consultation, plan)
        plan.phase = 'working'
        self.window.finish_query('complete', 'La reserva ya figura confirmada en el registro local.')
        self.assertIsNone(self.window.consultation)
        self.assertEqual(self.window.query_state.text(), 'Reserva confirmada')

    def test_unexpected_scheduler_exception_recovers(self):
        worker = self.periodic_worker()
        plan = self.window.consultation
        with patch.object(self.window, '_advance_consultation', side_effect=RuntimeError('local')):
            self.window.advance_consultation()
        self.assertEqual(plan.phase, 'monitor_idle')
        worker.requestInterruption.assert_not_called()
        self.assertEqual(plan.recoveries, 1)

    def test_100_delayed_authentication_steps_never_replace_authorized_worker(self):
        worker = self.periodic_worker()
        plan = self.window.consultation
        with patch.object(self.window, 'start_browser') as reconnect:
            for _ in range(100):
                self.advance(120)
                self.assertEqual(plan.phase, 'monitor_idle')
                self.advance(120)
                self.assertEqual(plan.phase, 'refreshing')
                self.window.finish_cycle_refresh('')
                self.window.auth_status.setText('Autenticando')
            reconnect.assert_not_called()
        self.assertIs(self.window.browser, worker)
        self.assertEqual(worker.request_cycle_refresh.call_count, 100)
        worker.requestInterruption.assert_not_called()
        self.assertEqual(plan.recoveries, 100)

    def test_waiting_for_browser_permission_is_not_interrupted_by_page_watchdog(self):
        worker = self.periodic_worker()
        plan = self.window.consultation
        plan.phase = 'ready'
        self.window.browser_status.setText('Esperando autorización en Brave…')
        self.advance(300)
        worker.requestInterruption.assert_not_called()
        self.assertIs(self.window.consultation, plan)
        self.assertEqual(plan.recoveries, 0)

    def test_stop_during_booking_never_revives_watch_on_failure(self):
        worker = self.periodic_worker()
        plan = self.window.consultation
        plan.slot_found = True
        self.window.stop_browser()
        self.assertTrue(plan.stop_requested)
        worker.requestInterruption.assert_called_once()
        result = Result(ACCOUNT.alias, ACCOUNT.establishment, ACCOUNT.service, ACCOUNT.specialty,
                        self.now, self.now, self.now, (('01/06/2031', '08:15', 'A1', '1', 'Prueba'),),
                        (('Total', 1.0),), booking=Outcome('Intervención necesaria', 'Resultado pendiente'))
        self.window.finish_query('ok', result)
        self.assertIsNone(self.window.consultation)
        self.assertTrue(plan.watch.stopped)

    def test_establishment_stays_unapproved_but_is_rechecked(self):
        worker = self.periodic_worker()
        plan = self.window.consultation
        self.window.finish_query('establishment', '456 - OTRO EBAIS')
        self.assertEqual(plan.phase, 'establishment')
        self.assertEqual(self.window.selected().establishment, ACCOUNT.establishment)
        self.advance(119)
        worker.request_cycle_refresh.assert_not_called()
        self.advance(1)
        worker.request_cycle_refresh.assert_called_once_with(ACCOUNT.key)


if __name__ == "__main__":
    main()

