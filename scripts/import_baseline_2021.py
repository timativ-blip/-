"""Build app/baseline_2021.json from the CEC open data (2021 State Duma, federal list, by single-mandate okrug).

Source: http://www.cikrf.ru/opendata/2021/vib21fedsvod.php (data file /opendata/84.xml).
Okrugs 118-129 are the Moscow Oblast single-mandate districts. Run from the repository root:
    python scripts/import_baseline_2021.py
"""
import gzip
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

URL = "http://www.cikrf.ru/opendata/84.xml"
OUT = Path(__file__).resolve().parent.parent / "app" / "baseline_2021.json"
OKRUGS = range(118, 130)
PARTIES = {
    "Единая Россия": "ЕДИНАЯ РОССИЯ", "КПРФ": "КОММУНИСТИЧЕСКАЯ ПАРТИЯ РОССИЙСКОЙ ФЕДЕРАЦИИ", "ЛДПР": "ЛДПР",
    "Новые люди": "НОВЫЕ ЛЮДИ", "Справедливая Россия": "СПРАВЕДЛИВАЯ РОССИЯ", "Зелёные": "ЗЕЛЁНЫЕ",
    "Родина": "РОДИНА", "Партия пенсионеров": "ПАРТИЯ ПЕНСИОНЕРОВ", "Коммунисты России": "КОММУНИСТЫ РОССИИ",
    "Яблоко": "ЯБЛОКО",
}


def number(district, prefix):
    for result in district.findall("result"):
        if result.findtext("name", "").strip().startswith(prefix):
            return int(float(result.findtext("quantity") or 0))
    raise SystemExit(f"Missing line: {prefix}")


def main():
    with urllib.request.urlopen(urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"}), timeout=60) as response:
        raw = response.read()
    root = ET.fromstring(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
    okrugs = {}
    for district in root.findall("district"):
        match = re.search(r"№\s*(\d+)", district.get("name", ""))
        if not match or int(match.group(1)) not in OKRUGS:
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
    if len(okrugs) != 12:
        raise SystemExit(f"Expected 12 okrugs, found {len(okrugs)}")
    OUT.write_text(json.dumps({"year": 2021, "source": "ЦИК России, открытые данные: Госдума 2021, федеральный округ, сводные результаты",
                               "url": URL, "okrugs": dict(sorted(okrugs.items()))}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Wrote {OUT} ({len(okrugs)} okrugs, {sum(o['voters'] for o in okrugs.values())} voters)")


if __name__ == "__main__":
    sys.exit(main())
