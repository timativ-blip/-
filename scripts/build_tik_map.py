"""Build app/static/tik_map.json: simplified boundaries of the 56 Moscow Oblast TIK territories.

Each TIK is one municipality (admin_level=6) in OpenStreetMap. Boundaries: (c) OpenStreetMap contributors, ODbL.
Needs `pip install requests shapely` (build time only; the server does not use shapely). From the repository root:
    python scripts/build_tik_map.py
"""
import json
import sys
import time
from pathlib import Path

import requests
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import polygonize, unary_union

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "app" / "static" / "tik_map.json"
MIRRORS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
CACHE = Path("/tmp/tik_map_cache")
HEADERS = {"User-Agent": "exit-poll-dashboard/1.0 (research; https://github.com/timativ-blip)"}
TOLERANCE = 0.002  # degrees, about 200 m: plenty for a region-wide choropleth
JUNK = ("тик города", "тик поселка", "городской округ", "муниципальный округ")
# OSM municipality name -> TIK key where the names differ beyond an adjective ending.
OVERRIDES = {"богородский": "ногинск", "ленинский": "видное", "рузский": "руза", "раменский": "раменское",
             "павлово-посадский": "павловский посад", "сергиево-посадский": "сергиев посад"}


def key(name):
    text = name.lower().replace("ё", "е")
    for junk in JUNK:
        text = text.replace(junk, "")
    return text.strip()


def overpass(query, cache_name=None):
    """POST an Overpass query with short timeouts, mirror fallback and an on-disk cache so a rerun resumes."""
    cached = CACHE / f"{cache_name}.json" if cache_name else None
    if cached and cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))
    last = None
    for attempt in range(8):
        url = MIRRORS[attempt % len(MIRRORS)]
        try:
            response = requests.post(url, data={"data": query}, headers=HEADERS, timeout=60)
            response.raise_for_status()
            result = response.json()
            if cached:
                CACHE.mkdir(parents=True, exist_ok=True)
                cached.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            time.sleep(3)  # stay polite: Overpass rate-limits bursts
            return result
        except Exception as exc:
            last = exc
            print(f"  retry {attempt + 1} ({type(exc).__name__})", flush=True)
            time.sleep(10 * (attempt + 1))
    raise SystemExit(f"Overpass failed: {last}")


def area_of(relation):
    outer, inner = [], []
    for member in relation["members"]:
        if member["type"] == "way" and member.get("geometry"):
            line = LineString([(p["lon"], p["lat"]) for p in member["geometry"]])
            (inner if member["role"] == "inner" else outer).append(line)
    area = unary_union(list(polygonize(unary_union(outer))))
    if inner:
        area = area.difference(unary_union(list(polygonize(unary_union(inner)))))
    return area if area.is_valid else area.buffer(0)


def rings(polygon):
    def ring(coords):
        return [[round(x, 4), round(y, 4)] for x, y in coords]
    return [ring(polygon.exterior.coords)] + [ring(hole.coords) for hole in polygon.interiors]


def main():
    sys.path.insert(0, str(ROOT))
    from app.main import OKRUGS, TIK_TO_OKRUG  # noqa: E402
    tik_by_key = {key(tik): tik for tik in TIK_TO_OKRUG}
    listing = overpass('[out:json][timeout:90];area(3600051490)->.a;rel(area.a)["boundary"="administrative"]["admin_level"="6"];out tags;', "listing")
    matched, features = {}, []
    for element in listing["elements"]:
        osm_name = element["tags"]["name"]
        name_key = OVERRIDES.get(key(osm_name), key(osm_name))
        candidates = [tik for tik_key, tik in tik_by_key.items()
                      if tik_key == name_key or (len(tik_key) >= 5 and len(name_key) >= 5 and tik_key[:5] == name_key[:5]
                                                  and abs(len(tik_key) - len(name_key)) <= 4)]
        if name_key in tik_by_key:
            candidates = [tik_by_key[name_key]]
        if len(candidates) != 1:
            raise SystemExit(f"Cannot match {osm_name!r}: {candidates}")
        if candidates[0] in matched:
            raise SystemExit(f"{candidates[0]!r} matched twice: {matched[candidates[0]]!r} and {osm_name!r}")
        matched[candidates[0]] = osm_name
        relation = overpass(f"[out:json][timeout:90];rel({element['id']});out geom;", f"rel{element['id']}")["elements"][0]
        area = area_of(relation).simplify(TOLERANCE, preserve_topology=True)
        polygons = [p for p in (area.geoms if isinstance(area, MultiPolygon) else [area]) if isinstance(p, Polygon) and p.area > 2e-6]
        biggest = max(polygons, key=lambda p: p.area)
        point = biggest.representative_point()
        features.append({"tik": candidates[0], "okrug": TIK_TO_OKRUG[candidates[0]], "osm": osm_name,
                         "label": [round(point.x, 4), round(point.y, 4)], "polygons": [rings(p) for p in polygons]})
        print(f"{osm_name:42} -> {candidates[0]}", flush=True)
    missing = set(TIK_TO_OKRUG) - set(matched)
    if missing:
        raise SystemExit(f"TIKs without a boundary: {sorted(missing)}")
    features.sort(key=lambda f: (f["okrug"], f["tik"]))
    xs = [x for f in features for polygon in f["polygons"] for x, _ in polygon[0]]
    ys = [y for f in features for polygon in f["polygons"] for _, y in polygon[0]]
    OUT.write_text(json.dumps({"attribution": "Границы: © участники OpenStreetMap (ODbL)", "bbox": [min(xs), min(ys), max(xs), max(ys)],
                               "features": features}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {OUT} ({OUT.stat().st_size // 1024} KB, {len(features)} territories)")


if __name__ == "__main__":
    sys.exit(main())
