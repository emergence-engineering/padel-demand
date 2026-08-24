#!/bin/zsh
# Napi padel-foglaltság lekérdezés + riport újragenerálás.
# A launchd (com.padel.radar.plist) hívja, de kézzel is futtatható.
set -e
cd "$(dirname "$0")"
/usr/bin/env python3 padel_scraper.py --days 7 >> data/run.log 2>&1
/usr/bin/env python3 generate_report.py >> data/run.log 2>&1
echo "[$(date '+%Y-%m-%d %H:%M')] OK" >> data/run.log
