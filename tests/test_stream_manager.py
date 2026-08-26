# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Sélecteurs de qualité : validation, migration, paliers adaptatifs.

Une qualité part en argument de sous-processus streamlink : ce qui n'est pas
reconnu doit retomber sur une valeur sûre plutôt que d'être transmis tel quel.
"""

from __future__ import annotations

import pytest

from core.stream_manager import (
    QUALITY_GRID,
    _DEFAULT_ADAPTIVE_TIERS,
    _parse_tiers,
    migrate_quality,
    safe_quality,
)


# ── safe_quality ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("brut", [
    "best", "worst", "720p60", "720p60,480p30,360p30", "1080p60,best", "a_b",
])
def test_qualite_valide_passe(brut):
    assert safe_quality(brut, QUALITY_GRID) == brut


@pytest.mark.parametrize("brut", [
    "", None, "   ",
    "720p60;rm -rf /",       # injection de commande
    "720p60 480p30",         # espace
    "720p60,",               # virgule terminale
    ",720p60",
    "720p60,,480p30",
    "../../etc/passwd",
    "qualité",               # non ASCII
])
def test_qualite_invalide_retombe_sur_le_defaut(brut):
    assert safe_quality(brut, QUALITY_GRID) == QUALITY_GRID


def test_qualite_est_deballee_des_espaces():
    assert safe_quality("  best  ", QUALITY_GRID) == "best"


# ── migrate_quality ──────────────────────────────────────────────────────────

def test_migration_des_selecteurs_herites():
    assert migrate_quality("360p,worst") == QUALITY_GRID
    assert migrate_quality("1080p60,1080p,best") == "1080p60,1080p,best"


def test_migration_laisse_passer_ce_qu_elle_ne_connait_pas():
    assert migrate_quality("720p60,best") == "720p60,best"
    assert migrate_quality("") == ""


# ── _parse_tiers ─────────────────────────────────────────────────────────────

def test_paliers_valides_sont_tries_par_seuil():
    assert _parse_tiers([[9, "480p30"], [1, "best"], [4, "720p60"]]) == [
        (1, "best"), (4, "720p60"), (9, "480p30"),
    ]


def test_paliers_acceptent_les_tuples():
    assert _parse_tiers([(1, "best")]) == [(1, "best")]


@pytest.mark.parametrize("brut", [
    None, "", {}, [],                      # pas une liste non vide
    [[1]],                                 # arité fausse
    [[1, "best", "extra"]],
    [["1", "best"]],                       # seuil non entier
    [[0, "best"]],                         # seuil < 1
    [[-3, "best"]],
    ["pas un palier"],
])
def test_paliers_inexploitables_rendent_une_liste_vide(brut):
    """Un seul palier douteux invalide toute la table.

    Appliquer les paliers restants donnerait une qualité arbitraire là où
    l'utilisateur croyait avoir configuré autre chose.
    """
    assert _parse_tiers(brut) == []


def test_palier_a_la_qualite_douteuse_retombe_sur_la_grille():
    assert _parse_tiers([[1, "best;rm -rf"]]) == [(1, QUALITY_GRID)]


# ── Les deux graphies de Twitch ─────────────────────────────────────────────
#
# Relevé sur des chaînes de l'event, là où le code affirmait le contraire :
#
#   Available streams: audio_only, 160p (worst), 360p, 480p, 720p60, 1080p60
#
# …alors que d'autres chaînes n'exposent que « 360p30 ». streamlink exige une
# correspondance EXACTE : demander une seule graphie condamnait la moitié des
# chaînes, code 1 et cellule noire.

def test_une_echelle_liste_les_deux_graphies():
    from core.stream_manager import echelle
    assert echelle("480p", "360p") == "480p,480p30,360p,360p30,worst"


def test_une_cadence_explicite_garde_ses_replis():
    """Une chaîne sans 60 fps expose le même palier en 30, ou sans suffixe."""
    from core.stream_manager import echelle
    assert echelle("720p60") == "720p60,720p,720p30,worst"


def test_une_echelle_finit_toujours_par_worst():
    """« worst » existe partout : c'est le seul repli qui ne peut pas manquer."""
    from core.stream_manager import echelle
    assert echelle("1080p60").endswith("worst")
    assert echelle("worst") == "worst"


def test_une_echelle_ne_repete_pas_un_palier():
    from core.stream_manager import echelle
    noms = echelle("360p", "360p30", "360p").split(",")
    assert len(noms) == len(set(noms))


@pytest.mark.parametrize("palier", ["best", "worst", "audio_only"])
def test_les_noms_symboliques_ne_se_declinent_pas(palier):
    from core.stream_manager import echelle
    assert echelle(palier).split(",")[0] == palier


@pytest.mark.parametrize("echelle_testee", [
    QUALITY_GRID,
    *(q for _seuil, q in _DEFAULT_ADAPTIVE_TIERS),
])
def test_toutes_les_echelles_du_module_trouvent_une_chaine_reelle(echelle_testee):
    """Contrôle de bout en bout contre les rendus réellement servis.

    C'est exactement la liste relevée sur les chaînes qui échouaient.
    """
    servis = {"audio_only", "160p", "360p", "480p", "720p60", "1080p60",
              "best", "worst"}
    noms = echelle_testee.split(",")
    assert any(n in servis for n in noms), \
        f"aucun de {noms} n'existe sur une chaîne qui sert {sorted(servis)}"
