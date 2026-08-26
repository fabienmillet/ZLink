# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Lectures réseau : coroutines `fetch_*` et client HTTP partagé.

Ces coroutines promettent de « ne jamais lever ». Le panel les appelle depuis
des tâches de rafraîchissement où une exception passerait inaperçue, ou
emporterait la boucle : c'est donc ce contrat qu'on éprouve ici, en leur
servant tout ce qu'un vrai réseau peut rendre — une erreur HTTP, une coupure,
du HTML à la place du JSON, ou un JSON valide mais d'une forme inattendue.

Aucun accès réseau : `core.api_client._client` est détourné vers un client
factice. Les réponses, en revanche, sont de vraies `httpx.Response` construites
en mémoire, pour que `raise_for_status()` et `.json()` échouent avec les types
d'exception que le module rencontrera en production, pas avec des imitations.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from core import api_client
from core.api_client import (
    GDOC_EVENT_ID,
    GDOC_URL,
    ZEVENT_URL,
    _parse_global_stats,
    _parse_streamer_entry,
    fetch_donation_goals,
    fetch_events,
    fetch_gdoc_streamers,
    fetch_participations,
    fetch_zevent_data,
)


# ── outillage ────────────────────────────────────────────────────────────────

_REQUETE = httpx.Request("GET", "https://test.invalid/")


def _rep(charge, status: int = 200) -> httpx.Response:
    """Réponse JSON bien formée."""
    return httpx.Response(status, json=charge, request=_REQUETE)


def _rep_brute(contenu: bytes, status: int = 200) -> httpx.Response:
    """Réponse dont le corps n'est pas du JSON (page d'erreur d'un proxy…)."""
    return httpx.Response(status, content=contenu, request=_REQUETE)


class _ClientFactice:
    """Client httpx réduit à ce que le module en utilise : `.get()`.

    Mémorise les appels, ce qui permet de vérifier l'URL visée et — pour
    `fetch_donation_goals` sans identifiant — qu'aucune requête n'est partie.
    """

    def __init__(self, reponse: httpx.Response | None = None,
                 erreur: Exception | None = None) -> None:
        self.reponse = reponse
        self.erreur = erreur
        self.appels: list[tuple[str, dict | None]] = []

    async def get(self, url, params=None):
        self.appels.append((url, params))
        if self.erreur is not None:
            raise self.erreur
        return self.reponse


@pytest.fixture
def brancher(monkeypatch):
    """Détourne `_client()` vers un client factice, et le rend au test.

    Le module appelle `_client()` à chaque requête : remplacer la fonction
    suffit, et évite de toucher au cache par boucle (`_LOOP_CLIENTS`).
    """
    def _brancher(reponse=None, erreur=None) -> _ClientFactice:
        client = _ClientFactice(reponse, erreur)
        monkeypatch.setattr(api_client, "_client", lambda: client)
        return client
    return _brancher


# Tout ce qui peut mal tourner entre l'appel et un JSON exploitable. Chaque
# coroutine `fetch_*` doit traverser cette liste sans lever.
CAS_ECHEC = [
    pytest.param({"reponse": _rep({}, 500)}, id="http-500"),
    pytest.param({"reponse": _rep({}, 404)}, id="http-404"),
    pytest.param({"reponse": _rep({}, 503)}, id="http-503"),
    pytest.param({"erreur": httpx.ConnectTimeout("délai dépassé")}, id="timeout"),
    pytest.param({"erreur": httpx.ConnectError("hôte injoignable")}, id="reseau"),
    pytest.param({"erreur": httpx.RemoteProtocolError("connexion coupée")},
                 id="protocole"),
    pytest.param({"reponse": _rep_brute(b"<html>Bad Gateway</html>")},
                 id="json-invalide"),
    pytest.param({"reponse": _rep_brute(b"")}, id="corps-vide"),
]


# ── _client / close_loop_client ──────────────────────────────────────────────

def test_client_est_partage_dans_une_meme_boucle():
    """Un client par boucle, pas un par requête : sinon chaque appel refait
    une poignée de main TLS vers les mêmes hôtes."""
    async def scenario():
        a = api_client._client()
        b = api_client._client()
        try:
            return a is b, a.is_closed
        finally:
            await api_client.close_loop_client()

    identique, ferme = asyncio.run(scenario())
    assert identique
    assert not ferme


def test_client_est_recree_s_il_a_ete_ferme():
    """Un client fermé ne sert plus à rien : il doit être remplacé."""
    async def scenario():
        a = api_client._client()
        await a.aclose()
        b = api_client._client()
        try:
            return a is b, b.is_closed
        finally:
            await api_client.close_loop_client()

    identique, ferme = asyncio.run(scenario())
    assert not identique
    assert not ferme


def test_client_est_propre_a_chaque_boucle():
    """Les transports d'un client sont liés à leur boucle : une nouvelle
    boucle doit obtenir son propre client, pas celui de la précédente."""
    async def scenario():
        client = api_client._client()
        await api_client.close_loop_client()
        return client

    assert asyncio.run(scenario()) is not asyncio.run(scenario())


def test_close_loop_client_sans_client_ouvert_ne_leve_pas():
    """Fermer deux fois — ou avant toute requête — reste sans effet."""
    async def scenario():
        await api_client.close_loop_client()
        await api_client.close_loop_client()

    assert asyncio.run(scenario()) is None


def test_close_loop_client_hors_boucle_ne_leve_pas():
    """Appelée sans boucle en cours, la coroutine renonce au lieu d'échouer.

    Elle est appelée pendant l'arrêt de l'application, où la boucle peut déjà
    avoir disparu. On la fait avancer d'un pas à la main, faute de boucle
    justement : elle doit rendre la main immédiatement (StopIteration).
    """
    coro = api_client.close_loop_client()
    with pytest.raises(StopIteration):
        coro.send(None)


def test_close_loop_client_avale_une_fermeture_qui_echoue():
    """Un échec de fermeture ne doit pas remonter dans la séquence d'arrêt."""
    class _ClientRecalcitrant:
        is_closed = False

        async def aclose(self):
            raise RuntimeError("transport déjà mort")

    async def scenario():
        boucle = asyncio.get_running_loop()
        api_client._LOOP_CLIENTS[boucle] = _ClientRecalcitrant()
        await api_client.close_loop_client()
        # L'entrée est retirée malgré l'échec : sinon on retenterait sans fin.
        return boucle in api_client._LOOP_CLIENTS

    assert asyncio.run(scenario()) is False


# ── zevent.fr/api/ : parsing ─────────────────────────────────────────────────

def _streamer_zevent(**extra):
    base = {
        "twitch": "zerator",
        "display": "ZeratoR",
        "online": True,
        "game": "Minecraft",
        "location": "lan",
        "viewersAmount": {"number": 42000},
        "donationAmount": {"number": 694000.0, "formatted": "694 000 €"},
        "profileUrl": "https://static.test/z.png",
        "title": "On lance le ZEvent !",
        "donationUrl": "https://zevent.fr/dons/zerator",
    }
    base.update(extra)
    return base


def _payload_zevent(**extra):
    base = {
        "donationAmount": {"number": 1154211.58, "formatted": "1 154 212 €"},
        "viewersCount": {"number": 120000},
        "websiteMode": "live",
        "live": [_streamer_zevent()],
    }
    base.update(extra)
    return base


def test_stats_globales_completes():
    stats = _parse_global_stats(_payload_zevent())
    assert stats.donation_total == pytest.approx(1154211.58)
    assert stats.donation_formatted == "1 154 212 €"
    assert stats.viewers_total == 120000
    assert stats.website_mode == "live"


def test_stats_globales_acceptent_un_compteur_de_viewers_nu():
    """L'API a déjà renvoyé `viewersCount` comme nombre et non comme bloc."""
    assert _parse_global_stats({"viewersCount": 4200}).viewers_total == 4200


def test_stats_globales_sans_montant_retombent_sur_les_valeurs_neutres():
    stats = _parse_global_stats({"viewersCount": 1})
    assert stats.donation_total == 0.0
    assert stats.donation_formatted == "0 €"
    assert stats.website_mode == "offline"


@pytest.mark.parametrize("charge", [
    pytest.param({"donationAmount": {"number": 12.0}}, id="absent"),
    pytest.param({"donationAmount": {"number": 12.0}, "viewersCount": {}},
                 id="bloc-vide"),
    pytest.param({"donationAmount": {"number": 12.0},
                  "viewersCount": {"number": None}}, id="nombre-nul"),
    pytest.param({"donationAmount": {"number": 12.0}, "viewersCount": 0},
                 id="zero-spectateur"),
])
def test_stats_globales_sans_compteur_de_viewers(charge):
    """Sans spectateurs annoncés, on attend 0 spectateur — pas une exception.

    Le cas n'est pas théorique : hors direct, l'API peut très bien ne rien
    annoncer, et le total des dons, lui, reste une information à afficher.
    """
    stats = _parse_global_stats(charge)
    assert stats.viewers_total == 0
    assert stats.donation_total == pytest.approx(12.0)


def test_entree_streamer_complete():
    s = _parse_streamer_entry(_streamer_zevent())
    assert s.twitch_login == "zerator"
    assert s.display == "ZeratoR"
    assert s.online is True
    assert s.game == "Minecraft"
    assert s.location == "lan"
    assert s.viewers == 42000
    assert s.donation == pytest.approx(694000.0)
    assert s.donation_formatted == "694 000 €"
    assert s.profile_url == "https://static.test/z.png"
    assert s.title == "On lance le ZEvent !"
    assert s.donation_url == "https://zevent.fr/dons/zerator"


def test_entree_streamer_vide_ne_leve_pas():
    """Les blocs `donationAmount` / `viewersAmount` peuvent manquer."""
    s = _parse_streamer_entry({})
    assert s.twitch_login == "" and s.viewers == 0
    assert s.donation == 0.0 and s.donation_formatted == "0 €"


def test_entree_streamer_reprend_le_login_faute_de_nom():
    assert _parse_streamer_entry({"twitch": "zerator"}).display == "zerator"


@pytest.mark.parametrize("brut", [
    "https://evil.test/dons",        # hôte hors allowlist
    "http://zevent.fr/dons",         # pas de TLS
    "javascript:alert(1)",
])
def test_entree_streamer_rejette_un_lien_de_don_detourne(brut):
    """Le lien s'ouvre dans le navigateur embarqué, sans barre d'adresse :
    un hôte détourné y serait indétectable."""
    entree = _streamer_zevent(donationUrl=brut)
    assert _parse_streamer_entry(entree).donation_url == ""


def test_entree_streamer_rejette_un_avatar_non_https():
    entree = _streamer_zevent(profileUrl="http://static.test/z.png")
    assert _parse_streamer_entry(entree).profile_url == ""


# ── fetch_zevent_data ────────────────────────────────────────────────────────

def test_fetch_zevent_data_reponse_normale(brancher):
    client = brancher(_rep(_payload_zevent()))
    stats, streamers = asyncio.run(fetch_zevent_data())
    assert client.appels == [(ZEVENT_URL, None)]
    assert stats.viewers_total == 120000
    assert stats.website_mode == "live"
    assert [s.twitch_login for s in streamers] == ["zerator"]


def test_fetch_zevent_data_ecarte_les_logins_invalides(brancher):
    """Une entrée douteuse ne doit pas emporter les autres : le login sert de
    nom de fichier et d'argument de sous-processus, on ne jette que lui."""
    brancher(_rep(_payload_zevent(live=[
        _streamer_zevent(),
        _streamer_zevent(twitch="ze rator", display="Douteux"),
        _streamer_zevent(twitch="", display="Anonyme"),
        _streamer_zevent(twitch="mistermv", display="MisterMV"),
    ])))
    _, streamers = asyncio.run(fetch_zevent_data())
    assert [s.twitch_login for s in streamers] == ["zerator", "mistermv"]


def test_fetch_zevent_data_sans_liste_live_garde_les_stats(brancher):
    """Avant l'ouverture, l'API annonce les compteurs sans aucun direct."""
    brancher(_rep({"donationAmount": {"number": 42.0, "formatted": "42 €"},
                   "viewersCount": {"number": 7}, "websiteMode": "offline"}))
    stats, streamers = asyncio.run(fetch_zevent_data())
    assert stats.donation_total == pytest.approx(42.0)
    assert streamers == []


@pytest.mark.parametrize("charge", [
    pytest.param([], id="liste-au-lieu-d-objet"),
    pytest.param([{"twitch": "zerator"}], id="liste-de-streamers"),
    pytest.param("donnees", id="chaine"),
    pytest.param(None, id="null"),
    pytest.param(42, id="nombre"),
    pytest.param({"viewersCount": 1, "live": "zerator"}, id="live-chaine"),
    pytest.param({"viewersCount": 1, "live": [None, 3]},
                 id="live-sans-objets"),
    pytest.param({"viewersCount": 1, "live": {"a": {}}}, id="live-objet"),
])
def test_fetch_zevent_data_forme_inattendue(brancher, charge):
    """Un JSON valide mais d'une autre forme rend les valeurs neutres."""
    brancher(_rep(charge))
    stats, streamers = asyncio.run(fetch_zevent_data())
    assert (stats.donation_total, stats.viewers_total) == (0.0, 0)
    assert stats.website_mode == "offline"
    assert streamers == []


@pytest.mark.parametrize("panne", CAS_ECHEC)
def test_fetch_zevent_data_ne_leve_jamais(brancher, panne):
    brancher(**panne)
    stats, streamers = asyncio.run(fetch_zevent_data())
    assert stats.donation_formatted == "0 €"
    assert stats.website_mode == "offline"
    assert streamers == []


# ── fetch_participations / fetch_gdoc_streamers ──────────────────────────────

def _participation(**extra):
    base = {
        "id": "sid-1",
        "participation_id": "part-1",
        "name": "ZeratoR",
        "location": "lan",
        "live": True,
        "amount_raised": 69400000,
        "profile_url": "https://static.test/z.png",
        "streamers": [{
            "id": "sid-1",
            "name": "ZeratoR",
            "socials": {"twitch": {"login": "zerator"}},
            "streaming_states": [
                {"platform": "twitch", "live": True, "game": "Minecraft",
                 "viewers": 42000},
            ],
        }],
    }
    base.update(extra)
    return base


def _mistermv():
    return _participation(id="sid-2", participation_id="part-2", name="MisterMV",
                          streamers=[{"id": "sid-2", "name": "MisterMV",
                                      "socials": {"twitch": {"login": "MisterMV"}},
                                      "streaming_states": []}])


def _sans_login():
    """Participation dont on ne peut rien faire : pas de login Twitch."""
    return _participation(streamers=[{"id": "sid-1", "name": "Anonyme",
                                      "socials": {}, "streaming_states": []}])


def _sans_identifiant():
    """Participation sans id : rien ne peut lui être rattaché."""
    return _participation(id="", streamers=[{
        "socials": {"twitch": {"login": "fantome"}}, "streaming_states": []}])


def test_fetch_participations_reponse_normale(brancher):
    client = brancher(_rep([_participation()]))
    parts = asyncio.run(fetch_participations())
    assert client.appels == [
        (f"{GDOC_URL}/events/{GDOC_EVENT_ID}/participations", None)]
    assert len(parts) == 1
    assert parts[0].twitch_login == "zerator"
    assert parts[0].participation_id == "part-1"
    assert parts[0].location == "LAN"
    assert parts[0].donation == pytest.approx(694000.0)


@pytest.mark.parametrize("bancale", [
    pytest.param(_sans_login(), id="sans-login"),
    pytest.param(_sans_identifiant(), id="sans-id"),
])
def test_fetch_participations_ecarte_les_entrees_inexploitables(
        brancher, bancale):
    """Login et identifiant sont les deux clés de rattachement : sans l'un des
    deux l'entrée ne relie rien, mais ses voisines restent bonnes."""
    brancher(_rep([_participation(), bancale, _mistermv()]))
    parts = asyncio.run(fetch_participations())
    assert [p.twitch_login for p in parts] == ["zerator", "mistermv"]


@pytest.mark.parametrize("charge", [
    pytest.param({}, id="objet-au-lieu-de-liste"),
    pytest.param({"participations": [1]}, id="objet-enveloppe"),
    pytest.param(None, id="null"),
    pytest.param(7, id="nombre"),
    pytest.param([None, "x", 3], id="liste-sans-objets"),
])
def test_fetch_participations_forme_inattendue(brancher, charge):
    brancher(_rep(charge))
    assert asyncio.run(fetch_participations()) == []


@pytest.mark.parametrize("panne", CAS_ECHEC)
def test_fetch_participations_ne_leve_jamais(brancher, panne):
    brancher(**panne)
    assert asyncio.run(fetch_participations()) == []


def test_fetch_gdoc_streamers_associe_login_et_identifiant(brancher):
    brancher(_rep([_participation(), _mistermv()]))
    # Le login est normalisé en minuscules : c'est la clé de rapprochement
    # avec l'API ZEvent, qui les renvoie déjà ainsi.
    assert asyncio.run(fetch_gdoc_streamers()) == {
        "zerator": "sid-1", "mistermv": "sid-2"}


@pytest.mark.parametrize("panne", CAS_ECHEC)
def test_fetch_gdoc_streamers_ne_leve_jamais(brancher, panne):
    """La table est vide plutôt qu'absente : l'appelant itère dessus."""
    brancher(**panne)
    assert asyncio.run(fetch_gdoc_streamers()) == {}


# ── fetch_events ─────────────────────────────────────────────────────────────

_JOUR = "2026-09-05"


def _show(**extra):
    base = {
        "id": "show-1",
        "name": "Le Zbowl",
        "description": "Match de gala",
        "schedule": {"start": "2026-09-05T16:30:00Z",
                     "end": "2026-09-05T18:00:00Z"},
        "participants": [
            {"streamer_id": "h1", "streamer_name": "Hôte", "role": "host",
             "socials": {"twitch": {"login": "hote"}},
             "profile_url": "https://static.test/h.png"},
            {"streamer_id": "p1", "streamer_name": "Invité", "role": "guest",
             "socials": {"twitch": {"login": "invite"}}},
        ],
    }
    base.update(extra)
    return base


def test_fetch_events_reponse_normale(brancher):
    client = brancher(_rep([_show()]))
    events = asyncio.run(fetch_events(_JOUR))
    assert client.appels == [
        (f"{GDOC_URL}/events/{GDOC_EVENT_ID}/shows", {"day": _JOUR})]
    ev = events[0]
    assert ev.id == "show-1" and ev.name == "Le Zbowl"
    assert ev.day == _JOUR
    assert ev.description == "Match de gala"
    assert (ev.start_local, ev.end_local) == ("18:30", "20:00")
    assert ev.start_ts == pytest.approx(1788625800.0)
    assert ev.end_ts > ev.start_ts
    assert ev.host_uuids == ["h1"] and ev.participant_uuids == ["p1"]
    assert ev.names == {"h1": "Hôte", "p1": "Invité"}
    assert ev.logins == {"h1": "hote", "p1": "invite"}
    # L'avatar d'un invité n'existe que dans la charge du show : le perdre le
    # rendrait introuvable, il ne figure pas dans la liste des streamers.
    assert ev.profile_urls == {"h1": "https://static.test/h.png"}


@pytest.mark.parametrize("cle_debut,cle_fin", [
    ("start_at", "end_at"),
    ("startAt", "endAt"),
    ("start", "end"),
])
def test_fetch_events_accepte_les_horaires_hors_schedule(
        brancher, cle_debut, cle_fin):
    """Les éditions précédentes plaçaient les horaires à la racine du show."""
    brancher(_rep([_show(schedule=None, **{
        cle_debut: "2026-09-05T16:30:00Z", cle_fin: "2026-09-05T18:00:00Z"})]))
    ev = asyncio.run(fetch_events(_JOUR))[0]
    assert (ev.start_local, ev.end_local) == ("18:30", "20:00")


def test_fetch_events_show_minimal_ne_leve_pas(brancher):
    """Un show sans horaire ni participant reste affichable, en creux."""
    brancher(_rep([{"title": "Sans détails"}]))
    ev = asyncio.run(fetch_events(_JOUR))[0]
    assert ev.name == "Sans détails"
    assert (ev.start_local, ev.end_local) == ("", "")
    assert (ev.start_ts, ev.end_ts) == (0.0, 0.0)
    assert ev.host_uuids == [] and ev.names == {}


def test_fetch_events_horaire_illisible_reste_affichable(brancher):
    brancher(_rep([_show(schedule={"start": "bientôt", "end": ""})]))
    ev = asyncio.run(fetch_events(_JOUR))[0]
    assert ev.start_ts == 0.0
    assert ev.start_local == "bient"   # repli : les 5 premiers caractères


def test_fetch_events_garde_ce_qui_precede_une_entree_illisible(brancher):
    """L'analyse s'arrête à la première entrée aberrante, mais ce qui a déjà
    été lu est rendu : mieux vaut un programme partiel que pas de programme."""
    brancher(_rep([_show(), "pas un show", _show(id="show-3")]))
    assert [e.id for e in asyncio.run(fetch_events(_JOUR))] == ["show-1"]


@pytest.mark.parametrize("charge", [
    pytest.param({}, id="objet-au-lieu-de-liste"),
    pytest.param({"shows": []}, id="objet-enveloppe"),
    pytest.param(None, id="null"),
    pytest.param("2026-09-05", id="chaine"),
    pytest.param([None], id="liste-sans-objets"),
])
def test_fetch_events_forme_inattendue(brancher, charge):
    brancher(_rep(charge))
    assert asyncio.run(fetch_events(_JOUR)) == []


@pytest.mark.parametrize("panne", CAS_ECHEC)
def test_fetch_events_ne_leve_jamais(brancher, panne):
    brancher(**panne)
    assert asyncio.run(fetch_events(_JOUR)) == []


# ── fetch_donation_goals ─────────────────────────────────────────────────────

def _goal(**extra):
    base = {"id": "g1", "name": "Chanter en direct", "amount": 5000,
            "accomplished": True, "category": "défi",
            "links": ["https://zevent.fr/g1"]}
    base.update(extra)
    return base


def test_fetch_donation_goals_reponse_normale(brancher):
    client = brancher(_rep([_goal()]))
    goals = asyncio.run(fetch_donation_goals("part-1"))
    assert client.appels == [
        (f"{GDOC_URL}/participations/part-1/donation_goals", None)]
    g = goals[0]
    assert g.id == "g1" and g.name == "Chanter en direct"
    assert g.amount == pytest.approx(50.0)   # centimes → euros
    assert g.accomplished is True
    assert g.category == "défi"
    assert g.links == ["https://zevent.fr/g1"]


def test_fetch_donation_goals_sans_identifiant_n_appelle_pas_l_api(brancher):
    """Sans participation_id, l'URL viserait /participations//donation_goals :
    autant s'arrêter avant la requête."""
    client = brancher(_rep([_goal()]))
    assert asyncio.run(fetch_donation_goals("")) == []
    assert client.appels == []


@pytest.mark.parametrize("champ", ["accomplished", "done"])
def test_fetch_donation_goals_reconnait_les_deux_noms_d_accomplissement(
        brancher, champ):
    brancher(_rep([{"id": "g1", champ: True}]))
    assert asyncio.run(fetch_donation_goals("part-1"))[0].accomplished is True


@pytest.mark.parametrize("champ", ["category", "type", "nature"])
def test_fetch_donation_goals_reconnait_les_trois_noms_de_categorie(
        brancher, champ):
    """Le nom du champ a changé d'une édition à l'autre."""
    brancher(_rep([{"id": "g1", champ: "défi"}]))
    assert asyncio.run(fetch_donation_goals("part-1"))[0].category == "défi"


def test_fetch_donation_goals_objectif_minimal_ne_leve_pas(brancher):
    brancher(_rep([{}]))
    g = asyncio.run(fetch_donation_goals("part-1"))[0]
    assert (g.id, g.name, g.category) == ("", "", "")
    assert g.amount == 0.0 and g.accomplished is False and g.links == []


def test_fetch_donation_goals_montant_illisible_vaut_zero(brancher):
    """Un montant aberrant ne doit pas faire disparaître l'objectif."""
    brancher(_rep([_goal(amount="beaucoup")]))
    assert asyncio.run(fetch_donation_goals("part-1"))[0].amount == 0.0


def test_fetch_donation_goals_garde_ce_qui_precede_une_entree_illisible(
        brancher):
    brancher(_rep([_goal(), "pas un objectif", _goal(id="g3")]))
    assert [g.id for g in asyncio.run(fetch_donation_goals("part-1"))] == ["g1"]


@pytest.mark.parametrize("charge", [
    pytest.param({}, id="objet-au-lieu-de-liste"),
    pytest.param({"goals": []}, id="objet-enveloppe"),
    pytest.param(None, id="null"),
    pytest.param(3, id="nombre"),
    pytest.param([None, 1], id="liste-sans-objets"),
])
def test_fetch_donation_goals_forme_inattendue(brancher, charge):
    brancher(_rep(charge))
    assert asyncio.run(fetch_donation_goals("part-1")) == []


@pytest.mark.parametrize("panne", CAS_ECHEC)
def test_fetch_donation_goals_ne_leve_jamais(brancher, panne):
    brancher(**panne)
    assert asyncio.run(fetch_donation_goals("part-1")) == []
