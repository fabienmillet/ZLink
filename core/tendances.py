# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Ce qu'une chaîne a fait dans la dernière heure.

`HistoryStore` garde les totaux de l'ÉVÉNEMENT — cagnotte et audience
cumulées. Il ne dit rien de chaque streamer, alors que c'est là qu'est
l'information intéressante un soir de ZEvent : qui monte en ce moment, qui
vient de recevoir un afflux, qui redescend.

Aucune API ne le donne : les participations rendent un instantané, jamais une
série. On la constitue donc en gardant ce qui passe. Deux heures de relevés
toutes les trente secondes, pour trois cents chaînes, tiennent en mémoire sans
qu'on ait à y penser — et rien n'est écrit sur le disque : une tendance ne
survit pas à la fermeture, et n'aurait aucun sens après.

La mesure est un ÉCART entre deux relevés, jamais une dérivée instantanée :
l'API rafraîchit ses chiffres toutes les quelques minutes, et deux sondages
consécutifs rendent souvent la même valeur.
"""

from __future__ import annotations

import logging
import time
from collections import deque

logger = logging.getLogger(__name__)

#: Fenêtre par défaut : l'heure écoulée.
FENETRE_S = 3600.0

#: Ce qu'on garde. Un peu plus que la fenêtre, pour qu'un relevé un peu vieux
#: reste disponible quand le sondage a sauté un tour.
MEMOIRE_S = 2 * 3600.0

#: login → relevés (instant, viewers, cagnotte), du plus ancien au plus récent.
_series: dict[str, deque[tuple[float, int, float]]] = {}


def noter(streamers, maintenant: float | None = None) -> None:
    """Enregistre un relevé. Appelé à chaque mise à jour des streamers."""
    instant = time.time() if maintenant is None else maintenant
    for s in streamers or ():
        login = str(getattr(s, "twitch_login", "") or "")
        if not login:
            continue
        serie = _series.setdefault(login, deque())
        serie.append((instant, int(getattr(s, "viewers", 0) or 0),
                      float(getattr(s, "donation", 0.0) or 0.0)))
        limite = instant - MEMOIRE_S
        while serie and serie[0][0] < limite:
            serie.popleft()


def _reference(login: str, fenetre: float, maintenant: float):
    """Le relevé le plus ancien encore DANS la fenêtre, et le plus récent.

    Rend None tant qu'il n'y a pas deux relevés distants d'au moins un quart
    de la fenêtre : sur les premières minutes, un écart mesuré sur trente
    secondes et rapporté à l'heure donnerait des chiffres absurdes.
    """
    serie = _series.get(login)
    if not serie or len(serie) < 2:
        return None
    debut = maintenant - fenetre
    anciens = [p for p in serie if p[0] >= debut]
    if len(anciens) < 2:
        anciens = list(serie)[-2:]
    premier, dernier = anciens[0], anciens[-1]
    if dernier[0] - premier[0] < fenetre / 4:
        return None
    return premier, dernier


def viewers(login: str, fenetre: float = FENETRE_S,
            maintenant: float | None = None) -> int | None:
    """Viewers gagnés ou perdus sur la fenêtre. None si on ne sait pas encore."""
    instant = time.time() if maintenant is None else maintenant
    bornes = _reference(login, fenetre, instant)
    if bornes is None:
        return None
    premier, dernier = bornes
    return dernier[1] - premier[1]


def cagnotte(login: str, fenetre: float = FENETRE_S,
             maintenant: float | None = None) -> float | None:
    """Euros récoltés sur la fenêtre. None si on ne sait pas encore.

    Jamais négatif : une cagnotte ne redescend pas, et un écart négatif ne
    peut venir que d'une correction de l'API — l'annoncer comme une perte
    serait faux.
    """
    instant = time.time() if maintenant is None else maintenant
    bornes = _reference(login, fenetre, instant)
    if bornes is None:
        return None
    premier, dernier = bornes
    return max(0.0, dernier[2] - premier[2])


def oublier_tout() -> None:
    """Vide les séries. Utile aux tests et à un changement d'édition."""
    _series.clear()
