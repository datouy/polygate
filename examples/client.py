"""polygate Python 客户端示例。

用法：
    export POLYGATE_BASE=http://127.0.0.1:8000
    export POLYGATE_KEY=change-me
    python examples/client.py
"""

import os

import httpx

BASE = os.environ.get("POLYGATE_BASE", "http://127.0.0.1:8000")
KEY = os.environ.get("POLYGATE_KEY", "change-me")


def chat(model: str, messages: list[dict], stream: bool = False) -> None:
    headers = {"Authorization": f"Bearer {KEY}"}
    payload = {"model": model, "messages": messages, "stream": stream}
    with httpx.stream("POST", f"{BASE}/v1/chat/completions", headers=headers, json=payload, timeout=120) as r:
        r.raise_for_status()
        if stream:
            for line in r.iter_lines():
                if line.startswith("data:"):
                    print(line)
        else:
            data = r.json()
            print(data["choices"][0]["message"]["content"])


def list_models() -> None:
    headers = {"Authorization": f"Bearer {KEY}"}
    r = httpx.get(f"{BASE}/v1/models", headers=headers)
    r.raise_for_status()
    for m in r.json()["data"]:
        print(f"- {m['id']}  (provider: {m['owned_by']})")


if __name__ == "__main__":
    list_models()
    print("---")
    chat("deepseek-chat", [{"role": "user", "content": "用一句话介绍你自己"}])
