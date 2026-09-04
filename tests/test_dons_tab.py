# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Onglet Dons — le fil des donations poussé par le flux temps réel.

Rien n'est demandé au réseau : l'onglet ne fait que recevoir ce que
`FluxCagnotte` lui donne. Les dons ci-dessous ont la forme exacte de ceux
relevés sur le vrai flux.
"""

from __future__ import annotations

import time
from collections import deque

import pytest

from windows import panel


def _don(id_="1", montant=10, donateur="Cedo", streamer="MiiOrca",
         commentaire=None, quand="2026-09-04T16:34:10+00:00") -> dict:
    return {"id": id_, "donor": donateur, "amount": montant, "currency": "EUR",
            "comment": commentaire, "streamer": streamer, "createdAt": quand}


@pytest.fixture
def onglet(qtbot):
    o = panel._DonsTab()
    qtbot.addWidget(o)
    return o


def _lignes(onglet) -> int:
    """Le stretch final occupe une position et n'est pas un don."""
    return onglet._liste.count() - 1


# ── alimentation ─────────────────────────────────────────────────────────────

def test_un_don_apparait_dans_le_fil(onglet):
    onglet.ajouter_don(_don())
    _vider_la_file(onglet)
    assert _lignes(onglet) == 1


def _textes(ligne) -> list[str]:
    """Les libellés d'une ligne de don, dans l'ordre où ils sont posés."""
    from PyQt6.QtWidgets import QLabel

    return [lb.text() for lb in ligne.findChildren(QLabel)]


def _en_tete(onglet) -> list[str]:
    """Les libellés de la première ligne du fil."""
    return _textes(onglet._liste.itemAt(0).widget())


def test_le_dernier_don_arrive_en_tete(onglet):
    """C'est un fil de direct : ce qui vient d'arriver se lit en haut."""
    onglet.ajouter_don(_don("1", donateur="premier"))
    onglet.ajouter_don(_don("2", donateur="second"))
    _vider_la_file(onglet)
    assert "second" in _en_tete(onglet)


def test_l_historique_se_pose_sous_le_direct(onglet):
    """Le snapshot est du passé : il ne doit pas recouvrir un don en cours."""
    onglet.ajouter_don(_don("direct", donateur="maintenant"))
    _vider_la_file(onglet)
    onglet.poser_historique([_don("vieux1"), _don("vieux2")])
    assert _lignes(onglet) == 3
    assert "maintenant" in _en_tete(onglet)


def test_un_don_deja_vu_n_est_pas_reaffiche(onglet):
    """Une reconnexion renvoie un snapshot qui recouvre des dons déjà reçus
    en direct : sans dédoublonnage ils apparaîtraient deux fois."""
    onglet.ajouter_don(_don("42"))
    _vider_la_file(onglet)
    onglet.poser_historique([_don("42"), _don("43")])
    assert _lignes(onglet) == 2


def test_un_don_sans_identifiant_passe_quand_meme(onglet):
    """Mieux vaut un doublon possible qu'un don escamoté."""
    onglet.ajouter_don({"donor": "sans id", "amount": 5})
    onglet.ajouter_don({"donor": "sans id", "amount": 5})
    _vider_la_file(onglet)
    assert _lignes(onglet) == 2


@pytest.mark.parametrize("charge", [None, "texte", 42, {"pas": "un don"}])
def test_une_charge_inattendue_ne_plante_pas(onglet, charge):
    """La donnée vient d'un navigateur : on ne suppose rien de sa forme."""
    onglet.ajouter_don(charge)
    onglet.poser_historique(charge)


# ── plafond ──────────────────────────────────────────────────────────────────

def test_le_fil_est_plafonne(onglet):
    """Onze mille dons feraient onze mille widgets, et la fenêtre se figerait."""
    for i in range(panel._DonsTab._MAX_LIGNES + 40):
        onglet.ajouter_don(_don(str(i)))
    _vider_la_file(onglet)
    assert _lignes(onglet) == panel._DonsTab._MAX_LIGNES


def test_l_elagage_garde_les_plus_recents(onglet):
    """Ce sont les anciens qui partent, pas ceux qu'on vient de recevoir."""
    for i in range(panel._DonsTab._MAX_LIGNES + 5):
        onglet.ajouter_don(_don(str(i), donateur=f"don{i}"))
    _vider_la_file(onglet)
    assert f"don{panel._DonsTab._MAX_LIGNES + 4}" in _en_tete(onglet)


# ── mise en forme d'une ligne ────────────────────────────────────────────────

def test_l_heure_est_rendue_en_heure_locale():
    """Le flux date en UTC : afficher l'horodatage brut montrerait 16 h pour
    un don de 18 h."""
    from datetime import datetime, timezone
    attendu = (datetime(2026, 9, 4, 16, 34, tzinfo=timezone.utc)
               .astimezone().strftime("%H:%M"))
    assert panel._heure_du_don(_don()) == attendu


@pytest.mark.parametrize("brut", ["", "pas une date", None])
def test_une_date_illisible_ne_rend_rien(brut):
    assert panel._heure_du_don({"createdAt": brut}) == ""


@pytest.mark.parametrize("brut,attendu", [
    ({"amount": 100}, 100.0),
    ({"amount": "12.5"}, 12.5),
    ({"amount": None}, 0.0),
    ({}, 0.0),
    ({"amount": "beaucoup"}, 0.0),
])
def test_le_montant_est_lu_sans_jamais_lever(brut, attendu):
    assert panel._montant_du_don(brut) == attendu


def test_un_gros_don_est_marque(qtbot):
    """C'est ce qu'on cherche du regard dans un fil qui défile."""
    grosse = panel._LigneDon(_don(montant=500))
    petite = panel._LigneDon(_don(montant=2))
    qtbot.addWidget(grosse)
    qtbot.addWidget(petite)
    assert "border-left" in grosse.styleSheet()
    assert "border-left" not in petite.styleSheet()


def test_le_style_de_la_ligne_ne_deborde_pas_sur_ses_etiquettes(qtbot):
    """Une règle nue s'applique à toute la descendance : le liseré vert se
    répétait sur chaque étiquette au lieu de border la carte."""
    ligne = panel._LigneDon(_don(montant=500, commentaire="coucou"))
    qtbot.addWidget(ligne)
    feuille = ligne.styleSheet()
    assert feuille.startswith("QFrame#ligneDon {"), "sélecteur non ciblé"
    from PyQt6.QtWidgets import QLabel
    for etiquette in ligne.findChildren(QLabel):
        assert "border" not in etiquette.styleSheet()


def test_le_commentaire_est_affiche_quand_il_y_en_a_un(qtbot):
    """C'est ce qu'on lit à l'antenne."""
    ligne = panel._LigneDon(_don(commentaire="Pour la quiet room"))
    qtbot.addWidget(ligne)
    assert "Pour la quiet room" in _textes(ligne)


# ── état du flux ─────────────────────────────────────────────────────────────

def test_l_etat_dit_si_le_flux_est_vivant(onglet):
    onglet.ajouter_don(_don())
    onglet.signaler_etat(True)
    assert "direct" in onglet._etat.text()
    onglet.signaler_etat(False)
    assert "hors ligne" in onglet._etat.text()


# ── filtres ──────────────────────────────────────────────────────────────────

def _visibles(onglet) -> list[str]:
    """Les donateurs des lignes effectivement montrées."""
    montrees = [lg for lg in onglet._lignes() if not lg.isHidden()]
    return [t for lg in montrees for t in _textes(lg) if t.startswith("don")]


def _vider_la_file(onglet):
    """Pose tout ce qui attend et force le rafraîchissement.

    Deux reports sont en jeu, tous deux voulus : les dons sont ÉGRENÉS un par
    un pour qu'on les voie arriver, et les recomptes sont REGROUPÉS pour ne pas
    reparcourir deux cents lignes à chaque don. Les tests ne font pas tourner
    de boucle d'événements : ils déclenchent les deux à la main.
    """
    while onglet._en_attente:
        onglet._egrener()
    onglet._egrenage.stop()
    onglet._regroupe.stop()
    onglet._rafraichir()


def _peupler(onglet):
    onglet.ajouter_don(_don("1", montant=1, donateur="donA", streamer="Ponce"))
    onglet.ajouter_don(_don("2", montant=100, donateur="donB", streamer="Ponce"))
    onglet.ajouter_don(_don("3", montant=50, donateur="donC", streamer="Joyca"))
    _vider_la_file(onglet)


def test_sans_filtre_tout_le_fil_est_montre(onglet):
    _peupler(onglet)
    assert len(_visibles(onglet)) == 3


def test_le_seuil_ecarte_les_petits_dons(onglet):
    _peupler(onglet)
    onglet._filtre_montant.setCurrentIndex(
        panel._DonsTab._SEUILS.index(50.0))
    assert sorted(_visibles(onglet)) == ["donB", "donC"]


def test_le_filtre_streamer_ne_garde_que_lui(onglet):
    _peupler(onglet)
    onglet._filtre_streamer.setCurrentIndex(
        onglet._filtre_streamer.findText("Ponce"))
    assert sorted(_visibles(onglet)) == ["donA", "donB"]


def test_les_deux_filtres_se_cumulent(onglet):
    _peupler(onglet)
    onglet._filtre_streamer.setCurrentIndex(
        onglet._filtre_streamer.findText("Ponce"))
    onglet._filtre_montant.setCurrentIndex(
        panel._DonsTab._SEUILS.index(50.0))
    assert _visibles(onglet) == ["donB"]


def test_un_filtre_pose_cache_ce_qui_est_deja_la(onglet):
    """Sinon il ne prendrait effet qu'au don suivant — plusieurs minutes sur
    un seuil élevé."""
    _peupler(onglet)
    assert len(_visibles(onglet)) == 3
    onglet._filtre_montant.setCurrentIndex(
        panel._DonsTab._SEUILS.index(500.0))
    assert _visibles(onglet) == []


def test_un_don_ecarte_n_apparait_pas_puis_ne_disparait(onglet):
    """Il naît filtré : l'insérer visible puis le cacher le ferait clignoter."""
    onglet._filtre_montant.setCurrentIndex(
        panel._DonsTab._SEUILS.index(100.0))
    onglet.ajouter_don(_don("petit", montant=1, donateur="donPetit"))
    _vider_la_file(onglet)
    assert onglet._lignes()[0].isHidden() is True


def test_la_liste_des_streamers_suit_le_fil(onglet):
    _peupler(onglet)
    proposes = [onglet._filtre_streamer.itemText(i)
                for i in range(onglet._filtre_streamer.count())]
    assert proposes == [panel._DonsTab._TOUS, "Joyca", "Ponce"]


def test_le_streamer_filtre_reste_propose_apres_elagage(onglet):
    """Son dernier don sort du fil : la sélection ne doit pas retomber sur
    « Tous », sinon le filtre s'annule et on se croit devant un fil complet."""
    onglet.ajouter_don(_don("seul", streamer="Ephemere"))
    _vider_la_file(onglet)
    onglet._filtre_streamer.setCurrentIndex(
        onglet._filtre_streamer.findText("Ephemere"))
    for i in range(panel._DonsTab._MAX_LIGNES + 5):
        onglet.ajouter_don(_don(f"x{i}", streamer="Ponce"))
    _vider_la_file(onglet)
    assert onglet._filtre_streamer.currentText() == "Ephemere"
    assert _visibles(onglet) == []


def test_l_etat_dit_combien_sont_montres_sur_le_total(onglet):
    _peupler(onglet)
    onglet.signaler_etat(True)
    assert onglet._etat.text().startswith("3 derniers dons")
    onglet._filtre_montant.setCurrentIndex(
        panel._DonsTab._SEUILS.index(100.0))
    assert onglet._etat.text().startswith("1 sur 3 dons")


# ── cadence et animation ─────────────────────────────────────────────────────

def test_une_rafale_ne_recompte_qu_une_fois(onglet, qtbot):
    """Deux parcours de deux cents lignes PAR DON faisaient saccader le fil."""
    tours = []
    onglet._regroupe.timeout.connect(lambda: tours.append(1))
    for i in range(30):
        onglet.ajouter_don(_don(str(i)))
    assert tours == [], "rien ne doit être recompté pendant la rafale"
    qtbot.waitUntil(lambda: bool(tours), timeout=2000)
    assert len(tours) == 1


def test_un_afflux_continu_ne_prive_jamais_le_rafraichissement(onglet, qtbot):
    """Le piège du regroupement : `start()` sur un timer actif le REPOUSSE.

    Les dons tombent parfois plus vite que la fenêtre de regroupement. Avec un
    report, le rafraîchissement aurait été renvoyé à plus tard indéfiniment —
    liste des streamers et compteur figés pendant toute la vague, c'est-à-dire
    au moment précis où on les regarde.
    """
    tours = []
    onglet._regroupe.timeout.connect(lambda: tours.append(1))
    fin = time.monotonic() + 0.5
    i = 0
    while time.monotonic() < fin:          # afflux ininterrompu
        onglet.ajouter_don(_don(str(i), streamer=f"chaine{i % 7}"))
        i += 1
        qtbot.wait(1)
    assert tours, "le rafraîchissement n'a jamais eu lieu pendant l'afflux"


def test_quand_ca_afflue_les_fondus_s_effacent(onglet):
    """Un fondu par don ferait autant de rendus hors écran simultanés : le fil
    ralentirait là où il doit aller vite, et dix fondus superposés ne se
    lisent pas."""
    from PyQt6.QtCore import QPropertyAnimation

    for i in range(20):                    # aussi vite que Python le permet
        onglet.ajouter_don(_don(str(i)))
    _vider_la_file(onglet)
    animes = [lg for lg in onglet._lignes()
              if lg.findChildren(QPropertyAnimation)]
    assert len(animes) <= 1, "les fondus doivent s'effacer sous l'afflux"


def test_le_fondu_est_parente_a_sa_ligne(onglet):
    """Une animation qui survivrait à sa cible écrirait dans un effet libéré :
    segfault, pas exception. Le fil élague en permanence."""
    from PyQt6.QtCore import QPropertyAnimation

    onglet.ajouter_don(_don("1"))
    _vider_la_file(onglet)
    ligne = onglet._lignes()[0]
    animations = ligne.findChildren(QPropertyAnimation)
    assert animations, "aucun fondu posé sur la ligne"
    assert all(a.parent() is ligne for a in animations)


def test_l_historique_n_est_pas_anime(onglet):
    """Voir cent dons déjà passés apparaître un à un ferait croire à une
    rafale qui n'a pas lieu."""
    from PyQt6.QtCore import QPropertyAnimation

    onglet.poser_historique([_don("a"), _don("b")])
    for ligne in onglet._lignes():
        assert ligne.findChildren(QPropertyAnimation) == []


def test_un_don_filtre_n_est_pas_anime(onglet):
    """Animer ce qu'on ne montre pas coûte un rendu hors écran pour rien."""
    from PyQt6.QtCore import QPropertyAnimation

    onglet._filtre_montant.setCurrentIndex(
        panel._DonsTab._SEUILS.index(500.0))
    onglet.ajouter_don(_don("petit", montant=1))
    _vider_la_file(onglet)
    assert onglet._lignes()[0].findChildren(QPropertyAnimation) == []


def test_l_echelle_des_seuils_monte_jusqu_a_dix_mille(onglet):
    """Un ZEvent voit passer des dons à cinq chiffres : s'arrêter à 500 €
    laissait sans filtre la moitié haute, celle qu'on cherche."""
    assert max(panel._DonsTab._SEUILS) == 10000.0
    assert list(panel._DonsTab._SEUILS) == sorted(panel._DonsTab._SEUILS)
    assert onglet._filtre_montant.count() == len(panel._DonsTab._SEUILS)


def test_les_milliers_sont_espaces_dans_les_libelles(onglet):
    """« ≥ 10000 € » ne se lit pas d'un coup d'œil sur un fil qui défile."""
    libelles = [onglet._filtre_montant.itemText(i)
                for i in range(onglet._filtre_montant.count())]
    assert "≥ 10\u00a0000 €" in libelles


def test_le_plus_haut_seuil_filtre_vraiment(onglet):
    onglet.ajouter_don(_don("gros", montant=12000))
    onglet.ajouter_don(_don("moyen", montant=900))
    _vider_la_file(onglet)
    onglet._filtre_montant.setCurrentIndex(
        panel._DonsTab._SEUILS.index(10000.0))
    assert [lg.montant for lg in onglet._lignes() if not lg.isHidden()] == [12000.0]


# ── égrenage ─────────────────────────────────────────────────────────────────

def test_un_paquet_de_dons_n_apparait_pas_d_un_bloc(onglet):
    """Le flux livre tout ce qui s'est accumulé depuis le vidage précédent.
    Poser le paquet d'un coup fait surgir cinq lignes ensemble puis plus rien :
    c'est l'à-coup qu'on prenait pour de la lenteur."""
    for i in range(6):
        onglet.ajouter_don(_don(str(i)))
    assert _lignes(onglet) == 1, "un seul posé, le reste attend son tour"
    assert len(onglet._en_attente) == 5


def test_le_premier_don_ne_fait_pas_antichambre(onglet):
    """Sur un fil calme, attendre la cadence d'égrenage se verrait."""
    onglet.ajouter_don(_don())
    assert _lignes(onglet) == 1


def test_la_file_se_vide_don_par_don(onglet, qtbot):
    for i in range(4):
        onglet.ajouter_don(_don(str(i)))
    qtbot.waitUntil(lambda: not onglet._en_attente, timeout=3000)
    assert _lignes(onglet) == 4


def test_une_grappe_sort_par_petits_paquets_jamais_d_un_bloc(onglet):
    """Mesuré sur le vrai flux : le serveur pousse par grappes de trente à
    soixante. Un plafond au-delà duquel on vidait tout était franchi à chaque
    grappe — et tout retombait d'un bloc, ce qu'on voulait éviter."""
    for i in range(50):
        onglet.ajouter_don(_don(str(i)))
    restant = len(onglet._en_attente)
    onglet._egrener()                      # un seul tour de timer
    pose = restant - len(onglet._en_attente)
    assert 1 < pose < 10, f"{pose} posés d'un coup : ni filet, ni bloc"


def test_une_grappe_s_ecoule_en_quelques_secondes(onglet):
    """Une règle purement proportionnelle décroît géométriquement : la file
    fond vite puis traîne, et une grappe de cinquante mettait six secondes."""
    for i in range(50):
        onglet.ajouter_don(_don(str(i)))
    tours = 0
    while onglet._en_attente:
        onglet._egrener()
        tours += 1
    duree_ms = tours * panel._DonsTab._EGRENAGE_MS
    assert duree_ms < 3000, f"{duree_ms} ms pour écouler une grappe de 50"


def test_sous_la_fenetre_le_fil_s_ecrit_ligne_a_ligne(onglet):
    """Le régime normal : un don par tour, pas un paquet."""
    absorbable = (panel._DonsTab._FENETRE_RATTRAPAGE_MS
                  // panel._DonsTab._EGRENAGE_MS)
    for i in range(absorbable):
        onglet.ajouter_don(_don(str(i)))
    restant = len(onglet._en_attente)
    onglet._egrener()
    assert restant - len(onglet._en_attente) == 1


def test_un_don_isole_sort_seul(onglet):
    """C'est ce qui donne le fil qui s'écrit ligne à ligne."""
    for i in range(3):
        onglet.ajouter_don(_don(str(i)))
    restant = len(onglet._en_attente)
    onglet._egrener()
    assert restant - len(onglet._en_attente) == 1


def test_l_egrenage_s_arrete_quand_la_file_est_vide(onglet):
    """Un timer qui tourne pour rien réveille l'application vingt fois par
    seconde toute la soirée."""
    onglet.ajouter_don(_don())
    onglet._egrener()
    assert onglet._egrenage.isActive() is False


def test_l_historique_ne_passe_pas_par_la_file(onglet):
    """Cent dons passés égrenés un par un mettraient sept secondes à
    s'afficher, et donneraient l'illusion d'une rafale."""
    onglet.poser_historique([_don(str(i)) for i in range(10)])
    assert onglet._en_attente == deque()
    assert _lignes(onglet) == 10
