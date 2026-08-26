# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Le schéma des moniteurs et l'attribution des rôles.

Ce widget décide de la disposition des fenêtres AVANT qu'aucune ne soit
créée : une erreur ici ne se rattrape qu'au redémarrage. Il sert à deux
endroits — l'assistant et les réglages — et une régression se paierait donc
deux fois.

Ces tests visent la logique : géométrie, attribution, ce que le menu propose
et ce qu'il interdit. Le dessin n'est vérifié que sur un point — qu'il ne
lève pas.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, QSize, Qt
from PyQt6.QtGui import QMouseEvent, QResizeEvent

from widgets import screen_picker as SP


# ── outils ───────────────────────────────────────────────────────────────────

def _clic(widget, point, bouton=Qt.MouseButton.LeftButton) -> None:
    """Relâchement de bouton sur `point`, sans passer par une vraie fenêtre."""
    pos = QPointF(point)
    widget.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, pos, pos, bouton,
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
    ))


def _picker(geos, largeur=600, hauteur=300):
    """Sélecteur dimensionné, ses rectangles déjà calculés."""
    p = SP.ScreenPicker(geos)
    p.resize(largeur, hauteur)
    p._compute()
    return p


#: Deux écrans côte à côte, le second à droite du premier.
_DEUX = [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)]

#: Trois écrans alignés.
_TROIS = [(i * 1920, 0, 1920, 1080) for i in range(3)]


# ── plan par défaut ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("nb,attendu", [
    (1, {"0": "fullscreen"}),
    (2, {"0": "panel", "1": "fullscreen"}),
    (3, {"0": "panel", "1": "fullscreen", "2": "grid"}),
])
def test_roles_attribues_selon_le_nombre_d_ecrans(qapp, nb, attendu):
    p = SP.ScreenPicker([(i * 1920, 0, 1920, 1080) for i in range(nb)])
    assert p.assignments() == attendu


def test_au_dela_de_trois_ecrans_les_suivants_restent_libres(qapp):
    """Il n'y a que trois vues : un quatrième moniteur n'a rien à recevoir."""
    p = SP.ScreenPicker([(i * 100, 0, 100, 100) for i in range(5)])
    assert p.roles() == ["panel", "fullscreen", "grid", "", ""]
    assert p.enabled_indexes() == [0, 1, 2]


def test_le_plan_par_defaut_suit_la_disposition_physique(qapp):
    """L'ordre système n'est pas l'ordre à l'écran : c'est le second qui compte."""
    p = SP.ScreenPicker([(1000, 0, 800, 600), (-500, 0, 800, 600),
                         (200, 0, 800, 600)])
    assert p.roles() == ["grid", "panel", "fullscreen"]
    assert p.enabled_indexes() == [1, 2, 0]


def test_aucune_geometrie_donne_un_ecran_par_defaut(qapp):
    """Sans écran détecté, le sélecteur doit rester utilisable."""
    p = SP.ScreenPicker([])
    assert p._geos == [(0, 0, 1920, 1080)]
    assert p.assignments() == {"0": "fullscreen"}


def test_un_ecran_sans_role_est_absent_des_assignments(qapp):
    """Un écran inutilisé s'écrit par son ABSENCE, pas par un rôle sentinelle."""
    p = SP.ScreenPicker(_TROIS)
    p._roles = ["fullscreen", "", ""]
    assert p.assignments() == {"0": "fullscreen"}
    assert p.enabled_indexes() == [0]


# ── restauration d'une attribution enregistrée ───────────────────────────────

def test_une_attribution_enregistree_est_reprise_telle_quelle(qapp):
    p = SP.ScreenPicker(_TROIS)
    p.definir_assignments({"0": "grid", "1": "fullscreen", "2": "panel"})
    assert p.roles() == ["grid", "fullscreen", "panel"]


@pytest.mark.parametrize("enregistre", [
    {},                          # rien de retenu
    {"0": "disabled"},           # ancien rôle sentinelle
    {"0": ""},                   # rôle vide
    {"abc": "panel"},            # clé qui n'est pas un indice
    {"0": "rôle inventé"},       # config.json édité à la main
    {"9": "panel"},              # écran débranché depuis
    "pas un objet",              # fichier corrompu
])
def test_une_attribution_inexploitable_laisse_le_plan_par_defaut(
        qapp, enregistre):
    p = SP.ScreenPicker(_DEUX)
    p.definir_assignments(enregistre)
    assert p.roles() == ["panel", "fullscreen"]


def test_un_role_donne_deux_fois_n_est_retenu_qu_une(qapp):
    """Deux plein écrans ouvriraient deux directs pour un seul lecteur."""
    p = SP.ScreenPicker(_DEUX)
    p.definir_assignments({"0": "fullscreen", "1": "fullscreen"})
    assert p.roles() == ["fullscreen", ""]


def test_une_attribution_sans_plein_ecran_s_en_voit_donner_un(qapp):
    """Le reste du choix était valable : le jeter serait pire que le réparer."""
    p = SP.ScreenPicker(_TROIS)
    p.definir_assignments({"0": "grid"})
    # L'écran 2 prend le direct, puis le 3 le panel : la grille ne va pas
    # sans lui, et il restait un écran libre pour le porter.
    assert p.roles() == ["grid", "fullscreen", "panel"]


def test_sans_ecran_libre_la_reparation_prend_le_premier(qapp):
    """Il faut bien que quelqu'un cède : c'est l'écran de gauche."""
    p = SP.ScreenPicker(_DEUX)
    p.definir_assignments({"0": "panel", "1": "grid"})
    # Le direct s'impose à gauche, faute d'écran libre. La grille se retrouve
    # alors sans panel, et personne ne peut le porter : c'est elle qui cède.
    assert p.roles() == ["fullscreen", ""]


def test_une_grille_orpheline_recoit_un_panel_s_il_reste_un_ecran(qapp):
    """config.json écrit à la main peut demander direct + grille sans panel."""
    p = SP.ScreenPicker(_TROIS)
    p.definir_assignments({"0": "fullscreen", "1": "grid"})
    assert p.roles() == ["fullscreen", "grid", "panel"]


# ── attribution ──────────────────────────────────────────────────────────────

def test_donner_un_role_libre_ne_touche_a_personne(qapp):
    p = SP.ScreenPicker([(i * 100, 0, 100, 100) for i in range(4)])
    assert p.attribuer(3, "") is False, "il n'avait déjà pas de rôle"
    p._roles = ["panel", "fullscreen", "", ""]
    assert p.attribuer(2, "grid") is True
    assert p.roles() == ["panel", "fullscreen", "grid", ""]


def test_donner_un_role_deja_pris_echange_les_deux_ecrans(qapp):
    """Sans échange, l'écran dépossédé perdrait son rôle sans qu'on l'ait voulu."""
    p = SP.ScreenPicker(_TROIS)                # panel, fullscreen, grid
    assert p.attribuer(2, "fullscreen") is True
    assert p.roles() == ["panel", "grid", "fullscreen"]


def test_un_ecran_sans_role_qui_prend_un_role_le_retire_a_l_autre(qapp):
    p = SP.ScreenPicker([(i * 100, 0, 100, 100) for i in range(4)])
    assert p.attribuer(3, "panel") is True
    assert p.roles() == ["", "fullscreen", "grid", "panel"]


def test_le_plein_ecran_ne_s_eteint_pas(qapp):
    """C'est la seule vue dont l'application ne peut pas se passer."""
    p = SP.ScreenPicker(_TROIS)
    assert p.peut_attribuer(1, "") is False
    assert p.attribuer(1, "") is False
    assert p.roles() == ["panel", "fullscreen", "grid"]


def test_le_plein_ecran_ne_bouge_que_par_echange(qapp):
    """À un seul moniteur, lui donner le panel ferait disparaître le direct."""
    p = SP.ScreenPicker([(0, 0, 1920, 1080)])
    assert p.peut_attribuer(0, "panel") is False
    assert p.attribuer(0, "panel") is False
    assert p.roles() == ["fullscreen"]


def test_le_plein_ecran_se_troque_contre_un_role_tenu_par_un_autre(qapp):
    p = SP.ScreenPicker(_DEUX)                 # panel, fullscreen
    assert p.attribuer(1, "panel") is True
    assert p.roles() == ["fullscreen", "panel"]


def test_redonner_le_meme_role_ne_fait_rien(qapp):
    p = SP.ScreenPicker(_DEUX)
    recu: list[bool] = []
    p.changed.connect(lambda: recu.append(True))
    assert p.attribuer(0, "panel") is False
    assert recu == []


def test_un_indice_hors_des_ecrans_est_refuse(qapp):
    p = SP.ScreenPicker(_DEUX)
    assert p.peut_attribuer(7, "panel") is False
    assert p.attribuer(-1, "panel") is False
    assert p.roles() == ["panel", "fullscreen"]


def test_chaque_attribution_previent_qui_ecoute(qapp):
    p = SP.ScreenPicker(_TROIS)
    recu: list[bool] = []
    p.changed.connect(lambda: recu.append(True))
    p.attribuer(0, "grid")
    p.attribuer(2, "fullscreen")
    assert len(recu) == 2


def test_le_plein_ecran_est_toujours_attribue_apres_n_importe_quel_geste(qapp):
    """La règle qui compte : aucune suite de gestes ne peut supprimer le direct."""
    p = SP.ScreenPicker(_TROIS)
    for index in (0, 1, 2, 1, 0, 2, 2, 1):
        for role in ("", "panel", "grid", "fullscreen"):
            p.attribuer(index, role)
            assert "fullscreen" in p.roles()


# ── menu ─────────────────────────────────────────────────────────────────────

def _menu(p, index):
    return {(a.text(), a.isEnabled(), a.isChecked())
            for a in p.construire_menu(index).actions() if not a.isSeparator()}


def test_le_menu_coche_le_role_courant(qapp):
    p = SP.ScreenPicker(_TROIS)
    coches = [a.text() for a in p.construire_menu(1).actions() if a.isChecked()]
    assert coches == [SP.ROLE_LABELS["fullscreen"]]


def test_le_menu_propose_les_quatre_choix(qapp):
    """Masquer un choix impossible ferait croire que le rôle n'existe pas."""
    p = SP.ScreenPicker(_TROIS)
    textes = [a.text() for a in p.construire_menu(0).actions()
              if not a.isSeparator()]
    assert textes == [SP.ROLE_LABELS[r] for r in SP.ROLES] + [
        "Ne pas utiliser cet écran"]


def test_le_menu_grise_ce_qui_est_impossible(qapp):
    """L'écran du direct, seul, ne peut ni s'éteindre ni changer de rôle."""
    p = SP.ScreenPicker([(0, 0, 1920, 1080)])
    assert _menu(p, 0) == {
        (SP.ROLE_LABELS["fullscreen"], True, True),
        (SP.ROLE_LABELS["panel"], False, False),
        (SP.ROLE_LABELS["grid"], False, False),
        ("Ne pas utiliser cet écran", False, False),
    }


def test_le_menu_d_un_ecran_libre_ouvre_tout(qapp):
    p = SP.ScreenPicker([(i * 100, 0, 100, 100) for i in range(4)])
    assert all(active for _t, active, _c in _menu(p, 3))


def test_le_menu_d_un_indice_absent_ne_leve_pas(qapp):
    """Le menu se construit aussi pour les tests et le clavier : pas de piège."""
    p = SP.ScreenPicker(_DEUX)
    assert p.construire_menu(9).actions()


def test_choisir_dans_le_menu_applique_le_role(qapp, monkeypatch):
    """`exec` est bloquant : on lui fait rendre l'entrée voulue."""
    p = SP.ScreenPicker(_TROIS)
    menus: list = []
    vrai = p.construire_menu

    def espion(index):
        menu = vrai(index)
        menus.append(menu)
        return menu

    monkeypatch.setattr(p, "construire_menu", espion)
    monkeypatch.setattr(SP.QMenu, "exec",
                        lambda self, *a: next(x for x in self.actions()
                                              if x.data() == "fullscreen"))
    p.ouvrir_menu(0, QPoint(0, 0))
    assert p.roles() == ["fullscreen", "panel", "grid"]


def test_fermer_le_menu_sans_choisir_ne_change_rien(qapp, monkeypatch):
    p = SP.ScreenPicker(_TROIS)
    monkeypatch.setattr(SP.QMenu, "exec", lambda self, *a: None)
    p.ouvrir_menu(0, QPoint(0, 0))
    assert p.roles() == ["panel", "fullscreen", "grid"]


# ── clics ────────────────────────────────────────────────────────────────────

def test_un_clic_sur_un_ecran_ouvre_son_menu(qapp, monkeypatch):
    p = _picker(_DEUX)
    ouverts: list[int] = []
    monkeypatch.setattr(p, "ouvrir_menu",
                        lambda index, pos: ouverts.append(index))
    _clic(p, p._rects[1].center())
    assert ouverts == [1]


def test_un_clic_hors_des_ecrans_n_ouvre_rien(qapp, monkeypatch):
    p = _picker(_DEUX)
    ouverts: list[int] = []
    monkeypatch.setattr(p, "ouvrir_menu",
                        lambda index, pos: ouverts.append(index))
    _clic(p, QPoint(0, 0))
    assert ouverts == []


def test_le_bouton_droit_est_ignore(qapp, monkeypatch):
    p = _picker(_DEUX)
    ouverts: list[int] = []
    monkeypatch.setattr(p, "ouvrir_menu",
                        lambda index, pos: ouverts.append(index))
    _clic(p, p._rects[0].center(), Qt.MouseButton.RightButton)
    assert ouverts == []


def test_index_a_rend_moins_un_hors_des_ecrans(qapp):
    p = _picker(_DEUX)
    assert p.index_a(p._rects[0].center()) == 0
    assert p.index_a(QPoint(0, 0)) == -1


# ── géométrie ────────────────────────────────────────────────────────────────

def test_une_seule_echelle_pour_les_deux_axes(qapp):
    """Sinon un moniteur vertical apparaîtrait aussi large qu'un horizontal."""
    p = _picker([(0, 0, 1920, 1080), (1920, 0, 1080, 1920)])
    paysage, portrait = p._rects
    assert portrait.width() < paysage.width()
    assert portrait.height() > paysage.height()


def test_les_ecrans_gardent_leur_position_relative(qapp):
    gauche, droite = _picker(_DEUX)._rects
    assert gauche.x() < droite.x()
    assert gauche.y() == droite.y()


def test_le_schema_tient_dans_le_widget(qapp):
    p = _picker(_DEUX, largeur=400, hauteur=200)
    for r in p._rects:
        assert r.x() >= 0 and r.y() >= 0


def test_un_rectangle_minuscule_garde_une_taille_lisible(qapp):
    """Un écran réduit à quelques pixels ne montrerait plus ni numéro ni rôle."""
    r = _picker([(0, 0, 1920, 1080)], largeur=30, hauteur=20)._rects[0]
    assert (r.width(), r.height()) == (52, 38)


def test_le_redimensionnement_recalcule_les_rectangles(qapp):
    p = _picker(_DEUX, largeur=400, hauteur=200)
    avant = list(p._rects)
    p.resize(900, 500)
    p.resizeEvent(QResizeEvent(QSize(900, 500), QSize(400, 200)))
    assert p._rects != avant


# ── rendu ────────────────────────────────────────────────────────────────────

def test_le_schema_se_dessine_sans_lever(qapp):
    """Le sélecteur s'ouvre au tout premier lancement : y planter serait fatal.

    Un rendu hors écran suffit à parcourir les deux états d'un rectangle,
    avec et sans rôle, ainsi que la légende réservée aux grands rectangles.
    """
    p = _picker([(0, 0, 1920, 1080), (1920, 0, 1080, 1920)],
                largeur=800, hauteur=400)
    p._roles = ["fullscreen", ""]
    assert p.grab().size().width() == 800


def test_le_schema_se_dessine_meme_avant_tout_calcul(qapp):
    """Un rendu sur un widget jamais dimensionné doit sortir un schéma."""
    p = SP.ScreenPicker(_DEUX)
    p.grab()
    assert len(p._rects) == 2


def test_un_rectangle_trop_court_se_passe_de_la_definition(qapp):
    """« 1920×1080 » sous le rôle déborderait d'un rectangle de trente pixels."""
    p = _picker(_DEUX, largeur=200, hauteur=60)
    assert all(r.height() < 70 for r in p._rects)
    assert p.grab().size().width() == 200


# ── la grille ne va pas sans le panel ────────────────────────────────────────

def test_le_geste_qui_isolait_la_grille_est_refuse(qapp):
    """Reproduit la manœuvre qui menait à « direct + grille, rien d'autre ».

    Trois écrans, on donne le direct au premier — les rôles s'échangent —
    puis on tente d'éteindre celui qui a hérité du panel. C'est ce dernier
    geste qui produisait une disposition dont main n'ouvrait que le direct.
    """
    p = SP.ScreenPicker(_TROIS)
    assert p.attribuer(0, "fullscreen") is True
    assert p.roles() == ["fullscreen", "panel", "grid"]
    assert p.attribuer(1, "") is False, "éteindre le panel isolerait la grille"
    assert p.roles() == ["fullscreen", "panel", "grid"]


def test_le_panel_ne_s_eteint_pas_tant_qu_une_grille_existe(qapp):
    p = SP.ScreenPicker(_TROIS)
    assert p.peut_attribuer(0, "") is False


def test_le_panel_s_eteint_des_que_la_grille_a_disparu(qapp):
    """La règle vise la grille orpheline, pas le panel en soi."""
    p = SP.ScreenPicker(_TROIS)
    assert p.attribuer(2, "") is True, "la grille, elle, peut partir"
    assert p.attribuer(0, "") is True
    assert p.roles() == ["", "fullscreen", ""]


def test_un_ecran_libre_ne_peut_pas_prendre_la_grille_sans_panel(qapp):
    p = SP.ScreenPicker([(i * 100, 0, 100, 100) for i in range(3)])
    p._roles = ["fullscreen", "", ""]
    assert p.peut_attribuer(1, "grid") is False
    assert p.peut_attribuer(1, "panel") is True


def test_la_grille_devient_possible_une_fois_le_panel_pose(qapp):
    p = SP.ScreenPicker([(i * 100, 0, 100, 100) for i in range(3)])
    p._roles = ["fullscreen", "", ""]
    p.attribuer(1, "panel")
    assert p.attribuer(2, "grid") is True
    assert p.roles() == ["fullscreen", "panel", "grid"]


def test_le_panel_se_deplace_toujours_par_echange(qapp):
    """Interdire de l'éteindre ne doit pas interdire de le changer d'écran."""
    p = SP.ScreenPicker(_TROIS)
    assert p.attribuer(2, "panel") is True
    assert p.roles() == ["grid", "fullscreen", "panel"]


@pytest.mark.parametrize("roles,attendu", [
    (["fullscreen"], True),
    (["panel", "fullscreen"], True),
    (["panel", "fullscreen", "grid"], True),
    (["fullscreen", "grid"], False),      # la grille orpheline
    (["panel", "grid"], False),           # pas de direct
    (["", ""], False),
])
def test_dispositions_tenables(qapp, roles, attendu):
    assert SP.ScreenPicker.tenable(roles) is attendu


def test_aucune_suite_de_gestes_ne_produit_une_disposition_intenable(qapp):
    """La vraie garantie : l'invariant tient quoi qu'on clique."""
    p = SP.ScreenPicker(_TROIS)
    for index in (0, 1, 2, 2, 1, 0, 1, 2, 0):
        for role in ("", "grid", "panel", "fullscreen", ""):
            p.attribuer(index, role)
            assert SP.ScreenPicker.tenable(p.roles()), p.roles()
