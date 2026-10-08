"""言论判定：两级策略

第一级（L1）：关键词/正则规则库 —— 零成本秒判
    辱骂攻击 / 人身威胁 / 广告引流 / 色情低俗 / 政治有害 / 刷屏
    命中即定级，不再送大模型。

第二级（L2）：大模型语义复核
    只处理 L1 未命中、但含「弱信号」的可疑消息（llm_mode=suspicious，默认）
    用于识别变体表达、软广引流、隐晦辱骂；同时排除误伤（如引用脏话讨论）。

判定结果 verdict：ok（正常）/ warn（不当）/ severe（严重不当）
"""
import json
import queue
import re

import requests
from PySide6.QtCore import QThread, Signal

from app.config import load_config, CATEGORY_NAMES

# 规则类别 -> 默认严重级
RULE_LEVEL = {
    "abuse": "warn",
    "threat": "severe",
    "ad": "warn",
    "porn": "severe",
    "political": "severe",
}
CAT_NAME = dict(CATEGORY_NAMES)
CAT_NAME["spam"] = "刷屏"

SYSTEM_PROMPT = (
    "你是微信群管理员助手，负责判断群聊消息是否属于不当言论。\n"
    "不当言论包括：辱骂攻击、人身威胁、色情低俗、政治有害、赌博诈骗、广告引流（含软广、"
    "兼职刷单、贷款办卡、联系方式招揽、外链推广）、恶意刷屏。\n"
    "注意区分：正常讨论、玩笑、引用或讨论某个不当词汇本身（如讨论脏话的用法、转发新闻标题）不算不当；"
    "群内正常的通知、答疑、工作沟通不算广告。\n"
    "对输入的每条消息（带序号 i）输出 JSON 数组，格式："
    '[{"i":0,"v":"ok|warn|severe","r":"简短原因"}]。\n'
    "拿不准就判 ok。正常消息 v 为 ok 且 r 为空。只输出 JSON 数组，不要输出任何其他内容。"
)


class Judge:
    """同步判定器：可被任何线程调用"""

    def judge(self, batch):
        """batch: list[dict(text,x,y,w,h,repeat)] -> list[dict(...,verdict,reason,source)]"""
        cfg = load_config()
        # ---------- L1：规则库 ----------
        results = self._judge_rules(batch, cfg)
        pending = [m for m in results if m["verdict"] is None]
        if not pending:
            return results

        # ---------- L2：大模型复核 ----------
        llm = cfg.get("llm", {})
        mode = cfg.get("llm_mode", "suspicious")
        usable = llm.get("enabled") and llm.get("api_key") and llm.get("base_url") and mode != "off"
        if usable:
            targets = pending if mode == "all" else [m for m in pending if self._is_suspicious(m["text"], cfg)]
            if targets:
                try:
                    reviewed = self._judge_llm(targets, llm)
                    if reviewed:
                        by_text = {id(t): r for t, r in zip(targets, reviewed)}
                        for m in pending:
                            r = by_text.get(id(m))
                            if r is not None:
                                m["verdict"] = r["verdict"]
                                m["reason"] = r["reason"]
                                m["source"] = "llm"
                except Exception as e:  # 大模型出错不应影响整体流程
                    for m in pending:
                        if m["verdict"] is None:
                            m["reason"] = "大模型复核失败：" + str(e)[:60]

        # 未命中规则且未送审 -> 正常
        for m in results:
            if m["verdict"] is None:
                m["verdict"] = "ok"
                m["reason"] = ""
                m["source"] = m["source"] or "rule"
        return results

    # ------------------------------------------------------------------ L1

    @staticmethod
    def _judge_rules(batch, cfg):
        rules = cfg.get("rules", {}) or {}
        compiled = {}
        for cat, pats in rules.items():
            pats_ok = []
            for p in (pats or []):
                if not p:
                    continue
                try:
                    pats_ok.append(re.compile(p, re.I))
                except re.error:
                    continue  # 非法正则跳过，不让单条规则拖垮整个判定
            if pats_ok:
                compiled[cat] = pats_ok
        # 兼容旧配置里的 keywords 列表，并入辱骂类
        extra = [k for k in (cfg.get("keywords") or []) if k]
        if extra:
            compiled.setdefault("abuse", []).append(
                re.compile("|".join(re.escape(k) for k in extra), re.I)
            )
        spam_n = int(cfg.get("spam_repeat", 3) or 3)

        out = []
        for m in batch:
            text = m.get("text", "") or ""
            m2 = dict(m)
            m2["verdict"] = None
            m2["reason"] = ""
            m2["source"] = ""

            hit_cat, hit_txt = None, None
            for cat, pats in compiled.items():
                for p in pats:
                    mm = p.search(text)
                    if mm:
                        hit_cat, hit_txt = cat, mm.group(0)
                        break
                if hit_cat:
                    break

            if hit_cat:
                m2["verdict"] = RULE_LEVEL.get(hit_cat, "warn")
                m2["reason"] = f"规则[{CAT_NAME.get(hit_cat, hit_cat)}]：{hit_txt}"
                m2["source"] = "rule:" + hit_cat
            elif int(m.get("repeat", 1) or 1) >= spam_n:
                m2["verdict"] = "warn"
                m2["reason"] = f"刷屏：同一内容同屏出现 {m['repeat']} 次"
                m2["source"] = "rule:spam"
            out.append(m2)
        return out

    @staticmethod
    def _is_suspicious(text, cfg):
        for pat in (cfg.get("weak_signals") or []):
            try:
                if re.search(pat, text, re.I):
                    return True
            except re.error:
                continue
        return False

    # ------------------------------------------------------------------ L2

    def _judge_llm(self, batch, llm):
        size = int(llm.get("batch_size", 20) or 20)
        verdicts = []
        for i in range(0, len(batch), size):
            chunk = batch[i:i + size]
            payload = [{"i": j, "t": m["text"]} for j, m in enumerate(chunk)]
            body = {
                "model": llm.get("model", "deepseek-chat"),
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                "temperature": 0,
            }
            r = requests.post(
                llm["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + llm["api_key"]},
                json=body,
                timeout=int(llm.get("timeout_sec", 20) or 20),
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            parsed = self._parse_json(content)
            vmap = {}
            for it in parsed:
                if isinstance(it, dict) and "i" in it:
                    vmap[int(it["i"])] = (
                        str(it.get("v", "ok")),
                        str(it.get("r", "")),
                    )
            for j, m in enumerate(chunk):
                v, reason = vmap.get(j, ("ok", ""))
                m2 = dict(m)
                m2["verdict"] = v if v in ("ok", "warn", "severe") else "ok"
                m2["reason"] = reason
                m2["source"] = "llm"
                verdicts.append(m2)
        return verdicts

    @staticmethod
    def _parse_json(content):
        content = content.strip()
        content = re.sub(r"^```(json)?", "", content).strip()
        content = re.sub(r"```$", "", content).strip()
        m = re.search(r"\[.*\]", content, re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []


class JudgeThread(QThread):
    """异步判定线程：从队列取批次，判定后发信号回主线程"""
    verdictReady = Signal(list)   # list[dict(text,x,y,w,h,verdict,reason,source)]
    statusMsg = Signal(str)

    def __init__(self):
        super().__init__()
        self._queue = queue.Queue(maxsize=50)
        self._running = False
        self._judge = Judge()

    def submit(self, batch):
        try:
            self._queue.put_nowait(batch)
        except queue.Full:
            pass  # 积压时丢弃，避免阻塞

    def start(self):
        self._running = True
        super().start()

    def stop(self):
        self._running = False
        self._queue.put(None)

    def run(self):
        while self._running:
            item = self._queue.get()
            if item is None:
                break
            try:
                results = self._judge.judge(item)
                self.verdictReady.emit(results)
            except Exception as e:
                self.statusMsg.emit(f"判定失败：{e}")
