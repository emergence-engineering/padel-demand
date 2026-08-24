# Budapest Padel Radar

Budapesti padelpályák foglaltságát mérő eszköz. A Playtomic nyilvános webes
adataiból lekéri a szabad idősávokat a következő 7 napra, és klubonként /
naponként / idősávonként kihasználtsági százalékot számol.

## Fájlok

| Fájl | Mit csinál |
|---|---|
| `padel_scraper.py` | Klubfelderítés + 7 napos foglaltság-lekérdezés → `data/snapshot_*.json`, `data/occupancy_*.csv`, `data/latest.json` |
| `generate_report.py` | A `data/latest.json`-ból (és az összes korábbi snapshotból) magyar nyelvű `report.html` riportot készít |
| `run_daily.sh` | A kettő egyben — ezt hívja az időzítő |
| `com.padel.radar.plist` | macOS launchd időzítés (naponta kétszer: 10:00 és 17:00) |
| `data/` | Minden lekérdezés megőrződik — ebből épül a feltöltődési görbe |

## Kézi futtatás

```bash
./run_daily.sh
open report.html
```

## Időzített futtatás bekapcsolása (macOS launchd)

```bash
cp com.padel.radar.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.padel.radar.plist
```

Kikapcsolás: `launchctl unload ~/Library/LaunchAgents/com.padel.radar.plist`

Megjegyzés: a launchd csak akkor fut, ha a gép ébren van. Ha a megadott időpontban
aludt a gép, a következő ébredéskor pótolja a futást (a cronnal ellentétben).

## Módszertani jegyzetek

- **Foglaltság** = 1 − (szabad pályaóra / nyitvatartási pályaóra). Három ablakban
  számoljuk: teljes nyitvatartás, napközben (07–23), csúcsidő (17–22).
- A klub által **blokkolt sávok** (edzés, verseny) foglaltnak látszanak — a tartósan
  100%-os klub adata gyanús, nem kereslet.
- **Lead-time torzítás**: egyetlen pillanatfelvétel a távoli napokat alulbecsüli,
  mert a foglalások zöme az utolsó 1–3 napban érkezik. Ezért kell naponta futtatni:
  az azonos célnapra vonatkozó egymás utáni mérésekből áll össze a feltöltődési
  görbe és a tényleges (végső) kihasználtság.
- **Nem mérhető klubok**: amelyik klub nem publikál webes szabad idősávot
  (pl. Top Padel Club, Musketas), az kimarad.
- A scraper kb. 130 kérést indít futásonként, 0,25 s szünetekkel — ez a Playtomic
  számára elhanyagolható terhelés.
