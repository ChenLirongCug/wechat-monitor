"""监控引擎：定时截图监控区 -> OCR 提取文字+坐标 -> 增量去重 -> 发出新消息批次

在独立 QThread 中运行。mss / RapidOCR 实例均在子线程内创建（mss 非线程安全）。
"""
import hashlib
import time
import datetime

import numpy as np
import mss
from rapidocr_onnxruntime import RapidOCR
from PySide6.QtCore import QThread, Signal

from app.config import load_config


class MonitorThread(QThread):
    # 新消息批次：list[dict(text, x, y, w, h)]，坐标为全局屏幕坐标
    newBatch = Signal(list)
    # 本帧全部文本（用于实时预览）：list[str]
    liveFrame = Signal(list)
    errorMsg = Signal(str)

    def __init__(self, region):
        super().__init__()
        self.region = region          # (x, y, w, h)
        self._running = False
        self._seen = {}                # hash -> timestamp
        self._last_frame = None        # np.ndarray BGR
        self._frame_lock = None
        self._pending_region = None    # 拖动监控框时热更新区域
        self._force_judge = False      # 「重判当前屏幕」请求：下一帧把屏上内容全部送判

    def rejudge_once(self):
        """请求把当前屏幕上的可见内容全部重新判定（不等待新消息出现）"""
        self._force_judge = True

    def start(self):
        self._running = True
        super().start()

    def stop(self):
        self._running = False

    def set_region(self, region):
        """运行中更新监控区域（由主线程调用，线程内下一帧生效）

        跨屏拖动监控框后无需重启监控：区域变更后的第一帧视为历史消息，
        避免把整屏已有聊天内容当成新消息批量报警。
        """
        if not region or len(region) != 4:
            return
        self._pending_region = tuple(int(v) for v in region)

    @property
    def running(self):
        return self._running

    def save_evidence(self):
        """保存最近一帧截图，返回文件路径；失败返回 None"""
        import threading
        if self._frame_lock is None:
            self._frame_lock = threading.Lock()
        with self._frame_lock:
            frame = self._last_frame
        if frame is None:
            return None
        from PIL import Image
        from app.config import EVIDENCE_DIR
        import os
        os.makedirs(EVIDENCE_DIR, exist_ok=True)
        path = os.path.join(EVIDENCE_DIR, datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".png")
        Image.fromarray(frame[:, :, ::-1]).save(path)  # BGR -> RGB
        return path

    # ------------------------------------------------------------------

    def run(self):
        rx, ry, rw, rh = self.region
        ocr = RapidOCR()
        seen = self._seen
        first_frame = True
        try:
            with mss.mss() as sct:
                while self._running:
                    cfg = load_config()
                    interval = max(0.5, float(cfg.get("interval_sec", 2.0)))
                    if self._pending_region:
                        # 监控框被拖动到新位置/新屏幕：切换区域并把首帧当历史
                        rx, ry, rw, rh = self._pending_region
                        self.region = self._pending_region
                        self._pending_region = None
                        seen.clear()
                        first_frame = True
                    min_len = int(cfg.get("min_text_len", 2))
                    expire = int(cfg.get("dedup_expire_sec", 600))

                    img = np.array(sct.grab({"left": rx, "top": ry, "width": rw, "height": rh}))[:, :, :3]
                    if self._frame_lock is None:
                        import threading
                        self._frame_lock = threading.Lock()
                    with self._frame_lock:
                        self._last_frame = img.copy()

                    lines = self._ocr_lines(ocr, img, rx, ry)
                    lines = self._merge_lines(lines)
                    self.liveFrame.emit([l["text"] for l in lines])

                    now = time.time()
                    if self._force_judge:
                        # 手动重判：清空指纹，把屏上所有可见内容当新消息送判
                        self._force_judge = False
                        seen.clear()
                        fresh = self._pick_fresh(lines, seen, now, min_len, False)
                    else:
                        fresh = self._pick_fresh(lines, seen, now, min_len, first_frame)
                    self._gc(seen, now, expire)
                    first_frame = False

                    if fresh:
                        self.newBatch.emit(fresh)
                    time.sleep(interval)
        except Exception as e:
            self.errorMsg.emit(f"监控线程异常退出：{e}")
            self._running = False

    @staticmethod
    def _norm(text):
        """去除所有空白后归一化，避免 OCR 空格抖动导致同一句话生成不同指纹"""
        return "".join(text.split())

    @classmethod
    def _pick_fresh(cls, lines, seen, now, min_len, first_frame):
        """增量去重：只保留本帧「新出现」的行；同时统计同屏重复次数供刷屏判定

        - 已登记指纹（仍在屏上）的行会被跳过，避免每帧重复判定、重复报警
        - 消息滚出屏幕后指纹才停止刷新，expire 秒后过期；之后若再出现（被重发）会重新判定
        """
        counts = {}
        for l in lines:
            t = l["text"].strip()
            if len(t) < min_len:
                continue
            n = cls._norm(t)
            counts[n] = counts.get(n, 0) + 1

        fresh = []
        for l in lines:
            t = l["text"].strip()
            if len(t) < min_len:
                continue
            n = cls._norm(t)
            h = hashlib.md5(n.encode("utf-8")).hexdigest()
            l["repeat"] = counts.get(n, 1)
            is_new = h not in seen
            seen[h] = now
            if first_frame:
                continue   # 首帧视为历史消息，只登记不判定
            if not is_new:
                continue   # 已在屏上，跳过
            fresh.append(l)
        return fresh

    @staticmethod
    def _ocr_lines(ocr, img, rx, ry):
        """OCR 返回行信息，坐标换算为全局屏幕坐标"""
        result, _ = ocr(img)
        lines = []
        if not result:
            return lines
        for item in result:
            box, text, score = item[0], item[1], item[2]
            try:
                score = float(score)
            except (TypeError, ValueError):
                score = 0.0
            if score < 0.55:
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            x, y = int(min(xs)), int(min(ys))
            w, h = int(max(xs) - x), int(max(ys) - y)
            lines.append({
                "text": str(text).strip(),
                "x": rx + x, "y": ry + y, "w": w, "h": h,
            })
        return lines

    @staticmethod
    def _merge_lines(lines):
        """把同一条消息被 OCR 拆成的多行合并回一条

        微信消息超宽会自动折行，OCR 按视觉行输出——一行长网址被拦腰截断后，
        单行判定必然漏报。合并依据：垂直间距小于 0.7 倍行高（同一段落内换行）
        且左边缘接近（同一气泡的文字左对齐），按从上到下顺序拼接。
        """
        if len(lines) < 2:
            return lines
        out = []
        for l in sorted(lines, key=lambda d: (d["y"], d["x"])):
            if out:
                p = out[-1]
                h = max(p["h"], l["h"]) or 1
                gap = l["y"] - (p["y"] + p["h"])
                if -0.3 * h <= gap <= 0.7 * h and abs(l["x"] - p["x"]) <= 2.0 * h:
                    p["text"] += l["text"]
                    p["w"] = max(p["x"] + p["w"], l["x"] + l["w"]) - p["x"]
                    p["h"] = l["y"] + l["h"] - p["y"]
                    continue
            out.append(dict(l))
        return out

    @staticmethod
    def _gc(seen, now, expire):
        dead = [h for h, t in seen.items() if now - t > expire]
        for h in dead:
            del seen[h]
