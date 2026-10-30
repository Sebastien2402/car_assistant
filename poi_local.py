import os
import sqlite3
from math import cos, radians

import numpy as np

DB_PATH = "data/poi.db"


def available():
    return os.path.exists(DB_PATH)


def _address(street, num, city):
    if not street:
        return ""
    return ", ".join(filter(None, [f"{num or ''} {street}".strip(), city or ""]))


def places_local(line, category, radius=1500, name_like=None):
    """Lieux à moins de `radius` mètres du tracé (liste de dicts).

    category : une des catégories, ou None pour chercher dans toutes.
    name_like : morceau de nom d'enseigne (ex. « mcdonald »), optionnel.
    """
    pts = np.array(line[::max(1, len(line) // 2000)], dtype=float)  # (M, 2) : lat, lon
    k_lat = 111_000.0
    k_lon = 111_000.0 * cos(radians(float(pts[:, 0].mean())))
    d_lat, d_lon = radius / k_lat, radius / k_lon

    conds = ["lat BETWEEN ? AND ?", "lon BETWEEN ? AND ?"]
    params = [pts[:, 0].min() - d_lat, pts[:, 0].max() + d_lat,
              pts[:, 1].min() - d_lon, pts[:, 1].max() + d_lon]
    if category:
        conds.append("category = ?")
        params.append(category)
    if name_like:
        conds.append("lower(name) LIKE ?")
        params.append(f"%{name_like.lower()}%")

    con = sqlite3.connect(DB_PATH)
    try:
        rows = con.execute(
            "SELECT name, lat, lon, street, housenumber, city, horaires FROM poi WHERE "
            + " AND ".join(conds), params).fetchall()
    finally:
        con.close()
    if not rows:
        return []

    # Distance de chaque lieu au tracé (approximation plane, très suffisante ici)
    coords = np.array([(r[1], r[2]) for r in rows], dtype=float)
    dist = np.empty(len(rows))
    for i in range(0, len(rows), 500):
        chunk = coords[i:i + 500]
        dy = (chunk[:, 0:1] - pts[None, :, 0]) * k_lat
        dx = (chunk[:, 1:2] - pts[None, :, 1]) * k_lon
        dist[i:i + 500] = np.sqrt(dx * dx + dy * dy).min(axis=1)

    places = []
    for (name, lat, lon, street, num, city, horaires), d in zip(rows, dist):
        if d > radius:
            continue
        places.append({
            "name": name, "lat": lat, "lon": lon,
            "adresse": _address(street, num, city),
            "addr_ok": bool(street and city),
            "horaires": horaires or "",
        })
    return places

def places_in_box(south, west, north, east, categories, limit=1500):
    """Lieux d'une zone rectangulaire (carte affichée), pour les points sur la carte.
    S'il y en a trop, l'ordre « mélangé » mais fixe répartit les points gardés sur toute la zone."""
    if not available() or not categories:
        return []
    marks = ",".join("?" * len(categories))
    con = sqlite3.connect(DB_PATH)
    try:
        rows = con.execute(
            f"SELECT category, name, lat, lon, street, housenumber, city, horaires FROM poi "
            f"WHERE category IN ({marks}) AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? "
            f"ORDER BY (rowid * 7919) % 10007 LIMIT ?",
            [*categories, south, north, west, east, limit]).fetchall()
    finally:
        con.close()
    return [{"cat": c, "name": n, "lat": round(la, 6), "lon": round(lo, 6),
             "adresse": _address(st, num, city), "horaires": h or ""}
            for c, n, la, lo, st, num, city, h in rows]