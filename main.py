"""微信群言论监控助手 - 入口"""
import os
import sys

# PyInstaller --noconsole 打包后 stdout/stderr 为 None，
# 库内部（cv2/rapidocr）一旦 print 就会崩溃，这里兜底重定向
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

from PySide6.QtWidgets import QApplication
from app.panel import MainWindow


def main():
    try:
        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(True)
        win = MainWindow()
        win.show()
        sys.exit(app.exec())
    except BaseException:
        import traceback
        try:
            with open("crash.log", "a", encoding="utf-8") as f:
                f.write(traceback.format_exc() + "\n")
        except OSError:
            pass
        raise


if __name__ == "__main__":
    main()
