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
- **Un process par écran** : changer un écran ne redémarre pas les autres.
- **Assignation par écran** : un **fond fixe** ou une **playlist tournante**.
- **Playlists** créées par **cases à cocher** dans la grille, avec **intervalle**
  et **ordre** (séquentiel / aléatoire).
- **Import depuis Wallpaper Engine** : récupère les playlists du `config.json`
  de WPE (extrait les IDs, ignore les chemins/moniteurs Windows non portables).
- **Transition sans coupure** : le nouveau fond est affiché *par-dessus* l'ancien
  avant que celui-ci soit tué (pas de fondu alpha — le backend ne le permet pas —
  mais plus de flash du fond de bureau entre deux).
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

> La rotation avance tant que l'application est ouverte (un `QTimer` la pilote).
> Un mode systray/daemon pour la faire survivre en arrière-plan est prévu.

## Fichiers de config

- `~/.config/wpe-manager/config.json` — chemins, muet, FPS, transition.
- `~/.config/wpe-manager/state.json` — assignations par écran (fond / playlist).
- `~/.config/wpe-manager/playlists.json` — playlists.
- `~/.config/wpe-manager/engine.json` — process backend en cours (par écran).

## Autostart (restaurer les fonds à l'ouverture de session)

`wpe-manager --autostart` relit l'état sauvegardé et relance le backend (sans
fenêtre). Crée `~/.config/autostart/wpe-manager.desktop` :

```ini
[Desktop Entry]
Type=Application
Name=Wallpaper Engine Manager (autostart)
Exec=wpe-manager --autostart
X-KDE-autostart-phase=2
```

## Limite connue

Comme tout ce qui repose sur `linux-wallpaperengine`, le fond est dessiné
**par-dessus** le bureau sous Wayland : pas d'icônes de bureau ni de clic droit
sur le bureau tant qu'un fond animé est actif. C'est une limite du backend, pas
de cette app.

## Idées pour la suite

- Icône systray + mode daemon (rotation persistante, reprise au démarrage).
- Réglage des propriétés par wallpaper (`--list-properties` / `--set-property`).
- Filtre par type (scene / video / web) et par tag.

## Remerciements

Ce projet n'est qu'un frontend ; tout le rendu est assuré par
[Almamu/linux-wallpaperengine](https://github.com/Almamu/linux-wallpaperengine).
