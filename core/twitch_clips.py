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

#: Demandés par chaîne. Twitch en rend jusqu'à ce nombre, et refuse les pages
#: suivantes : le curseur se heurte à un contrôle d'intégrité signé, que seul
#: le client web sait produire. Une page large est donc tout ce qu'on peut
#: obtenir — mais une par chaîne, ce qui suffit largement.
PAR_REQUETE = 100

#: Chaînes par requête. GraphQL accepte autant d'alias qu'on veut dans un même
#: document ; on borne quand même, pour que l'échec d'un lot n'emporte pas la
#: liste entière et que la requête reste de taille raisonnable. C'est le
#: réglage que `core/live_uptime.py` applique déjà aux durées de direct.
MAX_PAR_LOT = 25

#: Fenêtre demandée à Twitch. C'est la plus fine qu'il propose au-dessus de la
#: journée, et elle écarte déjà les éditions précédentes.
PERIODE = "LAST_WEEK"


def depuis_quand() -> float:
    """L'instant avant lequel un clip n'a rien à voir avec l'événement.

    Interroger les participants chaîne par chaîne rattrape tous leurs clips, y
    compris ceux de leurs streams ordinaires : sur quatre chaînes du plateau,
    cent cinquante-six clips précédaient l'ouverture de la cagnotte contre un
    seul après — du VALORANT, du PUBG, rien du ZEvent.

    Sept jours ne suffisent donc pas à trancher, et la catégorie du clip non
    plus : pendant l'événement les participants jouent à tout, et un clip garde
    la catégorie du moment où il a été pris. C'est la DATE qui sépare — depuis
    l'ouverture de la cagnotte, ce qu'un participant clippe est du ZEvent.
    """
    from core.history_store import OUVERTURE_CAGNOTTE

    return OUVERTURE_CAGNOTTE

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


_REQUETE_CHAINE = """u%(rang)d: user(login: "%(login)s") {
    clips(first: %(nombre)d, criteria: {period: %(periode)s, sort: VIEWS_DESC}) {
      edges { node {
        slug title viewCount createdAt durationSeconds
        broadcaster { login displayName }
        curator { displayName }
        thumbnailURL(width: 480, height: 272)
      } }
    }
  }"""


def _lot(logins: list[str]) -> str:
    """Un document GraphQL qui interroge plusieurs chaînes d'un coup."""
    return "{\n" + "\n".join(
        _REQUETE_CHAINE % {"rang": rang, "login": login,
                           "nombre": PAR_REQUETE, "periode": PERIODE}
        for rang, login in enumerate(logins)) + "\n}"


def _clips_du_lot(donnees: dict, combien: int) -> tuple[list[Clip], int]:
    """Les clips d'un lot d'alias, et combien ont été écartés.

    Sortie de `lister_par_chaines` pour la garder lisible : trois boucles
    imbriquées et deux conditions dans une fonction qui gère aussi son client
    HTTP, on ne voyait plus laquelle faisait quoi.
    """
    plancher = depuis_quand()
    retenus: list[Clip] = []
    ecartes = 0
    for rang in range(combien):
        chaine = donnees.get(f"u{rang}") or {}
        for arete in ((chaine.get("clips") or {}).get("edges")) or []:
            clip = _lire(arete.get("node") or {})
            if clip is None:
                continue
            # Le stream ordinaire d'un participant n'est pas le ZEvent.
            if clip.cree_le < plancher:
                ecartes += 1
                continue
            retenus.append(clip)
    return retenus, ecartes


async def lister_par_chaines(logins: list[str],
                             client: httpx.AsyncClient | None = None
                             ) -> list[Clip]:
    """Les clips des chaînes données, sur les sept derniers jours.

    La catégorie ne voit que les clips ÉTIQUETÉS ZEvent : soixante-dix-huit au
    total, là où six chaînes seules en rendent deux cent quarante-neuf. Un clip
    n'hérite pas de la catégorie de la chaîne — il porte celle du moment où il
    a été pris, et une chaîne qui bascule sur autre chose entre deux temps
    forts sort du décompte.

    Interroger chaîne par chaîne les rattrape tous. En lots, pour ne pas faire
    trois cents requêtes là où douze suffisent.
    """
    propre = client is None
    client = client or httpx.AsyncClient(timeout=_TIMEOUT)
    connus: dict[str, Clip] = {}
    ecartes = 0
    try:
        for depart in range(0, len(logins), MAX_PAR_LOT):
            lot = logins[depart:depart + MAX_PAR_LOT]
            donnees = await _demander(_lot(lot), client)
            retenus, hors_sujet = _clips_du_lot(donnees, len(lot))
            ecartes += hors_sujet
            # Dédoublonné : un clip peut remonter par sa chaîne ET par la
            # catégorie, et deux cartes pour un même moment se remarquent tout
            # de suite.
            connus.update({c.slug: c for c in retenus})
    finally:
        if propre:
            await client.aclose()
    logger.info(
        "Clips : %d retenus sur %d chaînes en %d requête(s), %d écartés "
        "comme antérieurs à l'événement",
        len(connus), len(logins), -(-len(logins) // MAX_PAR_LOT), ecartes)
    return sorted(connus.values(), key=lambda c: -c.vues)


async def lister(client: httpx.AsyncClient | None = None) -> list[Clip]:
    """Les clips de la catégorie sur les sept derniers jours, les plus vus d'abord."""
    donnees = await _demander(
        _REQUETE_LISTE % {"slug": SLUG_CATEGORIE, "nombre": PAR_REQUETE,
                          "periode": PERIODE},
        client)
    jeu = donnees.get("game") or {}
    aretes = ((jeu.get("clips") or {}).get("edges")) or []
    plancher = depuis_quand()
    clips = [c for c in (_lire(a.get("node") or {}) for a in aretes)
             if c and c.cree_le >= plancher]
    logger.info("Clips : %d retenus sur la catégorie %s", len(clips),
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
