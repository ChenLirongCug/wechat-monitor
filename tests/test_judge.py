"""两级判定 + 增量去重 测试

运行：python tests/test_judge.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.judge as judge_mod
from app.config import DEFAULTS, parse_rules_text, rules_to_text
from app.engine import MonitorThread
from app.judge import Judge

ok_count = 0


def check(name, cond, extra=""):
    global ok_count
    if cond:
        ok_count += 1
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {extra}")
        raise AssertionError(name + " " + str(extra))


def msg(text, repeat=1):
    return {"text": text, "x": 0, "y": 0, "w": 1, "h": 1, "repeat": repeat}


def cfg_with(**kw):
    cfg = dict(DEFAULTS)
    cfg["llm"] = dict(DEFAULTS["llm"])
    cfg["llm"]["enabled"] = True
    cfg["llm"]["api_key"] = "test-key"
    cfg["llm"]["base_url"] = "https://example.com/v1"
    cfg["rules"] = {k: list(v) for k, v in DEFAULTS["rules"].items()}
    cfg["weak_signals"] = list(DEFAULTS["weak_signals"])
    cfg.update(kw)
    return cfg


class FakeResp:
    def __init__(self, content):
        self._c = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._c}}]}


def fake_llm(mapping):
    """mapping: 文本片段 -> (verdict, reason)；按输入顺序匹配"""

    def _post(url, headers=None, json=None, timeout=None):
        payload = json["messages"][1]["content"]
        items = []
        for it in __import__("json").loads(payload):
            v, r = "ok", ""
            for frag, (vv, rr) in mapping.items():
                if frag in it["t"]:
                    v, r = vv, rr
                    break
            items.append({"i": it["i"], "v": v, "r": r})
        return FakeResp(__import__("json").dumps(items, ensure_ascii=False))

    return _post


def run(cfg, batch, post=None):
    orig_load, orig_post = judge_mod.load_config, judge_mod.requests.post
    judge_mod.load_config = lambda: cfg
    if post is not None:
        judge_mod.requests.post = post
    else:
        def _boom(*a, **k):
            raise AssertionError("不应调用大模型")

        judge_mod.requests.post = _boom
    try:
        return Judge().judge([dict(m) for m in batch])
    finally:
        judge_mod.load_config, judge_mod.requests.post = orig_load, orig_post


print("\n[1] 第一级：规则库命中即判（不调用大模型）")
r = run(cfg_with(), [
    msg("你就是个傻逼吧"),
    msg("再闹我打死你"),
    msg("兼职刷单日赚300，加我微信 abc12345"),
    msg("这个说法很脏，我们讨论下脏话的用法"),
    msg("今天天气不错"),
])
check("辱骂命中 warn", r[0]["verdict"] == "warn" and r[0]["source"] == "rule:abuse", r[0])
check("威胁命中 severe", r[1]["verdict"] == "severe" and r[1]["source"] == "rule:threat", r[1])
check("广告命中 warn（这是本次修复的重点）", r[2]["verdict"] == "warn" and r[2]["source"] == "rule:ad", r[2])
check("引用脏话不入辱骂", r[3]["verdict"] == "ok", r[3])
check("正常消息 ok", r[4]["verdict"] == "ok", r[4])

print("\n[2] 广告各类形态")
ads = [
    "扫码进群领福利",
    "联系手机 13812345678",
    "正规贷款秒批，无抵押",
    "点击链接 https://xxx.top 查看",
]
r = run(cfg_with(), [msg(t) for t in ads])
for t, m in zip(ads, r):
    check(f"广告识别：{t}", m["verdict"] == "warn" and m["source"] == "rule:ad", m)

print("\n[3] 刷屏判定")
r = run(cfg_with(), [msg("走过路过不要错过", repeat=3)])
check("同屏重复3次判刷屏", r[0]["verdict"] == "warn" and r[0]["source"] == "rule:spam", r[0])
r = run(cfg_with(), [msg("大家好", repeat=2)])
check("重复2次不判", r[0]["verdict"] == "ok", r[0])

print("\n[4] 第二级：只复核可疑消息（suspicious 模式）")
called = {"n": 0}


def post(url, headers=None, json=None, timeout=None):
    called["n"] += json["messages"][1]["content"].count('"t"')
    return fake_llm({"内部渠道": ("warn", "引流推广")})(url, headers, json, timeout)


r = run(cfg_with(llm_mode="suspicious"), [
    msg("今天中午吃什么"),
    msg("我这边有内部渠道，需要的私聊"),
], post=post)
check("普通消息不送大模型", r[0]["verdict"] == "ok" and r[0]["source"] == "rule", r[0])
check("可疑消息送大模型并被判 warn", r[1]["verdict"] == "warn" and r[1]["source"] == "llm", r[1])
check("大模型只被调用于可疑消息", called["n"] == 1, called)

print("\n[5] 大模型复核可排除误伤")
r = run(cfg_with(llm_mode="suspicious"), [
    msg("这个优惠活动是官方的，大家可以看看"),
], post=fake_llm({"官方": ("ok", "")}))
check("可疑但正常 -> 大模型判 ok", r[0]["verdict"] == "ok" and r[0]["source"] == "llm", r[0])

print("\n[6] 模式开关")
r = run(cfg_with(llm_mode="off"), [msg("我这边有内部渠道，需要的私聊")])
check("off 模式不送大模型", r[0]["verdict"] == "ok" and r[0]["source"] == "rule", r[0])
called2 = {"n": 0}


def post2(url, headers=None, json=None, timeout=None):
    called2["n"] += json["messages"][1]["content"].count('"t"')
    return fake_llm({})(url, headers, json, timeout)


r = run(cfg_with(llm_mode="all"), [msg("今天中午吃什么"), msg("你好啊")], post=post2)
check("all 模式全部送大模型", called2["n"] == 2, called2)

print("\n[7] 引擎增量去重（广告反复在屏上不再重复判定）")
seen, now = {}, 1000.0
lines = [{"text": "扫码进群领福利", "x": 0, "y": 0, "w": 1, "h": 1}]
f1 = MonitorThread._pick_fresh([dict(l) for l in lines], seen, now, 2, False)
f2 = MonitorThread._pick_fresh([dict(l) for l in lines], seen, now + 2, 2, False)
check("首次出现 -> 判定", len(f1) == 1, f1)
check("仍在屏上 -> 不再重复判定", len(f2) == 0, f2)
MonitorThread._gc(seen, now + 5000, 600)   # 离屏超过 expire 后被回收
f3 = MonitorThread._pick_fresh([dict(l) for l in lines], seen, now + 5000, 2, False)
check("滚出屏幕并过期后重现 -> 重新判定", len(f3) == 1, f3)
lines3 = [{"text": "扫码进群领福利", "x": 0, "y": i, "w": 1, "h": 1} for i in range(3)]
seen2 = {}
f4 = MonitorThread._pick_fresh(lines3, seen2, 1000.0, 2, False)
check("同屏3条相同 -> 只有1条进入判定", len(f4) == 1, f4)
check("但带上 repeat=3 供刷屏判定", f4[0]["repeat"] == 3, f4[0])

print("\n[8] 规则库文本序列化往返")
cfg = cfg_with()
txt = rules_to_text(cfg)
back = parse_rules_text(txt)
check("序列化后类别条数一致", all(len(back[k]) == len(v) for k, v in cfg["rules"].items()), back)
check("广告规则仍在", any("兼职" in p for p in back["ad"]), back.get("ad"))
edited = parse_rules_text("[广告引流] 卖片|低价代写\n[辱骂攻击] 笨蛋")
check("手工编辑可解析", edited["ad"] == ["卖片|低价代写"] and edited["abuse"] == ["笨蛋"], edited)

print(f"\nALL_PASS ({ok_count} 项检查通过)")
