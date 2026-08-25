# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Chemins du projet — résolus en absolu, jamais relatifs au répertoire courant.

Lancer l'application depuis un autre dossier ne doit ni lire ni écrire un
config.json étranger (les clés API y sont stockées).

Deux racines, et la distinction compte dès qu'on empaquette :

- `RESOURCE_ROOT` : les fichiers LIVRÉS avec l'application (sons, bibliothèques).
  Dans un exécutable PyInstaller, ils sont extraits à côté du code, pas dans le
  dépôt.
- `DATA_ROOT` : ce que l'application ÉCRIT. Un exécutable installé dans
  « Program Files » ou « /Applications » n'a pas le droit d'écrire chez lui —
  la configuration part donc dans le dossier de profil de l'utilisateur.

Lancé depuis les sources, tout reste à la racine du dépôt, comme avant.
"""

from __future__ import annotations

import os
import pathlib
import sys

#: Vrai dans un exécutable produit par PyInstaller.
FROZEN: bool = bool(getattr(sys, "frozen", False))

if FROZEN:
    # _MEIPASS pointe le dossier d'extraction (onefile) ou le dossier interne
    # du paquet (onedir) ; à défaut, le dossier de l'exécutable.
    RESOURCE_ROOT: pathlib.Path = pathlib.Path(
        getattr(sys, "_MEIPASS", pathlib.Path(sys.executable).resolve().parent)
    )
else:
    RESOURCE_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Conservé pour l'existant : la racine des ressources est ce que le code
#: entendait par « racine du projet ».
PROJECT_ROOT: pathlib.Path = RESOURCE_ROOT


def _dossier_utilisateur() -> pathlib.Path:
    """Dossier de configuration propre à la plateforme."""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA")
        return pathlib.Path(base) / "ZLink" if base else pathlib.Path.home() / "ZLink"
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library" / "Application Support" / "ZLink"
    base = os.environ.get("XDG_CONFIG_HOME")
    return (pathlib.Path(base) if base else pathlib.Path.home() / ".config") / "zlink"


#: Où l'application écrit. Depuis les sources, la racine du dépôt.
DATA_ROOT: pathlib.Path = _dossier_utilisateur() if FROZEN else RESOURCE_ROOT

# ZLINK_CONFIG déplace le fichier de configuration. Utile pour faire tourner une
# seconde instance sur un autre profil, et indispensable aux tests : sans lui,
# le moindre banc qui enregistre un réglage écrit dans la configuration réelle
# de l'utilisateur.
_ENV_CONFIG = (os.environ.get("ZLINK_CONFIG") or "").strip()
CONFIG_PATH: pathlib.Path = (
    pathlib.Path(_ENV_CONFIG).expanduser() if _ENV_CONFIG
    else DATA_ROOT / "config.json"
)
