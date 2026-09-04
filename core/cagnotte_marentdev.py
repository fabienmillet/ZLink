# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Cagnotte relevée chez marentdev.eu, un relais communautaire du ZEvent.

**Pourquoi une deuxième source.** `zevent.fr/api/` est la source officielle et
le reste ; ce relais compte les dons un cran plus tôt et un cran plus fin.
L'écart est réel — 860 184 € contre 835 473 € au même instant lors du premier
relevé — parce que les deux n'agrègent pas au même rythme, pas parce que l'un
se trompe. C'est un complément, jamais un remplacement : si ce relais tombe,
la cagnotte officielle continue seule, et rien ne s'en aperçoit.

**Par quelle porte.** Le point d'entrée annoncé était
`wss://zevent.marentdev.eu/api/flux/socket`, mais il est derrière un challenge
Cloudflare : la poignée de main WebSocket revient en 403 pour tout client qui
n'est pas un navigateur. Passer outre demanderait de rejouer un cookie de
clearance — un contournement, et fragile de surcroît.

Ce n'était pas nécessaire. La page publique du site ne se sert pas de ce socket
non plus : elle interroge `/api/report.html` toutes les trente secondes, en
requêtes conditionnelles, et lit un JSON embarqué dans le HTML rendu. Ce
chemin-là n'est pas protégé. On emprunte donc exactement la même porte, à la
même cadence, avec les mêmes en-têtes — rien de contourné, et un serveur qui
peut nous répondre `304`.

**Le coût côté serveur.** Le document fait 180 ko. La requête conditionnelle
est ce qui rend ce relevé acceptable : tant que le rapport n'a pas bougé, la
réponse est un `304` vide. D'où l'ETag et le Last-Modified conservés d'un appel
à l'autre.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Le rapport que la page du site interroge elle-même. Le JSON est embarqué
#: dans un <script type="application/json">, ce n'est pas un point d'entrée
#: JSON nu — d'où l'extraction par motif plutôt qu'un `r.json()`.
REPORT_URL = "https://zevent.marentdev.eu/api/report.html"

#: Cadence annoncée par le rapport lui-même (`report-refresh.poll_ms`). On ne
#: descend pas en dessous : c'est le rythme que le site s'est fixé, et ZLink
#: n'a aucune raison d'être plus gourmand que sa propre page.
POLL_MS = 30_000

_TIMEOUT_S = 15.0

#: Le bloc de données du rapport. L'`id` est cherché OÙ QU'IL SOIT dans la
#: balise : le site l'écrit aujourd'hui en premier, mais rien ne l'y oblige, et
#: un `type="application/json"` glissé devant suffisait à ne plus rien trouver.
#: `re.S` parce que le JSON tient sur des dizaines de lignes.
#:
#: Un motif, et non un parseur HTML : c'est une balise unique et nommée, et
#: ZLink n'embarque pas de quoi analyser 180 ko de HTML.
_BLOC_DONNEES = re.compile(
    r'<script[^>]*\bid="report-data"[^>]*>(.*?)</script>', re.S)


@dataclass(frozen=True)
class CagnotteRelais:
    """Ce qu'on retient du rapport. Le reste est ignoré volontairement."""

    total: float             #: euros
    dons: int                #: nombre de dons comptés
    donateurs: int           #: donateurs uniques
    dernier_don_iso: str     #: horodatage du dernier don, tel qu'annoncé


class RelaisCagnotte:
    """Relève la cagnotte du relais, en requêtes conditionnelles.

    L'instance porte l'ETag et le Last-Modified du dernier rapport obtenu :
    c'est tout son état, et c'est ce qui permet au serveur de répondre `304`.
    Un objet par application, pas un par appel.
    """

    def __init__(self, url: str = REPORT_URL) -> None:
        self.url = url
        self._etag: str = ""
        self._last_modified: str = ""
        #: Dernier relevé abouti. Rendu tel quel sur un 304 — le rapport n'a
        #: pas changé, la valeur non plus.
        self._dernier: CagnotteRelais | None = None

    @property
    def dernier(self) -> CagnotteRelais | None:
        """Dernier relevé connu, sans rien demander au réseau."""
        return self._dernier

    def _entetes(self) -> dict[str, str]:
        """Les mêmes en-têtes conditionnels que la page du site."""
        entetes: dict[str, str] = {}
        if self._etag:
            entetes["If-None-Match"] = self._etag
        if self._last_modified:
            entetes["If-Modified-Since"] = self._last_modified
        return entetes

    async def relever(self, client) -> CagnotteRelais | None:
        """Relève la cagnotte. Rend le dernier état connu si rien n'a bougé.

        Ne lève jamais : cette source est un complément, et son indisponibilité
        ne doit pas emporter le cycle de rafraîchissement qui l'appelle.
        """
        try:
            r = await client.get(self.url, headers=self._entetes(),
                                 timeout=_TIMEOUT_S)
        except Exception as exc:      # noqa: BLE001 — source d'appoint
            logger.debug("Relais cagnotte injoignable — %s", exc)
            return self._dernier

        if r.status_code == 304:
            return self._dernier
        if r.status_code != 200:
            logger.debug("Relais cagnotte : HTTP %d", r.status_code)
            return self._dernier

        releve = self.lire(r.text)
        if releve is None:
            return self._dernier

        # Les validateurs ne sont retenus QU'APRÈS une lecture réussie : les
        # garder sur un rapport illisible ferait répondre 304 au prochain
        # appel, et on resterait aveugle jusqu'à ce que le rapport change.
        self._etag = r.headers.get("etag", "") or ""
        self._last_modified = r.headers.get("last-modified", "") or ""
        self._dernier = releve
        return releve

    @staticmethod
    def lire(html: str) -> CagnotteRelais | None:
        """Extrait le relevé du rapport HTML. None si illisible."""
        m = _BLOC_DONNEES.search(html or "")
        if not m:
            logger.debug("Relais cagnotte : bloc report-data absent")
            return None
        try:
            resume = (json.loads(m.group(1)).get("event") or {}).get("summary") or {}
            total = float(resume.get("total_amount") or 0.0)
        except (ValueError, AttributeError, TypeError) as exc:
            logger.debug("Relais cagnotte : rapport illisible — %s", exc)
            return None
        # Un total nul n'est pas une cagnotte à zéro : c'est un rapport qu'on
        # n'a pas su lire. La cagnotte officielle vaut mieux que ce zéro.
        if total <= 0:
            return None
        return CagnotteRelais(
            total=total,
            dons=int(resume.get("donations_count") or 0),
            donateurs=int(resume.get("unique_donors") or 0),
            dernier_don_iso=str(resume.get("last_donation_at") or ""),
        )
