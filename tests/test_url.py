"""复现"购买链接没识别"问题：URL 被 OCR 拆行/丢字后的合并与判定"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.engine import MonitorThread
from app.judge import Judge
from app.config import load_config

PASS = []


def check(name, cond, extra=""):
    if not cond:
        print(f"FAIL  {name}  {extra}")
        sys.exit(1)
    PASS.append(name)
    print(f"ok    {name}" + (f"  ({extra})" if extra else ""))


# ---------------------------------------------------------------- 行合并
# 模拟真实 OCR 输出：一条消息折成两行，左对齐、行距小
lines = [
    {"text": "请购买白鹿牌卫生纸购买连", "x": 100, "y": 200, "w": 220, "h": 18},
    {"text": "chat/3844187977637043", "x": 100, "y": 220, "w": 180, "h": 16},
    {"text": "马季，中行长插件", "x": 100, "y": 400, "w": 160, "h": 18},  # 独立消息，不合并
]
merged = MonitorThread._merge_lines(lines)
check("折行消息合并为一条", len(merged) == 2, f"{len(merged)} 行")
check("合并后文本拼接正确",
      any("请购买白鹿牌卫生纸购买连" in l["text"] and "chat/3844187977637043" in l["text"] for l in merged),
      merged[0]["text"])
check("独立消息不被合并", any(l["text"] == "马季，中行长插件" for l in merged))

# 三行折叠（URL 断成三段）
lines3 = [
    {"text": "请购买 白鹿牌卫生纸 购买连接：", "x": 50, "y": 300, "w": 200, "h": 18},
    {"text": "https://www.bailu", "x": 50, "y": 320, "w": 150, "h": 16},
    {"text": ".com/chat/38441879776370434", "x": 50, "y": 338, "w": 190, "h": 16},
]
m3 = MonitorThread._merge_lines(lines3)
check("三行折叠合并为一条", len(m3) == 1, f"{len(m3)} 行")
check("三行合并含完整 URL", "https://www.bailu.com/chat/38441879776370434" in m3[0]["text"], m3[0]["text"])

# ---------------------------------------------------------------- 判定
judge = Judge()
cfg = load_config()


def verdict(text):
    return Judge._judge_rules([{"text": text, "x": 0, "y": 0, "w": 1, "h": 1, "repeat": 1}], cfg)[0]


m = verdict("请购买白鹿牌卫生纸购买连chat/3844187977637043")
check("合并后的购买链接消息判为广告", m["verdict"] == "warn" and m.get("source", "").startswith("rule:ad"),
      f"{m['verdict']}/{m.get('source')} {m.get('reason')}")

m = verdict("请购买这款卫生纸")
check("「请购买」句式判为广告（不依赖URL）", m["verdict"] == "warn" and "请购买" in m.get("reason", ""),
      f"{m['verdict']}/{m.get('source')} {m.get('reason')}")

m = verdict("chat/3844187977637043")
check("URL 残片 chat/数字 判为广告", m["verdict"] == "warn" and "ad" in m.get("source", ""),
      f"{m['verdict']}/{m.get('source')}")

m = verdict("https://www.bailu.com/chat/38441879776370434?channel=microsoft")
check("完整 URL 判为广告", m["verdict"] == "warn" and "ad" in m.get("source", ""),
      f"{m['verdict']}/{m.get('source')}")

m = verdict("昨天我在超市买了包卫生纸")
check("正常聊天不误报", m["verdict"] in (None, "ok"), f"{m['verdict']} {m.get('reason')}")

m = verdict("这个团购我参加过，挺划算")
check("普通提及团购不算广告", m["verdict"] in (None, "ok"), f"{m['verdict']} {m.get('reason')}")

# 弱信号：购买类词汇进入 L2 复核范围
lines_weak = [{"text": "麻烦大家请购买这款产品", "x": 0, "y": 0, "w": 1, "h": 1, "repeat": 1}]
check("「请购买」触发弱信号送大模型复核", Judge._is_suspicious("麻烦大家请购买这款产品", cfg), "weak signal")

print(f"\nURL_TEST_OK: {len(PASS)} 项全部通过")
