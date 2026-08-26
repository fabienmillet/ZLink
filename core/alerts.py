# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Interrupteurs des alertes — une case par famille, au même endroit.

Chaque signalement ajouté au fil du temps avait ses propres réglages, ou aucun :
HypeWatcher pouvait se couper, les sons aussi, mais les paliers de cagnotte, les
raids, les afflux de dons ou les objectifs imminents s'imposaient. Une alerte
qu'on ne peut pas éteindre finit par être subie.

Le contrôle se fait À LA SOURCE : un détecteur désactivé ne calcule rien et
n'écrit rien dans le journal, plutôt que de produire un événement qu'on jette
ensuite.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Familles d'alertes, leur libellé et leur état par défaut.
#: L'ordre est celui de la fenêtre de réglages.
FAMILLES: list[tuple[str, str, bool, str]] = [
    ("hype", "Moments forts (HypeWatcher)", True,
     "Un pic de messages très au-dessus du rythme habituel d'une chaîne."),
    ("milestone", "Paliers de cagnotte", True,
     "Franchissement d'un palier rond : 250 k€, 1 M€, 2 M€…"),
    ("donation", "Afflux de dons sur une chaîne", True,
     "Une chaîne qui reçoit une somme notable entre deux relevés."),
    ("goal_imminent", "Objectifs sur le point de tomber", True,
     "Moins de 500 € ou plus de 98 % — de quoi basculer pour le voir tomber."),
    ("goal_done", "Objectifs atteints", True,
     "Un objectif de don vient d'être accompli."),
    ("favorite_live", "Un favori passe en direct", True,
     "Proposition de basculer, sans jamais l'imposer."),
    ("show_started", "Un show du programme commence", True,
     "Proposition de basculer sur le présentateur."),
    ("raid", "Raids reçus", True,
     "Une chaîne affichée reçoit un raid."),
    ("top_entry", "Entrée dans les 3 plus grosses audiences", True,
     "Seulement pour une chaîne qui n'est pas déjà affichée."),
    ("ressources", "Saturation du poste", True,
     "Le processeur ou le décodeur vidéo sature, et ZLink en est la cause : "
     "conseil de réduire le nombre de flux."),
]

_DEFAUTS = {cle: defaut for cle, _lib, defaut, _aide in FAMILLES}
_ETATS: dict[str, bool] = dict(_DEFAUTS)


def configure(config: dict) -> None:
    """Applique la configuration. Une famille absente garde son défaut."""
    brut = (config or {}).get("alerts")
    brut = brut if isinstance(brut, dict) else {}
    for cle, defaut in _DEFAUTS.items():
        _ETATS[cle] = bool(brut.get(cle, defaut))
    coupees = [c for c, v in _ETATS.items() if not v]
    if coupees:
        logger.info("Alertes désactivées : %s", ", ".join(sorted(coupees)))


def enabled(famille: str) -> bool:
    """Vrai si cette famille d'alertes doit être produite."""
    return _ETATS.get(famille, True)


def states() -> dict[str, bool]:
    return dict(_ETATS)
