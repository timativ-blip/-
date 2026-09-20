"""Build app/baseline_2021.json from the CEC open data (2021 State Duma, federal list, by single-mandate okrug).

Source: http://www.cikrf.ru/opendata/2021/vib21fedsvod.php (data file /opendata/84.xml).
In 2021 Moscow Oblast had 11 single-mandate districts (CEC numbers 117-127); for 2026 the map was redrawn into 12 districts
(numbers 118-129, different boundaries). The 2021 results of the eleven old districts are therefore split between the twelve new
ones in proportion to how much of every old district entered each new one (SPLIT below), so "2021 in this okrug" refers to the
territory of the 2026 okrug. Run from the repository root:
    python scripts/import_baseline_2021.py
"""
import gzip
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

URL = "http://www.cikrf.ru/opendata/84.xml"
OUT = Path(__file__).resolve().parent.parent / "app" / "baseline_2021.json"
OLD_OKRUGS = range(117, 128)  # Moscow Oblast in 2021: Balashikhinsky (117) ... Shchelkovsky (127)
NEW_OKRUGS = range(118, 130)
# New (2026) okrug -> {old (2021) okrug: percent of the OLD okrug that entered the new one}.
# Source: iditena.org/districts/118 ... /129 (overlap of the 2021 and 2026 maps), collected on 2026-09-20. Every old okrug sums to ~100%
# over the new ones, and old voters x share reproduces the page's own "share of the new okrug" within about a point.
SPLIT = {
    "118": {"117": 70.9}, "119": {"118": 70.5, "120": 19.8}, "120": {"119": 82.7, "126": 17.8, "121": 3.9},
    "121": {"120": 80.2, "122": 9.3}, "122": {"121": 53.0, "124": 24.5}, "123": {"117": 29.1, "118": 13.6, "125": 33.4},
    "124": {"122": 90.7}, "125": {"123": 100.0, "119": 17.3, "127": 1.3}, "126": {"124": 75.5, "121": 16.6},
    "127": {"125": 66.6, "118": 16.0, "127": 10.9}, "128": {"126": 81.2, "121": 21.7}, "129": {"127": 85.3, "121": 4.8},
}
PARTIES = {
    "Единая Россия": "ЕДИНАЯ РОССИЯ", "КПРФ": "КОММУНИСТИЧЕСКАЯ ПАРТИЯ РОССИЙСКОЙ ФЕДЕРАЦИИ", "ЛДПР": "ЛДПР",
    "Новые люди": "НОВЫЕ ЛЮДИ", "Справедливая Россия": "СПРАВЕДЛИВАЯ РОССИЯ", "Зелёные": "ЗЕЛЁНЫЕ",
    "Родина": "РОДИНА", "Партия пенсионеров": "ПАРТИЯ ПЕНСИОНЕРОВ", "Коммунисты России": "КОММУНИСТЫ РОССИИ",
    "Яблоко": "ЯБЛОКО",
}


# 2021 party-list shares (percent of all ballots, valid and invalid) recalculated for the territory of each 2026 okrug.
# Source: iditena.org/districts/118 ... /129, collected on 2026-09-20 from CEC precinct protocols: "direct" okrugs sum the protocols of
# their own precincts, the others are old-district shares weighted by the electorate that moved. This is better than splitting old
# district totals (SPLIT) where a new okrug took an atypical part of an old one (e.g. 123: United Russia 33% against 47% by the split).
ORDER = ("Единая Россия", "КПРФ", "ЛДПР", "Справедливая Россия", "Новые люди", "Партия пенсионеров", "Коммунисты России", "Зелёные", "Родина", "Яблоко")
RECALCULATED = {
    "118": (51.92, 17.84, 6.62, 6.95, 4.38, 2.33, 1.16, 1.09, 0.89, 1.49),
    "119": (42.59, 19.66, 8.15, 9.58, 5.05, 2.82, 1.46, 1.23, 0.99, 2.16),
    "120": (45.44, 21.35, 7.59, 6.89, 5.17, 3.49, 1.77, 1.04, 0.89, 1.04),
    "121": (54.21, 16.55, 7.24, 6.15, 4.24, 2.23, 1.27, 1.09, 0.88, 1.46),
    "122": (44.74, 22.25, 9.46, 5.58, 5.38, 2.13, 1.19, 1.13, 1.06, 1.66),
    "123": (33.10, 24.05, 7.43, 10.79, 6.57, 3.33, 1.52, 1.62, 1.42, 2.96),
    "124": (40.68, 22.24, 7.73, 7.08, 6.13, 3.11, 1.74, 1.46, 1.09, 2.14),
    "125": (51.58, 19.15, 7.21, 5.37, 4.47, 2.93, 1.21, 1.12, 0.80, 1.10),
    "126": (43.04, 23.68, 7.25, 6.26, 5.45, 2.89, 1.45, 1.32, 1.10, 1.74),
    "127": (46.71, 19.20, 7.06, 8.50, 5.20, 2.88, 1.48, 1.17, 1.22, 1.66),
    "128": (44.22, 22.22, 7.70, 6.53, 5.44, 2.92, 1.83, 1.14, 1.03, 1.46),
    "129": (42.87, 22.69, 7.83, 6.23, 5.47, 3.40, 1.84, 1.33, 0.95, 1.49),
}
WEIGHTED_ONLY = ("120", "126", "128", "129")  # not summed from their own protocols


def number(district, prefix):
    for result in district.findall("result"):
        if result.findtext("name", "").strip().startswith(prefix):
            return int(float(result.findtext("quantity") or 0))
    raise SystemExit(f"Missing line: {prefix}")


def main():
    import requests  # build-time only: pip install requests (urllib is dropped by the CEC server)
    for attempt in range(6):  # the CEC server sometimes drops the first connections
        try:
            raw = requests.get(URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=90).content
            break
        except requests.ConnectionError:
            if attempt == 5:
                raise
            time.sleep(5 * (attempt + 1))
    root = ET.fromstring(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
    okrugs = {}
    for district in root.findall("district"):
        match = re.search(r"№\s*(\d+)", district.get("name", ""))
        if not match or int(match.group(1)) not in OLD_OKRUGS:
            continue
        votes = {}
        for label, marker in PARTIES.items():
            hits = [r for r in district.findall("result") if re.match(r"\d+\.", r.findtext("name", "")) and marker in r.findtext("name", "").upper()]
            if len(hits) != 1:
                raise SystemExit(f"Okrug {match.group(1)}: party {label!r} matched {len(hits)} lines")
            votes[label] = int(float(hits[0].findtext("quantity") or 0))
        issued = (number(district, "Число избирательных бюллетеней, выданных избирателям, проголосовавшим досрочно")
                  + number(district, "Число избирательных бюллетеней, выданных в помещении")
                  + number(district, "Число избирательных бюллетеней, выданных вне помещения"))
        okrugs[match.group(1)] = {"voters": number(district, "Число избирателей, внесенных"), "issued": issued,
                                  "valid": number(district, "Число действительных"), "votes": votes}
    if len(okrugs) != len(OLD_OKRUGS):
        raise SystemExit(f"Expected {len(OLD_OKRUGS)} okrugs, found {len(okrugs)}")
    fields = ("voters", "issued", "valid")
    new_okrugs = {}
    for new_id in map(str, NEW_OKRUGS):
        entry = {field: 0.0 for field in fields}
        entry["votes"] = {label: 0.0 for label in PARTIES}
        for old_id, percent in SPLIT[new_id].items():
            part = percent / 100
            for field in fields:
                entry[field] += okrugs[old_id][field] * part
            for label in PARTIES:
                entry["votes"][label] += okrugs[old_id]["votes"][label] * part
        valid = round(entry["valid"])
        # Electorate and turnout come from the split; party votes from the recalculation of the new territory (scaled to its valid ballots).
        votes = {label: round(share * valid / 100) for label, share in zip(ORDER, RECALCULATED[new_id])}
        new_okrugs[new_id] = {**{field: round(entry[field]) for field in fields}, "votes": votes, "votes_method": "weighted" if new_id in WEIGHTED_ONLY else "direct"}
    moved = {old_id: sum(SPLIT[n].get(old_id, 0) for n in SPLIT) for old_id in okrugs}
    for old_id, total in moved.items():
        if not 95 <= total <= 101:
            raise SystemExit(f"Old okrug {old_id} is split {total:.1f}% between the new okrugs, expected about 100%")
    OUT.write_text(json.dumps({"year": 2021, "source": "ЦИК России, открытые данные: Госдума 2021, федеральный округ, сводные результаты",
                               "url": URL, "okrugs": new_okrugs, "old_okrugs": dict(sorted(okrugs.items())), "split": SPLIT}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Wrote {OUT} ({len(new_okrugs)} okrugs of the 2026 map, {sum(o['voters'] for o in new_okrugs.values())} voters)")


if __name__ == "__main__":
    sys.exit(main())
