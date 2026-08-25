#!/bin/zsh
# Napi padel-foglaltság lekérdezés + riport újragenerálás.
# A launchd (com.padel.radar.plist) hívja, de kézzel is futtatható.
set -e
cd "$(dirname "$0")"
# launchd alatt minimális a PATH — a Homebrew-s python3 és a git kell nekünk
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

log() { echo "[$(date '+%Y-%m-%d %H:%M')] $1" >> data/run.log }

# Netkimaradás esetén nem esünk el azonnal: 2 órán át 15 percenként újrapróbáljuk.
# A scraper hibakóddal áll le, ha nem ért el érdemi adatot (ilyenkor nem ír fájlt).
attempt=0
until /usr/bin/env python3 padel_scraper.py --days 7 >> data/run.log 2>&1; do
  attempt=$((attempt + 1))
  if [ $attempt -ge 8 ]; then
    log "FAILED — a lekérdezés 8 próbálkozás után sem sikerült (nincs net?)"
    exit 1
  fi
  log "nincs net / hiba — újrapróbálás 15 perc múlva ($attempt/8)"
  sleep 900
done

/usr/bin/env python3 generate_report.py >> data/run.log 2>&1
log "OK"

# Az új snapshotok és a friss riport feltöltése a GitHubra.
# Ha a push nem megy, a helyi adat attól még megvan.
git add data/ report.html
if ! git diff --cached --quiet; then
  git commit -q -m "Data update $(date '+%Y-%m-%d %H:%M')" \
    && git push -q >> data/run.log 2>&1 \
    || log "git push FAILED (helyi adat rendben)"
fi
