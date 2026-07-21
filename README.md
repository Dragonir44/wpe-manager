# Wallpaper Engine Manager

Une GUI native (PySide6/Qt) au-dessus de
[`linux-wallpaperengine`](https://github.com/Almamu/linux-wallpaperengine).
Elle fait ce que les frontends existants faisaient mal : gérer correctement une
bibliothèque Steam sur un autre disque, transmettre le bon `--assets-dir`, et
proposer des **playlists tournantes par écran** — le tout en restant fluide.

L'interface reprend les **codes visuels de Wallpaper Engine** (thème sombre,
sidebar de filtres à gauche, panneau de propriétés à droite, bande playlist en
bas, sélecteur d'écran visuel) pour une prise en main immédiate.

Testé sur KDE Plasma 6 / Wayland (CachyOS), mais ne dépend d'aucune API KDE.

## Fonctionnalités

- **Détection auto** de la bibliothèque (`workshop/content/431960`) même sur un
  autre disque — lit `libraryfolders.vdf` et résout les symlinks pour retrouver
  le dossier des assets.
- **Grille de vignettes**, chargées en tâche de fond (fluide même à 500+ fonds).
- **Recherche** par titre.
- **Filtres combinés** : genre, type, évaluation d'âge (tout en local depuis
  `project.json`), **résolution**, et un filtre **« Compatible écran »** qui ne
  montre que les fonds au bon ratio pour l'écran sélectionné (idéal multi-écran /
  ultrawide). Tri par titre ou par taille.
- **Résolution via Steam** : la résolution est un tag Workshop absent des fichiers
  locaux ; l'app la récupère depuis l'API publique Steam (**sans clé**), la met en
  cache (`metadata.json`) — instantané et hors-ligne ensuite. Bouton « Sync Steam »
  et sync auto au premier lancement.
- **Un process par écran** : changer un écran ne redémarre pas les autres.
- **Sélecteur d'écran visuel** (popup *Écrans…*) : les moniteurs sont dessinés
  selon leur disposition réelle ; on clique un écran puis on lui assigne un
  **fond fixe** ou la **playlist courante**. Sélectionner un écran recharge ce
  qu'il affiche.
- **Playlists** créées par **cases à cocher** dans la grille, avec une **bande
  de vignettes** en bas (clic = aperçu, × au survol = retirer). **Intervalle**
  et **ordre** (séquentiel / aléatoire) en ligne ; bouton *Configurer…* pour les
  options avancées par playlist : intervalle en h + min, **démarrage sur le
  premier fond**, et **transition propre à la playlist**. Une playlist aléatoire
  démarre sur un fond au hasard.
- **Panneau de propriétés** (à droite) : personnalise le fond sélectionné
  (couleurs, curseurs, cases, listes). Enregistré par fond et réappliqué à chaque
  lancement via `--set-property` ; si le fond est affiché, il se recharge aussitôt.
- **Import depuis Wallpaper Engine** : récupère les playlists du `config.json`
  de WPE (extrait les IDs, ignore les chemins/moniteurs Windows non portables).
- **Transition sans coupure** : le nouveau fond est affiché *par-dessus* l'ancien
  avant que celui-ci soit tué (pas de fondu alpha — le backend ne le permet pas —
  mais plus de flash du fond de bureau entre deux).
- **Barre système (systray)** : fermer la fenêtre la réduit dans le tray ; la
  **rotation continue** en arrière-plan. Le menu de l'icône permet de changer la
  playlist de chaque écran, de vider un écran, de tout arrêter, et de rouvrir.
- **Démarrage avec la session** : une case (dans *⚙ Réglages* ou le menu tray)
  installe un lanceur autostart qui relance l'app dans le tray et **restaure les
  fonds (rotation comprise)** à l'ouverture de session.
- **Pause automatique** : les fonds étant des rendus GPU continus, ils volent des
  FPS à un jeu — surtout en multi-écran (les autres moniteurs continuent de
  rendre) ou en *fenêtré sans bordure*. Deux déclencheurs, dans *⚙ Réglages* :
  - **liste d'apps** — tant qu'une app de la liste tourne, **tous** les fonds
    sont coupés (GPU libéré), puis relancés à sa fermeture ; ajout en un clic
    depuis les apps en cours. Marche quel que soit le GPU et même en borderless.
  - **charge GPU** (option, filet de secours pour les apps hors liste) — coupe
    les fonds quand l'utilisation GPU reste au-dessus d'un seuil, avec
    hystérésis pour éviter les allers-retours. Lecture via sysfs **amdgpu** ou
    **`nvidia-smi`** (grisé sur les GPU non supportés).
- **⚙ Réglages** : audio (muet), FPS, durée de transition globale, pause
  automatique, chemins de la bibliothèque, autostart, entrée du menu applications.

## Prérequis

- [`linux-wallpaperengine`](https://github.com/Almamu/linux-wallpaperengine)
  installé et dans le `PATH`.
- Wallpaper Engine (l'app Steam, app id `431960`) installé, pour disposer des
  **assets** et des wallpapers du workshop.
- Python ≥ 3.10 et PySide6 (installé automatiquement via pip).

## Installation

### Toute distribution (PyPI)

```bash
pipx install wpe-manager     # recommandé (environnement isolé)
# ou
pip install --user wpe-manager
```

### Arch / CachyOS / dérivés (AUR)

```bash
paru -S wpe-manager          # ou : yay -S wpe-manager
```

Le paquet AUR tire le backend `linux-wallpaperengine-git` automatiquement.

### Depuis les sources

```bash
pipx install .               # ou : pip install --user .
# ou sans installer :
python -m wpe_manager
```

Puis lance la commande :

```bash
wpe-manager
```

## Utilisation

1. **Fond fixe** : clique un fond dans la grille → **🖥 Écrans…**, clique un
   écran → **Fond sélectionné → cet écran**.
2. **Playlist tournante** : coche des fonds dans la grille (ils apparaissent dans
   la bande du bas) → **Nouvelle**, nomme la playlist, règle l'**intervalle** et
   l'**ordre** (ou **Configurer…** pour les options avancées) → **🖥 Écrans…**,
   clique un écran → **Playlist courante → cet écran**.
3. Pour éditer une playlist existante : choisis-la dans le menu déroulant du bas
   (ça la charge), modifie les cases cochées, puis **MàJ items**.
4. **Vider cet écran** / **Tout arrêter** depuis le popup Écrans / ⚙ Réglages.

Si la bibliothèque n'est pas trouvée automatiquement, ouvre **⚙ Réglages →
Chemins de la bibliothèque…** pour la pointer à la main.

> Fermer la fenêtre la **réduit dans la barre système** : la rotation continue.
> Clic sur l'icône pour rouvrir, clic droit → **Quitter** pour fermer réellement.

## Fichiers de config

- `~/.config/wpe-manager/config.json` — chemins, muet, FPS, transition.
- `~/.config/wpe-manager/state.json` — assignations par écran (fond / playlist).
- `~/.config/wpe-manager/playlists.json` — playlists.
- `~/.config/wpe-manager/engine.json` — process backend en cours (par écran).
- `~/.config/wpe-manager/metadata.json` — cache des résolutions (Steam Workshop).
- `~/.config/wpe-manager/properties.json` — propriétés personnalisées par fond.

## Autostart (restaurer les fonds à l'ouverture de session)

Le plus simple : coche **Démarrer avec la session** (barre du haut ou menu du
tray). L'app écrit `~/.config/autostart/wpe-manager.desktop` pour toi.

Sous le capot, l'entrée lance `wpe-manager --daemon` (alias historique :
`--autostart`) : l'app démarre **cachée dans la barre système**, restaure les
fonds sauvegardés et **reprend la rotation**.

## Entrée dans le menu des applications

Coche **Menu applications** (barre du haut ou menu du tray) pour ajouter un
lanceur dans le menu de ton bureau (`~/.local/share/applications/`) et ouvrir
l'app comme n'importe quel programme. Fonctionne aussi bien depuis une
installation pip/pipx que depuis les sources.

## Compatibilité & limites connues

Cette app n'est qu'un frontend ; ce qui fonctionne dépend surtout du backend.

- **Backend requis** : `linux-wallpaperengine` doit être installé et dans le
  `PATH`. Il est packagé sur l'AUR ; sur Debian/Fedora/etc. il faut le
  **compiler depuis les sources**.
- **Bureau** : testé sur **KDE Plasma / Wayland**. Fonctionne sur les
  compositeurs qui supportent `wlr-layer-shell` (KWin, wlroots) et sous X11.
  **GNOME Wayland (Mutter) n'implémente pas layer-shell** → le backend ne peut
  pas y dessiner le fond ; c'est une limite du compositeur, pas de cette app.
- **Wayland** : le fond est dessiné **par-dessus** le bureau → pas d'icônes ni
  de clic droit sur le bureau tant qu'un fond animé est actif (limite du
  backend).

## Remerciements

Ce projet n'est qu'un frontend ; tout le rendu est assuré par
[Almamu/linux-wallpaperengine](https://github.com/Almamu/linux-wallpaperengine).
