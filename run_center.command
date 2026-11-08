#!/bin/sh
# 맥·리눅스에서 실행. 브라우저를 열고 서버를 켠다.
cd "$(dirname "$0")"
( sleep 1; open http://127.0.0.1:8765 2>/dev/null || xdg-open http://127.0.0.1:8765 ) &
python3 app.py
