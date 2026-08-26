# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Le câblage du DataManager : threads, boucles asyncio et fusion des sources.

Les alertes ont leur propre fichier ; celui-ci verrouille tout ce qui les
entoure et qui, en pratique, ne se voit qu'en production : le sondage qui se
recouvre lui-même, le worker qui meurt en laissant son drapeau levé, la boucle
asyncio qui laisse fuir ses sockets, et surtout la fusion des deux APIs —
c'est elle qui décide de ce que l'utilisateur voit à l'écran.

Aucun test n'ouvre de socket ni ne lance de thread réel : les `threading.Thread`
sont remplacés par une doublure qui retient sa cible sans l'exécuter, ce qui
permet de vérifier CE QUI aurait été lancé plutôt que d'attendre un résultat.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from core import alerts as _alerts
from core import avatar_cache, data_manager, favorites, live_uptime
from core.api_client import (
    DonationGoal,
    EventItem,
    GlobalStats,
    Participation,
    StreamerInfo,
)
from core.data_manager import (
    _EVENT_DAYS,
    _TOP_ENTRY_COOLDOWN_S,
    _TOP_ENTRY_N,
    DataManager,
    _gather_events,
    _gather_zevent_gdoc,
    _run,
)

# time.monotonic compte depuis le démarrage de la MACHINE : l'horloge factice
# doit partir au-delà du plus long cooldown, sinon tout serait étouffé.
T0 = 100_000.0


# ── outillage ────────────────────────────────────────────────────────────────

def _streamer(**kw) -> StreamerInfo:
    base = dict(
        twitch_login="zerator", display="ZeratoR", online=True, game="",
        location="", viewers=0, donation=0.0, donation_formatted="",
        profile_url="",
    )
    base.update(kw)
    return StreamerInfo(**base)


def _participation(**kw) -> Participation:
    base = dict(
        streamer_id="sid-zerator", participation_id="pid-zerator",
        twitch_login="zerator", display="ZeratoR", location="LAN", live=True,
        game="Minecraft", viewers=42_000, donation=694_000.0,
        profile_url="https://s.test/z.png",
    )
    base.update(kw)
    return Participation(**base)


def _stats(mode: str = "offline", **kw) -> GlobalStats:
    base = dict(donation_total=0.0, donation_formatted="0 €",
                viewers_total=0, website_mode=mode)
    base.update(kw)
    return GlobalStats(**base)


def _event(**kw) -> EventItem:
    base = dict(id="ev1", name="Lancement", day="2026-09-03",
                start_local="18:00", end_local="18:10", description="")
    base.update(kw)
    return EventItem(**base)


def _goal(**kw) -> DonationGoal:
    base = dict(id="g1", name="Piment", amount=10_000.0, accomplished=False,
                category="")
    base.update(kw)
    return DonationGoal(**base)


def _collecte(signal) -> list[tuple]:
    """Branche une liste sur un signal Qt et la retourne pour inspection."""
    recu: list[tuple] = []
    signal.connect(lambda *args: recu.append(args))
    return recu


class _FauxThread:
    """Un thread qui n'en est pas un : il retient sa cible sans l'exécuter.

    Lancer le vrai worker rendrait le test dépendant d'un ordonnancement et
    d'un réseau ; ce qu'on veut vérifier est ce que l'application AURAIT lancé.
    """

    def __init__(self, registre: list, target=None, args=(), daemon=False):
        self.registre = registre
        self.target = target
        self.args = tuple(args)
        self.daemon = daemon

    def start(self) -> None:
        self.registre.append(self)


@pytest.fixture
def threads(monkeypatch) -> list:
    """Remplace `threading` DANS data_manager, sans toucher au module réel."""
    lances: list[_FauxThread] = []
    monkeypatch.setattr(
        data_manager, "threading",
        types.SimpleNamespace(Thread=lambda **kw: _FauxThread(lances, **kw)),
    )
    return lances


@pytest.fixture
def dm(qapp, threads):
    """DataManager inerte : aucun timer, aucun réseau, aucun thread réel."""
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


@pytest.fixture(autouse=True)
def alertes_actives(monkeypatch):
    """Toutes les familles actives, quoi qu'ait fait un autre test.

    core.alerts garde son état dans un dictionnaire de module : un test qui
    coupe une famille la couperait pour tous les suivants.
    """
    monkeypatch.setattr(_alerts, "enabled", lambda famille: True)


@pytest.fixture
def sans_favoris(monkeypatch):
    """Aucun favori : la configuration réelle de la machine ne doit pas peser."""
    monkeypatch.setattr(favorites, "get", lambda: set())


def _faux_run(monkeypatch, resultat=None, exception: BaseException | None = None):
    """Remplace `_run` : la coroutine est fermée au lieu d'être exécutée.

    Sans le `close()`, Python signalerait une coroutine jamais attendue et le
    test polluerait la sortie de tous les suivants.
    """
    vues: list = []

    def _faux(coro):
        vues.append(coro)
        coro.close()
        if exception is not None:
            raise exception
        return resultat

    monkeypatch.setattr(data_manager, "_run", _faux)
    return vues


# ── _run : la boucle asyncio de remplacement ─────────────────────────────────

def test_une_coroutine_lancee_hors_boucle_qt_rend_son_resultat():
    """asyncio.run() pose des gestionnaires de signaux qui entrent en conflit
    avec la boucle Qt : `_run` existe pour ne pas les installer."""
    async def _quarante_deux() -> int:
        return 42

    assert _run(_quarante_deux()) == 42


def test_les_taches_encore_en_vol_sont_attendues_avant_la_fermeture():
    """Fermer la boucle sur une tâche en cours la tuerait en silence — et une
    requête HTTP à demi émise ne remonte aucune erreur."""
    temoin: list[str] = []

    async def _fille() -> None:
        await asyncio.sleep(0)
        temoin.append("terminee")

    async def _mere() -> str:
        asyncio.ensure_future(_fille())
        return "mere"

    assert _run(_mere()) == "mere"
    assert temoin == ["terminee"], "la tâche orpheline a été abandonnée"


def test_une_erreur_de_nettoyage_ne_masque_pas_le_resultat(monkeypatch):
    """Le nettoyage est une hygiène, pas une étape du calcul : s'il échoue,
    l'appelant doit quand même recevoir ce qu'il a demandé."""
    async def _explose() -> None:
        raise RuntimeError("client déjà fermé")

    monkeypatch.setattr(data_manager, "_close_loop_client", _explose)

    async def _valeur() -> str:
        return "ok"

    assert _run(_valeur()) == "ok"


def test_l_exception_de_la_coroutine_remonte_a_l_appelant():
    """Les workers comptent dessus pour remettre leur drapeau anti-recouvrement."""
    async def _echoue() -> None:
        raise ValueError("API injoignable")

    with pytest.raises(ValueError):
        _run(_echoue())


# ── les rassemblements d'appels parallèles ───────────────────────────────────

def test_les_deux_apis_sont_interrogees_dans_le_meme_aller_retour(monkeypatch):
    """Les enchaîner doublerait la latence du sondage, toutes les 30 secondes."""
    async def _parts():
        return [_participation()]

    async def _zevent():
        return _stats("live"), [_streamer()]

    monkeypatch.setattr(data_manager, "fetch_participations", _parts)
    monkeypatch.setattr(data_manager, "fetch_zevent_data", _zevent)

    participations, stats, streamers = _run(_gather_zevent_gdoc())
    assert participations[0].twitch_login == "zerator"
    assert stats.website_mode == "live"
    assert streamers[0].twitch_login == "zerator"


def test_le_programme_est_demande_pour_chacun_des_jours_de_l_edition(monkeypatch):
    """Il manquait le dernier jour de l'édition : son programme n'existait
    tout simplement pas pour l'application."""
    demandes: list[str] = []

    async def _events(jour: str):
        demandes.append(jour)
        return [_event(day=jour)]

    monkeypatch.setattr(data_manager, "fetch_events", _events)

    resultats = _run(_gather_events())
    assert demandes == _EVENT_DAYS
    assert [jour[0].day for jour in resultats] == _EVENT_DAYS


# ── cycle de vie ─────────────────────────────────────────────────────────────

def test_le_demarrage_arme_les_trois_timers_sans_bloquer_l_interface(dm, threads):
    """Charger l'historique 2026 dans le fil principal figeait la fenêtre
    plusieurs secondes au lancement."""
    dm.start()

    cibles = {t.target.__name__ for t in threads}
    assert cibles == {"_history_worker", "_streamers_worker", "_events_worker"}
    assert all(t.daemon for t in threads), "un worker non-daemon retiendrait la sortie"
    assert dm._timer_streamers.isActive()
    assert dm._timer_events.isActive()
    assert dm._timer_goals.isActive()


def test_l_arret_du_polling_coupe_les_trois_timers(dm, threads):
    """Le mode mock rejoue des données figées : un sondage les écraserait."""
    dm.start()
    dm.stop_polling()
    assert not dm._timer_streamers.isActive()
    assert not dm._timer_events.isActive()
    assert not dm._timer_goals.isActive()


def test_l_historique_est_publie_meme_si_son_chargement_echoue(dm, monkeypatch):
    """Le dépôt tiers qui l'héberge peut être hors service : le panel doit
    quand même recevoir un historique — vide plutôt qu'aucun."""
    _faux_run(monkeypatch, exception=OSError("dépôt injoignable"))
    recu = _collecte(dm.history_updated)

    dm._history_worker()

    assert recu == [(dm._history,)]


def test_l_historique_charge_est_publie_a_l_interface(dm, monkeypatch):
    """C'est ce signal, et lui seul, qui remplit la courbe de l'onglet Accueil."""
    recu = _collecte(dm.history_updated)
    _faux_run(monkeypatch)
    dm._history_worker()
    assert recu == [(dm._history,)]


@pytest.mark.parametrize("config,attendu", [
    ({"donations": {"threshold": 5000.0}}, {"donations": {"threshold": 5000.0}}),
    (None, {}),
    ({}, {}),
])
def test_les_seuils_se_rechargent_a_chaud(dm, config, attendu):
    """Interroger le disque à chaque sondage pour trois nombres serait absurde :
    la configuration est tenue en mémoire, et le panel la repousse ici."""
    dm.reload_config(config)
    assert dm._alert_cfg == attendu


def test_la_configuration_rechargee_est_recopiee(dm):
    """Garder la référence du panel ferait muter les seuils sous les pieds du
    détecteur, au milieu d'un sondage."""
    source = {"donations": {"threshold": 1.0}}
    dm.reload_config(source)
    source["donations"] = {"threshold": 999.0}
    assert dm._alert_cfg["donations"] == {"threshold": 1.0}


# ── les accesseurs interrogés par le panel ───────────────────────────────────

def test_seules_les_chaines_en_direct_sont_rendues(dm):
    """La mosaïque n'ouvre que des directs : une chaîne hors ligne y ferait
    une tuile noire."""
    dm._streamers = [_streamer(twitch_login="a", online=True),
                     _streamer(twitch_login="b", online=False)]
    assert [s.twitch_login for s in dm.get_streamers_live()] == ["a"]


def test_le_classement_des_cagnottes_est_decroissant_et_tronque(dm):
    """Le panel n'affiche qu'un haut de tableau : c'est ici qu'il est choisi."""
    dm._streamers = [_streamer(twitch_login=f"c{i}", donation=float(i))
                     for i in range(5)]
    assert [s.donation for s in dm.get_top_donations(3)] == [4.0, 3.0, 2.0]


def test_un_jour_sans_programme_rend_une_liste_vide(dm):
    """Le panel itère sans vérifier : None ferait tomber l'onglet Programme."""
    dm._events = {"2026-09-03": [_event()]}
    assert dm.get_events_for_day("2026-09-03") != []
    assert dm.get_events_for_day("2026-12-25") == []


@pytest.mark.parametrize("demande,trouve", [
    ("zerator", True),
    ("ZeratoR", True),      # Twitch renvoie la casse d'affichage
    ("ZERATOR", True),
    ("inconnu", False),
])
def test_une_chaine_se_retrouve_quelle_que_soit_sa_casse(dm, demande, trouve):
    """Les logins voyagent entre trois APIs qui ne s'accordent pas sur la casse."""
    dm._streamers = [_streamer(twitch_login="ZeratoR")]
    assert (dm.get_streamer(demande) is not None) is trouve


@pytest.mark.parametrize("methode,table,attendu", [
    ("get_gdoc_id", "_gdoc_map", "sid-zerator"),
    ("get_participation_id", "_participation_map", "pid-zerator"),
])
def test_les_identifiants_communautaires_se_lisent_en_minuscules(
        dm, methode, table, attendu):
    """Le streamer_id survit aux éditions, le participation_id ouvre les
    objectifs : les deux tables sont indexées en minuscules."""
    setattr(dm, table, {"zerator": attendu})
    assert getattr(dm, methode)("ZeratoR") == attendu
    assert getattr(dm, methode)("inconnu") is None


def test_un_participant_de_show_inconnu_rend_une_chaine_vide(dm):
    """Les invités non-streamers (GIMS, Bigflo et Oli) n'ont pas de
    participation : None ferait planter la mise en forme du programme."""
    dm._uuid_to_name = {"sid-zerator": "ZeratoR"}
    assert dm.resolve_participant_uuid("sid-zerator") == "ZeratoR"
    assert dm.resolve_participant_uuid("uuid-invite") == ""


def test_les_statistiques_globales_sont_lisibles_avant_tout_sondage(dm):
    """Le panel se dessine avant la première réponse de l'API."""
    stats = dm.get_stats()
    assert stats.donation_total == 0.0 and stats.website_mode == "offline"


# ── anti-recouvrement des sondages ───────────────────────────────────────────

@pytest.mark.parametrize("declencheur,drapeau,worker", [
    ("_poll_streamers", "_polling_streamers", "_streamers_worker"),
    ("_poll_events", "_polling_events", "_events_worker"),
])
def test_un_sondage_deja_en_cours_n_est_pas_relance(
        dm, threads, declencheur, drapeau, worker):
    """Sur une connexion lente, le sondage dépasse sa période de 30 s : sans
    garde, le timer empilait des passes concurrentes sur la même API."""
    getattr(dm, declencheur)()
    assert [t.target.__name__ for t in threads] == [worker]
    assert getattr(dm, drapeau) is True

    getattr(dm, declencheur)()
    assert len(threads) == 1, "la seconde passe aurait doublé les requêtes"


@pytest.mark.parametrize("worker,drapeau", [
    ("_streamers_worker", "_polling_streamers"),
    ("_events_worker", "_polling_events"),
])
def test_un_worker_qui_echoue_libere_son_drapeau(dm, monkeypatch, worker, drapeau):
    """Sans cette remise à zéro, une seule coupure réseau gelait le sondage
    pour le reste de la soirée."""
    _faux_run(monkeypatch, exception=OSError("réseau coupé"))
    setattr(dm, drapeau, True)
    getattr(dm, worker)()
    assert getattr(dm, drapeau) is False


def test_le_worker_streamers_repasse_ses_resultats_au_fil_principal(dm, monkeypatch):
    """Toucher les widgets depuis un thread ferait tomber Qt : le worker
    n'écrit rien, il émet un signal."""
    _faux_run(monkeypatch,
              resultat=([_participation()], _stats("live"), [_streamer()]))
    recu = _collecte(dm.streamers_updated)

    dm._polling_streamers = True
    dm._streamers_worker()

    assert dm._polling_streamers is False
    assert [s.twitch_login for s in recu[0][0]] == ["zerator"]


def test_le_worker_events_repasse_ses_resultats_au_fil_principal(dm, monkeypatch):
    """Même règle pour le programme : c'est le signal qui traverse les threads."""
    _faux_run(monkeypatch, resultat=[[_event(id=f"ev-{j}", day=j)]
                                     for j in _EVENT_DAYS])
    recu = _collecte(dm.events_updated)

    dm._polling_events = True
    dm._events_worker()

    assert dm._polling_events is False
    assert len(recu[0][0]) == len(_EVENT_DAYS)


# ── durées de direct ─────────────────────────────────────────────────────────

def test_seules_les_chaines_sans_releve_frais_sont_redemandees(
        dm, threads, monkeypatch):
    """Le début d'un direct ne bouge pas : redemander toutes les 30 s à une
    interface non documentée la ferait fermer devant nous."""
    monkeypatch.setattr(live_uptime, "a_rafraichir", lambda logins: [])
    dm.rafraichir_durees(["zerator", "mistermv"])
    assert threads == []
    assert dm._polling_durees is False


def test_les_chaines_a_rafraichir_partent_en_tache_de_fond(dm, threads, monkeypatch):
    """Seules les chaînes AFFICHÉES sont demandées : le worker doit recevoir
    exactement la liste qu'on lui a préparée, sans la recalculer."""
    monkeypatch.setattr(live_uptime, "a_rafraichir", lambda logins: ["zerator"])
    dm.rafraichir_durees(["zerator"])
    assert threads[0].target.__name__ == "_durees_worker"
    assert threads[0].args == (["zerator"],)
    assert dm._polling_durees is True


def test_un_releve_de_durees_deja_en_cours_n_est_pas_double(dm, threads, monkeypatch):
    """Deux passes concurrentes sur gql.twitch.tv, c'est le meilleur moyen de
    se faire couper l'accès à une interface qui ne nous doit rien."""
    monkeypatch.setattr(live_uptime, "a_rafraichir", lambda logins: ["zerator"])
    dm._polling_durees = True
    dm.rafraichir_durees(["zerator"])
    assert threads == []


@pytest.mark.parametrize("panne", [None, OSError("gql injoignable")])
def test_le_redessin_est_demande_meme_si_le_releve_echoue(dm, monkeypatch, panne):
    """L'interface non documentée peut disparaître sans préavis : son absence
    ne doit se voir qu'à une ligne manquante, pas à un affichage figé."""
    _faux_run(monkeypatch, exception=panne)
    recu = _collecte(dm.durees_updated)

    dm._polling_durees = True
    dm._durees_worker(["zerator"])

    assert recu == [()]
    assert dm._polling_durees is False


# ── _apply_streamers : la fusion des deux sources ────────────────────────────

def test_hors_event_les_streamers_viennent_des_participations(
        dm, sans_favoris, threads):
    """zevent.fr/api/ ne renvoie aucun streamer hors édition. Sans ce repli,
    l'application était vide onze mois sur douze."""
    recu = _collecte(dm.streamers_updated)

    dm._apply_streamers([_participation()], _stats("offline"), [])

    (liste,) = recu[0]
    assert len(liste) == 1
    s = liste[0]
    assert (s.twitch_login, s.display, s.online) == ("zerator", "ZeratoR", True)
    assert s.donation == pytest.approx(694_000.0)
    assert s.donation_formatted, "la cagnotte doit être mise en forme pour l'écran"
    assert (s.gdoc_id, s.participation_id) == ("sid-zerator", "pid-zerator")


def test_en_mode_live_l_absence_de_streamers_n_est_pas_compensee(
        dm, sans_favoris, threads):
    """En mode live, zevent.fr fait foi : une liste vide veut dire vide, et
    fabriquer des chaînes depuis les participations les ferait doublonner."""
    recu = _collecte(dm.streamers_updated)
    dm._apply_streamers([_participation()], _stats("live"), [])
    assert recu[0][0] == []


def test_un_sondage_sans_participations_conserve_les_identifiants_connus(
        dm, sans_favoris, threads):
    """L'API communautaire tombe plus souvent que l'officielle : perdre ses
    tables couperait l'accès aux objectifs jusqu'au sondage suivant."""
    dm._gdoc_map = {"zerator": "sid-zerator"}
    dm._participation_map = {"zerator": "pid-zerator"}

    dm._apply_streamers([], _stats("live"), [_streamer(twitch_login="ZeratoR")])

    assert dm._gdoc_map == {"zerator": "sid-zerator"}
    assert dm._streamers[0].participation_id == "pid-zerator"


def test_une_participation_sans_id_d_edition_n_ouvre_pas_les_objectifs(
        dm, sans_favoris, threads):
    """La table des objectifs est indexée par participation_id : une entrée
    vide y ferait une requête vers /participations//donation_goals."""
    dm._apply_streamers([_participation(participation_id="")],
                        _stats("live"), [])
    assert dm._participation_map == {}
    assert dm._gdoc_map == {"zerator": "sid-zerator"}


def test_les_noms_des_invites_de_shows_survivent_aux_streamers(
        dm, sans_favoris, threads):
    """Le programme ne connaît ses participants que par UUID. Les invités
    n'ont pas de chaîne : leur nom ne peut venir que des participations."""
    dm._apply_streamers(
        [_participation(), _participation(streamer_id="sid-gims",
                                          twitch_login="", display="GIMS")],
        _stats("live"), [_streamer()],
    )
    assert dm.resolve_participant_uuid("sid-gims") == "GIMS"
    assert dm.resolve_participant_uuid("sid-zerator") == "ZeratoR"


def test_l_arrivee_des_donnees_libere_le_drapeau_de_sondage(
        dm, sans_favoris, threads):
    """Le drapeau est levé par le déclencheur et n'est abaissé qu'ici : oublier
    de l'abaisser gèlerait tous les sondages suivants."""
    dm._polling_streamers = True
    dm._apply_streamers([], _stats("live"), [])
    assert dm._polling_streamers is False


def test_le_premier_chargement_declenche_le_prechargement_des_objectifs(
        dm, sans_favoris, threads):
    """Les objectifs ne peuvent être demandés qu'une fois les streamers connus :
    c'est leur participation_id qui sert de clé."""
    dm._apply_streamers([_participation()], _stats("live"), [_streamer()])
    assert "_goals_worker" in {t.target.__name__ for t in threads}


def test_un_cache_d_objectifs_deja_rempli_ne_relance_pas_le_prechargement(
        dm, sans_favoris, threads):
    """Sinon chaque sondage de 30 s rejouerait une passe complète sur l'API des
    objectifs, alors que le timer dédié s'en charge toutes les cinq minutes."""
    dm._goals_cache = {"zerator": []}
    dm._apply_streamers([_participation()], _stats("live"), [_streamer()])
    assert "_goals_worker" not in {t.target.__name__ for t in threads}


def test_chaque_sondage_alimente_l_historique_et_les_statistiques(
        dm, sans_favoris, threads):
    """Les courbes du panel se construisent uniquement ici : un point manqué
    est un trou définitif dans le graphique."""
    releves: list[tuple[float, int]] = []
    dm._history = types.SimpleNamespace(
        add_point=lambda donation, viewers: releves.append((donation, viewers)))
    stats = _collecte(dm.global_stats_updated)
    histo = _collecte(dm.history_updated)

    dm._apply_streamers([], _stats("live", donation_total=1_234.0,
                                   viewers_total=99), [])

    assert stats[0][0].donation_total == pytest.approx(1_234.0)
    assert releves == [(1_234.0, 99)]
    assert histo == [(dm._history,)], "le panel doit être invité à redessiner"


def test_famille_palier_coupee_la_cagnotte_est_publiee_quand_meme(
        dm, sans_favoris, threads, monkeypatch):
    """Couper l'annonce des paliers ne doit pas couper la cagnotte elle-même :
    c'est l'alerte qu'on éteint, pas le chiffre affiché en haut du panel."""
    monkeypatch.setattr(_alerts, "enabled", lambda f: f != "milestone")
    paliers = _collecte(dm.milestone_reached)
    stats = _collecte(dm.global_stats_updated)

    dm._apply_streamers([], _stats("live", donation_total=1_400_000.0), [])
    dm._apply_streamers([], _stats("live", donation_total=1_600_000.0), [])

    assert paliers == []
    assert dm._last_milestone is None, "rien n'est même mesuré"
    assert len(stats) == 2


# ── pré-chargement des avatars ───────────────────────────────────────────────

@pytest.mark.parametrize("streamers,motif", [
    ([], "aucun streamer"),
    ([_streamer(profile_url="")], "pas de photo publiée"),
    ([_streamer(twitch_login="", profile_url="https://s.test/x.png")],
     "sans login, aucun nom de fichier de cache"),
])
def test_aucun_avatar_a_charger_ne_lance_aucun_thread(dm, threads, streamers, motif):
    """Un thread par sondage pour ne rien faire coûterait plus que le calcul."""
    dm._prefetch_avatars(streamers)
    assert threads == [], motif


def test_les_couples_login_photo_partent_ensemble_en_tache_de_fond(dm, threads):
    """Le nom de fichier du cache est le login : les deux voyagent appariés."""
    dm._prefetch_avatars([_streamer(profile_url="https://s.test/z.png"),
                          _streamer(twitch_login="mv", profile_url="")])
    assert threads[0].args == ([("zerator", "https://s.test/z.png")],)


def test_un_avatar_deja_en_cache_n_est_pas_retelecharge(dm, tmp_path, monkeypatch):
    """510 requêtes pour 300 images distinctes : c'est ce doublon-là que le
    cache disque supprime."""
    monkeypatch.setattr(avatar_cache, "CACHE_DIR", tmp_path)
    appels: list = []
    monkeypatch.setattr(avatar_cache, "download",
                        lambda *a: appels.append(a) or True)
    avatar_cache.path_for("zerator").write_bytes(b"png")

    dm._avatars_prefetch_worker([("zerator", "https://s.test/z.png")])

    assert appels == []


def test_les_avatars_manquants_sont_telecharges(dm, tmp_path, monkeypatch):
    """Faute de photo, la mosaïque retombe sur des initiales : l'utilisateur
    ne reconnaît plus personne d'un coup d'œil."""
    monkeypatch.setattr(avatar_cache, "CACHE_DIR", tmp_path)
    appels: list = []
    monkeypatch.setattr(avatar_cache, "download",
                        lambda *a: appels.append(a) or True)

    dm._avatars_prefetch_worker([("zerator", "https://s.test/z.png"),
                                 ("mv", "https://s.test/m.png")])

    assert sorted(appels) == [("mv", "https://s.test/m.png"),
                              ("zerator", "https://s.test/z.png")]


# ── _detect_top_entry ────────────────────────────────────────────────────────

def _top(n: int) -> list[StreamerInfo]:
    """n chaînes en direct, audiences décroissantes et distinctes."""
    return [_streamer(twitch_login=f"c{i}", display=f"Chaîne {i}",
                      viewers=1000 - i)
            for i in range(n)]


def test_le_premier_releve_d_audience_ne_signale_aucune_entree(dm, horloge):
    """Au lancement, les trois premières chaînes y sont déjà depuis des heures."""
    recu = _collecte(dm.top_stream_entered)
    dm._detect_top_entry(_top(5))
    assert recu == []
    assert dm._prev_top == {"c0", "c1", "c2"}
    assert dm._top_init_done is True


def test_une_chaine_qui_entre_dans_le_top_est_annoncee_avec_son_rang(dm, horloge):
    """Le rang dit à lui seul ce qui se passe : une première place soudaine
    est un show qui démarre, une troisième un raid qui vient d'atterrir."""
    recu = _collecte(dm.top_stream_entered)
    dm._detect_top_entry(_top(3))

    horloge["t"] += 30.0
    nouvelle = _streamer(twitch_login="mv", display="MisterMV", viewers=50_000)
    dm._detect_top_entry([nouvelle] + _top(3))

    assert recu == [("mv", "MisterMV", 50_000, 1)]


def test_une_chaine_deja_installee_dans_le_top_ne_se_reannonce_pas(dm, horloge):
    """Sinon les trois mêmes chaînes produiraient une alerte toutes les 30 s
    pendant quatre jours."""
    recu = _collecte(dm.top_stream_entered)
    dm._detect_top_entry(_top(3))
    horloge["t"] += 30.0
    dm._detect_top_entry(_top(3))
    assert recu == []


def test_une_chaine_qui_oscille_autour_de_la_derniere_place_se_tait(dm, horloge):
    """Entrer et sortir du top à chaque sondage est le cas NORMAL en bas de
    classement : sans cooldown, c'est de là que viendrait tout le bruit."""
    recu = _collecte(dm.top_stream_entered)
    dm._top_init_done = True
    dm._top_alert_at["c0"] = T0

    horloge["t"] = T0 + _TOP_ENTRY_COOLDOWN_S - 1.0
    dm._detect_top_entry(_top(1))
    assert recu == []

    dm._prev_top = set()
    horloge["t"] = T0 + _TOP_ENTRY_COOLDOWN_S + 1.0
    dm._detect_top_entry(_top(1))
    assert len(recu) == 1


def test_la_premiere_entree_apres_un_demarrage_de_machine_n_est_pas_etouffee(dm):
    """time.monotonic part du démarrage de la MACHINE : un cooldown par défaut
    à 0.0 taisait toute alerte pendant les quinze premières minutes d'uptime."""
    recu = _collecte(dm.top_stream_entered)
    dm._top_init_done = True
    dm._detect_top_entry(_top(1))
    assert len(recu) == 1


@pytest.mark.parametrize("champs,motif", [
    (dict(viewers=90_000, online=False),
     "hors ligne : elle n'est dans aucun classement"),
    (dict(viewers=0),
     "audience nulle ou absente de la charge API"),
    (dict(viewers=90_000, twitch_login=""),
     "sans login, impossible de l'ouvrir"),
])
def test_les_chaines_inexploitables_ne_peuvent_pas_entrer_dans_le_top(
        dm, horloge, champs, motif):
    """L'API renvoie régulièrement des entrées incomplètes ; le classement ne
    doit ni s'y arrêter ni les annoncer."""
    recu = _collecte(dm.top_stream_entered)
    dm._top_init_done = True
    dm._detect_top_entry([_streamer(**champs)])
    assert recu == [], motif


def test_le_top_ne_retient_que_les_toutes_premieres_places(dm, horloge):
    """Au-delà, le classement bouge trop pour qu'une entrée signifie quoi que
    ce soit."""
    dm._detect_top_entry(_top(10))
    assert len(dm._prev_top) == _TOP_ENTRY_N


def test_famille_entree_dans_le_top_coupee_rien_n_est_meme_mesure(dm, monkeypatch):
    """Le contrôle se fait à la source : une alerte éteinte ne coûte rien, et
    surtout ne laisse pas un état à moitié à jour derrière elle."""
    monkeypatch.setattr(_alerts, "enabled", lambda f: f != "top_entry")
    recu = _collecte(dm.top_stream_entered)
    dm._detect_top_entry(_top(3))
    dm._detect_top_entry(_top(3))
    assert recu == []
    assert dm._top_init_done is False


# ── _detect_favorites_live ───────────────────────────────────────────────────

def test_les_favoris_deja_en_direct_au_lancement_ne_notifient_pas(dm, monkeypatch):
    """Sans ce garde-fou, ouvrir l'application en pleine soirée déclencherait
    une notification par favori en ligne — alors qu'il ne s'est rien passé."""
    monkeypatch.setattr(favorites, "get", lambda: {"zerator"})
    recu = _collecte(dm.favorite_live)
    dm._detect_favorites_live([_streamer(online=True)])
    assert recu == []
    assert dm._online_logins == {"zerator"}
    assert dm._live_init_done is True


def test_un_favori_qui_passe_en_direct_est_annonce(dm, monkeypatch):
    """C'est la seule alerte qui justifie d'interrompre : on a explicitement
    demandé à être prévenu pour cette chaîne-là."""
    monkeypatch.setattr(favorites, "get", lambda: {"zerator"})
    recu = _collecte(dm.favorite_live)
    dm._detect_favorites_live([_streamer(online=False)])
    dm._detect_favorites_live([_streamer(online=True)])
    assert recu == [("zerator", "ZeratoR")]


def test_un_favori_deja_en_direct_ne_se_reannonce_pas(dm, monkeypatch):
    """Une chaîne reste en ligne des heures : ce sont les TRANSITIONS qui
    parlent, pas l'état."""
    monkeypatch.setattr(favorites, "get", lambda: {"zerator"})
    recu = _collecte(dm.favorite_live)
    for _ in range(3):
        dm._detect_favorites_live([_streamer(online=True)])
    assert recu == []


def test_la_comparaison_aux_favoris_ignore_la_casse_du_login(dm, monkeypatch):
    """Les favoris sont enregistrés en minuscules, Twitch renvoie la casse
    d'affichage : comparer brut ne notifiait jamais rien."""
    monkeypatch.setattr(favorites, "get", lambda: {"zerator"})
    recu = _collecte(dm.favorite_live)
    dm._detect_favorites_live([_streamer(twitch_login="ZeratoR", online=False)])
    dm._detect_favorites_live([_streamer(twitch_login="ZeratoR", online=True)])
    assert recu == [("ZeratoR", "ZeratoR")]


@pytest.mark.parametrize("favs,motif", [
    (set(), "aucun favori enregistré"),
    ({"mistermv"}, "ce n'est pas la chaîne suivie"),
])
def test_une_chaine_non_suivie_qui_demarre_ne_notifie_rien(
        dm, monkeypatch, favs, motif):
    """Trois cents chaînes démarrent pendant l'édition : seules celles qu'on a
    choisies méritent d'interrompre."""
    monkeypatch.setattr(favorites, "get", lambda: favs)
    recu = _collecte(dm.favorite_live)
    dm._detect_favorites_live([_streamer(online=False)])
    dm._detect_favorites_live([_streamer(online=True)])
    assert recu == [], motif


def test_famille_favori_en_direct_coupee(dm, monkeypatch):
    """Coupée à la source : le détecteur ne mémorise même pas son relevé."""
    monkeypatch.setattr(_alerts, "enabled", lambda f: f != "favorite_live")
    recu = _collecte(dm.favorite_live)
    dm._detect_favorites_live([_streamer(online=True)])
    assert recu == []
    assert dm._live_init_done is False


# ── programme : nouveautés et application ────────────────────────────────────

def test_un_show_sans_identifiant_garde_une_identite_stable(dm):
    """L'API a déjà livré des shows sans id : sans repli sur date, heure et
    nom, chacun passerait pour un nouveau show à chaque sondage."""
    assert DataManager._event_key(_event(id="")) == "2026-09-03_18:00_Lancement"
    assert DataManager._event_key(_event(id="ev1")) == "ev1"


def test_le_programme_connu_au_lancement_n_est_pas_annonce(dm):
    """Quarante shows annoncés d'un coup à l'ouverture n'apprendraient rien."""
    recu = _collecte(dm.programme_added)
    dm._detect_new_events([_event(), _event(id="ev2")])
    assert recu == []
    assert dm._events_init_done is True


def test_un_show_ajoute_en_cours_d_edition_est_annonce_une_fois(dm):
    """Le programme change en direct : c'est la seule façon de l'apprendre
    sans surveiller l'onglet."""
    recu = _collecte(dm.programme_added)
    surprise = _event(id="ev2", name="Surprise", start_local="22:00")
    dm._detect_new_events([_event()])
    dm._detect_new_events([_event(), surprise])
    dm._detect_new_events([_event(), surprise])
    assert recu == [("Surprise", "2026-09-03 22:00")]


def test_un_show_sans_nom_ni_horaire_reste_annoncable(dm):
    """L'API publie parfois un créneau avant de l'avoir nommé ou daté : une
    notification vide serait pire qu'un libellé générique."""
    recu = _collecte(dm.programme_added)
    dm._detect_new_events([])
    dm._events_init_done = True
    dm._detect_new_events([_event(name="", start_local="")])
    assert recu == [("Événement", "2026-09-03")]


def test_le_programme_de_chaque_jour_est_range_a_sa_date(dm):
    """L'onglet Programme lit par jour : un show à cheval sur minuit apparaît
    des deux côtés, et c'est voulu."""
    resultats = [[_event(id=f"ev-{j}", day=j)] for j in _EVENT_DAYS]
    recu = _collecte(dm.events_updated)

    dm._polling_events = True
    dm._apply_events(resultats)

    assert dm._polling_events is False
    assert set(dm._events) == set(_EVENT_DAYS)
    assert len(recu[0][0]) == len(_EVENT_DAYS)


def test_un_jour_en_erreur_ne_perd_pas_les_autres(dm):
    """asyncio.gather rend les exceptions en ligne : jeter tout le programme
    parce qu'un seul jour a échoué serait une régression visible."""
    resultats: list = [OSError("503")] + [[_event(id=f"ev-{j}", day=j)]
                                          for j in _EVENT_DAYS[1:]]
    recu = _collecte(dm.events_updated)

    dm._apply_events(resultats)

    assert _EVENT_DAYS[0] not in dm._events
    assert len(recu[0][0]) == len(_EVENT_DAYS) - 1


def test_un_programme_entierement_vide_ne_publie_rien(dm):
    """Hors édition, les cinq jours reviennent vides : republier une liste vide
    effacerait le programme déjà affiché et déclencherait un redessin inutile."""
    recu = _collecte(dm.events_updated)
    dm._apply_events([[] for _ in _EVENT_DAYS])
    assert recu == []
    assert dm._events_init_done is False


# ── pré-chargement des objectifs ─────────────────────────────────────────────

def test_une_passe_d_objectifs_en_cours_n_est_pas_doublee(dm, threads):
    """Cette passe interroge l'API une fois par streamer suivi : deux passes
    concurrentes doublent les requêtes ET se disputent le même cache."""
    dm._start_goals_prefetch()
    assert [t.target.__name__ for t in threads] == ["_goals_worker"]
    dm._start_goals_prefetch()
    assert len(threads) == 1


def test_les_objectifs_charges_repartent_vers_le_fil_principal(dm, monkeypatch):
    """Le worker calcule, le fil principal publie : c'est ce passage de relais
    qui alimente le bandeau des objectifs proches."""
    _faux_run(monkeypatch)
    dm._streamers = [_streamer(donation=9_600.0)]
    dm._goals_cache = {"zerator": [_goal(amount=10_000.0)]}
    recu = _collecte(dm.goals_updated)

    dm._goals_running = True
    dm._goals_worker()

    assert dm._goals_running is False
    assert len(recu[0][0]) == 1


def test_une_passe_d_objectifs_qui_echoue_libere_la_garde(dm, monkeypatch):
    """Le drapeau restait levé pour toujours, et plus aucun objectif n'était
    rafraîchi ensuite — l'application semblait simplement figée."""
    _faux_run(monkeypatch, exception=OSError("API objectifs hors service"))
    recu = _collecte(dm.goals_updated)

    dm._goals_running = True
    dm._goals_worker()

    assert dm._goals_running is False
    assert recu == [], "aucun résultat à publier après un échec"


# ── _prefetch_top_goals ──────────────────────────────────────────────────────

@pytest.fixture
def objectifs_captes(monkeypatch):
    """Capture les participation_id demandés à l'API des objectifs."""
    demandes: list[str] = []

    async def _fetch(pid: str):
        demandes.append(pid)
        return [_goal(name=f"objectif de {pid}")]

    monkeypatch.setattr(data_manager, "fetch_donation_goals", _fetch)
    return demandes


def _cinq_streamers() -> list[StreamerInfo]:
    """Cinq chaînes de cagnottes croissantes, chacune avec son id d'édition."""
    return [_streamer(twitch_login=f"c{i}", donation=float(i),
                      participation_id=f"pid{i}")
            for i in range(5)]


def test_seules_les_plus_grosses_cagnottes_sont_prechargees(
        dm, objectifs_captes, sans_favoris):
    """Trois cents requêtes toutes les cinq minutes pour des objectifs que
    personne n'ouvrira : le haut de tableau suffit."""
    dm._streamers = _cinq_streamers()
    _run(dm._prefetch_top_goals(n=2))
    assert sorted(objectifs_captes) == ["pid3", "pid4"]


def test_un_favori_hors_du_haut_de_tableau_est_precharge_quand_meme(
        dm, objectifs_captes, monkeypatch):
    """Sans lui, l'objectif accompli d'un streamer suivi mais modeste n'était
    jamais détecté — précisément le cas où l'utilisateur attend l'alerte."""
    monkeypatch.setattr(favorites, "get", lambda: {"c0"})
    dm._streamers = _cinq_streamers()
    _run(dm._prefetch_top_goals(n=2))
    assert sorted(objectifs_captes) == ["pid0", "pid3", "pid4"]


def test_un_favori_deja_dans_le_haut_de_tableau_n_est_pas_demande_deux_fois(
        dm, objectifs_captes, monkeypatch):
    """Deux requêtes pour la même chaîne, toutes les cinq minutes, sur une
    liste où les favoris sont justement les mieux dotés."""
    monkeypatch.setattr(favorites, "get", lambda: {"c4"})
    dm._streamers = _cinq_streamers()
    _run(dm._prefetch_top_goals(n=2))
    assert sorted(objectifs_captes) == ["pid3", "pid4"]


def test_l_id_d_edition_est_repris_dans_la_table_si_le_streamer_l_ignore(
        dm, objectifs_captes, sans_favoris):
    """Les StreamerInfo venus de zevent.fr n'ont pas d'id d'édition : la table
    des participations reste la source de secours, en minuscules."""
    dm._streamers = [_streamer(twitch_login="ZeratoR", participation_id=None)]
    dm._participation_map = {"zerator": "pid-zerator"}
    _run(dm._prefetch_top_goals(n=20))
    assert objectifs_captes == ["pid-zerator"]


def test_un_streamer_sans_id_d_edition_est_ecarte(
        dm, objectifs_captes, sans_favoris):
    """Une requête vers /participations//donation_goals rendrait une 404 à
    chaque passe, toutes les cinq minutes."""
    dm._streamers = [_streamer(twitch_login="inconnu", participation_id=None)]
    _run(dm._prefetch_top_goals(n=20))
    assert objectifs_captes == []
    assert dm._goals_cache == {}


def test_un_objectif_illisible_ne_fait_pas_tomber_les_autres(
        dm, monkeypatch, sans_favoris):
    """Un `gather` sans garde propage la première exception et perd les vingt
    autres réponses déjà arrivées."""
    async def _fetch(pid: str):
        if pid == "pid-casse":
            raise ValueError("charge inattendue")
        return [_goal()]

    monkeypatch.setattr(data_manager, "fetch_donation_goals", _fetch)
    dm._streamers = [
        _streamer(twitch_login="casse", donation=10.0,
                  participation_id="pid-casse"),
        _streamer(twitch_login="sain", donation=5.0, participation_id="pid-sain"),
    ]
    _run(dm._prefetch_top_goals(n=20))
    assert list(dm._goals_cache) == ["sain"]
