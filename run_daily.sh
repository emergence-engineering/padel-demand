#!/bin/zsh
# Napi padel-foglaltság lekérdezés + riport újragenerálás.
# A launchd (com.padel.radar.plist) hívja, de kézzel is futtatható.
set -e
cd "$(dirname "$0")"
/usr/bin/env python3 padel_scraper.py --days 7 >> data/run.log 2>&1
/usr/bin/env python3 generate_report.py >> data/run.log 2>&1
echo "[$(date '+%Y-%m-%d %H:%M')] OK" >> data/run.log

# Az új snapshotok és a friss riport feltöltése a GitHubra.
# Ha nincs net vagy a push nem megy, a helyi adat attól még megvan.
git add data/ report.html
if ! git diff --cached --quiet; then
  git commit -q -m "Data update $(date '+%Y-%m-%d %H:%M')" \
    && git push -q >> data/run.log 2>&1 \
    || echo "[$(date '+%Y-%m-%d %H:%M')] git push FAILED (helyi adat rendben)" >> data/run.log
fi
