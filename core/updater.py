# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Vérification des mises à jour via l'API GitHub Releases.

Portée volontairement limitée à la NOTIFICATION. Télécharger et exécuter un
binaire distant est un canal d'exécution de code à distance : tant qu'il n'y a
pas d'artefacts signés à vérifier, on se contente de prévenir l'utilisateur et
de lui ouvrir la page de release dans son navigateur, où il voit ce qu'il
télécharge.

`verify_signature()` est déjà là pour le jour où les artefacts existeront : la
clé publique Ed25519 sera embarquée ici, la privée restant dans les secrets du
dépôt.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request

from PyQt6.QtCore import QObject, pyqtSignal

from core.version import GITHUB_OWNER, GITHUB_REPO, __version__, is_newer

logger = logging.getLogger(__name__)

_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
_TIMEOUT_S = 8.0
# Une release GitHub reste petite ; au-delà, quelque chose ne va pas.
_MAX_BYTES = 512 * 1024

# Clé publique Ed25519 des releases, au format brut (32 octets, en hexadécimal).
# Vide tant qu'aucune release n'est signée — la vérification refuse alors tout.
RELEASE_PUBKEY_HEX = "1304b070f3d0f77349f81c9926ec0bebe494c6ba39ecf766ab137bdb03362b07"


def verify_signature(payload: bytes, signature: bytes) -> bool:
    """Vérifie une signature Ed25519 détachée d'un artefact.

    Retourne False si aucune clé n'est configurée : sans clé, on ne peut rien
    garantir, et « je ne sais pas » doit se comporter comme « non ».
    """
    if not RELEASE_PUBKEY_HEX or not signature:
        return False
    try:
        from Crypto.Signature import eddsa

        # eddsa.import_public_key pour une clé brute de 32 octets :
        # ECC.import_key ne sait pas lire ce format.
        key = eddsa.import_public_key(bytes.fromhex(RELEASE_PUBKEY_HEX))
        eddsa.new(key, "rfc8032").verify(payload, signature)
        return True
    except Exception as exc:
        logger.warning("Signature de release invalide — %s", exc)
        return False


def fetch_latest() -> dict | None:
    """Interroge GitHub. Retourne None en cas d'échec — jamais d'exception.

    Une mise à jour indisponible ne doit pas déranger : pas de release publiée
    (404), hors ligne, quota dépassé, tout se solde par un silence.
    """
    req = urllib.request.Request(
        _API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"ZLink/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            raw = resp.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            logger.warning("Réponse GitHub anormalement volumineuse, ignorée")
            return None
        return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            logger.debug("Aucune release publiée pour le moment")
        else:
            logger.debug("Vérification des mises à jour : HTTP %s", exc.code)
    except Exception as exc:
        logger.debug("Vérification des mises à jour impossible — %s", exc)
    return None


class UpdateChecker(QObject):
    """Interroge GitHub en arrière-plan et signale une version plus récente."""

    update_available = pyqtSignal(str, str)   # version, url de la release

    def check(self) -> None:
        """Lance la vérification. Ne bloque pas l'interface."""
        threading.Thread(
            target=self._worker, daemon=True, name="update-check",
        ).start()

    def _worker(self) -> None:
        data = fetch_latest()
        if not data:
            return
        tag = str(data.get("tag_name") or "")
        url = str(data.get("html_url") or "")
        if data.get("draft") or data.get("prerelease"):
            return
        if not tag or not url.startswith("https://github.com/"):
            return
        if not is_newer(tag):
            logger.info("ZLink est à jour (%s)", __version__)
            return
        logger.info("Mise à jour disponible : %s (installée : %s)", tag, __version__)
        self.update_available.emit(tag, url)
