# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Les onglets chiffrés du panel : Stats, Goals, Mixer, Accueil.

Ce fichier couvre ce que les trois onglets PROMETTENT, pas la façon dont ils
sont écrits :

- Stats : les colonnes qui ne se trient pas comme leur texte (« 11.9k » avant
  « 617 », « 9 h » avant « 12 h »), et la part d'objectifs qui compare des
  proportions et non des comptes ;
- Goals : la DISTANCE à l'objectif, la sécurité des liens venus d'une API
  communautaire, et l'empreinte qui évite de reconstruire soixante lignes
  identiques toutes les trois secondes ;
- Mixer : les niveaux mémorisés d'une reconstruction à l'autre, et la tranche
  « plein écran » qui doit suivre le flux affiché ;
- Accueil : les quatre chiffres clés et le fil d'événements.

`windows.panel` est importé au chargement du module, donc AVANT que pytest-qt
ne crée la QApplication : il tire QtWebEngine, qui refuse d'être importé après.
La vue Chart.js est ensuite remplacée par une doublure — QtWebEngine ne
survit pas à la plateforme `offscreen`.
"""

from __future__ import annotations

import json
import time

import pytest
from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import (
    QLabel,
    QPushButton,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QTableWidget,
    QWidget,
)

from windows import panel


# ═══════════════════════════════════════════════════════════════════════════
# Doublures
# ═══════════════════════════════════════════════════════════════════════════

class _FausseVueWeb(QWidget):
    """Ce que _StatsTab attend d'une QWebEngineView, et rien de plus.

    Retient aussi les scripts poussés : c'est par là que passent les séries.
    """

    loadFinished = pyqtSignal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.scripts: list[str] = []

    def setUrl(self, _url) -> None:
        pass

    def page(self):
        return self

    def runJavaScript(self, script: str) -> None:
        self.scripts.append(script)


class _S:
    """Un StreamerInfo réduit aux champs que les onglets consultent."""

    def __init__(self, login: str, viewers: int = 0, donation: float = 0.0,
                 location: str = "Online", game: str = "",
                 online: bool = True, display: str = "",
                 participation_id: str = "p-1", gdoc_id: str = "") -> None:
        self.twitch_login = login
        self.display = display or login
        self.viewers = viewers
        self.donation = donation
        self.donation_formatted = f"{donation:.0f} €"
        self.location = location
        self.game = game
        self.online = online
        self.profile_url = ""
        self.participation_id = participation_id
        self.gdoc_id = gdoc_id


class _G:
    """Un objectif de dons, tel que l'onglet Goals le lit."""

    def __init__(self, nom: str, montant: float = 100.0,
                 accompli: bool = False, categorie: str = "",
                 liens: tuple = ()) -> None:
        self.id = nom
        self.name = nom
        self.amount = montant
        self.accomplished = accompli
        self.category = categorie
        self.links = list(liens)


class _ObjectifCompte:
    """Un objectif tel que la COLONNE du classement le compte.

    Elle interroge `done`, quand l'API et l'onglet Goals parlent
    d'`accomplished` — voir le xfail plus bas.
    """

    def __init__(self, done: bool) -> None:
        self.done = done


class _FauxHistorique:
    """Un HistoryStore réduit à ce que les deux onglets lui demandent."""

    def __init__(self, dons=(), viewers=(), rate=None, projection=None,
                 comparaison=None, debut: float = 0.0,
                 fin: float = 4e9, ref_dons=None, ref_viewers=None) -> None:
        self._dons = list(dons)
        self._viewers = list(viewers)
        self._rate = rate
        self._projection = projection
        self._comparaison = comparaison
        self.event_start_ts = debut
        self.event_end_ts = fin
        # Courbes de l'édition précédente, superposées aux graphes. None =
        # aucune référence chargée, le cas le plus courant hors event.
        self._ref_dons = ref_dons
        self._ref_viewers = ref_viewers

    def serie_precedente_alignee(self, ts):
        return list(self._ref_dons) if self._ref_dons else [None] * len(ts)

    def _sur_axe(self, points, ts_axe):
        """Rééchantillonnage du mode « toute la course ».

        La vraie interpolation est éprouvée dans test_history_store : ici on
        veut seulement qu'un axe long produise un axe long.
        """
        if len(points) < 2:
            return [None] * len(ts_axe)
        premier, dernier = points[0][0], points[-1][0]
        return [None if t < premier or t > dernier else points[-1][1]
                for t in ts_axe]

    def serie_courante_sur_axe(self, ts_axe):
        return self._sur_axe(self._dons, ts_axe)

    def serie_viewers_sur_axe(self, ts_axe):
        return self._sur_axe(self._viewers, ts_axe)

    def serie_viewers_precedente_alignee(self, ts):
        return list(self._ref_viewers) if self._ref_viewers else [None] * len(ts)

    def series_editions_alignees(self, ts):
        return {"2025": self.serie_precedente_alignee(ts)}

    def series_viewers_editions_alignees(self, ts):
        return {"2025": self.serie_viewers_precedente_alignee(ts)}

    def get_donation_series(self):
        return ([t for t, _ in self._dons], [v for _, v in self._dons])

    def get_viewers_series(self):
        return ([t for t, _ in self._viewers], [v for _, v in self._viewers])

    def donation_rate(self):
        return self._rate

    def projected_total(self, _fin):
        return self._projection

    def compare_to_previous(self, _total):
        return self._comparaison


def _event(nom: str, debut: float, fin: float, jour: str = "",
           hotes: tuple = (), heure: str = "18:00",
           heure_fin: str = "19:00") -> panel.EventItem:
    """Un show du programme, daté en timestamps pour ne pas dépendre du jour."""
    return panel.EventItem(
        id=nom, name=nom, day=jour, start_local=heure, end_local=heure_fin,
        description="", host_uuids=list(hotes), start_ts=debut, end_ts=fin)


# ═══════════════════════════════════════════════════════════════════════════
# Tuile — le chiffre qu'on lit en premier
# ═══════════════════════════════════════════════════════════════════════════

def test_une_tuile_porte_son_chiffre_et_sa_precision(qtbot):
    """Le détail explique de quoi le nombre est la somme.

    « 25 717 » seul ne dit pas s'il s'agit d'une chaîne ou de toutes.
    """
    t = panel._Tuile("VIEWERS")
    qtbot.addWidget(t)
    t.set_valeur("25 717", "cumulés sur les directs")
    assert t._valeur.text() == "25 717"
    assert t._detail.text() == "cumulés sur les directs"


def test_une_tuile_sans_detail_n_affiche_pas_l_ancien(qtbot):
    """Le détail est effacé, pas conservé : il décrirait un autre chiffre."""
    t = panel._Tuile("VIEWERS")
    qtbot.addWidget(t)
    t.set_valeur("1", "un détail")
    t.set_valeur("2")
    assert t._detail.text() == ""


# ═══════════════════════════════════════════════════════════════════════════
# _BarreDeCellule — la comparaison est DERRIÈRE le nombre, pas à sa place
# ═══════════════════════════════════════════════════════════════════════════

def _peindre_cellule(part, delegue=None) -> QImage:
    """Peint une cellule de 100×20 portant cette part, et rend l'image."""
    table = QTableWidget(1, 1)
    item = panel._CelluleNombre("x", 0.0)
    if part is not None:
        item.setData(panel._BarreDeCellule.PART, part)
    table.setItem(0, 0, item)
    image = QImage(QSize(100, 20), QImage.Format.Format_ARGB32)
    image.fill(QColor("#000000"))
    peintre = QPainter(image)
    option = QStyleOptionViewItem()
    option.rect = image.rect()
    if delegue is None:
        delegue = panel._BarreDeCellule("#38bdf8")
    delegue.paint(peintre, option, table.model().index(0, 0))
    peintre.end()
    return image


def _teintes(part) -> list[bool]:
    """Colonnes où la barre ajoute quelque chose à une cellule ordinaire.

    La référence est la MÊME cellule peinte par le délégué standard : ce qui
    diffère vient de la barre, et de rien d'autre — ni du thème, ni du style.
    """
    reference = _peindre_cellule(None, QStyledItemDelegate())
    image = _peindre_cellule(part)
    ligne = image.height() // 2
    return [image.pixel(x, ligne) != reference.pixel(x, ligne)
            for x in range(image.width())]


def test_la_barre_couvre_la_part_de_la_cellule(qtbot):
    """C'est la LONGUEUR qui compare : la moitié doit occuper la moitié."""
    teintes = _teintes(0.5)
    assert teintes[10] is True
    assert teintes[90] is False


def test_une_part_nulle_ne_peint_aucune_barre(qtbot):
    """Une chaîne hors ligne n'a pas d'audience : pas de repère non plus."""
    assert not any(_teintes(0.0))


def test_une_part_absente_ne_peint_aucune_barre(qtbot):
    """Les colonnes de texte partagent la table ; elles ne posent pas ce rôle."""
    assert not any(_teintes(None))


def test_une_part_illisible_ne_fait_pas_tomber_le_classement(qtbot):
    """Rien ne garantit qu'une cellule range un nombre dans ce rôle."""
    assert not any(_teintes("beaucoup"))


def test_une_part_au_dela_de_un_ne_deborde_pas(qtbot):
    """Un arrondi peut dépasser 1 : la barre reste dans sa cellule."""
    assert all(_teintes(3.0))


def test_la_barre_reste_derriere_le_nombre(qtbot):
    """C'est un repère de grandeur : le texte doit rester le premier lu.

    Une barre opaque le rendrait illisible sur les grosses valeurs.
    """
    assert panel._BarreDeCellule("#38bdf8")._couleur.alpha() == 45


# ═══════════════════════════════════════════════════════════════════════════
# _CelluleNombre — se compare par son nombre
# ═══════════════════════════════════════════════════════════════════════════

def test_une_cellule_de_nombre_se_range_devant_une_cellule_de_texte(qtbot):
    """Toutes les colonnes ne portent pas de nombre : le tri ne doit pas
    lever quand la comparaison sort du lot."""
    from PyQt6.QtWidgets import QTableWidgetItem

    nombre = panel._CelluleNombre("11.9k", 11900.0)
    texte = QTableWidgetItem("zzz")
    assert (nombre < texte) is True


# ═══════════════════════════════════════════════════════════════════════════
# Stats — où les barres de grandeur sont posées
# ═══════════════════════════════════════════════════════════════════════════

def test_les_barres_de_grandeur_sont_sur_les_colonnes_chiffrees(stats):
    """Les barres avaient disparu du classement.

    Elles étaient posées par NUMÉRO de colonne. Les colonnes ajoutées depuis
    — Depuis, Objectifs — ont décalé Viewers et Cagnotte de deux rangs, et les
    barres se peignaient sur des cellules sans valeur à comparer, donc nulle
    part. Ce test tient le lien par constante.
    """
    table = stats._ranking_table
    for colonne in (panel._C_VUE, panel._C_DON):
        assert isinstance(table.itemDelegateForColumn(colonne),
                          panel._BarreDeCellule), f"colonne {colonne} sans barre"


@pytest.mark.parametrize("colonne", ["_C_RANG", "_C_NOM", "_C_LIEU", "_C_JEU",
                                     "_C_DUREE", "_C_OBJ", "_C_TEND"])
def test_les_colonnes_sans_grandeur_n_ont_pas_de_barre(stats, colonne):
    """Une durée ou un nom n'ont pas de proportion : une barre y serait un
    repère faux, pas un repère absent."""
    delegue = stats._ranking_table.itemDelegateForColumn(getattr(panel, colonne))
    assert not isinstance(delegue, panel._BarreDeCellule)


def test_les_deux_colonnes_qui_s_etirent_sont_celles_qui_portent_une_barre(stats):
    """La place en trop devient de la longueur de barre : les deux réglages
    doivent désigner les mêmes colonnes, sinon l'un des deux est à côté."""
    from PyQt6.QtWidgets import QHeaderView

    entete = stats._ranking_table.horizontalHeader()
    etirees = {c for c in range(stats._ranking_table.columnCount())
               if entete.sectionResizeMode(c) == QHeaderView.ResizeMode.Stretch}
    assert etirees == {panel._C_VUE, panel._C_DON}


# ═══════════════════════════════════════════════════════════════════════════
# Stats — colonnes Depuis / Objectifs / +/h
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def stats(qtbot, monkeypatch):
    """Onglet Stats sans graphe réel, avec des directs de durées connues."""
    monkeypatch.setattr(panel, "QWebEngineView", _FausseVueWeb)
    o = panel._StatsTab()
    qtbot.addWidget(o)
    return o


@pytest.fixture
def uptimes(monkeypatch):
    """Durées de direct contrôlées, en secondes, par login."""
    from core import live_uptime

    table: dict[str, float] = {}
    monkeypatch.setattr(live_uptime, "depuis",
                        lambda login, *a, **k: table.get(login))
    return table


@pytest.fixture
def deltas(monkeypatch):
    """Tendances d'audience contrôlées, par login."""
    from core import tendances

    table: dict[str, int] = {}
    monkeypatch.setattr(tendances, "viewers",
                        lambda login, *a, **k: table.get(login))
    return table


def _colonne(onglet, col: int) -> list[str]:
    t = onglet._ranking_table
    return [t.item(i, col).text() for i in range(t.rowCount())]


def _noms(onglet) -> list[str]:
    return _colonne(onglet, panel._C_NOM)


# ── Depuis ───────────────────────────────────────────────────────────────

def test_la_colonne_depuis_annonce_une_duree_pas_une_heure(stats, uptimes):
    """« depuis 3 h 09 » s'écrivait comme trois heures neuf du matin."""
    uptimes["zerator"] = 3 * 3600 + 9 * 60
    stats.update_streamers([_S("zerator")])
    assert _colonne(stats, panel._C_DUREE) == ["3 h 09 min"]


def test_une_chaine_hors_ligne_n_est_en_direct_depuis_rien(stats, uptimes):
    uptimes["horty"] = 9999.0
    stats.update_streamers([_S("horty", online=False)])
    assert _colonne(stats, panel._C_DUREE) == ["—"]


def test_une_duree_inconnue_ne_se_confond_pas_avec_zero(stats, uptimes):
    """Twitch ne répond pas toujours : un tiret dit qu'on ne sait pas."""
    stats.update_streamers([_S("mistermv")])
    assert _colonne(stats, panel._C_DUREE) == ["—"]


def test_le_tri_par_duree_compare_des_secondes(stats, uptimes):
    """« 9 h 05 min » passerait avant « 12 h 00 min » en ordre alphabétique."""
    uptimes.update({"court": 9 * 3600 + 5 * 60, "long": 12 * 3600})
    stats.update_streamers([_S("court"), _S("long")])
    stats._sur_clic_entete(panel._C_DUREE)
    assert _noms(stats) == ["long", "court"]


def test_la_duree_ne_sert_a_trier_que_les_directs(stats, uptimes):
    """Un relevé périmé ferait remonter une chaîne éteinte en tête."""
    uptimes.update({"eteint": 50 * 3600, "allume": 3600})
    stats.update_streamers([_S("eteint", online=False), _S("allume")])
    stats._sur_clic_entete(panel._C_DUREE)
    assert _noms(stats)[0] == "allume"


# ── Objectifs ────────────────────────────────────────────────────────────

def test_la_colonne_objectifs_dit_les_atteints_sur_le_total(stats):
    stats.update_streamers([_S("ponce")])
    stats.seed_goals({"ponce": [_ObjectifCompte(True), _ObjectifCompte(True),
                                _ObjectifCompte(False)]})
    assert _colonne(stats, panel._C_OBJ) == ["2/3"]


def test_une_chaine_sans_objectif_publie_ne_dit_pas_zero_sur_zero(stats):
    """« 0/0 » se lirait comme un échec ; il n'y a rien à afficher."""
    stats.update_streamers([_S("ponce")])
    stats.seed_goals({})
    assert _colonne(stats, panel._C_OBJ) == ["—"]


def test_le_classement_des_objectifs_compare_des_parts_pas_des_comptes(stats):
    """Trois objectifs sur quatre valent mieux que trois sur vingt.

    Un classement qui dit l'inverse ment sur le résultat des chaînes.
    """
    stats.update_streamers([_S("modeste"), _S("prolixe")])
    stats.seed_goals({
        "modeste": [_ObjectifCompte(True)] * 3 + [_ObjectifCompte(False)],
        "prolixe": [_ObjectifCompte(True)] * 3 + [_ObjectifCompte(False)] * 17,
    })
    stats._sur_clic_entete(panel._C_OBJ)
    assert _noms(stats) == ["modeste", "prolixe"]


def test_une_chaine_muette_sur_ses_objectifs_ferme_le_classement(stats):
    """Ne rien publier n'est pas égal à ne rien atteindre : sinon une chaîne
    silencieuse se retrouverait à égalité avec celle qui a tout raté."""
    stats.update_streamers([_S("muette"), _S("rate")])
    stats.seed_goals({"rate": [_ObjectifCompte(False)]})
    stats._sur_clic_entete(panel._C_OBJ)
    assert _noms(stats) == ["rate", "muette"]


def test_semer_les_objectifs_repeint_le_classement(stats):
    """Le prefetch arrive APRÈS la liste : sans repeinte, la colonne resterait
    à « — » sur des objectifs désormais connus."""
    stats.update_streamers([_S("ponce")])
    assert _colonne(stats, panel._C_OBJ) == ["—"]
    stats.seed_goals({"ponce": [_ObjectifCompte(True)]})
    assert _colonne(stats, panel._C_OBJ) == ["1/1"]


def test_semer_les_objectifs_avant_les_streamers_ne_leve_pas(stats):
    """Rien ne garantit l'ordre d'arrivée des deux sources."""
    stats.seed_goals({"ponce": [_ObjectifCompte(True)]})
    assert stats._ranking_table.rowCount() == 0


def test_semer_un_cache_vide_efface_les_objectifs_connus(stats):
    """Le cache reçu fait autorité : garder l'ancien afficherait des comptes
    qui ne correspondent plus à l'édition en cours."""
    stats.update_streamers([_S("ponce")])
    stats.seed_goals({"ponce": [_ObjectifCompte(True)]})
    stats.seed_goals(None)
    assert _colonne(stats, panel._C_OBJ) == ["—"]


def test_un_objectif_accompli_est_compte_comme_atteint(stats):
    """Le cache vient de DataManager, qui y range des DonationGoal."""
    stats.update_streamers([_S("ponce")])
    stats.seed_goals({"ponce": [
        panel.DonationGoal("1", "fait", 100.0, True, "donation"),
        panel.DonationGoal("2", "à faire", 500.0, False, "donation"),
    ]})
    assert _colonne(stats, panel._C_OBJ) == ["1/2"]


# ── +/h ──────────────────────────────────────────────────────────────────

def test_une_chaine_qui_monte_se_voit(stats, deltas):
    """Une chaîne peut être petite et MONTER : c'est ce que le classement par
    audience ne dit jamais, et c'est souvent là qu'il se passe quelque chose."""
    deltas["low4n"] = 1500
    stats.update_streamers([_S("low4n", viewers=600)])
    t = stats._ranking_table
    assert t.item(0, panel._C_TEND).text() == "+1.5k"
    assert t.item(0, panel._C_TEND).foreground().color().name() == "#00ff87"


def test_une_chaine_qui_descend_se_voit_aussi(stats, deltas):
    deltas["low4n"] = -1500
    stats.update_streamers([_S("low4n", viewers=600)])
    t = stats._ranking_table
    assert t.item(0, panel._C_TEND).text() == "-1.5k"
    assert t.item(0, panel._C_TEND).foreground().color().name() == "#ff6b6b"


def test_une_audience_stable_le_dit_sans_signe(stats, deltas):
    deltas["low4n"] = 0
    stats.update_streamers([_S("low4n", viewers=600)])
    assert _colonne(stats, panel._C_TEND) == ["="]


def test_une_tendance_inconnue_reste_muette(stats, deltas):
    """Il faut une heure de relevés : avant, « = » serait un mensonge."""
    stats.update_streamers([_S("low4n", viewers=600)])
    assert _colonne(stats, panel._C_TEND) == ["—"]


def test_le_tri_par_tendance_met_les_hausses_en_tete(stats, deltas):
    deltas.update({"monte": 800, "descend": -400})
    stats.update_streamers([_S("monte"), _S("descend")])
    stats._sur_clic_entete(panel._C_TEND)
    assert _noms(stats) == ["monte", "descend"]


def test_la_tendance_ne_concerne_que_les_directs(stats, deltas):
    deltas["eteint"] = 5000
    stats.update_streamers([_S("eteint", online=False)])
    assert _colonne(stats, panel._C_TEND) == ["—"]


# ── courbes ──────────────────────────────────────────────────────────────

def test_les_courbes_se_deploient_des_le_premier_point(stats):
    """Hors event, 420 px d'axes vides mangeaient la moitié de la page."""
    stats.update_history(_FauxHistorique(dons=[(1_700_000_000.0, 12.0)]))
    assert stats._charts_empty.isHidden()
    assert not stats._charts_view.isHidden()


def test_sans_point_les_courbes_restent_repliees(stats):
    stats.update_history(_FauxHistorique())
    assert not stats._charts_empty.isHidden()
    assert stats._charts_view.isHidden()


def test_une_serie_arrivee_avant_la_page_n_est_pas_perdue(stats):
    """Les données peuvent précéder la fin du chargement : un runJavaScript
    lancé trop tôt n'aurait aucun effet et la série serait perdue."""
    stats.update_history(_FauxHistorique(dons=[(1_700_000_000.0, 12.0)]))
    assert stats._charts_view.scripts == []
    stats._on_charts_loaded(True)
    assert len(stats._charts_view.scripts) == 1
    assert "zlUpdate" in stats._charts_view.scripts[0]


def test_une_page_qui_ne_charge_pas_ne_recoit_rien(stats):
    stats._on_charts_loaded(False)
    stats.update_history(_FauxHistorique(dons=[(1_700_000_000.0, 12.0)]))
    assert stats._charts_view.scripts == []


def test_la_serie_survit_a_un_rechargement_de_la_page(stats):
    """Une page rechargée repart avec des graphes vides : loadFinished doit
    pouvoir repousser la dernière série telle quelle."""
    stats._on_charts_loaded(True)
    stats.update_history(_FauxHistorique(viewers=[(1_700_000_000.0, 42)]))
    stats._on_charts_loaded(True)
    assert len(stats._charts_view.scripts) == 2


# ═══════════════════════════════════════════════════════════════════════════
# Goals — la distance, la sécurité des liens, l'empreinte
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def goals(qtbot, monkeypatch):
    """Onglet Goals sans aucun accès réseau."""
    o = panel._GoalsTab()
    qtbot.addWidget(o)
    monkeypatch.setattr(o, "_do_fetch", lambda *a: None)
    return o


def _lignes(onglet) -> list:
    return onglet.findChildren(panel._LigneObjectif)


def _textes(widget) -> list[str]:
    return [w.text() for w in widget.findChildren(QLabel)]


# ── la ligne d'un objectif ───────────────────────────────────────────────

def test_un_objectif_a_atteindre_montre_ce_qu_il_reste_a_reunir(qtbot):
    """« 559 600 € » ne dit pas si l'objectif tombe dans l'heure ou s'il
    restera lettre morte : c'est la DISTANCE qui intéresse."""
    ligne = panel._LigneObjectif(_G("piment", 1000.0), cagnotte=250.0)
    qtbot.addWidget(ligne)
    assert any("plus que" in t and "750" in t for t in _textes(ligne))


def test_un_objectif_a_portee_le_signale_par_sa_couleur(qtbot):
    """Au-delà de 90 %, on doit le voir avant d'avoir lu le pourcentage."""
    ligne = panel._LigneObjectif(_G("imminent", 1000.0), cagnotte=950.0)
    qtbot.addWidget(ligne)
    distances = [w for w in ligne.findChildren(QLabel) if "%" in w.text()]
    assert distances and "#f5c518" in distances[0].styleSheet()


def test_un_objectif_lointain_reste_discret(qtbot):
    ligne = panel._LigneObjectif(_G("loin", 100_000.0), cagnotte=10.0)
    qtbot.addWidget(ligne)
    distances = [w for w in ligne.findChildren(QLabel) if "%" in w.text()]
    assert distances and "#f5c518" not in distances[0].styleSheet()


def test_un_objectif_accompli_n_affiche_plus_sa_distance(qtbot):
    """Zéro à réunir : la barre et le reste-à-faire n'ont plus rien à dire."""
    ligne = panel._LigneObjectif(_G("fait", 100.0, accompli=True),
                                 cagnotte=500.0)
    qtbot.addWidget(ligne)
    assert ligne.findChildren(panel._BarreObjectif) == []
    assert not any("%" in t for t in _textes(ligne))


def test_un_objectif_accompli_porte_sa_coche(qtbot):
    ligne = panel._LigneObjectif(_G("fait", 100.0, accompli=True), 500.0)
    qtbot.addWidget(ligne)
    assert "✓" in _textes(ligne)


def test_un_objectif_a_venir_porte_son_cercle(qtbot):
    ligne = panel._LigneObjectif(_G("à faire", 100.0), 0.0)
    qtbot.addWidget(ligne)
    assert "○" in _textes(ligne)


def test_un_objectif_sans_nom_reste_designable(qtbot):
    """L'API laisse parfois le nom vide : une ligne muette est inutilisable."""
    ligne = panel._LigneObjectif(_G("", 100.0), 0.0)
    qtbot.addWidget(ligne)
    assert "Objectif sans nom" in _textes(ligne)


def test_le_nom_d_un_objectif_n_est_pas_interprete(qtbot):
    """Ces noms viennent d'une API : Qt rendrait le texte riche autrement."""
    ligne = panel._LigneObjectif(_G("<b>gras</b>", 100.0), 0.0)
    qtbot.addWidget(ligne)
    nom = [w for w in ligne.findChildren(QLabel) if w.text() == "<b>gras</b>"]
    assert nom and nom[0].textFormat() == Qt.TextFormat.PlainText


def test_une_categorie_trop_longue_est_tronquee(qtbot):
    """Elle partage la ligne avec le nom, qui porte l'information utile."""
    lbl = panel._etiquette_categorie("x" * 50)
    qtbot.addWidget(lbl)
    assert len(lbl.text()) == 18


# ── liens ────────────────────────────────────────────────────────────────

def test_seuls_les_liens_surs_d_un_objectif_sont_offerts(qtbot):
    """Ces liens viennent d'une API communautaire et pointent où leurs auteurs
    veulent : un `javascript:` n'a rien à faire dans le système."""
    ligne = panel._LigneObjectif(
        _G("liens", 100.0, liens=("https://ok.test/a", "javascript:alert(1)")),
        0.0)
    qtbot.addWidget(ligne)
    assert len(ligne.findChildren(QPushButton)) == 1


def test_une_ligne_n_offre_pas_plus_de_deux_liens(qtbot):
    """Au-delà, ils chassent le nom de l'objectif hors de la ligne."""
    ligne = panel._LigneObjectif(
        _G("liens", 100.0, liens=tuple(f"https://ok.test/{i}"
                                       for i in range(6))), 0.0)
    qtbot.addWidget(ligne)
    assert len(ligne.findChildren(QPushButton)) == 2


def test_ouvrir_un_lien_cede_d_abord_le_premier_plan(qtbot, monkeypatch):
    """Sans ça le navigateur s'ouvre DERRIÈRE le plein écran de ZLink et le
    clic paraît sans effet."""
    ordre: list[str] = []
    monkeypatch.setattr(panel, "ceder_premier_plan",
                        lambda: ordre.append("cede"))
    monkeypatch.setattr(panel.QDesktopServices, "openUrl",
                        lambda url: ordre.append(f"ouvre:{url.toString()}"))
    panel._ouvrir_lien_objectif("https://ok.test/a")
    assert ordre == ["cede", "ouvre:https://ok.test/a"]


def test_le_bouton_de_lien_ouvre_bien_son_url(qtbot, monkeypatch):
    ouverts: list[str] = []
    monkeypatch.setattr(panel, "_ouvrir_lien_objectif", ouverts.append)
    bouton = panel._bouton_lien("https://ok.test/a")
    qtbot.addWidget(bouton)
    bouton.click()
    assert ouverts == ["https://ok.test/a"]


# ── barre d'objectif ─────────────────────────────────────────────────────

def _couleur_barre(barre, x: int) -> str:
    image = barre.grab().toImage()
    return QColor(image.pixel(x, image.height() // 2)).name()


def test_la_barre_d_objectif_remplit_la_part_atteinte(qtbot):
    barre = panel._BarreObjectif(0.5)
    qtbot.addWidget(barre)
    barre.resize(100, panel._BarreObjectif.HAUTEUR)
    assert _couleur_barre(barre, 10) == "#38bdf8"
    assert _couleur_barre(barre, 90) == "#1c1c1c"


def test_une_barre_a_portee_change_de_couleur(qtbot):
    """Au-delà de 90 %, l'objectif est à portée et la barre le dit."""
    barre = panel._BarreObjectif(0.95)
    qtbot.addWidget(barre)
    barre.resize(100, panel._BarreObjectif.HAUTEUR)
    assert _couleur_barre(barre, 10) == "#f5c518"


def test_une_barre_accomplie_passe_au_vert(qtbot):
    barre = panel._BarreObjectif(1.0, accompli=True)
    qtbot.addWidget(barre)
    barre.resize(100, panel._BarreObjectif.HAUTEUR)
    assert _couleur_barre(barre, 50) == "#00ff87"


def test_une_barre_vide_ne_peint_que_son_fond(qtbot):
    barre = panel._BarreObjectif(0.0)
    qtbot.addWidget(barre)
    barre.resize(100, panel._BarreObjectif.HAUTEUR)
    assert _couleur_barre(barre, 1) == "#1c1c1c"


@pytest.mark.parametrize("demande,attendu", [(-2.0, 0.0), (7.0, 1.0)])
def test_une_barre_d_objectif_reste_dans_ses_bornes(qtbot, demande, attendu):
    """La part est un rapport de montants : rien n'interdit qu'il déborde."""
    barre = panel._BarreObjectif(0.5)
    qtbot.addWidget(barre)
    barre.set_part(demande)
    assert barre._part == attendu


def test_une_barre_change_de_part_sans_etre_remplacee(qtbot):
    """Détruire la barre pour en poser une neuve la détachait de son parent,
    et un widget détaché qui n'a pas été masqué est une fenêtre à l'écran."""
    entete = panel._EnteteStreamer()
    qtbot.addWidget(entete)
    avant = entete._barre
    entete.montrer(_S("a"), [_G("x", 10.0, accompli=True), _G("y", 20.0)])
    entete.montrer(_S("a"), [_G("x", 10.0), _G("y", 20.0)])
    assert entete._barre is avant
    assert entete._barre._part == 0.0


# ── en-tête du streamer ──────────────────────────────────────────────────

@pytest.mark.parametrize("faits,total,attendu", [
    (0, 0, "aucun objectif publié"),
    (0, 5, "0 objectif atteint sur 5"),
    (1, 5, "1 objectif atteint sur 5"),
    (2, 5, "2 objectifs atteints sur 5"),
])
def test_le_compte_d_objectifs_s_accorde(faits, total, attendu):
    """Un compte au pluriel sur un seul objectif se remarque immédiatement."""
    assert panel._EnteteStreamer._compte_objectifs(faits, total) == attendu


def test_l_entete_affiche_la_cagnotte_de_la_chaine(qtbot):
    entete = panel._EnteteStreamer()
    qtbot.addWidget(entete)
    entete.montrer(_S("mistermv", donation=1500.0), [])
    assert "1" in entete._cagnotte.text()
    assert entete._nom.text() == "mistermv"


# ── les deux vues ────────────────────────────────────────────────────────

def test_un_streamer_sans_participation_le_dit(goals):
    """Un participant peut n'avoir aucune participation à cette édition :
    « Chargement… » resterait alors affiché indéfiniment."""
    goals.set_streamers([_S("nouveau", participation_id="")])
    assert any("pas d'objectifs publiés" in t for t in _textes(goals))


def test_un_streamer_inconnu_du_cache_annonce_le_chargement(goals):
    goals.set_streamers([_S("domingo")])
    assert any("Chargement" in t for t in _textes(goals))


def test_la_vue_tous_nomme_la_chaine_de_chaque_objectif(goals):
    """Hors de la fiche d'un streamer, un objectif ne veut rien dire sans
    savoir de qui il est."""
    goals.set_streamers([_S("ponce", donation=500.0, display="Ponce")])
    goals.seed_cache({"ponce": [_G("piment", 1000.0)]})
    goals._changer_vue("tous")
    assert "Ponce" in _textes(goals)


def test_la_vue_du_streamer_ne_repete_pas_son_nom_sur_chaque_ligne(goals):
    """Il est déjà en gros dans l'en-tête juste au-dessus."""
    goals.set_streamers([_S("ponce", donation=500.0, display="Ponce")])
    goals.seed_cache({"ponce": [_G("piment", 1000.0)]})
    assert _textes(_lignes(goals)[0]).count("Ponce") == 0


def test_changer_de_vue_se_voit_sur_les_boutons(goals):
    goals._changer_vue("tous")
    assert "#00ff87" in goals._boutons_vue["tous"].styleSheet()
    assert "#00ff87" not in goals._boutons_vue["streamer"].styleSheet()


def test_la_vue_tous_ignore_un_objectif_sans_montant(goals):
    """Une cible nulle n'a pas de distance : la ligne ne dirait rien."""
    goals.set_streamers([_S("ponce", donation=500.0)])
    goals.seed_cache({"ponce": [_G("sans montant", 0.0)]})
    goals._changer_vue("tous")
    assert _lignes(goals) == []


# ── empreinte : ne pas reconstruire pour rien ────────────────────────────

def test_reafficher_les_memes_objectifs_ne_reconstruit_rien(goals):
    """Le mock réémet ses données toutes les trois secondes et l'application
    toutes les trente : reconstruire soixante lignes identiques ne change rien
    à l'écran et fait ramer la machine."""
    goals.set_streamers([_S("ponce", donation=500.0)])
    goals.seed_cache({"ponce": [_G("piment", 1000.0)]})
    avant = _lignes(goals)
    goals._on_goals_arrived("ponce", [_G("piment", 1000.0)])
    assert _lignes(goals) == avant


def test_une_cagnotte_qui_bouge_refait_les_lignes(goals):
    """La distance à l'objectif en dépend : la garder afficherait un chiffre
    faux."""
    goals.set_streamers([_S("ponce", donation=500.0)])
    goals.seed_cache({"ponce": [_G("piment", 1000.0)]})
    avant = _lignes(goals)
    goals.set_streamers([_S("ponce", donation=900.0)])
    goals._on_goals_arrived("ponce", [_G("piment", 1000.0)])
    assert _lignes(goals) != avant


def test_un_objectif_qui_tombe_refait_les_lignes(goals):
    goals.set_streamers([_S("ponce", donation=500.0)])
    goals.seed_cache({"ponce": [_G("piment", 1000.0)]})
    avant = _lignes(goals)
    goals._on_goals_arrived("ponce", [_G("piment", 1000.0, accompli=True)])
    assert _lignes(goals) != avant


def test_changer_de_vue_force_la_reconstruction(goals):
    """L'empreinte de la vue précédente ne décrit pas ce qu'on va afficher."""
    goals.set_streamers([_S("ponce", donation=500.0)])
    goals.seed_cache({"ponce": [_G("piment", 1000.0)]})
    goals._changer_vue("tous")
    goals._changer_vue("streamer")
    assert len(_lignes(goals)) == 1


def test_la_vue_tous_ne_se_reconstruit_pas_a_l_identique(goals):
    goals.set_streamers([_S("ponce", donation=500.0)])
    goals.seed_cache({"ponce": [_G("piment", 1000.0)]})
    goals._changer_vue("tous")
    avant = _lignes(goals)
    goals.seed_cache({"ponce": [_G("piment", 1000.0)]})
    assert _lignes(goals) == avant


def test_changer_de_streamer_change_les_lignes(goals):
    """Deux chaînes peuvent avoir des objectifs de même nom et même montant :
    l'empreinte doit porter aussi le login."""
    goals.set_streamers([_S("a", donation=500.0), _S("b", donation=500.0)])
    goals.seed_cache({"a": [_G("piment", 1000.0)],
                      "b": [_G("piment", 1000.0)]})
    goals._combo.setCurrentIndex(goals._combo.findText("a"))
    avant = _lignes(goals)
    goals._combo.setCurrentIndex(goals._combo.findText("b"))
    assert _lignes(goals) != avant


def test_le_streamer_choisi_survit_a_un_rafraichissement(goals):
    """Les données repassent toutes les 30 s : la sélection ne doit pas
    retomber sur le premier de la liste à chaque tour."""
    goals.set_streamers([_S("a"), _S("b"), _S("c")])
    goals._combo.setCurrentIndex(goals._combo.findText("c"))
    goals.set_streamers([_S("a"), _S("b"), _S("c")])
    assert goals._combo.currentText() == "c"


# ═══════════════════════════════════════════════════════════════════════════
# Mixer — tranches, coupures, dépinglage, reconstruction
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mixer(qtbot):
    m = panel._MixerTab()
    qtbot.addWidget(m)
    return m


def _bouton_fermer(strip) -> QPushButton | None:
    for b in strip.findChildren(QPushButton):
        if b.toolTip() == "Retirer des audios épinglés":
            return b
    return None


def _nom_affiche(strip) -> str:
    """Le dernier libellé de la tranche : le nom de la chaîne."""
    return strip.findChildren(QLabel)[-1].text()


# ── le bug de la tranche « plein écran » ─────────────────────────────────

def test_la_tranche_plein_ecran_suit_le_changement_de_chaine(mixer):
    """Sa clé interne vaut toujours `_MAIN` : comparer les seules clés laissait
    la console afficher l'avatar et le nom de la chaîne PRÉCÉDENTE."""
    mixer.set_displays({"zerator": "ZeratoR", "ponce": "Ponce"})
    mixer.set_main_stream("zerator")
    mixer.set_pinned([])
    assert _nom_affiche(mixer._strips[mixer._MAIN]) == "ZeratoR"

    mixer.set_main_stream("ponce")
    assert _nom_affiche(mixer._strips[mixer._MAIN]) == "Ponce"
    assert mixer._strips[mixer._MAIN]._login_reel == "ponce"


def test_la_tranche_plein_ecran_route_toujours_par_sa_cle(mixer):
    """`_login` est la clé de routage, pas la chaîne affichée : le plein écran
    répond au même signal quel que soit le flux du moment."""
    mixer.set_main_stream("zerator")
    mixer.set_pinned([])
    strip = mixer._strips[mixer._MAIN]
    assert strip.login == mixer._MAIN
    assert strip._login_reel == "zerator"


# ── tranches ─────────────────────────────────────────────────────────────

def test_le_plein_ecran_ouvre_la_console(mixer):
    """C'est la source principale, celle par rapport à laquelle on dose."""
    mixer.set_main_stream("zerator")
    mixer.set_pinned(["ponce", "mistermv"])
    assert list(mixer._strips) == [mixer._MAIN, "ponce", "mistermv"]


def test_le_plein_ecran_n_apparait_qu_une_fois(mixer):
    """Il peut être aussi épinglé dans la grille : deux tranches pour un même
    son se contrediraient."""
    mixer.set_main_stream("zerator")
    mixer.set_pinned(["zerator", "ponce"])
    assert list(mixer._strips) == [mixer._MAIN, "ponce"]


def test_la_tranche_plein_ecran_ne_se_depingle_pas(mixer):
    """On ne retire pas le flux qu'on est en train de regarder."""
    mixer.set_main_stream("zerator")
    mixer.set_pinned(["ponce"])
    assert _bouton_fermer(mixer._strips[mixer._MAIN]) is None
    assert _bouton_fermer(mixer._strips["ponce"]) is not None


def test_depingler_depuis_la_console_le_demande_a_la_fenetre(mixer):
    """Constater qu'on ne veut plus entendre une chaîne, puis devoir retourner
    faire un clic droit dans la grille, n'avait aucun sens."""
    mixer.set_pinned(["ponce"])
    demandes: list[str] = []
    mixer.unpin_requested.connect(demandes.append)
    _bouton_fermer(mixer._strips["ponce"]).click()
    assert demandes == ["ponce"]


def test_une_tranche_montre_le_nom_pas_le_login(mixer):
    mixer.set_displays({"mistermv": "MV"})
    mixer.set_pinned(["mistermv"])
    assert _nom_affiche(mixer._strips["mistermv"]) == "MV"


def test_un_nom_trop_long_est_coupe_mais_reste_consultable(mixer):
    """La tranche fait 84 px : un nom entier la ferait grossir et déformerait
    toute la rangée. L'infobulle garde le nom complet."""
    mixer.set_displays({"a": "Un pseudonyme vraiment interminable"})
    mixer.set_pinned(["a"])
    nom = mixer._strips["a"].findChildren(QLabel)[-1]
    assert nom.text() != "Un pseudonyme vraiment interminable"
    assert "interminable" in nom.toolTip()


def test_une_chaine_sans_nom_connu_garde_son_login(mixer):
    """Mieux vaut un login qu'une tranche anonyme."""
    mixer.set_pinned(["inconnu"])
    assert _nom_affiche(mixer._strips["inconnu"]) == "inconnu"


# ── volumes et coupures ──────────────────────────────────────────────────

def test_bouger_un_curseur_previent_la_grille(mixer):
    mixer.set_pinned(["ponce"])
    recus: list[tuple] = []
    mixer.volume_changed.connect(lambda lg, v: recus.append((lg, v)))
    mixer._strips["ponce"]._slider.setValue(30)
    assert recus == [("ponce", 30)]


def test_le_curseur_du_plein_ecran_emprunte_son_propre_signal(mixer):
    """La grille et le plein écran sont deux lecteurs distincts."""
    mixer.set_main_stream("zerator")
    mixer.set_pinned([])
    principal: list[int] = []
    grille: list[tuple] = []
    mixer.main_volume_changed.connect(principal.append)
    mixer.volume_changed.connect(lambda lg, v: grille.append((lg, v)))
    mixer._strips[mixer._MAIN]._slider.setValue(30)
    assert principal == [30] and grille == []


def test_bouger_le_curseur_d_une_tranche_coupee_ne_reveille_pas_le_son(mixer):
    """Doser une source muette, c'est préparer son retour — pas la rallumer."""
    mixer.set_pinned(["ponce"])
    mixer._strips["ponce"]._mute.setChecked(True)
    recus: list[tuple] = []
    mixer.volume_changed.connect(lambda lg, v: recus.append((lg, v)))
    mixer._strips["ponce"]._slider.setValue(30)
    assert recus == []


def test_le_curseur_garde_sa_valeur_quand_on_coupe(mixer):
    """La coupure est une notion distincte du volume : couper puis rétablir
    doit retrouver le réglage."""
    mixer.set_pinned(["ponce"])
    mixer._strips["ponce"]._slider.setValue(30)
    mixer._strips["ponce"]._mute.setChecked(True)
    assert mixer._strips["ponce"].volume() == 30


def test_couper_une_tranche_previent_la_grille(mixer):
    mixer.set_pinned(["ponce"])
    recus: list[tuple] = []
    mixer.mute_changed.connect(lambda lg, m: recus.append((lg, m)))
    mixer._strips["ponce"]._mute.setChecked(True)
    assert recus == [("ponce", True)]


def test_couper_le_plein_ecran_emprunte_son_propre_signal(mixer):
    mixer.set_main_stream("zerator")
    mixer.set_pinned([])
    principal: list[bool] = []
    grille: list[tuple] = []
    mixer.main_mute_changed.connect(principal.append)
    mixer.mute_changed.connect(lambda lg, m: grille.append((lg, m)))
    mixer._strips[mixer._MAIN]._mute.setChecked(True)
    assert principal == [True] and grille == []


def test_une_coupure_se_voit_sur_le_chiffre(mixer):
    mixer.set_pinned(["ponce"])
    mixer._strips["ponce"]._mute.setChecked(True)
    assert "#ff4444" in mixer._strips["ponce"]._val.styleSheet()


# ── reconstruction ───────────────────────────────────────────────────────

def test_une_chaine_rappelee_retrouve_son_niveau(mixer):
    """Dépingler puis rappeler une chaîne ne doit pas remettre son volume à
    fond — on l'avait baissée pour une raison."""
    mixer.set_pinned(["ponce"])
    mixer._strips["ponce"]._slider.setValue(25)
    mixer.set_pinned([])
    mixer.set_pinned(["ponce"])
    assert mixer._strips["ponce"].volume() == 25


def test_une_tranche_reconstruite_retrouve_sa_coupure(mixer):
    mixer.set_pinned(["ponce", "mistermv"])
    mixer._strips["ponce"]._mute.setChecked(True)
    mixer.set_pinned(["ponce"])
    assert mixer._strips["ponce"]._muet is True


def test_la_reconstruction_reapplique_les_niveaux_sans_attendre(mixer):
    """Une chaîne rappelée doit retrouver son niveau sur le champ, pas au
    premier mouvement de curseur."""
    mixer.set_pinned(["ponce"])
    mixer._strips["ponce"]._slider.setValue(25)
    mixer.set_pinned([])
    recus: list[tuple] = []
    mixer.volume_changed.connect(lambda lg, v: recus.append((lg, v)))
    mixer.set_pinned(["ponce"])
    assert ("ponce", 25) in recus


def test_reposer_les_memes_sources_ne_reconstruit_pas(mixer):
    """Les données repassent toutes les 30 s : refaire les tranches ferait
    sauter les curseurs sous les doigts."""
    mixer.set_main_stream("zerator")
    mixer.set_pinned(["ponce"])
    avant = mixer._strips["ponce"]
    mixer.set_pinned(["ponce"])
    assert mixer._strips["ponce"] is avant


def test_reposer_la_meme_chaine_principale_ne_reconstruit_pas(mixer):
    mixer.set_main_stream("zerator")
    mixer.set_pinned([])
    avant = mixer._strips[mixer._MAIN]
    mixer.set_main_stream("zerator")
    assert mixer._strips[mixer._MAIN] is avant


# ── console vide ─────────────────────────────────────────────────────────

def test_une_console_sans_source_explique_comment_en_ajouter(mixer):
    mixer.set_pinned([])
    assert not mixer._vide.isHidden()
    assert mixer._conteneur.isHidden()


def test_la_premiere_source_fait_place_aux_tranches(mixer):
    mixer.set_pinned(["ponce"])
    assert mixer._vide.isHidden()
    assert not mixer._conteneur.isHidden()


@pytest.mark.parametrize("combien,attendu", [
    (0, ""), (1, "· 1 source"), (2, "· 2 sources"), (7, "· 7 sources"),
])
def test_le_compte_de_sources_s_accorde(combien, attendu):
    """Rien du tout quand la console est vide : le message de vide le dit déjà."""
    assert panel._MixerTab._compte_sources(combien) == attendu


def test_le_compte_affiche_suit_les_sources(mixer):
    mixer.set_main_stream("zerator")
    mixer.set_pinned(["ponce", "mistermv"])
    assert mixer._compte.text() == "· 3 sources"


def test_une_chaine_vide_n_ouvre_pas_de_tranche(mixer):
    """La liste épinglée vient de la configuration : elle peut être sale."""
    mixer.set_pinned(["", None, "ponce"])
    assert list(mixer._strips) == ["ponce"]


# ═══════════════════════════════════════════════════════════════════════════
# Accueil — chiffres clés et fil d'événements
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def accueil(qtbot):
    o = panel._AccueilTab()
    qtbot.addWidget(o)
    return o


def _stats(donation: float = 0.0, viewers: int = 0) -> panel.GlobalStats:
    return panel.GlobalStats(donation_total=donation,
                             donation_formatted=f"{donation:.0f} €",
                             viewers_total=viewers, website_mode="live")


# ── chiffres clés ────────────────────────────────────────────────────────

def test_les_chiffres_cles_reprennent_les_donnees_globales(accueil):
    accueil.refresh([_S("a", 100), _S("b", 200), _S("c", online=False)],
                    _stats(1500.0, 300))
    assert accueil._amt_lbl.text() == "1500 €"
    assert accueil._viewers_lbl.text() == "300"
    assert accueil._live_count_lbl.text() == "2 / 3"


def test_l_audience_qui_monte_porte_sa_fleche(accueil):
    """La flèche compare au RELEVÉ PRÉCÉDENT, pas à une moyenne : ce qu'on veut
    savoir, c'est si ça monte à l'instant."""
    accueil.refresh([_S("a", 100)], _stats(viewers=100))
    accueil.refresh([_S("a", 200)], _stats(viewers=200))
    assert accueil._trend_lbl.text() == "▲"


def test_l_audience_qui_baisse_porte_l_autre_fleche(accueil):
    accueil.refresh([_S("a", 200)], _stats(viewers=200))
    accueil.refresh([_S("a", 100)], _stats(viewers=100))
    assert accueil._trend_lbl.text() == "▼"


def test_une_audience_stable_n_affiche_aucune_fleche(accueil):
    """Une flèche figée laisserait croire à un mouvement."""
    accueil.refresh([_S("a", 200)], _stats(viewers=200))
    accueil.refresh([_S("a", 200)], _stats(viewers=200))
    assert accueil._trend_lbl.text() == ""


def test_le_podium_ne_montre_que_les_trois_plus_grosses_audiences(accueil):
    accueil.refresh([_S(f"s{i}", viewers=i * 100) for i in range(5)],
                    _stats(viewers=1000))
    assert [c.isHidden() for c in accueil._player_cards] == [False] * 3


def test_le_podium_masque_les_cartes_en_trop(accueil):
    accueil.refresh([_S("a", 100)], _stats(viewers=100))
    assert [c.isHidden() for c in accueil._player_cards] == [False, True, True]


def test_les_cartes_du_podium_ne_sont_pas_recreees(accueil):
    """Les recréer ferait clignoter la rangée à chaque rafraîchissement."""
    avant = list(accueil._player_cards)
    accueil.refresh([_S("a", 100)], _stats(viewers=100))
    accueil.refresh([_S("b", 200)], _stats(viewers=200))
    assert accueil._player_cards == avant


def test_un_podium_sans_personne_en_direct_ne_leve_pas(accueil):
    """Hors event, aucune chaîne n'est allumée : max(...) n'a rien à comparer."""
    accueil.refresh([_S("a", online=False)], _stats())
    assert all(c.isHidden() for c in accueil._player_cards)


# ── favoris ──────────────────────────────────────────────────────────────

def test_les_favoris_en_direct_sont_annonces(monkeypatch):
    monkeypatch.setattr(panel.favorites, "get", lambda: {"a", "b"})
    phrase = panel._AccueilTab._phrase_favoris(
        [_S("a", display="Alpha"), _S("b", display="Beta"),
         _S("c", display="Gamma")])
    assert phrase == "Vos favoris en direct : Alpha, Beta"


def test_les_favoris_hors_ligne_ne_sont_pas_annonces(monkeypatch):
    monkeypatch.setattr(panel.favorites, "get", lambda: {"a"})
    assert panel._AccueilTab._phrase_favoris([_S("a", online=False)]) == ""


def test_au_dela_de_trois_favoris_le_reste_est_compte(monkeypatch):
    """Citer quinze noms dans une bande de 36 px ne se lit pas."""
    monkeypatch.setattr(panel.favorites, "get",
                        lambda: {f"s{i}" for i in range(6)})
    phrase = panel._AccueilTab._phrase_favoris(
        [_S(f"s{i}") for i in range(6)])
    assert phrase.endswith("et 3 autres")


def test_un_seul_favori_en_trop_reste_au_singulier(monkeypatch):
    monkeypatch.setattr(panel.favorites, "get",
                        lambda: {f"s{i}" for i in range(4)})
    phrase = panel._AccueilTab._phrase_favoris([_S(f"s{i}") for i in range(4)])
    assert phrase.endswith("et 1 autre")


# ── projection, rythme, comparaison ──────────────────────────────────────

def test_la_projection_se_donne_au_rythme_actuel(accueil):
    accueil.update_history(_FauxHistorique(projection=2_000_000.0))
    assert "2" in accueil._proj_lbl.text()
    assert accueil._proj_sub.text() == "projection au rythme actuel"


def test_une_projection_absente_dit_pourquoi(accueil):
    """Hors event il n'y a rien à extrapoler : ce n'est pas une panne."""
    accueil.update_history(_FauxHistorique(debut=time.time() + 86_400))
    assert accueil._proj_lbl.text() == "—"
    assert accueil._proj_sub.text() == "disponible au début de l'event"


def test_apres_l_event_la_projection_le_dit(accueil):
    accueil.update_history(_FauxHistorique(debut=1.0, fin=2.0))
    assert accueil._proj_sub.text() == "event terminé"


def test_pendant_l_event_une_projection_manquante_est_une_attente(accueil):
    accueil.update_history(_FauxHistorique(debut=time.time() - 3600,
                                           fin=time.time() + 3600))
    assert accueil._proj_sub.text() == "en attente de données"


def test_le_rythme_de_collecte_s_affiche(accueil):
    """La question qu'on se pose en boucle pendant l'event."""
    accueil.update_history(_FauxHistorique(rate=1234.0))
    assert "€/min" in accueil._rate_lbl.text()
    assert accueil._rate_lbl.text().startswith("+")


def test_un_rythme_inconnu_n_affiche_rien(accueil):
    accueil.update_history(_FauxHistorique())
    assert accueil._rate_lbl.text() == ""


def test_la_comparaison_a_l_edition_precedente_se_dit_sous_la_cagnotte(accueil):
    accueil.update_history(_FauxHistorique(comparaison=(900_000.0, 12.0)))
    assert accueil._don_sub.text() == "cagnotte totale · +12 % vs 2025"


def test_sans_edition_precedente_la_cagnotte_reste_nue(accueil):
    accueil.update_history(_FauxHistorique(comparaison=(900_000.0, 12.0)))
    accueil.update_history(_FauxHistorique())
    assert accueil._don_sub.text() == "cagnotte totale"


# ── objectifs au bandeau ─────────────────────────────────────────────────

def _fond(accueil, cle: str) -> str:
    entree = accueil._banner._ambient.get(cle)
    return entree[1] if entree else ""


def test_l_objectif_le_plus_proche_monte_au_bandeau(accueil):
    """C'est l'information qui a une chance de se vérifier dans les minutes
    qui suivent."""
    accueil.update_goals([
        panel.GoalWithStreamer("a", "Alpha", "loin", 1000.0, False, 10.0),
        panel.GoalWithStreamer("b", "Beta", "imminent", 100.0, False, 96.0),
    ])
    assert "Beta" in _fond(accueil, "goal")
    assert "imminent" in _fond(accueil, "goal")


def test_un_objectif_deja_atteint_ne_monte_pas_au_bandeau(accueil):
    accueil.update_goals([
        panel.GoalWithStreamer("a", "Alpha", "fait", 100.0, True, 100.0)])
    assert _fond(accueil, "goal") == ""


def test_le_bandeau_oublie_l_objectif_quand_il_n_y_en_a_plus(accueil):
    accueil.update_goals([
        panel.GoalWithStreamer("b", "Beta", "imminent", 100.0, False, 96.0)])
    accueil.update_goals([])
    assert _fond(accueil, "goal") == ""


# ── fil d'événements ─────────────────────────────────────────────────────

def test_le_fil_horodate_chaque_entree(accueil):
    """Dix minutes d'absence et tout était perdu quand ils n'existaient que
    sous forme de toasts."""
    accueil.add_feed_event("hype", "ponce", "ça s'emballe")
    texte = accueil._feed._list.item(0).text()
    assert texte.endswith("ça s'emballe")
    assert texte[2] == ":" and texte[:2].isdigit()


def test_le_fil_colore_selon_la_nature(accueil):
    accueil.add_feed_event("goal", "ponce", "objectif atteint")
    couleur = accueil._feed._list.item(0).foreground().color().name()
    assert couleur == "#00ff87"


def test_une_nature_inconnue_reste_lisible(accueil):
    """Les natures viennent d'appelants variés : une couleur manquante ne doit
    pas rendre la ligne invisible."""
    accueil.add_feed_event("inconnue", "ponce", "quelque chose")
    couleur = accueil._feed._list.item(0).foreground().color().name()
    assert couleur == "#cccccc"


def test_la_derniere_nouvelle_arrive_en_tete(accueil):
    accueil.add_feed_event("hype", "a", "première")
    accueil.add_feed_event("hype", "b", "seconde")
    assert accueil._feed._list.item(0).text().endswith("seconde")


def test_le_fil_ne_grossit_pas_indefiniment(accueil):
    """Une alerte de chat toutes les quelques secondes, sur quatre jours."""
    for i in range(panel._EventFeed._MAX_ITEMS + 20):
        accueil.add_feed_event("hype", "a", f"nouvelle {i}")
    assert accueil._feed._list.count() == panel._EventFeed._MAX_ITEMS


def test_le_fil_range_son_message_de_vide_des_la_premiere_entree(accueil):
    assert not accueil._feed._empty.isHidden()
    accueil.add_feed_event("hype", "a", "quelque chose")
    assert accueil._feed._empty.isHidden()


def test_un_clic_dans_le_fil_ramene_au_stream_concerne(accueil):
    """Le login voyage avec l'entrée : sans lui, lire une alerte ne mène nulle
    part."""
    demandes: list[str] = []
    accueil.stream_selected.connect(demandes.append)
    accueil.add_feed_event("hype", "ponce", "ça s'emballe")
    accueil._feed._on_activate(accueil._feed._list.item(0))
    assert demandes == ["ponce"]


def test_une_entree_sans_chaine_ne_mene_nulle_part(accueil):
    """Un palier de cagnotte ne concerne personne en particulier."""
    demandes: list[str] = []
    accueil.stream_selected.connect(demandes.append)
    accueil.add_feed_event("money", "", "un million")
    accueil._feed._on_activate(accueil._feed._list.item(0))
    assert demandes == []


def test_seuls_les_evenements_rares_montent_au_bandeau(accueil):
    """Les alertes de chat se comptent par dizaines par heure : les faire
    défiler dans la bande la rendrait illisible."""
    accueil.add_feed_event("hype", "a", "ça s'emballe")
    assert accueil._banner._items == []
    accueil.add_feed_event("goal", "a", "objectif atteint")
    assert [t for _k, t in accueil._banner._items] == ["objectif atteint"]


# ── bandeau d'annonces ───────────────────────────────────────────────────

@pytest.fixture
def bandeau(qtbot):
    b = panel._AccueilBanner()
    qtbot.addWidget(b)
    return b


def test_une_annonce_passe_devant_les_messages_de_fond(bandeau):
    """Elle vient de se produire : la faire attendre son tour de rotation lui
    ferait rater son moment."""
    bandeau.set_ambient("next", "next", "À suivre : quelque chose")
    bandeau.push("goal", "objectif atteint")
    assert bandeau._label.text() == "objectif atteint"


def test_une_annonce_vide_est_ignoree(bandeau):
    avant = bandeau._label.text()
    bandeau.push("goal", "   ")
    assert bandeau._label.text() == avant


def test_le_bandeau_ne_garde_que_les_dernieres_annonces(bandeau):
    """Une bande qui ressort une nouvelle d'il y a deux heures ne donne plus
    l'impression de suivre quoi que ce soit."""
    for i in range(panel._AccueilBanner._MAX_KEPT + 5):
        bandeau.push("goal", f"nouvelle {i}")
    assert len(bandeau._items) == panel._AccueilBanner._MAX_KEPT


def test_une_annonce_repetee_remonte_au_lieu_de_se_dedoubler(bandeau):
    bandeau.push("goal", "A")
    bandeau.push("goal", "B")
    bandeau.push("goal", "A")
    assert [t for _k, t in bandeau._items] == ["A", "B"]


def test_un_message_de_fond_remplace_le_precedent_du_meme_sujet(bandeau):
    """Sans cela le bandeau accumulerait dix versions du même « à suivre » au
    fil des rafraîchissements."""
    bandeau.set_ambient("next", "next", "À suivre : A")
    bandeau.set_ambient("next", "next", "À suivre : B")
    assert [t for _k, t in bandeau._pool()] == ["À suivre : B"]


def test_un_message_de_fond_vide_retire_le_sujet(bandeau):
    bandeau.set_ambient("next", "next", "À suivre : A")
    bandeau.set_ambient("next", "next", "")
    assert bandeau._ambient == {}


def test_retirer_un_sujet_absent_ne_fait_rien(bandeau):
    bandeau.set_ambient("next", "next", "")
    assert bandeau._ambient == {}


def test_les_messages_de_fond_sortent_dans_un_ordre_fixe(bandeau):
    """Une rotation dont l'ordre change à chaque tour se lit comme du bruit."""
    bandeau.set_ambient("count", "next", "trois rendez-vous")
    bandeau.set_ambient("now", "event", "en ce moment")
    assert [t for _k, t in bandeau._pool()] == ["en ce moment",
                                                "trois rendez-vous"]


def test_un_bandeau_sans_rien_a_dire_reste_habite(bandeau):
    """Une bande vide au milieu de la page passerait pour une panne."""
    assert bandeau._pool() and bandeau._pool()[0][1]


def test_une_nature_inconnue_ne_prive_pas_l_annonce_de_couleur(bandeau):
    bandeau.push("licorne", "quelque chose")
    assert bandeau._items[0][0] == "event"


def test_le_prochain_show_se_pose_comme_message_de_fond(bandeau):
    bandeau.set_next_show("Lancement ZEVENT", "18h00")
    assert "Lancement ZEVENT" in dict(
        (k, t) for k, (_n, t) in bandeau._ambient.items())["next"]


def test_un_prochain_show_incomplet_ne_pose_rien(bandeau):
    """Un show sans heure ne se suit pas."""
    bandeau.set_next_show("Lancement ZEVENT", "")
    assert bandeau._ambient == {}


def test_la_rotation_avance_dans_le_vivier(bandeau):
    bandeau.push("goal", "A")
    bandeau.push("goal", "B")
    bandeau._rotate()
    bandeau._on_anim_finished()
    assert bandeau._label.text() == "A", "B est en tête, A vient ensuite"


# ── phrases du bandeau ───────────────────────────────────────────────────

@pytest.mark.parametrize("secondes,attendu", [
    (0, "dans un instant"),
    (30, "dans un instant"),
    (60, "dans 1 min"),
    (25 * 60, "dans 25 min"),
    (3 * 3600 + 10 * 60, "dans 3 h 10"),
    (3 * 3600, "dans 3 h"),
    (2 * 86_400, "dans 2 jours"),
    (86_400 + 3600, "dans 1 jour"),
    (-500, "dans un instant"),
])
def test_le_delai_se_dit_a_la_bonne_echelle(secondes, attendu):
    """« dans 9 000 min » ne se lit pas ; « dans 2 jours », si."""
    assert panel._AccueilTab._delai(secondes) == attendu


def test_le_show_en_cours_est_annonce_avec_son_heure_de_fin(accueil):
    maintenant = time.time()
    accueil.update_events([_event("Lancement", maintenant - 60,
                                  maintenant + 600, heure_fin="19:30")])
    assert _fond(accueil, "now") == "En ce moment : Lancement jusqu'à 19h30"


def test_le_prochain_show_est_annonce_avec_son_delai(accueil):
    maintenant = time.time()
    accueil.update_events([_event("Suite", maintenant + 1505,
                                  maintenant + 3000, heure="20:00")])
    assert _fond(accueil, "next") == "À suivre : Suite à 20h00 (dans 25 min)"


def test_deux_shows_qui_se_chevauchent_n_en_annoncent_qu_un(accueil):
    """Annoncer les deux ferait clignoter le bandeau entre eux."""
    maintenant = time.time()
    accueil.update_events([
        _event("A", maintenant - 60, maintenant + 600),
        _event("B", maintenant - 30, maintenant + 600),
    ])
    assert "A" in _fond(accueil, "now") and "B" not in _fond(accueil, "now")


def test_le_prochain_show_est_le_plus_proche(accueil):
    maintenant = time.time()
    accueil.update_events([
        _event("tard", maintenant + 9000, maintenant + 10_000),
        _event("bientot", maintenant + 600, maintenant + 1200),
    ])
    assert "bientot" in _fond(accueil, "next")


def test_le_reste_du_jour_ne_se_dit_qu_a_partir_de_deux(accueil):
    """Annoncer « 1 rendez-vous encore » alors qu'il est nommé juste au-dessus
    n'apprend rien."""
    from datetime import datetime as _dt

    jour = _dt.now().strftime("%Y-%m-%d")
    maintenant = time.time()
    accueil.update_events([_event("seul", maintenant + 600,
                                  maintenant + 1200, jour=jour)])
    assert _fond(accueil, "count") == ""

    accueil.update_events([
        _event("a", maintenant + 600, maintenant + 1200, jour=jour),
        _event("b", maintenant + 1800, maintenant + 2400, jour=jour),
    ])
    assert _fond(accueil, "count").startswith("2 rendez-vous")


def test_sans_programme_le_bandeau_n_annonce_aucun_show(accueil):
    accueil.update_events([])
    assert _fond(accueil, "now") == "" and _fond(accueil, "next") == ""


def test_un_show_sans_horaire_lisible_est_ignore(accueil):
    """L'API renvoie parfois des horaires vides : ils ne doivent pas faire
    tomber la recomposition du bandeau."""
    accueil.update_events([panel.EventItem(
        id="x", name="Sans heure", day="", start_local="", end_local="",
        description="")])
    assert _fond(accueil, "now") == ""


# ── shows qui démarrent ──────────────────────────────────────────────────

def _accueil_avec_shows(accueil, evenements) -> None:
    """Amorce l'onglet : un premier passage sert de référence.

    Au lancement, un show en cours depuis une heure n'est pas une nouvelle.
    """
    accueil.refresh([_S("zerator", gdoc_id="uuid-z")], _stats())
    accueil.update_events(evenements)
    accueil._check_started_shows()


def test_un_show_qui_vient_de_commencer_est_propose(accueil, monkeypatch):
    """Les rappels du Programme préviennent AVANT ; ici il s'agit de proposer
    la bascule au moment où ça démarre."""
    from core import alerts

    monkeypatch.setattr(alerts, "enabled", lambda _f: True)
    _accueil_avec_shows(accueil, [])
    recus: list[tuple] = []
    accueil.show_started.connect(lambda lg, nom: recus.append((lg, nom)))

    accueil.update_events([_event("Lancement", time.time() - 10,
                                  time.time() + 600, hotes=("uuid-z",))])
    accueil._check_started_shows()
    assert recus == [("zerator", "Lancement")]


def test_un_show_commence_depuis_une_heure_n_est_pas_une_nouvelle(
        accueil, monkeypatch):
    from core import alerts

    monkeypatch.setattr(alerts, "enabled", lambda _f: True)
    _accueil_avec_shows(accueil, [])
    recus: list[tuple] = []
    accueil.show_started.connect(lambda lg, nom: recus.append((lg, nom)))

    accueil.update_events([_event("Vieux", time.time() - 3600,
                                  time.time() + 600, hotes=("uuid-z",))])
    accueil._check_started_shows()
    assert recus == []


def test_un_show_n_est_propose_qu_une_fois(accueil, monkeypatch):
    """Le bandeau est recomposé toutes les 45 s : reproposer la bascule à
    chaque tour rendrait les deux premières minutes d'un show insupportables."""
    from core import alerts

    monkeypatch.setattr(alerts, "enabled", lambda _f: True)
    _accueil_avec_shows(accueil, [])
    recus: list[tuple] = []
    accueil.show_started.connect(lambda lg, nom: recus.append((lg, nom)))

    accueil.update_events([_event("Lancement", time.time() - 10,
                                  time.time() + 600, hotes=("uuid-z",))])
    accueil._check_started_shows()
    accueil._check_started_shows()
    assert len(recus) == 1


def test_un_show_qui_demarre_au_premier_passage_ne_reveille_personne(
        accueil, monkeypatch):
    """Au lancement de l'application, on ne sait pas ce qui vient de commencer
    et ce qui était déjà là : rien ne doit surgir."""
    from core import alerts

    monkeypatch.setattr(alerts, "enabled", lambda _f: True)
    accueil.refresh([_S("zerator", gdoc_id="uuid-z")], _stats())
    recus: list[tuple] = []
    accueil.show_started.connect(lambda lg, nom: recus.append((lg, nom)))
    accueil.update_events([_event("Lancement", time.time() - 10,
                                  time.time() + 600, hotes=("uuid-z",))])
    accueil._check_started_shows()
    assert recus == []


def test_une_alerte_desactivee_ne_propose_rien(accueil, monkeypatch):
    from core import alerts

    monkeypatch.setattr(alerts, "enabled", lambda _f: False)
    _accueil_avec_shows(accueil, [])
    recus: list[tuple] = []
    accueil.show_started.connect(lambda lg, nom: recus.append((lg, nom)))

    accueil.update_events([_event("Lancement", time.time() - 10,
                                  time.time() + 600, hotes=("uuid-z",))])
    accueil._check_started_shows()
    assert recus == []


def test_un_show_sans_presentateur_resolvable_ne_propose_rien(
        accueil, monkeypatch):
    """Les invités non-streamers n'ont pas de chaîne à ouvrir."""
    from core import alerts

    monkeypatch.setattr(alerts, "enabled", lambda _f: True)
    _accueil_avec_shows(accueil, [])
    recus: list[tuple] = []
    accueil.show_started.connect(lambda lg, nom: recus.append((lg, nom)))

    accueil.update_events([_event("Concert", time.time() - 10,
                                  time.time() + 600, hotes=("uuid-gims",))])
    accueil._check_started_shows()
    assert recus == []


def test_un_hote_designe_par_son_login_est_reconnu(accueil, monkeypatch):
    """Le programme mélange identifiants gdoc et logins selon les éditions."""
    from core import alerts

    monkeypatch.setattr(alerts, "enabled", lambda _f: True)
    _accueil_avec_shows(accueil, [])
    recus: list[tuple] = []
    accueil.show_started.connect(lambda lg, nom: recus.append((lg, nom)))

    accueil.update_events([_event("Lancement", time.time() - 10,
                                  time.time() + 600, hotes=("zerator",))])
    accueil._check_started_shows()
    assert recus == [("zerator", "Lancement")]


def test_un_show_sans_nom_reste_annoncable(accueil, monkeypatch):
    from core import alerts

    monkeypatch.setattr(alerts, "enabled", lambda _f: True)
    _accueil_avec_shows(accueil, [])
    recus: list[tuple] = []
    accueil.show_started.connect(lambda lg, nom: recus.append((lg, nom)))

    accueil.update_events([_event("", time.time() - 10, time.time() + 600,
                                  hotes=("zerator",))])
    accueil._check_started_shows()
    assert recus == [("zerator", "Événement")]


# ── rien ne doit flotter ─────────────────────────────────────────────────

def test_reconstruire_les_onglets_chiffres_ne_laisse_rien_flotter(
        qtbot, monkeypatch):
    """Un widget détaché par `setParent(None)` alors qu'il est encore VISIBLE
    devient une fenêtre du bureau — le défaut mesuré à +124 fenêtres par
    rafraîchissement sur l'onglet Goals."""
    from PyQt6.QtWidgets import QApplication

    monkeypatch.setattr(panel, "QWebEngineView", _FausseVueWeb)
    onglets = [panel._GoalsTab(), panel._MixerTab(), panel._StatsTab()]
    for o in onglets:
        qtbot.addWidget(o)
    onglets[0]._do_fetch = lambda *a: None
    connues = {id(w) for w in QApplication.topLevelWidgets() if w.isVisible()}

    for tour in range(4):
        onglets[0].set_streamers([_S(f"s{i}", donation=100.0 * tour)
                                  for i in range(5)])
        onglets[0].seed_cache({f"s{i}": [_G(f"g{i}", 500.0 + tour)]
                               for i in range(5)})
        onglets[1].set_main_stream(f"s{tour}")
        onglets[1].set_pinned([f"s{i}" for i in range(tour % 3)])
        onglets[2].update_streamers([_S(f"s{i}") for i in range(5)])
        QApplication.processEvents()

    surgies = [w.metaObject().className()
               for w in QApplication.topLevelWidgets()
               if w.isVisible() and id(w) not in connues]
    assert surgies == [], "widgets devenus des fenêtres : " + ", ".join(surgies)


def test_les_colonnes_qui_peuvent_rester_vides_s_expliquent(stats):
    """Un tiret sans explication passe pour une panne.

    « Objectifs » et « +/h » n'ont légitimement rien à montrer une bonne part
    du temps : la question se pose devant le tableau, la réponse doit y être.
    """
    entete = stats._ranking_table
    for colonne in (panel._C_OBJ, panel._C_TEND):
        bulle = entete.horizontalHeaderItem(colonne).toolTip()
        assert bulle, f"colonne {colonne} sans infobulle"
    assert "cinq minutes" in entete.horizontalHeaderItem(panel._C_TEND).toolTip()
    assert "favoris" in entete.horizontalHeaderItem(panel._C_OBJ).toolTip()


def test_chaque_infobulle_vise_une_colonne_existante(stats):
    """Une clé restée sur un ancien numéro poserait la bulle à côté."""
    total = stats._ranking_table.columnCount()
    assert all(0 <= c < total for c in panel._StatsTab._INFOBULLES)


# ═══════════════════════════════════════════════════════════════════════════
# Stats — agir depuis le classement
# ═══════════════════════════════════════════════════════════════════════════

def _remplir(stats, *streamers):
    stats.update_streamers(list(streamers))
    return stats._ranking_table


def test_chaque_ligne_porte_son_login(stats):
    """Le libellé affiche le nom, parfois précédé d'une étoile : jamais le
    login. Sans cette donnée, une ligne ne peut désigner personne."""
    _remplir(stats, _S("morrigh4n", display="Morrigh4n"))
    assert stats.login_de_la_ligne(0) == "morrigh4n"


def test_une_ligne_qui_n_existe_pas_ne_designe_personne(stats):
    """Le tableau se retrie et se refiltre : un indice ne vaut qu'un instant."""
    assert stats.login_de_la_ligne(0) == ""
    assert stats.login_de_la_ligne(-1) == ""


def test_le_double_clic_demande_la_fiche(stats):
    _remplir(stats, _S("morrigh4n"))
    recu: list[str] = []
    stats.sheet_requested.connect(recu.append)
    stats._sur_double_clic(0, panel._C_NOM)
    assert recu == ["morrigh4n"]


def test_un_double_clic_dans_le_vide_ne_demande_rien(stats):
    recu: list[str] = []
    stats.sheet_requested.connect(recu.append)
    stats._sur_double_clic(7, panel._C_NOM)
    assert recu == []


def test_le_menu_contextuel_propose_les_memes_leviers_que_les_cartes(stats):
    _remplir(stats, _S("morrigh4n"))
    menu = stats.menu_de_la_ligne(0)
    libelles = " | ".join(a.text() for a in menu.actions() if not a.isSeparator())
    for attendu in ("plein écran", "grille", "favoris", "fiche"):
        assert attendu in libelles, f"« {attendu} » absent de : {libelles}"


def test_pas_de_menu_sur_une_ligne_absente(stats):
    assert stats.menu_de_la_ligne(3) is None


@pytest.mark.parametrize("signal,fragment", [
    ("stream_requested", "plein écran"),
    ("grid_requested", "grille"),
    ("sheet_requested", "fiche"),
])
def test_chaque_entree_du_menu_emet_ce_qu_elle_annonce(stats, signal, fragment):
    _remplir(stats, _S("morrigh4n"))
    menu = stats.menu_de_la_ligne(0)
    recu: list[str] = []
    getattr(stats, signal).connect(recu.append)
    action = next(a for a in menu.actions() if fragment in a.text())
    action.trigger()
    assert recu == ["morrigh4n"]


def test_le_menu_bascule_le_favori_et_le_dit(stats, monkeypatch, tmp_path):
    monkeypatch.setattr(panel.favorites, "CONFIG_PATH", tmp_path / "c.json",
                        raising=False)
    etats: dict[str, bool] = {}
    monkeypatch.setattr(panel.favorites, "is_favorite",
                        lambda lg: etats.get(lg, False))
    monkeypatch.setattr(panel.favorites, "toggle",
                        lambda lg: etats.__setitem__(lg, not etats.get(lg, False))
                        or etats[lg])
    _remplir(stats, _S("morrigh4n"))
    recu: list[tuple] = []
    stats.favori_change.connect(lambda lg, on: recu.append((lg, on)))

    ajouter = next(a for a in stats.menu_de_la_ligne(0).actions()
                   if "Ajouter aux favoris" in a.text())
    ajouter.trigger()
    assert recu == [("morrigh4n", True)]
    # L'étoile est peinte dans la cellule du nom : sans redessin, elle
    # n'apparaîtrait qu'au prochain sondage.
    assert "\u2605" in stats._ranking_table.item(0, panel._C_NOM).text()
    assert any("Retirer des favoris" in a.text()
               for a in stats.menu_de_la_ligne(0).actions())


def test_sans_objectifs_charges_l_entete_dit_pourquoi(stats):
    """Il n'existe aucun appel à la demande : laisser l'en-tête muet ferait
    croire que la chaîne ne publie aucun objectif."""
    _remplir(stats, _S("morrigh4n"))
    entete = stats.menu_de_la_ligne(0).actions()[0]
    assert entete.isEnabled() is False
    assert "favoris" in entete.text()


def test_l_entete_porte_le_nom_et_le_compte_des_objectifs(stats):
    _remplir(stats, _S("morrigh4n", display="Morrigh4n"))
    stats.seed_goals({"morrigh4n": [_G("a", accompli=True), _G("b"), _G("c")]})
    entete = stats.menu_de_la_ligne(0).actions()[0]
    assert entete.isEnabled() is False, "un titre n'est pas une action"
    assert "Morrigh4n" in entete.text()
    assert "1/3" in entete.text()


def test_le_menu_n_ouvre_la_fiche_que_par_une_seule_entree(stats):
    """« Objectifs de dons » et « Ouvrir la fiche » faisaient la même chose :
    deux entrées pour un seul geste, alors que le compte n'est qu'une
    information — il est passé dans le titre du menu."""
    _remplir(stats, _S("morrigh4n"))
    stats.seed_goals({"morrigh4n": [_G("a")]})
    recu: list[str] = []
    stats.sheet_requested.connect(recu.append)
    cliquables = [a for a in stats.menu_de_la_ligne(0).actions()
                  if a.isEnabled() and not a.isSeparator()]
    for action in cliquables:
        action.trigger()
    assert recu == ["morrigh4n"], "une seule entrée doit ouvrir la fiche"


@pytest.mark.parametrize("delta,attendu", [
    (None, "inconnu"),
    (0.0, "stable"),
    (0.4, "stable"),      # bruit d'arrondi de la cagnotte, pas une montée
    (1.0, "hausse"),
    (12_400.0, "hausse"),
])
def test_le_sens_d_une_tendance_en_euros(delta, attendu):
    """Jamais « baisse » : une cagnotte ne redescend pas, et `tendances.cagnotte`
    borne déjà l'écart à zéro."""
    assert panel._sens_tendance_euros(delta) == attendu


# ── coupe signalée ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("texte,limite,attendu", [
    ("court", 16, "court"),
    ("pile seize lettr", 16, "pile seize lettr"),      # à la limite, rien à dire
    ("Je repeins le décor en rose", 16, "Je repeins le…"),
    ("Supercalifragilisticexpialidocious", 16, "Supercalifragili…"),
])
def test_une_coupe_se_signale_par_des_points(texte, limite, attendu):
    """« Je repein » se lit comme un nom complet. « Je repeins le… » non."""
    assert panel._couper_avec_points(texte, limite) == attendu


def test_la_coupe_prefere_lacher_un_mot_entier():
    """Un mot entier de moins vaut mieux qu'un mot tronqué — tant que la coupe
    ne remonte pas trop haut : au-delà d'un tiers perdu, on coupe au caractère.
    """
    assert panel._couper_avec_points("alpha bravo charlie", 12) == "alpha bravo…"
    # L'espace est trop tôt dans la chaîne : le couper perdrait l'essentiel.
    assert panel._couper_avec_points("a bravissimoooo", 12) == "a bravissimo…"


def test_un_objectif_coupe_porte_son_texte_entier_en_infobulle(qtbot):
    """Sans l'infobulle, la coupe n'apprend rien de plus que rien du tout."""
    # Le vrai type, et non une doublure à la main : celle qui vivait ici
    # portait « amount » quand le dataclass dit « amount_target », et n'a donc
    # jamais vérifié que l'item sait lire un objectif réel.
    objectif = panel.GoalWithStreamer(
        streamer_login="x",
        streamer_display="Un streamer au nom interminable",
        goal_name="Je repeins tout le décor du studio en rose fluo",
        amount_target=100.0, accomplished=False, pct=42.0)

    item = panel._AccueilGoalItem(objectif)
    qtbot.addWidget(item)
    bulles = [w.toolTip() for w in item.findChildren(QLabel) if w.toolTip()]
    assert any("interminable" in b for b in bulles)
    assert any("rose fluo" in b for b in bulles)


# ═══════════════════════════════════════════════════════════════════════════
# Programme — s'abonner depuis la frise
# ═══════════════════════════════════════════════════════════════════════════

class _Show:
    """Un EventItem réduit à ce que la frise et les rappels consultent."""

    def __init__(self, ident="ev-1", nom="Blind Test Musical",
                 debut_dans=3600.0):
        import time as _t
        self.id = ident
        self.name = nom
        self.day = "2026-09-04"
        self.start_local = "18:00"
        self.end_local = "19:00"
        self.description = ""
        self.host_uuids = ["antoinedaniel"]
        self.participant_uuids = []
        self.start_ts = _t.time() + debut_dans
        self.end_ts = self.start_ts + 3600.0
        self.names = {}
        self.logins = {}
        self.profile_urls = {}


@pytest.fixture
def accueil(qtbot, monkeypatch):
    monkeypatch.setattr(panel, "QWebEngineView", _FausseVueWeb)
    o = panel._AccueilTab()
    qtbot.addWidget(o)
    return o


def _entrees(menu):
    return [a.text() for a in menu.actions() if not a.isSeparator()]


def test_la_frise_propose_le_rappel(accueil, config_vierge):
    """Le rappel n'existait que sous forme de cloche dans l'onglet Programme :
    depuis la frise — là où l'on découvre qu'un show approche — il fallait
    changer d'onglet et retrouver la bonne carte."""
    menu = accueil.menu_d_un_show(_Show(), "antoinedaniel")
    assert any("Me rappeler" in t for t in _entrees(menu))


def test_un_show_deja_abonne_propose_de_se_desabonner(accueil, config_vierge,
                                                      monkeypatch):
    monkeypatch.setattr(panel, "_load_reminders", lambda: {"ev-1"})
    menu = accueil.menu_d_un_show(_Show(), "antoinedaniel")
    assert any("Désactiver le rappel" in t for t in _entrees(menu))


def test_un_show_passe_garde_son_entree_grisee(accueil, config_vierge):
    """La masquer ferait croire que le rappel n'existe pas pour ce show-là,
    alors qu'il est seulement trop tard."""
    menu = accueil.menu_d_un_show(_Show(debut_dans=-600.0), "antoinedaniel")
    rappel = next(a for a in menu.actions() if "Rappel" in a.text())
    assert rappel.isEnabled() is False
    assert "déjà commencé" in rappel.text()


@pytest.mark.parametrize("choix,signal,attendu", [
    ("rappel", "rappel_bascule", "ev-1"),
    ("fullscreen", "stream_selected", "antoinedaniel"),
    ("grille", "add_to_grid", "antoinedaniel"),
])
def test_chaque_entree_de_la_frise_emet_ce_qu_elle_annonce(
        accueil, config_vierge, monkeypatch, choix, signal, attendu):
    """`exec` est bloquant : on lui fait rendre l'entrée voulue."""
    show = _Show()
    accueil._events = [show]
    accueil._uuid_to_login = {"antoinedaniel": "antoinedaniel"}
    accueil._all_logins = {"antoinedaniel"}
    monkeypatch.setattr(
        panel.QMenu, "exec",
        lambda self, *a: next(x for x in self.actions() if x.data() == choix))
    recu: list[str] = []
    getattr(accueil, signal).connect(recu.append)
    accueil._on_timeline_click(show)
    assert recu == [attendu]


def test_fermer_le_menu_de_la_frise_sans_choisir_ne_fait_rien(
        accueil, config_vierge, monkeypatch):
    show = _Show()
    accueil._uuid_to_login = {"antoinedaniel": "antoinedaniel"}
    accueil._all_logins = {"antoinedaniel"}
    monkeypatch.setattr(panel.QMenu, "exec", lambda self, *a: None)
    recu: list[str] = []
    accueil.rappel_bascule.connect(recu.append)
    accueil.stream_selected.connect(recu.append)
    accueil._on_timeline_click(show)
    assert recu == []


def test_la_cle_d_un_show_est_la_meme_des_deux_cotes():
    """Deux calculs qui divergeraient d'un caractère feraient deux
    abonnements distincts pour un même show."""
    show = _Show()
    assert panel.cle_evenement(show) == panel._ProgrammeTab._event_key(show)


def test_sans_identifiant_la_cle_reste_stable():
    """L'API peut ne pas fournir d'id : jour, heure et nom ne bougent pas non
    plus d'un sondage à l'autre."""
    show = _Show(ident="")
    cle = panel.cle_evenement(show)
    assert cle == "2026-09-04_18:00_Blind Test Musical"
    assert panel.cle_evenement(_Show(ident="")) == cle


# ── superposition de l'édition précédente sur les graphes ────────────────────

def _charge(stats):
    return json.loads(stats._charts_payload)


@pytest.fixture
def comparaison_active(monkeypatch):
    """Déclare la course commencée.

    Les éditions passées sont calées sur le vendredi 18 h : avant, elles n'ont
    rien à dire et ne sont pas envoyées.
    """
    from core import history_store

    monkeypatch.setattr(history_store, "course_commencee",
                        lambda *_a: True)


def test_les_graphes_transportent_la_courbe_de_reference(stats, comparaison_active):
    """Sans elle, on voit sa propre courbe monter sans savoir si c'est mieux
    ou moins bien que l'an dernier — la question de tout le monde pendant
    l'événement."""
    stats._charts_ready = True
    stats.update_history(_FauxHistorique(
        dons=[(1_000.0, 500.0), (2_000.0, 900.0)],
        viewers=[(1_000.0, 10), (2_000.0, 20)],
        ref_dons=[400.0, 1_100.0], ref_viewers=[8, 25]))
    charge = _charge(stats)
    assert charge["rd"]["2025"] == [400, 1100]
    assert charge["rv"]["2025"] == [8, 25]
    assert len(charge["rd"]["2025"]) == len(charge["vd"]), (
        "Chart.js aligne par indice : deux longueurs différentes décaleraient")


def test_sans_reference_la_courbe_est_absente_plutot_que_plate(stats):
    """Une liste de zéros dessinerait une ligne au sol qu'on croirait vraie."""
    stats._charts_ready = True
    stats.update_history(_FauxHistorique(
        dons=[(1_000.0, 500.0)], viewers=[(1_000.0, 10)]))
    charge = _charge(stats)
    assert charge["rd"] == {}
    assert charge["rv"] == {}


def test_un_trou_dans_la_reference_reste_un_trou(stats, comparaison_active):
    """`null` interrompt la courbe ; zéro dessinerait une falaise."""
    stats._charts_ready = True
    stats.update_history(_FauxHistorique(
        dons=[(1_000.0, 500.0), (2_000.0, 900.0)],
        viewers=[(1_000.0, 10), (2_000.0, 20)],
        ref_dons=[400.0, None], ref_viewers=[None, None]))
    charge = _charge(stats)
    assert charge["rd"]["2025"] == [400, None]
    assert "2025" not in charge["rv"], "entièrement vide : la courbe se masque"


@pytest.mark.parametrize("serie,attendu", [
    (None, []),
    ([], []),
    ([None, None], []),
    ([1.4, None, 2.6], [1, None, 3]),
])
def test_l_arrondi_de_la_reference_garde_les_trous(serie, attendu):
    assert panel._arrondi_ou_rien(serie) == attendu


def test_les_abscisses_portent_l_heure_vraie_des_releves(stats):
    """Elles étaient rebasées sur l'ouverture de la cagnotte.

    Le remède valait quand ZLink ne traçait que ses propres relevés : leur date
    n'avait alors aucun rapport avec le temps de course. L'édition en cours
    étant préchargée depuis son début, ses horodatages SONT ce temps — rebaser
    une seconde fois déplaçait l'axe d'une journée entière.
    """
    from datetime import datetime, timezone

    stats._charts_ready = True
    depart = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc).timestamp()
    stats.update_history(_FauxHistorique(
        dons=[(depart + i * 21_600.0, 100.0 * i) for i in range(3)],
        viewers=[(depart, 10)]))
    charge = _charge(stats)

    attendu = []
    for i in range(3):
        dt = (datetime.fromtimestamp(depart + i * 21_600.0, tz=timezone.utc)
              + panel.PARIS)
        attendu.append(f"{panel.JOURS_FR[dt.weekday()]} {dt.hour:02d}h")
    assert charge["ld"] == attendu


# ── Goals : restreindre « les plus proches » à la grille ou aux favoris ─────

@pytest.fixture
def _sans_portee(monkeypatch):
    """Grille et favoris pilotés depuis le test, sans toucher au disque."""
    def poser(grille=(), favoris=()):
        monkeypatch.setattr(panel, "portee_des_objectifs",
                            lambda vue: {"grille": set(grille),
                                         "favoris": set(favoris)}.get(vue))
    return poser


def _noms_affiches(goals) -> set[str]:
    """Les chaînes réellement listées, et non tout QLabel de l'onglet.

    `_textes` ramène aussi l'en-tête de la fiche streamer, masqué dans ces
    vues mais toujours présent : il portait le nom du dernier streamer choisi
    et faisait passer le filtre pour inopérant.
    """
    noms = set()
    for ligne in _lignes(goals):
        noms |= set(_textes(ligne))
    return noms


def _trois_streamers(goals):
    goals.set_streamers([
        _S("ponce", donation=500.0, display="Ponce"),
        _S("zerator", donation=800.0, display="ZeratoR"),
        _S("domingo", donation=200.0, display="Domingo"),
    ])
    goals.seed_cache({
        "ponce": [_G("piment", 1000.0)],
        "zerator": [_G("crâne rasé", 1000.0)],
        "domingo": [_G("karaoké", 1000.0)],
    })


def test_la_portee_grille_ne_garde_que_les_chaines_affichees(goals, _sans_portee):
    """Un objectif à deux euros de tomber chez quelqu'un qu'on n'affiche pas
    ne se joue pas : c'est la grille qu'on pilote."""
    _sans_portee(grille=("ponce", "domingo"))
    _trois_streamers(goals)
    goals._changer_vue("grille")
    noms = _noms_affiches(goals)
    assert "Ponce" in noms and "Domingo" in noms
    assert "ZeratoR" not in noms


def test_la_portee_favoris_ne_garde_que_les_favoris(goals, _sans_portee):
    _sans_portee(favoris=("zerator",))
    _trois_streamers(goals)
    goals._changer_vue("favoris")
    noms = _noms_affiches(goals)
    assert "ZeratoR" in noms
    assert "Ponce" not in noms and "Domingo" not in noms


def test_la_vue_tous_ne_filtre_toujours_rien(goals, _sans_portee):
    """La portée d'origine ne bouge pas : les deux autres s'ajoutent à elle."""
    _sans_portee(grille=("ponce",))
    _trois_streamers(goals)
    goals._changer_vue("tous")
    assert {"Ponce", "ZeratoR", "Domingo"} <= _noms_affiches(goals)


def test_l_entete_dit_la_portee_retenue(goals, _sans_portee):
    """Trois listes d'apparence identique : sans le dire, on ne sait plus
    laquelle on regarde."""
    _sans_portee(grille=("ponce",), favoris=("zerator",))
    _trois_streamers(goals)
    for vue, attendu in (("tous", "LES PLUS PROCHES (3)"),
                         ("grille", "LES PLUS PROCHES · GRILLE (1)"),
                         ("favoris", "LES PLUS PROCHES · FAVORIS (1)")):
        goals._changer_vue(vue)
        assert attendu in _textes(goals), vue


def test_une_portee_vide_le_dit_sans_promettre_l_attente(goals, _sans_portee):
    """« Ils apparaîtront au fil des chaînes consultées » est vrai pour
    l'ensemble, et trompeur pour une grille vide : là, c'est la grille qu'il
    faut remplir."""
    _sans_portee(grille=())
    _trois_streamers(goals)
    goals._changer_vue("grille")
    textes = " ".join(_textes(goals))
    assert "grille" in textes.lower()
    assert "au fil des chaînes consultées" not in textes


def test_changer_de_portee_reconstruit_bien_la_liste(goals, _sans_portee):
    """L'empreinte évite les reconstructions inutiles — elle ne doit pas
    empêcher celle-ci : les deux listes ont la même longueur."""
    _sans_portee(grille=("ponce",), favoris=("zerator",))
    _trois_streamers(goals)
    goals._changer_vue("grille")
    assert "Ponce" in _noms_affiches(goals)
    goals._changer_vue("favoris")
    noms = _noms_affiches(goals)
    assert "ZeratoR" in noms and "Ponce" not in noms


def test_les_quatre_portees_sont_proposees(goals):
    assert list(goals._boutons_vue) == ["streamer", "tous", "grille", "favoris"]


# ── « Prochains objectifs » : la distance, pas seulement le pourcentage ─────

def _prochain(cible: float, pct: float) -> panel.GoalWithStreamer:
    return panel.GoalWithStreamer("a", "Alpha", "objectif", cible, False, pct)


def test_un_objectif_proche_annonce_ce_qu_il_reste_a_reunir():
    """Ces objectifs sont tous entre 90 et 100 % : affichés « 100% » les uns
    sous les autres, ils étaient indiscernables. Ce qui les sépare — et ce sur
    quoi on peut agir — c'est le montant qui manque."""
    texte = panel._distance_objectif(_prochain(1000.0, 96.0))
    assert "40" in texte and "€" in texte and "96%" in texte


def test_le_montant_precede_le_pourcentage():
    """C'est lui qu'on lit en diagonale ; la barre redit déjà le pourcentage."""
    texte = panel._distance_objectif(_prochain(1000.0, 96.0))
    assert texte.index("€") < texte.index("%")


def test_un_objectif_atteint_n_annonce_pas_un_reste_nul():
    """« plus que 0 € » se lit comme une somme à réunir."""
    assert panel._distance_objectif(_prochain(500.0, 100.0)) == "100%"


def test_la_ligne_affiche_bien_la_distance(qtbot):
    item = panel._AccueilGoalItem(_prochain(1000.0, 96.0))
    qtbot.addWidget(item)
    assert any("plus que" in t for t in _textes(item))


def test_la_distance_n_annonce_jamais_moins_que_le_necessaire():
    """Arrondie au supérieur : la somme affichée doit suffire à faire tomber
    l'objectif, sans quoi on donne et il ne se passe rien."""
    assert "7 €" in panel._distance_objectif(_prochain(100.0, 93.0))
    assert "7 €" in panel._distance_objectif(_prochain(1000.0, 99.35))


def test_un_objectif_a_quelques_euros_ne_s_annonce_pas_termine():
    """Il affichait « 100% » et paraissait bloqué ; il manquait quatre euros."""
    texte = panel._distance_objectif(_prochain(1000.0, 99.6))
    assert "99%" in texte and "4 €" in texte


def test_un_objectif_finance_sort_des_prochains_objectifs(qtbot):
    """Trié sur -pct, il se classait premier et n'en sortait jamais."""
    w = panel._AccueilGoalsWidget()
    qtbot.addWidget(w)
    w.update_goals([
        _prochain(100.0, 100.0),      # financé : arrivé, pas « prochain »
        _prochain(1000.0, 94.0),
        _prochain(1000.0, 91.0),
    ])
    lignes = w.findChildren(panel._AccueilGoalItem)
    assert len(lignes) == 2
    assert all("100%" not in t for ligne in lignes for t in _textes(ligne))


def test_le_compte_d_objectifs_suit_la_cagnotte(qtbot):
    """« 0 atteint sur 17 » alors que chaque barre affichait 100,0 %."""
    class _But:
        def __init__(self, montant):
            self.name = "objectif"
            self.amount = montant
            self.accomplished = False

    buts = [_But(m) for m in (101.0, 306.0, 500.0, 20_000.0)]
    faits = sum(1 for b in buts if panel._objectif_atteint(b, 16_140.0))
    assert faits == 3


@pytest.fixture
def comparaison_impossible(monkeypatch):
    from core import history_store

    monkeypatch.setattr(history_store, "course_commencee",
                        lambda *_a: False)


def test_avant_le_depart_aucune_edition_n_est_comparee(stats, comparaison_impossible):
    """La cagnotte 2026 ouvre vingt-quatre heures avant la course.

    Superposées à ce jeudi soir, les éditions passées y traçaient une
    progression qui, pour elles, n'a pas eu lieu."""
    import json

    class _Hist:
        def get_donation_series(self):
            return [1000.0 + 30 * i for i in range(26)], [1.0] * 26

        def get_viewers_series(self):
            return [1000.0 + 30 * i for i in range(26)], [1.0] * 26

        def series_editions_alignees(self, ts):
            return {"2025": [float(i) for i in range(len(ts))]}

        def series_viewers_editions_alignees(self, ts):
            return {"2025": [float(i) for i in range(len(ts))]}

    stats.update_history(_Hist())
    charge = json.loads(json.loads(stats._charts_payload)) \
        if isinstance(json.loads(stats._charts_payload), str) \
        else json.loads(stats._charts_payload)
    assert charge["rd"] == {}
    assert charge["rv"] == {}
    assert charge["ld"], "la courbe de l'édition en cours, elle, reste tracée"


# ── le switch « toute la course » ──────────────────────────────────────────

def test_le_switch_etend_l_axe_jusqu_au_lundi(stats, comparaison_active):
    """Coché, il trace les éditions jusqu'au bout ; la courbe de l'année avance
    derrière, à mesure que l'événement se déroule."""
    from core.history_store import DEBUT_COURSE, FIN_COURSE, OUVERTURE_CAGNOTTE

    stats._charts_ready = True
    stats.update_history(_FauxHistorique(
        dons=[(DEBUT_COURSE + i * 3600.0, 1000.0 * i) for i in range(3)],
        viewers=[(DEBUT_COURSE, 10)]))
    court = len(_charge(stats)["ld"])

    stats._toute_la_course.setChecked(True)
    charge = _charge(stats)
    assert len(charge["ld"]) > court
    attendu = int((FIN_COURSE - OUVERTURE_CAGNOTTE) / 1800.0) + 1
    assert len(charge["ld"]) == attendu


def test_le_switch_rejoue_le_dernier_historique(stats, comparaison_active):
    """Sans cela, il faudrait attendre la relève suivante — dix minutes."""
    from core.history_store import DEBUT_COURSE

    stats._charts_ready = True
    stats.update_history(_FauxHistorique(
        dons=[(DEBUT_COURSE + i * 3600.0, 1000.0 * i) for i in range(3)],
        viewers=[(DEBUT_COURSE, 10)]))
    avant = _charge(stats)["ld"]
    stats._toute_la_course.setChecked(True)      # le signal suffit
    assert _charge(stats)["ld"] != avant


# ── Onglet Clips ────────────────────────────────────────────────────────────

def _clip(slug="a", titre="Un moment", vues=100, cree=300.0, duree=30.0,
          login="ponce", chaine="Ponce"):
    from core.twitch_clips import Clip

    return Clip(slug=slug, titre=titre, vues=vues, cree_le=cree,
                duree_s=duree, login=login, chaine=chaine, auteur="",
                vignette="")


@pytest.fixture
def clips(qtbot):
    """L'onglet garni à la main, sans réseau."""
    def monter(*liste):
        onglet = panel._ClipsTab()
        qtbot.addWidget(onglet)
        onglet._clips = list(liste)
        onglet._remplir_les_chaines()
        onglet._reafficher()
        return onglet
    return monter


def _cartes_de(onglet):
    return onglet.findChildren(panel._CarteClip)


def test_l_onglet_liste_les_clips(clips):
    onglet = clips(_clip("a"), _clip("b", vues=50))
    assert len(_cartes_de(onglet)) == 2
    assert "2 clips" in onglet._compte.text()
    assert "depuis l'ouverture" in onglet._compte.text()


def test_le_filtre_par_chaine_ne_propose_que_les_chaines_clippees(clips):
    """Trois cents participants dont la plupart n'ont rien : une liste où les
    entrées ne rendent rien ne se parcourt pas."""
    onglet = clips(_clip("a", login="ponce", chaine="Ponce"),
                   _clip("b", login="ponce", chaine="Ponce"),
                   _clip("c", login="zerator", chaine="ZeratoR"))
    libelles = [onglet._chaine.itemText(i) for i in range(onglet._chaine.count())]
    assert libelles[0].startswith("Toutes les chaînes (2)")
    # La plus clippée d'abord, avec son compte.
    assert "Ponce" in libelles[1] and "(2)" in libelles[1]
    assert "ZeratoR" in libelles[2]


def test_le_filtre_par_chaine_restreint_la_liste(clips):
    onglet = clips(_clip("a", login="ponce"), _clip("b", login="ponce"),
                   _clip("c", login="zerator", chaine="ZeratoR"))
    onglet._chaine.setCurrentIndex(onglet._chaine.findData("zerator"))
    assert len(_cartes_de(onglet)) == 1


def test_le_filtre_est_retenu_apres_un_rafraichissement(clips):
    """Rafraîchir ne doit pas ramener d'autorité sur « toutes les chaînes »."""
    onglet = clips(_clip("a", login="ponce"), _clip("b", login="zerator",
                                                    chaine="ZeratoR"))
    onglet._chaine.setCurrentIndex(onglet._chaine.findData("zerator"))
    onglet._recevoir([_clip("c", login="ponce"),
                      _clip("d", login="zerator", chaine="ZeratoR")])
    assert onglet._chaine.currentData() == "zerator"


def test_le_tri_reordonne_sans_recharger(clips):
    """Les quatre tris se font sur la liste déjà chargée : la redemander pour
    la retrier serait une requête pour rien."""
    onglet = clips(_clip("vieux", vues=90, cree=100.0),
                   _clip("neuf", vues=10, cree=900.0))
    assert _cartes_de(onglet)[0]._clip.slug == "vieux"
    onglet._tri.setCurrentIndex(
        [onglet._tri.itemData(i) for i in range(onglet._tri.count())].index("recents"))
    assert _cartes_de(onglet)[0]._clip.slug == "neuf"


def test_cliquer_un_clip_le_signale(clips, qtbot):
    onglet = clips(_clip("a"))
    carte = _cartes_de(onglet)[0]
    with qtbot.waitSignal(onglet.clip_choisi) as attrape:
        carte.clique.emit(carte._clip)
    assert attrape.args[0].slug == "a"


def test_une_chaine_sans_clip_le_dit(clips):
    """Le message change avec le filtre : « aucun clip » tout court laisserait
    croire que le chargement a échoué."""
    onglet = clips(_clip("a", login="ponce"))
    onglet._clips.append(_clip("b", login="zerator", chaine="ZeratoR"))
    onglet._remplir_les_chaines()
    onglet._chaine.setCurrentIndex(onglet._chaine.findData("zerator"))
    onglet._clips = [_clip("a", login="ponce")]
    onglet._reafficher()
    # `isVisible` serait faux sur un onglet jamais affiché : c'est le masquage
    # explicite qu'on vérifie, pas la présence à l'écran.
    assert not onglet._vide.isHidden()
    assert "cette chaîne" in onglet._vide.text()


def test_deux_rafraichissements_ne_se_chevauchent_pas(clips, monkeypatch):
    """La plus lente des deux requêtes écrasait la plus récente."""
    onglet = clips()
    lances = []
    monkeypatch.setattr(panel.threading, "Thread",
                        lambda *a, **k: type("T", (), {
                            "start": lambda _s: lances.append(1)})())
    onglet.rafraichir()
    onglet.rafraichir()
    assert len(lances) == 1


@pytest.mark.parametrize("secondes,attendu", [
    (0, "0:00"), (9, "0:09"), (59, "0:59"), (65, "1:05"), (125, "2:05"),
])
def test_la_duree_se_lit_en_minutes(secondes, attendu):
    assert panel._duree_courte(secondes) == attendu


@pytest.mark.parametrize("ecart,attendu", [
    (120, "il y a 2 min"), (7200, "il y a 2 h"), (172800, "il y a 2 j"),
])
def test_la_fraicheur_prime_sur_la_date(ecart, attendu):
    """La date exacte d'un clip n'apprend rien ; sa fraîcheur si."""
    assert panel._il_y_a(1000.0, maintenant=1000.0 + ecart) == attendu


def test_le_fil_de_chargement_repasse_par_un_signal(clips, qtbot, monkeypatch):
    """`QTimer.singleShot` posé DEPUIS un fil de travail ne part jamais.

    Le timer naît dans un fil sans boucle d'événements : le résultat n'était
    jamais rendu au fil de Qt, et l'onglet restait sur « Chargement… » avec une
    liste vide. Qt, lui, met une émission de signal en file d'attente vers le
    fil du destinataire.
    """
    from core import twitch_clips

    onglet = clips()
    attendus = [_clip("a"), _clip("b")]
    monkeypatch.setattr(panel, "_run_coro", lambda _coro: attendus)
    monkeypatch.setattr(twitch_clips, "lister", lambda *a, **k: None)

    with qtbot.waitSignal(onglet._charges, timeout=2000) as attrape:
        onglet._charger()                 # le corps du fil, appelé directement
    assert [c.slug for c in attrape.args[0]] == ["a", "b"]


def test_une_liste_vide_ne_remplace_pas_celle_qu_on_a(clips):
    """Un réseau qui se dérobe ne doit pas effacer ce qui est affiché."""
    onglet = clips(_clip("a"))
    onglet._recevoir([])
    assert len(_cartes_de(onglet)) == 1
    assert onglet._bouton.text() == "Rafraîchir"


def test_le_lecteur_repasse_aussi_par_un_signal(qtbot, monkeypatch):
    """Même piège : le fil qui résout l'adresse ne peut pas toucher au lecteur."""
    from core import twitch_clips

    monkeypatch.setattr(panel, "_run_coro", lambda _coro: "https://cdn/x.mp4?sig=1")
    monkeypatch.setattr(twitch_clips, "url_de_lecture", lambda *a, **k: None)
    lecteur = panel._LecteurClip(_clip("a"))
    qtbot.addWidget(lecteur)
    with qtbot.waitSignal(lecteur._resolue, timeout=2000) as attrape:
        lecteur._resoudre()
    assert attrape.args[0] == "https://cdn/x.mp4?sig=1"


def test_un_clip_illisible_propose_twitch(qtbot):
    """Plutôt que de rester muet devant un lecteur noir."""
    lecteur = panel._LecteurClip(_clip("a"))
    qtbot.addWidget(lecteur)
    lecteur._lire("")
    assert "Ouvrir sur Twitch" in lecteur._etat.text()
    assert not lecteur._etat.isHidden()


# ── la grille de vignettes ──────────────────────────────────────────────────

def test_les_cartes_se_rangent_en_grille(clips):
    """Une liste de titres ne dit pas ce qu'on va voir ; la vignette si."""
    onglet = clips(*[_clip(f"c{i}") for i in range(9)])
    onglet.resize(1400, 700)
    onglet._reafficher()
    assert onglet._colonnes >= 2
    places = {(onglet._liste.getItemPosition(i)[0],
               onglet._liste.getItemPosition(i)[1])
              for i in range(onglet._liste.count())}
    assert len(places) == 9, "chaque carte a sa propre case"
    assert len({c for _l, c in places}) == onglet._colonnes


@pytest.mark.parametrize("largeur,attendu", [
    (1900, 6), (1290, 4), (630, 2), (300, 1), (80, 1),
])
def test_le_nombre_de_colonnes_suit_la_largeur(clips, largeur, attendu):
    """Le panel s'affiche sur 1920 comme sur la moitié d'un 2560 : une grille
    figée déborde sur l'un et laisse le vide sur l'autre.

    La largeur est imposée plutôt que mesurée : sans passage par la boucle
    d'événements, un `resize` ne se propage pas encore au viewport.
    """
    onglet = clips(_clip("a"))
    onglet._zone.viewport().resize(largeur, 400)
    assert onglet._colonnes_tenables() == attendu


def test_la_duree_est_posee_sur_la_vignette(clips):
    onglet = clips(_clip("a", duree=95.0))
    carte = _cartes_de(onglet)[0]
    assert carte._duree.text() == "1:35"


def test_une_vignette_arrivee_ailleurs_ne_repeint_pas_la_carte(clips, qtbot):
    """Toutes les cartes écoutent le même cache : sans la comparaison
    d'adresse, chacune se repeindrait à chaque image reçue."""
    onglet = clips(_clip("a"))
    carte = _cartes_de(onglet)[0]
    appels = []
    carte._appliquer_vignette = lambda: appels.append(1)
    carte._sur_vignette("https://autre.test/x.jpg")
    assert appels == []
    carte._sur_vignette(carte._clip.vignette)
    assert appels == [1]


def test_le_cache_ne_telecharge_qu_une_fois(qtbot, monkeypatch):
    """Soixante-dix-huit cartes, et souvent la même chaîne : sans ce garde,
    la même image partait autant de fois qu'elle apparaît."""
    cache = panel._CacheVignettes()
    lances = []
    monkeypatch.setattr(panel.threading, "Thread",
                        lambda *a, **k: type("T", (), {
                            "start": lambda _s: lances.append(k.get("args"))})())
    cache.pixmap("https://exemple.test/a.jpg")
    cache.pixmap("https://exemple.test/a.jpg")
    assert len(lances) == 1


def test_une_adresse_vide_ne_lance_rien(monkeypatch):
    cache = panel._CacheVignettes()
    monkeypatch.setattr(panel.threading, "Thread",
                        lambda *a, **k: pytest.fail("aucun fil attendu"))
    assert cache.pixmap("") is None


# ── le lecteur : transport, copie, démontage ────────────────────────────────

class _FauxLecteur:
    """Un MpvWidget réduit à ce que la fenêtre lui demande."""

    def __init__(self, duree=60.0) -> None:
        self._duree = duree
        self._position = 0.0
        self._pause = False
        self.gestes: list[str] = []

    def duree(self): return self._duree
    def position(self): return self._position
    def en_pause(self): return self._pause
    def set_pause(self, v): self._pause = bool(v)
    def chercher(self, s): self._position = float(s)
    def play(self, _u): self.gestes.append("play")
    def shutdown(self): self.gestes.append("shutdown")
    def hide(self): self.gestes.append("hide")
    def setParent(self, _p): self.gestes.append("setParent")
    def deleteLater(self): self.gestes.append("deleteLater")


@pytest.fixture
def lecteur(qtbot):
    """La fenêtre de lecture, avec une doublure à la place de mpv."""
    def monter(duree=60.0):
        fenetre = panel._LecteurClip(_clip("a"))
        qtbot.addWidget(fenetre)
        fenetre._lecteur = _FauxLecteur(duree)
        # Une passe pour que la barre prenne sa plage : sans durée connue,
        # elle va de zéro à zéro et refuse toute valeur.
        fenetre._rafraichir_transport()
        return fenetre
    return monter


def test_l_horloge_et_la_barre_suivent_la_lecture(lecteur):
    fenetre = lecteur()
    fenetre._lecteur.chercher(26.0)
    fenetre._rafraichir_transport()
    assert fenetre._horloge.text() == "0:26 / 1:00"
    assert fenetre._barre.maximum() == 600      # dixièmes de seconde
    assert fenetre._barre.value() == 260


def test_le_curseur_ne_saute_pas_sous_le_doigt(lecteur):
    """Relue cinq fois par seconde, la position ramenait le curseur sous le
    doigt à chaque fois pendant un glissé."""
    fenetre = lecteur()
    fenetre._tenu = True                  # comme si on tenait le curseur
    fenetre._barre.setValue(500)
    fenetre._lecteur.chercher(1.0)        # la lecture, elle, est ailleurs
    fenetre._rafraichir_transport()
    assert fenetre._barre.value() == 500


def test_relacher_le_curseur_deplace_la_lecture(lecteur):
    fenetre = lecteur()
    fenetre._tenu = True
    fenetre._barre.setValue(420)
    fenetre._relacher()
    assert fenetre._lecteur.position() == pytest.approx(42.0)
    assert fenetre._tenu is False


def test_cliquer_dans_la_barre_deplace_aussi(lecteur):
    """C'est le geste qu'on fait d'abord, et il ne produisait rien."""
    fenetre = lecteur()
    fenetre._barre.setValue(300)          # valueChanged, hors glissé
    assert fenetre._lecteur.position() == pytest.approx(30.0)


def test_la_pause_bascule_et_se_voit(lecteur):
    fenetre = lecteur()
    fenetre.basculer_pause()
    assert fenetre._lecteur.en_pause() and fenetre._bouton_pause.text() == "▶"
    fenetre.basculer_pause()
    assert not fenetre._lecteur.en_pause() and fenetre._bouton_pause.text() == "⏸"


def test_l_espace_met_en_pause(lecteur, qtbot):
    from PyQt6.QtGui import QKeyEvent
    from PyQt6.QtCore import QEvent

    fenetre = lecteur()
    fenetre.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Space,
                                    Qt.KeyboardModifier.NoModifier))
    assert fenetre._lecteur.en_pause()


@pytest.mark.parametrize("touche,attendu", [
    (Qt.Key.Key_Right, 15.0), (Qt.Key.Key_Left, 5.0),
])
def test_les_fleches_sautent_de_cinq_secondes(lecteur, touche, attendu):
    from PyQt6.QtGui import QKeyEvent
    from PyQt6.QtCore import QEvent

    fenetre = lecteur()
    fenetre._lecteur.chercher(10.0)
    fenetre.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, touche,
                                    Qt.KeyboardModifier.NoModifier))
    assert fenetre._lecteur.position() == pytest.approx(attendu)


def test_le_lien_se_copie_et_le_dit(lecteur):
    """Sans retour visible, on ne sait pas si le clic a porté."""
    from PyQt6.QtWidgets import QApplication

    fenetre = lecteur()
    fenetre.copier_le_lien()
    assert QApplication.clipboard().text() == "https://clips.twitch.tv/a"
    assert fenetre._copier.text() == "Lien copié"


def test_la_fermeture_demonte_le_lecteur_dans_l_ordre(lecteur):
    """`stop()` seul laissait Qt détruire la fenêtre native de mpv après coup,
    sur un identifiant déjà rendu : « BadWindow » sur X_DestroyWindow, fatal
    pour tout le programme.

    Masquer AVANT de détacher : un widget détaché et visible devient une
    fenêtre à l'écran.
    """
    fenetre = lecteur()
    faux = fenetre._lecteur
    fenetre.close()
    assert faux.gestes == ["shutdown", "hide", "setParent", "deleteLater"]
    assert fenetre._lecteur is None


def test_le_transport_survit_a_la_fermeture(lecteur):
    """Le minuteur peut battre une fois de plus après le démontage."""
    fenetre = lecteur()
    fenetre.close()
    fenetre._rafraichir_transport()       # ne lève pas
    fenetre.basculer_pause()
    fenetre._relacher()


def test_le_retour_de_copie_meurt_avec_la_fenetre(lecteur, qtbot):
    """`QTimer.singleShot` survit à sa cible.

    Fermer le lecteur dans la seconde et demie faisait lever « wrapped C/C++
    object has been deleted » au fond de la boucle de Qt — loin du geste qui
    l'avait causé, et dans un tout autre test.
    """
    fenetre = lecteur()
    fenetre.copier_le_lien()
    assert fenetre._retour.isActive()
    fenetre.close()
    qtbot.wait(50)
    assert not fenetre._retour.isActive()


def test_l_onglet_interroge_les_participants_quand_il_les_connait(clips,
                                                                  monkeypatch):
    """La catégorie ne voit que les clips ÉTIQUETÉS ZEvent : soixante-dix-huit,
    là où six chaînes seules en rendent deux cent quarante-neuf."""
    from core import twitch_clips

    onglet = clips()
    onglet.set_streamers([type("S", (), {"twitch_login": "ponce"})(),
                          type("S", (), {"twitch_login": "zerator"})()])
    appels: dict = {}

    def _par_chaines(logins, *a, **k):
        appels["chaines"] = list(logins)
        return []                       # une liste de Clips, pas les logins

    monkeypatch.setattr(twitch_clips, "lister_par_chaines", _par_chaines)
    monkeypatch.setattr(twitch_clips, "lister",
                        lambda *a, **k: appels.setdefault("categorie", []))
    monkeypatch.setattr(panel, "_run_coro", lambda coro: coro)
    onglet._charger()
    assert appels.get("chaines") == ["ponce", "zerator"]
    assert "categorie" not in appels


def test_sans_participants_l_onglet_retombe_sur_la_categorie(clips, monkeypatch):
    """Au tout premier affichage, ils ne sont pas encore arrivés."""
    from core import twitch_clips

    onglet = clips()
    appels = {}
    monkeypatch.setattr(twitch_clips, "lister",
                        lambda *a, **k: appels.setdefault("categorie", []))
    monkeypatch.setattr(panel, "_run_coro", lambda coro: coro)
    onglet._charger()
    assert "categorie" in appels


def test_le_nombre_de_cartes_est_borne(clips):
    """Trois cents chaînes rendent des milliers de clips ; autant de widgets
    figeraient la fenêtre à chaque tri."""
    onglet = clips(*[_clip(f"c{i}", vues=i) for i in range(panel._ClipsTab._MAX_CARTES + 40)])
    assert len(_cartes_de(onglet)) == panel._ClipsTab._MAX_CARTES
    assert "40 de plus" in onglet._compte.text()


def test_sous_le_plafond_rien_n_est_annonce_en_trop(clips):
    onglet = clips(_clip("a"), _clip("b"))
    assert "de plus" not in onglet._compte.text()
