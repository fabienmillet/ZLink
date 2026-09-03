# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Les clips Twitch de la catégorie ZEvent.

Ce que la page `twitch.tv/directory/category/zevent/clips` montre, ramené dans
le panel. Le meilleur du plateau y remonte tout seul : un moment fort clippé
par la communauté est vu par elle avant de l'être par nous.

Trois décisions, et les trois comptent :

**La même porte que le reste.** `gql.twitch.tv` avec le Client-ID public du
client web, celui que `core/live_uptime.py` utilise déjà. Aucune clé, aucune
connexion, rien à configurer — et une dépendance de moins que l'API Helix, qui
exige une application enregistrée et un jeton à renouveler.

**Une seule requête, pas de pagination.** `first: 100` rend jusqu'à
quatre-vingts clips ; au-delà, Twitch oppose un contrôle d'intégrité aux
requêtes paginées. Quatre-vingts suffisent largement à une liste qu'on parcourt
en diagonale, et c'est une requête au lieu de cinq.

**Sept jours au plus.** Sans cette borne, la catégorie remonte les clips des
éditions précédentes — un moment de 2025 n'a rien à faire au milieu de ceux de
la nuit dernière. Le tri, lui, se fait ici : l'API n'offre que « vues » et
« tendance », alors qu'on veut aussi la date, la durée et la chaîne.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from core.live_uptime import CLIENT_ID, URL

logger = logging.getLogger(__name__)

#: Le slug de la catégorie, tel qu'il apparaît dans l'adresse de la page.
SLUG_CATEGORIE = "zevent"

#: Demandés en une fois. Twitch en rend une part seulement — soixante-dix-huit
#: pour cent demandés lors du relevé — et refuse les pages suivantes.
PAR_REQUETE = 100

#: Fenêtre retenue. « Sept jours » est ce que demande la page de Twitch, et ce
#: qui écarte les clips de l'édition précédente.
PERIODE = "LAST_WEEK"

_TIMEOUT = httpx.Timeout(15.0)

#: Tris proposés. Les deux premiers viennent de l'API ; les autres se font sur
#: place, sur la liste déjà chargée — la redemander pour la retrier serait une
#: requête pour rien.
TRIS: dict[str, str] = {
    "vues": "Les plus vus",
    "recents": "Les plus récents",
    "duree": "Les plus longs",
    "chaine": "Par chaîne",
}


@dataclass(frozen=True)
class Clip:
    """Un clip, réduit à ce que la liste et le lecteur en demandent."""

    slug: str
    titre: str
    vues: int
    cree_le: float                # epoch UTC
    duree_s: float
    login: str
    chaine: str
    auteur: str                   # qui l'a clippé
    vignette: str

    @property
    def url(self) -> str:
        """La page du clip sur Twitch, pour qui veut l'ouvrir ailleurs."""
        return f"https://clips.twitch.tv/{self.slug}"


_REQUETE_LISTE = """
{
  game(slug: "%(slug)s") {
    clips(first: %(nombre)d, criteria: {period: %(periode)s, sort: VIEWS_DESC}) {
      edges { node {
        slug title viewCount createdAt durationSeconds
        broadcaster { login displayName }
        curator { displayName }
        thumbnailURL(width: 480, height: 272)
      } }
    }
  }
}
"""

#: La lecture demande DEUX choses : les pistes disponibles et un jeton signé.
#: Sans le jeton, le CDN répond 403 — l'URL seule ne suffit pas.
_REQUETE_LECTURE = """
{
  clip(slug: "%(slug)s") {
    videoQualities { quality sourceURL }
    playbackAccessToken(params: {platform: "web", playerBackend: "mediaplayer",
                                 playerType: "clips"}) { signature value }
  }
}
"""


def _instant(iso: str) -> float:
    """« 2026-09-03T18:18:00Z » → epoch. Zéro si la date est illisible."""
    try:
        return datetime.fromisoformat(
            str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        logger.debug("Clip : date illisible %r", iso)
        return 0.0


def _lire(noeud: dict) -> Clip | None:
    """Un nœud de la réponse → un Clip, ou None s'il lui manque l'essentiel."""
    slug = str((noeud or {}).get("slug") or "").strip()
    if not slug:
        return None
    diffuseur = noeud.get("broadcaster") or {}
    curateur = noeud.get("curator") or {}
    return Clip(
        slug=slug,
        titre=str(noeud.get("title") or "").strip() or "Sans titre",
        vues=int(noeud.get("viewCount") or 0),
        cree_le=_instant(noeud.get("createdAt")),
        duree_s=float(noeud.get("durationSeconds") or 0.0),
        login=str(diffuseur.get("login") or "").lower(),
        chaine=str(diffuseur.get("displayName") or diffuseur.get("login") or ""),
        auteur=str(curateur.get("displayName") or ""),
        vignette=str(noeud.get("thumbnailURL") or ""),
    )


async def _demander(requete: str, client: httpx.AsyncClient | None = None) -> dict:
    """Une requête GraphQL. Rend {} plutôt que de lever.

    Un clip manquant n'empêche pas de suivre l'événement : la liste reste
    celle du dernier chargement, et le journal dit pourquoi.
    """
    propre = client is None
    client = client or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        reponse = await client.post(URL, json={"query": requete},
                                    headers={"Client-Id": CLIENT_ID})
        reponse.raise_for_status()
        charge = reponse.json()
    except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as exc:
        logger.warning("Clips : requête refusée — %s", exc)
        return {}
    finally:
        if propre:
            await client.aclose()
    if isinstance(charge, dict) and charge.get("errors"):
        logger.warning("Clips : %s", str(charge["errors"])[:160])
    return (charge or {}).get("data") or {}


async def lister(client: httpx.AsyncClient | None = None) -> list[Clip]:
    """Les clips de la catégorie sur les sept derniers jours, les plus vus d'abord."""
    donnees = await _demander(
        _REQUETE_LISTE % {"slug": SLUG_CATEGORIE, "nombre": PAR_REQUETE,
                          "periode": PERIODE},
        client)
    jeu = donnees.get("game") or {}
    aretes = ((jeu.get("clips") or {}).get("edges")) or []
    clips = [c for c in (_lire(a.get("node") or {}) for a in aretes) if c]
    logger.info("Clips : %d chargés sur la catégorie %s", len(clips),
                SLUG_CATEGORIE)
    return clips


async def url_de_lecture(slug: str,
                         client: httpx.AsyncClient | None = None) -> str:
    """L'adresse du MP4, signée, prête pour mpv. Vide si elle se dérobe.

    Le lecteur de ZLink plutôt que l'embed de Twitch : celui-ci vérifie le
    domaine de la page qui l'accueille, ce qu'une application de bureau n'a
    pas. Le MP4 direct évite ce contrôle, et surtout il se lit avec le lecteur
    que le reste du logiciel utilise déjà.
    """
    donnees = await _demander(_REQUETE_LECTURE % {"slug": slug}, client)
    clip = donnees.get("clip") or {}
    pistes = clip.get("videoQualities") or []
    jeton = clip.get("playbackAccessToken") or {}
    if not pistes or not jeton.get("signature"):
        logger.warning("Clips : lecture indisponible pour %s", slug)
        return ""
    # La première piste est la meilleure : Twitch les rend par qualité
    # décroissante.
    parametres = urllib.parse.urlencode({"sig": jeton["signature"],
                                         "token": jeton["value"]})
    return f"{pistes[0]['sourceURL']}?{parametres}"


def trier(clips: list[Clip], cle: str) -> list[Clip]:
    """La liste triée. Une clé inconnue laisse l'ordre reçu de Twitch."""
    if cle == "recents":
        return sorted(clips, key=lambda c: -c.cree_le)
    if cle == "duree":
        return sorted(clips, key=lambda c: -c.duree_s)
    if cle == "chaine":
        # La chaîne d'abord, puis les vues : un tri par nom qui mélangerait
        # les clips d'un même streamer n'aiderait pas à les parcourir.
        return sorted(clips, key=lambda c: (c.chaine.lower(), -c.vues))
    return sorted(clips, key=lambda c: -c.vues)
