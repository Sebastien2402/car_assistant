import json
import re
import time

import ollama

import historique
from lieux import corrige_lieu
from tools import CATEGORIES, STATE, find_stops, geocode, select_stop, trip_links

MODEL = "qwen2.5:3b"
GREETING = "Salut ! Où est-ce qu'on va ?"
EXIT_WORDS = ("au revoir", "quitter", "on arrête")

ROUTER = (
    "Tu analyses la phrase d'un conducteur et tu réponds en JSON. "
    "Renseigne origin et destination uniquement si le conducteur les cite, "
    "sinon laisse-les vides. Recopie les noms de lieux exactement comme "
    "prononcés, avec le numéro d'arrondissement s'il y en a un (Paris 13e). "
    "S'il part de sa position (« d'ici », « de ma position »), laisse origin vide.\n"
    "- action = trajet : il cite un départ et/ou une arrivée (ville ou adresse) "
    "sans demander d'arrêt. Exemples : « je vais de Paris à Lyon » (origin = Paris, "
    "destination = Lyon) ; « emmène-moi à Lyon » (destination = Lyon, origin vide). "
    "S'il ne cite aucun lieu, ce n'est PAS un trajet.\n"
    "- action = chercher : il veut s'arrêter ou trouver un lieu sur la route, "
    "ou il change d'avis sur le lieu voulu (« finalement plutôt un Pizza Hut »). "
    "category = fast_food (fast-food, burger, McDo, sandwich, kebab), restaurant "
    "(restaurant, manger, repas, j'ai faim), cafe (café, pause), fuel (essence, "
    "carburant, plein), pharmacy (pharmacie). Si le type de lieu n'est pas clair, "
    "category = aucune. nom = le nom de l'enseigne UNIQUEMENT s'il demande une "
    "enseigne précise (McDonald's, Burger King, Pizza Hut, Quick, Total...), "
    "jamais pour un type de plat ou de lieu ; sinon laisse nom vide.\n"
    "- action = autre : tout le reste. reponse = une réponse courte en français "
    "(deux phrases maximum, sans symboles). Si la phrase est incompréhensible, "
    "reponse = « Je n'ai pas bien compris, tu peux répéter ? »."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["trajet", "chercher", "autre"]},
        "origin": {"type": "string"},
        "destination": {"type": "string"},
        "category": {"type": "string", "enum": list(CATEGORIES) + ["aucune"]},
        "nom": {"type": "string"},
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

# Paris huitième -> Paris 8e (arrondissements)
ORDINAUX = {
    "deuxième": 2, "troisième": 3, "quatrième": 4, "cinquième": 5, "sixième": 6,
    "septième": 7, "huitième": 8, "neuvième": 9, "dixième": 10, "onzième": 11,
    "douzième": 12, "treizième": 13, "quatorzième": 14, "quinzième": 15,
    "seizième": 16, "dix-septième": 17, "dix-huitième": 18, "dix-neuvième": 19,
    "vingtième": 20,
}

# Mots qui décrivent un type de plat ou de lieu, pas une enseigne
GENERIC = {"burger", "burgers", "pizza", "pizzas", "kebab", "sushi", "sushis", "café",
           "cafe", "restaurant", "fast-food", "fast food", "fastfood", "essence",
           "pharmacie", "sandwich", "tacos", "frites", "poulet", "station", "manger"}

# Catégorie déduite par mots-clés quand le modèle ne la trouve pas (le premier qui correspond gagne)
CATEGORY_KEYWORDS = [
    ("pharmacy", r"pharmacie|m[ée]dicament"),
    ("fuel", r"essence|carburant|station[- ]service|\bplein\b|gazole|diesel"),
    ("restaurant", r"restaurant|\bresto\b|brasserie|bistro"),
    ("fast_food", r"fast[- ]?food|burger|mc ?do|kebab|tacos|sandwich|frites|poulet|\bquick\b|\bkfc\b"),
    ("restaurant", r"pizz|sushi|d[ée]jeuner|d[iî]ner|repas|manger|faim"),
    ("cafe", r"caf[eé]|\bth[eé]\b|croissant|petit[- ]d[ée]j"),
]

# Réponses courtes comprises directement par le code (sans passer par le modèle)
NON = re.compile(
    r"\b(non|nan|pas besoin|pas maintenant|pas d'arr[êe]t|pas m'arr[êe]ter|"
    r"ne veux pas|rien|aucun|inutile|c'est bon|ça ira|ca ira|simplement|"
    r"continu\w*|tout droit|direct)\b", re.I)
NON_START = re.compile(r"^\W*(non|nan|nom|nous)\b", re.I)  # Non, je ne peux pas
OUI = re.compile(r"\b(oui|ouais|ouai|volontiers|carrément|yes|ok)\b", re.I)
ACK = re.compile(r"\b(merci|parfait|super|d'accord|ok|top|génial|nickel|très bien|cool)\b", re.I)
PLACE = re.compile(
    r"(burger|fast|mcdo|resto|restaurant|manger|repas|faim|caf[eé]|essence|"
    r"carburant|plein|station|pharmacie|sandwich|kebab|pizza)", re.I)
# Tolère les erreurs de transcription (itinéraire entendu tineret)
ITINERAIRE = re.compile(r"itin\w*|tin[eé]r\w*|navigation|\bgps\b|\blien\b", re.I)
LIEUX_MOTS = re.compile(r"\b(de|à|vers|pour)\b", re.I)
# d'ici, de ma position : le départ est la position GPS
FROM_HERE = re.compile(
    r"\b(d'ici|depuis ici|à partir d'ici|de ma position|depuis ma position|"
    r"ma position actuelle|de mon emplacement|d'où je suis|là où je suis|"
    r"de l'endroit où je suis|depuis où je suis)\b", re.I)
# Historique : refaire le dernier trajet, énumérer les derniers trajets
LAST_TRIP = re.compile(
    r"\b(dernier trajet|même trajet|meme trajet|comme tout à l'heure|"
    r"comme la dernière fois|refais le trajet|même endroit|même destination)\b", re.I)
HISTORY_ASK = re.compile(r"\b(historique|derniers trajets|trajets récents)\b", re.I)
QUESTION_CATEGORIE = ("Qu'est-ce que tu cherches ? Un restaurant, un café, "
                      "une station-service ou une pharmacie ?")
REASK = {
    "ask_stop": "Pardon, tu veux t'arrêter quelque part ? Réponds par oui ou par non.",
    "ask_generic": "Pardon, tu veux que je cherche autre chose du même type ? Oui ou non ?",
    "ask_category": QUESTION_CATEGORIE,
    "fix_origin": "Pardon, d'où pars-tu ?",
    "fix_destination": "Pardon, où est-ce que tu vas ?",
}


def warm_up():
    """Charge le modèle en mémoire (à lancer dans un thread au démarrage)."""
    try:
        ollama.generate(model=MODEL, prompt="", keep_alive="30m")
    except Exception as e:
        print(f"[préchauffage : {e}]")


def normaliser(text):
    """Convertit Paris huitième en Paris 8e."""
    def repl(m):
        n = ORDINAUX.get(m.group(2).lower())
        return f"{m.group(1)} {n}e" if n else m.group(0)
    return re.sub(r"\b(Paris)[,\s]+([a-zéèêîô-]+ième)\b", repl, text, flags=re.I)


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


def quick_choice(text, options):
    """Détecte le choix d'une option (1, 2, 3) dans la phrase. None si aucun choix clair."""
    t = text.lower()
    for n, pattern in ORDINALS:
        if re.search(pattern, t):
            return n
    m = re.search(r"\b(?:num[ée]ro|option|le|la)\s+([123])\b", t)
    if m:
        return int(m.group(1))
    if re.search(r"\bderni[eè]re?\b", t):
        return len(options)
    return None


def clean_nom(nom):
    """Nettoie le nom d'enseigne ; None si vide, trop générique ou ressemblant à une phrase."""
    nom = (nom or "").strip()
    nom = re.split(r"['’]", nom)[0].strip()  # McDonald's -> McDonald
    if len(nom) < 3 or nom.lower() in GENERIC or nom.lower() == "aucune":
        return None
    if re.search(r"\b(et|ou|qui|avec|vend|vendent)\b", nom, re.I):
        return None  # c'est une description, pas une enseigne
    return nom


def detect_category(text):
    """Devine la catégorie de lieu à partir de mots-clés (None si rien de clair)."""
    t = text.lower()
    for cat, pattern in CATEGORY_KEYWORDS:
        if re.search(pattern, t):
            return cat
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


class Brain:
    """Cerveau du copilote : texte entrant -> phrase à dire, plus liens d'itinéraire éventuels.

    notify(texte) : appelé pour une phrase d'attente pendant une recherche longue.
    position : (lat, lon) du téléphone, fixée par le serveur avant chaque tour (ou None).
    """

    def __init__(self, notify=None):
        self.trip = {"origin": "", "destination": "", "origin_gps": False}
        self.etat = {"pending": None, "reasks": 0}
        self.notify = notify or (lambda text: None)
        self.position = None
        self._last_pos = None
        self._links = None

    def handle(self, text):
        """Retourne un dictionnaire avec les clés reply, links et end."""
        self._links = None
        if self.position:
            self._last_pos = self.position
        if any(w in text.lower() for w in EXIT_WORDS):
            return {"reply": "À bientôt !", "links": None, "end": True}
        reply = self._traiter(text)
        return {"reply": reply, "links": self._links, "end": False}

    # ----- départ : texte ou position GPS -----
    def _pos(self):
        return self.position or self._last_pos

    def _origin_value(self):
        """Départ utilisable par les outils : lat,lon si on part de la position GPS."""
        if self.trip["origin_gps"]:
            p = self._pos()
            return f"{p[0]:.5f},{p[1]:.5f}" if p else ""
        return self.trip["origin"]

    def _origin_label(self):
        return "ta position" if self.trip["origin_gps"] else self.trip["origin"]

    def _check_trip(self):
        """Retourne une phrase si le trajet est incomplet, sinon None."""
        trip, etat = self.trip, self.etat
        if trip["origin_gps"] and not self._pos():
            trip["origin_gps"] = False
            etat["pending"] = "fix_origin"
            return "Je n'ai pas encore ta position. D'où pars-tu ?"
        if not trip["destination"]:
            if self._origin_value():
                return "D'accord. Mais d'abord, où est-ce que tu vas ?"
            return "D'accord. Mais d'abord, d'où pars-tu et où vas-tu ?"
        if not self._origin_value():
            return "D'accord. Mais d'abord, d'où pars-tu ?"
        return None

    def _direct_links(self):
        """Liens et carte du trajet direct (sans arrêt)."""
        res = trip_links(self._origin_value(), self.trip["destination"])
        m = res.get("links", {}).get("map") if "links" in res else None
        if m and self.trip["origin_gps"]:
            for p in m["points"]:
                if p["role"] == "start":
                    p["name"] = "Ma position"
        return res

    # ----- historique -----
    def _parler_historique(self):
        items = historique.recent(3)
        if not items:
            return "Je n'ai pas encore de trajet enregistré."
        noms = [i["destination"] for i in items]
        liste = noms[0] if len(noms) == 1 else ", ".join(noms[:-1]) + " et " + noms[-1]
        return f"Tes derniers trajets : {liste}. Dis par exemple : emmène-moi à {noms[0]}."

    def _rejouer_dernier(self):
        items = historique.recent(1)
        if not items:
            return "Je n'ai pas encore de trajet enregistré."
        trip = self.trip
        trip["destination"] = items[0]["destination"]
        trip["origin"], trip["origin_gps"] = "", bool(self._pos())
        STATE.clear()
        return self._annoncer_trajet()

    # ----- actions -----
    def _annoncer_trajet(self):
        """Confirme le trajet après avoir vérifié que les lieux existent."""
        trip, etat = self.trip, self.etat
        if trip["origin_gps"] and not self._pos():
            trip["origin_gps"] = False
            etat["pending"] = "fix_origin"
            return "Je n'ai pas encore ta position. D'où pars-tu ?"
        if not trip["destination"]:
            return f"D'accord, tu pars de {self._origin_label()}. Où est-ce que tu vas ?"
        if not self._origin_value():
            return f"D'accord, direction {trip['destination']}. Tu pars d'où ?"

        # On vérifie que les lieux cités existent (la position GPS, elle, est déjà fiable)
        checks = [("destination", "fix_destination")]
        if not trip["origin_gps"]:
            checks.insert(0, ("origin", "fix_origin"))
        for cle, pend in checks:
            nom = corrige_lieu(trip[cle]) or trip[cle]   # Choisie -> Choisy
            trip[cle] = nom
            try:
                trouve = geocode(nom)
            except Exception:
                trouve = True  # service indisponible : on ne bloque pas
            if not trouve:
                print(f"[géocodage : introuvable {nom!r}]")
                trip[cle] = ""
                etat["pending"] = pend
                return f"Je ne trouve pas {nom}. Redis-moi juste ce lieu, ou une ville proche."

        try:  # aperçu du trajet sur la carte, dès l'annonce
            res = self._direct_links()
            if "links" in res:
                self._links = {"titre": f"De {self._origin_label()} à {trip['destination']}",
                               **res["links"]}
        except Exception:
            pass
        etat["pending"] = "ask_stop"
        return (f"D'accord, de {self._origin_label()} à {trip['destination']}. "
                "Dis-moi si tu veux t'arrêter quelque part.")

    def _itineraire_direct(self):
        trip = self.trip
        try:
            res = self._direct_links()
        except Exception as e:
            print(f"[itinéraire : erreur {e}]")
            return "Je n'arrive pas à préparer l'itinéraire, réessaie dans un instant."
        if "links" not in res:
            return res.get("error", "Je n'ai pas trouvé ce trajet.")
        self._links = {"titre": f"De {self._origin_label()} à {trip['destination']}",
                       **res["links"]}
        historique.add(trip["destination"], self._origin_label())
        self._links["final"] = True
        STATE.clear()
        return "Ton itinéraire est prêt. Bonne route !"

    def _choisir(self, numero):
        res = select_stop(numero)
        if "links" not in res:
            return res["error"]
        self._links = {"titre": f"Via {res['arret']} ({res['adresse']})", **res["links"]}
        historique.add(self.trip["destination"], self._origin_label(), res["arret"])
        self._links["final"] = True
        STATE.clear()
        return f"C'est noté, on passe par {res['arret']}. Ton itinéraire est prêt."

    def _rechercher(self, category, nom=None):
        self.notify("Je cherche, un instant.")
        t1 = time.time()
        try:
            res = find_stops(self._origin_value(), self.trip["destination"],
                             category, name_like=nom)
        except Exception as e:
            print(f"[recherche : erreur {e}]")
            return "Je n'arrive pas à joindre le service de cartes, réessaie dans un instant."
        print(f"[recherche : {time.time() - t1:.1f} s]")

        if res.get("code") == "none":
            if category:  # on propose de chercher sans l'enseigne
                self.etat["pending"] = "ask_generic"
                self.etat["last_category"] = category
                return (f"Je n'ai pas trouvé de {nom} sur ton trajet. "
                        "Tu veux que je cherche autre chose du même type ?")
            return f"Je n'ai pas trouvé de {nom} sur ton trajet."
        if "error" in res:
            print(f"[recherche : {res['error']}]")
            return "Je n'ai rien trouvé pour l'instant, on réessaie ?"
        return annonce(res)

    # ----- dialogue -----
    def _traiter(self, heard):
        trip, etat = self.trip, self.etat
        heard = normaliser(heard)
        t = heard.lower()
        pending = etat.get("pending")
        etat["pending"] = None
        words = t.split()
        if not pending:
            etat["reasks"] = 0

        # 0a) Historique : lister les derniers trajets, refaire le dernier
        if HISTORY_ASK.search(t) and len(words) <= 8:
            return self._parler_historique()
        if LAST_TRIP.search(t) and len(words) <= 10:
            return self._rejouer_dernier()

        # 0b) On attendait un nom de lieu (celui qu'on n'a pas trouvé) : réponse courte = ce lieu
        if pending in ("fix_origin", "fix_destination"):
            # « Je voulais dire, Villejuif » -> Villejuif
            court = re.sub(
                r"^\W*(?:(?:je voulais dire|je veux dire|je disais|en fait|plutôt|plutot|c'est|euh|ah|oui)\b[\s,:]*)+",
                "", heard.strip(), flags=re.I).strip(" .!?")
            if (court and len(court.split()) <= 5
                    and not re.search(r"\b(je|j'|veux|vais|aller|non)\b", court, re.I)):
                if pending == "fix_origin" and FROM_HERE.search(t):
                    trip["origin"], trip["origin_gps"] = "", True
                else:
                    lieu = re.sub(r"^(?:de|d'|du|des|à|au|aux|vers|pour|depuis|en)\s*", "",
                                  court, flags=re.I)
                    if pending == "fix_origin":
                        trip["origin"], trip["origin_gps"] = lieu, False
                    else:
                        trip["destination"] = lieu
                return self._annoncer_trajet()

        # 1) Réponses courtes à la question qu'on vient de poser
        if pending:
            non = bool(NON.search(t) or NON_START.match(t)) and not PLACE.search(t)
            if non:
                if self._check_trip() is None:
                    return self._itineraire_direct()
                return "Très bien, bonne route !"
            if pending == "ask_generic" and OUI.search(t):
                return self._rechercher(etat.get("last_category"))
            if pending == "ask_stop" and OUI.search(t) and not PLACE.search(t):
                etat["pending"] = "ask_category"
                return QUESTION_CATEGORIE
            # Réponse courte qu'on n'a pas comprise : on repose la question (2 fois max)
            if (len(words) <= 4 and etat["reasks"] < 2
                    and not (PLACE.search(t) or ITINERAIRE.search(t) or LIEUX_MOTS.search(t))):
                etat["reasks"] += 1
                etat["pending"] = pending
                return REASK.get(pending, "Pardon, tu peux répéter ?")
        else:
            if len(words) <= 3 and (NON.search(t) or NON_START.match(t)) and not PLACE.search(t):
                return "D'accord."
            if (len(words) <= 5 and ACK.search(t) and not PLACE.search(t)
                    and not STATE.get("options")):
                return "Avec plaisir !"

        # 2) Choix d'une option déjà proposée : détecté par le code, jamais par le modèle
        options = STATE.get("options")
        if options:
            numero = quick_choice(heard, options)
            if numero:
                return self._choisir(numero)

        # 3) Demande d'itinéraire direct (donne-moi l'itinéraire)
        if (ITINERAIRE.search(t) and not PLACE.search(t) and not LIEUX_MOTS.search(t)
                and len(words) <= 8):
            return self._check_trip() or self._itineraire_direct()

        # 4) Sinon, le modèle analyse la phrase
        out = comprendre(heard)
        print(f"[modèle : {out}]")
        action = out.get("action", "autre")

        # Demande d'arrêt courte que le modèle n'a pas reconnue (par exemple : Un restaurant.)
        cat_kw = detect_category(t)
        if action == "autre" and cat_kw and trip["destination"] and len(words) <= 8:
            action = "chercher"

        origin = (out.get("origin") or "").strip()
        dest = (out.get("destination") or "").strip()
        from_here = bool(FROM_HERE.search(t))
        if from_here:
            origin = ""
        changed = bool((origin and (origin != trip["origin"] or trip["origin_gps"]))
                       or (dest and dest != trip["destination"])
                       or (from_here and not trip["origin_gps"]))

        if action in ("trajet", "chercher"):
            if from_here:
                trip["origin"], trip["origin_gps"] = "", True
            elif origin:
                trip["origin"], trip["origin_gps"] = origin, False
            if dest:
                trip["destination"] = dest
            # Aucun départ cité et position connue : on part de là où on est
            if not trip["origin"] and not trip["origin_gps"] and self._pos():
                trip["origin_gps"] = True

        if action == "trajet":
            if not (origin or dest or from_here):
                return out.get("reponse") or "D'accord."
            if not changed:
                return "C'est noté, rien ne change."
            STATE.clear()  # d'anciennes options ne correspondent plus au nouveau trajet
            return self._annoncer_trajet()

        if action == "chercher":
            category = out.get("category")
            if category not in CATEGORIES:
                category = cat_kw  # le modèle n'a pas trouvé : on déduit par mots-clés
            nom = clean_nom(out.get("nom"))
            if not category and not nom:
                if pending == "ask_category":
                    etat["reasks"] += 1
                    if etat["reasks"] >= 2:
                        return "D'accord, on laisse tomber l'arrêt. Dis-moi si tu changes d'avis."
                etat["pending"] = "ask_category"
                return QUESTION_CATEGORIE
            msg = self._check_trip()
            if msg:
                return msg
            return self._rechercher(category, nom)

        return out.get("reponse") or "Je n'ai pas bien compris, tu peux répéter ?"