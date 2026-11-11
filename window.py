"""Ventana principal de EDUS Bot."""

import logging
import winsound
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from sqlite3 import Error as DatabaseError
from time import monotonic

from PySide6.QtCore import QIODevice, QRectF, QSaveFile, QSize, Qt, QTimer, Slot
from PySide6.QtGui import QColor, QImageReader, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QStyle, QSystemTrayIcon,
    QVBoxLayout, QWidget, QHeaderView, QTableWidget, QTableWidgetItem,
)

from accounts import Account, Accounts, IDENTIFICATION_TYPES
from activity import costa_rica_now, create_logger
from booking import Outcome, PREFERENCES
from browser import (
    AUTHENTICATED, BAD_CAPTCHA, BAD_CREDENTIALS, EXPIRED, SIGNED_OUT, SOLVING_CAPTCHA,
    BrowserThread, UNKNOWN, WAITING,
)
from credentials import CredentialStore
from consultation_history import ConsultationHistory
from monitoring import (
    MAX_DURATION, MAX_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS,
    REAL_WATCH_INTERVAL_SECONDS, WatchCycles,
    CYCLE_STAGE_TIMEOUT_SECONDS,
)
from query import Result
from settings import Settings, data_directory, load_settings, save_settings


def button(label, callback):
    control = QPushButton(label)
    control.clicked.connect(callback)
    return control


def row(*widgets):
    layout = QHBoxLayout()
    for widget in widgets:
        layout.addWidget(widget)
    return layout


def plain_label(text=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class BackgroundScrollArea(QScrollArea):
    def __init__(self):
        super().__init__()
        self.background = QPixmap()

    def set_background(self, pixmap):
        self.background = pixmap
        self.widget().setAutoFillBackground(pixmap.isNull())
        self.widget().setStyleSheet("" if pixmap.isNull() else
                                    "QGroupBox { background-color: rgba(33, 33, 33, 218); }")
        self.viewport().update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.background.isNull():
            return
        viewport = self.viewport()
        if viewport.size().isEmpty():
            return
        painter = QPainter(viewport)
        painter.fillRect(viewport.rect(), QColor("#212121"))
        painter.setOpacity(0.55)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        width, height = self.background.width(), self.background.height()
        scale = max(viewport.width() / width, viewport.height() / height)
        visible_width, visible_height = viewport.width() / scale, viewport.height() / scale
        crop = QRectF((width - visible_width) / 2, (height - visible_height) / 2, visible_width, visible_height)
        painter.drawPixmap(QRectF(viewport.rect()), self.background, crop)
        painter.end()


class AccountDialog(QDialog):
    def __init__(self, parent, account=None, identification="", temporary=False):
        super().__init__(parent)
        self.setWindowTitle("Acceso de una vez" if temporary else "Editar cuenta" if account else "Añadir cuenta")
        self.resize(400, 170)
        form = QFormLayout(self)
        self.alias = QLineEdit(account.alias if account else "")
        self.kind = QComboBox()
        for key, label in IDENTIFICATION_TYPES.items():
            self.kind.addItem(label, key)
        if account:
            self.kind.setCurrentIndex(self.kind.findData(account.kind))
        self.identification = QLineEdit(identification)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        if account:
            self.password.setPlaceholderText("Deja vacío para conservar la contraseña")
        if not temporary:
            form.addRow("Alias", self.alias)
        form.addRow("Tipo de identificación", self.kind)
        form.addRow("Identificación", self.identification)
        form.addRow("Contraseña", self.password)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Preparar acceso" if temporary else "Guardar cuenta"
        )
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancelar")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


@dataclass
class Consultation:
    account: str
    starts: datetime
    watch: WatchCycles
    auto_booking: bool
    simulation: bool
    reload_each_cycle: bool = False
    phase: str = "ready"
    connection_requested: bool = False
    login_requested: bool = False
    stop_requested: bool = False
    slot_found: bool = False
    reconnect_at: float = 0
    recoveries: int = 0
    stage_at: float = 0
    last_failure: str = ""


class MainWindow(QMainWindow):
    def __init__(self, directory=None, vault=None):
        super().__init__()
        directory = directory if directory is not None else data_directory()
        self.settings_path = directory / "settings.json"
        self.background_path = directory / "background.png"
        self.history = ConsultationHistory(directory / "consultations.sqlite3")
        self.browser = None
        self.closing = False
        self.verified_account = ""
        self.view = None
        self.consultation = None
        self.slot_dialog = None
        self.vault = vault if vault is not None else CredentialStore()
        self.accounts = Accounts(self.settings_path, self.vault)
        self.logger = logging.getLogger("edus_bot")
        self.build_interface()
        self.refresh_background()
        try:
            self.logger = create_logger(directory)
        except OSError:
            self.record(logging.ERROR, "No se pudo abrir el registro local.")
        try:
            self.settings = load_settings(self.settings_path)
        except (OSError, ValueError):
            self.settings = Settings()
            self.record(logging.ERROR, "No se pudo leer la configuración. Se cargaron valores predeterminados.")
        else:
            try:
                self.settings = self.accounts.migrate(self.settings)
            except Exception:
                self.record(logging.ERROR, "No se pudo incorporar la cuenta guardada anteriormente.")
        self.clock_24h.setChecked(self.settings.clock_24h)
        self.watch_interval.setValue(self.settings.watch_interval)
        self.reload_accounts(self.settings.default_account)
        self.update_clock()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.on_tick)
        self.timer.start(1000)
        self.record(logging.INFO, "EDUS Bot iniciado.")

    def build_interface(self):
        self.setWindowTitle("EDUS Bot")
        self.resize(580, 690)
        self.setMinimumWidth(440)
        root = QWidget()
        scroll = BackgroundScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(root)
        self.setCentralWidget(scroll)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(8)

        title = QLabel("EDUS Bot")
        title.setObjectName("title")
        self.change_background_button = button("Cambiar fondo…", self.choose_background)
        self.reset_background_button = button("Quitar fondo", self.remove_background)
        header = QHBoxLayout()
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.change_background_button)
        header.addWidget(self.reset_background_button)
        layout.addLayout(header)
        subtitle = QLabel("Fase 9 · Vigilancia y reserva")
        subtitle.setObjectName("subtitle")
        layout.addWidget(subtitle)

        plan = QGroupBox("Inicio")
        plan_form = QFormLayout(plan)
        self.advanced = QWidget()
        advanced = QVBoxLayout(self.advanced)
        advanced.setContentsMargins(0, 0, 0, 0)
        account = QGroupBox("Administración de cuentas")
        account_form = QFormLayout(account)
        self.account_selector = QComboBox()
        self.account_selector.currentIndexChanged.connect(self.account_changed)
        plan_form.addRow("Cuenta EDUS", self.account_selector)
        self.edit_account = button("Editar", lambda: self.edit_selected(False))
        self.delete_account = button("Eliminar", self.delete_selected)
        self.default_account = button("Hacer predeterminada", self.make_default)
        account_form.addRow(row(
            button("Añadir cuenta", lambda: self.edit_selected(True)),
            self.edit_account, self.delete_account,
        ))
        self.temporary = button("Acceso de una vez", self.temporary_login)
        account_form.addRow(row(self.default_account, self.temporary))
        advanced.addWidget(account)

        status = QGroupBox("Estado")
        form = QFormLayout(status)
        self.browser_status = plain_label("Brave desconectado")
        self.browser_status.setObjectName("connection")
        self.auth_status = plain_label(UNKNOWN)
        self.account_status = plain_label()
        self.clock = QLabel()
        form.addRow("Navegador", self.browser_status)
        form.addRow("Sesión EDUS", self.auth_status)
        form.addRow("Cuenta en sesión", self.account_status)
        form.addRow("Hora de Costa Rica", self.clock)
        advanced.addWidget(status)

        self.open_portal = button("Abrir EDUS", self.open_edus)
        self.open_portal.setObjectName("primary")
        self.reconnect = button("Reconectar Brave", self.probe_brave)
        self.login = button("Iniciar sesión", self.login_edus)
        self.start = button("INICIAR EDUS BOT", self.start_edus)
        self.start.setObjectName("primary")
        self.stop = button("DETENER", self.stop_browser)
        self.stop.setEnabled(False)
        self.stop.setToolTip("Desconecta EDUS Bot y deja Brave abierto.")
        manual = QGroupBox("Pruebas manuales")
        manual_layout = QVBoxLayout(manual)
        manual_layout.addLayout(row(self.open_portal, self.login, self.reconnect))
        advanced.addWidget(manual)

        appointment = QGroupBox("Disponibilidad")
        form = self.appointment_form = QFormLayout(appointment)
        self.appointment = button("Abrir consulta manual", self.open_appointment)
        self.query_now = button("Consultar ahora", self.consult_now)
        self.query_preferences = button("Preferencias de consulta…", self.edit_query_preferences)
        self.query_config = plain_label()
        self.query_state = plain_label("Sin iniciar")
        self.run_status = plain_label()
        self.run_status.setObjectName("subtitle")
        self.query_summary = plain_label()
        self.slots = QTableWidget(0, 5)
        self.slots.setHorizontalHeaderLabels(("Fecha", "Hora", "N.º de cita", "Consultorio", "Funcionario"))
        self.slots.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.slots.verticalHeader().hide()
        self.slots.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.slots.setMinimumHeight(125)
        self.slots.hide()
        self.establishment = plain_label()
        self.confirm = button("Confirmar establecimiento", self.confirm_establishment)
        self.service = QComboBox()
        self.specialty = QComboBox()
        self.service.activated.connect(lambda index: self.choose_option(0, index))
        self.specialty.activated.connect(lambda index: self.choose_option(1, index))
        self.result = plain_label("Abre la consulta con una cuenta verificada.")
        manual_layout.addLayout(row(self.query_now, self.appointment))
        manual_layout.addWidget(self.query_preferences)
        self.history_button = button("Historial de la cuenta…", self.show_history)
        manual_layout.addWidget(self.history_button)
        form.addRow("Filtros", self.query_config)
        form.addRow("Establecimiento", self.establishment)
        form.addRow(self.confirm)
        form.addRow("Servicio", self.service)
        form.addRow("Especialidad", self.specialty)
        form.addRow(self.result)
        form.addRow(self.query_summary)
        form.addRow(self.slots)

        reservation = QGroupBox("Reserva automática")
        reservation_form = QFormLayout(reservation)
        self.booking_preference = QComboBox()
        for key, label in PREFERENCES.items():
            self.booking_preference.addItem(label, key)
        self.booking_preference.activated.connect(self.save_booking_preference)
        self.booking_context = plain_label()
        self.booking_status = plain_label("Sin iniciar")
        self.booking_result = plain_label()
        self.authorization_note = plain_label()
        self.booking_brave = button("Ver en Brave", self.open_edus)
        reservation_form.addRow("Preferencia", self.booking_preference)
        reservation_form.addRow("Estado", self.booking_status)
        reservation_form.addRow(self.booking_brave)
        advanced.addWidget(reservation)
        form.addRow(self.booking_result)

        self.config = QGroupBox("Opciones de vigilancia")
        form = QFormLayout(self.config)
        self.clock_24h = QCheckBox("Reloj de 24 horas")
        form.addRow(self.clock_24h)
        self.clock_24h.clicked.connect(self.save_configuration)
        form.addRow(row(
            button("Opciones de Brave…", self.brave_options),
        ))
        advanced.addWidget(self.config)
        self.watch_environment = QComboBox()
        self.watch_environment.addItem("EDUS real · Consulta única", "production")
        self.watch_environment.addItem("EDUS real · Recargar cada 2 minutos", "periodic")
        self.watch_environment.addItem("Simulación local", "simulation")
        self.watch_environment.currentIndexChanged.connect(self.environment_changed)
        self.watch_interval = QSpinBox()
        self.watch_interval.setRange(MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS)
        self.watch_interval.setSuffix(" s")
        self.watch_interval.valueChanged.connect(self.update_controls)
        self.watch_context = plain_label()
        self.requested_rate = plain_label()
        self.watch_rate = plain_label()
        self.watch_stats = plain_label()
        self.watch_stats.setMinimumWidth(200)
        form.addRow("Entorno", self.watch_environment)
        form.addRow("Intervalo solicitado", self.watch_interval)
        plan_form.addRow("Entorno", self.watch_context)
        plan_form.addRow("Intervalo solicitado", self.requested_rate)
        plan_form.addRow("Frecuencia efectiva", self.watch_rate)
        plan_form.addRow(self.watch_stats)
        plan_form.addRow("Cuenta y criterios", self.booking_context)
        plan_form.addRow(self.authorization_note)
        plan_form.addRow(row(self.start, self.stop))
        plan_form.addRow("Estado", self.query_state)
        plan_form.addRow(self.run_status)
        layout.addWidget(plan)
        layout.addWidget(appointment)
        config_toggle = button("Opciones avanzadas", lambda: self.advanced.setVisible(self.advanced.isHidden()))
        self.advanced.hide()
        layout.addWidget(config_toggle)
        layout.addWidget(self.advanced)

        layout.addWidget(QLabel("Registro de actividad"))
        self.activity = QPlainTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setMinimumHeight(85)
        self.activity.setMaximumBlockCount(300)
        self.activity.setAccessibleName("Registro de actividad")
        layout.addWidget(self.activity, 1)
        self.tray = QSystemTrayIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_MessageBoxInformation
        ), self)
        self.tray.setToolTip("EDUS Bot")

    def refresh_background(self):
        pixmap = QPixmap(str(self.background_path)) if self.background_path.is_file() else QPixmap()
        self.centralWidget().set_background(pixmap)
        self.reset_background_button.setVisible(not pixmap.isNull())

    def choose_background(self):
        formats = sorted({bytes(fmt).decode("ascii") for fmt in QImageReader.supportedImageFormats()})
        filters = "Imágenes (" + " ".join(f"*.{fmt}" for fmt in formats) + ");;Todos los archivos (*)"
        source, _ = QFileDialog.getOpenFileName(self, "Elegir imagen de fondo", "", filters)
        if not source:
            return
        reader = QImageReader(source)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and max(size.width(), size.height()) > 2560:
            reader.setScaledSize(size.scaled(QSize(2560, 2560), Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        if image.isNull():
            QMessageBox.warning(self, "Cambiar fondo", "Elige un archivo de imagen compatible.")
            return
        try:
            self.background_path.parent.mkdir(parents=True, exist_ok=True)
            destination = QSaveFile(str(self.background_path))
            if not destination.open(QIODevice.OpenModeFlag.WriteOnly) or not image.save(destination, "PNG") or not destination.commit():
                raise OSError(destination.errorString())
        except OSError:
            QMessageBox.warning(self, "Cambiar fondo", "No se pudo guardar la imagen de fondo.")
            return
        self.refresh_background()

    def remove_background(self):
        try:
            self.background_path.unlink(missing_ok=True)
        except OSError:
            QMessageBox.warning(self, "Quitar fondo", "No se pudo quitar la imagen de fondo.")
            return
        self.refresh_background()

    def environment_changed(self):
        self.watch_stats.clear()
        self.update_controls()

    def on_tick(self):
        self.update_clock()
        if self.consultation is not None:
            self.advance_consultation()

    def update_clock(self):
        now = costa_rica_now()
        if self.settings.clock_24h:
            display = now.strftime("%H:%M:%S")
        else:
            period = "a. m." if now.hour < 12 else "p. m."
            display = f"{now.hour % 12 or 12}:{now:%M:%S} {period}"
        self.clock.setText(display)

    def save_preferences(self, **values):
        try:
            settings = replace(self.settings, **values)
            save_settings(self.settings_path, settings)
        except (OSError, ValueError):
            self.record(logging.ERROR, "No se pudo guardar la configuración.")
            return False
        self.settings = settings
        return True

    def save_configuration(self):
        if self.save_preferences(clock_24h=self.clock_24h.isChecked()):
            self.update_clock()

    def brave_options(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Opciones de Brave")
        dialog.resize(420, 180)
        layout = QVBoxLayout(dialog)
        layout.addWidget(plain_label(
            "EDUS Bot busca Brave automáticamente y se conecta si ya ofrece CDP local. "
            "Si dice Requiere configuración, consulta la sección de Brave del README."
        ))
        current = plain_label(self.settings.brave_path or "Detección automática")
        layout.addWidget(current)

        def choose():
            path, _ = QFileDialog.getOpenFileName(
                dialog, "Seleccionar Brave", self.settings.brave_path, "Brave (brave.exe)",
                options=QFileDialog.Option.DontUseNativeDialog,
            )
            if path and Path(path).name.lower() == "brave.exe" and self.save_preferences(brave_path=path):
                current.setText(path)

        def reset():
            if self.save_preferences(brave_path=""):
                current.setText("Detección automática")

        layout.addLayout(row(button("Seleccionar Brave…", choose), button("Detectar automáticamente", reset)))
        layout.addWidget(button("Cerrar", dialog.accept))
        dialog.exec()

    @Slot(int, str)
    def record(self, level, message):
        self.logger.log(level, message)
        color = "#ffb0ad" if level >= logging.ERROR else "#ebcb91" if level >= logging.WARNING else "#f5f5f5"
        style = self.activity.currentCharFormat()
        style.setForeground(QColor(color))
        cursor = self.activity.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        if not self.activity.document().isEmpty():
            cursor.insertBlock()
        cursor.insertText(f"{costa_rica_now():%H:%M:%S}  {message}", style)
        self.activity.setTextCursor(cursor)

    def selected(self):
        key = self.account_selector.currentData()
        return next((Account(**item) for item in self.settings.accounts if item["key"] == key), None)

    def reload_accounts(self, key):
        self.account_selector.blockSignals(True)
        self.account_selector.clear()
        for item in self.settings.accounts:
            suffix = " · predeterminada" if item["key"] == self.settings.default_account else ""
            self.account_selector.addItem(item["alias"] + suffix, item["key"])
        if key:
            self.account_selector.setCurrentIndex(self.account_selector.findData(key))
        self.account_selector.blockSignals(False)
        self.account_changed()

    def account_changed(self):
        if self.consultation is not None:
            self.stop_browser()
        if self.slot_dialog is not None:
            self.slot_dialog.close()
        self.query_state.setText("Sin iniciar")
        self.query_summary.clear()
        self.watch_stats.clear()
        self.slots.setRowCount(0)
        self.slots.hide()
        self.booking_status.setText("Sin iniciar")
        self.booking_result.clear()
        self.render_availability()
        self.update_controls()

    def show_history(self):
        account = self.selected()
        if account is None:
            return
        try:
            entries = self.history.recent(account.key)
        except (DatabaseError, OSError):
            self.record(logging.ERROR, "No se pudo leer el historial de la cuenta.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Historial de consultas")
        dialog.resize(620, 380)
        layout = QVBoxLayout(dialog)
        layout.addWidget(plain_label(account.alias))
        history = QPlainTextEdit()
        history.setReadOnly(True)
        history.setPlainText("\n".join(
            f"{datetime.fromisoformat(when):%d/%m/%Y %H:%M:%S} · {slots} cupos\n"
            f"{place} · {service} · {specialty}\n"
            for when, place, service, specialty, slots in entries
        ) or "Todavía no hay consultas verificadas para esta cuenta.")
        layout.addWidget(history)
        layout.addWidget(button("Cerrar", dialog.accept))
        dialog.exec()

    def edit_selected(self, adding):
        account = None if adding else self.selected()
        if self.consultation is not None or (not adding and account is None):
            return
        try:
            previous = self.vault.load(account.key) if account else None
        except Exception:
            self.record(logging.ERROR, "No se pudieron leer las credenciales de Windows.")
            return
        dialog = AccountDialog(self, account, previous[0] if previous else "")
        while dialog.exec():
            try:
                self.settings, key = self.accounts.save(
                    self.settings, account.key if account else "", dialog.alias.text(),
                    dialog.kind.currentData(), dialog.identification.text().strip(), dialog.password.text(),
                )
            except ValueError:
                QMessageBox.warning(self, "Cuenta EDUS", "Completa el alias, la identificación y la contraseña.")
                continue
            except Exception:
                self.record(logging.ERROR, "No se pudo guardar la cuenta.")
                break
            if account and self.browser is not None:
                self.stop_browser()
            self.reload_accounts(key)
            self.record(logging.INFO, "Cuenta guardada.")
            break
        dialog.password.clear()

    def delete_selected(self):
        account = self.selected()
        if account is None or QMessageBox.question(
            self, "Eliminar cuenta", "¿Eliminar esta cuenta y sus credenciales de EDUS Bot?"
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.settings = self.accounts.delete(self.settings, account.key)
        except Exception:
            self.record(logging.ERROR, "No se pudo eliminar la cuenta.")
            return
        if self.browser is not None:
            self.stop_browser()
        self.reload_accounts(self.settings.default_account)
        self.record(logging.INFO, "Cuenta eliminada.")

    def make_default(self):
        account = self.selected()
        if account and self.save_preferences(default_account=account.key):
            self.reload_accounts(account.key)

    def login_edus(self):
        account = self.selected()
        if not account or not self.login.isEnabled():
            return
        self.queue_login(account)

    def queue_login(self, account):
        try:
            credentials = self.vault.load(account.key)
        except Exception:
            self.record(logging.ERROR, "No se pudieron recuperar las credenciales de Windows.")
            return False
        if credentials is None:
            self.record(logging.WARNING, "Edita la cuenta y guarda sus credenciales.")
            return False
        self.browser.request_login((account.key, account.kind, *credentials))
        return True

    def temporary_login(self):
        if not self.temporary.isEnabled():
            return
        dialog = AccountDialog(self, temporary=True)
        if dialog.exec() and self.temporary.isEnabled():
            identity, password = dialog.identification.text().strip(), dialog.password.text()
            if identity and password:
                self.browser.request_login(("temporary", dialog.kind.currentData(), identity, password))
            else:
                self.record(logging.WARNING, "Completa la identificación y la contraseña.")
        dialog.password.clear()

    def probe_brave(self):
        if self.browser is not None:
            self.record(logging.INFO, "Brave ya está conectado o se está comprobando.")
        elif not self.closing:
            self.start_browser("probe")

    def open_edus(self):
        if self.browser is None:
            self.start_browser("open")
        elif self.browser_status.text() == "Brave conectado":
            self.browser.request_focus()

    def start_browser(self, action):
        if self.browser is not None or self.closing:
            return
        self.browser = BrowserThread(
            self.settings, action, parent=self,
            booking_path=self.settings_path.with_name("bookings.sqlite3"),
            session_path=self.settings_path.with_name("session-link.json"), load_credentials=self.vault.load,
        )
        self.browser.status.connect(self.update_browser_status)
        self.browser.authentication.connect(self.update_authentication)
        self.browser.verified.connect(self.update_verified)
        self.browser.availability.connect(self.update_availability)
        self.browser.query_progress.connect(self.show_query_progress)
        self.browser.query_finished.connect(self.finish_query)
        self.browser.cycle_refreshed.connect(self.finish_cycle_refresh)
        self.browser.slot_found.connect(self.show_slot_window)
        self.browser.activity.connect(self.record)
        self.browser.finished.connect(self.browser_finished)
        self.browser_status.setText("Comprobando Brave…")
        self.auth_status.setText(UNKNOWN)
        self.stop.setEnabled(True)
        self.update_controls()
        self.browser.start()

    def update_controls(self):
        account = self.selected()
        connected = bool(
            self.browser is not None and self.stop.isEnabled() and self.browser_status.text() == "Brave conectado"
        )
        signed_in = self.auth_status.text() == AUTHENTICATED
        verified = bool(connected and signed_in and account and account.key == self.verified_account)
        self.open_portal.setEnabled(not self.closing and (self.browser is None or connected))
        self.reconnect.setEnabled(not self.closing and self.browser is None)
        can_login = connected and not signed_in and self.auth_status.text() in (
            SIGNED_OUT, BAD_CREDENTIALS, EXPIRED, WAITING, BAD_CAPTCHA,
        )
        self.login.setEnabled(bool(can_login and account))
        self.temporary.setEnabled(can_login)
        active = self.consultation is not None
        if active and self.consultation.simulation:
            for control in (self.open_portal, self.reconnect, self.login, self.temporary):
                control.setEnabled(False)
        simulation_selected = (
            self.consultation.simulation if active else self.watch_environment.currentData() == "simulation"
        )
        interval = self.consultation.watch.interval if active else self.effective_interval()
        self.watch_context.setText(
            "Simulación local" if simulation_selected else
            "EDUS real · Vigilancia periódica" if interval is not None else "EDUS real · Consulta única"
        )
        if simulation_selected:
            self.authorization_note.setText("La simulación usa cupos y reservas ficticios.")
        else:
            action = "recargar EDUS cada dos minutos y " if interval is not None else ""
            self.authorization_note.setText(
                f"Al iniciar autorizas a EDUS Bot a {action}confirmar automáticamente "
                "una cita válida para esta cuenta y esta ejecución."
            )
        requested = self.watch_interval.value() if simulation_selected else REAL_WATCH_INTERVAL_SECONDS
        self.requested_rate.setText(f"{requested} s")
        self.watch_rate.setText(
            f"Cada {interval} s" if simulation_selected and interval is not None else
            "2 minutos después de cada consulta sin cupos" if interval is not None else "Consulta única"
        )
        self.watch_environment.setEnabled(not active)
        self.watch_interval.setEnabled(simulation_selected and not active)
        self.account_selector.setEnabled(not active)
        self.edit_account.setEnabled(account is not None and not active)
        self.delete_account.setEnabled(account is not None and not active)
        self.default_account.setEnabled(bool(account and account.key != self.settings.default_account and not active))
        self.temporary.setEnabled(can_login and not active)
        self.query_now.setEnabled(bool(account and not active and not self.closing))
        self.start.setEnabled(self.query_now.isEnabled())
        self.history_button.setEnabled(bool(account and not active))
        if active:
            if self.consultation.simulation:
                self.run_status.setText("Simulación local · datos ficticios")
            else:
                self.run_status.setText(
                    "Reserva autorizada para esta ejecución" if self.consultation.auto_booking else
                    "Consulta sin envío automático"
                )
        else:
            self.run_status.clear()
        self.query_preferences.setEnabled(bool(account and not active))
        self.appointment.setEnabled(verified and not active)
        self.booking_preference.setEnabled(bool(account and not active))
        self.booking_brave.setEnabled(connected and not active)
        if account:
            self.booking_preference.setCurrentIndex(self.booking_preference.findData(account.booking_preference))
            establishment = account.establishment or "Establecimiento por verificar"
            preference = PREFERENCES[account.booking_preference]
            self.booking_context.setText(f"{account.alias} · {establishment}\n{account.service} · {account.specialty}\n{preference}")
        else:
            self.booking_context.setText("Selecciona una cuenta.")
        if active:
            self.stop.setEnabled(True)
        self.query_config.setText(f"{account.service} · {account.specialty}" if account else "Selecciona una cuenta.")
        if verified:
            message = "Cuenta seleccionada verificada"
        elif signed_in and self.verified_account:
            message = "Otra cuenta está activa. Cierra sesión en EDUS antes de cambiar."
        elif signed_in:
            message = "Identidad sin verificar. Cierra sesión en EDUS e inicia desde el bot."
        else:
            message = "Pendiente de inicio de sesión"
        self.account_status.setText(message)

    @Slot(str)
    def update_browser_status(self, status):
        if self.stop.isEnabled():
            self.browser_status.setText(status)
            if self.consultation and status == "Reconectando Brave…":
                self.consultation.ready = False
                self.query_state.setText("Reconectando Brave · Se está verificando la pestaña…")
            self.update_controls()

    @Slot(str)
    def update_authentication(self, state):
        if not self.stop.isEnabled():
            return
        previous = self.auth_status.text()
        self.auth_status.setText(state)
        if (self.consultation and self.consultation.reload_each_cycle
                and state in (EXPIRED, SIGNED_OUT) and state != previous):
            self.consultation.login_requested = False
        self.update_controls()
        if state == previous:
            return
        if state == WAITING:
            message = "Completa el CAPTCHA y pulsa Ingresar en Brave."
            self.record(logging.INFO, message)
            self.notify(message)
        elif state == BAD_CAPTCHA:
            self.notify("EDUS rechazó el CAPTCHA. Escribe el código que ahora muestra Brave.")
        elif state == EXPIRED or (previous == AUTHENTICATED and state == SIGNED_OUT):
            self.notify("La sesión de EDUS terminó. Completa el acceso en Brave cuando se solicite.")
        elif state == BAD_CREDENTIALS:
            self.notify("EDUS rechazó las credenciales. Revisa la cuenta en Brave o actualízala en EDUS Bot.")

    @Slot(str, str, str, str)
    def show_slot_window(self, date, hour, establishment, status):
        plan = self.consultation
        if plan is None or plan.stop_requested:
            return
        plan.slot_found = True
        self.query_state.setText("Cupo detectado")
        self.render_watch(plan)
        if self.slot_dialog is None:
            dialog = QDialog(self)
            dialog.setWindowTitle("Cupo encontrado en EDUS")
            dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
            screen = self.screen().availableGeometry()
            dialog.resize(min(900, int(screen.width() * 0.85)), min(480, int(screen.height() * 0.8)))
            dialog.finished.connect(self.stop_slot_alarm)
            layout = QVBoxLayout(dialog)
            layout.setContentsMargins(34, 28, 34, 28)
            self.slot_heading = plain_label()
            self.slot_establishment = plain_label()
            self.slot_status = plain_label()
            for label, size in ((self.slot_heading, 30), (self.slot_establishment, 18), (self.slot_status, 16)):
                font = label.font()
                font.setPointSize(size)
                label.setFont(font)
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.slot_heading.setStyleSheet("font-weight: bold;")
            layout.addStretch()
            layout.addWidget(self.slot_heading)
            layout.addWidget(self.slot_establishment)
            layout.addWidget(self.slot_status)
            layout.addStretch()
            self.silence_alarm = button("Silenciar alarma", self.stop_slot_alarm)
            layout.addLayout(row(self.silence_alarm, button("Cerrar aviso", dialog.close)))
            self.slot_dialog = dialog
        heading = "Cita ficticia" if self.consultation.simulation else "Cita disponible"
        self.slot_heading.setText(f"{heading} · {date} a las {hour}")
        self.slot_dialog.setWindowTitle("Cupo simulado" if plan.simulation else "Cupo encontrado en EDUS")
        self.slot_establishment.setText(establishment)
        self.slot_status.setText(status)
        self.slot_dialog.show()
        self.slot_dialog.raise_()
        self.slot_dialog.activateWindow()
        self.silence_alarm.setEnabled(True)
        try:
            winsound.PlaySound(str(Path(__file__).with_name("diana.wav")),
                               winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP | winsound.SND_NODEFAULT)
        except RuntimeError:
            self.silence_alarm.setEnabled(False)
            self.record(logging.WARNING, "No se pudo reproducir el sonido de alerta.")
        prefix = "Cupo simulado" if self.consultation.simulation else "Cupo encontrado"
        self.record(logging.INFO, f"{prefix} para {date} a las {hour}.")
        self.notify(f"{prefix} para {date} a las {hour}.")

    def stop_slot_alarm(self):
        try:
            winsound.PlaySound(None, 0)
        except RuntimeError:
            pass
        self.silence_alarm.setEnabled(False)

    @Slot(str)
    def update_verified(self, account):
        self.verified_account = account if self.stop.isEnabled() else ""
        self.update_controls()

    @Slot(object)
    def update_availability(self, view):
        self.view = view if self.stop.isEnabled() else None
        self.render_availability()

    def render_availability(self):
        account = self.selected()
        view = self.view if account and self.view and self.view[0] == account.key else None
        observed, result, options, selected = view[1:] if view else (
            "", "Abre la consulta con una cuenta verificada.", (), (),
        )
        self.establishment.setText(observed or (account.establishment if account else "") or "Por verificar en EDUS")
        can_confirm = bool(view and observed and account.establishment != observed)
        self.appointment_form.setRowVisible(self.confirm, can_confirm)
        self.confirm.setEnabled(can_confirm)
        self.result.setText(result)
        self.appointment_form.setRowVisible(self.result, view is not None)
        for index, control in enumerate((self.service, self.specialty)):
            values = options[index] if options else ()
            control.clear()
            control.addItem("Selecciona…", "")
            for value in values:
                control.addItem(value, value)
            if selected:
                control.setCurrentIndex(max(0, control.findData(selected[index])))
            self.appointment_form.setRowVisible(control, view is not None)
            control.setEnabled(bool(values and self.stop.isEnabled() and self.consultation is None))

    def open_appointment(self):
        account = self.selected()
        if account and self.appointment.isEnabled():
            self.browser.request_appointment(account.key, account.establishment)

    def confirm_establishment(self):
        account = self.selected()
        if not account or not self.confirm.isEnabled():
            return
        observed = self.view[1]
        try:
            self.settings = self.accounts.set_establishment(self.settings, account.key, observed)
        except OSError:
            self.record(logging.ERROR, "No se pudo guardar el establecimiento.")
            return
        self.browser.confirm_establishment(account.key, observed)
        self.render_availability()
        self.update_controls()
        self.record(logging.INFO, "Establecimiento confirmado para la cuenta.")
        if self.consultation and self.consultation.phase == "establishment":
            self.consultation.phase = "ready"
            self.advance_consultation()

    def choose_option(self, index, item):
        account = self.selected()
        control = (self.service, self.specialty)[index]
        value = control.itemData(item)
        if account and value and control.isEnabled() and self.browser:
            self.browser.request_choice(account.key, index, value)

    def save_booking_preference(self, index):
        account = self.selected()
        if account and self.consultation is None:
            try:
                self.settings = self.accounts.update_preferences(
                    self.settings, account.key, booking_preference=self.booking_preference.itemData(index),
                )
            except (OSError, ValueError):
                self.record(logging.ERROR, "No se pudo guardar la preferencia de cita.")
            self.update_controls()

    def edit_query_preferences(self):
        account = self.selected()
        if not account or self.consultation:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Preferencias de consulta")
        form = QFormLayout(dialog)
        service = QLineEdit(account.service)
        specialty = QLineEdit(account.specialty)
        form.addRow(plain_label("Escribe los nombres tal como aparecen en los menús de EDUS."))
        form.addRow("Servicio", service)
        form.addRow("Especialidad", specialty)
        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        controls.accepted.connect(dialog.accept)
        controls.rejected.connect(dialog.reject)
        form.addRow(controls)
        if dialog.exec():
            try:
                self.settings = self.accounts.update_preferences(
                    self.settings, account.key, service=service.text().strip(), specialty=specialty.text().strip(),
                )
            except (OSError, ValueError):
                self.record(logging.ERROR, "Completa Servicio y Especialidad para guardar los filtros.")
            self.update_controls()

    def effective_interval(self):
        environment = self.watch_environment.currentData()
        if environment == "simulation":
            return self.watch_interval.value()
        return REAL_WATCH_INTERVAL_SECONDS if environment == "periodic" else None

    def start_edus(self):
        if not self.start.isEnabled():
            return
        if not self.save_preferences(
            watch_interval=self.watch_interval.value(),
        ):
            self.query_state.setText("Error · No se pudieron guardar las preferencias de vigilancia.")
            return
        self.begin_consultation(
            auto_booking=True,
            interval=self.effective_interval(),
            simulation=self.watch_environment.currentData() == "simulation",
        )

    def consult_now(self):
        self.begin_consultation()

    def begin_consultation(self, *, auto_booking=False, interval=None, simulation=False):
        account = self.selected()
        if account is None or self.consultation is not None or self.closing:
            return
        now = costa_rica_now()
        periodic = interval is not None and not simulation
        end = None if periodic else now + (MAX_DURATION if simulation else timedelta(minutes=10))
        watch = WatchCycles(interval, None if interval is not None else 1, end, monotonic())
        self.consultation = Consultation(account.key, now, watch, auto_booking, simulation, reload_each_cycle=periodic)
        self.consultation.stage_at = monotonic()
        if interval is not None:
            environment = "simulada" if simulation else "de EDUS"
            self.record(logging.INFO, f"Vigilancia {environment} iniciada · intervalo {interval} s.")
        elif auto_booking:
            self.record(logging.INFO, "Consulta única de EDUS.")
        if self.slot_dialog is not None:
            self.slot_dialog.close()
        self.slots.setRowCount(0)
        self.slots.hide()
        self.query_summary.clear()
        self.booking_status.setText("Preparando consulta")
        self.booking_result.clear()
        self.update_controls()
        self.render_availability()
        self.advance_consultation()

    def advance_consultation(self):
        try:
            self._advance_consultation()
        except Exception as error:
            if self.consultation and self.consultation.reload_each_cycle and not self.closing:
                self.retry_consultation(f"Fallo inesperado del ciclo ({type(error).__name__}).")
            else:
                self.end_consultation(f"Error · Fallo del ciclo ({type(error).__name__}).")

    def _advance_consultation(self):
        plan = self.consultation
        if plan is None:
            return
        self.render_watch(plan)
        tick = monotonic()
        if (plan.reload_each_cycle and plan.phase in ("working", "refreshing", "ready")
                and self.browser_status.text() == "Brave conectado"
                and tick - plan.stage_at >= CYCLE_STAGE_TIMEOUT_SECONDS):
            self.retry_consultation("El paso actual no terminó dentro de 120 s; se recuperará con la conexión autorizada.")
            return
        if plan.phase in ("working", "refreshing"):
            return
        now = costa_rica_now()
        tick = monotonic()
        watch = plan.watch
        if watch.end is not None and now >= watch.end:
            self.end_consultation("Consulta finalizada · Se alcanzó el límite de tiempo.")
            return
        account = self.selected()
        if not account or account.key != plan.account:
            self.end_consultation("Error · La cuenta seleccionada cambió.")
            return
        if plan.phase == "establishment":
            if plan.reload_each_cycle and tick >= watch.next_at:
                plan.phase = "monitor_idle"
            else:
                return
        if plan.simulation:
            if watch.begin(now, tick):
                plan.phase = "working"
                self.query_state.setText("VIGILANCIA ACTIVA · Consulta simulada en curso…")
                self.record(logging.INFO, "Consulta simulada iniciada.")
                QTimer.singleShot(0, self, lambda: self.simulate_watch_cycle(plan, account))
            return
        connected = self.browser is not None and self.browser_status.text() == "Brave conectado"
        if not connected:
            if self.browser is None:
                if plan.reload_each_cycle and tick < plan.reconnect_at:
                    self.query_state.setText("VIGILANCIA ACTIVA · Esperando para reconectar Brave…")
                    return
                if plan.connection_requested:
                    if plan.reload_each_cycle:
                        self.retry_consultation("Brave personal sigue sin conexión.")
                        plan.connection_requested = False
                        plan.reconnect_at = plan.watch.next_at
                        return
                    self.end_consultation("Error · Brave personal sin conexión. Revisa brave://inspect/#remote-debugging y el aviso Permitir.")
                    return
                plan.connection_requested = True
                plan.stage_at = tick
                self.start_browser("probe")
            if self.browser_status.text() == "Esperando autorización en Brave…":
                self.query_state.setText("Pulsa Permitir si Brave solicita autorizar la conexión local.")
            else:
                self.query_state.setText("Conectando con Brave…")
            return
        if plan.reload_each_cycle and plan.phase == "monitor_idle":
            if tick >= watch.next_at:
                plan.phase = "refreshing"
                plan.stage_at = tick
                self.query_state.setText("VIGILANCIA ACTIVA · Recargando EDUS…")
                self.browser.request_cycle_refresh(account.key)
            else:
                self.query_state.setText("VIGILANCIA ACTIVA · Esperando el siguiente intento." if plan.last_failure else
                                         "VIGILANCIA ACTIVA · Sin cupos. Esperando la siguiente recarga.")
            return
        state = self.auth_status.text()
        if state != AUTHENTICATED or self.verified_account != plan.account:
            if state in (SIGNED_OUT, EXPIRED) and not plan.login_requested:
                plan.login_requested = True
                if not self.queue_login(account):
                    if plan.reload_each_cycle:
                        self.retry_consultation("No se pudieron leer las credenciales guardadas.")
                        return
                    self.end_consultation("Error · Guarda las credenciales de la cuenta.")
                    return
            messages = {
                AUTHENTICATED: "Intervención necesaria · Cierra la sesión actual y entra con la cuenta seleccionada.",
                SIGNED_OUT: "Iniciando sesión en EDUS…",
                SOLVING_CAPTCHA: "Leyendo el CAPTCHA en Brave…",
                WAITING: "CAPTCHA pendiente · Completa el código en Brave.",
                BAD_CAPTCHA: "CAPTCHA incorrecto · Intenta con el código actual en Brave.",
                BAD_CREDENTIALS: "Credenciales incorrectas · Revisa el acceso en Brave.",
                EXPIRED: "Sesión caducada · Recuperando el ingreso en Brave…",
            }
            self.query_state.setText(messages.get(state, "Verificando el acceso en Brave…"))
            return
        if not watch.begin(now, tick):
            if plan.phase == "monitor_idle":
                self.query_state.setText("VIGILANCIA ACTIVA · Sin cupos. Esperando la siguiente consulta.")
            return
        plan.login_requested = False
        plan.phase = "working"
        plan.stage_at = tick
        self.booking_status.setText("Preparando consulta")
        prefix = "VIGILANCIA ACTIVA" if watch.interval is not None else "Consulta única"
        self.query_state.setText(f"{prefix} · Preparando consulta…")
        self.record(logging.INFO, "Sesión verificada · Consulta iniciada.")
        self.browser.request_query(account, plan.starts if plan.auto_booking else None)

    def simulate_watch_cycle(self, plan, account):
        if self.consultation is not plan:
            return
        now = costa_rica_now()
        rows = ()
        booking = None
        if plan.watch.count == 2:
            date = now.strftime("%d/%m/%Y")
            hour = "08:15"
            rows = ((date, hour, "SIM-1", "1", "Ficticio"),)
            self.show_slot_window(date, hour, "Establecimiento ficticio", "Simulación local · Cupo ficticio")
            booking = Outcome("Reserva simulada", "Simulación completada · Envío ficticio.")
        data = Result(
            account.alias, "Establecimiento ficticio", account.service, account.specialty,
            now, now, now, rows, (("Simulación", 0.0),), booking=booking,
        )
        self.finish_query("ok", data)

    def render_watch(self, plan):
        watch = plan.watch
        now, tick = costa_rica_now(), monotonic()
        next_date = now + timedelta(seconds=max(0, watch.next_at - tick))
        session_ready = plan.reload_each_cycle or plan.simulation or (
            self.browser_status.text() == "Brave conectado"
            and self.auth_status.text() == AUTHENTICATED and self.verified_account == plan.account
        )
        upcoming = "—"
        if (watch.interval is not None and plan.phase in ("monitor_idle", "recovering", "establishment") and session_ready
                and not plan.slot_found and not watch.stopped and (watch.end is None or next_date < watch.end)):
            upcoming = next_date.strftime("%d/%m %H:%M:%S")
        last = watch.last.strftime("%d/%m %H:%M:%S") if watch.last else "—"
        elapsed = str(timedelta(seconds=max(0, int(tick - watch.activated))))
        result = f"{watch.slots} cupos" if watch.slots else "Sin cupos" if watch.last else "—"
        self.watch_stats.setText(
            f"Última: {last} · Próxima: {upcoming}\n"
            f"Consultas completadas: {watch.count} · Cupos: {watch.slots}\n"
            f"Último resultado: {result} · Actividad: {elapsed}\n"
            f"Recuperaciones programadas: {plan.recoveries}"
            + (f"\nÚltimo fallo: {plan.last_failure}" if plan.last_failure else "")
        )

    @Slot(str)
    def show_query_progress(self, message):
        if self.consultation and self.consultation.phase == "working":
            prefix = "VIGILANCIA ACTIVA" if self.consultation.watch.interval is not None else "Consulta única"
            if message in ("Seleccionando cita", "Abriendo detalle de cita", "Confirmando reserva"):
                if message == "Confirmando reserva":
                    self.notify("Reserva de EDUS en proceso.")
                self.booking_status.setText(message)
                self.query_state.setText(message)
                self.record(logging.INFO, message)
            else:
                self.query_state.setText(f"{prefix} · {message}")

    @Slot(str, object)
    def finish_query(self, outcome, data):
        plan = self.consultation
        if plan is None or plan.phase != "working":
            return
        watch = plan.watch
        if plan.stop_requested and outcome != "ok":
            self.end_consultation("Vigilancia detenida.")
            return
        if outcome == "complete":
            self.end_consultation("Reserva confirmada")
            self.booking_result.setText(data)
            return
        if outcome in ("retry", "error") and plan.reload_each_cycle and not plan.stop_requested:
            self.retry_consultation(data)
            return
        if outcome in ("establishment", "session"):
            watch.pause()
        if outcome == "establishment":
            if plan.reload_each_cycle:
                self.retry_consultation("El establecimiento requiere confirmación; se volverá a comprobar.")
            plan.phase = "establishment"
            self.booking_status.setText("Intervención necesaria")
            self.query_state.setText("Intervención necesaria · Confirma el establecimiento.")
            self.notify("Confirma el establecimiento de la cuenta antes de continuar.")
            return
        if outcome == "session":
            if plan.reload_each_cycle:
                self.retry_consultation(data)
                return
            plan.phase = "ready"
            plan.login_requested = False
            self.notify(data)
            return
        if outcome != "ok":
            self.end_consultation("Error · " + data)
            return
        self.record(logging.INFO, "Duración · " + " | ".join(
            f"{stage} {elapsed:.2f} s" for stage, elapsed in data.timings
        ))
        self.record(logging.INFO, f"Consulta terminada · {len(data.rows)} cupos visibles.")
        if not plan.simulation:
            try:
                self.history.record(plan.account, data)
            except (DatabaseError, OSError):
                self.record(logging.ERROR, "No se pudo guardar la consulta en el historial de la cuenta.")
        self.query_summary.setText(
            f"{data.alias} · {data.establishment} · {data.service} · {data.specialty}\n"
            f"Última consulta {data.timestamp:%d/%m/%Y %H:%M:%S} · {data.duration:.2f} s · {len(data.rows)} cupos visibles"
        )
        self.slots.setRowCount(len(data.rows))
        for index, values in enumerate(data.rows):
            for column, value in enumerate(values):
                self.slots.setItem(index, column, QTableWidgetItem(value))
        self.slots.setVisible(bool(data.rows))
        watch.finish(data.timestamp, len(data.rows), data.started_tick,
                     finished_tick=monotonic() if plan.reload_each_cycle else None)
        if plan.stop_requested and data.booking is None:
            self.end_consultation("Vigilancia detenida.")
            return
        if not data.rows and watch.interval is not None:
            plan.last_failure = ""
            now = costa_rica_now()
            self.record(logging.INFO, "Vigilancia · Sin cupos.")
            plan.phase = "monitor_idle"
            next_action = "recarga" if plan.reload_each_cycle else "consulta"
            self.query_state.setText(f"VIGILANCIA ACTIVA · Sin cupos. Esperando la siguiente {next_action}.")
            delay = max(0, watch.next_at - monotonic())
            if watch.end is None or now + timedelta(seconds=delay) < watch.end:
                self.record(logging.INFO, f"Próxima {next_action} programada para {(now + timedelta(seconds=delay)):%H:%M:%S}.")
            self.render_watch(plan)
            return
        booking_state = data.booking.state if data.booking else ""
        if plan.stop_requested and booking_state != "Reserva confirmada":
            self.end_consultation("Vigilancia detenida.")
            return
        if plan.reload_each_cycle and booking_state != "Reserva confirmada":
            reason = data.booking.message if data.booking else "Cupo detectado sin reserva confirmada."
            if self.slot_dialog is not None:
                self.slot_status.setText(reason)
            self.retry_consultation(reason)
            return
        status = booking_state if booking_state in ("Reserva confirmada", "Reserva simulada", "Error", "Intervención necesaria") and plan.auto_booking else (
            ("Cupo simulado" if plan.simulation else "Cupos disponibles") if data.rows else "Sin cupos"
        )
        self.end_consultation(status)
        if data.rows:
            self.booking_status.setText(data.booking.state if data.booking else "Cupo encontrado")
            if data.booking:
                self.booking_result.setText(data.booking.message)
                if self.slot_dialog is not None:
                    self.slot_status.setText(data.booking.message)
                if plan.auto_booking:
                    self.notify(data.booking.message)

    @Slot(str)
    def finish_cycle_refresh(self, error):
        plan = self.consultation
        if plan is None or plan.phase != "refreshing":
            return
        if error:
            if plan.reload_each_cycle and not plan.stop_requested:
                self.retry_consultation(error)
                return
            self.end_consultation("Intervención necesaria · " + error)
            return
        plan.phase = "ready"
        plan.stage_at = monotonic()
        plan.login_requested = False
        self.verified_account = ""
        self.auth_status.setText(SIGNED_OUT)
        self.update_controls()
        self.advance_consultation()

    def retry_consultation(self, reason):
        plan = self.consultation
        if plan is None or not plan.reload_each_cycle or plan.stop_requested:
            return
        plan.slot_found = False
        plan.watch.stopped = False
        plan.watch.pause()
        plan.watch.next_at = monotonic() + REAL_WATCH_INTERVAL_SECONDS
        plan.phase = "monitor_idle"
        plan.login_requested = False
        plan.recoveries += 1
        plan.last_failure = reason
        plan.stage_at = monotonic()
        # Una demora del portal no cierra el websocket autorizado. La cola del
        # mismo trabajador serializa la próxima recarga tras la operación actual.
        plan.connection_requested = False
        plan.reconnect_at = plan.watch.next_at
        self.record(logging.WARNING, f"Vigilancia · Recuperación {plan.recoveries}: {reason}")
        self.record(logging.INFO, "La vigilancia continúa. Nueva recuperación en 120 s; sin repetir reservas.")
        self.query_state.setText("VIGILANCIA ACTIVA · Recuperando la consulta. Próxima recarga en 120 s.")
        if self.slot_dialog is not None:
            self.slot_status.setText(reason + "\nLa vigilancia sigue activa; próximo intento en 120 s.")
        self.render_watch(plan)

    def notify(self, message):
        if QSystemTrayIcon.isSystemTrayAvailable() and QSystemTrayIcon.supportsMessages():
            self.tray.show()
            self.tray.showMessage("EDUS Bot", message)

    def end_consultation(self, message):
        plan = self.consultation
        if plan:
            plan.watch.stop()
            self.render_watch(plan)
            if plan.watch.interval is not None and message != "Vigilancia detenida.":
                self.record(logging.INFO, "Vigilancia detenida.")
        self.consultation = None
        self.query_state.setText(message)
        if self.slot_dialog is not None and message.startswith(("Error", "Vigilancia detenida")):
            self.slot_status.setText(message)
        self.booking_status.setText(message.split(" · ", 1)[0])
        self.record(logging.ERROR if message.startswith("Error") else logging.INFO, message)
        self.stop.setEnabled(self.browser is not None and self.stop.isEnabled())
        self.update_controls()
        self.render_availability()
        if message.startswith(("Error", "Intervención necesaria")):
            self.notify(message)

    def stop_browser(self):
        if self.consultation is not None and self.consultation.simulation and not self.closing:
            self.end_consultation("Vigilancia simulada detenida.")
            return
        if self.browser is not None and self.consultation and self.consultation.phase == "working" and self.consultation.auto_booking:
            self.consultation.stop_requested = True
            self.browser.requestInterruption()
            self.query_state.setText("Deteniendo · verificando cualquier envío pendiente…")
            self.stop.setEnabled(False)
            return
        if self.consultation is not None:
            self.end_consultation("Vigilancia detenida.")
        if self.browser is not None and self.stop.isEnabled():
            self.stop.setEnabled(False)
            self.browser.requestInterruption()
            self.verified_account = ""
            self.update_availability(None)
            self.update_controls()
            self.record(logging.INFO, "Desconexión solicitada. Brave seguirá abierto.")

    @Slot()
    def browser_finished(self):
        self.browser.deleteLater()
        self.browser = None
        self.stop.setEnabled(False)
        if self.browser_status.text() != "Requiere configuración":
            self.browser_status.setText("Brave desconectado")
        self.verified_account = ""
        self.auth_status.setText(UNKNOWN)
        self.update_availability(None)
        if self.consultation and not self.consultation.simulation and self.consultation.phase in (
            "ready", "working", "refreshing", "establishment", "monitor_idle",
        ):
            plan = self.consultation
            if plan.reload_each_cycle and not plan.stop_requested and not self.closing:
                self.retry_consultation("Se interrumpió la conexión con Brave.")
                plan.connection_requested = False
                plan.reconnect_at = plan.watch.next_at
            else:
                self.end_consultation(
                    "Vigilancia detenida." if plan.stop_requested else
                    "Error · Se interrumpió la conexión con Brave."
                )
        if self.closing:
            self.close()
        else:
            self.update_controls()

    def closeEvent(self, event):
        if self.browser is not None:
            self.closing = True
            self.stop_browser()
            event.ignore()
            return
        self.closing = True
        self.consultation = None
        if self.slot_dialog is not None:
            self.slot_dialog.close()
        self.tray.hide()
        self.record(logging.INFO, "EDUS Bot cerrado.")
        event.accept()



