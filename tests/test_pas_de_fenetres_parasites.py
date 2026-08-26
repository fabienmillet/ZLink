# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Aucune reconstruction d'interface ne doit laisser de fenêtre derrière elle.

Un widget détaché de son parent devient une fenêtre de PREMIER NIVEAU. S'il
était visible, c'en est une à l'écran. Les listes de ZLink se reconstruisent à
chaque rafraîchissement — toutes les 30 s en usage réel, toutes les 3 s en
mock — et chaque ligne détachée sans avoir été masquée surgissait quelque part
sur le bureau.

Mesuré avant correction sur l'onglet Goals : +124 fenêtres par rafraîchissement,
1 340 widgets de premier niveau accumulés en dix tours. Après : 4, stable.

Deux garde-fous ici : un contrôle du CODE, qui interdit de détacher sans
masquer, et un contrôle du COMPORTEMENT, qui exerce les rafraîchissements et
compte ce qui flotte.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtWidgets import QApplication, QWidget

from windows import panel

RACINE = pathlib.Path(__file__).resolve().parent.parent

#: `X.setParent(None)` en début d'instruction, avec sa cible.
_DETACHE = re.compile(r"^(\s*)([A-Za-z_][\w.\[\]\"']*)\.setParent\(None\)")


def _detachements_sans_masquage(chemin: pathlib.Path) -> list[str]:
    """Lignes qui détachent un widget sans l'avoir masqué juste avant."""
    fautes = []
    lignes = chemin.read_text(encoding="utf-8").split("\n")
    for i, ligne in enumerate(lignes):
        m = _DETACHE.match(ligne)
        if not m:
            continue
        cible = m.group(2)
        # On remonte au-dessus des commentaires : ils ne changent rien au flot.
        j = i - 1
        while j >= 0 and lignes[j].strip().startswith("#"):
            j -= 1
        precedente = lignes[j].strip() if j >= 0 else ""
        # `startswith` : la ligne de masquage porte souvent un commentaire.
        if not precedente.startswith(f"{cible}.hide()"):
            fautes.append(f"{chemin.name}:{i + 1} — {ligne.strip()}")
    return fautes


@pytest.mark.parametrize("dossier", ["windows", "widgets", "core"])
def test_on_masque_toujours_avant_de_detacher(dossier):
    """Le garde-fou de code : un oubli ici et les fenêtres reviennent."""
    fautes = []
    for chemin in (RACINE / dossier).rglob("*.py"):
        fautes += _detachements_sans_masquage(chemin)
    assert fautes == [], (
        "un widget détaché et visible est une fenêtre à l'écran ; "
        "appeler .hide() juste avant :\n  " + "\n  ".join(fautes))


class _S:
    """Le strict nécessaire des onglets qui listent des streamers."""

    def __init__(self, login: str, online: bool = True) -> None:
        self.twitch_login = login
        self.display = login
        self.online = online
        self.viewers = 100
        self.donation = 1000.0
        self.donation_formatted = "1000 €"
        self.location = "LAN"
        self.game = "Jeu"
        self.profile_url = ""
        self.participation_id = "p" + login
        self.gdoc_id = "g" + login
        self.title = ""
        self.donation_url = ""


class _Guet(QObject):
    """Attrape le MOMENT où un widget sans parent devient visible.

    Un instantané ne suffit pas : ces fenêtres CLIGNOTENT. Le widget est rendu
    visible, puis `addWidget` le reparente quelques microsecondes plus tard —
    il n'est déjà plus de premier niveau quand on regarde la liste. C'est
    pourtant bien une fenêtre qui a surgi sur le bureau, et c'est ce que voyait
    l'utilisateur.

    On passe par un filtre d'événements Qt plutôt que par une substitution de
    `setVisible` : le filtre voit l'événement Show de TOUT objet, sans toucher
    à la classe ni risquer de laisser une méthode substituée derrière soi.
    """

    def __init__(self, ignorer=()) -> None:
        super().__init__()
        self.apparitions: list[str] = []
        self._ignorer = set(ignorer)

    def __enter__(self):
        QApplication.instance().installEventFilter(self)
        return self

    def __exit__(self, *_exc):
        QApplication.instance().removeEventFilter(self)
        return False

    def eventFilter(self, obj, event):  # type: ignore[override]
        if (event.type() == QEvent.Type.Show
                and isinstance(obj, QWidget)
                and obj.parent() is None
                and obj not in self._ignorer):
            self.apparitions.append(type(obj).__name__)
        return False


def _flottantes(sauf) -> list:
    """Widgets de premier niveau visibles qui ne sont pas les nôtres."""
    return [w for w in QApplication.instance().topLevelWidgets()
            if w.isVisible() and w not in sauf]


def test_rafraichir_les_streamers_ne_laisse_rien_flotter(qtbot):
    """Le chemin qui tourne SANS mock : des chaînes qui passent en ligne et
    hors ligne, donc des cartes détruites à chaque tour."""
    onglet = panel._StreamersTab()
    qtbot.addWidget(onglet)
    onglet.resize(1000, 700)
    onglet.show()
    avant = _flottantes([onglet])

    for tour in range(6):
        onglet.refresh([_S(f"s{i}", online=((i + tour) % 3 != 0))
                        for i in range(20)], [])
        QApplication.processEvents()

    assert _flottantes([onglet] + avant) == []


def test_reconstruire_la_console_ne_laisse_rien_flotter(qtbot):
    onglet = panel._MixerTab()
    qtbot.addWidget(onglet)
    onglet.show()
    avant = _flottantes([onglet])

    for tour in range(6):
        onglet.set_main_stream(f"s{tour}")
        onglet.set_pinned([f"s{i}" for i in range(tour % 4)])
        QApplication.processEvents()

    assert _flottantes([onglet] + avant) == []


def test_reagencer_les_sections_ne_fait_surgir_aucune_fenetre(qtbot):
    """Le cas trouvé en traçant l'application réelle.

    Les six widgets de section — trois en-têtes, trois grilles — étaient créés
    SANS parent, et `_rebuild_cards` les rendait visibles AVANT de les insérer
    dans le layout. Six fenêtres nues surgissaient à chaque réagencement, et un
    réagencement a lieu dès qu'un streamer change d'état.
    """
    onglet = panel._StreamersTab()
    qtbot.addWidget(onglet)
    onglet.resize(1000, 700)
    onglet.show()

    with _Guet(ignorer=[onglet]) as guet:
        for tour in range(4):
            onglet.refresh([_S(f"s{i}", online=((i + tour) % 3 != 0))
                            for i in range(12)], [])
            QApplication.processEvents()

    assert guet.apparitions == [], (
        "un widget rendu visible avant d'avoir un parent est une fenêtre : "
        + ", ".join(sorted(set(guet.apparitions))))


def test_le_guet_attrape_bien_une_fenetre_nue(qtbot):
    """Garde-fou du garde-fou : sans lui, un test vert ne prouverait rien."""
    from PyQt6.QtWidgets import QLabel

    with _Guet() as guet:
        orphelin = QLabel("je surgis")
        orphelin.show()
        orphelin.hide()
    assert "QLabel" in guet.apparitions


def test_reconstruire_les_goals_ne_fait_surgir_aucune_fenetre(qtbot):
    """L'autre chemin signalé : la liste des objectifs, reconstruite à chaque
    arrivée de données."""
    onglet = panel._GoalsTab()
    qtbot.addWidget(onglet)
    onglet._do_fetch = lambda *a: None
    onglet.show()

    class _G:
        def __init__(self, nom, montant, accompli=False):
            self.id = nom
            self.name = nom
            self.amount = montant
            self.accomplished = accompli
            self.category = "donation"
            self.links = []

    streamers = [_S(f"s{i}") for i in range(8)]
    with _Guet(ignorer=[onglet]) as guet:
        for tour in range(4):
            onglet.set_streamers(streamers)
            onglet.seed_cache({
                s.twitch_login: [_G(f"g{tour}-{j}", 900.0 + j * 100)
                                 for j in range(3)]
                for s in streamers})
            QApplication.processEvents()
        onglet._changer_vue("tous")
        QApplication.processEvents()

    assert guet.apparitions == [], ", ".join(sorted(set(guet.apparitions)))
