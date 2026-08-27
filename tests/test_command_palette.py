# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Palette de commandes — la recherche au clavier.

Elle vivait dans le panel, et Ctrl+K n'y répondait donc que là. C'est pourtant
en plein écran ou devant la grille qu'on veut changer de chaîne sans lâcher le
clavier. Ces tests fixent les deux choses qui comptent : ce qu'elle remonte pour
une recherche donnée, et le fait qu'elle n'émette que le choix, sans rien savoir
de la fenêtre qui l'héberge.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget

from widgets.command_palette import CommandPalette


class _FauxStreamer:
    def __init__(self, login: str, viewers: int = 0, online: bool = True,
                 game: str = "") -> None:
        self.twitch_login = login
        self.display = login
        self.viewers = viewers
        self.online = online
        self.game = game
        self.profile_url = ""


@pytest.fixture
def palette(qtbot):
    parent = QWidget()
    parent.resize(800, 600)
    qtbot.addWidget(parent)
    p = CommandPalette(parent, ["Accueil", "Stats"])
    # Le parent est local : sans cette référence, Python le collecte à la
    # sortie de la fixture et Qt détruit la palette avec lui.
    p._parent_du_test = parent
    p.set_streamers([
        _FauxStreamer("zerator", 5000, game="Just Chatting"),
        _FauxStreamer("domingo", 3000),
        _FauxStreamer("horty", 0, online=False),
    ])
    return p


def _cles(palette) -> list[str]:
    return [cle for _kind, cle, _lbl in palette._results]


# ── filtrage ─────────────────────────────────────────────────────────────────

def test_a_vide_seuls_les_streamers_remontent(qtbot):
    """Neuf fois sur dix on cherche une chaîne : les onglets encombreraient."""
    parent = QWidget()
    qtbot.addWidget(parent)
    p = CommandPalette(parent, ["Accueil", "Stats"])
    p._parent_du_test = parent
    p.set_streamers([_FauxStreamer("zerator", 10)])
    p._refilter("")
    assert [k for kind, k, _ in p._results if kind == "tab"] == []
    assert "zerator" in _cles(p)


def test_une_recherche_explicite_remonte_les_onglets(palette):
    palette._refilter("stat")
    assert ("tab", "Stats", "Onglet · Stats") in palette._results


def test_les_actions_sont_atteignables_par_leur_libelle(palette):
    palette._refilter("moment")
    assert ("action", "clip") in [(k, c) for k, c, _ in palette._results]


def test_une_recherche_sans_resultat_ne_leve_pas(palette):
    palette._refilter("xyzzy")
    assert palette._results == []


def test_la_liste_est_plafonnee(qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)
    p = CommandPalette(parent, [])
    p._parent_du_test = parent
    p.set_streamers([_FauxStreamer(f"s{i}", 100 - i) for i in range(40)])
    p._refilter("s")
    assert len(p._results) <= p._MAX_RESULTS


# ── activation ───────────────────────────────────────────────────────────────

def test_entree_ouvre_le_flux(palette):
    recus: list[str] = []
    palette.stream_requested.connect(recus.append)
    palette._refilter("zera")
    palette._list.setCurrentRow(0)
    palette._activate()
    assert recus == ["zerator"]


def test_ctrl_entree_ajoute_a_la_grille(palette):
    """Deux gestes distincts sur le même résultat : regarder, ou ajouter."""
    recus: list[str] = []
    palette.grid_requested.connect(recus.append)
    palette._refilter("zera")
    palette._list.setCurrentRow(0)
    palette._activate(to_grid=True)
    assert recus == ["zerator"]


def test_un_onglet_n_est_jamais_ajoute_a_la_grille(palette):
    """Ctrl+Entrée sur un onglet reste une demande d'onglet."""
    onglets: list[str] = []
    grille: list[str] = []
    palette.tab_requested.connect(onglets.append)
    palette.grid_requested.connect(grille.append)
    palette._refilter("stat")
    palette._list.setCurrentRow(0)
    palette._activate(to_grid=True)
    assert onglets == ["Stats"] and grille == []


def test_activer_sans_selection_ne_leve_pas(palette):
    palette._results = []
    palette._activate()      # ne doit rien émettre, ni lever


def test_l_activation_referme_la_palette(palette):
    palette._refilter("zera")
    palette._list.setCurrentRow(0)
    palette.show()
    palette._activate()
    assert not palette.isVisible()


# ── présentation ─────────────────────────────────────────────────────────────

def test_les_streamers_portent_leur_photo(palette):
    """Un pseudo seul demande de le connaître ; la photo se lit d'un coup."""
    palette._refilter("zera")
    item = palette._list.item(0)
    assert not item.icon().isNull() or item.icon().availableSizes() == [], \
        "une icône est posée, même vide en attendant l'image"
    assert palette._results[0][0] == "streamer"


def test_un_onglet_n_a_pas_de_photo(palette):
    palette._refilter("stat")
    kinds = [k for k, _c, _l in palette._results]
    assert "tab" in kinds


def test_la_boite_suit_le_nombre_de_resultats(palette):
    """Neuf lignes de haut pour deux résultats laissait un grand vide."""
    palette._refilter("zera")
    court = palette._list.height()
    palette._refilter("")
    long = palette._list.height()
    assert court < long


def test_la_palette_ne_recouvre_pas_toute_la_fenetre(palette):
    """Le voile plein écran tournait au noir opaque au-dessus de la vidéo.

    Elle est une fenêtre NATIVE : Qt ne peut pas composer par-dessus.
    """
    palette.open()
    parent = palette.parentWidget()
    assert palette.width() < parent.width()
    assert palette.height() < parent.height()


def test_une_photo_qui_arrive_apres_coup_repeint_les_lignes(palette):
    """La palette se referme en quelques secondes : attendre le prochain
    rafraîchissement des données serait trop tard."""
    palette._refilter("zera")
    palette.show()
    palette._redessiner()      # ne doit pas lever, ni vider la liste
    assert palette._list.count() == len(palette._results)


def test_redessiner_une_palette_fermee_ne_fait_rien(palette):
    palette._refilter("zera")
    palette.hide()
    palette._redessiner()


# ── Le raccourci lui-même ────────────────────────────────────────────────────

def test_la_palette_garde_un_fond_opacifiable_et_non_translucide(palette):
    """La distinction n'est pas cosmétique.

    La vidéo est une fenêtre NATIVE posée par-dessus le rendu Qt. Qt la découpe
    sous les widgets frères qui la recouvrent — mais un widget déclaré
    translucide n'a rien à découper : la vidéo restait devant, et la palette
    n'apparaissait qu'au panel, seule fenêtre sans vidéo.
    """
    assert palette.testAttribute(Qt.WidgetAttribute.WA_StyledBackground)
    assert not palette.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert "rgba(" in palette.styleSheet(), "un fond, mais pas opaque"


def test_le_fond_ne_deteint_pas_sur_les_enfants(palette):
    """Le sélecteur porte le nom d'objet : sans lui, la règle descendrait sur
    la ligne de saisie et la liste."""
    assert palette.objectName() == "commandPalette"
    assert "#commandPalette" in palette.styleSheet()


# ── la boîte suit son contenu ────────────────────────────────────────────────

def _STREAMERS_TEST():
    """Assez de chaînes pour que la liste atteigne son plafond."""
    return [_FauxStreamer(f"zed{i}", 100 - i) for i in range(12)] + [
        _FauxStreamer("artemize", 0, online=False)]


@pytest.fixture
def palette_peuplee(palette):
    """La palette garnie, sur un hôte AFFICHÉ.

    Qt n'applique la géométrie aux enfants d'une fenêtre cachée qu'à son
    affichage : sans ce `show`, le cadre resterait haut de trente pixels et
    toute mesure de débordement porterait sur du vide.
    """
    palette._parent_du_test.show()
    palette.set_streamers(_STREAMERS_TEST())
    return palette


def _boite_et_aide(palette):
    boite = palette._boite
    return boite, boite.layout().itemAt(2).widget()


def test_la_hauteur_suit_le_nombre_de_resultats(palette_peuplee):
    """La palette gardait la taille de son PREMIER affichage.

    `setFixedHeight` sur la liste marque la mise en page à refaire, mais Qt ne
    la refait qu'au traitement d'un événement posté : `adjustSize`, appelé
    dans la foulée, lisait l'ancienne taille.
    """
    p = palette_peuplee
    p.open()
    large = p.height()
    p._input.setText("zed1")
    etroite = p.height()
    assert etroite < large, "la palette doit se resserrer sur peu de résultats"
    p._input.setText("")
    assert p.height() == large, "et se rouvrir quand ils reviennent"


def test_rien_ne_deborde_de_la_palette(palette_peuplee):
    """Les lignes se peignaient par-dessus la ligne d'aide."""
    p = palette_peuplee
    p.open()
    for texte in ("", "zed", "zed1", "", "artem", "ze"):
        p._input.setText(texte)
        boite, aide = _boite_et_aide(p)
        bas = boite.y() + aide.y() + aide.height()
        assert bas <= p.height(), f"débordement de {bas - p.height()} px sur {texte!r}"


def test_aucun_vide_au_dessus_du_champ(palette_peuplee):
    """La boîte flottait au milieu d'une palette restée trop haute."""
    p = palette_peuplee
    p.open()
    for texte in ("zed1", "artem", ""):
        p._input.setText(texte)
        assert p._boite.y() == 0, f"{p._boite.y()} px de vide sur {texte!r}"


def test_une_palette_ouverte_sans_streamers_s_agrandit_ensuite(palette_peuplee):
    """Le cas réel : Ctrl+K avant que le premier sondage ait répondu.

    La palette se figeait alors à sa taille minimale, et les résultats
    suivants débordaient par le bas.
    """
    p = palette_peuplee
    p.set_streamers([])
    p.open()
    minuscule = p.height()
    p.set_streamers(_STREAMERS_TEST())
    p._input.setText("zed")
    assert p.height() > minuscule
    boite, aide = _boite_et_aide(p)
    assert boite.y() + aide.y() + aide.height() <= p.height()
