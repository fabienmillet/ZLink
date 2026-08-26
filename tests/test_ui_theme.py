# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Apparence des composants que Qt crée pour son compte.

Le module ne sert qu'à une chose : empêcher le noir sur noir des menus,
infobulles et listes déroulantes sous un thème de bureau qui mélange fonds
sombres et texte noir. Les tests vérifient donc les couleurs posées, mais
surtout la propriété qui compte — chaque texte reste lisible sur son fond.
"""

from __future__ import annotations

import re

import pytest
from PyQt6.QtGui import QColor, QPalette

from core.ui_theme import MENU_QSS, apply_dark_palette

ROLE = QPalette.ColorRole
GROUPE = QPalette.ColorGroup


@pytest.fixture
def palette_posee(qapp):
    """Applique la palette sombre, puis rend à l'application la sienne.

    `setPalette` vaut pour toute l'application : sans restauration, un test
    ultérieur hériterait de ce thème.
    """
    origine = qapp.palette()
    apply_dark_palette(qapp)
    yield qapp.palette()
    qapp.setPalette(origine)


# ── couleurs posées ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("role,attendu", [
    (ROLE.Window, "#111111"),
    (ROLE.WindowText, "#e0e0e0"),
    (ROLE.Base, "#0c0c0c"),
    (ROLE.AlternateBase, "#111111"),
    (ROLE.Text, "#e0e0e0"),
    (ROLE.Button, "#111111"),
    (ROLE.ButtonText, "#e0e0e0"),
    (ROLE.ToolTipBase, "#1a1a1a"),
    (ROLE.ToolTipText, "#e0e0e0"),
    (ROLE.Highlight, "#00ff87"),
    (ROLE.HighlightedText, "#08130d"),
    (ROLE.Link, "#00ff87"),
    (ROLE.PlaceholderText, "#6a6a6a"),
])
def test_les_couleurs_de_base_sont_posees(palette_posee, role, attendu):
    assert palette_posee.color(role) == QColor(attendu)


@pytest.mark.parametrize("role,attendu", [
    (ROLE.Light, "#2a2a2a"), (ROLE.Midlight, "#222222"),
    (ROLE.Mid, "#1a1a1a"), (ROLE.Dark, "#0a0a0a"),
    (ROLE.Shadow, "#000000"), (ROLE.BrightText, "#ffffff"),
])
def test_les_nuances_de_relief_sont_sombres(palette_posee, role, attendu):
    """Laissées aux valeurs claires par défaut, elles dessinaient des contours
    blancs autour des contrôles sombres."""
    assert palette_posee.color(role) == QColor(attendu)


@pytest.mark.parametrize("role", [ROLE.WindowText, ROLE.Text, ROLE.ButtonText])
def test_le_texte_desactive_se_distingue_du_texte_actif(palette_posee, role):
    """Sinon rien ne signale qu'un contrôle est hors service."""
    desactive = palette_posee.color(GROUPE.Disabled, role)
    assert desactive == QColor("#6a6a6a")
    assert desactive != palette_posee.color(GROUPE.Active, role)


# ── lisibilité ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("texte,fond", [
    (ROLE.WindowText, ROLE.Window),
    (ROLE.Text, ROLE.Base),
    (ROLE.Text, ROLE.AlternateBase),
    (ROLE.ButtonText, ROLE.Button),
    (ROLE.ToolTipText, ROLE.ToolTipBase),
    (ROLE.HighlightedText, ROLE.Highlight),
    (ROLE.PlaceholderText, ROLE.Window),
])
def test_aucun_texte_n_est_de_la_couleur_de_son_fond(palette_posee, texte, fond):
    """Le noir sur noir est exactement le défaut que ce module corrige.

    On exige un écart de luminosité franc, et pas seulement deux couleurs
    différentes : #101010 sur #111111 serait tout aussi illisible.
    """
    ecart = abs(palette_posee.color(texte).lightnessF()
                - palette_posee.color(fond).lightnessF())
    assert ecart > 0.3


def test_le_lien_reste_visible_sur_le_fond(palette_posee):
    ecart = abs(palette_posee.color(ROLE.Link).lightnessF()
                - palette_posee.color(ROLE.Window).lightnessF())
    assert ecart > 0.3


def test_la_palette_ne_depend_d_aucun_theme_installe(qapp):
    """Deux applications successives donnent la même palette : les couleurs
    sont écrites en dur, elles ne sont pas lues sur le bureau."""
    origine = qapp.palette()
    try:
        apply_dark_palette(qapp)
        premiere = qapp.palette()
        apply_dark_palette(qapp)
        assert qapp.palette().color(ROLE.Window) == premiere.color(ROLE.Window)
        assert qapp.palette().color(ROLE.Text) == premiere.color(ROLE.Text)
    finally:
        qapp.setPalette(origine)


def test_apply_dark_palette_pose_une_seule_palette():
    """La fonction ne demande qu'un `setPalette` : elle peut viser autre chose
    que QApplication, un sous-arbre de widgets par exemple."""
    posees: list = []
    apply_dark_palette(type("_FausseCible", (), {
        "setPalette": lambda _self, pal: posees.append(pal)})())
    assert len(posees) == 1
    assert isinstance(posees[0], QPalette)


# ── feuille de style des menus ───────────────────────────────────────────────

@pytest.mark.parametrize("selecteur", [
    "QMenu {", "QMenu::item {", "QMenu::item:selected {", "QMenu::separator {",
])
def test_la_feuille_couvre_les_quatre_etats_du_menu(selecteur):
    """Un sélecteur oublié retombe sur le thème du bureau, donc sur le bug."""
    assert selecteur in MENU_QSS


def test_toutes_les_couleurs_de_la_feuille_sont_valides(qapp):
    """Une couleur mal écrite est ignorée en silence par Qt."""
    couleurs = re.findall(r"#[0-9a-fA-F]{3,6}\b", MENU_QSS)
    assert couleurs, "feuille sans couleur : le menu retomberait sur le thème"
    assert all(QColor(c).isValid() for c in couleurs)


def test_le_texte_du_menu_tranche_sur_son_fond():
    """Le menu contextuel de la grille était l'exemple du noir sur noir."""
    fond = re.search(r"QMenu \{[^}]*background: (#[0-9a-fA-F]+)", MENU_QSS)
    texte = re.search(r"QMenu \{[^}]*color: (#[0-9a-fA-F]+)", MENU_QSS)
    assert fond and texte
    ecart = abs(QColor(fond.group(1)).lightnessF()
                - QColor(texte.group(1)).lightnessF())
    assert ecart > 0.3


def test_l_entree_survolee_se_distingue_des_autres():
    """Sans quoi on ne verrait pas ce qu'on s'apprête à choisir."""
    normal = re.search(r"QMenu \{[^}]*background: (#[0-9a-fA-F]+)", MENU_QSS)
    survol = re.search(r"QMenu::item:selected \{[^}]*background: (#[0-9a-fA-F]+)",
                       MENU_QSS)
    assert normal and survol
    assert QColor(normal.group(1)) != QColor(survol.group(1))


def test_la_feuille_s_applique_a_un_vrai_menu(qapp):
    """Un QSS invalide serait rejeté par Qt sans que la chaîne change : on
    vérifie donc l'application, pas seulement le texte."""
    from PyQt6.QtWidgets import QMenu

    menu = QMenu()
    try:
        menu.setStyleSheet(MENU_QSS)
        menu.addAction("Ouvrir")
        assert menu.styleSheet() == MENU_QSS
    finally:
        menu.deleteLater()
