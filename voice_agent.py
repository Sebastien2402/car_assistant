import json
import re
import threading
import time

import ollama

from tools import CATEGORIES, STATE, find_stops, select_stop
from voice import Speaker, listen

MODEL = "qwen2.5:3b"
EXIT_WORDS = ("au revoir", "quitter", "on arrête")

ROUTER = (
    "Tu analyses la phrase d'un conducteur et tu réponds en JSON.\n"
    "- action = chercher : il veut s'arrêter quelque part sur son trajet. "
    "Renseigne origin et destination (villes ou adresses citées) et category : "
    "fast_food (fast-food, burger, McDo, j'ai faim), restaurant, cafe (café, pause), "
    "fuel (essence, carburant, plein), pharmacy (pharmacie).\n"
    "- action = choisir : il choisit une option déjà proposée. numero = 1 pour "
    "premier ou première, 2 pour deuxième, 3 pour troisième.\n"
    "- action = autre : tout le reste. reponse = une réponse courte en français "
    "(deux phrases maximum, sans symboles). Si la phrase est incompréhensible, "
    "reponse = « Je n'ai pas bien compris, tu peux répéter ? »."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["chercher", "choisir", "autre"]},
        "origin": {"type": "string"},
        "destination": {"type": "string"},
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "numero": {"type": "integer"},
        "reponse": {"type": "string"},
    },
    "required": ["action"],
}

ORDINALS = [
    (1, r"premi[eè]r"),
    (2, r"deuxi[eè]me|\ble second\b"),
    (3, r"troisi[eè]me"),
]
RANGS = ["Première", "Deuxième", "Troisième"]
POS = {"début": "au début du trajet", "milieu": "vers le milieu du trajet",
       "fin": "vers la fin du trajet"}

speaker = Speaker()


def warm_up():
    """Charge le modèle en mémoire pendant le message d'accueil."""
    try:
        ollama.generate(model=MODEL, prompt="", keep_alive="30m")
    except Exception as e:
        print(f"[préchauffage : {e}]")


def comprendre(text):
    try:
        resp = ollama.chat(
            model=MODEL,
            messages=[{"role": "system", "content": ROUTER},
                      {"role": "user", "content": text}],
            format=SCHEMA, options={"temperature": 0}, keep_alive="30m",
        )
        return json.loads(resp.message.content)
    except Exception as e:
        print(f"[compréhension : {e}]")
        return {"action": "autre", "reponse": "Je n'ai pas bien compris, tu peux répéter ?"}


def quick_choice(text):
    t = text.lower()
    for n, pattern in ORDINALS:
        if re.search(pattern, t):
            return n
    return None


def minutes(m):
    n = round(m)
    if n <= 0:
        return "moins d'une minute de détour"
    return "1 minute de détour" if n == 1 else f"{n} minutes de détour"


def annonce(res):
    phrases = []
    for o in res["options"]:
        lieu = o["name"] + (f", {o['adresse']}" if o["adresse"] else "")
        pos = POS.get(o.get("avancement", ""), "")
        phrases.append(f"{RANGS[o['numero'] - 1]} option : {lieu}, {minutes(o['detour_min'])}"
                       + (f", {pos}" if pos else "") + ".")
    phrases.append("Laquelle tu choisis ?")
    return " ".join(phrases)


def traiter(heard, trip):
    """Retourne la phrase à dire. Appelle les outils selon l'intention détectée."""
    numero = quick_choice(heard) if STATE.get("options") else None
    out = {"action": "choisir", "numero": numero} if numero else comprendre(heard)
    action = out.get("action", "autre")

    if action == "chercher":
        origin = (out.get("origin") or "").strip() or trip["origin"]
        dest = (out.get("destination") or "").strip() or trip["destination"]
        category = out.get("category")
        if category not in CATEGORIES:
            category = "fast_food"
        if not origin or not dest:
            return "Je n'ai pas bien compris ton trajet. D'où pars-tu, et où vas-tu ?"
        trip.update(origin=origin, destination=dest)

        speaker.say("Je cherche, un instant.")
        t1 = time.time()
        try:
            res = find_stops(origin, dest, category)
        except Exception as e:
            print(f"[recherche : erreur {e}]")
            return "Je n'arrive pas à joindre le service de cartes, réessaie dans un instant."
        print(f"[recherche : {time.time() - t1:.1f} s]")
        if "error" in res:
            print(f"[recherche : {res['error']}]")
            return "Je n'ai rien trouvé pour l'instant, on réessaie ?"
        return annonce(res)

    if action == "choisir":
        if not STATE.get("options"):
            return "Je n'ai pas encore proposé d'arrêt. Dis-moi ce que tu cherches."
        res = select_stop(int(out.get("numero") or 0))
        if "links" not in res:
            return res["error"]
        print("\n--- Itinéraire prêt ---")
        print(f"Arrêt : {res['arret']} ({res['adresse']})")
        print(f"Google Maps : {res['links']['google_maps']}")
        print(f"Apple Plans : {res['links']['apple_plans']}")
        print("-----------------------\n")
        STATE.clear()
        return f"C'est noté, on passe par {res['arret']}. Ton itinéraire est prêt."

    return out.get("reponse") or "Je n'ai pas bien compris, tu peux répéter ?"


def main():
    threading.Thread(target=warm_up, daemon=True).start()
    trip = {"origin": "", "destination": ""}
    speaker.say("Salut ! Où est-ce qu'on va ?")
    speaker.wait()

    while True:
        print("\nJ'écoute...")
        heard = listen()
        if len(heard) < 3:
            continue
        print(f"Toi : {heard}")
        if any(w in heard.lower() for w in EXIT_WORDS):
            speaker.say("À bientôt !")
            speaker.wait()
            break

        t0 = time.time()
        reply = traiter(heard, trip)
        print(f"Copilote : {reply}   [{time.time() - t0:.1f} s]")
        speaker.say(reply)
        speaker.wait()  # on n'écoute qu'une fois la réponse terminée (pas d'écho)


if __name__ == "__main__":
    main()