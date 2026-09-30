import json
import time
import ollama
from tools import find_stops, select_stop

MODEL = "qwen2.5:3b"
SYSTEM = (
    "Tu es un copilote vocal en voiture. Réponds en français, en trois phrases "
    "maximum. Pour toute demande d'arrêt sur le trajet, utilise l'outil "
    "chercher_arrets, puis présente les options numérotées avec le nom, la rue "
    "ou la ville, le détour en minutes et leur position sur le trajet (champ "
    "avancement : début, milieu ou fin), et demande laquelle choisir. "
    "Ne parle des horaires que si on te les demande. Quand l'utilisateur choisit "
    "une option (ex. « le deuxième »), utilise l'outil choisir_arret avec son "
    "numéro, puis confirme que l'itinéraire est prêt. Le lien est affiché "
    "automatiquement à l'écran : ne l'écris jamais toi-même."
)


def chercher_arrets(origin: str, destination: str, category: str) -> str:
    """Cherche des arrêts sur le trajet et calcule le détour de chacun.

    Args:
        origin: Ville ou adresse de départ.
        destination: Ville ou adresse d'arrivée.
        category: Un parmi fast_food, restaurant, cafe, fuel, pharmacy.
    """
    t0 = time.time()
    result = find_stops(origin, destination, category)
    print(f"[recherche : {time.time() - t0:.1f} s]")
    return json.dumps(result, ensure_ascii=False)


def choisir_arret(numero: int) -> str:
    """Valide l'arrêt choisi par l'utilisateur et prépare l'itinéraire modifié.

    Args:
        numero: Numéro de l'option choisie (1, 2 ou 3).
    """
    res = select_stop(int(numero))
    if "links" in res:
        print("\n--- Itinéraire prêt ---")
        print(f"Arrêt : {res['arret']} ({res['adresse']})")
        print(f"Google Maps : {res['links']['google_maps']}")
        print(f"Apple Plans : {res['links']['apple_plans']}")
        print("-----------------------\n")
        return json.dumps({"ok": True, "arret": res["arret"]}, ensure_ascii=False)
    return json.dumps(res, ensure_ascii=False)


TOOLS = {"chercher_arrets": chercher_arrets, "choisir_arret": choisir_arret}

messages = [{"role": "system", "content": SYSTEM}]

while True:
    messages.append({"role": "user", "content": input("> ")})
    while True:
        resp = ollama.chat(model=MODEL, messages=messages, tools=list(TOOLS.values()))
        messages.append(resp.message)
        if not resp.message.tool_calls:
            print(resp.message.content)
            break
        for call in resp.message.tool_calls:
            try:
                result = TOOLS[call.function.name](**call.function.arguments)
            except Exception as e:
                result = json.dumps({"error": f"Échec de l'outil : {e}"}, ensure_ascii=False)
            messages.append({"role": "tool", "content": result,
                             "tool_name": call.function.name})