import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from math import radians, sin, cos, asin, sqrt

import requests

from poi_local import available as local_db_available, places_local

UA = {"User-Agent": "copilote-vocal-proto/0.1 (projet perso)"}
OSRM = "https://router.project-osrm.org"
NOMINATIM = "https://nominatim.openstreetmap.org"
IDF_VIEWBOX = "1.446,49.241,3.559,48.120"   # Île-de-France : gauche, haut, droite, bas
OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
]

CATEGORIES = {
    "fast_food": ("amenity", "fast_food"),
    "restaurant": ("amenity", "restaurant"),
    "cafe": ("amenity", "cafe"),
    "fuel": ("amenity", "fuel"),
    "pharmacy": ("amenity", "pharmacy"),
}

# --- Réglages ---
DEBUG = True            # affiche le temps de chaque étape
REVERSE_GEOCODE = True  # False = plus rapide, mais certaines options sans adresse
TRANCHES = [("début", 0.05, 0.35), ("milieu", 0.35, 0.65), ("fin", 0.65, 0.90)]
PAR_TRANCHE = 4         # candidats gardés par tranche avant le calcul des détours
CACHE_FILE = ".cache/places.json"
CACHE_TTL = 24 * 3600

STATE = {}  # dernière recherche, pour valider un choix ensuite
_GEO_CACHE = {}
_last_nominatim = 0.0
_nominatim_lock = threading.Lock()


def _log(label, t0):
    if DEBUG:
        print(f"   [{label} : {time.time() - t0:.1f} s]")


def _dist_m(a, b):
    lat1, lon1, lat2, lon2 = map(radians, (a[0], a[1], b[0], b[1]))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * asin(sqrt(h))


# ---------- Cache disque (Overpass) ----------
def _cache_get(key):
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            entry = json.load(f).get(key)
        if entry and time.time() - entry["t"] < CACHE_TTL:
            return entry["places"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def _cache_set(key, places):
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        data[key] = {"t": time.time(), "places": places}
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError:
        pass


# ---------- Nominatim ----------
def _nominatim(path, params):
    """Appel Nominatim : une seule requête à la fois, 1 par seconde maximum."""
    global _last_nominatim
    with _nominatim_lock:
        wait = 1.1 - (time.time() - _last_nominatim)
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.get(f"{NOMINATIM}/{path}", params={**params, "format": "json"},
                             headers=UA, timeout=15)
        finally:
            _last_nominatim = time.time()
        r.raise_for_status()
        return r.json()


def _parse_coords(query):
    """Reconnaît une position « lat,lon » (ex. 48.85660,2.35220)."""
    parts = query.split(",")
    if len(parts) == 2:
        try:
            lat, lon = float(parts[0]), float(parts[1])
        except ValueError:
            return None
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
    return None


def geocode(query):
    coords = _parse_coords(query)
    if coords:
        return coords
    key = query.strip().lower()
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]
    # 1) recherche normale ; 2) si rien, on cherche uniquement en Île-de-France (utile pour les noms de lieux)
    for extra in ({}, {"viewbox": IDF_VIEWBOX, "bounded": 1}):
        data = _nominatim("search", {"q": query, "limit": 1, **extra})
        if data:
            result = (float(data[0]["lat"]), float(data[0]["lon"]))
            _GEO_CACHE[key] = result
            return result
    return None


def reverse_address(lat, lon):
    try:
        a = _nominatim("reverse", {"lat": lat, "lon": lon, "zoom": 18}).get("address", {})
    except (requests.RequestException, ValueError):
        return ""
    street = " ".join(filter(None, [a.get("house_number"), a.get("road")]))
    city = a.get("city") or a.get("town") or a.get("village") or ""
    return ", ".join(filter(None, [street, city]))


# ---------- OSRM ----------
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


# ---------- Overpass (secours) ----------
def _osm_address(tags):
    street = tags.get("addr:street")
    if not street:
        return ""  # un numéro seul n'est pas une adresse exploitable
    num = tags.get("addr:housenumber", "")
    return ", ".join(filter(None, [f"{num} {street}".strip(), tags.get("addr:city", "")]))


def _overpass_query(query, first_server):
    """Essaie les serveurs à tour de rôle (en commençant par first_server), délai court."""
    servers = OVERPASS_SERVERS[first_server:] + OVERPASS_SERVERS[:first_server]
    for server in servers:
        t0 = time.time()
        host = server.split("/")[2]
        try:
            r = requests.post(server, data={"data": query}, headers=UA, timeout=(5, 14))
            r.raise_for_status()
            data = r.json()
            if data.get("remark") and not data.get("elements"):
                raise ValueError(data["remark"])
            _log(f"overpass {host} ok", t0)
            return data
        except (requests.RequestException, ValueError) as e:
            code = getattr(getattr(e, "response", None), "status_code", "")
            _log(f"overpass {host} échec ({type(e).__name__} {code})", t0)
    return None


def places_along(line, category, radius=1500, n_points=12):
    """Retourne (lieux, complet). complet = False si une des requêtes a échoué."""
    key, value = CATEGORIES[category]
    sampled = line[::max(1, len(line) // n_points)]
    n = len(sampled)

    # Trois segments du trajet (qui se recouvrent d'un point), un par requête
    bounds = [0, n // 3, 2 * n // 3, n]
    jobs = []
    for i in range(3):
        seg = sampled[max(0, bounds[i] - 1):bounds[i + 1]]
        if not seg:
            continue
        poly = ",".join(f"{lat:.5f},{lon:.5f}" for lat, lon in seg)
        query = (f'[out:json][timeout:12];nwr["{key}"="{value}"]'
                 f'(around:{radius},{poly});out center 30;')
        jobs.append((query, i % len(OVERPASS_SERVERS)))

    with ThreadPoolExecutor(max_workers=3) as ex:
        results = list(ex.map(lambda j: _overpass_query(*j), jobs))

    places, seen = [], set()
    for data in results:
        if not data:
            continue
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
                "addr_ok": bool(tags.get("addr:street") and tags.get("addr:city")),
                "horaires": tags.get("opening_hours", ""),
            })
    return places, all(r is not None for r in results)


# ---------- Position sur le trajet ----------
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


# ---------- Recherche d'arrêts ----------
def find_stops(origin, destination, category, max_results=3, name_like=None):
    """category : une des catégories (ou None si on cherche une enseigne précise).
    name_like : morceau de nom d'enseigne, ex. « mcdonald »."""
    t_all = time.time()

    t0 = time.time()
    a, b = geocode(origin), geocode(destination)
    _log("géocodage", t0)
    if not a or not b:
        return {"error": "Lieu introuvable"}

    t0 = time.time()
    _, _, line = route([a, b])
    _log("itinéraire", t0)

    # 1) Base locale (rapide). 2) Overpass en secours si la base est absente ou vide.
    places = None
    local_used = local_db_available()
    if local_used:
        t0 = time.time()
        places = places_local(line, category, name_like=name_like)
        _log(f"lieux base locale ({len(places)} trouvés)", t0)

    if not places and not (local_used and name_like):
        if category not in CATEGORIES:
            return {"error": "Type de lieu manquant", "code": "category"}
        cache_key = f"{a[0]:.3f},{a[1]:.3f}|{b[0]:.3f},{b[1]:.3f}|{category}"
        places = _cache_get(cache_key)
        if places is not None:
            _log("lieux (cache)", time.time())
        else:
            t0 = time.time()
            places, complete = places_along(line, category)
            _log(f"lieux Overpass ({len(places)} trouvés)", t0)
            if places and complete:
                _cache_set(cache_key, places)

    if name_like and places:
        nl = name_like.lower()
        places = [p for p in places if nl in p["name"].lower()]
    if not places:
        if name_like:
            return {"error": "Aucun lieu correspondant", "code": "none"}
        return {"error": "Service de recherche de lieux indisponible, réessaie dans un instant."}

    t0 = time.time()
    pts, cum = _profile_route(line)
    for p in places:
        p["dist_trace"], p["frac"] = _position_on_route(p, pts, cum)
    _log("position sur le trajet", t0)

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
    t0 = time.time()
    matrix = durations_matrix([a, b] + [(p["lat"], p["lon"]) for p in candidates])
    _log("calcul des détours", t0)
    base = matrix[0][1]
    if base is None:
        return {"error": "Itinéraire introuvable"}
    for i, p in enumerate(candidates, start=2):
        to_p, from_p = matrix[0][i], matrix[i][1]
        p["detour_min"] = (None if to_p is None or from_p is None
                           else round(max(0.0, to_p + from_p - base) / 60, 1))
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

    # Adresse absente ou incomplète dans OSM : on la retrouve pour les options finales
    if REVERSE_GEOCODE:
        t0 = time.time()
        for opt in chosen:
            if not opt.get("addr_ok"):
                opt["adresse"] = reverse_address(opt["lat"], opt["lon"]) or opt["adresse"]
        _log("adresses", t0)

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
    _log("TOTAL find_stops", t_all)
    return {"trajet_direct_min": round(base / 60), "options": public}


# ---------- Liens et carte ----------
def build_links(origin, stop, destination):
    o = f"{origin[0]},{origin[1]}"
    s = f"{stop[0]},{stop[1]}"
    d = f"{destination[0]},{destination[1]}"
    return {
        "google_maps": f"https://www.google.com/maps/dir/?api=1&origin={o}&destination={d}&waypoints={s}&travelmode=driving",
        "apple_plans": f"https://maps.apple.com/?saddr={o}&daddr={s}+to:{d}&dirflg=d",
    }


def _dp_simplify(line, tol_m=6.0):
    """Allège un tracé en gardant sa forme (Douglas-Peucker), pour l'envoyer au téléphone."""
    n = len(line)
    if n <= 2:
        return [[round(lat, 5), round(lon, 5)] for lat, lon in line]
    ky = 110540.0
    kx = 111320.0 * cos(radians(line[0][0]))
    pts = [(lon * kx, lat * ky) for lat, lon in line]
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay = pts[a]
        dx, dy = pts[b][0] - ax, pts[b][1] - ay
        l2 = dx * dx + dy * dy
        best, bi = 0.0, -1
        for i in range(a + 1, b):
            px, py = pts[i][0] - ax, pts[i][1] - ay
            if l2 == 0:
                d = sqrt(px * px + py * py)
            else:
                t = max(0.0, min(1.0, (px * dx + py * dy) / l2))
                ex, ey = px - t * dx, py - t * dy
                d = sqrt(ex * ex + ey * ey)
            if d > best:
                best, bi = d, i
        if best > tol_m and bi != -1:
            keep[bi] = True
            stack.append((a, bi))
            stack.append((bi, b))
    return [[round(line[i][0], 5), round(line[i][1], 5)] for i in range(n) if keep[i]]


# ---------- Instructions de guidage en français ----------
_ORD_FR = {1: "première", 2: "deuxième", 3: "troisième", 4: "quatrième", 5: "cinquième",
           6: "sixième", 7: "septième", 8: "huitième", 9: "neuvième", 10: "dixième"}
_MOD_FR = {
    "left": ("à gauche", "left"),
    "right": ("à droite", "right"),
    "slight left": ("légèrement à gauche", "slight_left"),
    "slight right": ("légèrement à droite", "slight_right"),
    "sharp left": ("franchement à gauche", "sharp_left"),
    "sharp right": ("franchement à droite", "sharp_right"),
    "straight": ("tout droit", "straight"),
    "uturn": ("demi-tour", "uturn"),
}


def _step_fr(step, leg_index, n_legs):
    """Convertit une étape OSRM en instruction française (ou None si elle est inutile à dire)."""
    m = step.get("maneuver", {})
    typ, mod = m.get("type", ""), m.get("modifier", "")
    loc = m.get("location") or [0, 0]
    road = (step.get("name") or "").strip() or (step.get("ref") or "").strip()
    sur = f" sur {road}" if road else ""
    where, icon = _MOD_FR.get(mod, ("", "straight"))
    kind = "turn"

    if typ == "depart":
        if leg_index > 0:
            return None  # le départ de la deuxième partie (après l'arrêt) ne s'annonce pas
        text, icon, kind = f"Démarrez{sur}", "depart", "depart"
    elif typ == "arrive":
        if leg_index < n_legs - 1:
            text, icon, kind = "Vous êtes arrivé à votre arrêt", "stop", "stop"
        else:
            text, icon, kind = "Vous êtes arrivé à destination", "arrive", "arrive"
    elif typ == "turn":
        if mod == "straight":
            text = f"Continuez tout droit{sur}"
        elif mod == "uturn":
            text = f"Faites demi-tour{sur}"
        else:
            text = f"Tournez {where}{sur}"
    elif typ == "end of road":
        text = f"Au bout de la route, tournez {where}{sur}" if where else f"Au bout de la route, continuez{sur}"
    elif typ == "fork":
        if mod in ("left", "right", "slight left", "slight right"):
            side = "à gauche" if "left" in mod else "à droite"
            text = f"Restez {side} à la bifurcation{sur}"
            icon = "fork_left" if "left" in mod else "fork_right"
        else:
            text = f"Continuez à la bifurcation{sur}"
    elif typ == "merge":
        text = f"Insérez-vous{sur}"
    elif typ == "on ramp":
        text = f"Prenez la bretelle {where}{sur}" if where else f"Prenez la bretelle{sur}"
    elif typ == "off ramp":
        first = (step.get("destinations") or "").split(",")[0]
        dest = first.split(":")[-1].strip()
        text = f"Prenez la sortie {where}" if where else "Prenez la sortie"
        text += f" en direction de {dest}" if dest else sur
    elif typ in ("roundabout", "rotary"):
        ex = m.get("exit")
        if ex:
            text = f"Au rond-point, prenez la {_ORD_FR.get(ex, str(ex) + 'e')} sortie{sur}"
        else:
            text = f"Prenez le rond-point{sur}"
        icon, kind = "roundabout", "roundabout"
    elif typ in ("roundabout turn", "rotary turn"):
        text = f"Au rond-point, tournez {where}{sur}" if where else f"Au rond-point, continuez{sur}"
        icon, kind = "roundabout", "roundabout"
    elif typ == "continue" and mod not in ("", "straight"):
        text = f"Continuez {where}{sur}"
    else:
        return None  # changement de nom de rue, sortie de rond-point... : inutile à annoncer

    return {"text": text, "icon": icon, "kind": kind, "name": road,
            "lat": round(loc[1], 5), "lon": round(loc[0], 5)}


def _speed_profile(legs):
    """Vitesse (km/h) le long du trajet, d'après les vitesses de la route renvoyées par OSRM.
    Retourne {"total": mètres, "runs": [[départ_m, km/h], ...]} ou None. Jamais au-dessus de 130 km/h."""
    runs, d = [], 0.0
    for leg in legs:
        ann = leg.get("annotation") or {}
        for v, dd in zip(ann.get("speed") or [], ann.get("distance") or []):
            kmh = 5 * round(min(130.0, max(5.0, v * 3.6)) / 5)   # arrondi à 5 km/h
            if not runs or runs[-1][1] != kmh:
                runs.append([round(d), kmh])
            d += dd
    return {"total": round(d), "runs": runs} if runs else None


def route_full(points):
    """Comme route(), avec les instructions détaillées et les vitesses de la route.
    Retourne (durée_s, distance_m, tracé, étapes, vitesses)."""
    coords = ";".join(f"{lon},{lat}" for lat, lon in points)
    r = requests.get(
        f"{OSRM}/route/v1/driving/{coords}",
        params={"overview": "full", "geometries": "geojson", "steps": "true",
                "annotations": "speed,distance"},
        timeout=25,
    )
    r.raise_for_status()
    rt = r.json()["routes"][0]
    line = [(lat, lon) for lon, lat in rt["geometry"]["coordinates"]]
    steps = []
    legs = rt.get("legs", [])
    for li, leg in enumerate(legs):
        for st in leg.get("steps", []):
            item = _step_fr(st, li, len(legs))
            if item:
                steps.append(item)
    return rt["duration"], rt["distance"], line, steps, _speed_profile(legs)


def build_map(points):
    """points : liste de (nom, rôle, (lat, lon)) dans l'ordre du trajet.
    Rôles : start, stop, end. Retourne les données de la carte et du guidage (ou None)."""
    try:
        dur, dist, line, steps, speeds = route_full([p[2] for p in points])
    except Exception as e:
        print(f"[carte : {e}]")
        return None
    return {
        "points": [{"name": n, "role": r, "lat": c[0], "lon": c[1]} for n, r, c in points],
        "line": _dp_simplify(line),
        "steps": steps,
        "speeds": speeds,
        "duration_min": round(dur / 60),
        "duration_s": round(dur),
        "distance_km": round(dist / 1000, 1),
        "distance_m": round(dist),
    }


def select_stop(numero):
    options = STATE.get("options")
    if not options:
        return {"error": "Aucune recherche en cours."}
    if not 1 <= numero <= len(options):
        return {"error": f"Choisis un numéro entre 1 et {len(options)}."}
    opt = options[numero - 1]
    stop = (opt["lat"], opt["lon"])
    links = build_links(STATE["origin"], stop, STATE["destination"])
    links["map"] = build_map([
        ("Départ", "start", STATE["origin"]),
        (opt["name"], "stop", stop),
        ("Arrivée", "end", STATE["destination"]),
    ])
    return {"arret": opt["name"], "adresse": opt["adresse"], "links": links}


def trip_links(origin, destination):
    """Liens d'itinéraire direct (sans arrêt) et données de la carte."""
    a, b = geocode(origin), geocode(destination)
    if not a or not b:
        return {"error": "Je n'ai pas trouvé ce trajet."}
    o, d = f"{a[0]},{a[1]}", f"{b[0]},{b[1]}"
    links = {
        "google_maps": f"https://www.google.com/maps/dir/?api=1&origin={o}&destination={d}&travelmode=driving",
        "apple_plans": f"https://maps.apple.com/?saddr={o}&daddr={d}&dirflg=d",
    }
    links["map"] = build_map([(origin, "start", a), (destination, "end", b)])
    return {"links": links}


if __name__ == "__main__":
    print(find_stops("Cergy", "Paris", "restaurant"))
    print(select_stop(1))