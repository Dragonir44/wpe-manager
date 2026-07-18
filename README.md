# Wallpaper Engine Manager

Une petite GUI native (PySide6/Qt) au-dessus de
[`linux-wallpaperengine`](https://github.com/Almamu/linux-wallpaperengine).
Elle fait ce que les frontends existants faisaient mal : gérer correctement une
bibliothèque Steam sur un autre disque, transmettre le bon `--assets-dir`, et
proposer des **playlists tournantes par écran** — le tout en restant fluide.

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
- **Assignation par écran** : un **fond fixe** ou une **playlist tournante**.
- **Playlists** créées par **cases à cocher** dans la grille, avec **intervalle**
  et **ordre** (séquentiel / aléatoire).
- **Import depuis Wallpaper Engine** : récupère les playlists du `config.json`
  de WPE (extrait les IDs, ignore les chemins/moniteurs Windows non portables).
- **Propriétés par fond** : bouton *Propriétés…* pour personnaliser un fond
  (couleurs, curseurs, cases à cocher, listes de choix). Les réglages sont
  enregistrés par fond et réappliqués à chaque lancement via `--set-property` ;
  si le fond est affiché, il se recharge aussitôt.
- **Transition sans coupure** : le nouveau fond est affiché *par-dessus* l'ancien
  avant que celui-ci soit tué (pas de fondu alpha — le backend ne le permet pas —
  mais plus de flash du fond de bureau entre deux).
- **Barre système (systray)** : fermer la fenêtre la réduit dans le tray ; la
  **rotation continue** en arrière-plan. Le menu de l'icône permet de changer la
  playlist de chaque écran, de vider un écran, de tout arrêter, et de rouvrir.
- **Démarrage avec la session** : une case (ou l'entrée du menu tray) installe
  un lanceur autostart qui relance l'app dans le tray et **restaure les fonds
  (rotation comprise)** à l'ouverture de session.
- **Options** : muet, FPS, durée de transition (ms).

## Prérequis

- [`linux-wallpaperengine`](https://github.com/Almamu/linux-wallpaperengine)
  installé et dans le `PATH`.
- Wallpaper Engine (l'app Steam, app id `431960`) installé, pour disposer des
  **assets** et des wallpapers du workshop.
- Python ≥ 3.10 et PySide6 (installé automatiquement via pip).

## Installation

```bash
# depuis une copie du dépôt
pipx install .        # recommandé (environnement isolé)
# ou
pip install --user .
```

Puis lance la commande :

```bash
wpe-manager
```

Sans installer, directement depuis les sources :

```bash
python -m wpe_manager
```

## Utilisation

1. Choisis un **écran** dans le menu en haut.
2. Clique un fond (ou double-clic) → **Fond sélectionné → écran** pour un fond fixe.
3. Pour une rotation : coche des fonds → **Nouvelle (depuis cochés)**, nomme la
   playlist, règle **intervalle** + **ordre**, puis **Playlist → écran**.
4. **Vider l'écran** / **Tout arrêter** au besoin.

Si la bibliothèque n'est pas trouvée automatiquement, bouton **Chemins…** pour la
pointer à la main.

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

## Limite connue

Comme tout ce qui repose sur `linux-wallpaperengine`, le fond est dessiné
**par-dessus** le bureau sous Wayland : pas d'icônes de bureau ni de clic droit
sur le bureau tant qu'un fond animé est actif. C'est une limite du backend, pas
de cette app.

## Idées pour la suite

- Réglage des propriétés par wallpaper (`--list-properties` / `--set-property`).

## Remerciements

Ce projet n'est qu'un frontend ; tout le rendu est assuré par
[Almamu/linux-wallpaperengine](https://github.com/Almamu/linux-wallpaperengine).
