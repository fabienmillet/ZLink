# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Sons d'alerte — palier de cagnotte et objectif atteint.

Pendant un event, l'écran du panel n'est pas toujours celui qu'on regarde. Un
son court signale l'essentiel sans rien demander, à condition de rester
discret : deux timbres seulement, brefs, à mi-volume, et **désactivés par
défaut**.

QSoundEffect vient de PyQt6 : rien à installer, et il garde l'échantillon en
mémoire, donc pas de latence de lecture à la première alerte. Les instances
sont conservées volontairement — un QSoundEffect collecté pendant sa lecture
ne produit aucun son.
"""

from __future__ import annotations

import logging
import math

from PyQt6.QtCore import QUrl
from PyQt6.QtMultimedia import QSoundEffect

from core.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)

_DOSSIER = PROJECT_ROOT / "assets" / "sounds"
_FICHIERS = {
    "milestone": "milestone.wav",
    "goal": "goal.wav",
}

_effets: dict[str, QSoundEffect] = {}
_actif: bool = False
_volume: float = 0.6


def configure(config: dict) -> None:
    """Applique la configuration. `sounds.enabled` vaut faux par défaut."""
    global _actif, _volume
    cfg = (config or {}).get("sounds")
    # Le TYPE est verifie, pas seulement la presence : `{"sounds": "oui"}` dans
    # un config.json abime faisait lever AttributeError sur le .get() suivant,
    # au lancement — l'application ne demarrait plus au lieu de perdre ses sons.
    # core.alerts fait deja cette verification ; l'oubli etait ici.
    if not isinstance(cfg, dict):
        cfg = {}
    _actif = bool(cfg.get("enabled", False))
    try:
        brut = float(cfg.get("volume", 60)) / 100.0
        # NaN seulement, pas isfinite : les infinis se bornent tres bien
        # (-inf -> 0.0, +inf -> 1.0). NaN, lui, traverse le bornage — toute
        # comparaison avec lui etant fausse, max(0.0, min(1.0, nan)) rend 1.0,
        # soit le volume MAXIMUM. json.loads accepte le litteral NaN.
        _volume = 0.6 if math.isnan(brut) else max(0.0, min(1.0, brut))
    except (TypeError, ValueError):
        _volume = 0.6
    for eff in _effets.values():
        eff.setVolume(_volume)
    logger.info("Sons d'alerte : %s (volume %d %%)",
                "activés" if _actif else "désactivés", int(_volume * 100))


def _effet(nom: str) -> QSoundEffect | None:
    eff = _effets.get(nom)
    if eff is not None:
        return eff
    fichier = _FICHIERS.get(nom)
    if not fichier:
        return None
    chemin = _DOSSIER / fichier
    if not chemin.exists():
        logger.warning("Son introuvable : %s", chemin)
        return None
    eff = QSoundEffect()
    eff.setSource(QUrl.fromLocalFile(str(chemin)))
    eff.setVolume(_volume)
    _effets[nom] = eff
    return eff


def play(nom: str, force: bool = False) -> bool:
    """Joue un son. Sans effet si les sons sont coupés, sauf `force` (test)."""
    if not (_actif or force):
        return False
    eff = _effet(nom)
    if eff is None:
        return False
    try:
        eff.play()
        return True
    except Exception as exc:  # un son n'interrompt rien  # noqa: BLE001
        logger.debug("Son %s non joué — %s", nom, exc)
        return False


def is_enabled() -> bool:
    return _actif
