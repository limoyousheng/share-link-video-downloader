from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

import uvicorn


HOST = os.getenv("DOUYIN_APP_HOST", "0.0.0.0").strip() or "0.0.0.0"


def find_port() -> int:
    configured = os.getenv("DOUYIN_APP_PORT")
    try:
        ports = [int(configured)] if configured else list(range(8765, 8776))
    except ValueError as exc:
        raise RuntimeError("DOUYIN_APP_PORT 必须是 1 到 65535 之间的整数。") from exc
    for port in ports:
        if not 1 <= port <= 65535:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((HOST, port))
                return port
            except OSError:
                continue
    if configured:
        raise RuntimeError(f"端口 {configured} 不可用，请更换 DOUYIN_APP_PORT 后重试。")
    raise RuntimeError("8765-8775 端口均被占用，请关闭占用程序后重试。")


def open_when_ready(health_url: str, display_url: str) -> None:
    for _ in range(100):
        try:
            with urllib.request.urlopen(health_url, timeout=1) as response:
                if response.status == 200:
                    try:
                        if sys.platform == "win32":
                            os.startfile(display_url)  # type: ignore[attr-defined]
                        elif not webbrowser.open(display_url):
                            print(f"无法自动打开浏览器，请手动访问：{display_url}")
                    except OSError:
                        if not webbrowser.open(display_url):
                            print(f"无法自动打开浏览器，请手动访问：{display_url}")
                    return
        except Exception:
            time.sleep(0.15)
    print(f"服务已启动，但浏览器未自动打开。请手动访问：{display_url}")


if __name__ == "__main__":
    port = find_port()
    health_url = f"http://127.0.0.1:{port}/api/v1/health"
    public_url = os.getenv("DOUYIN_PUBLIC_URL", "").strip().rstrip("/")
    visible_host = "127.0.0.1" if HOST in {"0.0.0.0", "::"} else HOST
    display_url = public_url or f"http://{visible_host}:{port}"
    print(f"\n网页地址：{display_url}\n")
    if os.getenv("DOUYIN_NO_BROWSER") != "1":
        threading.Thread(
            target=open_when_ready,
            args=(health_url, display_url),
            daemon=True,
        ).start()
    uvicorn.run("app.main:app", host=HOST, port=port, log_level="info")
