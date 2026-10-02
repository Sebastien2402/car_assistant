import threading
import time

from brain import GREETING, Brain, warm_up
from voice import Speaker, listen

speaker = Speaker()


def show_links(links):
    print(f"\n--- {links['titre']} ---")
    print(f"Google Maps : {links['google_maps']}")
    print(f"Apple Plans : {links['apple_plans']}")
    print("-------------------------\n")


def main():
    threading.Thread(target=warm_up, daemon=True).start()
    brain = Brain(notify=speaker.say)
    speaker.say(GREETING)
    speaker.wait()

    while True:
        print("\nJ'écoute...")
        heard = listen()
        if len(heard) < 3:
            continue
        print(f"Toi : {heard}")

        t0 = time.time()
        out = brain.handle(heard)
        if out["links"]:
            show_links(out["links"])
        print(f"Copilote : {out['reply']}   [{time.time() - t0:.1f} s]")
        speaker.say(out["reply"])
        speaker.wait()  # on n'écoute qu'une fois la réponse terminée
        if out["end"]:
            break
        time.sleep(0.4)  # laisse retomber l'écho de la voix avant d'écouter


if __name__ == "__main__":
    main()