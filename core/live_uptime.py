# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Depuis combien de temps une chaîne est en direct.

**Aucune API du projet ne le dit.** Le relevé a été fait sur les quatre
sources dont dispose l'application :

| Source | Ce qu'elle donne |
|---|---|
| `/events/{id}/participations` | `live`, `game`, `viewers`, `updated_at` — l'heure du dernier rafraîchissement CÔTÉ API, pas le début du direct |
| `zevent.fr/api/` | `online`, `game`, `viewersAmount` — rien de temporel |
| `/stats/participations` | des totaux par édition |
| `statistics/all.json` | des agrégats globaux |

Observer soi-même les passages en direct ne marche pas non plus : un soir de
ZEvent, les chaînes sont en direct depuis des heures quand on ouvre ZLink, et
tout afficherait « depuis trois minutes ».

On demande donc à Twitch, par le point d'entrée que **streamlink** — embarqué
dans ZLink et lancé pour chaque flux ouvert — utilise déjà : `gql.twitch.tv`
avec le Client-ID public du client web. Aucune clé, aucune connexion
utilisateur, et la réponse porte `createdAt`, l'heure exacte du début.

Cette interface n'est pas documentée par Twitch. Tout est donc écrit pour que
son absence ne se voie pas ailleurs qu'ici : un échec rend une durée inconnue,
jamais une exception, et l'application affiche simplement une ligne de moins.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from typing import Iterable

import httpx

from core.api_client import _safe_login

logger = logging.getLogger(__name__)

#: Client-ID public du client web Twitch. Ce n'est pas un secret : il est
#: intégré à la page twitch.tv et utilisé tel quel par streamlink et les
#: outils du même genre. Aucun jeton ne l'accompagne.
CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
URL = "https://gql.twitch.tv/gql"

#: Chaînes par requête. Une seule requête GraphQL peut porter autant d'alias
#: qu'on veut ; on borne quand même, pour que l'échec d'un lot n'emporte pas
#: la grille entière et que la requête reste de taille raisonnable.
MAX_PAR_LOT = 25

#: Durée de validité d'un relevé. Le début d'un direct ne bouge pas : on ne
#: redemande que pour repérer les arrêts et les reprises.
TTL_S = 300.0

_TIMEOUT = httpx.Timeout(8.0)

#: login → (début du direct en epoch UTC ou None, instant du relevé)
_releves: dict[str, tuple[float | None, float]] = {}


def _frais(login: str, maintenant: float) -> tuple[float | None, float] | None:
    releve = _releves.get(login)
    if releve is None or maintenant - releve[1] > TTL_S:
        return None
    return releve


def a_rafraichir(logins: Iterable[str], maintenant: float | None = None) -> list[str]:
    """Ceux dont le relevé manque ou a vieilli. Sans doublon, ordre conservé."""
    instant = time.monotonic() if maintenant is None else maintenant
    vus: set[str] = set()
    besoin = []
    for brut in logins:
        login = _safe_login(brut).lower()
        if not login or login in vus:
            continue
        vus.add(login)
        if _frais(login, instant) is None:
            besoin.append(login)
    return besoin


def _requete(logins: list[str]) -> str:
    """La requête GraphQL, un alias par chaîne.

    Les logins sont passés par `_safe_login` en amont : seuls des caractères
    de login Twitch arrivent ici, jamais de quoi refermer une chaîne et
    greffer autre chose dans la requête.
    """
    corps = " ".join(
        f'c{i}: user(login: "{login}") {{ stream {{ createdAt }} }}'
        for i, login in enumerate(logins)
    )
    return "query {" + corps + "}"


def _lire_reponse(donnees: dict, logins: list[str], instant: float) -> int:
    """Range ce que Twitch a répondu. Rend le nombre de directs datés."""
    dates = 0
    for i, login in enumerate(logins):
        noeud = (donnees.get(f"c{i}") or {})
        flux = noeud.get("stream") if isinstance(noeud, dict) else None
        debut = None
        if isinstance(flux, dict) and flux.get("createdAt"):
            debut = _epoch(str(flux["createdAt"]))
            if debut is not None:
                dates += 1
        _releves[login] = (debut, instant)
    return dates


def _epoch(iso: str) -> float | None:
    """« 2026-08-26T16:42:49Z » → secondes epoch, ou None si illisible."""
    try:
        quand = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        logger.debug("Durée de direct : date illisible %r", iso[:40])
        return None
    if quand.tzinfo is None:
        quand = quand.replace(tzinfo=datetime.timezone.utc)
    return quand.timestamp()


async def rafraichir(logins: Iterable[str], client: httpx.AsyncClient | None = None) -> int:
    """Met à jour les relevés qui en ont besoin. Rend le nombre de directs datés.

    N'échoue jamais : une interface non documentée peut disparaître du jour au
    lendemain, et ZLink doit continuer sans elle.
    """
    besoin = a_rafraichir(logins)
    if not besoin:
        return 0
    lots = [besoin[i:i + MAX_PAR_LOT] for i in range(0, len(besoin), MAX_PAR_LOT)]
    propre = client is None
    client = client or httpx.AsyncClient(timeout=_TIMEOUT)
    dates = 0
    try:
        for lot in lots:
            dates += await _interroger(client, lot)
    finally:
        if propre:
            await client.aclose()
    return dates


async def _interroger(client: httpx.AsyncClient, lot: list[str]) -> int:
    instant = time.monotonic()
    try:
        reponse = await client.post(
            URL, json={"query": _requete(lot)},
            headers={"Client-Id": CLIENT_ID}, timeout=_TIMEOUT)
        reponse.raise_for_status()
        charge = reponse.json()
    except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as exc:
        # Un relevé daté de MAINTENANT et vide : sans lui, chaque cycle
        # réinterrogerait les mêmes chaînes toutes les trente secondes.
        for login in lot:
            _releves.setdefault(login, (None, instant))
        logger.debug("Durée de direct indisponible (%d chaînes) : %s", len(lot), exc)
        return 0
    donnees = charge.get("data") if isinstance(charge, dict) else None
    if not isinstance(donnees, dict):
        logger.debug("Durée de direct : réponse inattendue")
        return 0
    return _lire_reponse(donnees, lot, instant)


def depuis(login: str, maintenant: float | None = None) -> float | None:
    """Secondes de direct, ou None si inconnu ou hors ligne."""
    releve = _releves.get(_safe_login(login).lower())
    if releve is None or releve[0] is None:
        return None
    debut, _quand = releve
    horloge = time.time() if maintenant is None else maintenant
    return max(0.0, horloge - debut)


def texte(login: str, maintenant: float | None = None) -> str:
    """« depuis 4 h 12 min », ou "" si on ne sait pas."""
    secondes = depuis(login, maintenant)
    return f"depuis {duree(secondes)}" if secondes is not None else ""


def duree(secondes: float) -> str:
    """Une durée en heures et minutes, qu'on ne puisse pas lire comme une heure.

    « depuis 3 h 09 » s'écrit exactement comme trois heures neuf du matin :
    rien ne disait s'il s'agissait d'une durée ou du moment où le direct avait
    commencé. L'unité tranche — une heure de la journée ne porte jamais
    « min » — et elle est écrite même sur les heures rondes, qui seraient
    sinon les plus trompeuses de toutes.

    Jamais de secondes : c'est une durée qu'on lit d'un coup d'œil sur une
    barre d'information, pas un chronomètre.
    """
    minutes = int(max(0.0, secondes) // 60)
    heures, reste = divmod(minutes, 60)
    if heures:
        return f"{heures} h {reste:02d} min"
    return f"{reste} min"


def oublier_tout() -> None:
    """Vide les relevés. Utile aux tests et à un changement d'édition."""
    _releves.clear()
