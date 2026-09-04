# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Replay en pleine qualité, repris chez Twitch après coup.

LE PROBLÈME. Un replay de cellule sort en 360p, et ce n'est pas un défaut de
réglage : `dump-cache` restitue le TAMPON du lecteur, c'est-à-dire les octets
réellement reçus. La grille joue en 360p pour tenir vingt-cinq flux, donc son
tampon est en 360p. Rien ne peut reconstituer une définition jamais reçue.

LA SORTIE. Twitch publie toujours le direct en pleine qualité : on redemande
donc le moment à la source, en `best`, au lieu de le tirer du tampon local.

CE QUE ÇA COÛTE, ET IL FAUT LE SAVOIR :
  - La playlist d'un direct est une FENÊTRE GLISSANTE. Mesuré sur un direct
    réel : 14 segments de 2 s, soit 28 secondes. Au-delà, le passé n'existe
    plus côté Twitch — un replay de 60 s n'est pas récupérable, quoi qu'on
    demande. La durée obtenue est donc plafonnée par ce que le serveur garde.
  - Il faut retélécharger ces segments : quelques dizaines de mégaoctets, et
    deux à cinq secondes avant que le replay puisse commencer.

D'où le repli systématique sur le tampon local : mieux vaut un replay en 360p
tout de suite que rien du tout.

La sélection des segments est séparée du réseau : `segments_a_prendre` est de
la logique pure, testable sans connexion.
"""

from __future__ import annotations

import logging
import pathlib
import re
import subprocess
import tempfile
import time
import urllib.parse

import httpx

from core.stream_manager import _streamlink_exe, safe_quality
from core.sous_processus import sans_fenetre

logger = logging.getLogger(__name__)

#: Qualité demandée pour un replay : la meilleure disponible.
QUALITE = "best"
#: Durée d'un replay, en secondes.
#:
#: Trente et non soixante : la playlist d'un direct Twitch est une fenêtre
#: glissante, mesurée à 28 s (14 segments de 2 s). Annoncer une minute
#: promettrait ce que la source ne garde pas. Les CLIPS, eux, viennent du
#: tampon local et gardent leur propre durée, plus longue.
REPLAY_SECS = 30
#: Au-delà, on considère que le direct ne répond pas.
TIMEOUT_S = 15.0
#: Garde-fou de taille : un segment anormalement gros n'est pas un segment.
MAX_OCTETS = 400 * 1024 * 1024

# Un SEUL point, et des chiffres autour. « [\d.]+ » acceptait « 1.2.3 », que
# `float()` refuse ensuite — depuis deux fonctions que ce module présente comme
# de la logique pure, sans panne possible. La garde existante ne protégeait que
# des durées ne correspondant PAS au motif ; celles qui y correspondaient sans
# être des nombres passaient au travers, et transformaient un replay
# récupérable en aucun replay.
#
# La virgule finale n'est pas décorative : le format HLS l'impose après la
# durée, et c'est elle qui fait REJETER « 1.2.3 » au lieu d'en retenir
# « 1.2 » — une durée partielle serait pire qu'aucune, puisqu'elle se
# glisserait dans le total sans qu'on la remarque.
_EXTINF = re.compile(r"^#EXTINF:(\d+(?:\.\d+)?)\s*(?:,|$)", re.M)
_EXT_MAP = re.compile(r'#EXT-X-MAP:URI="([^"]+)"')


def segments_a_prendre(playlist: str, secondes: float,
                       base: str = "") -> list[str]:
    """URL des derniers segments couvrant `secondes`, du plus ancien au plus récent.

    On remonte depuis la FIN : c'est le passé immédiat qui nous intéresse, et
    la playlist d'un direct n'en garde qu'une fenêtre. Si elle contient moins
    que demandé, on rend tout ce qu'elle a — un replay plus court reste un
    replay, alors qu'une erreur ne montre rien.
    """
    if secondes <= 0:
        return []
    lignes = [l.strip() for l in playlist.splitlines()]
    paires: list[tuple[float, str]] = []
    duree = 0.0
    for ligne in lignes:
        if ligne.startswith("#EXTINF:"):
            m = _EXTINF.match(ligne)
            duree = float(m.group(1)) if m else 0.0
        elif ligne and not ligne.startswith("#"):
            paires.append((duree, ligne))
            duree = 0.0

    retenus: list[str] = []
    cumul = 0.0
    for d, url in reversed(paires):
        retenus.append(urllib.parse.urljoin(base, url) if base else url)
        cumul += d
        if cumul >= secondes:
            break
    retenus.reverse()
    return retenus


def segment_initial(playlist: str, base: str = "") -> str:
    """URL du segment d'initialisation (#EXT-X-MAP), ou '' s'il n'y en a pas.

    Twitch sert désormais du MP4 FRAGMENTÉ, pas du MPEG-TS. Les fragments ne
    portent ni `ftyp` ni `moov` : sans ce segment écrit EN PREMIER, le fichier
    obtenu commence par une boîte `emsg` et aucun lecteur ne l'ouvre.

    Les vieux flux en MPEG-TS n'en déclarent pas — leurs segments se collent
    bout à bout tels quels, et cette fonction rend alors une chaîne vide.
    """
    m = _EXT_MAP.search(playlist)
    if not m:
        return ""
    uri = m.group(1)
    return urllib.parse.urljoin(base, uri) if base else uri


def duree_disponible(playlist: str) -> float:
    """Secondes réellement présentes dans la playlist."""
    return sum(float(d) for d in _EXTINF.findall(playlist))


def _resoudre(login: str) -> str:
    """URL de la playlist `best` de cette chaîne, ou '' si indisponible."""
    exe = _streamlink_exe()
    if not exe:
        return ""
    try:
        res = subprocess.run(
            [exe, f"twitch.tv/{login}", safe_quality(QUALITE, QUALITE),
             "--stream-url", "--twitch-disable-ads"],
            capture_output=True, text=True, timeout=TIMEOUT_S,
            **sans_fenetre(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Replay HD : streamlink indisponible — %s", exc)
        return ""
    if res.returncode != 0:
        logger.debug("Replay HD : %s hors ligne ou illisible", login)
        return ""
    return res.stdout.strip()


def recuperer(login: str, secondes: float,
              dossier: str = "", prefixe: str = "replay") -> tuple[str, float]:
    """(chemin du fichier, durée réellement obtenue). ('', 0.0) en cas d'échec.

    Ne lève jamais : un replay est un agrément, son échec doit laisser
    l'appelant se rabattre sur le tampon local.
    """
    if not login or secondes <= 0:
        return "", 0.0
    url = _resoudre(login)
    if not url:
        return "", 0.0
    try:
        with httpx.Client(timeout=TIMEOUT_S, follow_redirects=True) as client:
            playlist = client.get(url).text
            dispo = duree_disponible(playlist)
            segments = segments_a_prendre(playlist, secondes, base=url)
            if not segments:
                logger.debug("Replay HD : aucune segment exploitable pour %s", login)
                return "", 0.0
            init = segment_initial(playlist, base=url)
            cible = pathlib.Path(dossier or tempfile.gettempdir())
            cible.mkdir(parents=True, exist_ok=True)
            # L'extension suit le conteneur : mpv se fie surtout au contenu,
            # mais un nom trompeur complique le diagnostic quand ça coince.
            ext = "mp4" if init else "ts"
            # Horodatage lisible plutôt qu'un temps Unix : un clip se garde, et
            # se retrouve dans un dossier six mois plus tard.
            quand = time.strftime("%Y%m%d_%H%M%S")
            fichier = cible / f"{prefixe}_{login}_{quand}.{ext}"
            ecrits = _telecharger(client, segments, fichier, init=init)
        if ecrits == 0:
            fichier.unlink(missing_ok=True)
            return "", 0.0
        obtenue = min(secondes, dispo)
        logger.info("Replay HD de %s : %.0f s en pleine qualité (%.1f Mo)",
                    login, obtenue, fichier.stat().st_size / 1e6)
        return str(fichier), obtenue
    except Exception:  # aucune panne réseau ne doit remonter  # noqa: BLE001
        logger.exception("Replay HD de %s impossible", login)
        return "", 0.0


def _telecharger(client: httpx.Client, segments: list[str],
                 fichier: pathlib.Path, init: str = "") -> int:
    """Écrit l'initialisation puis les fragments, dans cet ordre.

    L'ordre n'est pas cosmétique : `ftyp` et `moov` vivent dans le segment
    d'initialisation, et un MP4 fragmenté qui commence par un fragment n'est
    lisible par personne.

    Renvoie le nombre de fragments écrits : un fragment manquant laisse un trou
    mais ne condamne pas le replay. Une initialisation manquante, si.
    """
    ecrits = 0
    total = 0
    with fichier.open("wb") as sortie:
        if init:
            try:
                r = client.get(init)
                r.raise_for_status()
                sortie.write(r.content)
            except Exception as exc:      # noqa: BLE001
                logger.warning(
                    "Replay HD : segment d'initialisation illisible (%s) — "
                    "le fichier serait injouable", exc)
                return 0
        for url in segments:
            try:
                r = client.get(url)
                r.raise_for_status()
            except Exception as exc:      # noqa: BLE001
                logger.debug("Replay HD : segment perdu (%s)", exc)
                continue
            total += len(r.content)
            if total > MAX_OCTETS:
                logger.warning("Replay HD : taille anormale, arrêt du téléchargement")
                break
            sortie.write(r.content)
            ecrits += 1
    return ecrits
