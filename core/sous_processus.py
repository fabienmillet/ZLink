# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Lancer un programme sans lui ouvrir de fenêtre de console.

Sous Windows, démarrer un exécutable de console depuis une application
graphique lui alloue une console — une fenêtre noire qui apparaît, prend le
premier plan, puis disparaît. Une fois, c'est un clignotement. Vingt cellules
de grille qui résolvent leur flux, c'est une pluie de fenêtres qui surgissent
seules et volent le focus à ce qu'on était en train de faire.

`streamlink` est appelé une fois par cellule au démarrage, encore à chaque
reprise, et une fois de plus pour un replay ou un clip. `git` l'est au
démarrage pour le numéro de version. Aucun de ces appels n'a de raison de se
montrer.

CREATE_NO_WINDOW n'existe que sous Windows : ailleurs, la fonction rend un
dictionnaire vide et l'appel `subprocess.run(..., **sans_fenetre())` reste
exactement ce qu'il était.
"""

from __future__ import annotations

import subprocess
import sys


def sans_fenetre() -> dict:
    """Options à passer à `subprocess` pour qu'aucune console n'apparaisse.

    À déplier dans l'appel : `subprocess.run([...], **sans_fenetre())`.
    """
    if sys.platform != "win32":
        return {}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}


def interdire_les_consoles() -> bool:
    """Impose CREATE_NO_WINDOW à TOUT sous-processus lancé par l'application.

    Le garde-fou de dernier recours. Déplier `**sans_fenetre()` à chaque appel
    suppose qu'on n'en oublie aucun — et qu'aucune bibliothèque tierce n'en
    lance pour son compte. Une seule console oubliée sur un chemin appelé une
    fois par cellule, et ce sont des centaines de fenêtres qui surgissent et
    volent le premier plan à tour de rôle.

    On enveloppe donc `subprocess.Popen` : c'est le passage obligé de
    `run`, `call`, `check_output` et de tout ce qui lance un programme en
    Python. Le drapeau n'est ajouté que s'il n'y en a pas déjà un, pour ne
    jamais contredire un appelant qui sait ce qu'il fait.

    À appeler UNE FOIS au démarrage, avant toute création de fenêtre. Rend True
    si la garde a été posée. Sans effet — et sans risque — hors Windows.
    """
    if sys.platform != "win32":
        return False
    if getattr(subprocess.Popen, "_zlink_sans_fenetre", False):
        return True

    original = subprocess.Popen.__init__
    #: DETACHED_PROCESS et CREATE_NEW_CONSOLE demandent explicitement une
    #: console : les respecter, plutôt que de fabriquer un drapeau contradictoire.
    deja_decide = (subprocess.CREATE_NO_WINDOW
                   | subprocess.DETACHED_PROCESS
                   | subprocess.CREATE_NEW_CONSOLE)

    def _init(self, *args, **kwargs):
        drapeaux = kwargs.get("creationflags", 0)
        if not drapeaux & deja_decide:
            kwargs["creationflags"] = drapeaux | subprocess.CREATE_NO_WINDOW
        return original(self, *args, **kwargs)

    _init._zlink_sans_fenetre = True          # type: ignore[attr-defined]
    subprocess.Popen.__init__ = _init         # type: ignore[method-assign]
    subprocess.Popen._zlink_sans_fenetre = True   # type: ignore[attr-defined]
    return True
