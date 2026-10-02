import collections
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import wave

import numpy as np
import webrtcvad
from faster_whisper import WhisperModel

SR = 16000
FRAME_MS = 30
FRAME_BYTES = SR * FRAME_MS // 1000 * 2  # 960 octets (s16le, mono)
MIC = "RDPSource"
PIPER_MODEL = "voices/fr_FR-siwis-medium.onnx"
VOCAB = "Copilote de voiture. Itinéraire, trajet, arrêt, fast-food, restaurant, station-service, pharmacie, café, Cergy, Paris, Vitry-sur-Seine."

print("Chargement de Whisper...")
_stt = WhisperModel("small", device="cpu", compute_type="int8")
_vad = webrtcvad.Vad(3)  # 0 = permissif, 3 = très strict


def listen(max_wait=12, max_speech=15, end_silence=0.9):
    """Écoute jusqu'à la fin de la phrase. Retourne le texte ('' si rien entendu)."""
    proc = subprocess.Popen(
        ["parec", f"--device={MIC}", f"--rate={SR}", "--channels=1", "--format=s16le"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    preroll = collections.deque(maxlen=10)  # 300 ms avant le début de la parole
    frames, started, silence = [], False, 0.0
    t_start = time.time()
    speech_start = 0.0
    try:
        while True:
            chunk = proc.stdout.read(FRAME_BYTES)
            if len(chunk) < FRAME_BYTES:
                break
            is_speech = _vad.is_speech(chunk, SR)
            now = time.time()
            if not started:
                if is_speech:
                    started, speech_start = True, now
                    frames = list(preroll) + [chunk]
                else:
                    preroll.append(chunk)
                    if now - t_start > max_wait:
                        break
            else:
                frames.append(chunk)
                silence = 0.0 if is_speech else silence + FRAME_MS / 1000
                if silence >= end_silence or now - speech_start > max_speech:
                    break
    finally:
        proc.terminate()
        proc.wait()

    if not started:
        return ""

    audio = np.frombuffer(b"".join(frames), dtype=np.int16).astype(np.float32) / 32768.0
    t0 = time.time()
    segments, _ = _stt.transcribe(audio, language="fr", beam_size=1,
                                  vad_filter=True, initial_prompt=VOCAB)
    text = " ".join(s.text.strip() for s in segments).strip()
    print(f"[transcription : {time.time() - t0:.1f} s]")
    return text


# ---------- Synthèse vocale ----------
try:
    from piper import PiperVoice
    _voice = PiperVoice.load(PIPER_MODEL)
except Exception as e:
    print(f"[piper python indisponible, repli sur la ligne de commande : {e}]")
    _voice = None


def _synth(text, path):
    """Synthétise `text` dans le fichier WAV `path`."""
    if _voice is not None:
        try:
            with wave.open(path, "wb") as wf:
                if hasattr(_voice, "synthesize_wav"):
                    _voice.synthesize_wav(text, wf)
                else:
                    _voice.synthesize(text, wf)
            return
        except Exception as e:
            print(f"[piper python : {e} ; repli sur la ligne de commande]")
    subprocess.run(["piper", "-m", PIPER_MODEL, "-f", path],
                   input=text.encode("utf-8"), check=True, capture_output=True)


def _clean(text):
    return re.sub(r"[*#`_]", "", text).strip()


def _split(text):
    parts = re.split(r"(?<=[^\W\d_)][.!?])\s+|\n+", _clean(text))
    return [p for p in parts if p.strip()]


class Speaker:
    """Synthèse et lecture en parallèle : la phrase suivante se prépare pendant que la précédente joue."""

    def __init__(self):
        self._text_q, self._play_q = queue.Queue(), queue.Queue()
        threading.Thread(target=self._synth_worker, daemon=True).start()
        threading.Thread(target=self._play_worker, daemon=True).start()

    def say(self, text):
        text = _clean(text)
        if not text:
            return
        if len(text) <= 160:  # réponse courte : un seul bloc, sans pause au milieu
            self._text_q.put(text)
        else:
            for sentence in _split(text):
                self._text_q.put(sentence)

    def wait(self):
        self._text_q.join()
        self._play_q.join()

    def _synth_worker(self):
        while True:
            text = self._text_q.get()
            path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    path = f.name
                _synth(text, path)
                self._play_q.put(path)
            except Exception as e:
                print(f"[voix : {e}]")
                if path and os.path.exists(path):
                    os.remove(path)
            finally:
                self._text_q.task_done()

    def _play_worker(self):
        while True:
            path = self._play_q.get()
            try:
                subprocess.run(["paplay", path], check=True)
            except Exception as e:
                print(f"[lecture : {e}]")
            finally:
                if os.path.exists(path):
                    os.remove(path)
                self._play_q.task_done()