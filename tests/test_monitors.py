# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Détection des écrans et attribution des rôles de fenêtres.

Ce module décide, au démarrage, quelle fenêtre s'ouvre sur quel moniteur. Deux
choses doivent tenir :

- l'ordre gauche→droite, car les rôles sont assignés par position et non par
  ordre de branchement — un écran secondaire déclaré en premier par le système
  ne doit pas voler le rôle du panel ;
- la priorité de la configuration utilisateur sur l'auto-détection, avec un
  repli sûr dès que cette configuration est absente, illisible ou incohérente
  (aucun écran en plein écran = aucun flux visible).

Les écrans sont simulés : brancher trois moniteurs sur la machine de test n'est
pas une option, et `ScreenAssignment` ne demande à un écran que `name()` et
`geometry()`.
"""

from __future__ import annotations

import json

import pytest
from PyQt6.QtCore import QRect

from core import monitors
from core.models import DisplayMode, WindowRole


class _EcranFactice:
    """Le strict minimum de l'interface QScreen utilisée par le module."""

    def __init__(self, nom: str, x: int = 0, y: int = 0,
                 largeur: int = 1920, hauteur: int = 1080):
        self._nom = nom
        self._geo = QRect(x, y, largeur, hauteur)

    def name(self) -> str:
        return self._nom

    def geometry(self) -> QRect:
        return self._geo

    def __repr__(self) -> str:
        return f"<Écran {self._nom}>"


class _AppFactice:
    """QApplication réduite à `screens()`, seule méthode appelée ici."""

    def __init__(self, *ecrans: _EcranFactice):
        self._ecrans = list(ecrans)

    def screens(self) -> list[_EcranFactice]:
        # Une copie : detect_screens trie sur place la liste qu'il reçoit.
        return list(self._ecrans)


@pytest.fixture
def sans_config(tmp_path, monkeypatch):
    """Pas de config.json : force le chemin de l'auto-détection.

    `_apply_screen_config` importe CONFIG_PATH à chaque appel depuis
    `core.paths` ; c'est donc là qu'il faut le remplacer.
    """
    import core.paths
    cible = tmp_path / "absente.json"
    monkeypatch.setattr(core.paths, "CONFIG_PATH", cible)
    return cible


@pytest.fixture
def config_ecrans(tmp_path, monkeypatch):
    """Écrit un config.json contenant `screen_assignments`."""
    import core.paths
    cible = tmp_path / "config.json"
    monkeypatch.setattr(core.paths, "CONFIG_PATH", cible)

    def ecrire(contenu):
        cible.write_text(
            contenu if isinstance(contenu, str) else json.dumps(contenu),
            encoding="utf-8",
        )
        return cible

    return ecrire


# ── detect_screens ───────────────────────────────────────────────────────────

def test_les_ecrans_sont_tries_de_gauche_a_droite(sans_config):
    """L'ordre de `QApplication.screens()` suit le système, pas la disposition
    physique : c'est le tri qui garantit que « gauche » veut dire gauche."""
    app = _AppFactice(
        _EcranFactice("centre", x=0),
        _EcranFactice("droite", x=1920),
        _EcranFactice("gauche", x=-1920),
    )
    assert [e.name() for e in monitors.detect_screens(app)] == \
        ["gauche", "centre", "droite"]


def test_un_x_negatif_reste_a_gauche(sans_config):
    """Windows place l'écran secondaire à gauche avec des X négatifs ; un tri
    par valeur absolue ou par nom le mettrait au mauvais endroit."""
    app = _AppFactice(_EcranFactice("principal", x=0),
                      _EcranFactice("secondaire", x=-3840))
    assert monitors.detect_screens(app)[0].name() == "secondaire"


def test_detect_screens_accepte_l_application_reelle(qapp):
    """Garde-fou : la vraie QApplication expose bien ce que le module attend."""
    ecrans = monitors.detect_screens(qapp)
    assert len(ecrans) >= 1
    assert all(hasattr(e, "geometry") and e.name() is not None for e in ecrans)


# ── auto-détection ───────────────────────────────────────────────────────────

def test_un_seul_ecran_donne_le_mode_single(sans_config):
    """Un seul moniteur : seul le plein écran a un sens, la barre latérale
    passera en surimpression."""
    layout = monitors.build_layout(_AppFactice(_EcranFactice("unique")))
    assert layout.mode is DisplayMode.SINGLE
    assert len(layout.assignments) == 1
    assert layout.get_screen(WindowRole.FULLSCREEN).name == "unique"
    assert layout.get_screen(WindowRole.PANEL) is None
    assert layout.get_screen(WindowRole.GRID) is None


def test_deux_ecrans_donnent_le_mode_dual(sans_config):
    """En DUAL, la grille n'a pas d'écran à elle : elle partage celui du panel.
    Si elle atterrissait sur l'écran du plein écran, elle masquerait le flux."""
    app = _AppFactice(_EcranFactice("gauche", x=0), _EcranFactice("droite", x=1920))
    layout = monitors.build_layout(app)
    assert layout.mode is DisplayMode.DUAL
    assert layout.get_screen(WindowRole.PANEL).name == "gauche"
    assert layout.get_screen(WindowRole.FULLSCREEN).name == "droite"
    assert layout.get_screen(WindowRole.GRID).name == "gauche"


def test_trois_ecrans_donnent_le_mode_triple(sans_config):
    app = _AppFactice(_EcranFactice("g", x=0), _EcranFactice("c", x=1920),
                      _EcranFactice("d", x=3840))
    layout = monitors.build_layout(app)
    assert layout.mode is DisplayMode.TRIPLE
    assert [(a.role, a.name, a.index) for a in layout.assignments] == [
        (WindowRole.PANEL, "g", 0),
        (WindowRole.FULLSCREEN, "c", 1),
        (WindowRole.GRID, "d", 2),
    ]


def test_au_dela_de_trois_ecrans_les_surnumeraires_sont_ignores(sans_config):
    """Quatre moniteurs ne doivent pas dérouter la détection : on garde les
    trois premiers de gauche à droite."""
    app = _AppFactice(*[_EcranFactice(f"e{i}", x=i * 1920) for i in range(4)])
    layout = monitors.build_layout(app)
    assert layout.mode is DisplayMode.TRIPLE
    assert len(layout.assignments) == 3
    assert {a.name for a in layout.assignments} == {"e0", "e1", "e2"}


def test_les_roles_suivent_l_ordre_trie_et_non_l_ordre_systeme(sans_config):
    """Le vrai risque de régression : assigner les rôles avant le tri."""
    app = _AppFactice(_EcranFactice("droite", x=3840), _EcranFactice("gauche", x=0),
                      _EcranFactice("centre", x=1920))
    layout = monitors.build_layout(app)
    assert layout.get_screen(WindowRole.PANEL).name == "gauche"
    assert layout.get_screen(WindowRole.FULLSCREEN).name == "centre"
    assert layout.get_screen(WindowRole.GRID).name == "droite"


def test_les_metadonnees_d_assignation_decrivent_l_ecran(sans_config):
    """Ces propriétés servent aux journaux et à l'écran de réglages."""
    app = _AppFactice(_EcranFactice("HDMI-1", x=-1920, y=120,
                                    largeur=2560, hauteur=1440))
    assignation = monitors.build_layout(app).assignments[0]
    assert assignation.name == "HDMI-1"
    assert assignation.resolution == "2560x1440"
    assert assignation.position == (-1920, 120)


# ── configuration utilisateur ────────────────────────────────────────────────

def test_la_config_prime_sur_l_auto_detection(config_ecrans):
    """Toute la raison d'être de `screen_assignments` : l'utilisateur qui a
    inversé ses moniteurs doit pouvoir corriger le placement."""
    config_ecrans({"screen_assignments": {"0": "fullscreen", "1": "panel"}})
    app = _AppFactice(_EcranFactice("gauche", x=0), _EcranFactice("droite", x=1920))
    layout = monitors.build_layout(app)
    # L'auto-détection aurait donné exactement l'inverse.
    assert layout.get_screen(WindowRole.FULLSCREEN).name == "gauche"
    assert layout.get_screen(WindowRole.PANEL).name == "droite"


def test_config_a_trois_roles_donne_le_mode_triple(config_ecrans):
    config_ecrans({"screen_assignments":
                   {"0": "grid", "1": "fullscreen", "2": "panel"}})
    app = _AppFactice(*[_EcranFactice(f"e{i}", x=i * 1920) for i in range(3)])
    layout = monitors.build_layout(app)
    assert layout.mode is DisplayMode.TRIPLE
    assert layout.get_screen(WindowRole.GRID).name == "e0"
    assert layout.get_screen(WindowRole.PANEL).name == "e2"


def test_config_panel_et_fullscreen_donne_dual_avec_grille_sur_le_panel(config_ecrans):
    """Comme en auto-détection : sans écran dédié, la grille rejoint le panel
    plutôt que de recouvrir le flux."""
    config_ecrans({"screen_assignments": {"0": "panel", "1": "fullscreen"}})
    app = _AppFactice(_EcranFactice("gauche", x=0), _EcranFactice("droite", x=1920))
    layout = monitors.build_layout(app)
    assert layout.mode is DisplayMode.DUAL
    grille = layout.get_screen(WindowRole.GRID)
    assert grille.name == "gauche" and grille.index == 0


def test_config_fullscreen_seul_donne_le_mode_single(config_ecrans):
    config_ecrans({"screen_assignments": {"0": "fullscreen", "1": "disabled"}})
    app = _AppFactice(_EcranFactice("gauche", x=0), _EcranFactice("droite", x=1920))
    layout = monitors.build_layout(app)
    assert layout.mode is DisplayMode.SINGLE
    assert len(layout.assignments) == 1


def test_un_ecran_desactive_ne_recoit_aucune_fenetre(config_ecrans):
    """« disabled » doit vraiment laisser le moniteur tranquille — un écran de
    capture ou de régie ne doit rien afficher."""
    config_ecrans({"screen_assignments":
                   {"0": "panel", "1": "fullscreen", "2": "disabled"}})
    app = _AppFactice(*[_EcranFactice(f"e{i}", x=i * 1920) for i in range(3)])
    layout = monitors.build_layout(app)
    assert "e2" not in {a.name for a in layout.assignments}


def test_un_role_inconnu_est_ignore(config_ecrans):
    """Config écrite à la main ou venue d'une version future : ne pas planter."""
    config_ecrans({"screen_assignments": {"0": "fullscreen", "1": "hologramme"}})
    app = _AppFactice(_EcranFactice("gauche", x=0), _EcranFactice("droite", x=1920))
    layout = monitors.build_layout(app)
    assert layout.mode is DisplayMode.SINGLE
    assert {a.name for a in layout.assignments} == {"gauche"}


def test_une_config_sans_ecran_fullscreen_retombe_sur_l_auto_detection(config_ecrans):
    """Le repli le plus important : sans plein écran, aucun flux ne serait
    visible — mieux vaut ignorer la config que démarrer aveugle."""
    config_ecrans({"screen_assignments": {"0": "panel", "1": "grid"}})
    app = _AppFactice(_EcranFactice("gauche", x=0), _EcranFactice("droite", x=1920))
    layout = monitors.build_layout(app)
    assert layout.mode is DisplayMode.DUAL
    assert layout.get_screen(WindowRole.FULLSCREEN).name == "droite"


def test_une_config_visant_des_ecrans_debranches_retombe_sur_l_auto(config_ecrans):
    """Réglée pour trois moniteurs, relancée sur un portable seul : les indices
    1 et 2 n'existent plus, et avec eux le plein écran."""
    config_ecrans({"screen_assignments": {"1": "fullscreen", "2": "grid"}})
    layout = monitors.build_layout(_AppFactice(_EcranFactice("portable")))
    assert layout.mode is DisplayMode.SINGLE
    assert layout.get_screen(WindowRole.FULLSCREEN).name == "portable"


@pytest.mark.parametrize("contenu", [
    "{ ceci nest pas du json",           # fichier tronqué par un plantage
    '["une", "liste"]',                  # racine qui nest pas un objet
    '{"screen_assignments": {}}',        # section présente mais vide
    '{"autre_chose": 1}',                # section absente
    '{"screen_assignments": "panel"}',  # section du mauvais type
])
def test_une_config_inexploitable_ne_bloque_pas_le_demarrage(config_ecrans, contenu):
    """Aucune de ces formes ne doit lever : le démarrage doit toujours aboutir
    à un layout utilisable."""
    config_ecrans(contenu)
    app = _AppFactice(_EcranFactice("gauche", x=0), _EcranFactice("droite", x=1920))
    layout = monitors.build_layout(app)
    assert layout.mode is DisplayMode.DUAL
    assert layout.get_screen(WindowRole.FULLSCREEN) is not None


def test_config_absente_du_disque(sans_config):
    layout = monitors.build_layout(_AppFactice(_EcranFactice("unique")))
    assert layout.mode is DisplayMode.SINGLE


def test_apply_screen_config_rend_none_quand_la_config_ne_sert_a_rien(config_ecrans):
    """Contrat interne : None signifie « repli sur l'auto-détection »."""
    config_ecrans({"screen_assignments": {"0": "panel"}})
    assert monitors._apply_screen_config([_EcranFactice("gauche")]) is None
