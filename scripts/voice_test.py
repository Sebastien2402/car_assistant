import subprocess
import time
import wave
import tempfile
import os

import numpy as np
import soundfile as sf
from faster_whisper import WhisperModel

SR = 16000
PIPER_MODEL = "voices/fr_FR-siwis-medium.onnx"

print("Chargement de Whisper...")
stt = WhisperModel("small", device="cpu", compute_type="int8")


def listen(seconds=5):
    print(f"Parle maintenant ({seconds} s)...")

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name

    try:
        process = subprocess.Popen(
            [
                "parecord",
                "--device=RDPSource",
                "--rate=16000",
                "--channels=1",
                "--format=s16le",
                wav_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Enregistrement pendant X secondes
        time.sleep(seconds)

        # Arrêt propre de parecord
        process.terminate()
        process.wait()

        # Lecture du fichier audio
        rec, rate = sf.read(wav_path, dtype="float32")

        t0 = time.time()

        segments, _ = stt.transcribe(
            rec,
            language="fr",
            vad_filter=True
        )

        text = " ".join(s.text.strip() for s in segments)

        print(f"[transcription : {time.time() - t0:.1f} s]")

        return text

    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)


def speak(text):
    # Piper génère le fichier audio
    subprocess.run(
        ["piper", "-m", PIPER_MODEL, "-f", "out.wav"],
        input=text.encode("utf-8"),
        check=True,
        capture_output=True
    )

    # Lecture via PulseAudio / WSLg
    subprocess.run(
        ["paplay", "out.wav"],
        check=True
    )


if __name__ == "__main__":
    text = listen()

    print("Tu as dit :", text)

    speak(text or "Je n'ai rien entendu.")