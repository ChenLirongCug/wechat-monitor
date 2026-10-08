"""监控框可移动性测试：锁定/解锁、拖动、缩放、方向键微调、跨屏移动"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt, QPoint
from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv)

from app.selector import (
    RegionFrame, RegionSelector, virtual_desktop_rect, list_screens, screen_of_region,
)

PASS = []


def check(name, cond, extra=""):
    if not cond:
        print(f"FAIL  {name}  {extra}")
        sys.exit(1)
    PASS.append(name)
    print(f"ok    {name}" + (f"  ({extra})" if extra else ""))


class FakeEvent:
    def __init__(self, p, button=Qt.LeftButton, mods=Qt.NoModifier, key=None):
        self._p, self._b, self._m, self._k = p, button, mods, key

    def position(self):
        class P:
            def __init__(s, p):
                s._p = p

            def toPoint(s):
                return s._p
        return P(self._p)

    def button(self):
        return self._b

    def modifiers(self):
        return self._m

    def key(self):
        return self._k


# ---------------------------------------------------------------- 屏幕工具
scrs = list_screens()
check("list_screens 至少返回一个屏幕", len(scrs) >= 1, f"{len(scrs)} 块")
vr = virtual_desktop_rect()
check("虚拟桌面矩形覆盖所有屏幕", vr.width() >= scrs[0][3] and vr.height() >= scrs[0][4],
      f"{vr.width()}x{vr.height()} @({vr.x()},{vr.y()})")
check("区域定位到屏幕", screen_of_region((10, 10, 100, 100)) == 0)

# ---------------------------------------------------------------- 锁定态
fr = RegionFrame()
fr.set_region((300, 200, 400, 300))
check("默认锁定", fr.locked is True)
check("锁定时鼠标穿透", bool(fr.windowFlags() & Qt.WindowTransparentForInput))
check("锁定时不接收焦点", bool(fr.windowFlags() & Qt.WindowDoesNotAcceptFocus))

# 锁定时拖动应无效
fr.mousePressEvent(FakeEvent(QPoint(60, 60)))
fr.mouseMoveEvent(FakeEvent(QPoint(160, 160)))
fr.mouseReleaseEvent(FakeEvent(QPoint(160, 160)))
check("锁定状态下拖动无效", fr.region == (300, 200, 400, 300), str(fr.region))

# ---------------------------------------------------------------- 解锁
got = []
fr.regionChanged.connect(lambda r: got.append(tuple(r)))
fr.set_locked(False)
check("解锁后不再穿透", not (fr.windowFlags() & Qt.WindowTransparentForInput))

# 框内局部坐标：区域左上角 = (MARGIN=3, TOP_MARGIN=26)
inner = QPoint(3 + 200, 26 + 150)
fr.mousePressEvent(FakeEvent(inner))
check("框内按下进入移动模式", fr._mode == "move")
# 向右下各移动 50px
fr.mouseMoveEvent(FakeEvent(inner + QPoint(50, 30)))
fr.mouseReleaseEvent(FakeEvent(inner + QPoint(50, 30)))
check("拖动后区域整体平移", fr.region == (350, 230, 400, 300), str(fr.region))
check("拖动结束发出 regionChanged", got and got[-1] == (350, 230, 400, 300), str(got[-1:]))

# 缩放：拖右下角
br = QPoint(3 + 400, 26 + 300)
fr.mousePressEvent(FakeEvent(br))
check("角点命中手柄", fr._mode == "resize" and fr._handle == "br", fr._handle)
fr.mouseMoveEvent(FakeEvent(br + QPoint(20, 10)))
fr.mouseReleaseEvent(FakeEvent(br + QPoint(20, 10)))
check("拖角缩放", fr.region == (350, 230, 420, 310), str(fr.region))

# 方向键微调（Shift 加速 10px）
fr.keyPressEvent(FakeEvent(QPoint(0, 0), key=Qt.Key_Left, mods=Qt.ShiftModifier))
check("Shift+方向键移动 10px", fr.region == (340, 230, 420, 310), str(fr.region))
fr.keyPressEvent(FakeEvent(QPoint(0, 0), key=Qt.Key_Up))
check("方向键微调 1px", fr.region == (340, 229, 420, 310), str(fr.region))

# 双击锁定
fr.mouseDoubleClickEvent(FakeEvent(inner))
check("双击后重新锁定", fr.locked is True)
check("锁定后恢复穿透属性", bool(fr.windowFlags() & Qt.WindowTransparentForInput))

# ---------------------------------------------------------------- 跨屏移动
fr2 = RegionFrame()
fr2.set_region((0, 0, 300, 200))
moved = fr2.move_to_screen(0)
check("move_to_screen 返回新坐标", moved is not None and len(moved) == 4, str(moved))
_, sx, sy, sw, sh, _d = scrs[0]
check("移动后区域中心落在该屏", sx <= moved[0] + moved[2] / 2 <= sx + sw
      and sy <= moved[1] + moved[3] / 2 <= sy + sh, str(moved))
check("越界索引安全返回 None", fr2.move_to_screen(99) is None)

# 负坐标屏幕（副屏在主屏左侧）也支持
fr2.set_region((-1200, 100, 300, 200))
check("支持负坐标区域", fr2.region[0] == -1200 and fr2.x() == -1203, str(fr2.geometry()))

# ---------------------------------------------------------------- 圈选遮罩覆盖多屏
sel = RegionSelector()
sel.show_overlay()
app.processEvents()
g = sel.geometry()
check("圈选遮罩覆盖虚拟桌面", g.width() == vr.width() and g.height() == vr.height(),
      f"{g.width()}x{g.height()}")

print(f"\nFRAME_TEST_OK: {len(PASS)} 项全部通过")
