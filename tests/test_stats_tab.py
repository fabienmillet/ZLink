# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Onglet Stats — chiffres clés, filtres, tri, barres de proportion.

L'ancienne disposition disait trois fois la même chose : un graphe des top
viewers, un tableau des cagnottes et deux barres LAN/Online. Ces tests fixent
ce que la nouvelle promet : un classement qu'on peut trier et filtrer, dont
chaque colonne porte sa propre échelle de comparaison.

La vue Chart.js est remplacée par une doublure : QtWebEngine ne survit pas à
la plateforme `offscreen`, et les courbes ne sont pas le sujet ici.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QWidget

from windows import panel


class _FausseVueWeb(QWidget):
    """Ce que _StatsTab attend d'une QWebEngineView, et rien de plus."""

    loadFinished = pyqtSignal(bool)

    def setUrl(self, _url) -> None:
        pass

    def page(self):
        return self


class _S:
    """Un StreamerInfo réduit aux champs que l'onglet consulte."""

    def __init__(self, login: str, viewers: int = 0, donation: float = 0.0,
                 location: str = "Online", game: str = "",
                 online: bool = True) -> None:
        self.twitch_login = login
        self.display = login
        self.viewers = viewers
        self.donation = donation
        self.donation_formatted = f"{donation:.0f} €"
        self.location = location
        self.game = game
        self.online = online
        self.profile_url = ""


DONNEES = [
    _S("anyme023", 11900, 0.0, "LAN", "Minecraft"),
    _S("jltomy", 8200, 0.0, "Online", "Just Chatting"),
    _S("mistermv", 5000, 1500.0, "LAN", "TFT"),
    _S("low4n", 617, 0.0, "Online", "Warzone"),
    _S("horty", 0, 0.0, "LAN", "", online=False),
]


@pytest.fixture
def onglet(qtbot, monkeypatch):
    monkeypatch.setattr(panel, "QWebEngineView", _FausseVueWeb)
    o = panel._StatsTab()
    qtbot.addWidget(o)
    o.update_streamers(DONNEES)
    return o


def _noms(onglet) -> list[str]:
    t = onglet._ranking_table
    return [t.item(i, 1).text() for i in range(t.rowCount())]


# ── lieu ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("lieu,attendu", [
    ("LAN", True), ("Ankama", True), ("Villa", True),
    ("Online", False), ("remote", False), ("", False), (None, False),
])
def test_le_lieu_se_lit_par_complement(lieu, attendu):
    """L'API nomme plusieurs lieux physiques selon l'édition.

    Les énumérer un par un vieillirait : on prend le complément de ce qui est
    explicitement distant.
    """
    assert panel._est_sur_place(lieu) is attendu


# ── chiffres clés ────────────────────────────────────────────────────────────

def test_les_tuiles_resument_l_essentiel(onglet):
    assert onglet._t_direct._valeur.text() == "4", "quatre en direct sur cinq"
    assert onglet._t_lieux._valeur.text() == "3 / 2"
    assert "25" in onglet._t_viewers._valeur.text().replace(" ", ""), \
        "11900 + 8200 + 5000 + 617 = 25 717"


def test_les_viewers_hors_ligne_ne_comptent_pas(qtbot, monkeypatch):
    monkeypatch.setattr(panel, "QWebEngineView", _FausseVueWeb)
    o = panel._StatsTab()
    qtbot.addWidget(o)
    o.update_streamers([_S("a", 500, online=False)])
    assert o._t_viewers._valeur.text() == "0"


def test_une_liste_vide_ne_leve_pas(qtbot, monkeypatch):
    monkeypatch.setattr(panel, "QWebEngineView", _FausseVueWeb)
    o = panel._StatsTab()
    qtbot.addWidget(o)
    o.update_streamers([])
    assert o._ranking_table.rowCount() == 0


# ── tri ──────────────────────────────────────────────────────────────────────

def test_le_classement_part_de_la_cagnotte(onglet):
    """C'est un classement de cagnottes : mistermv est le seul à en avoir."""
    assert _noms(onglet)[0] == "mistermv"


def test_a_cagnotte_egale_les_viewers_departagent(onglet):
    """Avant l'event toutes les cagnottes valent zéro.

    Le tri rendait alors l'ordre alphabétique de l'API — illisible.
    """
    assert _noms(onglet)[1:] == ["anyme023", "jltomy", "low4n", "horty"]


def test_un_clic_sur_viewers_trie_par_viewers(onglet):
    onglet._sur_clic_entete(4)
    assert _noms(onglet) == ["anyme023", "jltomy", "mistermv", "low4n", "horty"]


def test_recliquer_la_meme_colonne_inverse_l_ordre(onglet):
    onglet._sur_clic_entete(4)
    onglet._sur_clic_entete(4)
    assert _noms(onglet)[0] == "horty"


def test_un_nom_se_trie_de_a_a_z(onglet):
    """Un nombre se regarde par le haut, un nom se lit dans l'autre sens."""
    onglet._sur_clic_entete(1)
    assert _noms(onglet) == sorted(_noms(onglet))


def test_la_colonne_du_rang_n_est_pas_un_critere(onglet):
    avant = _noms(onglet)
    onglet._sur_clic_entete(0)
    assert _noms(onglet) == avant


def test_une_colonne_hors_bornes_ne_leve_pas(onglet):
    onglet._sur_clic_entete(99)
    onglet._sur_clic_entete(-1)


def test_l_entete_dit_sur_quoi_l_ordre_repose(onglet):
    """Sinon rien n'explique l'ordre affiché."""
    onglet._sur_clic_entete(4)
    titres = [onglet._ranking_table.horizontalHeaderItem(i).text()
              for i in range(onglet._ranking_table.columnCount())]
    assert "Viewers ▼" in titres
    onglet._sur_clic_entete(4)
    titres = [onglet._ranking_table.horizontalHeaderItem(i).text()
              for i in range(onglet._ranking_table.columnCount())]
    assert "Viewers ▲" in titres


def test_le_rang_suit_l_ordre_affiche(onglet):
    """Le rang est une position, pas une propriété du streamer."""
    onglet._sur_clic_entete(4)
    t = onglet._ranking_table
    assert [t.item(i, 0).text() for i in range(t.rowCount())] == \
        ["1", "2", "3", "4", "5"]


# ── filtres ──────────────────────────────────────────────────────────────────

def test_le_filtre_sur_place(onglet):
    onglet._appliquer_filtre("lan")
    assert set(_noms(onglet)) == {"anyme023", "mistermv", "horty"}


def test_le_filtre_a_distance(onglet):
    onglet._appliquer_filtre("remote")
    assert set(_noms(onglet)) == {"jltomy", "low4n"}


def test_le_filtre_revient_a_tous(onglet):
    onglet._appliquer_filtre("lan")
    onglet._appliquer_filtre("tous")
    assert len(_noms(onglet)) == 5


def test_le_compte_affiche_suit_le_filtre(onglet):
    onglet._appliquer_filtre("remote")
    assert onglet._compte_lbl.text() == "· 2"


def test_le_filtre_actif_se_voit(onglet):
    onglet._appliquer_filtre("lan")
    assert "#00ff87" in onglet._boutons_filtre["lan"].styleSheet()
    assert "#00ff87" not in onglet._boutons_filtre["tous"].styleSheet()


# ── barres de proportion ─────────────────────────────────────────────────────

def test_la_barre_compare_au_plus_grand_de_la_selection(onglet):
    """Une échelle globale écrasait les petits derrière les trois gros."""
    t = onglet._ranking_table
    onglet._sur_clic_entete(4)
    parts = [t.item(i, 4).data(panel._BarreDeCellule.PART)
             for i in range(t.rowCount())]
    assert parts[0] == pytest.approx(1.0)
    assert parts[1] == pytest.approx(8200 / 11900)


def test_le_filtre_recalcule_l_echelle(onglet):
    """Filtrer « à distance » retire le plus gros : l'échelle doit suivre."""
    onglet._appliquer_filtre("remote")
    onglet._sur_clic_entete(4)
    t = onglet._ranking_table
    assert t.item(0, 4).data(panel._BarreDeCellule.PART) == pytest.approx(1.0)


def test_un_streamer_hors_ligne_n_a_pas_de_barre_de_viewers(onglet):
    t = onglet._ranking_table
    ligne = _noms(onglet).index("horty")
    assert t.item(ligne, 4).data(panel._BarreDeCellule.PART) == 0.0
    assert t.item(ligne, 4).text() == "—"


# ── cellules ─────────────────────────────────────────────────────────────────

def test_une_cellule_de_nombre_se_compare_par_son_nombre():
    """« 11.9k » est inférieur à « 617 » dans l'ordre alphabétique."""
    grand = panel._CelluleNombre("11.9k", 11900.0)
    petit = panel._CelluleNombre("617", 617.0)
    assert petit < grand
    assert not grand < petit


def test_un_favori_se_repere_dans_trois_cents_lignes(onglet, monkeypatch):
    monkeypatch.setattr(panel.favorites, "is_favorite",
                        lambda lg: lg == "low4n")
    onglet.update_streamers(DONNEES)
    t = onglet._ranking_table
    ligne = _noms(onglet).index("★  low4n")
    assert t.item(ligne, 1).foreground().color().name() == "#f5c518"


def test_un_streamer_hors_ligne_affiche_son_etat(onglet):
    t = onglet._ranking_table
    ligne = _noms(onglet).index("horty")
    assert t.item(ligne, 3).text() == "hors ligne"


def test_les_courbes_restent_repliees_sans_donnees(onglet):
    """420 px d'axes vides mangeaient la moitié de la page hors event."""
    assert onglet._charts_empty.isVisible() or not onglet._charts_view.isVisible()
