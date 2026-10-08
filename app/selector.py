"""区域圈选：全屏半透明遮罩 + 可调节选框（类 EV 录屏交互）

交互：
- 在空白处拖拽：画出新选框
- 拖动选框内部：整体移动
- 拖动 8 个手柄（四角 + 四边）：调整大小
- 底部工具条：精确输入宽高，取消 / 确定按钮
- 双击选框 / 回车：确定；Esc：取消

坐标体系：一律使用「虚拟桌面坐标」（多屏合并后的大坐标系，副屏可能为负坐标），
与 mss 截图的坐标系一致，因此跨屏移动、跨屏圈选都不会错位。
"""
from PySide6.QtCore import Qt, Signal, QRect, QPoint
from PySide6.QtGui import QGuiApplication, QPainter, QColor, QPen, QFont
from PySide6.QtWidgets import (
    QWidget, QFrame, QHBoxLayout, QLabel, QSpinBox, QPushButton,
)

MIN_SIZE = 10
HANDLE_HIT = 8


def virtual_desktop_rect():
    """覆盖所有屏幕的虚拟桌面矩形（副屏在主屏左侧时含负坐标）"""
    rect = QRect()
    for scr in QGuiApplication.screens():
        rect = rect.united(scr.geometry())
    if not rect.isValid():
        rect = QGuiApplication.primaryScreen().virtualGeometry()
    return rect


def list_screens():
    """返回 [(显示名, x, y, w, h, 缩放比), ...]，坐标为虚拟桌面坐标"""
    out = []
    for i, scr in enumerate(QGuiApplication.screens()):
        g = scr.geometry()
        dpr = round(float(scr.devicePixelRatio()), 2)
        name = f"屏幕 #{i + 1}  {g.width()}x{g.height()}  ({g.x()},{g.y()})"
        if scr == QGuiApplication.primaryScreen():
            name += "  主屏"
        if abs(dpr - 1.0) > 0.01:
            name += f"  缩放{int(dpr * 100)}%"
        out.append((name, g.x(), g.y(), g.width(), g.height(), dpr))
    return out


def screen_of_region(region):
    """判断区域中心落在哪个屏幕，返回索引；不在任何屏幕上时返回最近的屏幕索引"""
    if not region:
        return 0
    x, y, w, h = region
    cx, cy = x + w / 2, y + h / 2
    scrs = list_screens()
    best, best_d = 0, None
    for i, (_, sx, sy, sw, sh, _dpr) in enumerate(scrs):
        if sx <= cx < sx + sw and sy <= cy < sy + sh:
            return i
        dx = max(sx - cx, 0, cx - (sx + sw))
        dy = max(sy - cy, 0, cy - (sy + sh))
        d = dx * dx + dy * dy
        if best_d is None or d < best_d:
            best, best_d = i, d
    return best

# 手柄名 -> 光标形状
HANDLE_CURSORS = {
    "tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor,
    "tr": Qt.SizeBDiagCursor, "bl": Qt.SizeBDiagCursor,
    "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
    "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor,
}
# 手柄名 -> 对角锚点取法（用于拖拽缩放时的固定点）
OPPOSITE = {
    "tl": "br", "tr": "bl", "bl": "tr", "br": "tl",
    "t": "b", "b": "t", "l": "r", "r": "l",
}


class RegionSelector(QWidget):
    regionSelected = Signal(object)  # (x, y, w, h) 全局屏幕坐标
    cancelled = Signal()

    def __init__(self, initial=None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        # 关键：开启半透明窗口，选框区域才能透出真实桌面
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setGeometry(virtual_desktop_rect())
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)

        self._rect = None          # 选框（本窗口局部坐标）
        self._mode = "idle"        # idle / draw / move / resize
        self._anchor = None        # 拖拽固定点
        self._move_offset = None   # 移动选框时的按下点偏移
        self._handle = None        # 当前拖拽的手柄

        self._build_toolbar()
        if initial and len(initial) == 4:
            origin = self.geometry().topLeft()
            self._rect = QRect(
                QPoint(initial[0] - origin.x(), initial[1] - origin.y()),
                QPoint(initial[0] - origin.x() + initial[2] - 1,
                       initial[1] - origin.y() + initial[3] - 1),
            )
            self._sync_toolbar()
            self._place_toolbar()

    # ---------------------------------------------------------------- 显示

    def show_overlay(self):
        """覆盖全部屏幕显示（不能用 showFullScreen，那只会覆盖当前一块屏）"""
        self.setGeometry(virtual_desktop_rect())
        self.show()
        self.raise_()
        self.activateWindow()

    def showFullScreen(self):  # 兼容旧调用
        self.show_overlay()

    # ---------------------------------------------------------------- UI

    def _build_toolbar(self):
        self._toolbar = QFrame(self)
        self._toolbar.setStyleSheet(
            "QFrame { background: #2b2b2b; border-radius: 4px; }"
            "QLabel { color: #ffffff; font-size: 13px; background: transparent; }"
            "QSpinBox { background: #ffffff; min-height: 22px; }"
            "QPushButton { min-width: 64px; min-height: 24px; border: none; border-radius: 3px; }"
        )
        lay = QHBoxLayout(self._toolbar)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(6)
        lay.addWidget(QLabel("宽"))
        self.spin_w = QSpinBox()
        self.spin_w.setRange(MIN_SIZE, 9999)
        lay.addWidget(self.spin_w)
        lay.addWidget(QLabel("高"))
        self.spin_h = QSpinBox()
        self.spin_h.setRange(MIN_SIZE, 9999)
        lay.addWidget(self.spin_h)
        lay.addSpacing(12)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setStyleSheet("QPushButton { background:#6b6b6b; color:#fff; }"
                                      "QPushButton:hover { background:#7d7d7d; }")
        self.btn_cancel.clicked.connect(self._cancel)
        lay.addWidget(self.btn_cancel)
        self.btn_ok = QPushButton("确定")
        self.btn_ok.setStyleSheet("QPushButton { background:#1a73e8; color:#fff; }"
                                  "QPushButton:hover { background:#2f81f7; }")
        self.btn_ok.clicked.connect(self._confirm)
        lay.addWidget(self.btn_ok)
        self.spin_w.valueChanged.connect(self._on_spin)
        self.spin_h.valueChanged.connect(self._on_spin)
        self._toolbar.adjustSize()
        self._toolbar.hide()

    def _sync_toolbar(self):
        if self._rect is None:
            return
        self.spin_w.blockSignals(True)
        self.spin_h.blockSignals(True)
        self.spin_w.setValue(self._rect.width())
        self.spin_h.setValue(self._rect.height())
        self.spin_w.blockSignals(False)
        self.spin_h.blockSignals(False)

    def _place_toolbar(self):
        if self._rect is None:
            self._toolbar.hide()
            return
        tb = self._toolbar
        x = self._rect.left()
        y = self._rect.bottom() + 10
        if y + tb.height() > self.height():
            y = self._rect.top() - tb.height() - 10
        y = max(4, min(y, self.height() - tb.height() - 4))
        x = max(4, min(x, self.width() - tb.width() - 4))
        tb.move(x, y)
        tb.show()
        tb.raise_()

    def _on_spin(self):
        if self._rect is None:
            return
        w = self.spin_w.value()
        h = self.spin_h.value()
        tl = self._rect.topLeft()
        right = min(tl.x() + w - 1, self.width() - 2)
        bottom = min(tl.y() + h - 1, self.height() - 2)
        self._rect = QRect(tl, QPoint(right, bottom))
        self._place_toolbar()
        self.update()

    # ---------------------------------------------------------------- 绘制

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 110))
        if self._rect is None:
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(self.rect(), Qt.AlignCenter,
                             "按住鼠标左键拖拽，框选微信聊天区域；Esc 取消")
            return

        r = self._rect
        painter.setCompositionMode(QPainter.CompositionMode_Clear)
        painter.fillRect(r, Qt.transparent)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)

        pen = QPen(QColor(26, 115, 232), 2)
        painter.setPen(pen)
        painter.drawRect(r)

        painter.setPen(QPen(QColor(26, 115, 232), 1))
        painter.setBrush(QColor(255, 255, 255))
        for pt in self._handle_points().values():
            painter.drawRect(pt.x() - 4, pt.y() - 4, 8, 8)

        # 尺寸信息条
        g = self.mapToGlobal(r.topLeft())
        info = f"x:{g.x()},y:{g.y()} [{r.width()}x{r.height()}]  双击或回车确定，Esc 取消"
        painter.setFont(QFont("Microsoft YaHei", 10))
        rect_info = painter.boundingRect(self.rect().adjusted(0, 0, -8, 0), Qt.AlignLeft, info)
        iy = r.top() - rect_info.height() - 8
        if iy < 4:
            iy = r.top() + 8
        rect_info.moveTopLeft(QPoint(r.left(), iy))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(60, 160, 90))
        painter.drawRect(rect_info.adjusted(-6, -3, 6, 3))
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(rect_info, Qt.AlignLeft, info)

    def _handle_points(self):
        r = self._rect
        mx = (r.left() + r.right()) // 2
        my = (r.top() + r.bottom()) // 2
        return {
            "tl": r.topLeft(), "t": QPoint(mx, r.top()),
            "tr": r.topRight(), "l": QPoint(r.left(), my),
            "bl": r.bottomLeft(), "r": QPoint(r.right(), my),
            "b": QPoint(mx, r.bottom()), "br": r.bottomRight(),
        }

    # ---------------------------------------------------------------- 鼠标

    def _handle_at(self, p):
        if self._rect is None:
            return None
        for name, pt in self._handle_points().items():
            if (p - pt).manhattanLength() <= HANDLE_HIT:
                return name
        return None

    def _anchor_for(self, handle):
        return self._handle_points()[OPPOSITE[handle]]

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        p = e.position().toPoint()
        h = self._handle_at(p)
        if h:
            self._mode = "resize"
            self._handle = h
            self._anchor = self._anchor_for(h)
        elif self._rect is not None and self._rect.contains(p):
            self._mode = "move"
            self._move_offset = p - self._rect.topLeft()
        else:
            self._mode = "draw"
            self._rect = QRect(p, p)
            self._anchor = p
            self._toolbar.hide()
        self.update()

    def mouseMoveEvent(self, e):
        p = e.position().toPoint()
        if self._mode == "idle":
            self._update_cursor(p)
            return
        if self._mode == "draw" or self._mode == "resize":
            self._rect = QRect(self._anchor, p).normalized()
        elif self._mode == "move":
            tl = p - self._move_offset
            tl.setX(max(0, min(tl.x(), self.width() - self._rect.width())))
            tl.setY(max(0, min(tl.y(), self.height() - self._rect.height())))
            self._rect.moveTopLeft(tl)
        self._sync_toolbar()
        self.update()

    def mouseReleaseEvent(self, e):
        if self._mode in ("draw", "move", "resize"):
            if self._rect is not None and (self._rect.width() < MIN_SIZE or self._rect.height() < MIN_SIZE):
                self._rect = None
            self._mode = "idle"
            self._place_toolbar()
            self.update()

    def mouseDoubleClickEvent(self, e):
        if self._rect is not None and self._rect.contains(e.position().toPoint()):
            self._confirm()

    def _update_cursor(self, p):
        h = self._handle_at(p)
        if h:
            self.setCursor(HANDLE_CURSORS[h])
        elif self._rect is not None and self._rect.contains(p):
            self.setCursor(Qt.SizeAllCursor)
        else:
            self.setCursor(Qt.CrossCursor)

    # ---------------------------------------------------------------- 确认/取消

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self._cancel()
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._confirm()

    def _confirm(self):
        if self._rect is None or self._rect.width() < MIN_SIZE or self._rect.height() < MIN_SIZE:
            self._cancel()
            return
        g = self.mapToGlobal(self._rect.topLeft())
        self.regionSelected.emit((g.x(), g.y(), self._rect.width(), self._rect.height()))
        self.close()

    def _cancel(self):
        self.cancelled.emit()
        self.close()


class RegionFrame(QWidget):
    """常驻监控框：圈定后一直显示在屏幕上

    两种状态：
    - **锁定**（默认）：鼠标事件完全穿透，不影响微信操作
    - **解锁**：可拖动移动、拖边角缩放、方向键微调；双击框内重新锁定

    边框与标签一律绘制在监控区域**外侧**，不会被截图，不污染 OCR 内容。
    坐标使用虚拟桌面坐标（副屏可为负数），支持跨屏幕移动。
    """

    regionChanged = Signal(object)   # (x, y, w, h) 拖动/缩放结束后发出
    lockChanged = Signal(bool)       # 锁定状态变化

    MARGIN = 3        # 边框画在区域外侧的距离
    TOP_MARGIN = 26   # 顶部标签条高度
    HINT_H = 22       # 解锁时底部提示条高度
    HANDLE_HIT = 10
    MIN_SIZE = 20

    def __init__(self):
        super().__init__(
            None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setWindowTitle("监控区域")
        self._region = None
        self._label = "监控中"
        self._locked = None
        self._mode = "idle"      # idle / move / resize
        self._handle = None
        self._start = None       # 按下时的局部坐标
        self._rect0 = None       # 按下时的区域
        self.setMouseTracking(True)
        self.set_locked(True)

    # ---------------------------------------------------------------- 状态

    @property
    def locked(self):
        return self._locked

    @property
    def region(self):
        return self._region

    def set_locked(self, locked):
        """切换锁定状态。锁定=鼠标穿透；解锁=可拖动缩放（需重建窗口标志位）"""
        if self._locked == locked:
            return
        self._locked = locked
        visible = self.isVisible()
        if visible:
            self.hide()
        # 顺序很关键：必须先改 WA_TransparentForMouseEvents 属性再改窗口标志，
        # 否则 Qt 会依据旧属性自动把 WindowTransparentForInput 标志加回来
        self.setAttribute(Qt.WA_TransparentForMouseEvents, locked)
        # 用整数位运算：Qt.WindowFlags 上的 &= 取反在某些 PySide6 版本不生效
        flags = int(self.windowFlags())
        f_transparent = int(Qt.WindowTransparentForInput)
        f_nofocus = int(Qt.WindowDoesNotAcceptFocus)
        if locked:
            flags |= f_transparent | f_nofocus
        else:
            flags &= ~f_transparent
            flags &= ~f_nofocus
        self.setWindowFlags(Qt.WindowFlags(flags))
        self.setAttribute(Qt.WA_TransparentForMouseEvents, locked)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setMouseTracking(True)
        self._apply_geometry()
        if visible or not locked:
            self.show()
            self.raise_()
        self.update()
        self.lockChanged.emit(locked)

    def set_region(self, region):
        if not region or len(region) != 4:
            return
        self._region = tuple(int(v) for v in region)
        self._apply_geometry()
        self.update()

    def set_label(self, text):
        self._label = text
        self.update()

    def _apply_geometry(self):
        if self._region is None:
            return
        x, y, w, h = self._region
        bottom = self.HINT_H if not self._locked else self.MARGIN
        self.setGeometry(
            x - self.MARGIN, y - self.TOP_MARGIN,
            w + self.MARGIN * 2, h + self.TOP_MARGIN + bottom,
        )

    # ---------------------------------------------------------------- 几何

    def _border_rect(self):
        """边框在本窗口内的局部坐标矩形"""
        _, _, w, h = self._region
        return QRect(0, self.TOP_MARGIN - self.MARGIN,
                     w + self.MARGIN * 2, h + self.MARGIN * 2)

    def _handle_points(self):
        b = self._border_rect()
        mx = b.center().x()
        my = b.center().y()
        return {
            "tl": b.topLeft(), "t": QPoint(mx, b.top()), "tr": b.topRight(),
            "l": QPoint(b.left(), my), "r": QPoint(b.right(), my),
            "bl": b.bottomLeft(), "b": QPoint(mx, b.bottom()), "br": b.bottomRight(),
        }

    def _handle_at(self, p):
        for name, pt in self._handle_points().items():
            if abs(p.x() - pt.x()) <= self.HANDLE_HIT and abs(p.y() - pt.y()) <= self.HANDLE_HIT:
                return name
        return None

    @staticmethod
    def _resize_region(rect0, handle, dx, dy):
        x, y, w, h = rect0
        if "l" in handle:
            x, w = x + dx, w - dx
        if "r" in handle:
            w = w + dx
        if "t" in handle:
            y, h = y + dy, h - dy
        if "b" in handle:
            h = h + dy
        if w < RegionFrame.MIN_SIZE:
            w = RegionFrame.MIN_SIZE
            if "l" in handle:
                x = rect0[0] + rect0[2] - w
        if h < RegionFrame.MIN_SIZE:
            h = RegionFrame.MIN_SIZE
            if "t" in handle:
                y = rect0[1] + rect0[3] - h
        return (x, y, w, h)

    # ---------------------------------------------------------------- 跨屏

    def move_to_screen(self, index):
        """把监控框整体移到指定屏幕（保持尺寸，居中放置）"""
        if self._region is None:
            return None
        scrs = list_screens()
        if index < 0 or index >= len(scrs):
            return None
        _, sx, sy, sw, sh, _dpr = scrs[index]
        _x, _y, w, h = self._region
        w = max(self.MIN_SIZE, min(w, sw - 40))
        h = max(self.MIN_SIZE, min(h, sh - 60))
        nx = sx + (sw - w) // 2
        ny = sy + (sh - h) // 2
        self.set_region((nx, ny, w, h))
        return (nx, ny, w, h)

    # ---------------------------------------------------------------- 绘制

    def paintEvent(self, event):
        if self._region is None:
            return
        _, _, w, h = self._region
        locked = self._locked
        accent = QColor(26, 115, 232) if locked else QColor(230, 145, 20)
        painter = QPainter(self)
        border = self._border_rect()

        pen = QPen(accent, 2, Qt.DashLine if locked else Qt.SolidLine)
        painter.setPen(pen)
        painter.drawRect(border.adjusted(1, 1, -1, -1))

        painter.setPen(QPen(accent, 3))
        for p in (border.topLeft(), border.topRight(), border.bottomLeft(), border.bottomRight()):
            painter.drawEllipse(p, 3, 3)

        # 顶部标签
        tag_text = f"{self._label} {w}x{h}" if locked else f"{self._label} {w}x{h}  可拖动"
        font = QFont("Microsoft YaHei", 10)
        painter.setFont(font)
        tw = painter.boundingRect(QRect(), Qt.AlignLeft, tag_text).width() + 20
        tag = QRect(0, 0, max(tw, 132), 20)
        painter.setPen(Qt.NoPen)
        painter.setBrush(accent)
        painter.drawRoundedRect(tag, 3, 3)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(tag, Qt.AlignCenter, tag_text)

        if not locked:
            # 底部操作提示（画在监控区外侧，不污染截图）
            hy = border.bottom() + 2
            hint = QRect(0, hy, self.width(), self.HINT_H - 4)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(230, 145, 20))
            painter.drawRoundedRect(hint, 3, 3)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(hint, Qt.AlignCenter,
                             "拖动框内移动 · 拖边角缩放 · 方向键微调 · 双击锁定")
            # 八向手柄
            painter.setPen(QPen(QColor(255, 255, 255), 1))
            painter.setBrush(accent)
            for pt in self._handle_points().values():
                painter.drawRect(pt.x() - 4, pt.y() - 4, 8, 8)

    # ---------------------------------------------------------------- 交互

    def mousePressEvent(self, e):
        if self._locked or self._region is None or e.button() != Qt.LeftButton:
            return
        p = e.position().toPoint()
        h = self._handle_at(p)
        self._start = p
        self._rect0 = tuple(self._region)
        if h:
            self._mode = "resize"
            self._handle = h
        elif self._border_rect().contains(p):
            self._mode = "move"
        else:
            self._mode = "idle"

    def mouseMoveEvent(self, e):
        if self._locked or self._region is None:
            return
        p = e.position().toPoint()
        if self._mode == "idle":
            self._update_cursor(p)
            return
        dx, dy = p.x() - self._start.x(), p.y() - self._start.y()
        if self._mode == "move":
            x, y, w, h = self._rect0
            self.set_region((x + dx, y + dy, w, h))
        elif self._mode == "resize":
            self.set_region(self._resize_region(self._rect0, self._handle, dx, dy))

    def mouseReleaseEvent(self, e):
        if self._locked or self._mode == "idle":
            return
        self._mode = "idle"
        self._handle = None
        if self._region and self._region != self._rect0:
            self.regionChanged.emit(tuple(self._region))

    def mouseDoubleClickEvent(self, e):
        if not self._locked:
            self.set_locked(True)

    def keyPressEvent(self, e):
        if self._locked or self._region is None:
            return
        step = 10 if e.modifiers() & Qt.ShiftModifier else 1
        x, y, w, h = self._region
        key = e.key()
        if key == Qt.Key_Left:
            x -= step
        elif key == Qt.Key_Right:
            x += step
        elif key == Qt.Key_Up:
            y -= step
        elif key == Qt.Key_Down:
            y += step
        elif key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Escape, Qt.Key_Space):
            self.set_locked(True)
            return
        else:
            return
        self.set_region((x, y, w, h))
        self.regionChanged.emit(tuple(self._region))

    def _update_cursor(self, p):
        h = self._handle_at(p)
        if h:
            self.setCursor(HANDLE_CURSORS[h])
        elif self._border_rect().contains(p):
            self.setCursor(Qt.SizeAllCursor)
        else:
            self.unsetCursor()
