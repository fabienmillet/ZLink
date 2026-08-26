# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Depuis quand une chaîne est en direct.

La donnée vient d'une interface Twitch NON DOCUMENTÉE : elle peut changer de
forme ou disparaître sans préavis. Ces tests portent donc surtout sur ce qui
arrive quand elle se dérobe — l'application doit continuer sans, en affichant
une ligne de moins.

Aucun test n'ouvre de connexion : les réponses sont fournies telles que Twitch
les renvoie, relevées sur l'API réelle.
"""

from __future__ import annotations

import asyncio
import datetime

import httpx
import pytest

from core import live_uptime as lu


@pytest.fixture(autouse=True)
def table_vide():
    lu.oublier_tout()
    yield
    lu.oublier_tout()


def _reponse(payload, statut=200):
    """Un client httpx dont chaque POST rend `payload`."""
    def repondre(_requete):
        return httpx.Response(statut, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(repondre))


def _il_y_a(heures: float) -> str:
    quand = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=heures)
    return quand.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── lecture de la réponse ────────────────────────────────────────────────────

def test_une_duree_est_lue_depuis_l_heure_de_debut():
    async def essai():
        async with _reponse({"data": {"c0": {"stream": {
                "createdAt": _il_y_a(4.2)}}}}) as client:
            return await lu.rafraichir(["aducine"], client=client)

    assert asyncio.run(essai()) == 1
    assert lu.texte("aducine") == "depuis 4 h 12 min"


def test_une_chaine_hors_ligne_n_a_pas_de_duree():
    """Twitch rend `stream: null` : ce n'est pas une erreur, c'est une réponse."""
    async def essai():
        async with _reponse({"data": {"c0": {"stream": None}}}) as client:
            await lu.rafraichir(["aducine"], client=client)

    asyncio.run(essai())
    assert lu.depuis("aducine") is None
    assert lu.texte("aducine") == ""


def test_plusieurs_chaines_tiennent_dans_une_requete():
    async def essai():
        charge = {"data": {"c0": {"stream": {"createdAt": _il_y_a(1)}},
                           "c1": {"stream": None},
                           "c2": {"stream": {"createdAt": _il_y_a(9)}}}}
        async with _reponse(charge) as client:
            return await lu.rafraichir(["un", "deux", "trois"], client=client)

    assert asyncio.run(essai()) == 2
    assert lu.texte("un") == "depuis 1 h 00 min"
    assert lu.texte("deux") == ""
    assert lu.texte("trois") == "depuis 9 h 00 min"


# ── ce qui doit rester sans conséquence ──────────────────────────────────────

@pytest.mark.parametrize("charge,statut", [
    ({"errors": [{"message": "service unavailable"}]}, 200),
    ({"data": None}, 200),
    ({}, 503),
    ("pas du json", 200),
])
def test_une_interface_qui_se_derobe_ne_fait_rien_tomber(charge, statut):
    """Non documentée : elle peut disparaître du jour au lendemain."""
    async def essai():
        def repondre(_requete):
            if isinstance(charge, str):
                return httpx.Response(statut, text=charge)
            return httpx.Response(statut, json=charge)

        async with httpx.AsyncClient(
                transport=httpx.MockTransport(repondre)) as client:
            return await lu.rafraichir(["aducine"], client=client)

    assert asyncio.run(essai()) == 0
    assert lu.texte("aducine") == ""


def test_un_echec_n_est_pas_redemande_a_chaque_cycle():
    """Sans mémoriser l'échec, les mêmes chaînes repartiraient toutes les 30 s."""
    async def essai():
        def repondre(_requete):
            return httpx.Response(503, json={})

        async with httpx.AsyncClient(
                transport=httpx.MockTransport(repondre)) as client:
            await lu.rafraichir(["aducine"], client=client)

    asyncio.run(essai())
    assert lu.a_rafraichir(["aducine"]) == []


def test_une_date_illisible_est_ignoree():
    async def essai():
        async with _reponse({"data": {"c0": {"stream": {
                "createdAt": "hier soir"}}}}) as client:
            return await lu.rafraichir(["aducine"], client=client)

    assert asyncio.run(essai()) == 0
    assert lu.texte("aducine") == ""


# ── ce qu'on redemande, et quand ─────────────────────────────────────────────

def test_un_releve_frais_n_est_pas_redemande():
    """Le début d'un direct ne bouge pas : le redemander n'apprend rien."""
    async def essai():
        async with _reponse({"data": {"c0": {"stream": {
                "createdAt": _il_y_a(2)}}}}) as client:
            await lu.rafraichir(["aducine"], client=client)

    asyncio.run(essai())
    assert lu.a_rafraichir(["aducine"]) == []


def test_un_releve_perime_est_redemande(monkeypatch):
    """Il faut bien repérer les arrêts et les reprises."""
    async def essai():
        async with _reponse({"data": {"c0": {"stream": {
                "createdAt": _il_y_a(2)}}}}) as client:
            await lu.rafraichir(["aducine"], client=client)

    asyncio.run(essai())
    tard = [lu.time.monotonic() + lu.TTL_S + 1.0]
    assert lu.a_rafraichir(["aducine"], maintenant=tard[0]) == ["aducine"]


def test_les_doublons_ne_sont_demandes_qu_une_fois():
    assert lu.a_rafraichir(["aducine", "Aducine", "aducine"]) == ["aducine"]


def test_un_login_invalide_est_ecarte():
    """Les logins sont interpolés dans la requête : rien d'autre ne passe."""
    assert lu.a_rafraichir(['" } evil { "']) == []


def test_la_requete_ne_porte_que_des_logins_valides():
    requete = lu._requete(["aducine", "zerator"])
    assert 'c0: user(login: "aducine")' in requete
    assert 'c1: user(login: "zerator")' in requete
    assert requete.count("createdAt") == 2


# ── mise en forme ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("secondes,attendu", [
    (0, "0 min"),
    (59, "0 min"),          # la mesure n'a pas les secondes, on ne les invente pas
    (60, "1 min"),
    (3599, "59 min"),
    (3600, "1 h 00 min"),
    (15_120, "4 h 12 min"),
    (31_800, "8 h 50 min"),
    (-5, "0 min"),          # une horloge qui recule ne doit rien produire d'absurde
])
def test_duree_lisible(secondes, attendu):
    assert lu.duree(secondes) == attendu


def test_une_duree_ne_peut_pas_se_lire_comme_une_heure():
    """« depuis 3 h 09 » s'écrit comme trois heures neuf du matin.

    L'unité tranche, et elle doit figurer aussi sur les heures rondes — « 3 h »
    tout seul est encore plus trompeur que « 3 h 09 ».
    """
    for secondes in (3600, 3600 * 3 + 540, 3600 * 12):
        assert lu.duree(secondes).endswith(" min")
