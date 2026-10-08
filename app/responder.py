"""自动@回复（在「基本设置」里选择 自动@用户回复 后启用）

流程：把微信窗口带到前台 -> 右键点击不当言论消息 -> 截图右键菜单 ->
OCR 找到"@TA/提醒"菜单项 -> 点击该菜单项（输入框出现 @昵称并获得焦点）->
粘贴回复文字 -> 回车发送

注意：
- pyautogui.FAILSAFE=True：鼠标快速移到屏幕左上角可紧急中断
- 微信不同版本的菜单文字可能不同，可在 config.json 的 menu_keywords 中调整
- 每一步的结果都会回传主界面（状态栏 + 消息判定流 + 报警记录"动作"列）
"""
import ctypes
import os
import time

import numpy as np
import mss
import pyautogui
from rapidocr_onnxruntime import RapidOCR
from PySide6.QtCore import QThread, Signal

from app.config import load_config, EVIDENCE_DIR

pyautogui.PAUSE = 0.25
pyautogui.FAILSAFE = True

u32 = ctypes.windll.user32


def set_clipboard(text):
    """Windows 剪贴板写入中文（pyautogui 无法直接输入中文，走粘贴）"""
    CF_UNICODETEXT = 13
    u = ctypes.c_wchar_p(text)
    k32 = ctypes.windll.kernel32
    k32.GlobalLock.restype = ctypes.c_void_p
    if not u32.OpenClipboard(0):
        return False
    try:
        u32.EmptyClipboard()
        h = k32.GlobalAlloc(0x0002, (len(text) + 1) * 2)  # GMEM_MOVEABLE
        p = k32.GlobalLock(h)
        if not p:
            return False
        ctypes.memmove(p, u, (len(text) + 1) * 2)
        k32.GlobalUnlock(h)
        u32.SetClipboardData(CF_UNICODETEXT, h)
        return True
    finally:
        u32.CloseClipboard()


def _norm_menu_text(t):
    """菜单文字归一化：OCR 可能把 @TA 读成 @ TA / QTA 等"""
    return "".join(str(t).split()).lower().replace("＠", "@")


def _focus_wechat():
    """尽力把微信主窗口带到前台；返回是否找到微信窗口"""
    hwnd = u32.FindWindowW("WeChatMainWndForPC", None)
    if not hwnd:
        hwnd = u32.FindWindowW("Qt51514QWindowIcon", None)   # 微信 4.x
    if not hwnd:
        hwnd = u32.FindWindowW(None, "微信")
    if not hwnd:
        return False
    u32.SetForegroundWindow(hwnd)
    time.sleep(0.5)
    return True


class ResponderThread(QThread):
    done = Signal(str)  # 执行结果描述
    _ocr = None

    def __init__(self, item, parent=None):
        super().__init__(parent)
        self.item = item

    def run(self):
        try:
            msg = self._respond()
            self.done.emit(msg)
        except pyautogui.FailSafeException:
            self.done.emit("自动回复已手动中断（鼠标移至左上角）")
        except Exception as e:
            self.done.emit(f"自动回复失败：{e}")

    def _respond(self):
        cfg = load_config()
        reply = cfg.get("reply_text", "请注意言辞")
        menu_keywords = cfg.get("menu_keywords", ["提醒", "@TA", "提到", "＠"])

        it = self.item
        cx = it["x"] + max(it["w"] // 2, 4)
        cy = it["y"] + max(it["h"] // 2, 4)

        # 0. 微信必须在前台，否则右键会点到别的窗口
        if not _focus_wechat():
            pass  # 找不到窗口句柄时也尝试继续（按屏幕坐标点击）
        time.sleep(0.3)

        # 1. 右键消息
        pyautogui.moveTo(cx, cy, duration=0.15)
        pyautogui.click(button="right")
        time.sleep(0.9)

        # 2. OCR 菜单，定位 @ 菜单项
        target = self._find_menu_item(cx, cy, menu_keywords)
        if target is None:
            pyautogui.press("esc")
            return ("未找到 @/提醒 菜单项，已取消。"
                    "已保存菜单截图到 evidence 目录，可在 config.json 调整 menu_keywords")

        pyautogui.moveTo(target[0], target[1], duration=0.1)
        pyautogui.click()
        time.sleep(0.6)

        # 3. 输入框应已出现 @昵称并获得焦点，粘贴回复并发送
        set_clipboard(reply)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.3)
        pyautogui.press("enter")
        time.sleep(0.2)
        return "已自动发送：@" + " " + reply

    @classmethod
    def _find_menu_item(cls, cx, cy, keywords):
        """在右键点附近截屏并 OCR，找到 @/提醒 菜单项坐标"""
        if cls._ocr is None:
            cls._ocr = RapidOCR()
        left = max(0, cx - 400)
        top = max(0, cy - 200)
        with mss.mss() as sct:
            img = np.array(sct.grab({
                "left": left, "top": top, "width": 800, "height": 600,
            }))[:, :, :3]

        # 同时存一张菜单区域截图，便于菜单找不到时排查
        try:
            os.makedirs(EVIDENCE_DIR, exist_ok=True)
            from PIL import Image
            path = os.path.join(
                EVIDENCE_DIR,
                time.strftime("menu_%Y%m%d_%H%M%S") + ".png")
            Image.fromarray(img[:, :, ::-1]).save(path)
        except Exception:
            path = ""

        result, _ = cls._ocr(img)
        if not result:
            return None
        kws = [_norm_menu_text(k) for k in keywords]
        for item in result:
            box, text = item[0], str(item[1])
            t = _norm_menu_text(text)
            if len(t) > 12:      # 菜单项都很短，排除聊天内容干扰
                continue
            if any(k and (k in t or t in k) for k in kws):
                xs = [p[0] for p in box]
                ys = [p[1] for p in box]
                return left + sum(xs) / len(xs), top + sum(ys) / len(ys)
        return None
