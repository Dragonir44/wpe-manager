# Publier une release

Deux cibles : **PyPI** (base universelle, `pipx install wpe-manager`) puis
**AUR** (`wpe-manager`) qui consomme le sdist PyPI.

## 0. Bump de version (source unique)

La version vit **uniquement** dans `wpe_manager/__init__.py` (`__version__`) ;
`pyproject.toml` la lit dynamiquement. Pour une nouvelle release :

1. Mettre à jour `__version__` dans `wpe_manager/__init__.py`.
2. Committer + taguer :
   ```bash
   git tag v0.5.0 && git push --tags
   ```

## 1. PyPI

Une seule fois : créer un compte sur https://pypi.org, activer la 2FA, puis
générer un **jeton d'API** (Account settings → API tokens). Installer l'outil :

```bash
pipx install twine        # ou : pip install --user twine build
```

À chaque release :

```bash
rm -rf dist build
python -m build                    # crée dist/*.tar.gz + *.whl
twine check dist/*                 # sanity-check des métadonnées
twine upload dist/*                # user = __token__ , password = le jeton pypi-...
```

> `twine` installé via pipx vit dans son propre venv : appelle la commande
> `twine` directement (pas `python -m twine`, qui ne le trouverait pas).

Astuce : mettre le jeton dans `~/.pypirc` pour ne pas le retaper :

```ini
[pypi]
  username = __token__
  password = pypi-AgEIcHl...
```

Vérifier : `pipx install wpe-manager` depuis une autre machine / un venv propre.

> Conseil : pousser d'abord sur **TestPyPI** (https://test.pypi.org) avec
> `twine upload -r testpypi dist/*` pour valider la fiche sans polluer PyPI.

## 2. AUR (après la mise en ligne PyPI)

Une seule fois :
- Créer un compte sur https://aur.archlinux.org et y ajouter ta **clé SSH**.
- Cloner le dépôt du paquet (vide au premier push) :
  ```bash
  git clone ssh://aur@aur.archlinux.org/wpe-manager.git aur-wpe-manager
  ```

À chaque release, depuis `packaging/aur/` de ce repo :

1. Mettre `pkgver` à jour dans `PKGBUILD` (et remettre `pkgrel=1`).
2. Régénérer le hash **depuis le fichier réellement hébergé sur PyPI** :
   ```bash
   updpkgsums                       # paquet: pacman-contrib
   makepkg --printsrcinfo > .SRCINFO
   ```
3. Tester le build en local :
   ```bash
   makepkg -si                      # doit builder et s'installer proprement
   ```
4. Copier `PKGBUILD` + `.SRCINFO` dans le clone AUR, puis :
   ```bash
   cd aur-wpe-manager
   cp ../wpe-manager/packaging/aur/{PKGBUILD,.SRCINFO} .
   git add PKGBUILD .SRCINFO
   git commit -m "upgpkg: wpe-manager 0.5.0"
   git push
   ```

> Le `sha256sums` du `PKGBUILD` versionné ici correspond au sdist construit
> localement. Si tu reconstruis le tarball (timestamps différents) avant de
> l'uploader sur PyPI, relance `updpkgsums` pour resynchroniser.
