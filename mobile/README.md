# Copilote : appli mobile (Android et iPhone)

L'appli est une coquille [Capacitor](https://capacitorjs.com) : au démarrage, elle demande l'adresse
de ton serveur, la garde en mémoire, puis affiche l'interface servie par ton PC (Whisper, Ollama et Piper
restent sur le PC). Conséquence pratique : **une modification de l'interface (`private/index.html`) ne
demande pas de refaire l'appli**. Il suffit de relancer le serveur. Il faut refaire l'appli seulement
quand le dossier `mobile/` change.

```
mobile/
  www/                 écran de démarrage (adresse du serveur) et écran « Connexion perdue »
  capacitor.config.json  nom, identifiant, adresses autorisées (*.*.ts.net, *.trycloudflare.com)
  android/             projet Android (autorisations micro + GPS, clé de signature fixe)
  ios/                 projet iPhone (textes des autorisations micro + GPS)
  assets/              images sources de l'icône et de l'écran de lancement
```

---

## 1. Une adresse fixe et gratuite pour ton PC : Tailscale

Tailscale crée un réseau privé entre ton PC et ton téléphone. Ton PC obtient une adresse HTTPS
du type `https://ton-pc.tailxxxx.ts.net` qui **ne change jamais** et **n'est pas publique** : seuls tes
appareils connectés à ton compte Tailscale peuvent l'ouvrir. L'offre personnelle est gratuite.

### Sur le PC (une seule fois)

1. Installe Tailscale pour Windows depuis https://tailscale.com/download et connecte-toi
   (compte Google, Microsoft ou GitHub).
2. Dans la console Tailscale (https://login.tailscale.com/admin/dns) :
   - vérifie que **MagicDNS** est activé ;
   - active **HTTPS Certificates** (bouton « Enable HTTPS »).
3. Dans PowerShell, avec uvicorn lancé dans WSL comme d'habitude :
   ```powershell
   tailscale serve --bg 8000
   ```
   La commande affiche ton adresse, par exemple `https://ton-pc.tail1234.ts.net`. Ouvre-la une fois
   dans le navigateur du PC : la première ouverture peut prendre quelques secondes, le temps de créer
   le certificat.

   `--bg` garde ce relais actif en arrière-plan, même après un redémarrage du PC.
   Pour l'arrêter : `tailscale serve --https=443 off`.

> **Tailscale directement dans WSL** (à la place de Windows) : `curl -fsSL https://tailscale.com/install.sh | sh`,
> puis `sudo tailscale up` et `sudo tailscale serve --bg 8000`. Il faut que systemd soit actif dans WSL.

### Sur le téléphone

Installe l'appli **Tailscale** (App Store ou Play Store), connecte-toi avec le **même compte**, et
laisse le VPN Tailscale activé quand tu utilises le Copilote.

uvicorn reste lancé sur `127.0.0.1`, comme avant : rien n'est ouvert sur Internet.

---

## 2. Récupérer les applis (fabriquées par GitHub)

Le fichier `.github/workflows/mobile.yml` fait fabriquer les applis par GitHub Actions. C'est gratuit
pour un dépôt public.

- **Android** : fabriqué automatiquement à chaque push qui touche `mobile/`.
- **iPhone** : à la demande. Va dans l'onglet **Actions** du dépôt > **Appli mobile** >
  **Run workflow**, puis laisse la case iPhone cochée.

Quand le run est vert, ouvre-le : les fichiers sont en bas, dans **Artifacts**
(`Copilote-android` et `Copilote-iphone`). GitHub les livre dans un `.zip` qu'il faut décompresser.

---

## 3. Installer sur Android

1. Récupère `Copilote-android.apk` sur le téléphone, depuis la page du run ou en le copiant depuis le PC.
2. Ouvre-le. Android demande d'autoriser l'installation d'applis depuis cette source : accepte.
3. Lance **Copilote**, colle ton adresse Tailscale, puis accepte le micro et la position.

Les versions suivantes s'installent par-dessus, sans désinstaller l'ancienne, grâce à la clé de
signature fixe (`android/app/copilote.keystore`).

---

## 4. Installer sur iPhone, sans Mac : Sideloadly

Sans compte développeur Apple (99 €/an), une appli installée à la main doit être **re-signée tous
les 7 jours**, avec 3 applis maximum par identifiant Apple. Sideloadly le fait depuis Windows avec ton
identifiant Apple gratuit.

1. Sur le PC, installe **iTunes** et **iCloud** depuis le site d'Apple (pas les versions du Microsoft
   Store), puis **Sideloadly** depuis https://sideloadly.io.
2. Branche l'iPhone en USB et accepte « Faire confiance à cet ordinateur ».
3. Dans Sideloadly, glisse `Copilote-iphone.ipa`, entre ton identifiant Apple, puis clique sur **Start**.
4. Sur l'iPhone :
   - va dans Réglages > Général > **VPN et gestion de l'appareil**, puis fais confiance à ton
     identifiant Apple ;
   - iOS 16 et plus : active Réglages > Confidentialité et sécurité > **Mode développeur**
     (l'iPhone redémarre).
5. Lance **Copilote**, colle ton adresse Tailscale, puis accepte le micro et la position.

Au bout de 7 jours, l'appli ne s'ouvre plus : refais l'étape 3, ou active l'actualisation automatique
de Sideloadly (iPhone et PC sur le même Wi-Fi).

> Plus simple sur iPhone : la version web ajoutée à l'écran d'accueil (Safari > Partager > « Sur
> l'écran d'accueil ») n'expire jamais. Elle marche aussi avec l'adresse Tailscale.

---

## 5. Modifier l'appli

```bash
cd mobile
npm install
npx cap sync          # recopie www/ et la configuration dans les projets Android et iPhone
```

- **Nouvelle icône** : remplace les images de `assets/`, puis lance
  `npx -p @capacitor/assets@3.0.5 capacitor-assets generate --iconBackgroundColor '#16181c' --splashBackgroundColor '#16181c'`.
- **Autre domaine** (par exemple un nom de domaine à toi) : ajoute-le à `server.allowNavigation` dans
  `capacitor.config.json`, et dans le test d'adresse de `www/index.html`.
- **Nouvelle version Android** : augmente `versionCode` et `versionName` dans `android/app/build.gradle`.
