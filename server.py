import asyncio
import base64
import hashlib
import hmac
import json
import os
import queue
import subprocess
import tempfile
import threading
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
import historique

# Le code d'accès est vérifié en premier, avant de charger les modèles
TOKEN = os.environ.get("COPILOTE_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit(
        "COPILOTE_TOKEN n'est pas défini. Lance d'abord :\n"
        '  export COPILOTE_TOKEN="ton-code"\n'
        "puis relance uvicorn.")

import numpy as np
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from brain import Brain, warm_up
from voice import VOCAB, _clean, _stt, _synth

BASE = Path(__file__).parent
APP_PAGE = BASE / "private" / "index.html"
LOGIN_PAGE = BASE / "static" / "login.html"
if not APP_PAGE.exists() or not LOGIN_PAGE.exists():
    raise SystemExit("Fichiers manquants : private/index.html et static/login.html "
                     "(déplace static/index.html dans le dossier private).")

# ---------- Accès par code ----------
COOKIE = "copilote_auth"
# Valeur du cookie dérivée du code : elle survit au redémarrage du serveur,
# et change dès que tu changes le code (toutes les sessions sont alors déconnectées).
SESSION_VALUE = hmac.new(TOKEN.encode(), b"copilote-session-v1", hashlib.sha256).hexdigest()
MAX_FAILS, WINDOW_S = 5, 300
_fails = {}  # adresse -> horodatages des échecs récents


def is_authed(request: Request) -> bool:
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), SESSION_VALUE)


def check(request: Request):
    if not is_authed(request):
        raise HTTPException(status_code=401, detail="Accès refusé")


def client_ip(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


def too_many_fails(ip: str) -> bool:
    now = time.time()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < WINDOW_S]
    return len(_fails[ip]) >= MAX_FAILS


@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=warm_up, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)
# Public : uniquement le manifeste et l'écran du code (l'appli est dans private/)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

brain = Brain()
turn_lock = threading.Lock()  # un seul tour de dialogue à la fois (un seul utilisateur)


# ---------- Audio ----------
def decode_audio(data: bytes, content_type: str):
    """Convertit l'audio de l'iPhone (mp4/aac ou webm) en 16 kHz mono float32."""
    ext = ".mp4" if "mp4" in content_type else ".webm" if "webm" in content_type else ".audio"
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
        f.write(data)
        path = f.name
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
            capture_output=True, check=True)
    finally:
        os.remove(path)
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(audio):
    if len(audio) < 16000 * 0.4:  # moins de 0,4 s : rien à transcrire
        return ""
    segments, _ = _stt.transcribe(audio, language="fr", beam_size=1,
                                  vad_filter=True, initial_prompt=VOCAB)
    return " ".join(s.text.strip() for s in segments).strip()


def synth_b64(text):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = f.name
    try:
        _synth(_clean(text), path)
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    finally:
        os.remove(path)


# ---------- Un tour de dialogue, renvoyé en flux (une ligne JSON par événement) ----------
def run_turn(get_text, lat, lon):
    q = queue.Queue()

    def filler(text):
        try:
            q.put({"type": "filler", "text": text, "audio": synth_b64(text)})
        except Exception:
            traceback.print_exc()

    def worker():
        global brain
        with turn_lock:
            try:
                text = get_text()
                if not text:
                    q.put({"type": "empty"})
                    return
                print(f"Toi : {text}")
                q.put({"type": "heard", "text": text})
                brain.position = (lat, lon) if lat is not None and lon is not None else None
                brain.notify = filler
                out = brain.handle(text)
                print(f"Copilote : {out['reply']}")
                if out["end"]:
                    brain = Brain()  # prochaine conversation : on repart de zéro
                q.put({"type": "reply", "text": out["reply"], "audio": synth_b64(out["reply"]),
                       "links": out["links"], "end": out["end"]})
            except Exception as e:
                traceback.print_exc()
                q.put({"type": "error", "text": str(e) or type(e).__name__})
            finally:
                q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def gen():
        while True:
            item = q.get()
            if item is None:
                break
            yield json.dumps(item, ensure_ascii=False) + "\n"

    return StreamingResponse(
        gen(), media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- Routes ----------
@app.get("/")
def index(request: Request):
    """L'appli seulement avec le bon cookie, sinon l'écran du code."""
    page = APP_PAGE if is_authed(request) else LOGIN_PAGE
    return FileResponse(page, headers={"Cache-Control": "no-store"})


@app.post("/api/login")
async def login(request: Request, code: str = Form(...)):
    ip = client_ip(request)
    if too_many_fails(ip):
        raise HTTPException(status_code=429, detail="Trop d'essais")
    if not hmac.compare_digest(code.strip().encode(), TOKEN.encode()):
        _fails.setdefault(ip, []).append(time.time())
        await asyncio.sleep(1)  # ralentit les essais
        raise HTTPException(status_code=401, detail="Code incorrect")
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, SESSION_VALUE, max_age=60 * 60 * 24 * 30,
                    httponly=True, samesite="lax", secure=secure, path="/")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.post("/api/turn", dependencies=[Depends(check)])
async def turn(audio: UploadFile = File(...),
               lat: Optional[float] = Form(None), lon: Optional[float] = Form(None)):
    data = await audio.read()
    ctype = audio.content_type or ""
    return run_turn(lambda: transcribe(decode_audio(data, ctype)), lat, lon)


@app.post("/api/text", dependencies=[Depends(check)])
async def text_turn(text: str = Form(...),
                    lat: Optional[float] = Form(None), lon: Optional[float] = Form(None)):
    return run_turn(lambda: text.strip(), lat, lon)


@app.post("/api/reset", dependencies=[Depends(check)])
def reset():
    global brain
    with turn_lock:
        brain = Brain()
    return {"ok": True}


@app.get("/api/history", dependencies=[Depends(check)])
def get_history():
    return {"items": historique.recent(8)}


@app.post("/api/history/clear", dependencies=[Depends(check)])
def clear_history():
    historique.clear()
    return {"ok": True}