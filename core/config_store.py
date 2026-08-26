# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Lecture et écriture de config.json.

Quatre endroits écrivent ce fichier : les réglages, l'assistant de première
configuration, les favoris et les rappels du programme. Deux règles doivent
valoir pour tous, et la fenêtre de réglages en enfreignait les deux.

**Fusionner, ne pas remplacer.** Les réglages chargeaient la configuration à
leur construction — au démarrage — et réécrivaient cet instantané à la
sauvegarde. Tout ce qui avait changé entre-temps par un autre chemin (un favori
ajouté, un rappel posé, l'assistant) était silencieusement rétabli à sa valeur
de départ.

**Écrire de façon atomique.** `write_text` tronque le fichier à zéro avant
d'écrire : une coupure au mauvais moment laisse une configuration vide ou
tronquée — clés API comprises.
"""

from __future__ import annotations

import json
import logging
import os

from core.paths import CONFIG_PATH

logger = logging.getLogger(__name__)


#: Clés d'anciennes fonctionnalités, retirées du fichier à la prochaine
#: écriture. L'assistant IA a été supprimé du projet ; ses réglages restaient
#: dans config.json, dont deux emplacements de clés d'API qui n'ont plus de
#: raison d'exister — autant ne pas conserver de champ à secret inutile.
_RETIRED_KEYS = frozenset({
    "ai_provider", "ai_model", "gemini_api_key", "openai_api_key",
})


def load() -> dict:
    """Configuration sur disque. Dictionnaire vide si absente ou illisible."""
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            logger.warning("config.json ne contient pas un objet — ignoré")
    except (OSError, ValueError) as exc:
        logger.warning("config.json illisible — %s", exc)
    return {}


def save_merge(patch: dict) -> bool:
    """Applique `patch` par-dessus le fichier ACTUEL, puis écrit le tout.

    La relecture juste avant l'écriture est le cœur du contrat : elle garantit
    qu'on ne réécrit jamais des valeurs périmées lues bien plus tôt.
    """
    if not isinstance(patch, dict):
        return False
    merged = load()
    merged.update(patch)
    for dead in _RETIRED_KEYS:
        merged.pop(dead, None)
    return _write(merged)


def _write(config: dict) -> bool:
    try:
        # Dans un exécutable installé, la configuration part dans le profil de
        # l'utilisateur : ce dossier n'existe pas au premier lancement.
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        try:
            # Le fichier peut contenir des clés API : propriétaire seulement.
            os.chmod(tmp, 0o600)
        except OSError as exc:
            logger.warning("Permissions de %s non restreintes : %s",
                           CONFIG_PATH, exc)
        os.replace(tmp, CONFIG_PATH)
        return True
    except OSError:
        logger.exception("Écriture de la configuration impossible")
        try:
            tmp.unlink(missing_ok=True)
        except (OSError, NameError):
            pass
        return False
