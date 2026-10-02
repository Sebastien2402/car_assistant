# Copilote vocal

Assistant vocal pour la conduite : on parle, il comprend la destination, propose
des arrêts sur le trajet (restaurants, cafés, stations, pharmacies) en calculant
le détour, et affiche l'itinéraire sur une carte. Prototype personnel, 100 % open source.

## Fonctionnalités
- Dialogue vocal en français (faster-whisper pour l'écoute, Piper pour la voix)
- Compréhension des demandes par un petit modèle local (Ollama, qwen2.5:3b)
- Recherche d'arrêts dans une base locale OpenStreetMap, avec calcul du détour (OSRM)
- Carte dans le navigateur (Leaflet), départ depuis la position GPS du téléphone
- Historique des trajets, code d'accès, liens vers Plans et Google Maps

## Prérequis
Linux ou WSL, Python 3.10+, `ffmpeg`, `osmium-tool`, [Ollama](https://ollama.com)
avec le modèle `qwen2.5:3b`, et une voix Piper française (dossier `voices/`).

## Installation
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
sudo apt install -y ffmpeg osmium-tool
ollama pull qwen2.5:3b

# Voix française
mkdir -p voices && cd voices
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/fr/fr_FR/siwis/medium/fr_FR-siwis-medium.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/fr/fr_FR/siwis/medium/fr_FR-siwis-medium.onnx.json
cd ..

# Base de lieux (Île-de-France, à adapter)
mkdir -p data
wget -P data https://download.geofabrik.de/europe/france/ile-de-france-latest.osm.pbf
python build_poi_db.py
```

## Lancer
```bash
ollama serve                                   # terminal 1
export COPILOTE_TOKEN="un-code-long-et-secret"
uvicorn server:app --host 127.0.0.1 --port 8000   # terminal 2
```
Pour l'utiliser depuis un téléphone (le micro exige du HTTPS), ouvrir un tunnel,
par exemple `cloudflared tunnel --url http://localhost:8000`, puis ouvrir l'adresse
obtenue et saisir le code d'accès.

## Sécurité et vie privée
- Le serveur refuse de démarrer sans `COPILOTE_TOKEN`. Ne jamais publier ce code.
- Un tunnel rend l'adresse publique : le code d'accès protège l'appli, mais ferme le tunnel quand tu ne t'en sers pas.
- L'historique des trajets est stocké localement dans `data/` (jamais versionné).
- Les calculs d'itinéraire utilisent le serveur public d'OSRM, les adresses Nominatim et les tuiles OpenStreetMap : ces services reçoivent les coordonnées nécessaires. Les tuiles OSM ne conviennent qu'à un usage léger.

## Contributors
- Sebastien Nguyen
- Jennifer Chen