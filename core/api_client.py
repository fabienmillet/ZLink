"""Clients HTTP async pour les APIs ZEvent et communautaire."""

from __future__ import annotations

import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

ZEVENT_URL = "https://zevent.fr/api/"
# API communautaire evenmorestats, édition 2026 : l'event-id passe dans le chemin
# (plus de header "event-id") et tous les montants sont exprimés en centimes.
GDOC_URL = "https://api.ppr.evenmorestats.fr"
GDOC_EVENT_ID = "019f5bd1-fe07-7d78-a326-a02198a9d50f"   # ZEvent 2026 (3 → 7 sept.)
_TIMEOUT = httpx.Timeout(10.0)
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
    location: str          # "LAN" | "Online" | ""
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
    location: str          # "LAN" | "Online" | ""
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
_LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")

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
    viewers = int(vc_block.get("number") if isinstance(vc_block, dict) else vc_block or 0)
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
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(ZEVENT_URL)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        logger.error("fetch_zevent_data: %s", exc)
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
    except Exception as exc:
        logger.error("fetch_zevent_data (parse): %s", exc)
        return GlobalStats(0.0, "0 €", 0, "offline"), []


# ---------------------------------------------------------------------------
# API 2 — participations à l'édition (remplace l'ancien /streamers)
# ---------------------------------------------------------------------------

def _parse_participation(p: dict[str, Any]) -> Participation:
    """Parse une entrée de /events/{event_id}/participations."""
    streamers = p.get("streamers") or []
    first: dict[str, Any] = streamers[0] if streamers else {}
    twitch = (first.get("socials") or {}).get("twitch") or {}
    state: dict[str, Any] = next(
        (st for st in (first.get("streaming_states") or []) if st.get("platform") == "twitch"),
        {},
    )
    raw_loc = str(p.get("location") or "").lower().strip()
    live = bool(p.get("live") or state.get("live") or False)
    game = str(state.get("game") or "")
    return Participation(
        streamer_id=str(p.get("id") or first.get("id") or "").strip(),
        participation_id=str(p.get("participation_id") or "").strip(),
        twitch_login=_safe_login(twitch.get("login")).lower(),
        display=str(p.get("name") or first.get("name") or ""),
        location="LAN" if raw_loc == "lan" else ("Online" if raw_loc else ""),
        live=live,
        game=game if live and game.lower() != "offline" else "",
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
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{GDOC_URL}/events/{GDOC_EVENT_ID}/participations")
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        logger.error("fetch_participations: %s", exc)
        return []

    result: list[Participation] = []
    try:
        for entry in data:
            parsed = _parse_participation(entry)
            if parsed.twitch_login and parsed.streamer_id:
                result.append(parsed)
    except Exception as exc:
        logger.error("fetch_participations (parse): %s", exc)

    return result


async def fetch_gdoc_streamers() -> dict[str, str]:
    """Retourne {twitch_login: streamer_id}. Ne lève jamais."""
    return {p.twitch_login: p.streamer_id for p in await fetch_participations()}


# ---------------------------------------------------------------------------
# API 4 — programme (shows de l'édition)
# ---------------------------------------------------------------------------

def _parse_participants(parts_raw: Any) -> tuple[list[str], list[str], dict[str, str]]:
    """Extrait (host_ids, participant_ids, {id: nom}) depuis 'participants'.

    Format 2026 : liste de dicts {streamer_id, streamer_name, role: host|guest}.
    Les formats historiques (dict host/participant, liste d'UUIDs) restent gérés.
    """
    host_ids: list[str] = []
    participant_ids: list[str] = []
    names: dict[str, str] = {}

    if isinstance(parts_raw, dict):
        host_ids = [str(h) for h in (parts_raw.get("host") or [])]
        participant_ids = [str(p) for p in (parts_raw.get("participant") or [])]
    elif isinstance(parts_raw, list):
        for entry in parts_raw:
            if not isinstance(entry, dict):
                participant_ids.append(str(entry))
                continue
            sid = str(entry.get("streamer_id") or entry.get("id") or "").strip()
            if not sid:
                continue
            name = str(entry.get("streamer_name") or entry.get("name") or "")
            if name:
                names[sid] = name
            if str(entry.get("role") or "").lower() == "host":
                host_ids.append(sid)
            else:
                participant_ids.append(sid)

    return host_ids, participant_ids, names


async def fetch_events(day: str) -> list[EventItem]:
    """Charge les événements d'un jour (YYYY-MM-DD). Ne lève jamais."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(
                f"{GDOC_URL}/events/{GDOC_EVENT_ID}/shows", params={"day": day}
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        logger.error("fetch_events(%s): %s", day, exc)
        return []

    events: list[EventItem] = []
    try:
        for ev in data:
            schedule = ev.get("schedule") or {}
            start_raw = (schedule.get("start") or ev.get("start_at")
                         or ev.get("startAt") or ev.get("start") or "")
            end_raw = (schedule.get("end") or ev.get("end_at")
                       or ev.get("endAt") or ev.get("end") or "")
            host_uuids, participant_uuids, names = _parse_participants(ev.get("participants") or {})
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
            ))
    except Exception as exc:
        logger.error("fetch_events(%s) (parse): %s", day, exc)

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
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{GDOC_URL}/participations/{participation_id}/donation_goals")
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        logger.error("fetch_donation_goals(%s): %s", participation_id, exc)
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
    except Exception as exc:
        logger.error("fetch_donation_goals(%s) (parse): %s", participation_id, exc)

    return goals
