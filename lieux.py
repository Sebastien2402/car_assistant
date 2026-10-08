"""Correction des noms de lieux mal transcrits par Whisper.
Exemple : « Porte de Choisie » -> « Porte de Choisy ».

Pour ajouter tes lieux habituels, complète la liste LIEUX_PERSO."""
import re
import unicodedata
from difflib import get_close_matches

PORTES = [
    "Porte de Choisy", "Porte d'Italie", "Porte de Vitry", "Porte d'Ivry", "Porte de Bagnolet",
    "Porte de Montreuil", "Porte de Vincennes", "Porte de la Chapelle", "Porte de Clignancourt",
    "Porte de Saint-Ouen", "Porte de Clichy", "Porte de Champerret", "Porte Maillot",
    "Porte Dauphine", "Porte d'Auteuil", "Porte de Saint-Cloud", "Porte de Sèvres",
    "Porte de Versailles", "Porte de Vanves", "Porte de Châtillon", "Porte d'Orléans",
    "Porte de Gentilly", "Porte de Pantin", "Porte des Lilas", "Porte de la Villette",
    "Porte d'Aubervilliers", "Porte de Montmartre", "Porte de Charenton", "Porte Dorée",
    "Porte de Bercy", "Porte de Gennevilliers", "Porte d'Asnières", "Porte de la Muette",
]

COMMUNES = [
    "Cergy", "Pontoise", "Cergy-Pontoise", "Vitry-sur-Seine", "Ivry-sur-Seine", "Choisy-le-Roi",
    "Créteil", "Villejuif", "Arcueil", "Gentilly", "Le Kremlin-Bicêtre", "Orly", "Rungis", "Thiais",
    "Boulogne-Billancourt", "Issy-les-Moulineaux", "Nanterre", "La Défense", "Courbevoie",
    "Puteaux", "Neuilly-sur-Seine", "Levallois-Perret", "Clichy", "Saint-Denis", "Saint-Ouen",
    "Aubervilliers", "Montreuil", "Vincennes", "Saint-Mandé", "Nogent-sur-Marne", "Argenteuil",
    "Versailles", "Saint-Germain-en-Laye", "Poissy", "Conflans-Sainte-Honorine", "Roissy-en-France",
    "Massy", "Évry", "Marne-la-Vallée", "Disneyland Paris",
]

REPERES = [
    "Place de la Concorde", "Gare du Nord", "Gare de Lyon", "Gare de l'Est", "Gare Montparnasse",
    "Gare Saint-Lazare", "Gare d'Austerlitz", "Aéroport Charles de Gaulle", "Aéroport d'Orly",
    "Tour Eiffel", "Arc de Triomphe",
]

LIEUX_PERSO = []   # ajoute ici tes lieux habituels, ex. "Rue de Rivoli"

# Mots mal transcrits (ou lieux sans ville) -> adresse à chercher. Complète-les au fil des erreurs.
ALIAS = {
    "Beethoven Concorde": "Beethoven Concorde Vitry-sur-Seine",
    "Pétovon Concorde": "Beethoven Concorde Vitry-sur-Seine",
    "Très-Melin-Bissettre": "Le Kremlin-Bicêtre",
    "Kremlin Bissêtre": "Le Kremlin-Bicêtre",
    "Ville Juif": "Villejuif",
}

LIEUX = PORTES + COMMUNES + REPERES + LIEUX_PERSO

# Courte liste donnée à Whisper pour qu'il privilégie ces orthographes (limite : ~200 mots)
PROMPT_WHISPER = ("Trajet en voiture en Île-de-France : Porte de Choisy, Porte d'Italie, Porte d'Orléans, "
                  "Porte de Bagnolet, Porte Maillot, Porte de la Chapelle, Vitry-sur-Seine, "
                  "Ivry-sur-Seine, Cergy-Pontoise, La Défense, Boulogne-Billancourt, Issy-les-Moulineaux, "
                  "Beethoven Concorde à Vitry-sur-Seine, Le Kremlin-Bicêtre, Villejuif.")


def _norm(s):
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")   # retire les accents
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


_INDEX = {_norm(lieu): lieu for lieu in LIEUX}
_ALIAS = {_norm(k): v for k, v in ALIAS.items()}


def _cherche(n, seuil):
    if n in _ALIAS:
        return _ALIAS[n]
    if n in _INDEX:
        return _INDEX[n]
    proche = get_close_matches(n, list(_INDEX), n=1, cutoff=seuil)
    return _INDEX[proche[0]] if proche else None


def corrige_lieu(texte, seuil=0.85):
    """Retourne le lieu connu le plus proche de `texte`, ou None s'il n'y a aucun candidat sûr.
    Essaie le texte entier, puis la partie avant la première virgule (« Porte de Choisie, Paris »)."""
    brut = texte or ""
    for morceau in (brut, brut.split(",")[0]):
        n = _norm(morceau)
        if n:
            r = _cherche(n, seuil)
            if r:
                return r
    return None