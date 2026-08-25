# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Cache disque des photos de profil — point d'entrée unique.

Trois chemins réclament les mêmes images : le pré-chargement de `data_manager`,
la mosaïque `bigscreen_widget` et les pastilles du panel. Chacun avait sa propre
copie du téléchargement, aucun ne savait ce que les autres faisaient : 510
requêtes HTTP pour 300 images distinctes, soit 70 % de trafic inutile, et deux
écritures concurrentes sur le même fichier.

Le verrou par clé règle les deux : le second demandeur attend le premier, puis
trouve le fichier déjà là et n'émet aucune requête.
"""

from __future__ import annotations

import logging
import os
import pathlib
import threading
import urllib.request

logger = logging.getLogger(__name__)

CACHE_DIR = pathlib.Path.home() / ".zlink" / "avatars"
MAX_BYTES = 2 * 1024 * 1024   # plafond de lecture pour un avatar

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


def path_for(key: str) -> pathlib.Path:
    """Emplacement du fichier de cache pour une clé donnée."""
    return CACHE_DIR / f"{key}.png"


def _lock_for(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
        return lock


def download(key: str, url: str) -> bool:
    """Télécharge l'avatar si absent du cache. Vrai si le fichier est présent.

    Ne lève jamais : un avatar manquant se dégrade en initiales, ce n'est pas
    une erreur qui doit remonter à l'appelant.
    """
    if not key or not url:
        return False
    dest = path_for(key)
    if dest.exists():
        return True

    # `key` et `url` viennent d'APIs tierces : la première sert de nom de
    # fichier (pathlib ne normalise pas ".."), la seconde était passée telle
    # quelle à urlopen, qui accepte aussi file:// et ftp://.
    if dest.resolve().parent != CACHE_DIR.resolve():
        logger.error("Avatar %r : chemin hors du cache, ignoré", key[:40])
        return False
    if not url.lower().startswith("https://"):
        logger.error("Avatar %s : URL non https, ignorée", key)
        return False

    with _lock_for(key):
        # Relecture SOUS le verrou : pendant l'attente, l'autre demandeur a
        # très probablement terminé — c'est exactement le doublon qu'on évite.
        if dest.exists():
            return True
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(url, headers={"User-Agent": "ZLink/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = resp.read(MAX_BYTES + 1)
            if len(payload) > MAX_BYTES:
                logger.error("Avatar %s : réponse > %d octets, ignorée",
                             key, MAX_BYTES)
                return False
            # Écriture atomique : write_bytes tronque le fichier à zéro avant
            # d'écrire, et Qt décode un PNG partiel SANS le signaler nul — la
            # vignette abîmée serait mise en cache définitivement.
            tmp = dest.with_name(
                f"{dest.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                tmp.write_bytes(payload)
                os.replace(tmp, dest)
            finally:
                tmp.unlink(missing_ok=True)
            return True
        except Exception as exc:
            logger.debug("Avatar %s : téléchargement échoué — %s", key, exc)
            return False
