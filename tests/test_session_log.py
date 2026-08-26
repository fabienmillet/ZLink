# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Journal de session : accumulation, temps par chaîne, récapitulatif."""

from __future__ import annotations

import pytest

from core.session_log import SessionLog, fmt_duration, fmt_euros


# ── mise en forme ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("secondes,attendu", [
    (0, "0 s"), (30, "30 s"), (59, "59 s"),
    (60, "1 min"), (420, "7 min"), (3599, "59 min"),
    (3600, "1 h 00"), (3725, "1 h 02"), (86400, "24 h 00"),
    (-5, "0 s"),          # une durée négative n'a pas de sens : plancher à 0
])
def test_duree_lisible(secondes, attendu):
    assert fmt_duration(secondes) == attendu


def test_euros_avec_espace_separateur():
    assert fmt_euros(1154211.58) == "1 154 212 €"
    assert fmt_euros(0) == "0 €"


# ── accumulation ─────────────────────────────────────────────────────────────

def test_temps_par_chaine_suit_les_bascules(monkeypatch):
    """Le temps passé s'impute à la chaîne regardée, pas à la dernière connue."""
    horloge = {"t": 1000.0}
    monkeypatch.setattr("core.session_log.time.monotonic", lambda: horloge["t"])

    log = SessionLog()
    log.set_current_stream("zerator")
    horloge["t"] += 60
    log.set_current_stream("domingo")
    horloge["t"] += 30
    resume = log.summary()

    temps = dict(resume.watch)
    assert temps["zerator"] == pytest.approx(60.0)
    # La chaîne en cours compte son temps courant sans attendre la bascule.
    assert temps["domingo"] == pytest.approx(30.0)


def test_rebasculer_sur_la_meme_chaine_ne_double_pas_le_temps(monkeypatch):
    horloge = {"t": 5000.0}
    monkeypatch.setattr("core.session_log.time.monotonic", lambda: horloge["t"])
    log = SessionLog()
    log.set_current_stream("zerator")
    horloge["t"] += 10
    log.set_current_stream("zerator")
    horloge["t"] += 10
    assert dict(log.summary().watch)["zerator"] == pytest.approx(20.0)


def test_moments_collectes():
    log = SessionLog()
    log.add_hype("zerator", "Moment fort", 0.9)
    log.add_goal("domingo", "Piment")
    log.add_milestone("1 M€")
    log.add_clip("mistermv", "/tmp/clip.ts")
    r = log.summary()
    assert [m.login for m in r.hype] == ["zerator"]
    assert [m.text for m in r.goals] == ["Piment"]
    assert [m.text for m in r.milestones] == ["1 M€"]
    assert [m.text for m in r.clips] == ["/tmp/clip.ts"]


def test_clip_sans_chemin_ignore():
    log = SessionLog()
    log.add_clip("zerator", "")
    assert log.summary().clips == []


# ── cagnotte et audience ─────────────────────────────────────────────────────

def test_cagnotte_part_du_premier_releve_non_nul():
    """Avant l'event l'API renvoie zéro.

    Partir de zéro afficherait toute la cagnotte comme récoltée pendant la
    session, ce qui est faux dès qu'on lance ZLink en cours d'édition.
    """
    log = SessionLog()
    log.observe_stats(0.0, 0)
    log.observe_stats(500_000.0, 10)
    log.observe_stats(600_000.0, 30)
    r = log.summary()
    assert r.donation_start == pytest.approx(500_000.0)
    assert r.donation_end == pytest.approx(600_000.0)


def test_cagnotte_ne_recule_pas():
    log = SessionLog()
    log.observe_stats(600_000.0, 0)
    log.observe_stats(100_000.0, 0)   # relevé aberrant
    assert log.summary().donation_end == pytest.approx(600_000.0)


def test_pic_de_spectateurs_retenu():
    log = SessionLog()
    log.observe_stats(1.0, 100)
    log.observe_stats(1.0, 900)
    log.observe_stats(1.0, 400)
    assert log.summary().viewers_peak == 900


def test_stats_illisibles_ignorees():
    log = SessionLog()
    log.observe_stats("abc", "def")
    r = log.summary()
    assert r.donation_end == 0.0 and r.viewers_peak == 0
