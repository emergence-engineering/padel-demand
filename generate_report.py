#!/usr/bin/env python3
"""
Budapest padel occupancy report generator.

Reads data/latest.json (plus any historical snapshot_*.json for the fill-up
curve) and writes report.html — a self-contained Hungarian-language report.

Usage: python3 generate_report.py
"""

import glob
import json
import html as H
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"

HU_DAYS = {0: "hétfő", 1: "kedd", 2: "szerda", 3: "csütörtök",
           4: "péntek", 5: "szombat", 6: "vasárnap"}

DISTRICTS = {
    1: "I. ker.", 2: "II. ker.", 3: "III. ker. (Óbuda)", 4: "IV. ker. (Újpest)",
    5: "V. ker.", 6: "VI. ker.", 7: "VII. ker.", 8: "VIII. ker.", 9: "IX. ker.",
    10: "X. ker.", 11: "XI. ker.", 12: "XII. ker.", 13: "XIII. ker.",
    14: "XIV. ker. (Zugló)", 15: "XV. ker.", 16: "XVI. ker.", 17: "XVII. ker.",
    18: "XVIII. ker.", 19: "XIX. ker.", 20: "XX. ker.", 21: "XXI. ker.",
    22: "XXII. ker.", 23: "XXIII. ker.",
}


def district_of(club):
    pc = club.get("postal_code", "")
    if club.get("city", "").strip().lower() != "budapest":
        return club.get("city", "").strip()
    if len(pc) == 4 and pc.startswith("1"):
        return DISTRICTS.get(int(pc[1:3]), "Budapest")
    return "Budapest"


def wavg(pairs):
    """pairs: list of (occ_pct, weight_hours); -> weighted % or None"""
    num = sum(p * w for p, w in pairs if p is not None and w)
    den = sum(w for p, w in pairs if p is not None and w)
    return round(num / den, 1) if den else None


def load():
    d = json.loads((DATA / "latest.json").read_text())
    snaps = []
    for f in sorted(glob.glob(str(DATA / "snapshot_*.json"))):
        try:
            snaps.append(json.loads(Path(f).read_text()))
        except json.JSONDecodeError:
            pass
    return d, snaps


def build(d, snaps):
    scraped = datetime.fromisoformat(d["scraped_at"])
    today = scraped.date()
    clubs = {c["slug"]: c for c in d["clubs"]}
    rows = [r for r in d["occupancy"] if r.get("data_ok")]
    nodata = sorted({r["name"].strip() for r in d["occupancy"] if not r.get("data_ok")})
    measurable_slugs = {r["slug"] for r in rows}
    n_courts = sum(clubs[s]["courts"] for s in measurable_slugs)

    def dnum(r):
        return (date.fromisoformat(r["date"]) - today).days

    # ---- per-day aggregates (court-hour weighted) ----
    by_day = defaultdict(lambda: {"core": [], "prime": []})
    for r in rows:
        by_day[r["date"]]["core"].append((r["occ_core_pct"], r["core_court_hours"]))
        by_day[r["date"]]["prime"].append((r["occ_prime_pct"], r["prime_court_hours"]))
    day_rows = []
    for day, v in sorted(by_day.items()):
        dt = date.fromisoformat(day)
        day_rows.append({
            "date": day, "hu": HU_DAYS[dt.weekday()],
            "offset": (dt - today).days,
            "core": wavg(v["core"]), "prime": wavg(v["prime"]),
        })

    # ---- headline metrics: next 1-3 days (excl. remainder of today) ----
    n3 = [r for r in rows if 1 <= dnum(r) <= 3]
    head_core = wavg([(r["occ_core_pct"], r["core_court_hours"]) for r in n3])
    head_prime = wavg([(r["occ_prime_pct"], r["prime_court_hours"]) for r in n3])
    tomorrow = [r for r in rows if dnum(r) == 1]
    tom_prime = wavg([(r["occ_prime_pct"], r["prime_court_hours"]) for r in tomorrow])

    # ---- per-club ----
    per_club = []
    for slug in measurable_slugs:
        cr = [r for r in rows if r["slug"] == slug]
        c3 = [r for r in cr if 1 <= dnum(r) <= 3]
        entry = {
            "club": clubs[slug],
            "district": district_of(clubs[slug]),
            "n3_core": wavg([(r["occ_core_pct"], r["core_court_hours"]) for r in c3]),
            "n3_prime": wavg([(r["occ_prime_pct"], r["prime_court_hours"]) for r in c3]),
            "w_core": wavg([(r["occ_core_pct"], r["core_court_hours"]) for r in cr]),
            "w_prime": wavg([(r["occ_prime_pct"], r["prime_court_hours"]) for r in cr]),
            "flags": [],
        }
        if entry["n3_core"] is not None and entry["n3_core"] >= 97:
            entry["flags"].append("Gyanúsan telített — ellenőrizd, valóban foglalások-e, vagy blokkolt sávok.")
        per_club.append(entry)
    per_club.sort(key=lambda e: -(e["n3_core"] if e["n3_core"] is not None else -1))

    # ---- per-district ----
    by_dist = defaultdict(lambda: {"courts": 0, "clubs": 0, "core": [], "prime": []})
    for e in per_club:
        b = by_dist[e["district"]]
        b["courts"] += e["club"]["courts"]
        b["clubs"] += 1
        cr = [r for r in rows if r["slug"] == e["club"]["slug"] and 1 <= dnum(r) <= 3]
        b["core"] += [(r["occ_core_pct"], r["core_court_hours"]) for r in cr]
        b["prime"] += [(r["occ_prime_pct"], r["prime_court_hours"]) for r in cr]
    dist_rows = sorted(
        ({"name": k, "clubs": v["clubs"], "courts": v["courts"],
          "core": wavg(v["core"]), "prime": wavg(v["prime"])}
         for k, v in by_dist.items()),
        key=lambda x: -(x["core"] or 0))

    # ---- fill-up curve across snapshots (same target date, multiple scrapes) ----
    fill = defaultdict(dict)   # target_date -> scrape_date -> core%
    for s in snaps:
        sd = datetime.fromisoformat(s["scraped_at"]).date().isoformat()
        srows = [r for r in s["occupancy"] if r.get("data_ok")]
        sby = defaultdict(list)
        for r in srows:
            sby[r["date"]].append((r["occ_core_pct"], r["core_court_hours"]))
        for day, pairs in sby.items():
            v = wavg(pairs)
            if v is not None:
                fill[day][sd] = v
    fill_rows = [(day, sorted(m.items())) for day, m in sorted(fill.items())
                 if len(m) >= 2]

    return {
        "scraped": scraped, "today": today,
        "n_clubs": len(measurable_slugs), "n_courts": n_courts,
        "nodata": nodata, "day_rows": day_rows,
        "head_core": head_core, "head_prime": head_prime, "tom_prime": tom_prime,
        "per_club": per_club, "dist_rows": dist_rows, "fill_rows": fill_rows,
    }


def pct(v):
    return "n/a" if v is None else f"{v:.0f}%"


def bar(v, cls):
    if v is None:
        return '<span class="mut">n/a</span>'
    return (f'<div class="ibar"><div class="ibar-fill {cls}" style="width:{min(v,100):.0f}%"></div>'
            f'<span class="ibar-val">{v:.0f}%</span></div>')


def render(m):
    day_bars = ""
    max_h = 150
    for dr in m["day_rows"]:
        label = f'{dr["date"][5:].replace("-", ".")}<br>{dr["hu"]}'
        note = " ma" if dr["offset"] == 0 else ""
        core_h = 0 if dr["core"] is None else max(4, dr["core"] / 100 * max_h)
        prime_h = 0 if dr["prime"] is None else max(4, dr["prime"] / 100 * max_h)
        day_bars += f'''
        <div class="dgroup{' today' if dr["offset"] == 0 else ''}">
          <div class="dbars">
            <div class="dbar core" style="height:{core_h:.0f}px" data-tip="Napközbeni (07–23): {pct(dr['core'])}"><span>{pct(dr["core"])}</span></div>
            <div class="dbar prime" style="height:{prime_h:.0f}px" data-tip="Csúcsidő (17–22): {pct(dr['prime'])}"><span>{pct(dr["prime"])}</span></div>
          </div>
          <div class="dlabel">{label}{f'<em>{note}</em>' if note else ''}</div>
        </div>'''

    club_rows = ""
    for e in m["per_club"]:
        c = e["club"]
        name = H.escape(c["name"].strip())
        flag = ' <span class="flag" title="' + H.escape(e["flags"][0]) + '">⚠</span>' if e["flags"] else ""
        club_rows += f'''
        <tr>
          <td class="cname">{name}{flag}<div class="csub">{H.escape(e["district"])} · {c["km_from_center"]:.0f} km a centrumtól</div></td>
          <td class="num">{c["courts"]}</td>
          <td>{bar(e["n3_core"], "core")}</td>
          <td>{bar(e["n3_prime"], "prime")}</td>
          <td class="num">{pct(e["w_core"])}</td>
          <td class="num">{pct(e["w_prime"])}</td>
        </tr>'''

    dist_rows = ""
    for x in m["dist_rows"]:
        dist_rows += (f'<tr><td>{H.escape(x["name"])}</td><td class="num">{x["clubs"]}</td>'
                      f'<td class="num">{x["courts"]}</td><td>{bar(x["core"], "core")}</td>'
                      f'<td class="num">{pct(x["prime"])}</td></tr>')

    nodata_html = ""
    if m["nodata"]:
        items = ", ".join(H.escape(n) for n in m["nodata"])
        nodata_html = (f'<p class="note">Nem mérhető (a webes foglalást nem publikálja, '
                       f'vagy nem ad ki szabad idősávot): <strong>{items}</strong>. '
                       f'Ezek a fenti számokban nem szerepelnek.</p>')

    fill_html = ""
    if m["fill_rows"]:
        frows = ""
        for day, seq in m["fill_rows"]:
            cells = " → ".join(f'{sd[5:]}: <strong>{v:.0f}%</strong>' for sd, v in seq)
            frows += f'<tr><td>{day}</td><td>{cells}</td></tr>'
        fill_html = f'''
      <section>
        <h2>Feltöltődési görbe <span class="eyebrow-inline">több lekérdezés összevetése</span></h2>
        <p>Ugyanazon célnap foglaltsága különböző lekérdezési napokon — ebből látszik, hány nappal
        előre telnek meg a pályák, és mi a tényleges (végső) kihasználtság.</p>
        <div class="tscroll"><table>
          <thead><tr><th>Célnap</th><th>Napközbeni foglaltság a lekérdezés napján</th></tr></thead>
          <tbody>{frows}</tbody>
        </table></div>
      </section>'''

    generated = m["scraped"].strftime("%Y. %m. %d. %H:%M")
    horizon_end = m["day_rows"][-1]["date"] if m["day_rows"] else ""

    return f'''<title>Budapest Padel Radar</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700;12..96,800&family=Instrument+Sans:ital,wght@0,400;0,500;0,600;1,400&display=swap">
<style>
  :root {{
    --bg:#F4F7FA; --card:#FFFFFF; --ink:#16232E; --mut:#5A6B7A; --line:#DCE5EC;
    --core:#2563B8; --prime:#D97E28; --core-soft:#E3EDFA; --accent:#2563B8;
    --warn-bg:#FBF3E7; --warn-ink:#8A5A1E; --shadow:0 1px 3px rgba(22,35,46,.07);
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --bg:#10161D; --card:#1B242E; --ink:#E8EEF4; --mut:#93A4B4; --line:#2C3844;
      --core:#4A8CD9; --prime:#C77F38; --core-soft:#22303F; --accent:#4A8CD9;
      --warn-bg:#2E2417; --warn-ink:#E0B26A; --shadow:0 1px 3px rgba(0,0,0,.35);
    }}
  }}
  :root[data-theme="dark"] {{
    --bg:#10161D; --card:#1B242E; --ink:#E8EEF4; --mut:#93A4B4; --line:#2C3844;
    --core:#4A8CD9; --prime:#C77F38; --core-soft:#22303F; --accent:#4A8CD9;
    --warn-bg:#2E2417; --warn-ink:#E0B26A; --shadow:0 1px 3px rgba(0,0,0,.35);
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; background:var(--bg); color:var(--ink);
    font-family:"Instrument Sans",system-ui,sans-serif; font-size:15.5px; line-height:1.55;
  }}
  .wrap {{ max-width:1020px; margin:0 auto; padding:40px 22px 80px; }}
  header {{ margin-bottom:34px; }}
  .eyebrow {{ text-transform:uppercase; letter-spacing:.14em; font-size:12px; font-weight:600; color:var(--accent); }}
  h1 {{ font-family:"Bricolage Grotesque","Instrument Sans",sans-serif; font-weight:800;
       font-size:clamp(30px,5vw,44px); line-height:1.06; margin:8px 0 10px; text-wrap:balance; }}
  .meta {{ color:var(--mut); font-size:14px; }}
  h2 {{ font-family:"Bricolage Grotesque","Instrument Sans",sans-serif; font-weight:700;
       font-size:23px; margin:0 0 6px; text-wrap:balance; }}
  .eyebrow-inline {{ font-family:"Instrument Sans"; font-size:12px; font-weight:600; color:var(--mut);
       text-transform:uppercase; letter-spacing:.1em; margin-left:8px; }}
  section {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
       padding:26px 28px; margin-bottom:22px; box-shadow:var(--shadow); }}
  section > p {{ max-width:68ch; color:var(--ink); }}
  p.lead {{ font-size:17px; }}
  .mut, .note {{ color:var(--mut); }}
  .note {{ font-size:14px; max-width:75ch; }}

  .tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:14px; margin-bottom:22px; }}
  .tile {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:18px 20px; box-shadow:var(--shadow); }}
  .tile .v {{ font-family:"Bricolage Grotesque",sans-serif; font-weight:800; font-size:34px;
       font-variant-numeric:tabular-nums; line-height:1.1; }}
  .tile .v.core-c {{ color:var(--core); }} .tile .v.prime-c {{ color:var(--prime); }}
  .tile .l {{ font-size:13px; color:var(--mut); margin-top:4px; }}

  .legend {{ display:flex; gap:18px; font-size:13px; color:var(--mut); margin:8px 0 18px; }}
  .legend i {{ display:inline-block; width:11px; height:11px; border-radius:3px; margin-right:6px; vertical-align:-1px; }}
  .legend .lc i {{ background:var(--core); }} .legend .lp i {{ background:var(--prime); }}

  .chart {{ display:flex; align-items:flex-end; gap:10px; padding:14px 4px 0; overflow-x:auto; }}
  .dgroup {{ flex:1 1 0; min-width:86px; text-align:center; }}
  .dgroup.today .dlabel {{ color:var(--accent); font-weight:600; }}
  .dbars {{ display:flex; align-items:flex-end; justify-content:center; gap:4px; height:170px; }}
  .dbar {{ width:30px; border-radius:4px 4px 0 0; position:relative; }}
  .dbar.core {{ background:var(--core); }} .dbar.prime {{ background:var(--prime); }}
  .dbar span {{ position:absolute; top:-20px; left:50%; transform:translateX(-50%);
       font-size:11.5px; font-weight:600; font-variant-numeric:tabular-nums; color:var(--ink); }}
  .dbar:hover::after {{ content:attr(data-tip); position:absolute; bottom:calc(100% + 24px); left:50%;
       transform:translateX(-50%); background:var(--ink); color:var(--bg); font-size:12px;
       padding:5px 9px; border-radius:6px; white-space:nowrap; z-index:5; }}
  .dlabel {{ font-size:12.5px; color:var(--mut); margin-top:8px; line-height:1.3; }}
  .dlabel em {{ font-style:normal; color:var(--accent); }}

  .tscroll {{ overflow-x:auto; }}
  table {{ border-collapse:collapse; width:100%; font-size:14.5px; }}
  th {{ text-align:left; font-size:12px; text-transform:uppercase; letter-spacing:.07em;
       color:var(--mut); font-weight:600; padding:8px 12px 8px 0; border-bottom:1px solid var(--line); white-space:nowrap; }}
  td {{ padding:10px 12px 10px 0; border-bottom:1px solid var(--line); vertical-align:middle; }}
  tr:last-child td {{ border-bottom:none; }}
  td.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .cname {{ font-weight:600; min-width:190px; }}
  .csub {{ font-weight:400; font-size:12.5px; color:var(--mut); }}
  .flag {{ color:var(--warn-ink); cursor:help; }}
  .ibar {{ position:relative; background:var(--core-soft); border-radius:4px; height:20px; min-width:130px; }}
  .ibar-fill {{ height:100%; border-radius:4px; }}
  .ibar-fill.core {{ background:var(--core); }} .ibar-fill.prime {{ background:var(--prime); }}
  .ibar-val {{ position:absolute; right:6px; top:0; line-height:20px; font-size:12px; font-weight:600;
       font-variant-numeric:tabular-nums; color:var(--ink); }}

  .callout {{ background:var(--warn-bg); color:var(--warn-ink); border-radius:8px; padding:14px 18px;
       font-size:14px; max-width:75ch; }}
  ul {{ padding-left:20px; }} li {{ margin-bottom:7px; max-width:70ch; }}
  code {{ background:var(--core-soft); border-radius:4px; padding:1px 6px; font-size:13.5px; }}
  @media (prefers-reduced-motion:no-preference) {{
    .ibar-fill {{ transition:width .4s ease; }}
  }}
  @media (max-width:640px) {{ section {{ padding:20px 16px; }} }}
</style>

<div class="wrap">
  <header>
    <div class="eyebrow">Piackutatás · Playtomic-adatok</div>
    <h1>Budapest Padel Radar</h1>
    <div class="meta">Lekérdezve: {generated} · Horizont: ma → {horizon_end} ·
      {m["n_clubs"]} mérhető klub, {m["n_courts"]} pálya</div>
  </header>

  <div class="tiles">
    <div class="tile"><div class="v core-c">{pct(m["head_core"])}</div>
      <div class="l">Napközbeni foglaltság (07–23), következő 3 nap</div></div>
    <div class="tile"><div class="v prime-c">{pct(m["head_prime"])}</div>
      <div class="l">Csúcsidős foglaltság (17–22), következő 3 nap</div></div>
    <div class="tile"><div class="v prime-c">{pct(m["tom_prime"])}</div>
      <div class="l">Holnapi csúcsidő — a legérettebb, legmegbízhatóbb szám</div></div>
    <div class="tile"><div class="v">{m["n_courts"]}</div>
      <div class="l">Online foglalható pálya {m["n_clubs"]} klubban (Budapest + agglomeráció)</div></div>
  </div>

  <section>
    <h2>Foglaltság naponként</h2>
    <p>Minden oszloppár egy nap: a kék a teljes napközbeni sáv (07–23), a borostyán a
    csúcsidő (17–22). Pályaórával súlyozott átlag a mérhető klubokra.</p>
    <div class="legend"><span class="lc"><i></i>Napközben 07–23</span>
      <span class="lp"><i></i>Csúcsidő 17–22</span></div>
    <div class="chart">{day_bars}</div>
    <p class="note"><strong>Így olvasd:</strong> a padelfoglalások zöme az utolsó 1–3 napban érkezik,
    ezért a távolabbi napok alacsony száma nem alacsony keresletet jelent, hanem azt, hogy oda még
    nem érkeztek be a foglalások. A holnapi/holnaputáni érték áll a legközelebb a végső kihasználtsághoz —
    a hét végi napok valós számát a későbbi napi lekérdezések mutatják majd meg.</p>
  </section>

  <section>
    <h2>Klubok rangsora <span class="eyebrow-inline">következő 3 nap</span></h2>
    <div class="tscroll">
    <table>
      <thead><tr>
        <th>Klub</th><th class="num">Pálya</th>
        <th>Napközben 07–23</th><th>Csúcsidő 17–22</th>
        <th class="num">7 nap<br>napközben</th><th class="num">7 nap<br>csúcs</th>
      </tr></thead>
      <tbody>{club_rows}</tbody>
    </table>
    </div>
    {nodata_html}
  </section>

  <section>
    <h2>Területi bontás <span class="eyebrow-inline">következő 3 nap</span></h2>
    <div class="tscroll">
    <table>
      <thead><tr><th>Terület</th><th class="num">Klub</th><th class="num">Pálya</th>
        <th>Napközben 07–23</th><th class="num">Csúcsidő</th></tr></thead>
      <tbody>{dist_rows}</tbody>
    </table>
    </div>
  </section>
  {fill_html}

  <section>
    <h2>Szezonalitás</h2>
    <p class="lead">Augusztus vége a padelpiac egyik leggyengébb időszaka — a most mért számok
    a szezonális mélypont közelében készültek.</p>
    <ul>
      <li><strong>Nyár (júl–aug):</strong> szabadságolások, kültéri alternatívák — a beltéri pályák
      kereslete jellemzően 20–40%-kal esik a tavaszi‑őszi szinthez képest.</li>
      <li><strong>Főszezon (szept–nov és jan–ápr):</strong> a csúcsidős sávok a jó helyeken rendszeresen
      betelnek; a most mért csúcsidős értékekre óvatos becslésként +15–30 százalékpont tehető.</li>
      <li><strong>December:</strong> vegyes (ünnepek), <strong>május–június:</strong> lecsengő átmenet.</li>
    </ul>
    <p class="note">A pontos szezonális szorzót éppen ez az eszköz fogja megadni: ha a napi lekérdezés
    ősszel is fut, a saját adatsorodból látod majd az augusztus→október változást becslés helyett.</p>
  </section>

  <section>
    <h2>Módszertan és korlátok</h2>
    <ul>
      <li><strong>Forrás:</strong> a Playtomic nyilvános webes foglaltsági adatai (szabad idősávok
      klubonként és pályánként, 7 napra előre). A foglaltság = 1 − szabad pályaóra / összes nyitvatartási pályaóra.</li>
      <li><strong>„Foglalt” ≠ biztosan kifizetett foglalás:</strong> a klub által blokkolt sávok
      (edzés, verseny, karbantartás) is foglaltnak látszanak. A tartósan 100%-os klub gyanús —
      nála valószínűleg nem a kereslet, hanem az adatközlés az ok (⚠ jelölés).</li>
      <li><strong>Csak online foglalható pályák látszanak:</strong> a telefonos/helyszíni foglalásokat
      futtató klubok ({", ".join(H.escape(n) for n in m["nodata"]) if m["nodata"] else "—"}) kimaradnak.</li>
      <li><strong>Lead-time torzítás:</strong> egy pillanatfelvétel a távoli napokat alulbecsüli.
      A napi ismételt futtatás ezt kiküszöböli (feltöltődési görbe).</li>
      <li><strong>Minimális foglalási idő:</strong> a 60 percnél rövidebb lyukak nem jelennek meg
      szabadként — a valós kihasználtság ennyivel minimálisan alacsonyabb lehet a mértnél.</li>
    </ul>
  </section>
</div>'''


def main():
    d, snaps = load()
    m = build(d, snaps)
    out = ROOT / "report.html"
    out.write_text(render(m))
    print(f"Report written: {out}")


if __name__ == "__main__":
    main()
