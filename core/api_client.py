# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Clients HTTP async pour les APIs ZEvent et communautaire."""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from weakref import WeakKeyDictionary

import httpx

logger = logging.getLogger(__name__)

ZEVENT_URL = "https://zevent.fr/api/"
# API communautaire evenmorestats, édition 2026 : l'event-id passe dans le chemin
# (plus de header "event-id") et tous les montants sont exprimés en centimes.
GDOC_URL = "https://api.ppr.evenmorestats.fr"
GDOC_EVENT_ID = "019f5bd1-fe07-7d78-a326-a02198a9d50f"   # ZEvent 2026 (3 → 7 sept.)
_TIMEOUT = httpx.Timeout(10.0)

# Un client HTTP par boucle d'evenements, partage par toutes les requetes qui
# s'y executent. Chaque appel ouvrait le sien : 32 clients en 71 s, donc autant
# de poignees de main TLS vers les memes deux hotes, et aucune connexion
# reutilisee. Le client est lie a sa boucle (ses transports le sont), d'ou la
# table par boucle plutot qu'un singleton de module ; la cle est faible pour ne
# pas retenir une boucle fermee.
_LOOP_CLIENTS: "WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = (
    WeakKeyDictionary()
)


def _client() -> httpx.AsyncClient:
    """Client de la boucle courante, cree a la demande."""
    loop = asyncio.get_running_loop()
    client = _LOOP_CLIENTS.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=_TIMEOUT)
        _LOOP_CLIENTS[loop] = client
    return client


async def close_loop_client() -> None:
    """Ferme le client de la boucle courante. A appeler avant de la fermer.

    Sans cela, les sockets du pool survivent a la boucle et httpx signale des
    transports non fermes.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    client = _LOOP_CLIENTS.pop(loop, None)
    if client is not None and not client.is_closed:
        try:
            await client.aclose()
        except Exception as exc:
            logger.debug("Fermeture du client HTTP : %s", exc)

_UTC2 = timezone(timedelta(hours=2))
_CENTS_PER_EURO = 100.0


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class StreamerInfo:
    twitch_login: str
    display: str
    online: bool
    game: str
    location: str          # "LAN" | "Ankama" | "Villa" | "Online" | ""
    viewers: int
    donation: float
    donation_formatted: str
    profile_url: str
    gdoc_id: Optional[str] = None           # streamer_id evenmorestats (stable entre éditions)
    participation_id: Optional[str] = None  # id de participation à l'édition courante (goals)
    title: str = ""        # titre du live (fourni par zevent.fr/api/ pendant l'event)
    donation_url: str = ""  # lien direct de don pour ce streamer (zevent.fr/api donationUrl)


@dataclass
class Participation:
    """Une participation à l'édition courante (endpoint /events/{id}/participations)."""
    streamer_id: str
    participation_id: str
    twitch_login: str
    display: str
    location: str          # "LAN" | "Ankama" | "Villa" | "Online" | ""
    live: bool
    game: str
    viewers: int
    donation: float        # euros (l'API renvoie des centimes)
    profile_url: str


@dataclass
class GlobalStats:
    donation_total: float
    donation_formatted: str
    viewers_total: int
    website_mode: str      # "offline" | "live"


@dataclass
class EventItem:
    id: str
    name: str
    day: str               # YYYY-MM-DD
    start_local: str       # HH:MM UTC+2
    end_local: str         # HH:MM UTC+2
    description: str
    host_uuids: list[str] = field(default_factory=list)         # UUIDs bruts gdoc
    participant_uuids: list[str] = field(default_factory=list)  # UUIDs bruts gdoc
    start_ts: float = 0.0  # timestamp Unix secondes UTC
    end_ts: float = 0.0    # timestamp Unix secondes UTC
    names: dict[str, str] = field(default_factory=dict)  # streamer_id → nom (invités inclus)
    # Les invités d'un show (artistes, groupes) ne figurent pas dans la liste des
    # streamers ZEvent : leur login et leur avatar ne sont disponibles QUE dans la
    # charge du show, on les conserve donc ici plutôt que de les jeter.
    logins: dict[str, str] = field(default_factory=dict)        # streamer_id → login Twitch
    profile_urls: dict[str, str] = field(default_factory=dict)  # streamer_id → URL avatar


@dataclass
class DonationGoal:
    id: str
    name: str
    amount: float
    accomplished: bool
    category: str
    links: list[str] = field(default_factory=list)


@dataclass
class GoalWithStreamer:
    """Goal de donation enrichi avec les infos du streamer associé."""
    streamer_login: str
    streamer_display: str
    goal_name: str
    amount_target: float
    accomplished: bool
    pct: float   # min(100, streamer.donation / amount_target * 100)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_local_time(dt_str: str) -> str:
    """Convertit une ISO datetime en HH:MM UTC+2. Retourne '' si invalide."""
    if not dt_str:
        return ""
    try:
        cleaned = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_UTC2).strftime("%H:%M")
    except ValueError:
        return dt_str[:5] if len(dt_str) >= 5 else dt_str


# Un login Twitch valide : 4-25 caractères alphanumériques ou "_". On accepte
# dès 1 caractère par tolérance, mais rien d'autre : ces logins servent de nom
# de fichier (cache avatars), de canal IRC et d'argument de sous-processus.
# re.ASCII est OBLIGATOIRE : sans lui, \w accepterait les lettres Unicode
# et cette validation de login laisserait passer bien plus que prévu.
_LOGIN_RE = re.compile(r"^\w{1,25}$", re.ASCII)

# Hôtes autorisés pour les liens de don : la page est ouverte dans le navigateur
# embarqué, sans barre d'adresse — un lien détourné serait indétectable.
_DONATION_HOSTS = ("zevent.fr",)


def _safe_login(raw: Any) -> str:
    """Login Twitch validé. Retourne '' (et logge) si le format est inattendu."""
    login = str(raw or "").strip()
    if not login:
        return ""
    if not _LOGIN_RE.match(login):
        logger.warning("Login Twitch rejeté (format inattendu) : %r", login[:40])
        return ""
    return login


# L'API distingue quatre lieux. Les écraser en « LAN / Online » perdait
# l'information des sites satellites, que le panel ne pouvait donc plus filtrer.
_LOCATION_LABELS = {
    "lan":           "LAN",
    "remote_ankama": "Ankama",
    "remote_villa":  "Villa",
    "remote":        "Online",
}


def _location_label(raw_loc: str) -> str:
    """Libellé d'affichage du lieu de participation."""
    if not raw_loc:
        return ""
    known = _LOCATION_LABELS.get(raw_loc)
    if known:
        return known
    # Un nouveau site apparu en cours d'édition reste lisible plutôt qu'ignoré.
    logger.info("Lieu de participation inconnu : %r", raw_loc[:40])
    return raw_loc.replace("remote_", "").replace("_", " ").title()


def _safe_https_url(raw: Any, allowed_hosts: tuple[str, ...] = ()) -> str:
    """URL https validée. Retourne '' si le schéma n'est pas https ou si l'hôte
    est hors allowlist. Bloque file://, ftp:// et http:// en clair."""
    url = str(raw or "").strip()
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        logger.warning("URL rejetée (schéma %r) : %s", parsed.scheme, url[:60])
        return ""
    if allowed_hosts:
        host = parsed.netloc.split("@")[-1].split(":")[0].lower()
        if not any(host == h or host.endswith("." + h) for h in allowed_hosts):
            logger.warning("URL rejetée (hôte %r hors allowlist) : %s", host, url[:60])
            return ""
    return url


def _euros(raw: Any) -> float:
    """Convertit un montant de l'API communautaire (centimes) en euros."""
    try:
        return float(raw or 0) / _CENTS_PER_EURO
    except (TypeError, ValueError):
        logger.warning("_euros: montant illisible (%r)", raw)
        return 0.0


def _format_euros(amount: float) -> str:
    """Formate un montant en euros : 1154211.58 → '1 154 212 €'."""
    return f"{int(round(amount)):,} €".replace(",", "\u202f")


def _to_unix_ts(dt_str: str) -> float:
    """Convertit une ISO datetime en timestamp Unix secondes UTC. Retourne 0.0 si invalide."""
    if not dt_str:
        return 0.0
    try:
        cleaned = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# API 1 — zevent.fr/api/
# ---------------------------------------------------------------------------

def _parse_global_stats(data: dict[str, Any]) -> GlobalStats:
    don_block = data.get("donationAmount") or {}
    vc_block = data.get("viewersCount") or {}
    # Les parentheses comptent : le `or 0` ne couvrait que la branche
    # « compteur nu ». Un viewersCount absent, vide, nul, ou {"number": null}
    # donnait int(None) -> TypeError, et fetch_zevent_data retombait alors
    # sur des valeurs neutres COMPLETES : toute la liste des streamers etait
    # perdue. Zero spectateur est pourtant normal avant le debut de l'event.
    brut = vc_block.get("number") if isinstance(vc_block, dict) else vc_block
    viewers = int(brut or 0)
    return GlobalStats(
        donation_total=float(don_block.get("number") or 0.0),
        donation_formatted=str(don_block.get("formatted") or "0 €"),
        viewers_total=viewers,
        website_mode=str(data.get("websiteMode") or "offline"),
    )


def _parse_streamer_entry(s: dict[str, Any]) -> StreamerInfo:
    don = s.get("donationAmount") or {}
    v = s.get("viewersAmount") or {}
    return StreamerInfo(
        twitch_login=_safe_login(s.get("twitch")),
        display=str(s.get("display") or s.get("twitch") or ""),
        online=bool(s.get("online") or False),
        game=str(s.get("game") or ""),
        location=str(s.get("location") or ""),
        viewers=int(v.get("number") or 0),
        donation=float(don.get("number") or 0.0),
        donation_formatted=str(don.get("formatted") or "0 €"),
        profile_url=_safe_https_url(s.get("profileUrl")),
        title=str(s.get("title") or ""),
        donation_url=_safe_https_url(s.get("donationUrl"), _DONATION_HOSTS),
    )


async def fetch_zevent_data() -> tuple[GlobalStats, list[StreamerInfo]]:
    """Charge les données live depuis zevent.fr/api/. Ne lève jamais."""
    try:
        r = await _client().get(ZEVENT_URL)
        r.raise_for_status()
        data = r.json()
    except Exception:
        logger.exception("fetch_zevent_data")
        return GlobalStats(0.0, "0 €", 0, "offline"), []

    try:
        stats = _parse_global_stats(data)
        parsed = [_parse_streamer_entry(s) for s in (data.get("live") or [])]
        streamers = [s for s in parsed if s.twitch_login]
        if len(streamers) != len(parsed):
            logger.warning(
                "fetch_zevent_data: %d entrée(s) écartée(s) (login invalide)",
                len(parsed) - len(streamers),
            )
        return stats, streamers
    except Exception:
        logger.exception("fetch_zevent_data (parse)")
        return GlobalStats(0.0, "0 €", 0, "offline"), []


# ---------------------------------------------------------------------------
# API 2 — participations à l'édition (remplace l'ancien /streamers)
# ---------------------------------------------------------------------------

def _etat_twitch(first: dict[str, Any]) -> dict[str, Any]:
    """Premier streaming_state de plateforme twitch, ou {} s'il n'y en a pas."""
    return next(
        (st for st in (first.get("streaming_states") or [])
         if st.get("platform") == "twitch"),
        {},
    )


def _jeu_affiche(state: dict[str, Any], live: bool) -> str:
    """Jeu en cours, vide hors direct.

    L'API renvoie parfois « offline » comme jeu : l'afficher tel quel donnait
    des cartes annonçant « offline » en guise de catégorie.
    """
    game = str(state.get("game") or "")
    return game if live and game.lower() != "offline" else ""


def _parse_participation(p: dict[str, Any]) -> Participation:
    """Parse une entrée de /events/{event_id}/participations."""
    streamers = p.get("streamers") or []
    first: dict[str, Any] = streamers[0] if streamers else {}
    twitch = (first.get("socials") or {}).get("twitch") or {}
    state = _etat_twitch(first)
    raw_loc = str(p.get("location") or "").lower().strip()
    live = bool(p.get("live") or state.get("live") or False)
    return Participation(
        streamer_id=str(p.get("id") or first.get("id") or "").strip(),
        participation_id=str(p.get("participation_id") or "").strip(),
        twitch_login=_safe_login(twitch.get("login")).lower(),
        display=str(p.get("name") or first.get("name") or ""),
        location=_location_label(raw_loc),
        live=live,
        game=_jeu_affiche(state, live),
        viewers=int(state.get("viewers") or 0) if live else 0,
        donation=_euros(p.get("amount_raised")),
        profile_url=_safe_https_url(p.get("profile_url") or first.get("profile_url")),
    )


async def fetch_participations() -> list[Participation]:
    """Charge les participants de l'édition courante. Ne lève jamais.

    Fournit aussi live / game / viewers (rafraîchis côté API toutes les
    quelques minutes) et la cagnotte par streamer.
    """
    try:
        r = await _client().get(f"{GDOC_URL}/events/{GDOC_EVENT_ID}/participations")
        r.raise_for_status()
        data = r.json()
    except Exception:
        logger.exception("fetch_participations")
        return []

    result: list[Participation] = []
    try:
        for entry in data:
            parsed = _parse_participation(entry)
            if parsed.twitch_login and parsed.streamer_id:
                result.append(parsed)
    except Exception:
        logger.exception("fetch_participations (parse)")

    return result


async def fetch_gdoc_streamers() -> dict[str, str]:
    """Retourne {twitch_login: streamer_id}. Ne lève jamais."""
    return {p.twitch_login: p.streamer_id for p in await fetch_participations()}


# ---------------------------------------------------------------------------
# API 4 — programme (shows de l'édition)
# ---------------------------------------------------------------------------

def _parse_participants(
    parts_raw: Any,
) -> tuple[list[str], list[str], dict[str, str], dict[str, str], dict[str, str]]:
    """Extrait (host_ids, participant_ids, {id: nom}, {id: login}, {id: avatar}).

    Format 2026 : liste de dicts {streamer_id, streamer_name, role: host|guest,
    profile_url, socials.twitch.login}.
    Les formats historiques (dict host/participant, liste d'UUIDs) restent gérés.
    """
    host_ids: list[str] = []
    participant_ids: list[str] = []
    names: dict[str, str] = {}
    logins: dict[str, str] = {}
    avatars: dict[str, str] = {}

    if isinstance(parts_raw, dict):
        # Format historique : deux listes d'UUID, sans détail par personne.
        host_ids = [str(h) for h in (parts_raw.get("host") or [])]
        participant_ids = [str(p) for p in (parts_raw.get("participant") or [])]
    elif isinstance(parts_raw, list):
        for entry in parts_raw:
            _classer_entree(entry, host_ids, participant_ids,
                            names, logins, avatars)

    return host_ids, participant_ids, names, logins, avatars


def _classer_entree(entry: Any, host_ids: list[str], participant_ids: list[str],
                    names: dict[str, str], logins: dict[str, str],
                    avatars: dict[str, str]) -> None:
    """Range une entrée en hôte ou participant, et note ce qu'elle apporte.

    Une entrée qui n'est pas un dict est un UUID nu du format historique :
    on ne sait rien d'elle sinon qu'elle participe.
    """
    if not isinstance(entry, dict):
        participant_ids.append(str(entry))
        return
    sid = _noter_participant(entry, names, logins, avatars)
    if not sid:
        return
    if str(entry.get("role") or "").lower() == "host":
        host_ids.append(sid)
    else:
        participant_ids.append(sid)


def _noter_participant(entry: dict[str, Any], names: dict[str, str],
                       logins: dict[str, str],
                       avatars: dict[str, str]) -> str:
    """Enregistre nom, login Twitch et avatar. Renvoie l'id, ou "" si absent.

    Sans identifiant, rien ne peut être rattaché à la personne : l'entrée est
    alors rejetée plutôt que rangée sous une clé vide.
    """
    sid = str(entry.get("streamer_id") or entry.get("id") or "").strip()
    if not sid:
        return ""
    name = str(entry.get("streamer_name") or entry.get("name") or "")
    if name:
        names[sid] = name
    socials = entry.get("socials")
    twitch = socials.get("twitch") if isinstance(socials, dict) else None
    if isinstance(twitch, dict):
        # _safe_login : ce login sert de nom de fichier pour le cache avatars.
        login = _safe_login(twitch.get("login"))
        if login:
            logins[sid] = login
    avatar = _safe_https_url(entry.get("profile_url"))
    if avatar:
        avatars[sid] = avatar
    return sid


async def fetch_events(day: str) -> list[EventItem]:
    """Charge les événements d'un jour (YYYY-MM-DD). Ne lève jamais."""
    try:
        r = await _client().get(
            f"{GDOC_URL}/events/{GDOC_EVENT_ID}/shows", params={"day": day}
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        logger.exception("fetch_events(%s)", day)
        return []

    events: list[EventItem] = []
    try:
        for ev in data:
            schedule = ev.get("schedule") or {}
            start_raw = (schedule.get("start") or ev.get("start_at")
                         or ev.get("startAt") or ev.get("start") or "")
            end_raw = (schedule.get("end") or ev.get("end_at")
                       or ev.get("endAt") or ev.get("end") or "")
            (host_uuids, participant_uuids, names, ev_logins,
             ev_avatars) = _parse_participants(ev.get("participants") or {})
            events.append(EventItem(
                id=str(ev.get("id") or ""),
                name=str(ev.get("name") or ev.get("title") or ""),
                day=day,
                start_local=_to_local_time(start_raw),
                end_local=_to_local_time(end_raw),
                description=str(ev.get("description") or ""),
                host_uuids=host_uuids,
                participant_uuids=participant_uuids,
                start_ts=_to_unix_ts(start_raw),
                end_ts=_to_unix_ts(end_raw),
                names=names,
                logins=ev_logins,
                profile_urls=ev_avatars,
            ))
    except Exception:
        logger.exception("fetch_events(%s) (parse)", day)

    return events


# ---------------------------------------------------------------------------
# API 3 — donation goals (lazy)
# ---------------------------------------------------------------------------

async def fetch_donation_goals(participation_id: str) -> list[DonationGoal]:
    """Charge les objectifs de dons d'une participation. Ne lève jamais.

    Attention : la clé est le participation_id de l'édition courante,
    pas le streamer_id (cf. StreamerInfo.participation_id).
    """
    if not participation_id:
        return []
    try:
        r = await _client().get(
            f"{GDOC_URL}/participations/{participation_id}/donation_goals")
        r.raise_for_status()
        data = r.json()
    except Exception:
        logger.exception("fetch_donation_goals(%s)", participation_id)
        return []

    goals: list[DonationGoal] = []
    try:
        for g in data:
            goals.append(DonationGoal(
                id=str(g.get("id") or ""),
                name=str(g.get("name") or ""),
                amount=_euros(g.get("amount")),
                accomplished=bool(
                    g.get("accomplished") or g.get("done") or False
                ),
                category=str(
                    g.get("category") or g.get("type") or g.get("nature") or ""
                ),
                links=[str(lk) for lk in (g.get("links") or [])],
            ))
    except Exception:
        logger.exception("fetch_donation_goals(%s) (parse)", participation_id)

    return goals
