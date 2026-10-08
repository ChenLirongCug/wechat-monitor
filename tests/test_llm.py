"""大模型连接诊断脚本

用法（命令行）：
    python tests/test_llm.py            # 读取 config.json 中的配置并测试
    python tests/test_llm.py <base_url> <api_key> <model>   # 直接指定参数测试

会打印请求地址、HTTP 状态码和接口返回的完整错误信息，便于定位问题。
"""
import json
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import CONFIG_PATH, load_config  # noqa: E402

BASE_URL = ""
API_KEY = ""
MODEL = ""

if len(sys.argv) == 4:
    BASE_URL, API_KEY, MODEL = sys.argv[1:4]
else:
    cfg = load_config()
    llm = cfg.get("llm", {})
    BASE_URL = llm.get("base_url", "")
    API_KEY = llm.get("api_key", "")
    MODEL = llm.get("model", "")


def main():
    if not BASE_URL or not API_KEY:
        print("config.json 中缺少 base_url 或 api_key，请先在界面里填写并保存设置。")
        return

    url = BASE_URL.rstrip("/") + "/chat/completions"
    print(f"请求地址: {url}")
    print(f"模型/接入点: {MODEL or '(空)'}")
    if not MODEL:
        print("提示: 火山方舟(豆包)要求填写接入点 ID，形如 ep-2024xxxxxx-xxxxx\n")

    body = {
        "model": MODEL or "ep-placeholder",
        "messages": [{"role": "user", "content": "回复ok"}],
        "max_tokens": 8,
    }
    try:
        r = requests.post(
            url,
            headers={"Authorization": "Bearer " + API_KEY},
            json=body,
            timeout=20,
        )
    except requests.exceptions.SSLError as e:
        print("失败: SSL 证书错误（公司网络/代理拦截）->", str(e)[:300])
        return
    except requests.exceptions.ProxyError as e:
        print("失败: 代理错误 ->", str(e)[:300])
        return
    except requests.exceptions.Timeout:
        print("失败: 请求超时，检查网络或代理")
        return
    except requests.exceptions.ConnectionError as e:
        print("失败: 无法建立连接 ->", str(e)[:300])
        return
    except Exception as e:
        print("失败:", type(e).__name__, str(e)[:300])
        return

    print(f"HTTP 状态码: {r.status_code}")
    print("返回内容:", r.text[:600])

    if r.status_code == 200:
        print("\n结论: 连接成功，模型和 API Key 都可用。")
        return

    text = r.text.lower()
    if "model" in text and ("not found" in text or "不存在" in text or "invalid" in text):
        print("\n结论: 模型名/接入点 ID 不对。豆包请填火山方舟控制台里的接入点 ID（ep- 开头）。")
    elif "api key" in text or "unauthorized" in text or "authentication" in text:
        print("\n结论: API Key 校验失败，请检查 Key 是否完整、是否属于该地域(华北2-北京)。")
    elif "permission" in text or "forbidden" in text:
        print("\n结论: 无权限，接入点可能未开通或未绑定该模型。")
    else:
        print("\n结论: 接口返回错误，请按上方返回内容排查。")


if __name__ == "__main__":
    main()
