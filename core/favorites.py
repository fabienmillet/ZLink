# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Streamers favoris — mise en avant dans la grille, les stats et la palette.

Stockés dans config.json aux côtés des autres préférences, pour survivre au
redémarrage : sur un event de trois jours, l'application est forcément relancée.
"""

from __future__ import annotations

import json
import logging
import os
import threading

from core.paths import CONFIG_PATH

logger = logging.getLogger(__name__)

_KEY = "favorite_logins"
_LOCK = threading.Lock()
_cache: set[str] | None = None


def _read() -> set[str]:
    try:
        if not CONFIG_PATH.exists():
            return set()
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        valeurs = raw.get(_KEY)
        # Le type est vérifié, pas seulement la présence : sur une chaîne, la
        # compréhension itérait les CARACTÈRES et fabriquait des favoris à une
        # lettre à partir d'un fichier corrompu.
        if not isinstance(valeurs, list):
            return set()
        return {str(v).lower() for v in valeurs if v}
    except Exception as exc:
        logger.warning("Favoris illisibles — %s", exc)
        return set()


def get() -> set[str]:
    """Logins favoris. Lu une seule fois puis gardé en mémoire."""
    global _cache
    with _LOCK:
        if _cache is None:
            _cache = _read()
        return set(_cache)


def is_favorite(login: str) -> bool:
    return bool(login) and login.lower() in get()


def toggle(login: str) -> bool:
    """Ajoute ou retire un favori. Retourne le nouvel état."""
    global _cache
    if not login:
        return False
    login = login.lower()
    with _LOCK:
        if _cache is None:
            _cache = _read()
        now_fav = login not in _cache
        if now_fav:
            _cache.add(login)
        else:
            _cache.discard(login)
        _save(_cache)
    return now_fav


def _save(logins: set[str]) -> None:
    """Écrit les favoris dans config.json (lecture-modification-écriture)."""
    try:
        cfg = {}
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cfg[_KEY] = sorted(logins)
        # Atomique : une écriture directe tronque le fichier à zéro d'abord, et
        # un lecteur concurrent y verrait un JSON incomplet.
        tmp = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        logger.exception("Sauvegarde des favoris impossible")
