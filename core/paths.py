"""Chemins du projet — résolus en absolu, jamais relatifs au répertoire courant.

Lancer l'application depuis un autre dossier ne doit ni lire ni écrire un
config.json étranger (les clés API y sont stockées).
"""

from __future__ import annotations

import pathlib

PROJECT_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH: pathlib.Path = PROJECT_ROOT / "config.json"
