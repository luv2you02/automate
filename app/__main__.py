"""`python -m app` 으로 서버 실행 (run.bat 이 호출).

HOST/PORT 는 .env 에서 읽는다. 1단계는 127.0.0.1 (담당자 PC 전용).
"""
import threading
import webbrowser

import uvicorn

from .config import get_settings


def main() -> None:
    s = get_settings()
    url = f"http://{'127.0.0.1' if s.host in ('0.0.0.0', '') else s.host}:{s.port}"
    if s.open_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    print(f"서버 시작: {url}  (종료: Ctrl+C)")
    uvicorn.run("app.main:app", host=s.host, port=s.port, log_level="info")


if __name__ == "__main__":
    main()
