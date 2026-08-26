# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Détection de moments forts : ligne de base, token dominant, qualification.

Le cœur de la règle est qu'un moment fort se mesure en UTILISATEURS DISTINCTS
qui écrivent la même chose, pas en messages. Un spammeur seul ne doit pas
suffire, et un mot ordinaire non plus.
"""

from __future__ import annotations

import pytest

from core.hype_watcher import (
    _BASELINE_MIN_SAMPLES,
    _C_DONO,
    _C_FUNNY,
    _Ewma,
    _LIBELLE_MOMENT_FORT,
    _MIN_DOMINANT_USERS,
    _classify_local,
    _dominant_token,
    _kw_matcher,
)


# ── moyenne glissante ────────────────────────────────────────────────────────

def test_pas_de_verdict_avant_d_avoir_observe_assez():
    """Une chaîne doit être observée avant de pouvoir déclencher une alerte."""
    e = _Ewma()
    for _ in range(_BASELINE_MIN_SAMPLES - 1):
        e.update(10.0, 2.0)
        assert e.deviation(1000.0) is None
    e.update(10.0, 2.0)
    assert e.deviation(1000.0) is not None


def test_ecart_borne_entre_zero_et_un():
    e = _Ewma()
    for _ in range(_BASELINE_MIN_SAMPLES):
        e.update(10.0, 2.0)
    assert e.deviation(10_000.0) == pytest.approx(1.0)
    assert e.deviation(0.0) == 0.0, "un creux n'est pas un moment fort"


def test_une_valeur_normale_ne_sature_pas():
    e = _Ewma()
    for i in range(_BASELINE_MIN_SAMPLES * 2):
        e.update(10.0 + (i % 3), 2.0)
    assert e.deviation(11.0) < 0.5


def test_premiere_mesure_devient_la_moyenne():
    e = _Ewma()
    e.update(42.0, 2.0)
    assert e.mean == pytest.approx(42.0)
    assert e.n == 1


# ── correspondance de mots-clés ──────────────────────────────────────────────

def test_les_mots_cles_sont_cherches_en_MOTS_ENTIERS():
    """Une recherche de sous-chaîne classait « pardon » et « abandonne » en
    Donation — catégorie factuelle, donc prioritaire sur tout le reste."""
    match = _kw_matcher(["don", "dono"])
    assert match("gros don sur la cagnotte") is True
    assert match("pardon") is False
    assert match("abandonne") is False
    assert match("donc") is False
    assert match("donner") is False


def test_les_symboles_sont_cherches_tels_quels():
    """€ et emoji n'ont pas de limite de mot au sens des regex."""
    match = _kw_matcher(["€", "💀"])
    assert match("500€ !") is True
    assert match("mdr 💀") is True
    assert match("rien") is False


# ── token dominant ───────────────────────────────────────────────────────────

def _chat(*couples):
    return list(couples)


def test_le_token_dominant_compte_des_personnes():
    entries = _chat(*[(f"user{i}", "pogchamp") for i in range(5)])
    token, n = _dominant_token(entries)
    assert token == "pogchamp"
    assert n == 5


def test_un_spammeur_seul_ne_fait_pas_un_moment_fort():
    """Compter les occurrences laisserait une personne imposer son mot."""
    entries = _chat(*[("spammeur", "pogchamp") for _ in range(50)])
    _token, n = _dominant_token(entries)
    assert n == 1


def test_un_message_ne_vote_qu_une_fois_par_token():
    entries = _chat(("u1", "lul lul lul lul"), ("u2", "lul"))
    _token, n = _dominant_token(entries)
    assert n == 2


def test_chat_vide():
    assert _dominant_token([]) == ("", 0)


# ── qualification ────────────────────────────────────────────────────────────

def test_sans_chat_le_libelle_reste_generique():
    label, _color, excerpt = _classify_local([])
    assert label == _LIBELLE_MOMENT_FORT
    assert excerpt == ""


def test_une_donation_prime_sur_l_ambiance():
    """C'est un fait, pas une humeur : il ne doit pas être noyé par le volume."""
    entries = _chat(*[(f"u{i}", "lul") for i in range(10)])
    entries.append(("u99", "grosse donation de 500 euros"))
    label, color, excerpt = _classify_local(entries)
    assert color == _C_DONO
    assert "donation" in excerpt


def test_un_token_domine_sous_le_seuil_ne_qualifie_pas():
    entries = _chat(*[(f"u{i}", "pogchamp")
                      for i in range(_MIN_DOMINANT_USERS - 1)])
    label, _color, excerpt = _classify_local(entries)
    assert label == _LIBELLE_MOMENT_FORT
    assert excerpt == ""


def test_l_extrait_decrit_le_mouvement_de_chat():
    entries = _chat(*[(f"u{i}", "lul") for i in range(8)])
    _label, color, excerpt = _classify_local(entries)
    assert excerpt == "« lul » ×8"
    assert color == _C_FUNNY
