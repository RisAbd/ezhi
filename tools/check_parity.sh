#!/bin/sh
# Сверка docs/ezhi.js с ezhi_catalog.py на одних и тех же страницах из cache/:
#   python3 ezhi_catalog.py snapshot && sh tools/check_parity.sh
# Мобильная версия форума (её получает браузер телефона): те же темы с мобильным User-Agent в cache_mobile/ —
# разбор должен дать те же товары и id.
cd "$(dirname "$0")/.." || exit 1
python3 -m http.server 8799 --bind 127.0.0.1 >/dev/null 2>&1 &
SRV=$!
sleep 1
P=$(mktemp -d)
for V in "" "#mobile"; do   # полная версия форума и мобильная (cache_mobile/, если скачана)
  [ -n "$V" ] && [ ! -d cache_mobile ] && continue
  echo "== ${V:-#полная}"
  google-chrome --headless=new --no-sandbox --disable-gpu --user-data-dir="$P" --virtual-time-budget=60000 \
    --dump-dom "http://127.0.0.1:8799/tools/parity.html$V" 2>/dev/null | sed -n '/<pre id="out">/,/<\/pre>/p'
done
kill $SRV; rm -rf "$P"
