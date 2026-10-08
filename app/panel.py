"""控制面板主界面：监控启停、区域圈选、模式切换、LLM 设置、实时消息流与报警记录"""
import datetime
import threading
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon, QPixmap, QColor
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QGridLayout, QGroupBox,
    QPushButton, QLabel, QSpinBox, QDoubleSpinBox, QRadioButton, QLineEdit,
    QTextEdit, QTabWidget, QListWidget, QTableWidget, QTableWidgetItem,
    QStatusBar, QMessageBox, QHeaderView, QCheckBox, QComboBox, QScrollArea,
    QPlainTextEdit,
)

from app.config import load_config, save_config, rules_to_text, parse_rules_text
from app.selector import (
    RegionSelector, RegionFrame, list_screens, screen_of_region,
)
from app.engine import MonitorThread
from app.judge import JudgeThread
from app.responder import ResponderThread
from app.notifier import notify
from app import storage

VERDICT_TAG = {"ok": "[正常]", "warn": "[不当]", "severe": "[严重]", "": "[判定中]"}


class MainWindow(QMainWindow):
    llmTestDone = Signal(bool, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("微信群言论监控助手 — 武汉同辰科技有限公司 业务电话：15001168567")
        self.resize(960, 620)
        self.cfg = load_config()

        self.monitor = None
        self.judge = JudgeThread()
        self.judge.verdictReady.connect(self.on_verdicts)
        self.judge.statusMsg.connect(self.statusBarMessage)
        self.judge.start()
        self.responder = None
        self._responders = []  # 保留引用，避免执行中的回复线程被回收
        self.selector = None
        self.frame = RegionFrame()
        self.frame.regionChanged.connect(self.on_frame_region_changed)
        self.frame.lockChanged.connect(self.on_frame_lock_changed)
        self._alerted = {}  # 内容指纹 -> 最近提醒时间：同一内容本次运行只提醒一次（或按冷却期）
        self._alerted_recent = {}  # 最近提醒时间->指纹：近距离片段去重（折行碎片）

        self.tray = None
        self._setup_tray()
        self._build_ui()
        self.llmTestDone.connect(self.on_llm_test_done)
        if self.cfg.get("region"):
            self.frame.set_region(tuple(self.cfg["region"]))
            self.frame.set_label("监控区域")
            self.frame.show()
            self._refresh_region_label()

    # ---------------------------------------------------------------- UI

    def _setup_tray(self):
        from PySide6.QtWidgets import QSystemTrayIcon, QMenu
        pm = QPixmap(32, 32)
        pm.fill(QColor("#534AB7"))
        self.tray = QSystemTrayIcon(QIcon(pm), self)
        menu = QMenu()
        act_show = menu.addAction("打开主界面")
        act_show.triggered.connect(self.showNormal)
        act_quit = menu.addAction("退出")
        act_quit.triggered.connect(self.close)
        self.tray.setContextMenu(menu)
        self.tray.show()

    def _build_ui(self):
        central = QWidget()
        layout = QHBoxLayout(central)

        # ---------- 左侧：设置（可滚动） ----------
        left_panel = QWidget()
        left = QVBoxLayout(left_panel)
        left.setContentsMargins(6, 6, 6, 6)

        gb_region = QGroupBox("监控区域")
        v1 = QVBoxLayout(gb_region)
        self.lbl_region = QLabel("未设置（点击下方按钮圈选）")
        self.lbl_region.setWordWrap(True)
        btn_select = QPushButton("圈选监控区域")
        btn_select.clicked.connect(self.on_select_region)
        v1.addWidget(self.lbl_region)
        v1.addWidget(btn_select)

        # 监控框：锁定（鼠标穿透）/ 解锁（可拖动跨屏移动）
        self.chk_lock_frame = QCheckBox("锁定监控框位置（取消勾选后可拖动、缩放）")
        self.chk_lock_frame.setChecked(True)
        self.chk_lock_frame.setToolTip(
            "勾选：监控框鼠标穿透，不影响操作微信\n"
            "取消勾选：可拖动监控框到任意屏幕、拖边角缩放、方向键微调（双击框内重新锁定）"
        )
        self.chk_lock_frame.toggled.connect(self.on_toggle_lock_frame)
        v1.addWidget(self.chk_lock_frame)

        row_scr = QHBoxLayout()
        self.combo_screen = QComboBox()
        for name, *_ in list_screens():
            self.combo_screen.addItem(name)
        row_scr.addWidget(self.combo_screen, 1)
        btn_move_scr = QPushButton("移到该屏")
        btn_move_scr.setToolTip("把监控框整体移动到所选屏幕（保持尺寸，居中放置）")
        btn_move_scr.clicked.connect(self.on_move_to_screen)
        row_scr.addWidget(btn_move_scr)
        v1.addLayout(row_scr)

        gb_basic = QGroupBox("基本设置")
        g1 = QGridLayout(gb_basic)
        g1.addWidget(QLabel("截图间隔(秒)"), 0, 0)
        self.spin_interval = QDoubleSpinBox()
        self.spin_interval.setRange(0.5, 30)
        self.spin_interval.setValue(float(self.cfg.get("interval_sec", 2.0)))
        g1.addWidget(self.spin_interval, 0, 1)
        self.rb_alert = QRadioButton("仅提醒（推荐先用）")
        self.rb_auto = QRadioButton("自动@用户回复")
        self.rb_alert.toggled.connect(self.on_mode_changed)
        if self.cfg.get("mode") == "auto":
            self.rb_auto.setChecked(True)
        else:
            self.rb_alert.setChecked(True)
        g1.addWidget(self.rb_alert, 1, 0, 1, 2)
        g1.addWidget(self.rb_auto, 2, 0, 1, 2)
        g1.addWidget(QLabel("回复内容"), 3, 0)
        self.edit_reply = QLineEdit(self.cfg.get("reply_text", "请注意言辞"))
        g1.addWidget(self.edit_reply, 3, 1)
        self.combo_repeat = QComboBox()
        self.combo_repeat.addItem("不重复提醒（本次运行只弹一次）", 0)
        self.combo_repeat.addItem("冷却 10 分钟后若再出现则再提醒", 600)
        self.combo_repeat.addItem("冷却 30 分钟后若再出现则再提醒", 1800)
        self.combo_repeat.addItem("每次重新出现都提醒", -1)
        saved_cd = int(self.cfg.get("repeat_alert_cooldown_sec", 0))
        idx = self.combo_repeat.findData(saved_cd)
        self.combo_repeat.setCurrentIndex(idx if idx >= 0 else 0)
        g1.addWidget(QLabel("同一内容重复出现"), 5, 0)
        g1.addWidget(self.combo_repeat, 5, 1)
        self.chk_frame = QCheckBox("屏幕上显示监控框（鼠标可穿透）")
        self.chk_frame.setChecked(True)
        self.chk_frame.toggled.connect(self.on_toggle_frame)
        g1.addWidget(self.chk_frame, 6, 0, 1, 2)

        gb_llm = QGroupBox("大模型判定（OpenAI 兼容接口）")
        v2 = QVBoxLayout(gb_llm)
        self.chk_llm = QRadioButton("启用大模型（用于第二级语义复核）")
        self.chk_llm.setChecked(bool(self.cfg["llm"].get("enabled")))
        v2.addWidget(self.chk_llm)
        g2 = QGridLayout()
        g2.addWidget(QLabel("接口预设"), 0, 0)
        self.combo_preset = QComboBox()
        self.combo_preset.addItems([
            "自定义",
            "豆包(火山方舟)",
            "DeepSeek",
            "智谱 GLM",
            "Moonshot",
        ])
        self.combo_preset.currentTextChanged.connect(self.on_preset)
        g2.addWidget(self.combo_preset, 0, 1)
        g2.addWidget(QLabel("接口地址"), 1, 0)
        self.edit_baseurl = QLineEdit(self.cfg["llm"].get("base_url", ""))
        self.edit_baseurl.setPlaceholderText("https://ark.cn-beijing.volces.com/api/v3")
        g2.addWidget(self.edit_baseurl, 1, 1)
        g2.addWidget(QLabel("API Key"), 2, 0)
        self.edit_apikey = QLineEdit(self.cfg["llm"].get("api_key", ""))
        self.edit_apikey.setEchoMode(QLineEdit.Password)
        g2.addWidget(self.edit_apikey, 2, 1)
        g2.addWidget(QLabel("模型/接入点"), 3, 0)
        self.edit_model = QLineEdit(self.cfg["llm"].get("model", ""))
        self.edit_model.setPlaceholderText("豆包填接入点ID：ep-2024xxxxxx-xxxxx")
        g2.addWidget(self.edit_model, 3, 1)
        hint = QLabel("豆包(火山方舟)的“模型”必须填控制台创建的推理接入点 ID（ep- 开头），不是模型名。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#854F0B;")
        g2.addWidget(hint, 4, 0, 1, 2)
        v2.addLayout(g2)
        btn_test = QPushButton("测试连接")
        btn_test.clicked.connect(self.on_test_llm)
        v2.addWidget(btn_test)

        gb_kw = QGroupBox("第一级：规则库（命中即判，零成本秒判）")
        v3 = QVBoxLayout(gb_kw)
        tip = QLabel("每行一条正则，可用 | 分隔多个词；行首 [类别] 指定归属，未写类别的行沿用上一类别。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#5A5A5A;")
        v3.addWidget(tip)
        self.edit_rules = QPlainTextEdit(rules_to_text(self.cfg))
        self.edit_rules.setMinimumHeight(150)
        v3.addWidget(self.edit_rules)
        row_spam = QHBoxLayout()
        row_spam.addWidget(QLabel("刷屏阈值：同一内容同屏出现"))
        self.spin_spam = QSpinBox()
        self.spin_spam.setRange(2, 20)
        self.spin_spam.setValue(int(self.cfg.get("spam_repeat", 3)))
        row_spam.addWidget(self.spin_spam)
        row_spam.addWidget(QLabel("次即判不当"))
        row_spam.addStretch(1)
        v3.addLayout(row_spam)

        gb_weak = QGroupBox("第二级：大模型复核范围")
        v4 = QVBoxLayout(gb_weak)
        self.combo_mode = QComboBox()
        self.combo_mode.addItem("只复核可疑消息（推荐，省调用）", "suspicious")
        self.combo_mode.addItem("全部消息都送大模型（最准，费调用）", "all")
        self.combo_mode.addItem("不用大模型，只用规则库", "off")
        idx = self.combo_mode.findData(self.cfg.get("llm_mode", "suspicious"))
        self.combo_mode.setCurrentIndex(idx if idx >= 0 else 0)
        v4.addWidget(self.combo_mode)
        lbl_weak = QLabel("可疑信号词（含这些词但未命中规则的消息，送大模型复核）：")
        lbl_weak.setWordWrap(True)
        v4.addWidget(lbl_weak)
        self.edit_weak = QPlainTextEdit("\n".join(self.cfg.get("weak_signals", [])))
        self.edit_weak.setMaximumHeight(90)
        v4.addWidget(self.edit_weak)

        btn_selftest = QPushButton("自测一段文字（看它会怎么判）")
        btn_selftest.clicked.connect(self.on_selftest)
        v4.addWidget(btn_selftest)

        btn_rejudge = QPushButton("重判当前屏幕内容")
        btn_rejudge.setToolTip("把监控区内当前可见的文字全部重新判定一遍（用于开始监控后屏幕上已有的消息）")
        btn_rejudge.clicked.connect(self.on_rejudge)

        # 顶部操作区：保存/开始/停止 常驻顶部，滚动设置项时也始终可见性最好
        btn_save = QPushButton("保存设置")
        btn_save.clicked.connect(self.on_save_settings)
        btn_start = QPushButton("开始监控")
        btn_start.setStyleSheet("color: #fff; background: #0F6E56;")
        btn_start.clicked.connect(self.on_start)
        btn_stop = QPushButton("停止监控")
        btn_stop.setStyleSheet("color: #fff; background: #993C1D;")
        btn_stop.clicked.connect(self.on_stop)
        row_actions = QHBoxLayout()
        row_actions.addWidget(btn_save, 1)
        row_actions.addWidget(btn_start, 1)
        row_actions.addWidget(btn_stop, 1)
        left.addLayout(row_actions)

        left.addWidget(gb_region)
        left.addWidget(gb_basic)
        left.addWidget(gb_llm)
        left.addWidget(gb_kw)
        left.addWidget(gb_weak)
        left.addWidget(btn_rejudge)
        left.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(left_panel)
        scroll.setMinimumWidth(430)
        layout.addWidget(scroll, 0)

        # ---------- 右侧：消息区 ----------
        self.tabs = QTabWidget()
        self.list_frame = QListWidget()
        self.list_frame.setWordWrap(True)
        self.list_stream = QListWidget()
        self.list_stream.setWordWrap(True)
        self.table_alert = QTableWidget(0, 5)
        self.table_alert.setHorizontalHeaderLabels(["时间", "判定", "内容", "原因", "动作"])
        self.table_alert.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table_alert.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tabs.addTab(self.list_frame, "本帧识别")
        self.tabs.addTab(self.list_stream, "消息判定流")
        self.tabs.addTab(self.table_alert, "报警记录")

        # left 布局已属于 left_panel（滚动区内容），不能再 addLayout 进主布局，
        # 否则 Qt 报 "layout already has a parent" 警告
        layout.addWidget(self.tabs, 2)
        central.setLayout(layout)
        self.setCentralWidget(central)
        self.statusBar().showMessage("就绪。先圈选区域，再开始监控。")

        # 报警记录相关操作：放在左侧底部，紧邻其它操作按钮
        row_alerts = QHBoxLayout()
        btn_refresh = QPushButton("刷新报警记录")
        btn_refresh.clicked.connect(self.load_alerts)
        row_alerts.addWidget(btn_refresh, 1)
        btn_clear = QPushButton("清空报警记录")
        btn_clear.setToolTip("删除已经保存的全部报警记录（数据库清空，不可恢复）")
        btn_clear.clicked.connect(self.on_clear_alerts)
        row_alerts.addWidget(btn_clear, 1)
        left.addLayout(row_alerts)

    # ---------------------------------------------------------------- 槽函数

    def on_clear_alerts(self):
        from PySide6.QtWidgets import QMessageBox
        r = QMessageBox.question(
            self, "确认清空",
            "将删除全部报警记录，且不可恢复。\n是否继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        n = storage.clear_events()
        # 同时重置本次运行的去重记录，便于同一内容重新验证提醒/自动回复
        self._alerted.clear()
        self._alerted_recent.clear()
        self.load_alerts()
        self.statusBarMessage(f"已清空 {n} 条报警记录")

    def on_select_region(self):
        if self.monitor and self.monitor.running:
            self.statusBarMessage("请先停止监控再重新圈选区域")
            return
        self.selector = RegionSelector(self.cfg.get("region"))
        self.selector.regionSelected.connect(self.on_region_selected)
        self.selector.cancelled.connect(lambda: self.statusBarMessage("已取消圈选"))
        self.selector.show_overlay()

    def _refresh_region_label(self):
        """刷新区域信息：坐标、尺寸、所在屏幕"""
        r = self.cfg.get("region")
        if not r:
            self.lbl_region.setText("未设置（点击下方按钮圈选）")
            return
        x, y, w, h = r
        idx = screen_of_region(r)
        scrs = list_screens()
        scr_name = scrs[idx][0].split("  ")[0] if scrs else "—"
        self.lbl_region.setText(f"({x}, {y})  {w} x {h}\n当前位于 {scr_name}")

    def on_region_selected(self, rect):
        self.cfg["region"] = list(rect)
        save_config(self.cfg)
        self.frame.set_region(rect)
        self.frame.set_label("监控区域")
        self._refresh_region_label()
        idx = screen_of_region(rect)
        if 0 <= idx < self.combo_screen.count():
            self.combo_screen.setCurrentIndex(idx)
        if self.chk_frame.isChecked():
            self.frame.show()
        self.statusBarMessage("监控区域已设置，区域框已保留在屏幕上")

    def on_toggle_lock_frame(self, checked):
        self.frame.set_locked(checked)
        if checked:
            self.statusBarMessage("监控框已锁定（鼠标穿透）")
        else:
            self.statusBarMessage(
                "监控框已解锁：拖动移动 / 拖边角缩放 / 方向键微调 / 双击框内锁定（跨屏可直接拖过去）"
            )

    def on_frame_lock_changed(self, locked):
        self.chk_lock_frame.blockSignals(True)
        self.chk_lock_frame.setChecked(locked)
        self.chk_lock_frame.blockSignals(False)

    def on_frame_region_changed(self, rect):
        """拖动/缩放/跨屏移动监控框后：保存配置并让监控线程热更新区域"""
        self.cfg["region"] = list(rect)
        save_config(self.cfg)
        self._refresh_region_label()
        if self.monitor and self.monitor.running:
            self.monitor.set_region(rect)
        self.statusBarMessage(
            f"监控区域已更新：({rect[0]}, {rect[1]}) {rect[2]}x{rect[3]}"
            + ("（监控中，已自动切换到新区域）" if self.monitor and self.monitor.running else "")
        )

    def on_move_to_screen(self):
        if not self.cfg.get("region"):
            QMessageBox.information(self, "提示", "请先圈选监控区域")
            return
        idx = self.combo_screen.currentIndex()
        rect = self.frame.move_to_screen(idx)
        if rect is None:
            return
        scrs = list_screens()
        if idx < len(scrs) and abs(scrs[idx][5] - 1.0) > 0.01:
            QMessageBox.information(
                self, "注意",
                f"该屏幕缩放为 {int(scrs[idx][5] * 100)}%（非 100%）。\n"
                "若发现识别区域与实际错位，请把该屏缩放调为 100%，或重新圈选一次。",
            )
        self.on_frame_region_changed(rect)

    def on_toggle_frame(self):
        if self.chk_frame.isChecked() and self.cfg.get("region"):
            self.frame.set_region(tuple(self.cfg["region"]))
            self.frame.show()
        else:
            self.frame.hide()

    def on_mode_changed(self):
        self.cfg["mode"] = "auto" if self.rb_auto.isChecked() else "alert"

    def on_save_settings(self):
        self.cfg["interval_sec"] = self.spin_interval.value()
        self.cfg["reply_text"] = self.edit_reply.text() or "请注意言辞"
        self.cfg["repeat_alert_cooldown_sec"] = self.combo_repeat.currentData()
        self.cfg["llm"]["enabled"] = self.chk_llm.isChecked()
        self.cfg["llm"]["base_url"] = self.edit_baseurl.text().strip()
        self.cfg["llm"]["api_key"] = self.edit_apikey.text().strip()
        self.cfg["llm"]["model"] = self.edit_model.text().strip()
        parsed_rules = parse_rules_text(self.edit_rules.toPlainText())
        import re as _re
        bad = []
        for cat, pats in parsed_rules.items():
            ok_pats = []
            for p in pats:
                try:
                    _re.compile(p)
                    ok_pats.append(p)
                except _re.error as e:
                    bad.append(f"[{cat}] {p}  （{e}）")
            parsed_rules[cat] = ok_pats
        self.cfg["rules"] = parsed_rules
        if bad:
            QMessageBox.warning(
                self, "部分规则有误已跳过",
                "以下规则不是合法的正则表达式，已跳过（其余已保存）：\n\n" + "\n".join(bad[:10]),
            )
        self.cfg["spam_repeat"] = self.spin_spam.value()
        self.cfg["llm_mode"] = self.combo_mode.currentData() or "suspicious"
        self.cfg["weak_signals"] = [
            s.strip() for s in self.edit_weak.toPlainText().splitlines() if s.strip()
        ]
        save_config(self.cfg)
        self.statusBarMessage("设置已保存")

    def on_selftest(self):
        from PySide6.QtWidgets import QInputDialog
        self.on_save_settings()
        text, ok = QInputDialog.getMultiLineText(
            self, "判定自测", "输入一句群消息（可粘贴真实广告原文）：", ""
        )
        if not ok or not text.strip():
            return
        from app.judge import Judge
        try:
            res = Judge().judge([{"text": text.strip(), "x": 0, "y": 0, "w": 1, "h": 1, "repeat": 1}])
        except Exception as e:
            QMessageBox.warning(self, "自测失败", str(e))
            return
        m = res[0]
        tag = VERDICT_TAG.get(m["verdict"], "[未知]")
        src = m.get("source", "")
        if src.startswith("rule:"):
            src_txt = "第一级规则库"
        elif src == "llm":
            src_txt = "第二级大模型"
        else:
            src_txt = "未命中"
        detail = (
            f"结果：{tag}\n"
            f"判定来源：{src_txt}（{src or '无'}）\n"
            f"原因：{m.get('reason') or '—'}\n\n"
            f"原文：{text.strip()}"
        )
        QMessageBox.information(self, "自测结果", detail)

    def on_start(self):
        if not self.cfg.get("region"):
            QMessageBox.warning(self, "提示", "请先圈选监控区域")
            return
        if self.monitor and self.monitor.running:
            return
        self.on_save_settings()
        self.monitor = MonitorThread(tuple(self.cfg["region"]))
        self.monitor.newBatch.connect(self.on_new_batch)
        self.monitor.liveFrame.connect(self.on_live_frame)
        self.monitor.errorMsg.connect(self.statusBarMessage)
        self.monitor.start()
        self.frame.set_label("监控中")
        if self.chk_frame.isChecked():
            self.frame.show()
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.list_stream.insertItem(
            0,
            f"{ts}  [系统]  监控已开始。开始时屏幕上已有的内容视为历史消息，"
            "只有之后新出现的消息会在这里判定；可点左侧「重判当前屏幕内容」把手头这屏也判一遍。",
        )
        self.statusBarMessage("监控已开始" + ("（自动@模式）" if self.rb_auto.isChecked() else ""))
        if self.tray:
            self.tray.setToolTip("微信群言论监控 - 运行中")

    def on_rejudge(self):
        if not (self.monitor and self.monitor.running):
            self.statusBarMessage("请先开始监控，再使用重判")
            return
        self.monitor.rejudge_once()
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.list_stream.insertItem(0, f"{ts}  [系统]  正在重判当前屏幕内容……")
        self.statusBarMessage("已请求重判当前屏幕，下一帧（约1~2秒后）生效")

    def on_stop(self):
        if self.monitor:
            self.monitor.stop()
            self.monitor.wait(3000)
            self.monitor = None
        self.frame.set_label("监控区域")
        self.statusBarMessage("监控已停止")
        if self.tray:
            self.tray.setToolTip("微信群言论监控 - 已停止")

    def on_live_frame(self, texts):
        self.list_frame.clear()
        for t in texts:
            self.list_frame.addItem(t)

    def stream_line(self, line):
        """往「消息判定流」里插一条系统消息（自动回复进度等）"""
        self.list_stream.insertItem(0, line)

    def on_new_batch(self, batch):
        for m in batch:
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            self.list_stream.insertItem(0, f"{ts}  [判定中]  {m['text']}")
        self.judge.submit(batch)

    def on_verdicts(self, results):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        for m in results:
            # 移除对应的「[判定中]」占位行，避免判定流里同一消息出现两条
            norm = "".join(m["text"].split())
            for i in range(self.list_stream.count()):
                it = self.list_stream.item(i)
                if "[判定中]" in it.text() and norm in "".join(it.text().split()):
                    self.list_stream.takeItem(i)
                    break
            tag = VERDICT_TAG.get(m["verdict"], "[未知]")
            line = f"{ts}  {tag}  {m['text']}"
            if m.get("reason"):
                line += f"（{m['reason']}）"
            elif m.get("source") == "llm":
                line += "（大模型复核：正常）"
            self.list_stream.insertItem(0, line)
            if m["verdict"] in ("warn", "severe"):
                self.handle_alert(m)

    def handle_alert(self, m):
        key = "".join(m["text"].split())  # 与引擎指纹一致：去空白归一化
        now0 = time.time()
        cd = int(self.cfg.get("repeat_alert_cooldown_sec", 0))
        prev = self._alerted.get(key)
        if prev is not None:
            # cd == 0：本次运行只提醒一次；cd > 0：冷却期内不重复提醒；cd < 0：每次都提醒
            if cd == 0 or (cd > 0 and now0 - prev < cd):
                storage.add_event(m["text"], m["verdict"], m.get("reason", ""), "重复出现，未重复提醒", "")
                self.load_alerts()
                return
        # 近距离片段去重：OCR 把同一条消息拆成多行时（如长网址折行），
        # 各片段会作为不同指纹各自报警。与 2 分钟内已提醒的内容满足以下任一条件，
        # 视为同一消息：① 文本互为包含；② 屏幕位置垂直相邻（折行的上下两行）。
        now = time.time()
        x, y, w, h = m.get("x", 0), m.get("y", 0), m.get("w", 0), m.get("h", 20)
        for t0, prev in list(self._alerted_recent.items()):
            if now - t0 > 120:
                del self._alerted_recent[t0]
                continue
            pk, px, py, pw, ph = prev.get("key", ""), prev.get("x", 0), prev.get("y", -9999), prev.get("w", 0), prev.get("h", 20)
            contained = key in pk or pk in key
            adjacent = abs(y - py) <= 3.0 * max(h, ph, 10)
            if pk == key:
                continue          # 完全同一句由上方冷却策略决定，不走片段逻辑
            if contained or adjacent:
                storage.add_event(m["text"], m["verdict"], m.get("reason", ""), "重复出现（同一消息片段），未重复提醒", "")
                self.load_alerts()
                return
        self._alerted[key] = now0
        self._alerted_recent[now] = {"key": key, "x": x, "y": y, "w": w, "h": h}

        shot = ""
        try:
            if self.monitor:
                shot = self.monitor.save_evidence() or ""
        except Exception:
            pass
        event_id = storage.add_event(m["text"], m["verdict"], m.get("reason", ""), "", shot)
        notify(self.tray, "不当言论提醒", f"{m['text'][:40]}\n原因：{m.get('reason','') or '见详情'}")
        self.load_alerts()

        mode = "auto" if self.rb_auto.isChecked() else "alert"
        if mode == "auto":
            self.statusBarMessage("检测到不当言论，正在自动@回复……")
            self.stream_line(f"{ts}  [系统]  正在自动@回复该消息……")
            self._responders.append(ResponderThread(m))
            resp = self._responders[-1]
            resp.done.connect(
                lambda msg, eid=event_id: self.on_responder_done(eid, msg))
            resp.start()
            storage.update_action(event_id, "auto")
        else:
            storage.update_action(event_id, "仅提醒")

    def on_responder_done(self, event_id, msg):
        storage.update_action(event_id, msg)
        self.statusBarMessage(msg)
        self.stream_line(f"[系统]  自动回复结果：{msg}")
        self.load_alerts()

    def on_test_llm(self):
        base = self.edit_baseurl.text().strip()
        key = self.edit_apikey.text().strip()
        model = self.edit_model.text().strip()
        if not (base and key):
            QMessageBox.information(self, "提示", "请先填写接口地址和 API Key")
            return
        self.statusBarMessage("正在测试大模型连接……")

        def _run():
            try:
                import requests
                r = requests.post(
                    base.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + key},
                    json={"model": model or "ep-placeholder",
                          "messages": [{"role": "user", "content": "回复ok"}],
                          "max_tokens": 8},
                    timeout=20,
                )
                if r.status_code == 200:
                    self.llmTestDone.emit(True, "连接成功：" + r.json()["choices"][0]["message"]["content"][:30])
                    return
                body = r.text[:400]
                tip = ""
                low = body.lower()
                if "model" in low and ("not found" in low or "不存在" in low or "invalid" in low):
                    tip = "（模型名/接入点 ID 不对：豆包请填 ep- 开头的接入点 ID）"
                elif "api key" in low or "unauthorized" in low or "authentication" in low:
                    tip = "（API Key 校验失败：检查 Key 是否完整、地域是否匹配）"
                self.llmTestDone.emit(False, f"HTTP {r.status_code}{tip}\n{body}")
            except requests.exceptions.SSLError as e:
                self.llmTestDone.emit(False, f"SSL 证书错误（公司网络/代理拦截）：{str(e)[:200]}")
            except requests.exceptions.ProxyError as e:
                self.llmTestDone.emit(False, f"代理错误：{str(e)[:200]}")
            except requests.exceptions.Timeout:
                self.llmTestDone.emit(False, "请求超时（20 秒），检查网络或代理设置")
            except requests.exceptions.ConnectionError as e:
                self.llmTestDone.emit(False, f"无法建立连接：{str(e)[:200]}")
            except Exception as e:
                self.llmTestDone.emit(False, f"{type(e).__name__}：{str(e)[:200]}")

        threading.Thread(target=_run, daemon=True).start()

    def on_llm_test_done(self, ok, msg):
        box = QMessageBox(self)
        box.setWindowTitle("连接测试结果")
        box.setText(("成功。\n" if ok else "失败。\n") + msg)
        box.setDetailedText(msg)
        box.setIcon(QMessageBox.Information if ok else QMessageBox.Warning)
        box.show()
        self.statusBarMessage("连接测试完成：" + ("成功" if ok else "失败，见弹窗详情"))

    def on_preset(self, name):
        presets = {
            "豆包(火山方舟)": ("https://ark.cn-beijing.volces.com/api/v3",
                               "豆包填接入点ID：ep-2024xxxxxx-xxxxx"),
            "DeepSeek": ("https://api.deepseek.com/v1", "deepseek-chat"),
            "智谱 GLM": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
            "Moonshot": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),
        }
        if name in presets:
            url, hint = presets[name]
            self.edit_baseurl.setText(url)
            self.edit_model.setPlaceholderText(hint)

    def load_alerts(self):
        rows = storage.list_events(200)
        self.table_alert.setRowCount(0)
        for r in rows:
            i = self.table_alert.rowCount()
            self.table_alert.insertRow(i)
            for j, val in enumerate(r[1:6]):
                self.table_alert.setItem(i, j, QTableWidgetItem(str(val or "")))

    def statusBarMessage(self, msg):
        self.statusBar().showMessage(str(msg))

    # ---------------------------------------------------------------- 退出

    def closeEvent(self, event):
        self.on_stop()
        self.frame.close()
        self.judge.stop()
        if self.tray:
            self.tray.hide()
        event.accept()
