# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Ce qu'une chaîne a fait dans la dernière heure.

Aucune API ne rend de série : la mesure est constituée en gardant ce qui
passe. Ces tests portent surtout sur ce qu'il ne faut PAS annoncer — au
lancement, quand deux relevés sont trop rapprochés, ou quand l'API se corrige.
"""

from __future__ import annotations

import pytest

from core import tendances


class _Faux:
    def __init__(self, login, viewers, donation=0.0):
        self.twitch_login = login
        self.viewers = viewers
        self.donation = donation


@pytest.fixture(autouse=True)
def series_vides():
    tendances.oublier_tout()
    yield
    tendances.oublier_tout()


T0 = 1_000_000.0


def test_l_ecart_est_mesure_entre_deux_releves():
    tendances.noter([_Faux("zerator", 45_000)], T0)
    tendances.noter([_Faux("zerator", 51_000)], T0 + 3600)
    assert tendances.viewers("zerator", maintenant=T0 + 3600) == 6_000


def test_une_baisse_est_rendue_negative():
    tendances.noter([_Faux("mistermv", 18_000)], T0)
    tendances.noter([_Faux("mistermv", 15_500)], T0 + 3600)
    assert tendances.viewers("mistermv", maintenant=T0 + 3600) == -2_500


def test_un_seul_releve_ne_dit_rien():
    """Au lancement, il n'y a rien à comparer."""
    tendances.noter([_Faux("zerator", 45_000)], T0)
    assert tendances.viewers("zerator", maintenant=T0) is None


def test_deux_releves_trop_rapproches_ne_disent_rien():
    """Sur trente secondes, une variation ne se distingue pas du bruit d'un
    sondage : l'API ne rafraîchit ses chiffres que toutes les quelques
    minutes, et deux relevés consécutifs rendent souvent la même valeur."""
    tendances.noter([_Faux("zerator", 45_000)], T0)
    tendances.noter([_Faux("zerator", 45_600)], T0 + 30)
    assert tendances.viewers("zerator", maintenant=T0 + 30) is None


def test_cinq_minutes_de_recul_suffisent_a_se_prononcer():
    """Le seuil était à quinze minutes : la colonne restait vide tout ce
    temps-là, ce qui la faisait passer pour cassée. Ce qu'on publie est un
    écart OBSERVÉ, jamais rapporté à l'heure — il n'a donc pas besoin d'une
    heure entière pour valoir quelque chose."""
    tendances.noter([_Faux("zerator", 45_000)], T0)
    tendances.noter([_Faux("zerator", 46_000)], T0 + tendances.RECUL_MIN_S)
    assert tendances.viewers("zerator",
                             maintenant=T0 + tendances.RECUL_MIN_S) == 1_000


def test_juste_avant_le_recul_minimal_on_se_tait_encore():
    tendances.noter([_Faux("zerator", 45_000)], T0)
    tendances.noter([_Faux("zerator", 46_000)], T0 + tendances.RECUL_MIN_S - 1)
    assert tendances.viewers(
        "zerator", maintenant=T0 + tendances.RECUL_MIN_S - 1) is None


def test_une_chaine_jamais_vue_ne_dit_rien():
    assert tendances.viewers("inconnu") is None
    assert tendances.cagnotte("inconnu") is None


# ── la cagnotte ──────────────────────────────────────────────────────────────

def test_les_euros_de_l_heure_sont_comptes():
    tendances.noter([_Faux("zerator", 1, 120_000.0)], T0)
    tendances.noter([_Faux("zerator", 1, 132_000.0)], T0 + 3600)
    assert tendances.cagnotte("zerator", maintenant=T0 + 3600) == 12_000.0


def test_une_cagnotte_ne_descend_jamais():
    """Un écart négatif ne peut venir que d'une correction de l'API.

    L'annoncer comme une perte serait faux : une cagnotte ne se vide pas.
    """
    tendances.noter([_Faux("zerator", 1, 120_000.0)], T0)
    tendances.noter([_Faux("zerator", 1, 119_000.0)], T0 + 3600)
    assert tendances.cagnotte("zerator", maintenant=T0 + 3600) == 0.0


# ── la mémoire ───────────────────────────────────────────────────────────────

def test_les_releves_trop_vieux_sont_oublies():
    """Sans purge, trois cents chaînes × quatre jours d'event s'accumulent."""
    tendances.noter([_Faux("zerator", 10)], T0)
    tendances.noter([_Faux("zerator", 20)], T0 + tendances.MEMOIRE_S + 60)
    serie = tendances._series["zerator"]
    assert len(serie) == 1, "le relevé hors mémoire devait partir"


def test_seule_la_fenetre_demandee_est_comparee():
    """Un relevé de deux heures ne doit pas servir de référence à une heure."""
    tendances.noter([_Faux("zerator", 10_000)], T0)
    tendances.noter([_Faux("zerator", 40_000)], T0 + 3600)
    tendances.noter([_Faux("zerator", 42_000)], T0 + 7200)
    # Sur la dernière heure : de 40 000 à 42 000, et non depuis 10 000.
    assert tendances.viewers("zerator", maintenant=T0 + 7200) == 2_000


def test_une_entree_sans_login_est_ignoree():
    tendances.noter([_Faux("", 100)], T0)
    assert tendances._series == {}
