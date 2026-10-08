"""冒烟测试：OCR 管线 + 判定逻辑 + 存储（不需要真实屏幕/网络）"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image, ImageDraw, ImageFont
import numpy as np


def test_ocr():
    from rapidocr_onnxruntime import RapidOCR
    img = Image.new("RGB", (600, 160), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 28)
    except OSError:
        font = ImageFont.load_default()
    d.text((20, 20), "大家好，今天天气不错", fill="black", font=font)
    d.text((20, 80), "你就是个废物东西", fill="black", font=font)
    arr = np.array(img)[:, :, ::-1].copy()  # RGB -> BGR
    ocr = RapidOCR()
    result, _ = ocr(arr)
    texts = [str(r[1]) for r in (result or [])]
    joined = "".join(texts)
    print("OCR 结果:", texts)
    assert "天气" in joined and "废物" in joined, "OCR 未识别出预期文本"
    print("TEST_OCR: PASS")


def test_judge_keywords():
    from app.judge import Judge
    j = Judge()
    batch = [
        {"text": "今天天气不错", "x": 1, "y": 1, "w": 1, "h": 1},
        {"text": "你就是个废物东西", "x": 1, "y": 2, "w": 1, "h": 1},
    ]
    results = j.judge(batch)
    assert results[0]["verdict"] == "ok", "正常消息被误判"
    assert results[1]["verdict"] == "warn", "不当消息未被识别"
    print("TEST_KEYWORD_JUDGE: PASS ->", results[1]["reason"])


def test_parse_json():
    from app.judge import Judge
    cases = [
        ('[{"i":0,"v":"ok","r":""}]', 1),
        ('```json\n[{"i":0,"v":"warn","r":"辱骂"}]\n```', 1),
        ('好的，以下是判定结果：\n[{"i":0,"v":"severe","r":"威胁"}]', 1),
        ('废话', 0),
    ]
    for text, expect in cases:
        got = len(Judge._parse_json(text))
        assert got == expect, f"解析失败: {text!r} -> {got}"
    print("TEST_PARSE_JSON: PASS")


def test_storage():
    from app import storage
    eid = storage.add_event("测试不当言论", "warn", "冒烟测试", "仅提醒", "")
    rows = storage.list_events(10)
    assert any(r[0] == eid for r in rows), "报警记录写入/读取失败"
    storage.update_action(eid, "已自动回复")
    rows = storage.list_events(10)
    row = next(r for r in rows if r[0] == eid)
    assert row[5] == "已自动回复", "动作更新失败"
    print("TEST_STORAGE: PASS")


if __name__ == "__main__":
    test_ocr()
    test_judge_keywords()
    test_parse_json()
    test_storage()
    print("\nALL_SMOKE_TESTS_PASSED")
