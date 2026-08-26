# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Reprise d'un moment chez Twitch, en pleine qualité.

Le point le plus piégeux tient en une ligne : Twitch sert du MP4 FRAGMENTÉ.
Sans le segment d'initialisation écrit en premier, le fichier obtenu commence
par un fragment, ne porte ni `ftyp` ni `moov`, et aucun lecteur ne l'ouvre —
alors que le téléchargement, lui, s'est parfaitement déroulé.
"""

from __future__ import annotations

import pytest

from core.replay_hd import (
    REPLAY_SECS,
    duree_disponible,
    segment_initial,
    segments_a_prendre,
)

BASE = "https://cdn.test/v1/playlist.m3u8"

#: Playlist fMP4 telle que Twitch en sert : fenêtre glissante et initialisation.
PLAYLIST = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:2
#EXT-X-MAP:URI="init.mp4"
#EXTINF:2.000,
seg1.mp4
#EXTINF:2.000,
seg2.mp4
#EXTINF:2.000,
seg3.mp4
#EXTINF:2.000,
seg4.mp4
"""


# ── segment d'initialisation ─────────────────────────────────────────────────

def test_l_initialisation_est_reperee():
    """Sans elle, le fichier n'a ni ftyp ni moov et reste injouable."""
    assert segment_initial(PLAYLIST, BASE) == "https://cdn.test/v1/init.mp4"


def test_une_playlist_mpeg_ts_n_en_declare_pas():
    """Les vieux flux se collent bout à bout : pas d'initialisation à écrire."""
    assert segment_initial("#EXTM3U\n#EXTINF:2.0,\nseg1.ts\n") == ""


def test_une_url_absolue_d_initialisation_est_gardee_telle_quelle():
    p = '#EXT-X-MAP:URI="https://autre.test/init.mp4"\n'
    assert segment_initial(p, BASE) == "https://autre.test/init.mp4"


# ── sélection des segments ───────────────────────────────────────────────────

def test_on_remonte_depuis_la_fin():
    """C'est le passé IMMÉDIAT qui intéresse : le moment vient de se produire."""
    urls = segments_a_prendre(PLAYLIST, 4.0, BASE)
    assert urls == ["https://cdn.test/v1/seg3.mp4",
                    "https://cdn.test/v1/seg4.mp4"]


def test_l_ordre_reste_chronologique():
    """On sélectionne en remontant, mais on écrit dans l'ordre de lecture."""
    urls = segments_a_prendre(PLAYLIST, 8.0, BASE)
    assert urls == [f"https://cdn.test/v1/seg{i}.mp4" for i in (1, 2, 3, 4)]


def test_une_demande_partielle_prend_le_segment_qui_deborde():
    """Mieux vaut un peu trop que de couper le début du moment."""
    assert len(segments_a_prendre(PLAYLIST, 3.0, BASE)) == 2


def test_une_demande_plus_longue_que_la_fenetre_rend_tout():
    """La playlist d'un direct est une fenêtre glissante — mesurée à 28 s.

    Demander 60 s ne fait pas apparaître du passé qui n'existe plus côté
    serveur : on rend ce qu'il y a, un replay court valant mieux que rien.
    """
    assert len(segments_a_prendre(PLAYLIST, 600.0, BASE)) == 4


@pytest.mark.parametrize("secondes", [0, -1, -30.5])
def test_une_duree_nulle_ou_negative_ne_demande_rien(secondes):
    assert segments_a_prendre(PLAYLIST, secondes, BASE) == []


def test_une_playlist_vide_ne_leve_pas():
    assert segments_a_prendre("", 30.0, BASE) == []
    assert segments_a_prendre("#EXTM3U\n", 30.0, BASE) == []


def test_les_lignes_de_commentaire_ne_sont_pas_prises_pour_des_segments():
    p = "#EXTM3U\n#EXT-X-DISCONTINUITY\n#EXTINF:2.0,\nseg1.mp4\n"
    assert segments_a_prendre(p, 2.0, BASE) == ["https://cdn.test/v1/seg1.mp4"]


def test_sans_base_les_url_restent_relatives():
    assert segments_a_prendre(PLAYLIST, 2.0) == ["seg4.mp4"]


def test_une_duree_illisible_ne_bloque_pas_la_selection():
    p = "#EXTINF:pas un nombre,\nseg1.mp4\n#EXTINF:2.0,\nseg2.mp4\n"
    assert segments_a_prendre(p, 2.0, BASE) == ["https://cdn.test/v1/seg2.mp4"]


# ── fenêtre disponible ───────────────────────────────────────────────────────

def test_duree_disponible():
    assert duree_disponible(PLAYLIST) == pytest.approx(8.0)
    assert duree_disponible("") == 0.0


def test_la_duree_de_replay_tient_dans_ce_que_twitch_garde():
    """Mesuré sur un direct réel : 14 segments de 2 s, soit 28 secondes.

    Annoncer une minute promettrait ce que la source ne peut pas fournir.
    """
    assert REPLAY_SECS <= 30
