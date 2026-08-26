# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Détection des coupures publicitaires : marqueurs HLS et machine à états.

L'enjeu du compteur de confirmations est qu'un relevé aberrant isolé ne fasse
pas clignoter le bandeau. C'est ce qu'on vérifie ici.
"""

from __future__ import annotations

import pytest

from core import ad_watcher
from core.ad_watcher import _AD_CONFIRM, _END_CONFIRM, _playlist_has_ad


# ── marqueurs de playlist ────────────────────────────────────────────────────

@pytest.mark.parametrize("texte", [
    '#EXT-X-DATERANGE:ID="1",CLASS="twitch-stitched-ad"',
    '#EXT-X-DATERANGE:ID="1",CLASS="stitched-ad"',
    "https://cdn.test/ad_video/segment1.ts",
    "#EXT-X-ASSET:CAID=12345",
    '#ext-x-daterange:class="TWITCH-STITCHED-AD"',      # insensible à la casse
])
def test_marqueurs_de_pub_reconnus(texte):
    assert _playlist_has_ad(texte) is True


@pytest.mark.parametrize("texte", [
    "",
    "#EXTM3U\n#EXT-X-VERSION:3\nsegment1.ts",
    '#EXT-X-DATERANGE:ID="1",CLASS="autre-chose"',
    "https://cdn.test/video/segment1.ts",
])
def test_playlist_ordinaire_sans_marqueur(texte):
    assert _playlist_has_ad(texte) is False


# ── machine à états ──────────────────────────────────────────────────────────

class _Veilleur:
    """_StreamWatcher sans thread ni réseau : seule la transition nous intéresse."""

    def __init__(self):
        self.login = "zerator"
        self.debuts: list[str] = []
        self.fins: list[str] = []
        self._on_start = self.debuts.append
        self._on_end = self.fins.append
        self._pub_active = False
        self._pos_streak = 0
        self._neg_streak = 0

    transition = ad_watcher._StreamWatcher._transition


def test_une_pub_est_annoncee_apres_confirmation():
    v = _Veilleur()
    for _ in range(_AD_CONFIRM - 1):
        v.transition(True)
        assert v.debuts == [], "annoncée trop tôt"
    v.transition(True)
    assert v.debuts == ["zerator"]


def test_un_releve_positif_isole_n_annonce_rien():
    """Le cas que le compteur existe pour écarter."""
    v = _Veilleur()
    for _ in range(_AD_CONFIRM - 1):
        v.transition(True)
    v.transition(False)          # la série est cassée
    for _ in range(_AD_CONFIRM - 1):
        v.transition(True)
    assert v.debuts == []


def test_la_fin_demande_sa_propre_confirmation():
    v = _Veilleur()
    for _ in range(_AD_CONFIRM):
        v.transition(True)
    assert v.debuts == ["zerator"]
    for _ in range(_END_CONFIRM - 1):
        v.transition(False)
        assert v.fins == [], "fin annoncée trop tôt"
    v.transition(False)
    assert v.fins == ["zerator"]


def test_pas_de_seconde_annonce_tant_que_la_pub_dure():
    v = _Veilleur()
    for _ in range(_AD_CONFIRM * 3):
        v.transition(True)
    assert v.debuts == ["zerator"]


def test_pas_de_fin_sans_debut():
    v = _Veilleur()
    for _ in range(_END_CONFIRM * 2):
        v.transition(False)
    assert v.fins == []


def test_cycle_complet_puis_nouvelle_pub():
    v = _Veilleur()
    for _ in range(_AD_CONFIRM):
        v.transition(True)
    for _ in range(_END_CONFIRM):
        v.transition(False)
    for _ in range(_AD_CONFIRM):
        v.transition(True)
    assert v.debuts == ["zerator", "zerator"]
    assert v.fins == ["zerator"]
