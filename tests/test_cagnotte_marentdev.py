# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Relais de cagnotte marentdev.eu.

Aucun appel réseau : le client httpx est remplacé par un double qui rend les
réponses qu'on lui dicte. Ce qui est vérifié ici, c'est la lecture du rapport,
la conditionnalité des requêtes, et surtout le refus de dégrader la cagnotte
officielle quand le relais répond de travers.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from core.api_client import GlobalStats
from core.cagnotte_marentdev import CagnotteRelais, RelaisCagnotte
from core.data_manager import _appliquer_relais


# ── doubles ──────────────────────────────────────────────────────────────────

class _Reponse:
    def __init__(self, status_code=200, text="", headers=None) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class _Client:
    """Rend les réponses programmées, et note les en-têtes reçus."""

    def __init__(self, *reponses) -> None:
        self._file = list(reponses)
        self.entetes: list[dict] = []

    async def get(self, url, headers=None, timeout=None):
        self.entetes.append(dict(headers or {}))
        r = self._file.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _rapport(total=860184, dons=11364, donateurs=7467,
             dernier="2026-09-04T16:27:13+00:00") -> str:
    """Un rapport minimal, à la forme du vrai."""
    charge = {"event": {"summary": {
        "total_amount": total, "donations_count": dons,
        "unique_donors": donateurs, "last_donation_at": dernier,
    }}}
    return ('<html><body><script id="report-data" type="application/json">'
            + json.dumps(charge) + "</script></body></html>")


def _relever(relais, client):
    return asyncio.run(relais.relever(client))


# ── lecture du rapport ───────────────────────────────────────────────────────

def test_le_releve_est_lu_dans_le_bloc_embarque():
    r = RelaisCagnotte.lire(_rapport())
    assert r == CagnotteRelais(860184.0, 11364, 7467, "2026-09-04T16:27:13+00:00")


@pytest.mark.parametrize("html", [
    "",
    "<html><body>rien du tout</body></html>",
    '<script id="report-data">ceci n\'est pas du json</script>',
    '<script id="report-data">{"event": {}}</script>',
    '<script id="report-data">{"event": {"summary": {"total_amount": 0}}}</script>',
])
def test_un_rapport_illisible_ne_rend_rien(html):
    """Zéro n'est pas une cagnotte vide : c'est un rapport qu'on n'a pas su lire."""
    assert RelaisCagnotte.lire(html) is None


def test_l_ordre_des_attributs_de_la_balise_est_indifferent():
    """Rien ne garantit que le site garde `id` en premier."""
    html = ('<script type="application/json" id="report-data">'
            '{"event":{"summary":{"total_amount": 42}}}</script>')
    releve = RelaisCagnotte.lire(html)
    assert releve is not None and releve.total == 42.0


# ── requêtes conditionnelles ─────────────────────────────────────────────────

def test_le_premier_appel_ne_conditionne_rien():
    client = _Client(_Reponse(text=_rapport()))
    _relever(RelaisCagnotte(), client)
    assert client.entetes == [{}]


def test_les_validateurs_sont_rejoues_au_tour_suivant():
    """C'est ce qui permet au serveur de répondre 304 au lieu de 180 ko."""
    relais = RelaisCagnotte()
    client = _Client(
        _Reponse(text=_rapport(), headers={"etag": '"abc"',
                                           "last-modified": "Fri, 04 Sep 2026 16:00:00 GMT"}),
        _Reponse(status_code=304),
    )
    _relever(relais, client)
    _relever(relais, client)
    assert client.entetes[1] == {"If-None-Match": '"abc"',
                                 "If-Modified-Since": "Fri, 04 Sep 2026 16:00:00 GMT"}


def test_un_304_rend_le_dernier_releve():
    """Le rapport n'a pas changé : la valeur non plus."""
    relais = RelaisCagnotte()
    client = _Client(_Reponse(text=_rapport(total=1000)), _Reponse(status_code=304))
    premier = _relever(relais, client)
    assert _relever(relais, client) == premier


def test_un_rapport_illisible_ne_fige_pas_les_validateurs():
    """Retenir l'ETag d'un rapport qu'on n'a pas su lire ferait répondre 304 au
    tour suivant : on resterait aveugle jusqu'au prochain changement."""
    relais = RelaisCagnotte()
    client = _Client(
        _Reponse(text="<html>illisible</html>", headers={"etag": '"zzz"'}),
        _Reponse(text=_rapport(total=500)),
    )
    _relever(relais, client)
    assert _relever(relais, client).total == 500.0
    assert client.entetes[1] == {}


# ── pannes ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("panne", [
    _Reponse(status_code=403),          # le challenge Cloudflare, un jour
    _Reponse(status_code=500),
    OSError("réseau coupé"),
])
def test_une_panne_rend_le_dernier_etat_connu(panne):
    """Source d'appoint : son échec ne doit pas emporter le cycle qui l'appelle."""
    relais = RelaisCagnotte()
    client = _Client(_Reponse(text=_rapport(total=777)), panne)
    _relever(relais, client)
    assert _relever(relais, client).total == 777.0


def test_une_panne_au_premier_appel_ne_rend_rien():
    assert _relever(RelaisCagnotte(), _Client(OSError("coupé"))) is None


# ── application à la cagnotte affichée ───────────────────────────────────────

def _stats(total=835473.0):
    return GlobalStats(total, "835 473 €", 1000, "live")


def test_le_relais_remplace_la_cagnotte_quand_il_compte_plus():
    stats = _stats()
    _appliquer_relais(stats, CagnotteRelais(860184.0, 0, 0, ""))
    assert stats.donation_total == 860184.0
    assert stats.donation_formatted == "860 184 €"


@pytest.mark.parametrize("releve", [
    None,                                    # relais injoignable
    CagnotteRelais(800000.0, 0, 0, ""),      # relais en retard
    CagnotteRelais(835473.0, 0, 0, ""),      # à égalité
])
def test_la_cagnotte_officielle_ne_recule_jamais(releve):
    """Une cagnotte qui baisse à l'écran est toujours une erreur de lecture."""
    stats = _stats()
    _appliquer_relais(stats, releve)
    assert stats.donation_total == 835473.0
    assert stats.donation_formatted == "835 473 €"
