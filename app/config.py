"""配置管理：config.json 读写 + 默认值"""
import json
import os
import re
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DATA_DIR = os.path.join(BASE_DIR, "data")
EVIDENCE_DIR = os.path.join(DATA_DIR, "evidence")
DB_PATH = os.path.join(DATA_DIR, "monitor.db")

DEFAULTS = {
    "region": None,
    "interval_sec": 2.0,
    "mode": "alert",
    "reply_text": "请注意言辞",
    "min_text_len": 2,
    "dedup_expire_sec": 600,
    # 同一违规内容再次出现时是否重复提醒（秒）：
    #   0   = 本次运行只提醒一次（默认）
    #   600 = 冷却 10 分钟后若再次出现则再提醒
    #   -1  = 每次重新出现都提醒
    "repeat_alert_cooldown_sec": 0,
    "llm": {
        "enabled": False,
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "timeout_sec": 20,
        "batch_size": 20,
    },
    "keywords": ["傻逼", "去死", "滚出去", "废物东西", "操你", "全家死"],
    "menu_keywords": ["提醒", "@TA", "提到", "＠"],
    # 第一级规则库：命中即判，零成本秒判。每条为正则，可用 | 分隔多个词
    "rules": {
        # 辱骂攻击 —— warn
        "abuse": [
            r"傻[逼B比]|煞笔|沙雕|智障|脑残|弱智|贱[人货]|滚出[去]|去死|废物|狗东西|妈的|草泥马|操你|日你|全家死|死全家|有病吧|神经病",
        ],
        # 人身威胁 —— severe
        "threat": [
            r"打死你|弄死你|杀了你|砍死|弄残|小心点|等着瞧|找人收拾你|弄死全家",
        ],
        # 广告引流 —— warn（联系方式 / 推广 / 兼职刷单 / 贷款 / 外链）
        "ad": [
            r"(微信|VX|vx|weixin|威信|薇信|QQ|qq|扣扣|q号)\s*[:：号]?\s*[0-9a-zA-Z_-]{5,}",
            r"1[3-9]\d{9}",
            r"(加|添)\s*(我|下|个)?\s*(微信|好友|群|V)",
            r"扫码|扫一扫|二维码|长按识别|识别图中|进群|拉[你我他]进群|拉人|群号",
            r"兼职|刷单|返利|日赚|日入|月入|躺赚|招代理|代理招|加盟|引流|推广|薅羊毛|砍一刀|助力|点赞赚钱",
            r"博彩|赌场|六合彩|时时彩|开户送|注册送|首存|流水返|菠菜|下注",
            r"贷款|办卡|额度|秒批|低息|无抵押|征信修复|套现|花呗套",
            r"https?://|www\.|t\.cn|\.top\b|\.xyz\b|m\.tb\.cn|dwz\.cn",
            # OCR 常把长网址拆断/丢字，补 URL 残片形态：chat/3844187977637043、xxx.com/xxx
            r"\b[a-zA-Z][a-zA-Z0-9_-]{1,20}/\d{10,}",
            r"\.(com|cn|net|top|xyz|vip|shop)/\S",
            # 购物推销句式（"请购买xx 购买连接：…"）
            r"请购买|购买连[接接]?|购买链接|下单链?接?|购买地址|购买通道|代购|秒杀|拼团|团购链",
        ],
        # 色情低俗 —— severe
        "porn": [
            r"约炮|一夜情|裸聊|福利姬|上门服务|包夜|色情|淫秽|约吗|找小姐",
        ],
        # 政治有害 —— severe（可按群性质自行增删）
        "political": [
            r"法轮功|falun|台独|藏独|疆独|港独|推翻国家|颠覆国家政权",
        ],
    },
    # 刷屏：同一内容在同一屏内出现达到该次数即判 warn
    "spam_repeat": 3,
    # 第二级：未命中规则但含以下弱信号的消息，送大模型语义复核
    "weak_signals": [
        r"免费|优惠|折扣|活动|报名|名额|限时|福利|领取|红包|中奖",
        r"投资|赚钱|收益|涨幅|带单|老师|课程|培训|招生|加盟",
        r"私聊|联系|电话|咨询|客服|有意者|需要的|感兴趣的",
        r"http|链接|网址|点击|打开|详情|购买|下单|代购|拍下",
        r"广告|推广|宣传|业务|合作|代理|货源|厂家|批发",
    ],
    # 大模型介入方式：off=不用大模型 | suspicious=只复核可疑消息（推荐） | all=全部消息都送大模型
    "llm_mode": "suspicious",
}

CATEGORY_NAMES = {
    "abuse": "辱骂攻击",
    "threat": "人身威胁",
    "ad": "广告引流",
    "porn": "色情低俗",
    "political": "政治有害",
}
NAME_TO_CAT = {v: k for k, v in CATEGORY_NAMES.items()}
# 只认这 5 个类别名做标记，避免把规则内部的方括号（如 傻[逼B比]、[0-9]）误当类别
_NAME_ALT = "|".join(re.escape(n) for n in NAME_TO_CAT)


def rules_to_text(cfg):
    """规则库 -> 可编辑文本：每行 [类别] 正则"""
    rules = cfg.get("rules") or {}
    out = []
    for cat, name in CATEGORY_NAMES.items():
        for pat in (rules.get(cat) or []):
            out.append(f"[{name}] {pat}")
    return "\n".join(out)


def parse_rules_text(text):
    """可编辑文本 -> 规则库（未归类行归入 abuse）

    注意：QTextEdit.toPlainText() 的行分隔可能是 Unicode 段落符 U+2029，需统一成 \n
    """
    rules = {cat: [] for cat in CATEGORY_NAMES}
    norm = text or ""
    for sep in ("\u2029", "\u2028", "\r\n", "\r"):
        norm = norm.replace(sep, "\n")

    def _push(cat, body):
        """换行正常时按行切；换行丢失（被压成一行）时退化为按空白切"""
        chunks = body.split("\n") if "\n" in body else re.split(r"\s+", body)
        for c in chunks:
            c = c.strip()
            if c:
                rules[cat].append(c)

    if "\n" not in norm.strip():
        # 换行丢失的降级路径：按已知类别名标记切分
        parts = re.split(r"\[(" + _NAME_ALT + r")\]", norm)
        cur = "abuse"
        _push(cur, parts[0])
        for i in range(1, len(parts) - 1, 2):
            _push(NAME_TO_CAT[parts[i]], parts[i + 1])
        return rules

    cur = "abuse"
    for raw in norm.split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^\[(" + _NAME_ALT + r")\]\s*(.*)$", line)
        if m:
            cur = NAME_TO_CAT[m.group(1)]
            line = m.group(2).strip()
        if line:
            rules[cur].append(line)
    return rules


_lock = threading.Lock()


def load_config():
    with _lock:
        cfg = _deep_copy(DEFAULTS)
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                _merge(cfg, saved)
            except Exception:
                pass
        return cfg


def save_config(cfg):
    with _lock:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)


def _deep_copy(d):
    out = {}
    for k, v in d.items():
        out[k] = _deep_copy(v) if isinstance(v, dict) else v
    return out


def _merge(base, override):
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
