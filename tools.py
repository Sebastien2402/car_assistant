import time
from math import radians, sin, cos, asin, sqrt
import requests

UA = {"User-Agent": "copilote-vocal-proto/0.1 (projet perso)"}
OSRM = "https://router.project-osrm.org"
NOMINATIM = "https://nominatim.openstreetmap.org"
OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

CATEGORIES = {
    "fast_food": ("amenity", "fast_food"),
    "restaurant": ("amenity", "restaurant"),
    "cafe": ("amenity", "cafe"),
    "fuel": ("amenity", "fuel"),
    "pharmacy": ("amenity", "pharmacy"),
}

# --- Réglages ---
REVERSE_GEOCODE = True  # False = plus rapide, mais certaines options sans adresse
TRANCHES = [("début", 0.05, 0.35), ("milieu", 0.35, 0.65), ("fin", 0.65, 0.90)]
PAR_TRANCHE = 4  # candidats gardés par tranche avant le calcul des détours

STATE = {}  # dernière recherche, pour valider un choix ensuite
_GEO_CACHE = {}
_last_nominatim = 0.0


def _dist_m(a, b):
    lat1, lon1, lat2, lon2 = map(radians, (a[0], a[1], b[0], b[1]))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * asin(sqrt(h))


def _nominatim(path, params):
    """Appel Nominatim avec respect de la limite d'une requête par seconde."""
    global _last_nominatim
    wait = 1.1 - (time.time() - _last_nominatim)
    if wait > 0:
        time.sleep(wait)
    r = requests.get(f"{NOMINATIM}/{path}", params={**params, "format": "json"},
                     headers=UA, timeout=15)
    _last_nominatim = time.time()
    r.raise_for_status()
    return r.json()


def geocode(query):
    key = query.strip().lower()
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]
    data = _nominatim("search", {"q": query, "limit": 1})
    if not data:
        return None
    result = (float(data[0]["lat"]), float(data[0]["lon"]))
    _GEO_CACHE[key] = result
    return result


def reverse_address(lat, lon):
    try:
        a = _nominatim("reverse", {"lat": lat, "lon": lon, "zoom": 18}).get("address", {})
    except (requests.RequestException, ValueError):
        return ""
    street = " ".join(filter(None, [a.get("house_number"), a.get("road")]))
    city = a.get("city") or a.get("town") or a.get("village") or ""
    return ", ".join(filter(None, [street, city]))


def route(points):
    """points: liste de (lat, lon). Retourne (durée_s, distance_m, tracé)."""
    coords = ";".join(f"{lon},{lat}" for lat, lon in points)
    r = requests.get(
        f"{OSRM}/route/v1/driving/{coords}",
        params={"overview": "full", "geometries": "geojson"},
        timeout=20,
    )
    r.raise_for_status()
    rt = r.json()["routes"][0]
    line = [(lat, lon) for lon, lat in rt["geometry"]["coordinates"]]
    return rt["duration"], rt["distance"], line


def durations_matrix(points):
    """Matrice des durées (secondes) entre tous les points, en un seul appel."""
    coords = ";".join(f"{lon},{lat}" for lat, lon in points)
    r = requests.get(f"{OSRM}/table/v1/driving/{coords}",
                     params={"annotations": "duration"}, timeout=25)
    r.raise_for_status()
    return r.json()["durations"]


def _osm_address(tags):
    street = " ".join(filter(None, [tags.get("addr:housenumber"), tags.get("addr:street")]))
    return ", ".join(filter(None, [street, tags.get("addr:city", "")]))


def places_along(line, category, radius=1500, n_points=12):
    key, value = CATEGORIES[category]
    step = max(1, len(line) // n_points)
    sampled = line[::step]
    poly = ",".join(f"{lat:.5f},{lon:.5f}" for lat, lon in sampled)
    query = f'[out:json][timeout:20];nwr["{key}"="{value}"](around:{radius},{poly});out center 60;'

    data = None
    for attempt in range(2):
        for server in OVERPASS_SERVERS:
            try:
                r = requests.post(server, data={"data": query}, headers=UA, timeout=30)
                r.raise_for_status()
                data = r.json()
                break
            except (requests.RequestException, ValueError):
                continue
        if data:
            break
        time.sleep(2)

    if not data:
        return []

    places, seen = [], set()
    for el in data["elements"]:
        tags = el.get("tags", {})
        name = tags.get("name")
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")
        if not (name and lat and lon):
            continue
        dedup_key = (name, round(lat, 3), round(lon, 3))
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        places.append({
            "name": name, "lat": lat, "lon": lon,
            "adresse": _osm_address(tags),
            "horaires": tags.get("opening_hours", ""),
        })
    return places


def _profile_route(line):
    """Échantillonne le tracé (~200 points) et calcule la distance cumulée."""
    step = max(1, len(line) // 200)
    pts = line[::step]
    if pts[-1] != line[-1]:
        pts.append(line[-1])
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[i - 1] + _dist_m(pts[i - 1], pts[i]))
    return pts, cum


def _position_on_route(place, pts, cum):
    """Retourne (distance au tracé en m, avancement entre 0 et 1)."""
    p = (place["lat"], place["lon"])
    best_i, best_d = 0, float("inf")
    for i, s in enumerate(pts):
        d = _dist_m(p, s)
        if d < best_d:
            best_i, best_d = i, d
    return best_d, (cum[best_i] / cum[-1] if cum[-1] else 0.0)


def find_stops(origin, destination, category, max_results=3):
    a, b = geocode(origin), geocode(destination)
    if not a or not b:
        return {"error": "Lieu introuvable"}
    _, _, line = route([a, b])

    places = places_along(line, category)
    if not places:
        return {"error": "Service de recherche de lieux indisponible, réessaie dans un instant."}

    pts, cum = _profile_route(line)
    for p in places:
        p["dist_trace"], p["frac"] = _position_on_route(p, pts, cum)

    # Candidats : les plus proches du tracé dans chaque tranche du trajet
    candidates = []
    for nom, lo, hi in TRANCHES:
        in_bucket = sorted((p for p in places if lo <= p["frac"] < hi),
                           key=lambda p: p["dist_trace"])
        for p in in_bucket[:PAR_TRANCHE]:
            p["avancement"] = nom
            candidates.append(p)
    if not candidates:  # repli : aucun lieu dans les tranches
        candidates = sorted(places, key=lambda p: p["dist_trace"])[:8]
        for p in candidates:
            p["avancement"] = ""

    # Détour de chaque candidat : un seul appel OSRM pour tous
    matrix = durations_matrix([a, b] + [(p["lat"], p["lon"]) for p in candidates])
    base = matrix[0][1]
    if base is None:
        return {"error": "Itinéraire introuvable"}
    for i, p in enumerate(candidates, start=2):
        to_p, from_p = matrix[0][i], matrix[i][1]
        p["detour_min"] = (None if to_p is None or from_p is None
                           else round((to_p + from_p - base) / 60, 1))
    candidates = [p for p in candidates if p["detour_min"] is not None]
    if not candidates:
        return {"error": "Aucun arrêt accessible trouvé"}

    # Meilleur détour par tranche, puis on complète avec les meilleurs restants
    chosen = []
    for nom, _, _ in TRANCHES:
        group = [p for p in candidates if p["avancement"] == nom]
        if group:
            chosen.append(min(group, key=lambda p: p["detour_min"]))
    rest = sorted((p for p in candidates if p not in chosen), key=lambda p: p["detour_min"])
    chosen += rest[:max(0, max_results - len(chosen))]
    chosen = sorted(chosen[:max_results], key=lambda p: p["frac"])  # dans l'ordre du trajet

    # Adresse manquante dans OSM : on la retrouve pour les options finales
    if REVERSE_GEOCODE:
        for opt in chosen:
            if not opt["adresse"]:
                opt["adresse"] = reverse_address(opt["lat"], opt["lon"])

    for i, opt in enumerate(chosen, start=1):
        opt["numero"] = i

    STATE.clear()
    STATE.update({"origin": a, "destination": b, "options": chosen})

    public = [
        {"numero": o["numero"], "name": o["name"], "adresse": o["adresse"],
         "avancement": o["avancement"], "detour_min": o["detour_min"],
         "horaires": o["horaires"]}
        for o in chosen
    ]
    return {"trajet_direct_min": round(base / 60), "options": public}


def build_links(origin, stop, destination):
    o = f"{origin[0]},{origin[1]}"
    s = f"{stop[0]},{stop[1]}"
    d = f"{destination[0]},{destination[1]}"
    return {
        "google_maps": f"https://www.google.com/maps/dir/?api=1&origin={o}&destination={d}&waypoints={s}&travelmode=driving",
        "apple_plans": f"https://maps.apple.com/?saddr={o}&daddr={s}+to:{d}&dirflg=d",
    }


def select_stop(numero):
    options = STATE.get("options")
    if not options:
        return {"error": "Aucune recherche en cours."}
    if not 1 <= numero <= len(options):
        return {"error": f"Choisis un numéro entre 1 et {len(options)}."}
    opt = options[numero - 1]
    links = build_links(STATE["origin"], (opt["lat"], opt["lon"]), STATE["destination"])
    return {"arret": opt["name"], "adresse": opt["adresse"], "links": links}


if __name__ == "__main__":
    t0 = time.time()
    print(find_stops("Cergy", "Paris", "fast_food"))
    print(f"[{time.time() - t0:.1f} s]")
    print(select_stop(1))