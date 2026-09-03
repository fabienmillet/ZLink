# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Les cartes de streamers, le programme, et la fenêtre qui les porte.

Ce sont les trois pièces que l'utilisateur manipule le plus pendant l'event :
il choisit ses vingt-cinq chaînes dans les cartes, il consulte le programme
pour savoir ce qui commence, et il navigue entre les onglets. Chacune a déjà
régressé au moins une fois, toujours de la même façon : un signal qui n'était
pas émis, un widget rendu visible avant d'avoir un parent, une carte détruite
puis reconstruite pour rien.

Trois précautions valent pour tout le fichier :

- `windows.panel` est importé AVANT la QApplication — il charge QtWebEngine,
  qui refuse d'être initialisé après.
- Les favoris sont remplacés par une doublure en mémoire : `core.favorites`
  écrit dans la configuration de l'utilisateur.
- Aucun avatar n'est chargé : le loader partagé irait sur le réseau.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import Qt, QPoint, pyqtSignal
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

from windows import panel


# ── doublures ────────────────────────────────────────────────────────────────

class _FausseVueWeb(QWidget):
    """Ce que le panel attend d'une QWebEngineView, et rien de plus.

    QtWebEngine ne survit pas à la plateforme `offscreen`, et les courbes ne
    sont le sujet d'aucun test d'ici.
    """

    loadFinished = pyqtSignal(bool)

    def setUrl(self, _url) -> None:
        pass

    def page(self):
        return self


class _FauxFavoris:
    """Les favoris tenus en mémoire, sans toucher au config.json réel."""

    def __init__(self, initiaux=()) -> None:
        self.logins: set[str] = {x.lower() for x in initiaux}

    def get(self) -> set[str]:
        return set(self.logins)

    def is_favorite(self, login: str) -> bool:
        return bool(login) and login.lower() in self.logins

    def toggle(self, login: str) -> bool:
        login = login.lower()
        if login in self.logins:
            self.logins.discard(login)
            return False
        self.logins.add(login)
        return True


class _S:
    """Un StreamerInfo réduit aux champs que les cartes consultent."""

    def __init__(self, login: str, *, display: str | None = None,
                 online: bool = True, viewers: int = 100,
                 donation: float = 0.0, location: str = "LAN",
                 game: str = "Minecraft", donation_url: str = "") -> None:
        self.twitch_login = login
        self.display = display or login
        self.online = online
        self.viewers = viewers
        self.donation = donation
        self.donation_formatted = f"{donation:.0f} €"
        self.location = location
        self.game = game
        self.profile_url = ""
        self.donation_url = donation_url
        self.gdoc_id = "g_" + login
        self.participation_id = "p_" + login
        self.title = ""


@pytest.fixture(autouse=True)
def sans_reseau(monkeypatch):
    """Neutralise le chargement d'avatars — il télécharge en arrière-plan."""
    from widgets import bigscreen_widget
    monkeypatch.setattr(bigscreen_widget, "load_avatar_into_label",
                        lambda *a, **k: None)


@pytest.fixture
def favoris(monkeypatch):
    """Remplace `core.favorites` par une doublure, et la rend au test."""
    faux = _FauxFavoris()
    monkeypatch.setattr(panel, "favorites", faux)
    return faux


def _carte(qtbot, s: _S, slot: int | None = None) -> "panel._StreamerCard":
    carte = panel._StreamerCard(s, slot)
    qtbot.addWidget(carte)
    return carte


def _clic_gauche(widget) -> None:
    """Clic gauche au centre du widget, sans passer par le gestionnaire natif."""
    pos = widget.rect().center()
    widget.mousePressEvent(QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, pos.toPointF(),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _textes(racine: QWidget) -> list[str]:
    """Tous les libellés affichés sous `racine`, dans l'ordre de création."""
    return [lbl.text() for lbl in racine.findChildren(QLabel)]


# ═══════════════════════════════════════════════════════════════════════════
# _StreamerCard
# ═══════════════════════════════════════════════════════════════════════════

def test_une_chaine_hors_ligne_ne_se_selectionne_pas(qtbot, favoris):
    """La grille ne peut lire que du direct.

    Sélectionner une chaîne éteinte occuperait un slot pour une cellule noire,
    et le compteur mentirait sur ce qui va réellement s'ouvrir.
    """
    carte = _carte(qtbot, _S("horty", online=False))
    recu = []
    carte.toggled.connect(lambda lg, on: recu.append((lg, on)))
    _clic_gauche(carte)
    assert recu == []


def test_un_clic_demande_l_ajout_puis_le_retrait(qtbot, favoris):
    """La carte ne décide pas : elle annonce le sens du basculement.

    C'est l'onglet qui tient la sélection ; si la carte émettait toujours
    « ajoute », un second clic n'aurait jamais retiré la chaîne de la grille.
    """
    s = _S("zerator")
    ajout = _carte(qtbot, s, slot=None)
    retrait = _carte(qtbot, s, slot=3)
    recu = []
    ajout.toggled.connect(lambda lg, on: recu.append(on))
    retrait.toggled.connect(lambda lg, on: recu.append(on))
    _clic_gauche(ajout)
    _clic_gauche(retrait)
    assert recu == [True, False]


def test_le_clic_droit_ouvre_la_fiche(qtbot, favoris):
    """Le raccourci historique vers la fiche. Il reste, pour qui le connaît."""
    carte = _carte(qtbot, _S("mistermv"))
    recu = []
    carte.sheet_requested.connect(recu.append)
    carte.contextMenuEvent(_evenement_menu_contextuel(carte))
    assert recu == ["mistermv"]


def _evenement_menu_contextuel(widget):
    from PyQt6.QtGui import QContextMenuEvent
    return QContextMenuEvent(QContextMenuEvent.Reason.Mouse,
                             QPoint(5, 5), widget.mapToGlobal(QPoint(5, 5)))


def test_le_bouton_fiche_ouvre_la_meme_fiche_que_le_clic_droit(qtbot, favoris):
    """Une fonction qu'on ne trouve pas n'existe pas.

    La fiche n'était atteignable qu'au clic droit ; le bouton est là pour
    qu'on la voie, et il doit demander exactement la même chose.
    """
    carte = _carte(qtbot, _S("kamet0"))
    recu = []
    carte.sheet_requested.connect(recu.append)
    carte._fiche_btn.click()
    assert recu == ["kamet0"]


def test_l_etoile_previent_le_reste_de_l_application(qtbot, favoris):
    """Le favori était enregistré, mais personne n'était mis au courant.

    Le bouton ne repeignait que lui-même : une autre carte de la même chaîne
    gardait son étoile creuse, et la touche « Favori » du boîtier Stream Deck
    restait sur l'état d'AVANT le clic. Sans ce signal, les deux moitiés de
    l'application affichent l'inverse l'une de l'autre.
    """
    carte = _carte(qtbot, _S("aypierre"))
    recu = []
    carte.favori_change.connect(lambda lg, fav: recu.append((lg, fav)))

    carte._fav_btn.click()
    assert recu == [("aypierre", True)]
    assert favoris.is_favorite("aypierre")

    carte._fav_btn.click()
    assert recu == [("aypierre", True), ("aypierre", False)]
    assert not favoris.is_favorite("aypierre")


def test_l_etoile_dit_ce_qu_elle_fera_au_prochain_clic(qtbot, favoris):
    """L'infobulle est le seul indice de l'état pour qui ne distingue pas l'or.

    Elle doit donc changer AVEC le favori, pas rester sur « Mettre en favori »
    une fois l'étoile posée.
    """
    carte = _carte(qtbot, _S("ponce"))
    assert carte._fav_btn.toolTip() == "Mettre en favori"
    carte._fav_btn.click()
    assert carte._fav_btn.toolTip() == "Retirer des favoris"


def test_une_chaine_deja_favorite_arrive_avec_son_etoile(qtbot, monkeypatch):
    """La carte est reconstruite à chaque changement d'état en ligne.

    Si elle repartait de l'étoile creuse, un streamer favori la perdrait
    visuellement dès qu'il coupe puis reprend son direct.
    """
    monkeypatch.setattr(panel, "favorites", _FauxFavoris(["etoile"]))
    carte = _carte(qtbot, _S("etoile"))
    assert carte._fav_btn.toolTip() == "Retirer des favoris"


@pytest.mark.parametrize("lieu,libelle", [
    ("LAN", "LAN"), ("lan", "LAN"), ("Online", "Online"), ("", "Online"),
])
def test_le_badge_distingue_la_lan_du_reste(lieu, libelle):
    """Sur place ou à distance : c'est le premier filtre du regard.

    La comparaison est faite en majuscules parce que l'API communautaire
    renvoie « lan » là où l'API officielle renvoie « LAN ».
    """
    texte, css = panel._StreamerCard._style_badge_type(_S("x", location=lieu))
    assert texte == libelle
    assert "border-radius" in css


def test_une_chaine_eteinte_n_affiche_pas_d_audience(qtbot, favoris):
    """Le dernier chiffre connu d'une chaîne coupée n'est plus vrai."""
    carte = _carte(qtbot, _S("horty", online=False, viewers=4200))
    assert carte._viewers_badge is None
    carte.update_viewers(9999)   # ne doit pas lever


def test_l_audience_se_met_a_jour_sans_reconstruire_la_carte(qtbot, favoris):
    """Le chemin rapide du rafraîchissement.

    Toutes les 30 s, seuls les viewers bougent. Reconstruire les 300 cartes
    pour ça coûtait 785 ms de gel ; le badge doit donc pouvoir se réécrire
    seul.
    """
    carte = _carte(qtbot, _S("anyme023", viewers=1000))
    assert carte._viewers_badge.text() == "1.0k"
    carte.update_viewers(12_400)
    assert carte._viewers_badge.text() == "12.4k"


def test_le_jeu_et_la_cagnotte_absents_ne_laissent_pas_de_ligne_vide(qtbot,
                                                                    favoris):
    """La carte se resserre plutôt que d'afficher des lignes à blanc."""
    nue = _carte(qtbot, _S("nue", game="", donation=0.0))
    garnie = _carte(qtbot, _S("garnie", game="TFT", donation=1500.0))
    assert "TFT" in _textes(garnie)
    assert any(t.startswith("♥") for t in _textes(garnie))
    assert "TFT" not in _textes(nue)
    assert not any(t.startswith("♥") for t in _textes(nue))


# ── ligne « de l'heure » ─────────────────────────────────────────────────────

@pytest.fixture
def tendance(monkeypatch):
    """Pilote ce que `core.tendances` répond, sans attendre un vrai relevé."""
    from core import tendances
    valeurs = {"viewers": None, "cagnotte": None}
    monkeypatch.setattr(tendances, "viewers", lambda *a, **k: valeurs["viewers"])
    monkeypatch.setattr(tendances, "cagnotte", lambda *a, **k: valeurs["cagnotte"])
    return valeurs


def test_rien_ne_s_affiche_tant_qu_on_ne_sait_pas(qtbot, tendance):
    """Au lancement, il faut un quart d'heure de relevés avant qu'un écart ait
    un sens : une ligne vide vaut mieux qu'un chiffre inventé."""
    assert panel._StreamerCard._ligne_de_l_heure(_S("neuf")) is None


def test_une_chaine_eteinte_n_a_pas_de_variation(qtbot, tendance):
    """Elle ne gagne ni ne perd rien : la ligne n'aurait aucun sens."""
    tendance["viewers"] = 500
    assert panel._StreamerCard._ligne_de_l_heure(
        _S("horty", online=False)) is None


def test_une_audience_qui_monte_s_affiche_en_vert(qtbot, tendance):
    """La couleur porte le SENS. Un seul gris obligerait à lire le signe."""
    tendance["viewers"] = 1200
    lbl = panel._StreamerCard._ligne_de_l_heure(_S("monte"))
    assert lbl.text() == "+1.2k viewers / h"
    assert "#00ff87" in lbl.styleSheet()


def test_une_audience_qui_baisse_s_affiche_en_rouge(qtbot, tendance):
    """Le signe est un « moins » typographique, pas un tiret d'union."""
    tendance["viewers"] = -800
    lbl = panel._StreamerCard._ligne_de_l_heure(_S("baisse"))
    assert lbl.text() == "−800 viewers / h"
    assert "#ff6b6b" in lbl.styleSheet()


def test_les_euros_de_l_heure_rejoignent_l_audience(qtbot, tendance):
    """Une chaîne qui perd des viewers mais récolte reste une bonne nouvelle :
    la présence d'euros suffit à peindre la ligne en vert."""
    tendance["viewers"] = -100
    tendance["cagnotte"] = 2500.0
    lbl = panel._StreamerCard._ligne_de_l_heure(_S("mixte"))
    assert "viewers" in lbl.text() and "2 500 €" in lbl.text()
    assert "#00ff87" in lbl.styleSheet()


def test_un_texte_de_variation_ne_peut_pas_porter_de_balise(qtbot, tendance):
    """La ligne vient de chiffres, mais elle est posée en texte brut par
    principe : tout ce qui s'affiche dans le panel vient d'une API."""
    tendance["viewers"] = 10
    lbl = panel._StreamerCard._ligne_de_l_heure(_S("brut"))
    assert lbl.textFormat() == Qt.TextFormat.PlainText


# ── slot, style et boutons posés en surimpression ────────────────────────────

def test_le_numero_de_slot_n_apparait_que_si_la_carte_est_choisie(qtbot,
                                                                 favoris):
    """Le badge est la seule marque du RANG dans la grille."""
    carte = _carte(qtbot, _S("slot"))
    assert carte._slot_lbl.isHidden()
    carte.set_slot(4)
    assert not carte._slot_lbl.isHidden()
    assert carte._slot_lbl.text() == "4"
    carte.set_slot(None)
    assert carte._slot_lbl.isHidden()


def test_la_selection_se_voit_au_liseré_de_la_carte(qtbot, favoris):
    """L'habillage est restreint par nom d'objet, et ce n'est pas cosmétique.

    QLabel dérive de QFrame : un « QFrame { border } » posé sur la carte
    s'appliquait aussi à ses étiquettes, et l'avatar héritait du liseré vert.
    """
    carte = _carte(qtbot, _S("liseré"))
    assert carte.objectName() == "streamerCard"
    carte.set_slot(1)
    assert "QFrame#streamerCard" in carte.styleSheet()
    assert panel._COL_SEL in carte.styleSheet()


def test_reposer_le_meme_slot_ne_repeint_rien(qtbot, favoris):
    """Un setStyleSheet repolit tout le sous-arbre de la carte — 0,32 ms
    pièce, ~100 ms pour 300 cartes à chaque clic. Reposer une valeur
    identique doit donc être un non-événement."""
    carte = _carte(qtbot, _S("stable"), slot=2)
    avant = carte.styleSheet()
    carte.setStyleSheet("")          # trace : réécrit si _apply_style repasse
    carte.set_slot(2)
    assert carte.styleSheet() == ""
    carte.set_slot(3)
    assert carte.styleSheet() == avant


def test_le_bouton_de_don_n_existe_que_si_l_api_donne_un_lien(qtbot, favoris):
    """Un bouton qui n'ouvre rien vaut moins que pas de bouton du tout."""
    sans = _carte(qtbot, _S("sans"))
    avec = _carte(qtbot, _S("avec", donation_url="https://don.test/avec"))
    assert sans._don_btn is None
    assert avec._don_btn is not None


def test_le_bouton_de_don_ouvre_la_page_de_ce_streamer(qtbot, favoris,
                                                       monkeypatch):
    """L'URL est relevée par streamer, pas globalement : ouvrir la cagnotte
    générale à la place ferait donner au mauvais compteur."""
    from windows import fullscreen
    vues = []
    monkeypatch.setattr(fullscreen, "ouvrir_page_de_don", vues.append)
    carte = _carte(qtbot, _S("avec", donation_url="https://don.test/avec"))
    carte._don_btn.click()
    assert vues == ["https://don.test/avec"]


def test_les_boutons_se_rangent_de_droite_a_gauche_dans_la_carte(qtbot,
                                                                 favoris):
    """Ils sont posés en surimpression, à la main : un mauvais calcul les fait
    sortir de la carte ou se recouvrir."""
    carte = _carte(qtbot, _S("rangs", donation_url="https://don.test/x"))
    # Montrée : Qt garde le redimensionnement en attente tant que le widget
    # est caché, et c'est le redimensionnement qui replace les boutons.
    carte.show()
    carte.resize(240, panel._CARD_H)
    QApplication.processEvents()

    fav = carte._fav_btn.geometry()
    don = carte._don_btn.geometry()
    fiche = carte._fiche_btn.geometry()
    assert fiche.right() < don.left() <= don.right() < fav.left()
    assert fav.right() <= carte.width()
    for g in (fav, don, fiche):
        assert g.bottom() <= carte.height()


# ═══════════════════════════════════════════════════════════════════════════
# _SectionHeader et _CardsGrid
# ═══════════════════════════════════════════════════════════════════════════

def test_l_entete_de_section_annonce_son_effectif(qtbot):
    """Le compteur est réécrit à chaque réagencement plutôt que l'en-tête
    recréé : c'est ce qui permet de réutiliser les six widgets de section."""
    entete = panel._SectionHeader("LAN", 3)
    qtbot.addWidget(entete)
    assert entete._count_lbl.text() == "3"
    entete.set_count(17)
    assert entete._count_lbl.text() == "17"


def test_la_grille_range_les_cartes_par_rangees_de_quatre(qtbot, favoris):
    """Quatre colonnes : au-delà, une carte de 168 px devient illisible sur un
    panel partagé avec les autres onglets."""
    grille = panel._CardsGrid()
    qtbot.addWidget(grille)
    cartes = [_carte(qtbot, _S(f"s{i}")) for i in range(6)]
    grille.populate(cartes)
    positions = [grille._layout.getItemPosition(i)[:2]
                 for i in range(grille._layout.count())]
    assert positions == [(0, 0), (0, 1), (0, 2), (0, 3), (1, 0), (1, 1)]


def test_repeupler_la_grille_ne_detruit_pas_les_cartes(qtbot, favoris):
    """Les cartes appartiennent à l'onglet, pas à la grille.

    Les détruire ici reviendrait à refaire 2 900 widgets à chaque tri, alors
    que seul leur ordre a changé.
    """
    grille = panel._CardsGrid()
    qtbot.addWidget(grille)
    cartes = [_carte(qtbot, _S(f"s{i}")) for i in range(4)]
    grille.populate(cartes)
    grille.populate(list(reversed(cartes)))
    assert grille._layout.count() == 4
    assert [grille._layout.itemAt(i).widget() for i in range(4)] == \
        list(reversed(cartes))


# ═══════════════════════════════════════════════════════════════════════════
# _StreamersTab
# ═══════════════════════════════════════════════════════════════════════════

DONNEES = [
    _S("anyme023", viewers=11_900, location="LAN", game="Minecraft"),
    _S("jltomy", viewers=8_200, location="Online", game="Just Chatting"),
    _S("mistermv", viewers=5_000, donation=1500.0, location="LAN", game="TFT"),
    _S("low4n", viewers=617, location="Online", game="Warzone"),
    _S("horty", viewers=0, location="LAN", game="", online=False),
]


@pytest.fixture
def onglet(qtbot, favoris):
    o = panel._StreamersTab()
    qtbot.addWidget(o)
    o.resize(1200, 800)
    _rafraichir(o, DONNEES)
    return o


def _rafraichir(onglet, streamers, selection=()) -> None:
    """refresh() puis le tour de boucle que la reconstruction différée attend."""
    onglet.refresh(list(streamers), list(selection))
    QApplication.processEvents()


def _cartes_visibles(onglet, grille) -> list[str]:
    """Logins affichés dans une grille de section, dans l'ordre posé."""
    lay = grille._layout
    return [lay.itemAt(i).widget()._login for i in range(lay.count())]


def test_les_sections_separent_la_lan_du_reste_et_les_eteints(onglet):
    """Trois sections, parce qu'on ne cherche pas la même chose dans chacune :
    la LAN d'abord, le reste du direct ensuite, l'inaccessible en dernier."""
    assert _cartes_visibles(onglet, onglet._grid_lan) == ["anyme023", "mistermv"]
    assert _cartes_visibles(onglet, onglet._grid_online) == ["jltomy", "low4n"]
    assert _cartes_visibles(onglet, onglet._grid_off) == ["horty"]


def test_une_section_vide_disparait_au_lieu_de_se_superposer(qtbot, favoris):
    """L'en-tête n'est que retiré du layout, pas détruit : sans masquage
    explicite il continuait de se peindre à sa dernière position, et les
    titres se chevauchaient."""
    o = panel._StreamersTab()
    qtbot.addWidget(o)
    _rafraichir(o, [_S("solo", location="LAN")])
    assert o._hdr_lan.isHidden() is False
    assert o._hdr_online.isHidden() is True
    assert o._hdr_off.isHidden() is True


def test_le_tri_par_defaut_met_les_grosses_audiences_en_tete(onglet):
    """C'est ce qu'on regarde d'abord quand on compose une grille."""
    assert _cartes_visibles(onglet, onglet._grid_lan)[0] == "anyme023"


def test_le_tri_alphabetique_sert_a_retrouver_quelqu_un(onglet):
    """Il est indépendant de la casse : « MisterMV » ne doit pas se retrouver
    avant « anyme023 » sous prétexte d'une majuscule."""
    onglet._sort_combo.setCurrentIndex(1)
    assert _cartes_visibles(onglet, onglet._grid_lan) == ["anyme023", "mistermv"]
    assert _cartes_visibles(onglet, onglet._grid_online) == ["jltomy", "low4n"]


def test_le_tri_par_cagnotte_classe_les_montants(onglet):
    onglet._sort_combo.setCurrentIndex(2)
    assert _cartes_visibles(onglet, onglet._grid_lan) == ["mistermv", "anyme023"]


def test_la_recherche_porte_aussi_sur_le_jeu(onglet):
    """Trois cents participants sans moyen de chercher : il fallait faire
    défiler à l'œil. On cherche autant « qui joue à quoi » que « qui »."""
    onglet._search.setText("warzone")
    assert _cartes_visibles(onglet, onglet._grid_online) == ["low4n"]
    assert onglet._grid_lan.isHidden()


@pytest.mark.parametrize("saisie", ["MISTER", "mistermv", "  MisterMV  "])
def test_la_recherche_ignore_la_casse_et_les_espaces(onglet, saisie):
    """On tape vite, pendant l'event, souvent avec une majuscule de trop."""
    onglet._search.setText(saisie)
    assert _cartes_visibles(onglet, onglet._grid_lan) == ["mistermv"]


def test_le_compteur_du_filtre_ne_s_affiche_que_s_il_filtre(onglet):
    """Un « 5 / 5 » permanent n'apprend rien et occupe la barre."""
    assert onglet._filter_count.text() == ""
    onglet._search.setText("m")
    assert onglet._filter_count.text().endswith("/ 5")


def test_un_filtre_sans_resultat_le_dit(onglet):
    """Une page vide sans un mot laisse croire à un chargement qui n'arrive
    jamais."""
    onglet._search.setText("personne-de-ce-nom")
    assert not onglet._empty_lbl.isHidden()
    assert onglet._hdr_lan.isHidden()


def test_la_bascule_en_ligne_ecarte_les_chaines_eteintes(onglet):
    onglet._toggles["online"].setChecked(True)
    assert onglet._grid_off.isHidden()
    assert onglet._filter_count.text() == "4 / 5"


def test_la_bascule_lan_garde_aussi_ceux_qui_sont_hors_ligne(onglet):
    """Le filtre porte sur le LIEU, pas sur l'état du direct : les deux
    bascules se combinent, chacune sur son critère."""
    onglet._toggles["lan"].setChecked(True)
    assert _cartes_visibles(onglet, onglet._grid_off) == ["horty"]
    assert onglet._grid_online.isHidden()


def test_la_bascule_favoris_ne_garde_que_les_etoiles(onglet, favoris):
    """C'est le seul moyen de retrouver sa dizaine de chaînes habituelles dans
    trois cents participants."""
    favoris.logins = {"low4n"}
    onglet._toggles["fav"].setChecked(True)
    assert _cartes_visibles(onglet, onglet._grid_online) == ["low4n"]
    assert onglet._grid_lan.isHidden()


def test_le_filtre_par_jeu_ne_propose_que_des_jeux_joues(onglet):
    """La liste est bâtie sur les chaînes EN LIGNE : proposer le jeu d'un
    streamer éteint ne rendrait jamais aucun résultat."""
    jeux = [onglet._game_combo.itemData(i)
            for i in range(1, onglet._game_combo.count())]
    assert jeux == ["Just Chatting", "Minecraft", "TFT", "Warzone"]


def test_le_jeu_choisi_survit_a_l_arrivee_de_nouvelles_donnees(onglet):
    """La liste est réassemblée à chaque changement de structure ; sans
    précaution, le filtre en cours se remettait sur « Tous les jeux » toutes
    les trente secondes."""
    onglet._game_combo.setCurrentIndex(onglet._game_combo.findData("TFT"))
    assert _cartes_visibles(onglet, onglet._grid_lan) == ["mistermv"]
    _rafraichir(onglet, DONNEES + [_S("nouveau", game="Rust", viewers=5)])
    assert onglet._game_combo.currentData() == "TFT"
    assert _cartes_visibles(onglet, onglet._grid_lan) == ["mistermv"]


def test_une_carte_ecartee_par_le_filtre_est_masquee(onglet):
    """Elle reste enfant de sa grille : sans masquage explicite, elle
    continuerait de se peindre par-dessus les cartes retenues."""
    onglet._search.setText("mistermv")
    assert onglet._card_map["anyme023"].isHidden()
    assert not onglet._card_map["mistermv"].isHidden()


# ── sélection pour la grille ─────────────────────────────────────────────────

def test_les_slots_se_numerotent_dans_l_ordre_des_clics(onglet):
    """Le numéro est la position dans la grille : c'est l'ordre de sélection
    qui la fixe, pas l'ordre d'affichage."""
    onglet._on_card_toggled("low4n", True)
    onglet._on_card_toggled("anyme023", True)
    assert onglet._card_map["low4n"]._slot == 1
    assert onglet._card_map["anyme023"]._slot == 2


def test_retirer_une_chaine_renumerote_les_suivantes(onglet):
    """Sans renumérotation, la grille garderait un trou et les numéros
    afficheraient 1 et 3 pour deux cellules côte à côte."""
    for lg in ("low4n", "anyme023", "jltomy"):
        onglet._on_card_toggled(lg, True)
    onglet._on_card_toggled("anyme023", False)
    assert onglet._card_map["low4n"]._slot == 1
    assert onglet._card_map["jltomy"]._slot == 2
    assert onglet._card_map["anyme023"]._slot is None


def test_la_selection_est_annoncee_a_la_grille(onglet):
    """C'est ce signal qui ouvre et ferme les instances MPV."""
    recu = []
    onglet.grid_selection_changed.connect(recu.append)
    onglet._on_card_toggled("mistermv", True)
    assert recu == [["mistermv"]]


def test_le_plafond_refuse_une_chaine_de_plus(onglet):
    """Vingt-cinq instances MPV, c'est le budget VCN du GPU. Au-delà, la
    demande est refusée SANS toucher à la sélection en cours — un signal
    émis ici ferait rouvrir toute la grille pour rien.
    """
    onglet.set_max_streams(2)
    onglet._on_card_toggled("low4n", True)
    onglet._on_card_toggled("anyme023", True)
    recu = []
    onglet.grid_selection_changed.connect(recu.append)
    onglet._on_card_toggled("jltomy", True)
    assert onglet._selected == ["low4n", "anyme023"]
    assert recu == []


def test_le_compteur_annonce_le_plafond_courant(onglet):
    """Il est réglé dans les paramètres : afficher 25 quand l'utilisateur a
    demandé 9 lui ferait croire qu'il peut en ajouter seize de plus."""
    onglet.set_max_streams(9)
    assert onglet._counter_lbl.text() == "Grille : 0 / 9"
    onglet._on_card_toggled("low4n", True)
    assert onglet._counter_lbl.text() == "Grille : 1 / 9"


def test_un_plafond_nul_reste_a_une_chaine(onglet):
    """Zéro stream autorisé rendrait la grille inutilisable et le panel
    incapable d'en rouvrir un seul."""
    onglet.set_max_streams(0)
    assert onglet.MAX_SELECTED == 1


def test_tout_selectionner_s_arrete_au_plafond_et_ignore_les_eteints(onglet):
    """« Tous » veut dire « tous ceux qui tiennent, et qui diffusent »."""
    onglet.set_max_streams(3)
    onglet._select_all()
    assert onglet._selected == ["anyme023", "jltomy", "mistermv"]


def test_tout_deselectionner_vide_la_grille(onglet):
    onglet._select_all()
    recu = []
    onglet.grid_selection_changed.connect(recu.append)
    onglet._deselect_all()
    assert recu == [[]]
    assert all(c._slot is None for c in onglet._card_map.values())


@pytest.mark.parametrize("login", ["horty", "inconnu"])
def test_on_n_ajoute_pas_une_chaine_qu_on_ne_peut_pas_ouvrir(onglet, login):
    """`add_login` vient de la timeline et de la palette, où l'on peut nommer
    une chaîne éteinte ou disparue depuis le dernier sondage."""
    onglet.add_login(login)
    assert onglet._selected == []


def test_ajouter_deux_fois_la_meme_chaine_ne_la_double_pas(onglet):
    """Le clic sur un show de la timeline peut viser un présentateur déjà
    présent dans la grille."""
    onglet.add_login("low4n")
    onglet.add_login("low4n")
    assert onglet._selected == ["low4n"]


def test_ajouter_au_dela_du_plafond_ne_fait_rien(onglet):
    onglet.set_max_streams(1)
    onglet.add_login("low4n")
    onglet.add_login("anyme023")
    assert onglet._selected == ["low4n"]


def test_une_chaine_qui_coupe_son_direct_quitte_la_selection(onglet):
    """Sinon son slot resterait occupé par une cellule noire, et le compteur
    annoncerait une grille pleine alors qu'il reste de la place."""
    onglet._on_card_toggled("low4n", True)
    eteint = [_S("low4n", online=False, location="Online")] + \
        [s for s in DONNEES if s.twitch_login != "low4n"]
    _rafraichir(onglet, eteint, selection=["low4n"])
    assert onglet._selected == []


# ── réutilisation des cartes ─────────────────────────────────────────────────

def test_seuls_les_viewers_qui_bougent_ne_declenchent_pas_de_reconstruction(
        onglet):
    """La structure est l'empreinte {(login, en ligne)} : tant qu'elle ne
    change pas, il n'y a rien à réagencer. Le voyant « Chargement… » ne doit
    donc même pas apparaître.
    """
    avant = onglet._card_map["low4n"]
    bouges = [_S(s.twitch_login, online=s.online, viewers=s.viewers + 10,
                 location=s.location, game=s.game) for s in DONNEES]
    onglet.refresh(bouges, [])
    assert onglet._content_stack.currentIndex() == 1, "pas de voyant"
    assert onglet._card_map["low4n"] is avant
    assert avant._viewers_badge.text() == panel._fmt_viewers(627)


def test_un_changement_de_structure_montre_le_voyant_de_chargement(onglet):
    """Refaire trois cents cartes prend le temps qu'il faut : le dire vaut
    mieux que de laisser l'interface figée sans explication."""
    onglet.refresh(DONNEES[:3], [])
    assert onglet._content_stack.currentIndex() == 0
    QApplication.processEvents()
    assert onglet._content_stack.currentIndex() == 1


def test_une_carte_survit_a_un_reagencement(onglet):
    """La version précédente détruisait les 300 cartes dès qu'UN streamer
    changeait d'état — 785 ms mesurées, à chaque cycle de l'event."""
    avant = onglet._card_map["mistermv"]
    _rafraichir(onglet, DONNEES + [_S("nouveau", viewers=1)])
    assert onglet._card_map["mistermv"] is avant


def test_une_chaine_qui_change_d_etat_voit_sa_carte_refaite(onglet):
    """La carte porte sa couleur, son curseur et son badge selon l'état en
    ligne : la retoucher au lieu de la refaire laisserait une carte grisée
    cliquable, ou l'inverse."""
    avant = onglet._card_map["horty"]
    rallume = [_S("horty", online=True, viewers=42, location="LAN")] + \
        [s for s in DONNEES if s.twitch_login != "horty"]
    _rafraichir(onglet, rallume)
    assert onglet._card_map["horty"] is not avant
    assert onglet._card_map["horty"]._online is True


def test_une_chaine_disparue_perd_sa_carte(onglet):
    """Une carte gardée en mémoire pour un login absent de l'API fuit à
    chaque édition."""
    _rafraichir(onglet, [s for s in DONNEES if s.twitch_login != "low4n"])
    assert "low4n" not in onglet._card_map


# ── favoris relayés par l'onglet ─────────────────────────────────────────────

def test_l_onglet_relaie_l_etoile_d_une_carte(onglet):
    """C'est ce relais qui met la touche du Stream Deck à jour."""
    recu = []
    onglet.favori_change.connect(lambda lg, fav: recu.append((lg, fav)))
    onglet._card_map["mistermv"]._fav_btn.click()
    assert recu == [("mistermv", True)]


def test_un_favori_pose_ailleurs_repeint_la_carte(onglet, favoris):
    """Le favori se pose aussi au clavier et depuis le boîtier : sans cette
    resynchronisation, la carte affiche l'inverse de la vérité."""
    carte = onglet._card_map["jltomy"]
    assert carte._fav_btn.toolTip() == "Mettre en favori"
    favoris.logins.add("jltomy")
    onglet.rafraichir_favori("jltomy")
    assert carte._fav_btn.toolTip() == "Retirer des favoris"


def test_un_favori_pose_sur_une_chaine_inconnue_ne_leve_pas(onglet):
    """Le boîtier peut nommer une chaîne absente de la liste courante."""
    onglet.rafraichir_favori("jamais-vu")


def test_l_onglet_relaie_la_demande_de_fiche_d_une_carte(onglet):
    """La fenêtre ne connaît pas les cartes : c'est l'onglet qui fait le pont
    jusqu'à l'ouverture de la fiche."""
    recu = []
    onglet.sheet_requested.connect(recu.append)
    onglet._card_map["low4n"]._fiche_btn.click()
    assert recu == ["low4n"]


# ── dispositions enregistrées ────────────────────────────────────────────────

class _FauxStore:
    """Un SelectionStore en mémoire, pour ne pas écrire dans config.json."""

    def __init__(self, presets=None) -> None:
        self._presets = dict(presets or {})

    def presets(self):
        return {k: list(v) for k, v in self._presets.items()}

    def save_preset(self, nom, logins):
        self._presets[nom] = list(logins)
        return True

    def delete_preset(self, nom):
        return self._presets.pop(nom, None) is not None


@pytest.fixture
def store(monkeypatch):
    """Substitue le magasin de dispositions dans core.selection_store."""
    from core import selection_store
    faux = _FauxStore()
    monkeypatch.setattr(selection_store, "SelectionStore", lambda: faux)
    return faux


def test_charger_une_disposition_remplace_la_selection(qtbot, favoris, store):
    """Recocher vingt-cinq cases à chaque changement de contexte : c'est
    exactement ce que les dispositions évitent."""
    store._presets["Soirée"] = ["mistermv", "low4n"]
    o = panel._StreamersTab()
    qtbot.addWidget(o)
    _rafraichir(o, DONNEES)
    recu = []
    o.grid_selection_changed.connect(recu.append)
    o._preset_combo.setCurrentIndex(o._preset_combo.findData("Soirée"))
    o._on_preset_chosen(0)
    assert o._selected == ["mistermv", "low4n"]
    assert recu == [["mistermv", "low4n"]]


def test_une_disposition_d_hier_ne_ressuscite_pas_les_absents(qtbot, favoris,
                                                              store):
    """Les participants changent d'une édition à l'autre : un login inconnu
    occuperait un slot pour une cellule qui ne s'ouvrira jamais."""
    store._presets["Vieille"] = ["mistermv", "parti-en-2025"]
    o = panel._StreamersTab()
    qtbot.addWidget(o)
    _rafraichir(o, DONNEES)
    o._preset_combo.setCurrentIndex(o._preset_combo.findData("Vieille"))
    o._on_preset_chosen(0)
    assert o._selected == ["mistermv"]


def test_enregistrer_sans_selection_ne_cree_pas_de_disposition_vide(onglet,
                                                                    store):
    """Une disposition vide se rechargerait en éteignant toute la grille."""
    onglet._on_preset_save()
    assert store.presets() == {}


# ═══════════════════════════════════════════════════════════════════════════
# _ProgrammeTab
# ═══════════════════════════════════════════════════════════════════════════

def _ev(nom: str, jour: str, debut: str = "16:00", fin: str = "17:00",
        *, id_: str = "", hotes=(), parts=(), noms=None, logins=None,
        start_ts: float = 0.0):
    return panel.EventItem(
        id=id_, name=nom, day=jour, start_local=debut, end_local=fin,
        description="", host_uuids=list(hotes), participant_uuids=list(parts),
        start_ts=start_ts, end_ts=0.0, names=dict(noms or {}),
        logins=dict(logins or {}), profile_urls={},
    )


@pytest.fixture
def programme(qtbot, monkeypatch):
    """Onglet Programme, sans écriture des rappels ni toast flottant."""
    monkeypatch.setattr(panel, "_load_reminders", lambda: set())
    monkeypatch.setattr(panel, "_save_reminders", lambda keys: None)
    o = panel._ProgrammeTab()
    qtbot.addWidget(o)
    o.resize(1000, 700)
    return o


@pytest.mark.parametrize("jour,attendu", [
    ("2026-09-04", "Vendredi 4"),      # jour de l'édition, libellé fixé
    ("2026-10-01", "Jeudi 1"),         # hors édition : calculé
    ("pas-une-date", "pas-une-date"),  # illisible : rendu tel quel
])
def test_le_bouton_de_jour_porte_un_libelle_lisible(jour, attendu):
    """Une date ISO sur un bouton oblige à compter les jours de tête."""
    assert panel._ProgrammeTab._short_day_label(jour) == attendu


def test_tous_les_jours_de_l_edition_restent_proposes(programme):
    """Se limiter aux jours ayant déjà des événements faisait DISPARAÎTRE un
    jour dont le programme n'est pas encore publié — samedi s'était ainsi
    volatilisé entre vendredi et dimanche."""
    programme.update_events([_ev("Lancement", "2026-09-04")])
    assert list(programme._day_btns) == panel._PROG_DAYS_ORDERED


def test_un_jour_apporte_par_l_api_s_ajoute_aux_jours_connus(programme):
    """Les dates de l'édition sont codées en dur : l'API doit pouvoir en
    ajouter une sans qu'on republie l'application."""
    programme.update_events([_ev("Avant-première", "2026-09-02")])
    assert "2026-09-02" in programme._day_btns


def test_un_jour_sans_evenement_le_dit(programme):
    """Une page blanche laisse croire que le programme n'a pas chargé."""
    programme.update_events([_ev("Lancement", "2026-09-04")])
    programme._select_day("2026-09-05")
    assert "Aucun événement pour ce jour" in _textes(programme._content)


def test_le_jour_choisi_se_distingue_des_autres(programme):
    """Sans marquage, on ne sait plus ce qu'on regarde après un clic."""
    programme.update_events([_ev("Lancement", "2026-09-04")])
    programme._select_day("2026-09-05")
    assert programme._day_btns["2026-09-05"].styleSheet() == panel._BTN_ACTIVE
    assert programme._day_btns["2026-09-04"].styleSheet() == panel._BTN_INACTIVE


def test_les_evenements_du_jour_s_affichent_dans_l_ordre_horaire(programme):
    """On lit le programme de haut en bas pour savoir ce qui vient ensuite ;
    l'API ne garantit aucun ordre."""
    programme.update_events([
        _ev("Cloture", "2026-09-04", "22:00", "23:00"),
        _ev("Lancement", "2026-09-04", "16:00", "16:10"),
    ])
    programme._select_day("2026-09-04")
    textes = _textes(programme._content)
    assert textes.index("Lancement") < textes.index("Cloture")


def test_l_heure_d_un_show_s_affiche_a_la_francaise(programme):
    """L'API rend « 16:00 » ; on lit « 16h00 » en France, et la durée évite de
    soustraire de tête."""
    programme.update_events([_ev("Lancement", "2026-09-04", "16:00", "16:10")])
    programme._select_day("2026-09-04")
    textes = _textes(programme._content)
    assert "16h00" in textes
    assert "10min" in textes


def test_un_show_sans_le_moindre_intervenant_le_signale(programme):
    """Une carte muette laisse croire à un bug d'affichage plutôt qu'à une
    donnée que l'API n'a pas encore publiée."""
    programme.update_events([_ev("Mystère", "2026-09-04")])
    programme._select_day("2026-09-04")
    assert "Participants non disponibles" in _textes(programme._content)


# ── résolution des intervenants ──────────────────────────────────────────────

def test_le_nom_porte_par_le_show_prime_sur_le_mapping(programme):
    """Les invités non-streamers — GIMS, Bigflo et Oli — n'existent pas dans
    /participations : leur nom ne vient que de la charge du show."""
    programme.set_gdoc_map({"u1": "Ancien nom"}, {})
    assert programme._resolve(["u1"], {"u1": "GIMS"}) == ["GIMS"]


def test_le_mapping_global_sert_quand_le_show_ne_nomme_personne(programme):
    programme.set_gdoc_map({"u1": "ZeratoR"}, {"u1": "zerator"})
    assert programme._resolve(["u1"]) == ["ZeratoR"]


def test_un_uuid_inconnu_s_affiche_tronque_plutot_que_rien(programme):
    """Au premier affichage, le mapping n'est pas encore arrivé : montrer une
    ligne vide donnerait un show sans participants."""
    assert programme._resolve(["0123456789abcdef"]) == ["0123456789ab…"]


def test_un_uuid_inconnu_disparait_des_qu_un_autre_est_connu(programme):
    """Le repli ne s'applique qu'à défaut de TOUT : mêler un nom lisible et un
    uuid brut sur la même carte serait pire que de taire l'inconnu."""
    programme.set_gdoc_map({"u1": "ZeratoR"}, {})
    assert programme._resolve(["u1", "inconnu-xyz"]) == ["ZeratoR"]


def test_le_login_d_un_invite_vient_du_show_avant_le_mapping(programme):
    """C'est le login qui décide de l'avatar : celui du show est le plus
    récent, et le seul disponible pour un invité hors ZEvent."""
    programme.set_gdoc_map({"u1": "ZeratoR"}, {"u1": "ancien_login"})
    gens = programme._resolve_people(["u1"], logins={"u1": "zerator"})
    assert gens == [("ZeratoR", "zerator", "")]


def test_un_intervenant_sans_avatar_garde_sa_place(programme):
    """Sans login ni URL, la carte tombe sur la puce texte plutôt que d'omettre
    la personne."""
    gens = programme._resolve_people(["u1"], names={"u1": "Bigflo et Oli"})
    assert gens == [("Bigflo et Oli", "", "")]


@pytest.mark.parametrize("id_,attendu", [
    ("01a025bc", "01a025bc"),
    ("", "2026-09-04_16:00_Lancement"),
])
def test_un_show_sans_identifiant_reste_identifiable(programme, id_, attendu):
    """C'est cette clé qui porte l'abonnement au rappel : si elle changeait
    d'un sondage à l'autre, le rappel serait perdu."""
    assert programme._event_key(
        _ev("Lancement", "2026-09-04", id_=id_)) == attendu


# ── intervenants sur la carte d'un show ──────────────────────────────────────

def _pastilles(racine: QWidget) -> list[str]:
    """Les initiales des avatars ronds posés sous `racine`."""
    return [lbl.text() for lbl in racine.findChildren(QLabel)
            if lbl.width() == panel._PERSON_AV_SZ
            and lbl.height() == panel._PERSON_AV_SZ]


def test_un_streamer_est_montre_par_son_avatar(programme):
    """Un nom seul demande de connaître la personne ; la photo se lit d'un
    coup d'œil, et tient bien plus serré qu'une puce texte."""
    programme.update_events([
        _ev("Show", "2026-09-04", parts=["u1"], noms={"u1": "ZeratoR"},
            logins={"u1": "zerator"})])
    programme._select_day("2026-09-04")
    assert "ZE" in _pastilles(programme._content)


def test_un_invite_sans_compte_twitch_garde_une_puce_nommee(programme):
    """Les artistes invités n'ont ni login ni avatar : la puce texte reste la
    seule façon de les nommer sur la carte."""
    programme.update_events([
        _ev("Concert", "2026-09-04", parts=["u9"], noms={"u9": "GIMS"})])
    programme._select_day("2026-09-04")
    assert "GIMS" in _textes(programme._content)


def test_au_dela_de_huit_participants_la_carte_renvoie_a_la_liste(programme):
    """Au-delà, les intervenants feraient déborder la carte sur deux lignes et
    casseraient l'alignement des paires."""
    noms = {f"u{i}": f"Streamer{i}" for i in range(11)}
    programme.update_events([
        _ev("Grand show", "2026-09-04", parts=list(noms), noms=noms,
            logins={k: k for k in noms})])
    programme._select_day("2026-09-04")
    assert len(_pastilles(programme._content)) == \
        panel._ProgrammeTab._MONTRES_SUR_LA_CARTE
    assert "voir les 3 autres…" in _textes_de_boutons(programme._content)


def test_au_dela_de_huit_animateurs_la_carte_les_compte(programme):
    """Les animateurs sont peu nombreux d'habitude ; quand ils ne le sont pas,
    le « +N » évite de repousser les participants hors de la carte."""
    noms = {f"h{i}": f"Hote{i}" for i in range(10)}
    programme.update_events([
        _ev("Plateau", "2026-09-04", hotes=list(noms), noms=noms,
            logins={k: k for k in noms})])
    programme._select_day("2026-09-04")
    assert "+2" in _textes_de_boutons(programme._content)


def _textes_de_boutons(racine: QWidget) -> list[str]:
    return [b.text() for b in racine.findChildren(QPushButton)]


def test_le_lien_ouvre_la_liste_complete_des_intervenants(programme,
                                                          monkeypatch):
    """C'est le seul endroit où l'on voit tout le plateau d'un show."""
    ouvertes = []

    class _FausseListe:
        def __init__(self, nom, parts, hosts, parent=None) -> None:
            ouvertes.append((nom, len(parts), len(hosts)))

        def exec(self) -> None:
            pass

    monkeypatch.setattr(panel, "_ParticipantsDialog", _FausseListe)
    noms = {f"u{i}": f"Streamer{i}" for i in range(11)}
    programme.update_events([
        _ev("Grand show", "2026-09-04", parts=list(noms), noms=noms,
            logins={k: k for k in noms})])
    programme._select_day("2026-09-04")
    lien = next(b for b in programme._content.findChildren(QPushButton)
                if "autres" in b.text())
    lien.click()
    assert ouvertes == [("Grand show", 11, 0)]


# ── rendu différé ────────────────────────────────────────────────────────────

def test_un_mapping_inchange_ne_refait_pas_le_programme(programme):
    """Le mapping est reconstruit toutes les 30 s mais il est identique la
    quasi-totalité du temps : le rejouer détruisait et recréait ~650 widgets
    pour rien, sur un onglet le plus souvent caché."""
    programme.update_events([_ev("Lancement", "2026-09-04")])
    rendus = []
    programme._render_deferred = lambda: rendus.append(1)
    programme.set_gdoc_map({"u1": "ZeratoR"}, {"u1": "zerator"})
    programme.set_gdoc_map({"u1": "ZeratoR"}, {"u1": "zerator"})
    assert rendus == [1]


def test_un_onglet_cache_attend_d_etre_montre_pour_se_redessiner(qtbot,
                                                                 programme):
    """Repeindre un onglet que personne ne regarde coûte le même prix que s'il
    était visible, trente secondes durant."""
    programme.update_events([_ev("Lancement", "2026-09-04")])
    assert not programme.isVisible()
    programme.set_gdoc_map({"u1": "ZeratoR"}, {})
    assert programme._needs_render is True
    programme.show()
    qtbot.waitExposed(programme)
    assert programme._needs_render is False


# ── rappels ──────────────────────────────────────────────────────────────────

@pytest.fixture
def rappels(programme, monkeypatch):
    """Programme dont les toasts sont neutralisés et les écritures capturées.

    Le toast est une fenêtre `Qt.Tool` qui se ferme d'elle-même six secondes
    plus tard : la laisser vivre ferait flotter une fenêtre au milieu des
    tests suivants.
    """
    ecrits = []
    monkeypatch.setattr(panel, "_save_reminders",
                        lambda keys: ecrits.append(set(keys)))
    monkeypatch.setattr(panel, "_ReminderToast",
                        lambda msg, parent: type("_Muet", (), {
                            "show_near": lambda self, p: None})())
    programme.ecrits = ecrits
    return programme


def test_un_rappel_pose_est_ecrit_sur_le_disque(rappels):
    """Ils n'étaient qu'en mémoire, donc perdus à chaque redémarrage — gênant
    sur un event de trois jours où l'application est forcément relancée."""
    bouton = QPushButton()
    rappels._toggle_reminder("cle-1", bouton, _ev("Show", "2026-09-04"))
    assert rappels._subscribed_ids == {"cle-1"}
    assert rappels.ecrits == [{"cle-1"}]


def test_se_desabonner_efface_le_rappel(rappels):
    bouton = QPushButton()
    ev = _ev("Show", "2026-09-04")
    rappels._toggle_reminder("cle-1", bouton, ev)
    rappels._toggle_reminder("cle-1", bouton, ev)
    assert rappels._subscribed_ids == set()
    assert rappels.ecrits[-1] == set()


def test_se_reabonner_redonne_droit_a_un_rappel(rappels):
    """Sans purge, un événement déjà rappelé restait marqué à vie : se
    désabonner puis se réabonner ne redéclenchait plus rien."""
    bouton = QPushButton()
    ev = _ev("Show", "2026-09-04")
    rappels._reminded_ids.add("cle-1")
    rappels._toggle_reminder("cle-1", bouton, ev)
    assert "cle-1" not in rappels._reminded_ids


def _dans(secondes: float):
    import time
    return time.time() + secondes


def test_un_show_qui_approche_declenche_son_rappel(rappels):
    """Cinq minutes, c'est le temps de finir ce qu'on fait et de basculer."""
    rappels._subscribed_ids = {"k"}
    rappels._events = [_ev("Lancement", "2026-09-04", id_="k",
                           start_ts=_dans(180))]
    recu = []
    rappels.reminder_triggered.connect(lambda nom, msg: recu.append((nom, msg)))
    rappels._check_reminders()
    assert recu and recu[0][0] == "Lancement"
    assert "commence dans 2 min" in recu[0][1]


def test_un_show_qui_vient_de_commencer_le_dit_au_present(rappels):
    rappels._subscribed_ids = {"k"}
    rappels._events = [_ev("Lancement", "2026-09-04", id_="k",
                           start_ts=_dans(10))]
    recu = []
    rappels.reminder_triggered.connect(lambda nom, msg: recu.append(msg))
    rappels._check_reminders()
    assert "commence maintenant" in recu[0]


def test_un_show_commence_depuis_une_minute_le_dit_au_passe(rappels):
    """Le rappel peut arriver en retard — l'application vient d'être lancée,
    ou le sondage a sauté un tour. Annoncer « commence dans » serait faux."""
    rappels._subscribed_ids = {"k"}
    rappels._events = [_ev("Lancement", "2026-09-04", "16:00", id_="k",
                           start_ts=_dans(-30))]
    recu = []
    rappels.reminder_triggered.connect(lambda nom, msg: recu.append(msg))
    rappels._check_reminders()
    assert "a commencé à 16h00" in recu[0]


def test_un_rappel_ne_se_declenche_qu_une_fois(rappels):
    """La vérification tourne toutes les 30 s : sans mémoire, le même show
    déclencherait dix toasts sur sa fenêtre de cinq minutes."""
    rappels._subscribed_ids = {"k"}
    rappels._events = [_ev("Lancement", "2026-09-04", id_="k",
                           start_ts=_dans(120))]
    recu = []
    rappels.reminder_triggered.connect(lambda nom, msg: recu.append(msg))
    rappels._check_reminders()
    rappels._check_reminders()
    assert len(recu) == 1


def test_un_show_non_souscrit_ne_derange_personne(rappels):
    rappels._events = [_ev("Lancement", "2026-09-04", id_="k",
                           start_ts=_dans(120))]
    recu = []
    rappels.reminder_triggered.connect(lambda nom, msg: recu.append(msg))
    rappels._check_reminders()
    assert recu == []


@pytest.mark.parametrize("depart", [0.0, 6000.0])
def test_un_horaire_absent_ou_lointain_ne_declenche_rien(rappels, depart):
    """`start_ts` vaut 0 quand l'API n'a pas donné d'horaire exploitable :
    le prendre au pied de la lettre rappellerait un show de 1970."""
    rappels._subscribed_ids = {"k"}
    ts = depart if depart == 0.0 else _dans(depart)
    rappels._events = [_ev("Lancement", "2026-09-04", id_="k", start_ts=ts)]
    recu = []
    rappels.reminder_triggered.connect(lambda nom, msg: recu.append(msg))
    rappels._check_reminders()
    assert recu == []


# ═══════════════════════════════════════════════════════════════════════════
# PanelWindow
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def fenetre(qtbot, monkeypatch, favoris):
    """Panel complet, jamais montré : `show_on_init` déclencherait le plein
    écran et la bannière de démarrage."""
    monkeypatch.setattr(panel, "QWebEngineView", _FausseVueWeb)
    w = panel.PanelWindow(QApplication.primaryScreen(), show_grid_tab=True,
                          show_on_init=False)
    qtbot.addWidget(w)
    return w


ONGLETS = ["Accueil", "Programme", "Stats", "Goals", "Clips",
           "Streamers", "Mixer"]


def test_la_barre_porte_les_onglets_attendus(fenetre):
    """Leur ORDRE est celui des index de la pile : intervertir deux noms
    afficherait le contenu de l'un sous le titre de l'autre."""
    assert [b.text() for b in fenetre._tab_btns] == ONGLETS + ["Grille"]


def test_l_onglet_grille_n_existe_qu_en_mode_multi_ecrans(qtbot, monkeypatch,
                                                          favoris):
    """En mode deux écrans il n'y a pas de fenêtre grille à faire venir."""
    monkeypatch.setattr(panel, "QWebEngineView", _FausseVueWeb)
    w = panel.PanelWindow(QApplication.primaryScreen(), show_on_init=False)
    qtbot.addWidget(w)
    assert [b.text() for b in w._tab_btns] == ONGLETS


@pytest.mark.parametrize("nom,index", list(zip(ONGLETS, range(6))))
def test_chaque_onglet_montre_sa_page(fenetre, nom, index):
    fenetre.switch_to_tab(nom)
    assert fenetre._stack.currentIndex() == index
    assert [i for i, b in enumerate(fenetre._tab_btns) if b.isChecked()] == \
        [index], "un seul onglet marqué actif, celui qu'on regarde"


def test_cliquer_un_bouton_d_onglet_change_de_page(fenetre):
    """Le chemin réel : le bouton ne connaît pas son index, la fenêtre le
    retrouve en cherchant l'émetteur du signal."""
    fenetre._tab_btns[3].click()
    assert fenetre._stack.currentIndex() == 3


def test_changer_d_onglet_par_son_nom_ignore_la_grille(fenetre):
    """« Grille » ne désigne pas une page mais un basculement de fenêtre :
    l'activer depuis la palette de commandes cacherait le panel sans que
    l'utilisateur l'ait demandé."""
    fenetre.switch_to_tab("Stats")
    fenetre.switch_to_tab("Grille")
    assert fenetre._stack.currentIndex() == 2


def test_un_nom_d_onglet_inconnu_ne_change_rien(fenetre):
    """La palette de commandes propose des noms libres."""
    fenetre.switch_to_tab("Goals")
    fenetre.switch_to_tab("Onglet-Fantôme")
    assert fenetre._stack.currentIndex() == 3


def test_cliquer_l_onglet_grille_fait_venir_la_fenetre_grille(qtbot, fenetre):
    """C'est le mode deux écrans : panel et grille se partagent le même
    moniteur, l'un s'efface quand l'autre paraît."""
    class _FausseGrille(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.appelee = False

        def showFullScreen(self) -> None:   # type: ignore[override]
            self.appelee = True

    grille = _FausseGrille()
    qtbot.addWidget(grille)
    fenetre.set_grid_window(grille)
    fenetre._set_active(len(ONGLETS))
    assert grille.appelee is True
    assert fenetre.isHidden()


def test_l_onglet_grille_sans_fenetre_associee_ne_plante_pas(fenetre):
    """En mode trois écrans, la grille a son propre moniteur et n'est jamais
    associée au panel."""
    fenetre.switch_to_tab("Stats")
    fenetre._set_active(len(ONGLETS))
    assert fenetre._stack.currentIndex() == 2


# ── palette de commandes et clavier ──────────────────────────────────────────

def _touche(cle, modificateurs=Qt.KeyboardModifier.NoModifier):
    from PyQt6.QtGui import QKeyEvent
    from PyQt6.QtCore import QEvent
    return QKeyEvent(QEvent.Type.KeyPress, cle, modificateurs)


def test_ctrl_k_ouvre_la_palette_de_commandes(fenetre):
    """En régie, la souris sert au reste : tout doit être atteignable au
    clavier depuis n'importe quel onglet."""
    fenetre.keyPressEvent(_touche(Qt.Key.Key_K,
                                  Qt.KeyboardModifier.ControlModifier))
    assert not fenetre._palette.isHidden()


def test_echap_referme_la_palette_avant_de_fermer_la_fenetre(qtbot, fenetre):
    """Sans cette priorité, sortir de la palette éteindrait ZLink en plein
    event.

    La fenêtre est montrée pour de bon : le tri se fait sur `isVisible()` de
    la palette, qui répond faux tant que son parent n'est pas affiché.
    """
    fenetre.show()
    qtbot.waitExposed(fenetre)
    fenetre.keyPressEvent(_touche(Qt.Key.Key_K,
                                  Qt.KeyboardModifier.ControlModifier))
    fenetre.keyPressEvent(_touche(Qt.Key.Key_Escape))
    assert fenetre._palette.isHidden()
    assert fenetre.isVisible(), "la fenêtre elle-même ne doit pas se fermer"


def test_une_action_du_plein_ecran_est_relayee_a_main(fenetre):
    """Le panel ne connaît pas la fenêtre de lecture : clip et replay
    remontent à main.py, qui relie les deux."""
    recu = []
    fenetre.action_requested.connect(recu.append)
    fenetre._on_palette_action("clip")
    assert recu == ["clip"]


def test_le_recapitulatif_s_ouvre_sans_passer_par_main(fenetre, monkeypatch):
    """Il ne parle que de la session du panel : le faire transiter par main.py
    ferait un aller-retour pour rien."""
    from windows import recap
    ouverts = []

    class _FauxRecap:
        def __init__(self, parent=None) -> None:
            ouverts.append(parent)

        def exec(self) -> None:
            pass

    monkeypatch.setattr(recap, "RecapDialog", _FauxRecap)
    recu = []
    fenetre.action_requested.connect(recu.append)
    fenetre._on_palette_action("recap")
    assert ouverts == [fenetre]
    assert recu == [], "le récap ne concerne pas le plein écran"


# ── badge de version ─────────────────────────────────────────────────────────

def test_une_mise_a_jour_disponible_se_lit_dans_l_en_tete(fenetre):
    """En régie, on ouvre un ticket ou on envoie une capture sans avoir le
    temps de fouiller les paramètres : la version est collée au logo."""
    avant = fenetre._version_lbl.text()
    fenetre.set_update_available("9.9.9", "https://github.com/x/y/releases/tag/9")
    assert fenetre._version_lbl.text() == f"{avant} → 9.9.9"
    assert fenetre._version_url.endswith("/tag/9")


def test_une_adresse_de_version_hors_github_est_ignoree(fenetre):
    """L'URL vient du flux de mise à jour : la suivre aveuglément ouvrirait le
    navigateur de l'utilisateur sur ce que ce flux voudrait."""
    fenetre.set_update_available("9.9.9", "https://ailleurs.test/piege")
    assert fenetre._version_url.startswith("https://github.com/")


# ── signaux exposés par la fenêtre ───────────────────────────────────────────

def test_l_etoile_d_une_carte_remonte_jusqu_a_la_fenetre(fenetre, favoris):
    """Le chemin complet du bug : carte → onglet → fenêtre → Stream Deck.
    Chacun des trois maillons a manqué à un moment ou un autre."""
    fenetre.update_streamers(list(DONNEES), [])
    QApplication.processEvents()
    recu = []
    fenetre.favori_change.connect(lambda lg, fav: recu.append((lg, fav)))
    fenetre._streamers_tab._card_map["mistermv"]._fav_btn.click()
    assert recu == [("mistermv", True)]


def test_la_selection_de_la_grille_remonte_jusqu_a_la_fenetre(fenetre):
    """C'est ce que main.py écoute pour ouvrir et fermer les instances MPV."""
    fenetre.update_streamers(list(DONNEES), [])
    QApplication.processEvents()
    recu = []
    fenetre.grid_selection_changed.connect(recu.append)
    fenetre._streamers_tab._on_card_toggled("low4n", True)
    assert recu == [["low4n"]]


def test_un_favori_venu_du_boitier_repeint_la_carte(fenetre, favoris):
    """La fenêtre est le point d'entrée de la télécommande : c'est par elle
    que le panel apprend un favori posé sur le Stream Deck."""
    fenetre.update_streamers(list(DONNEES), [])
    QApplication.processEvents()
    favoris.logins.add("jltomy")
    fenetre.rafraichir_favori("jltomy")
    carte = fenetre._streamers_tab._card_map["jltomy"]
    assert carte._fav_btn.toolTip() == "Retirer des favoris"


def test_les_niveaux_de_mixage_sont_lisibles_depuis_la_fenetre(fenetre):
    """La télécommande demande l'état des tranches pour allumer ses touches ;
    elle ne connaît que la fenêtre."""
    fenetre.set_main_stream("zerator")
    fenetre.set_pinned_audio(["mistermv"])
    fenetre.regler_mixage("mistermv", 40)
    fenetre.couper_mixage("mistermv", True)
    niveaux = fenetre.niveaux_de_mixage()
    assert niveaux["mistermv"] == (40, True)


def test_le_plafond_des_reglages_arrive_jusqu_aux_cartes(fenetre):
    """Le réglage « streams simultanés » ne sert à rien s'il ne borne pas la
    sélection : la grille ouvrirait plus de flux que le GPU n'en tient."""
    recu = []
    fenetre.settings_changed.connect(recu.append)
    fenetre._on_settings_changed({"max_active_streams": 6})
    assert fenetre._streamers_tab.MAX_SELECTED == 6
    assert recu == [{"max_active_streams": 6}]


def test_des_reglages_sans_plafond_retombent_sur_une_valeur_sure(fenetre):
    """Une configuration ancienne peut ne pas porter la clé."""
    fenetre._on_settings_changed({})
    assert fenetre._streamers_tab.MAX_SELECTED == 20


# ── fiche d'un participant ───────────────────────────────────────────────────

def test_la_fiche_d_un_inconnu_ne_s_ouvre_pas(fenetre):
    """La demande peut venir de la palette ou d'un clic sur un show, avec un
    login absent du dernier sondage : ouvrir une fiche vide serait pire."""
    fenetre.open_streamer_sheet("jamais-vu")   # ne doit rien ouvrir ni lever


def test_la_fiche_recoit_les_objectifs_deja_connus(fenetre, monkeypatch):
    """Ils sont chargés à la demande pour l'onglet Goals ; les redemander à
    l'ouverture de la fiche referait un appel réseau pour rien."""
    vues = {}

    class _FausseFiche:
        def __init__(self, st, goals, events, parent=None) -> None:
            vues["login"] = st.twitch_login
            vues["goals"] = goals
            self.stream_requested = _SignalMuet()
            self.grid_requested = _SignalMuet()

        def exec(self) -> None:
            vues["ouverte"] = True

    from windows import streamer_sheet
    monkeypatch.setattr(streamer_sheet, "StreamerSheet", _FausseFiche)
    fenetre.update_streamers(list(DONNEES), [])
    QApplication.processEvents()
    fenetre.update_goals_cache({"mistermv": ["objectif-1"]})
    fenetre.open_streamer_sheet("mistermv")
    assert vues["login"] == "mistermv"
    assert vues["goals"] == ["objectif-1"]
    assert vues["ouverte"] is True


class _SignalMuet:
    """Un pyqtSignal en trompe-l'œil : la fiche en expose deux, la fenêtre
    les connecte, et rien de plus n'est exercé ici."""

    def connect(self, _slot) -> None:
        pass


# ── recouvrements plein cadre ────────────────────────────────────────────────

def test_le_mode_big_screen_efface_les_onglets(fenetre):
    """Il sert d'affichage de régie : le laisser sous la barre d'onglets
    donnerait une vue amputée de 100 px sur un écran dédié."""
    fenetre._toggle_bigscreen(True)
    assert fenetre._tab_bar.isHidden() and fenetre._stack.isHidden()
    assert not fenetre._bigscreen.isHidden()
    fenetre._close_bigscreen()
    assert not fenetre._tab_bar.isHidden()
    assert fenetre._bigscreen.isHidden()
    assert fenetre._bigscreen_btn.isChecked() is False


def test_les_reglages_se_referment_sur_les_onglets(fenetre):
    """Le bouton du header est bistable : le fermer depuis le panneau
    lui-même doit relever la coche, sinon le clic suivant ne rouvre rien."""
    fenetre._toggle_settings(True)
    assert fenetre._stack.isHidden()
    fenetre._close_settings()
    assert not fenetre._stack.isHidden()
    assert fenetre._settings_btn.isChecked() is False


# ── données propagées ────────────────────────────────────────────────────────

def test_une_seule_passe_alimente_tous_les_onglets(fenetre):
    """`update_data` existe pour éviter le double rafraîchissement de
    l'Accueil quand streamers et stats arrivent coup sur coup."""
    stats = panel.GlobalStats(1000.0, "1 000 €", 25_000, "live")
    fenetre.update_data(list(DONNEES), stats, ["mistermv"])
    QApplication.processEvents()
    assert fenetre._streamers_tab._selected == ["mistermv"]
    assert fenetre._last_stats is stats


def test_le_programme_de_la_fenetre_recoit_les_evenements(fenetre):
    evenements = [_ev("Lancement", "2026-09-04")]
    fenetre.update_events(evenements)
    assert fenetre._programme_tab._events == evenements


def test_les_objectifs_bruts_sont_gardes_pour_la_fiche(fenetre):
    """Ils transitent par la fenêtre : l'onglet Goals les affiche, la fiche
    d'un participant les réutilise sans nouvel appel."""
    fenetre.update_goals_cache({"mistermv": ["g1", "g2"]})
    assert fenetre._goals_raw == {"mistermv": ["g1", "g2"]}
    fenetre.update_goals_cache(None)
    assert fenetre._goals_raw == {}


# ── réglage abaissé alors que la grille est pleine ───────────────────────────

def test_abaisser_le_plafond_rogne_la_selection_en_cours(onglet):
    """Le plafond existe pour le budget d'encodage du GPU.

    L'utilisateur qui le descend de 25 à 3 en pleine session le fait parce que
    sa machine ne suit plus ; laisser vingt-cinq flux ouverts ne répond pas à
    sa demande, et le compteur annonce alors « 5 / 3 ».
    """
    onglet.set_max_streams(25)
    onglet._select_all()
    # Le nombre est celui du jeu d'essai — seules les chaînes EN LIGNE sont
    # sélectionnables — et non une valeur écrite en dur, qui casserait le
    # jour où une chaîne de plus est ajoutée à la fixture.
    combien = len(onglet._selected)
    assert combien >= 2, "il faut de quoi rogner pour que le test ait un sens"

    onglet.set_max_streams(combien - 1)
    assert len(onglet._selected) == combien - 1
    assert onglet.MAX_SELECTED == combien - 1
