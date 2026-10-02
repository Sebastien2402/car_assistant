import json
import sqlite3
import subprocess
import time

PBF = "data/ile-de-france-latest.osm.pbf"
FILTERED = "data/poi.osm.pbf"
GEOJSON = "data/poi.geojsonseq"
DB = "data/poi.db"
CATEGORIES = ["fast_food", "restaurant", "cafe", "fuel", "pharmacy"]


def run(cmd):
    print("→", " ".join(cmd))
    subprocess.run(cmd, check=True)


def center(geom):
    """Point représentatif (lat, lon) d'une géométrie GeoJSON."""
    t, c = geom.get("type"), geom.get("coordinates")
    if not c:
        return None
    if t == "Point":
        return c[1], c[0]
    ring = {"LineString": c,
            "Polygon": c[0] if t == "Polygon" else None,
            "MultiPolygon": c[0][0] if t == "MultiPolygon" else None}.get(t)
    if not ring:
        return None
    return (sum(p[1] for p in ring) / len(ring), sum(p[0] for p in ring) / len(ring))


def main():
    t0 = time.time()
    run(["osmium", "tags-filter", PBF]
        + [f"nwr/amenity={c}" for c in CATEGORIES]
        + ["-o", FILTERED, "--overwrite"])
    run(["osmium", "export", FILTERED, "-f", "geojsonseq", "-o", GEOJSON, "--overwrite"])

    con = sqlite3.connect(DB)
    con.executescript("""
        DROP TABLE IF EXISTS poi;
        CREATE TABLE poi (category TEXT, name TEXT, lat REAL, lon REAL,
                          street TEXT, housenumber TEXT, city TEXT, horaires TEXT);
    """)

    rows, seen = [], set()
    with open(GEOJSON, encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("\x1e").strip()
            if not line:
                continue
            try:
                feat = json.loads(line)
            except ValueError:
                continue
            props = feat.get("properties") or {}
            cat = props.get("amenity")
            name = props.get("name") or props.get("brand")
            if cat not in CATEGORIES or not name:
                continue
            pos = center(feat.get("geometry") or {})
            if not pos:
                continue
            lat, lon = pos
            key = (cat, name, round(lat, 3), round(lon, 3))
            if key in seen:
                continue
            seen.add(key)
            rows.append((cat, name, lat, lon, props.get("addr:street"),
                         props.get("addr:housenumber"), props.get("addr:city"),
                         props.get("opening_hours")))

    con.executemany("INSERT INTO poi VALUES (?,?,?,?,?,?,?,?)", rows)
    con.execute("CREATE INDEX idx_poi ON poi(category, lat, lon)")
    con.commit()

    print(f"\n{len(rows)} lieux enregistrés en {time.time() - t0:.0f} s")
    for cat, n in con.execute("SELECT category, COUNT(*) FROM poi GROUP BY category"):
        print(f"  {cat} : {n}")
    con.close()


if __name__ == "__main__":
    main()