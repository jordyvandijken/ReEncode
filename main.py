import sys
from PySide6.QtWidgets import QApplication
from reencode.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setOrganizationName("ReEncode")
    app.setApplicationName("ReEncode")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
