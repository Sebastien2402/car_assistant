import json
import os
import threading
import time

FILE = "data/history.json"
MAX_ITEMS = 50
MERGE_WINDOW_S = 30 * 60   # même destination en moins de 30 min : une seule entrée
_lock = threading.Lock()


def _load():
    try:
        with open(FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(items):
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(tmp, FILE)


def add(destination, origin="", stop=None):
    """Enregistre un trajet (noms seulement, jamais de coordonnées GPS)."""
    destination = (destination or "").strip()
    if not destination:
        return
    now = time.time()
    with _lock:
        try:
            items = _load()
            entry = {"ts": now, "destination": destination, "origin": origin or "", "stop": stop}
            last = items[-1] if items else None
            if (last and last.get("destination", "").lower() == destination.lower()
                    and now - last.get("ts", 0) < MERGE_WINDOW_S):
                if not stop:
                    entry["stop"] = last.get("stop")
                items[-1] = entry
            else:
                items.append(entry)
            _save(items[-MAX_ITEMS:])
        except OSError as e:
            print(f"[historique : {e}]")


def recent(n=5):
    """Derniers trajets, du plus récent au plus ancien, sans doublon de destination."""
    with _lock:
        items = _load()
    seen, out = set(), []
    for e in reversed(items):
        dest = (e.get("destination") or "").strip()
        key = dest.lower()
        if dest and key not in seen:
            seen.add(key)
            out.append({"destination": dest, "stop": e.get("stop"), "ts": e.get("ts", 0)})
        if len(out) >= n:
            break
    return out


def clear():
    with _lock:
        try:
            os.remove(FILE)
        except OSError:
            pass