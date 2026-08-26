# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Fiche d'un participant : série de cagnotte, mise en forme, contenu affiché.

Deux choses se jouent dans ce module et sont testées ici.

La SÉRIE d'abord : `note_donation` est appelée à chaque sondage, pour chacun
des streamers, pendant une session qui peut durer quatre jours. Elle doit donc
refuser les points qui n'apprennent rien et rester bornée en mémoire.

La MISE EN FORME ensuite : la fiche affiche des euros et des audiences avec un
séparateur de milliers, tolère un streamer dont la moitié des champs sont
vides, et tronque les listes longues. C'est ce qui est vérifié — pas la
disposition des cadres.

Note d'import : `windows.panel` est importé en tête de fichier, avant que la
fixture `qapp` n'existe. Il tire QtWebEngineWidgets, que Qt exige de charger
AVANT la création du QApplication. Ne pas le déplacer dans une fonction.
"""

from __future__ import annotations

import pytest
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QLabel

import windows.fullscreen as fullscreen_module
import windows.panel as panel_module
from core.api_client import DonationGoal, EventItem, StreamerInfo
from core.session_log import SessionLog
from windows import streamer_sheet as fiche


# ── outillage ────────────────────────────────────────────────────────────────

#: Espace fine insécable (U+202F), le séparateur de milliers typographique
#: français. Le module l'emploie pour l'audience, les objectifs et le gain de
#: session — mais pas pour la cagnotte, qui utilise une espace ordinaire
#: (voir test_les_separateurs_de_milliers_sont_homogenes).
_FINE = " "


@pytest.fixture(autouse=True)
def historique_vierge(monkeypatch):
    """La série est un dictionnaire de module : un test ne doit pas hériter
    des points d'un autre."""
    monkeypatch.setattr(fiche, "_HISTORIQUE", {})


@pytest.fixture
def sans_avatar(monkeypatch):
    """Remplace la pastille d'avatar : le vrai loader va chercher l'image."""
    monkeypatch.setattr(
        panel_module, "_make_person_avatar",
        lambda display, login, size=52, ring="", profile_url="": QLabel(""))


@pytest.fixture
def session_vide(monkeypatch):
    """Journal de session neuf : les moments forts d'un autre test pollueraient."""
    journal = SessionLog()
    monkeypatch.setattr("core.session_log.SESSION", journal)
    return journal


def _streamer(**champs) -> StreamerInfo:
    """StreamerInfo avec des valeurs plausibles, surchargeables au cas par cas."""
    defauts = {
        "twitch_login": "zerator", "display": "ZeratoR", "online": True,
        "game": "Just Chatting", "location": "LAN", "viewers": 42_318,
        "donation": 1_234_567.89, "donation_formatted": "",
        "profile_url": "",
    }
    defauts.update(champs)
    return StreamerInfo(**defauts)


def _textes(widget) -> list[str]:
    """Tous les libellés de la fiche, à plat."""
    return [lbl.text() for lbl in widget.findChildren(QLabel)]


def _ouvrir(qtbot, streamer, goals=None, events=None):
    f = fiche.StreamerSheet(streamer, goals, events)
    qtbot.addWidget(f)
    return f


# ── série de cagnotte ────────────────────────────────────────────────────────

def test_serie_vide_au_depart():
    assert fiche.historique("zerator") == []


def test_un_point_est_enregistre():
    fiche.note_donation("zerator", 1000.0)
    serie = fiche.historique("zerator")
    assert len(serie) == 1
    assert serie[0][1] == pytest.approx(1000.0)


@pytest.mark.parametrize("login", ["", None])
def test_un_login_vide_n_ouvre_pas_de_serie(login):
    """Les invités d'un show n'ont pas de compte Twitch.

    Sans ce garde-fou, ils partageraient tous la même série sous la clé "".
    """
    fiche.note_donation(login, 1000.0)
    assert fiche._HISTORIQUE == {}


@pytest.mark.parametrize("suivant,points_attendus", [
    (1000.0, 1),      # identique : rien de neuf
    (1000.4, 1),      # sous le demi-euro : bruit d'arrondi
    (999.6, 1),
    (1000.5, 2),      # au seuil : conservé
    (1500.0, 2),
])
def test_les_points_qui_n_apprennent_rien_sont_ecartes(suivant, points_attendus):
    """L'API republie le même cumul entre deux dons.

    Garder ces répétitions gonflerait la série jusqu'à évincer le début de la
    session, pour une courbe strictement identique.
    """
    fiche.note_donation("zerator", 1000.0)
    fiche.note_donation("zerator", suivant)
    assert len(fiche.historique("zerator")) == points_attendus


def test_une_baisse_est_enregistree():
    """Une cagnotte peut reculer (remboursement) : ne pas perdre le point."""
    fiche.note_donation("zerator", 1000.0)
    fiche.note_donation("zerator", 400.0)
    assert [v for _t, v in fiche.historique("zerator")] == [1000.0, 400.0]


def test_la_serie_est_bornee():
    """Quatre jours de sondages toutes les 30 s ne doivent pas remplir la RAM."""
    for i in range(fiche._MAX_POINTS + 250):
        fiche.note_donation("zerator", float(i) * 10.0)
    assert len(fiche.historique("zerator")) == fiche._MAX_POINTS


def test_la_borne_garde_les_points_les_plus_recents():
    """Une courbe tronquée par la fin ne montrerait plus l'actualité."""
    for i in range(fiche._MAX_POINTS + 10):
        fiche.note_donation("zerator", float(i) * 10.0)
    dernier = fiche.historique("zerator")[-1][1]
    assert dernier == pytest.approx((fiche._MAX_POINTS + 9) * 10.0)


def test_les_series_sont_separees_par_streamer():
    fiche.note_donation("zerator", 1000.0)
    fiche.note_donation("domingo", 2000.0)
    assert [v for _t, v in fiche.historique("domingo")] == [2000.0]


def test_historique_rend_une_copie():
    """L'appelant ne doit pas pouvoir amputer la série interne par mégarde."""
    fiche.note_donation("zerator", 1000.0)
    copie = fiche.historique("zerator")
    copie.clear()
    assert len(fiche.historique("zerator")) == 1


def test_un_montant_entier_devient_un_flottant():
    """Les comparaisons de la série supposent des flottants."""
    fiche.note_donation("zerator", 1000)
    assert isinstance(fiche.historique("zerator")[0][1], float)


# ── tracé ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("points", [
    [],
    [(0.0, 10.0)],                                   # un seul point : pas de ligne
    [(0.0, 10.0), (60.0, 200.0)],
    [(0.0, 10.0), (60.0, 10.0)],                     # série plate : dv nul
    [(5.0, 10.0), (5.0, 90.0)],                      # même instant : dt nul
    [(0.0, 100.0), (60.0, 10.0)],                    # décroissante
])
def test_le_trace_ne_plante_sur_aucune_serie(qtbot, points):
    """Les divisions par l'amplitude sont protégées par un epsilon.

    Une série plate ou instantanée est parfaitement possible — deux sondages
    dans la même seconde, ou aucun don entre les deux.
    """
    courbe = fiche._Courbe(list(points))
    qtbot.addWidget(courbe)
    courbe.resize(200, 90)
    courbe.render(QPixmap(200, 90))


# ── contenu de la fiche ──────────────────────────────────────────────────────

def test_la_cagnotte_preformattee_par_l_api_est_reprise(qtbot, sans_avatar,
                                                        session_vide):
    """L'API publie déjà un montant mis en forme : le refaire pourrait diverger."""
    f = _ouvrir(qtbot, _streamer(donation_formatted="1 234 568 €"))
    assert "1 234 568 €" in _textes(f)


@pytest.mark.parametrize("montant,attendu", [
    (1_234_567.89, "1 234 568 €"),
    (0.0, "0 €"),
    (999.0, "999 €"),
    (1000.0, "1 000 €"),
])
def test_la_cagnotte_est_mise_en_forme_a_defaut(qtbot, sans_avatar,
                                                session_vide, montant, attendu):
    """Sans montant préformaté, la fiche sépare les milliers elle-même.

    « 1234568 € » se lit mal, et c'est le chiffre principal de la fiche.
    """
    f = _ouvrir(qtbot, _streamer(donation=montant, donation_formatted=""))
    assert attendu in _textes(f)


def test_les_separateurs_de_milliers_sont_homogenes(qtbot, sans_avatar,
                                                    session_vide):
    """Cagnotte et audience se lisent l'une sous l'autre, dans le même cadre."""
    f = _ouvrir(qtbot, _streamer(donation=1_234_567.0, viewers=42_318))
    textes = _textes(f)
    assert f"1{_FINE}234{_FINE}567 €" in textes


def test_l_audience_est_affichee_en_direct(qtbot, sans_avatar, session_vide):
    f = _ouvrir(qtbot, _streamer(online=True, viewers=42_318))
    textes = _textes(f)
    assert "Audience" in textes
    assert f"42{_FINE}318" in textes


def test_pas_d_audience_hors_ligne(qtbot, sans_avatar, session_vide):
    """Le dernier compteur connu d'une chaîne éteinte n'a plus de sens."""
    f = _ouvrir(qtbot, _streamer(online=False, viewers=42_318))
    assert "Audience" not in _textes(f)


def test_le_lieu_n_apparait_que_s_il_est_connu(qtbot, sans_avatar,
                                               session_vide):
    """Les participants en ligne n'ont pas de lieu : pas de ligne vide."""
    avec = _textes(_ouvrir(qtbot, _streamer(location="LAN")))
    sans = _textes(_ouvrir(qtbot, _streamer(location="")))
    assert "LAN" in avec
    assert "Lieu" in avec
    assert "Lieu" not in sans


@pytest.mark.parametrize("online,game,attendu", [
    (True, "Minecraft", "en direct · zerator · Minecraft"),
    (True, "", "en direct · zerator"),
    (False, "Minecraft", "hors ligne · zerator"),   # le jeu n'est plus d'actualité
    (False, "", "hors ligne · zerator"),
])
def test_la_ligne_d_etat(qtbot, sans_avatar, session_vide, online, game,
                         attendu):
    f = _ouvrir(qtbot, _streamer(online=online, game=game))
    assert attendu in _textes(f)


def test_le_nom_est_affiche_en_texte_brut(qtbot, sans_avatar, session_vide):
    """Un pseudo est saisi par le streamer : le rendre en HTML serait une
    injection de balises dans la fiche."""
    from PyQt6.QtCore import Qt
    f = _ouvrir(qtbot, _streamer(display="<b>Zera</b>"))
    nom = [lbl for lbl in f.findChildren(QLabel)
           if lbl.text() == "<b>Zera</b>"]
    assert nom
    assert nom[0].textFormat() == Qt.TextFormat.PlainText


# ── objectifs et programme ───────────────────────────────────────────────────

def _goal(nom, montant, fait=False):
    return DonationGoal(id="g", name=nom, amount=montant,
                        accomplished=fait, category="")


def test_un_objectif_atteint_le_dit(qtbot, sans_avatar, session_vide):
    """Un montant en face d'un objectif accompli laisserait croire qu'il reste
    à atteindre."""
    f = _ouvrir(qtbot, _streamer(), goals=[_goal("Rasage", 5000.0, fait=True)])
    textes = _textes(f)
    assert "Rasage" in textes
    assert "atteint" in textes
    assert f"5{_FINE}000 €" not in textes


def test_un_objectif_en_cours_montre_son_montant(qtbot, sans_avatar,
                                                 session_vide):
    f = _ouvrir(qtbot, _streamer(), goals=[_goal("Rasage", 5000.0)])
    assert f"5{_FINE}000 €" in _textes(f)


def test_les_objectifs_sont_tronques(qtbot, sans_avatar, session_vide):
    """Certains streamers en publient des dizaines : la fiche n'est pas la
    liste complète, elle en donne un aperçu."""
    goals = [_goal(f"Objectif {i}", 100.0 * i) for i in range(30)]
    textes = _textes(_ouvrir(qtbot, _streamer(), goals=goals))
    assert "Objectif 11" in textes
    assert "Objectif 12" not in textes


def test_pas_de_section_objectifs_si_aucun(qtbot, sans_avatar, session_vide):
    """Une section vide occupe la place sans rien dire."""
    assert "OBJECTIFS" not in _textes(_ouvrir(qtbot, _streamer(), goals=[]))


def _event(nom, jour="2026-09-05", debut="21:00"):
    return EventItem(id="1", name=nom, day=jour, start_local=debut,
                     end_local="22:00", description="")


def test_le_programme_montre_jour_et_heure(qtbot, sans_avatar, session_vide):
    f = _ouvrir(qtbot, _streamer(), events=[_event("Le Grand Show")])
    textes = _textes(f)
    assert "Le Grand Show" in textes
    assert "2026-09-05 · 21:00" in textes


def test_un_evenement_sans_nom_reste_lisible(qtbot, sans_avatar, session_vide):
    """L'API laisse parfois le nom vide : une ligne sans intitulé serait muette."""
    f = _ouvrir(qtbot, _streamer(), events=[_event("")])
    assert "Événement" in _textes(f)


def test_le_programme_est_tronque(qtbot, sans_avatar, session_vide):
    events = [_event(f"Show {i}") for i in range(20)]
    textes = _textes(_ouvrir(qtbot, _streamer(), events=events))
    assert "Show 9" in textes
    assert "Show 10" not in textes


# ── courbe de session ────────────────────────────────────────────────────────

def test_le_gain_de_session_est_affiche(qtbot, sans_avatar, session_vide):
    """La série commence au lancement de ZLink, pas au début de l'event.

    L'écart affiché doit donc être celui de la session, et le dire.
    """
    fiche.note_donation("zerator", 1000.0)
    fiche.note_donation("zerator", 4500.0)
    f = _ouvrir(qtbot, _streamer())
    assert any(t.startswith(f"+3{_FINE}500 € depuis l'ouverture") for t in _textes(f))


def test_pas_de_gain_annonce_avec_un_seul_point(qtbot, sans_avatar,
                                                session_vide):
    """Un seul relevé ne permet aucun écart : annoncer « +0 € » tromperait."""
    fiche.note_donation("zerator", 1000.0)
    f = _ouvrir(qtbot, _streamer())
    assert not any("depuis l'ouverture" in t for t in _textes(f))


def test_la_courbe_dit_qu_elle_manque_de_points(qtbot, sans_avatar,
                                                session_vide):
    """La section reste affichée même vide : elle explique pourquoi la courbe
    ne commence qu'au lancement, plutôt que de disparaître sans raison."""
    f = _ouvrir(qtbot, _streamer())
    assert "CAGNOTTE DEPUIS LE LANCEMENT" in _textes(f)


# ── moments forts de la session ──────────────────────────────────────────────

def test_les_moments_du_streamer_sont_repris(qtbot, sans_avatar, session_vide):
    session_vide.add_hype("zerator", "Pic de chat", 0.9)
    f = _ouvrir(qtbot, _streamer())
    assert "Pic de chat" in _textes(f)


def test_les_moments_des_autres_sont_ecartes(qtbot, sans_avatar, session_vide):
    """La fiche ne parle que d'une personne : un moment d'une autre chaîne
    n'a rien à y faire."""
    session_vide.add_hype("domingo", "Pic chez Domingo", 0.9)
    f = _ouvrir(qtbot, _streamer())
    assert "Pic chez Domingo" not in _textes(f)


def test_pas_de_section_moments_sans_moment(qtbot, sans_avatar, session_vide):
    assert not any("Moments forts" in t
                   for t in _textes(_ouvrir(qtbot, _streamer())))


def test_les_moments_sont_tronques_et_les_recents_en_tete(qtbot, sans_avatar,
                                                          session_vide):
    """Une session longue accumule les alertes : on montre les dernières,
    la plus récente d'abord."""
    for i in range(12):
        session_vide.add_hype("zerator", f"Moment {i}", 0.9)
    textes = _textes(_ouvrir(qtbot, _streamer()))
    assert "Moment 11" in textes
    assert "Moment 3" not in textes
    assert textes.index("Moment 11") < textes.index("Moment 10")


# ── actions ──────────────────────────────────────────────────────────────────

def test_regarder_demande_le_flux_et_ferme(qtbot, sans_avatar, session_vide):
    from PyQt6.QtWidgets import QPushButton
    f = _ouvrir(qtbot, _streamer())
    recu: list[str] = []
    f.stream_requested.connect(recu.append)
    bouton = next(b for b in f.findChildren(QPushButton)
                  if b.text() == "Regarder")
    bouton.click()
    assert recu == ["zerator"]
    assert f.isVisible() is False


def test_ajouter_a_la_grille_demande_la_grille_et_ferme(qtbot, sans_avatar,
                                                        session_vide):
    from PyQt6.QtWidgets import QPushButton
    f = _ouvrir(qtbot, _streamer())
    recu: list[str] = []
    f.grid_requested.connect(recu.append)
    bouton = next(b for b in f.findChildren(QPushButton)
                  if b.text() == "Ajouter à la grille")
    bouton.click()
    assert recu == ["zerator"]


@pytest.mark.parametrize("url,attendu", [
    ("https://zevent.fr/don/zerator", True),
    ("", False),
])
def test_le_bouton_donner_suit_le_lien_fourni(qtbot, sans_avatar, session_vide,
                                              url, attendu):
    """Sans lien de don, le bouton ouvrirait le vide : il ne doit pas exister."""
    from PyQt6.QtWidgets import QPushButton
    f = _ouvrir(qtbot, _streamer(donation_url=url))
    boutons = [b.text() for b in f.findChildren(QPushButton)]
    assert ("Donner" in boutons) is attendu


def test_donner_ouvre_le_navigateur_avec_le_lien_du_streamer(
        qtbot, sans_avatar, session_vide, monkeypatch):
    """Le don part dans le navigateur du système, jamais dans la vue intégrée :
    l'utilisateur doit voir l'URL réelle avant d'y saisir une carte."""
    from PyQt6.QtWidgets import QPushButton

    demandes: list[str] = []
    monkeypatch.setattr(fullscreen_module, "ouvrir_page_de_don",
                        demandes.append)
    f = _ouvrir(qtbot, _streamer(donation_url="https://zevent.fr/don/zerator"))
    bouton = next(b for b in f.findChildren(QPushButton)
                  if b.text() == "Donner")
    bouton.click()
    assert demandes == ["https://zevent.fr/don/zerator"]
