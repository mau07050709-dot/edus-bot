from pathlib import Path

from PySide6.QtCore import QLockFile
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from settings import data_directory
from theme import apply_theme
from window import MainWindow


def main():
    app = QApplication([])
    app.setApplicationName("EDUS Bot")
    apply_theme(app)
    app.setWindowIcon(QIcon(str(Path(__file__).with_name('calendario.ico'))))
    try:
        directory = data_directory()
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        QMessageBox.warning(None, "EDUS Bot", "No se pudo acceder a la carpeta de configuración.")
        return 1
    lock = QLockFile(str(directory / "edus-bot.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        message = (
            "EDUS Bot ya está abierto. Usa la ventana que ya tienes en ejecución."
            if lock.error() == QLockFile.LockError.LockFailedError
            else "No se pudo proteger el acceso a la configuración de EDUS Bot."
        )
        QMessageBox.warning(None, "EDUS Bot", message)
        return 1
    try:
        window = MainWindow(directory)
        window.show()
        return app.exec()
    finally:
        lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())


