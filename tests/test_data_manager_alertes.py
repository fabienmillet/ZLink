# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Les garde-fous des alertes du DataManager.

Un ZEvent, c'est trois cents chaînes qui reçoivent des dons en continu pendant
quatre jours. Sans retenue, chaque détecteur produirait un flux ininterrompu et
l'utilisateur couperait tout — ce qui reviendrait à n'avoir aucune alerte.

Chaque test de ce fichier verrouille donc une des retenues : le premier relevé
est muet, une chaîne ne monopolise pas les alertes, un objectif n'est annoncé
qu'une fois, et sous une pluie de dons on garde les plus gros. Ce sont ces
règles-là, et non le calcul brut, qui rendent les alertes supportables.
"""

from __future__ import annotations

import pytest

from core import alerts as _alerts
from core.api_client import DonationGoal, GoalWithStreamer, StreamerInfo
from core.data_manager import (
    _DONATION_ALERT_COOLDOWN_S,
    _DONATION_FLOOD_POLLS,
    DataManager,
)

# Les cooldowns se comparent à time.monotonic() : l'horloge factice doit partir
# au-delà du plus long d'entre eux, faute de quoi tout serait étouffé (c'est
# précisément le sujet de test_cooldown_etouffe_la_premiere_alerte_apres_un_boot).
T0 = 100_000.0

SEUIL = 1000.0


# ── outillage ────────────────────────────────────────────────────────────────

def _streamer(**kw) -> StreamerInfo:
    base = dict(
        twitch_login="zerator", display="ZeratoR", online=True, game="",
        location="", viewers=0, donation=0.0, donation_formatted="",
        profile_url="",
    )
    base.update(kw)
    return StreamerInfo(**base)


def _goal(**kw) -> DonationGoal:
    base = dict(id="g1", name="Piment", amount=10_000.0, accomplished=False,
                category="")
    base.update(kw)
    return DonationGoal(**base)


def _enrichi(**kw) -> GoalWithStreamer:
    base = dict(streamer_login="zerator", streamer_display="ZeratoR",
                goal_name="Piment", amount_target=10_000.0,
                accomplished=False, pct=96.0)
    base.update(kw)
    return GoalWithStreamer(**base)


def _collecte(signal) -> list[tuple]:
    """Branche une liste sur un signal Qt et la retourne pour inspection."""
    recu: list[tuple] = []
    signal.connect(lambda *args: recu.append(args))
    return recu


@pytest.fixture(autouse=True)
def alertes_actives(monkeypatch):
    """Toutes les familles d'alertes actives, quoi qu'ait fait un autre test.

    core.alerts garde son état dans un dictionnaire de module : un test qui
    coupe une famille la couperait pour tous les suivants.
    """
    monkeypatch.setattr(_alerts, "enabled", lambda famille: True)


@pytest.fixture
def dm(qapp):
    """Un DataManager inerte : aucun timer, aucun réseau, aucun thread.

    Les détecteurs sont appelés à la main sur des attributs préremplis — c'est
    la seule façon d'observer les cooldowns sans attendre réellement.
    """
    m = DataManager()
    m.stop_polling()
    m._alert_cfg = {}
    return m


@pytest.fixture
def horloge(monkeypatch):
    """Pilote time.monotonic : les cooldowns se mesurent en secondes réelles."""
    etat = {"t": T0}
    monkeypatch.setattr("core.data_manager.time.monotonic", lambda: etat["t"])
    return etat


# ── _candidat_don : ce qui ne mérite pas d'alerte ────────────────────────────

@pytest.mark.parametrize("champs,motif", [
    (dict(twitch_login="", donation=50_000.0), "sans login, rien à annoncer"),
    (dict(donation="beaucoup"), "cagnotte illisible"),
    (dict(donation=None), "cagnotte absente"),
])
def test_candidat_don_ignore_les_chaines_inexploitables(dm, champs, motif):
    """Une donnée d'API abîmée ne doit pas produire d'alerte, ni d'exception."""
    dm._donations_init_done = True
    dm._prev_donations["zerator"] = 0.0
    assert dm._candidat_don(_streamer(**champs), SEUIL, 300.0, T0) is None, motif


def test_le_premier_releve_d_une_chaine_est_muet(dm):
    """Au lancement, la cagnotte est déjà haute : l'annoncer n'apprendrait rien.

    Le relevé sert uniquement de référence pour mesurer la hausse suivante.
    """
    dm._donations_init_done = True
    assert dm._candidat_don(_streamer(donation=50_000.0), SEUIL, 300.0, T0) is None
    assert dm._prev_donations["zerator"] == pytest.approx(50_000.0)


def test_le_premier_sondage_global_est_muet(dm):
    """Tant que le tout premier sondage n'est pas terminé, rien n'est annoncé.

    Sans ce drapeau, un redémarrage de l'application rejouerait toute la
    journée de dons d'un coup.
    """
    dm._donations_init_done = False
    dm._prev_donations["zerator"] = 0.0
    assert dm._candidat_don(_streamer(donation=50_000.0), SEUIL, 300.0, T0) is None


@pytest.mark.parametrize("avant,apres,attendu", [
    (0.0, 999.0, None),           # sous le seuil : le ZEvent en fait sans arrêt
    (0.0, 1000.0, 1000.0),        # pile au seuil : retenu
    (10_000.0, 12_500.0, 2500.0),
    (10_000.0, 9_000.0, None),    # une cagnotte qui baisse n'est pas un don
])
def test_seuil_porte_sur_l_ecart_entre_deux_sondages(dm, avant, apres, attendu):
    """L'API ne publie qu'un cumul : seule la hausse entre deux relevés parle."""
    dm._donations_init_done = True
    dm._prev_donations["zerator"] = avant
    entree = dm._candidat_don(_streamer(donation=apres), SEUIL, 300.0, T0)
    if attendu is None:
        assert entree is None
    else:
        montant, _s, nature = entree
        assert montant == pytest.approx(attendu)
        assert nature == "don"


def test_une_hausse_sous_le_seuil_interrompt_la_serie(dm):
    """La série mesure une DURÉE : un relevé calme clôt l'épisode en cours.

    Sans cette remise à zéro, trois pics isolés dans la soirée finiraient par
    être présentés comme un bombardement.
    """
    dm._donations_init_done = True
    dm._donation_streak["zerator"] = 2
    dm._donation_run["zerator"] = 4000.0
    dm._prev_donations["zerator"] = 10_000.0

    assert dm._candidat_don(_streamer(donation=10_100.0), SEUIL, 300.0, T0) is None
    assert "zerator" not in dm._donation_streak
    assert "zerator" not in dm._donation_run


# ── _candidat_don : don isolé contre bombardement ────────────────────────────

def test_la_serie_de_sondages_transforme_le_pic_en_bombardement(dm):
    """Depuis un cumul, la durée est le seul indice qui distingue les deux.

    Un don unique fait un pic puis retombe ; un chat qu'on a lancé à l'assaut
    tient le seuil plusieurs relevés d'affilée.
    """
    dm._donations_init_done = True
    dm._prev_donations["zerator"] = 0.0
    natures, montants = [], []
    for i in range(1, _DONATION_FLOOD_POLLS + 1):
        montant, _s, nature = dm._candidat_don(
            _streamer(donation=2000.0 * i), SEUIL, 300.0, T0)
        natures.append(nature)
        montants.append(montant)

    assert natures == ["don"] * (_DONATION_FLOOD_POLLS - 1) + ["bombardement"]
    # Un bombardement s'annonce par son cumul : « +6 000 € », pas « +2 000 € ».
    assert montants[-1] == pytest.approx(2000.0 * _DONATION_FLOOD_POLLS)


def test_le_cooldown_empeche_une_chaine_de_monopoliser_les_alertes(dm):
    """Une grosse chaîne franchit le seuil à presque chaque relevé."""
    dm._donations_init_done = True
    dm._prev_donations["zerator"] = 0.0
    dm._donation_alert_at[("zerator", "don")] = T0

    assert dm._candidat_don(_streamer(donation=5000.0), SEUIL, 300.0,
                            T0 + 299.0) is None
    assert dm._candidat_don(_streamer(donation=10_000.0), SEUIL, 300.0,
                            T0 + 301.0) is not None


def test_le_cooldown_vaut_par_nature_pas_par_chaine(dm):
    """Un bombardement qui s'installe après un pic est un AUTRE événement.

    Le taire sous prétexte qu'on vient d'annoncer un don sur cette chaîne
    ferait manquer le seul moment qui valait la bascule.
    """
    dm._donations_init_done = True
    dm._prev_donations["zerator"] = 0.0
    dm._donation_streak["zerator"] = _DONATION_FLOOD_POLLS - 1
    dm._donation_alert_at[("zerator", "don")] = T0   # pic annoncé à l'instant

    entree = dm._candidat_don(_streamer(donation=3000.0), SEUIL, 300.0, T0 + 1.0)
    assert entree is not None, "le cooldown du pic ne doit pas couvrir la rafale"
    assert entree[2] == "bombardement"


def test_cooldown_etouffe_la_premiere_alerte_apres_un_boot(dm):
    """Aucune alerte n'a encore eu lieu : rien ne devrait être en cooldown.

    `_donation_alert_at.get(cle, 0.0)` traite « jamais annoncé » comme
    « annoncé à l'instant monotonic 0 », ce qui n'est vrai qu'après cinq
    minutes de fonctionnement de la machine.
    """
    dm._donations_init_done = True
    dm._prev_donations["zerator"] = 0.0
    now = _DONATION_ALERT_COOLDOWN_S / 2.0   # machine démarrée il y a 2 min 30
    assert dm._candidat_don(_streamer(donation=50_000.0), SEUIL,
                            _DONATION_ALERT_COOLDOWN_S, now) is not None


# ── _emettre_alertes_dons : plafond horaire ──────────────────────────────────

def _candidats(*montants) -> list:
    return [(m, _streamer(twitch_login=f"chaine{i}", display=f"Chaîne {i}"),
             "don")
            for i, m in enumerate(montants)]


def test_sous_une_pluie_de_dons_on_garde_les_plus_gros(dm):
    """Le plafond ne coupe pas au hasard : il trie, puis tronque.

    Garder les premiers arrivés reviendrait à annoncer l'ordre alphabétique de
    l'API plutôt que ce qui s'est réellement passé.
    """
    recu = _collecte(dm.big_donation)
    dm._emettre_alertes_dons(_candidats(1500.0, 9000.0, 3000.0, 20_000.0),
                             par_heure=2, now=T0)
    assert [args[2] for args in recu] == [20_000.0, 9000.0]


def test_plafond_horaire_atteint_rien_n_est_emis(dm):
    """Douze alertes dans l'heure suffisent largement à tenir au courant."""
    recu = _collecte(dm.big_donation)
    dm._donation_alert_times = [T0 - 10.0] * 3
    dm._emettre_alertes_dons(_candidats(50_000.0), par_heure=3, now=T0)
    assert recu == []


def test_les_alertes_de_plus_d_une_heure_liberent_leur_place(dm):
    """Le plafond est glissant : sinon il finirait par tout bloquer pour la
    durée du ZEvent."""
    recu = _collecte(dm.big_donation)
    dm._donation_alert_times = [T0 - 3601.0] * 12
    dm._emettre_alertes_dons(_candidats(50_000.0), par_heure=12, now=T0)
    assert len(recu) == 1
    assert dm._donation_alert_times == [T0]


def test_l_emission_arme_le_cooldown_de_cette_nature(dm):
    """C'est l'émission, pas la candidature, qui fait courir le cooldown."""
    dm._emettre_alertes_dons(
        [(5000.0, _streamer(), "bombardement")], par_heure=12, now=T0)
    assert dm._donation_alert_at[("zerator", "bombardement")] == T0
    assert ("zerator", "don") not in dm._donation_alert_at


def test_l_alerte_porte_le_nom_affichable_ou_le_login(dm):
    """Le login brut est illisible : il ne sert que faute de nom d'affichage."""
    recu = _collecte(dm.big_donation)
    dm._emettre_alertes_dons([(5000.0, _streamer(display=""), "don")],
                             par_heure=12, now=T0)
    assert recu == [("zerator", "zerator", 5000.0, "don")]


# ── _detect_big_donations : le détecteur complet ─────────────────────────────

def test_deux_sondages_sont_necessaires_pour_une_alerte(dm, horloge):
    """Le premier sondage ne fait que prendre la mesure de l'existant."""
    recu = _collecte(dm.big_donation)
    dm._detect_big_donations([_streamer(donation=10_000.0)])
    assert recu == []

    horloge["t"] += 30.0
    dm._detect_big_donations([_streamer(donation=15_000.0)])
    assert [args[0] for args in recu] == ["zerator"]
    assert recu[0][2] == pytest.approx(5000.0)


def test_famille_donation_coupee_rien_n_est_calcule(dm, horloge, monkeypatch):
    """Une alerte qu'on ne peut pas éteindre finit par être subie.

    Le contrôle se fait à la source : rien n'est même mémorisé.
    """
    monkeypatch.setattr(_alerts, "enabled", lambda famille: famille != "donation")
    recu = _collecte(dm.big_donation)
    dm._detect_big_donations([_streamer(donation=10_000.0)])
    dm._detect_big_donations([_streamer(donation=90_000.0)])
    assert recu == []
    assert dm._prev_donations == {}


def test_les_reglages_utilisateur_priment_sur_les_valeurs_par_defaut(dm, horloge):
    """Un seuil relevé à 5 000 € doit taire une hausse de 2 000 €."""
    dm._alert_cfg = {"donations": {"threshold": 5000.0, "cooldown_s": 60.0,
                                   "per_hour": 4}}
    recu = _collecte(dm.big_donation)
    dm._detect_big_donations([_streamer(donation=0.0)])
    horloge["t"] += 30.0
    dm._detect_big_donations([_streamer(donation=2000.0)])
    assert recu == []
    horloge["t"] += 30.0
    dm._detect_big_donations([_streamer(donation=8000.0)])
    assert len(recu) == 1


def test_une_configuration_absente_ou_abimee_retombe_sur_les_defauts(dm):
    """config.json est modifiable à la main : il peut contenir n'importe quoi."""
    for brut in ({}, {"donations": None}, {"donations": "oui"}):
        dm._alert_cfg = brut
        assert dm._donation_alert_config() == {}


# ── _get_near_completion_goals ───────────────────────────────────────────────

@pytest.mark.parametrize("cagnotte,retenu", [
    (8_900.0, False),    # 89 % : encore loin, le bandeau n'a rien à dire
    (9_000.0, True),     # 90 % : borne basse incluse
    (9_600.0, True),
    (10_000.0, True),    # 100 % : borne haute incluse
    (10_100.0, False),   # dépassé : le proxy cagnotte/objectif ne veut plus rien dire
])
def test_seuls_les_objectifs_entre_90_et_100_pourcent_remontent(dm, cagnotte, retenu):
    """Au-delà de 100 %, le pourcentage est un artefact du calcul.

    Il est estimé depuis la cagnotte TOTALE du streamer : celle d'un gros
    participant dépasse largement ses petits objectifs sans rien prouver.
    """
    dm._streamers = [_streamer(donation=cagnotte)]
    dm._goals_cache = {"zerator": [_goal(amount=10_000.0)]}
    assert bool(dm._get_near_completion_goals()) is retenu


@pytest.mark.parametrize("objectif,motif", [
    (_goal(accomplished=True), "déjà accompli : il n'est plus à portée, il est tombé"),
    (_goal(amount=0.0), "objectif sans montant : division impossible"),
    (_goal(amount=-500.0), "montant négatif : donnée aberrante"),
])
def test_objectifs_ecartes_du_calcul(dm, objectif, motif):
    dm._streamers = [_streamer(donation=9600.0)]
    dm._goals_cache = {"zerator": [objectif]}
    assert dm._get_near_completion_goals() == [], motif


def test_un_objectif_sans_streamer_connu_est_ignore(dm):
    """Le cache survit à un sondage : un streamer peut avoir disparu de la liste."""
    dm._streamers = []
    dm._goals_cache = {"zerator": [_goal()]}
    assert dm._get_near_completion_goals() == []


def test_les_objectifs_les_plus_proches_du_but_arrivent_en_tete(dm):
    """Le bandeau n'affiche que les premiers : ce doit être les plus mûrs."""
    dm._streamers = [_streamer(donation=9600.0)]
    dm._goals_cache = {"zerator": [
        _goal(id="a", name="Loin", amount=10_600.0),    # ~90,6 %
        _goal(id="b", name="Proche", amount=9_700.0),   # ~99,0 %
        _goal(id="c", name="Moyen", amount=10_100.0),   # ~95,0 %
    ]}
    assert [g.goal_name for g in dm._get_near_completion_goals()] == [
        "Proche", "Moyen", "Loin"]


# ── _signaler_si_imminent ────────────────────────────────────────────────────

@pytest.mark.parametrize("cible,pct,annonce", [
    (10_000.0, 95.0, True),    # reste 500 € : à portée de deux dons
    (50_000.0, 95.0, False),   # reste 2 500 € : ça peut durer des heures
    (50_000.0, 98.5, True),    # 2 % d'un gros objectif : la fin est proche
    (1_000.0, 90.0, True),     # reste 100 € : quelques minutes
    (50_000.0, 90.0, False),
])
def test_les_deux_criteres_d_imminence_sont_alternatifs(dm, cible, pct, annonce):
    """500 € restants sur 50 000 € ne font que 1 %, et 98 % d'un petit objectif
    ne font que quelques dizaines d'euros : aucun des deux seuls ne suffit."""
    dm._imminent_init_done = True
    recu = _collecte(dm.goal_imminent)
    dm._signaler_si_imminent(_enrichi(amount_target=cible, pct=pct))
    assert bool(recu) is annonce


def test_un_objectif_n_est_annonce_qu_une_fois(dm):
    """Sinon il reviendrait à chaque sondage tant qu'il n'est pas atteint —
    et un objectif peut rester à 99 % pendant deux heures."""
    dm._imminent_init_done = True
    recu = _collecte(dm.goal_imminent)
    for _ in range(3):
        dm._signaler_si_imminent(_enrichi())
    assert len(recu) == 1


def test_le_premier_passage_est_muet_et_definitif(dm):
    """Au lancement, plusieurs objectifs sont déjà tout près du but.

    Ils sont mémorisés comme « déjà vus » sans être annoncés : seule leur
    arrivée sous nos yeux mérite une notification.
    """
    dm._imminent_init_done = False
    recu = _collecte(dm.goal_imminent)
    dm._signaler_si_imminent(_enrichi())
    assert recu == []
    assert ("zerator", "Piment") in dm._imminent_announced

    dm._imminent_init_done = True
    dm._signaler_si_imminent(_enrichi())
    assert recu == []


def test_l_url_de_don_voyage_avec_l_alerte(dm):
    """Sans elle, proposer de donner obligerait à retrouver le streamer soi-même."""
    dm._imminent_init_done = True
    dm._streamers = [_streamer(donation_url="https://zevent.fr/don/zerator")]
    recu = _collecte(dm.goal_imminent)
    dm._signaler_si_imminent(_enrichi(amount_target=10_000.0, pct=96.0))
    login, display, objectif, reste, url = recu[0]
    assert (login, display, objectif) == ("zerator", "ZeratoR", "Piment")
    assert reste == pytest.approx(400.0)
    assert url == "https://zevent.fr/don/zerator"


@pytest.mark.parametrize("streamers,attendu", [
    ([], ""),                                            # chaîne inconnue
    ([_streamer(twitch_login="autre",
                donation_url="https://x.test")], ""),    # ce n'est pas la bonne
    ([_streamer()], ""),                                 # pas de lien publié
    ([_streamer(donation_url="https://x.test")], "https://x.test"),
])
def test_url_de_don_absente_ne_casse_pas_l_alerte(dm, streamers, attendu):
    """Toutes les chaînes ne publient pas de lien : "" est une réponse valide."""
    dm._streamers = streamers
    assert dm._url_de_don("zerator") == attendu


def test_check_imminent_le_premier_tour_arme_le_detecteur(dm):
    """Bout en bout : le tour d'inventaire ne parle pas, le suivant si."""
    dm._streamers = [_streamer(donation=9_600.0)]
    dm._goals_cache = {"zerator": [_goal(name="Piment", amount=10_000.0)]}
    recu = _collecte(dm.goal_imminent)

    dm._check_imminent_goals()
    assert recu == []
    assert dm._imminent_init_done is True

    dm._goals_cache["zerator"].append(_goal(id="g2", name="Rasage",
                                            amount=9_800.0))
    dm._check_imminent_goals()
    assert [args[2] for args in recu] == ["Rasage"]


def test_famille_objectif_imminent_coupee(dm, monkeypatch):
    monkeypatch.setattr(_alerts, "enabled",
                        lambda famille: famille != "goal_imminent")
    dm._imminent_init_done = True
    dm._streamers = [_streamer(donation=9_600.0)]
    dm._goals_cache = {"zerator": [_goal(amount=10_000.0)]}
    recu = _collecte(dm.goal_imminent)
    dm._check_imminent_goals()
    assert recu == []


# ── _check_newly_accomplished ────────────────────────────────────────────────

def test_les_objectifs_deja_accomplis_au_lancement_ne_sont_pas_rejoues(dm):
    """Un streamer peut avoir accompli dix objectifs avant qu'on lance l'app."""
    recu = _collecte(dm.goal_accomplished)
    dm._goals_cache = {"zerator": [_goal(name="Piment", accomplished=True)]}
    dm._check_newly_accomplished()
    assert recu == []
    assert dm._accomplished_goals == {("zerator", "Piment")}


def test_un_objectif_qui_tombe_sous_nos_yeux_est_annonce_une_seule_fois(dm):
    recu = _collecte(dm.goal_accomplished)
    dm._goals_cache = {"zerator": [_goal(name="Piment", accomplished=False)]}
    dm._check_newly_accomplished()

    dm._goals_cache = {"zerator": [_goal(name="Piment", accomplished=True)]}
    dm._check_newly_accomplished()
    dm._check_newly_accomplished()
    assert recu == [("zerator", "Piment")]


def test_famille_objectif_atteint_coupee(dm, monkeypatch):
    monkeypatch.setattr(_alerts, "enabled",
                        lambda famille: famille != "goal_done")
    recu = _collecte(dm.goal_accomplished)
    dm._goals_init_done = True
    dm._goals_cache = {"zerator": [_goal(accomplished=True)]}
    dm._check_newly_accomplished()
    assert recu == []


# ── paliers de cagnotte ──────────────────────────────────────────────────────

def test_le_premier_releve_de_cagnotte_ne_declenche_aucun_palier(dm):
    """Au lancement, la cagnotte a déjà franchi tous les paliers de la journée."""
    recu = _collecte(dm.milestone_reached)
    dm._detect_milestone(1_240_000.0)
    assert recu == []
    assert dm._last_milestone == pytest.approx(1_000_000.0)


def test_un_palier_franchi_sous_nos_yeux_est_annonce(dm):
    recu = _collecte(dm.milestone_reached)
    dm._detect_milestone(1_400_000.0)
    dm._detect_milestone(1_510_000.0)
    assert recu == [(1_500_000.0, "1,5 M€")]


def test_plusieurs_paliers_d_un_coup_ne_produisent_qu_une_annonce(dm):
    """Trois messages qui se chassent l'un l'autre n'apprendraient rien de plus
    que le plus haut."""
    recu = _collecte(dm.milestone_reached)
    dm._detect_milestone(300_000.0)
    dm._detect_milestone(980_000.0)
    assert recu == [(750_000.0, "750 k€")]


def test_une_cagnotte_qui_stagne_ou_recule_ne_reannonce_rien(dm):
    """L'API peut renvoyer une valeur en retard : ce n'est pas un événement."""
    recu = _collecte(dm.milestone_reached)
    dm._detect_milestone(1_100_000.0)
    dm._detect_milestone(1_050_000.0)
    dm._detect_milestone(1_100_000.0)
    assert recu == []
    assert dm._last_milestone == pytest.approx(1_000_000.0)


@pytest.mark.parametrize("total", [0.0, -5.0, None, "beaucoup"])
def test_une_cagnotte_absente_ou_illisible_est_ignoree(dm, total):
    """Hors event, l'API renvoie 0 — et une panne peut renvoyer autre chose."""
    recu = _collecte(dm.milestone_reached)
    dm._detect_milestone(total)
    assert recu == []
    assert dm._last_milestone is None


@pytest.mark.parametrize("montant,libelle", [
    (250_000.0, "250 k€"),
    (750_000.0, "750 k€"),
    (1_000_000.0, "1 M€"),
    (1_500_000.0, "1,5 M€"),
    (11_000_000.0, "11 M€"),
])
def test_libelle_de_palier_lisible_a_l_oral(montant, libelle):
    """« 1,5 M€ » se lit d'un coup d'œil, « 1500000.0 » non."""
    assert DataManager._fmt_milestone(montant) == libelle


# ── participant_logins ───────────────────────────────────────────────────────

def test_les_logins_de_participants_sont_normalises_en_minuscules(dm):
    """Sert à distinguer un raid entre participants d'un raid venu d'ailleurs.

    Twitch renvoie la casse d'affichage : comparer sans normaliser ferait
    passer un participant pour un inconnu.
    """
    dm._streamers = [
        _streamer(twitch_login="ZeratoR"),
        _streamer(twitch_login="MisterMV", online=False),
        _streamer(twitch_login=""),
    ]
    assert dm.participant_logins() == {"zerator", "mistermv"}


# ── _apply_goals : l'enchaînement complet côté thread principal ──────────────

def test_appliquer_les_objectifs_publie_le_cache_et_arme_les_detecteurs(dm):
    """Un seul point d'entrée relie le worker réseau aux deux détecteurs.

    S'il n'appelait qu'un des deux, la moitié des alertes d'objectifs
    disparaîtrait sans que rien ne le signale.
    """
    dm._streamers = [_streamer(donation=9_600.0)]
    dm._goals_cache = {"zerator": [_goal(amount=10_000.0)]}
    enrichis = _collecte(dm.goals_updated)
    bruts = _collecte(dm.goals_raw_updated)

    dm._apply_goals(dm._get_near_completion_goals())

    assert len(enrichis[0][0]) == 1
    assert bruts[0][0] == dm._goals_cache
    assert bruts[0][0] is not dm._goals_cache, "copie : le cache continue d'évoluer"
    # Les deux détecteurs ont pris leur relevé de référence.
    assert dm._goals_init_done is True
    assert dm._imminent_init_done is True
