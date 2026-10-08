"""提醒通知：Windows 托盘 Toast + 提示音"""
import threading
import winsound

BEEP_DONE = threading.Event()


def beep_async(times=2):
    """异步提示音，不阻塞主线程"""

    def _run():
        for _ in range(times):
            winsound.Beep(1000, 250)
            winsound.Beep(1400, 250)

    threading.Thread(target=_run, daemon=True).start()


def notify(tray, title, message):
    """通过 QSystemTrayIcon 弹 Windows 通知"""
    beep_async(2)
    if tray is not None:
        try:
            tray.showMessage(title, message)
        except Exception:
            pass
