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

#: Familles qui parlent des OBJECTIFS d'une chaîne, et qu'on peut donc
#: restreindre à ses favoris. Trois cents participants publient des dizaines
#: d'objectifs chacun : tout signaler revient à ne rien signaler.
FAMILLES_OBJECTIFS: tuple[str, ...] = ("goal_imminent", "goal_done")

#: Clé du réglage dans config.json.
CLE_OBJECTIFS_FAVORIS = "alerts_objectifs_favoris_seulement"

#: Faux par défaut : couper des alertes sans qu'on l'ait demandé serait pire
#: que d'en recevoir trop — on ne remarque pas ce qui n'arrive pas.
_OBJECTIFS_FAVORIS_SEULEMENT: bool = False


def configure(config: dict) -> None:
    """Applique la configuration. Une famille absente garde son défaut."""
    global _OBJECTIFS_FAVORIS_SEULEMENT
    config = config or {}
    brut = config.get("alerts")
    brut = brut if isinstance(brut, dict) else {}
    for cle, defaut in _DEFAUTS.items():
        _ETATS[cle] = bool(brut.get(cle, defaut))
    _OBJECTIFS_FAVORIS_SEULEMENT = bool(config.get(CLE_OBJECTIFS_FAVORIS, False))
    coupees = [c for c, v in _ETATS.items() if not v]
    if coupees:
        logger.info("Alertes désactivées : %s", ", ".join(sorted(coupees)))
    if _OBJECTIFS_FAVORIS_SEULEMENT:
        logger.info("Alertes d'objectifs restreintes aux favoris")


def objectifs_favoris_seulement() -> bool:
    """Vrai si les alertes d'objectifs ne concernent que les favoris."""
    return _OBJECTIFS_FAVORIS_SEULEMENT


def enabled_pour(famille: str, login: str) -> bool:
    """Comme `enabled`, mais pour une alerte qui vise UNE chaîne.

    Le contrôle reste à la source : une alerte écartée ici n'est jamais
    produite, plutôt que d'être filtrée à l'affichage — sans quoi elle
    resterait dans le fil d'événements et dans le journal.

    Un login vide passe : l'alerte ne vise alors personne en particulier, et
    la restriction n'a rien à mordre.
    """
    if not enabled(famille):
        return False
    if not _OBJECTIFS_FAVORIS_SEULEMENT or famille not in FAMILLES_OBJECTIFS:
        return True
    if not login:
        return True
    from core import favorites
    return str(login).lower() in favorites.get()


def enabled(famille: str) -> bool:
    """Vrai si cette famille d'alertes doit être produite."""
    return _ETATS.get(famille, True)


def states() -> dict[str, bool]:
    return dict(_ETATS)
