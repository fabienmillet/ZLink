# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Modèles d'affichage : rôles de fenêtre et résultat de la détection d'écrans.

Deux choses valent d'être verrouillées ici. D'abord `WindowRole.value` : ces
chaînes sont écrites telles quelles dans `screen_assignments` de config.json et
relues par `core.monitors`, c'est donc un format de fichier et pas un détail
interne — les renommer casserait silencieusement la config des utilisateurs qui
ont choisi leurs écrans à la main. Ensuite `DisplayLayout.get_screen`, la
question que main.py pose à chaque démarrage pour savoir où poser ses fenêtres.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QRect

from core.models import DisplayLayout, DisplayMode, ScreenAssignment, WindowRole


class _EcranFactice:
    """Sosie de QScreen réduit à ce que ScreenAssignment consulte.

    Un vrai QScreen exige une QGuiApplication vivante ; les deux propriétés
    testées ne demandent que `name()` et `geometry()`.
    """

    def __init__(self, nom: str, x: int = 0, y: int = 0,
                 largeur: int = 1920, hauteur: int = 1080) -> None:
        self._nom = nom
        self._geo = QRect(x, y, largeur, hauteur)

    def name(self) -> str:
        return self._nom

    def geometry(self) -> QRect:
        return self._geo


def _assignment(role: WindowRole = WindowRole.PANEL, **kw) -> ScreenAssignment:
    ecran = kw.pop("ecran", None) or _EcranFactice("DISPLAY1", **kw)
    return ScreenAssignment(screen=ecran, role=role, index=0)


# ── énumérations ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role,attendu", [
    (WindowRole.PANEL, "panel"),
    (WindowRole.FULLSCREEN, "fullscreen"),
    (WindowRole.GRID, "grid"),
])
def test_les_roles_gardent_le_nom_ecrit_dans_la_config(role, attendu):
    """`core.monitors._apply_screen_config` compare ces chaînes littéralement."""
    assert role.value == attendu


def test_aucun_role_supplementaire_sans_lecture_correspondante():
    """Un quatrième rôle ajouté ici serait ignoré à la relecture de la config.

    `_apply_screen_config` traite « panel », « fullscreen » et « grid », et
    range tout le reste en « disabled ».
    """
    assert {r.value for r in WindowRole} == {"panel", "fullscreen", "grid"}


def test_les_trois_modes_d_affichage_sont_distincts():
    """`auto()` numérote ; deux modes égaux enverraient main.py sur la mauvaise
    branche de câblage des fenêtres."""
    assert len({m.value for m in DisplayMode}) == len(list(DisplayMode)) == 3


# ── ScreenAssignment ─────────────────────────────────────────────────────────

def test_le_nom_vient_de_l_ecran_et_non_d_une_copie():
    """Le nom sert d'étiquette dans l'assistant écrans : il doit rester à jour."""
    ecran = _EcranFactice("DISPLAY2")
    a = _assignment(ecran=ecran)
    assert a.name == "DISPLAY2"


@pytest.mark.parametrize("largeur,hauteur,attendu", [
    (1920, 1080, "1920x1080"),
    (3840, 2160, "3840x2160"),
    (1280, 1024, "1280x1024"),
])
def test_la_resolution_est_lisible_largeur_x_hauteur(largeur, hauteur, attendu):
    assert _assignment(largeur=largeur, hauteur=hauteur).resolution == attendu


def test_la_position_est_le_coin_haut_gauche_du_bureau_virtuel():
    """Un écran secondaire à droite a un x positif, un écran à gauche un x
    négatif : c'est ce couple qui ordonne les écrans dans l'assistant."""
    assert _assignment(x=-1920, y=200).position == (-1920, 200)


# ── DisplayLayout ────────────────────────────────────────────────────────────

def test_le_layout_retrouve_l_ecran_d_un_role():
    panel = _assignment(WindowRole.PANEL)
    plein = _assignment(WindowRole.FULLSCREEN)
    layout = DisplayLayout(mode=DisplayMode.DUAL, assignments=[panel, plein])
    assert layout.get_screen(WindowRole.FULLSCREEN) is plein
    assert layout.get_screen(WindowRole.PANEL) is panel


def test_un_role_absent_rend_none_plutot_que_de_lever():
    """En mode SINGLE il n'y a pas de fenêtre panel : main.py teste le None."""
    layout = DisplayLayout(mode=DisplayMode.SINGLE,
                           assignments=[_assignment(WindowRole.FULLSCREEN)])
    assert layout.get_screen(WindowRole.PANEL) is None


def test_le_premier_assignment_gagne_en_cas_de_doublon():
    """En mode DUAL, panel et grille partagent le même écran physique.

    `core.monitors` peut donc produire deux entrées pour un même rôle ; la
    règle retenue est la première, pas la dernière.
    """
    premier = _assignment(WindowRole.GRID, ecran=_EcranFactice("A"))
    second = _assignment(WindowRole.GRID, ecran=_EcranFactice("B"))
    layout = DisplayLayout(mode=DisplayMode.DUAL,
                           assignments=[premier, second])
    assert layout.get_screen(WindowRole.GRID).name == "A"


def test_deux_layouts_ne_partagent_pas_la_meme_liste():
    """Le piège classique de la liste en valeur par défaut : sans
    `default_factory`, tout layout créé sans assignments hériterait des écrans
    du précédent."""
    a = DisplayLayout(mode=DisplayMode.SINGLE)
    b = DisplayLayout(mode=DisplayMode.TRIPLE)
    a.assignments.append(_assignment())
    assert b.assignments == []
