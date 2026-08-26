# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Récapitulatif de session : mise en forme, enregistrement, fenêtre.

Le récapitulatif est la seule trace qui survit à la soirée : ce qui compte est
qu'il reste lisible avec des données partielles ou aberrantes, et qu'il n'écrive
un fichier que lorsqu'il a quelque chose à dire.
"""

from __future__ import annotations

import datetime

import pytest

from core.session_log import SessionSummary, _Moment
from windows import recap


# ── fabriques ────────────────────────────────────────────────────────────────

#: Horodatage fixe : le rendu passe par l'heure locale, on la recalcule dans le
#: test plutôt que de coder une heure en dur qui casserait ailleurs.
_TS = 1_700_000_000.0


def _heure_locale(ts: float = _TS) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")


def _resume(**kwargs) -> SessionSummary:
    """SessionSummary vide, sauf ce que le test précise."""
    kwargs.setdefault("started_at", _TS)
    return SessionSummary(**kwargs)


def _resume_complet() -> SessionSummary:
    return _resume(
        duration_s=3725.0,
        watch=[("zerator", 2400.0), ("domingo", 900.0)],
        hype=[_Moment(_TS, "zerator", "Ça s'emballe", "92%")],
        goals=[_Moment(_TS, "domingo", "Piment")],
        milestones=[_Moment(_TS, "", "1 M€")],
        clips=[_Moment(_TS, "mistermv", "/tmp/clips/moment.ts")],
        donation_start=500_000.0,
        donation_end=1_154_211.58,
        viewers_peak=123_456,
    )


# ── render_text ──────────────────────────────────────────────────────────────

def test_entete_et_duree_toujours_presentes():
    """Le squelette minimal doit tenir même sans le moindre événement."""
    texte = recap.render_text(_resume(duration_s=3725.0))
    debut = datetime.datetime.fromtimestamp(_TS).strftime("%d/%m/%Y %H:%M")
    assert texte.startswith(f"# Session ZLink — {debut}")
    assert "Durée : 1 h 02" in texte
    assert texte.endswith("\n")


def test_session_vide_n_affiche_aucune_section():
    """Une section vide serait un titre suivi de rien : on la supprime."""
    texte = recap.render_text(_resume())
    for titre in ("## Regardé", "## Paliers", "## Objectifs",
                  "## Moments forts", "## Clips"):
        assert titre not in texte
    assert "Cagnotte" not in texte
    assert "Pic d'audience" not in texte


# fmt_euros sépare les milliers par une espace fine insécable, pas par une
# espace ordinaire. La nommer évite un échec incompréhensible si quelqu'un
# retape l'attendu à la main.
_FINE = " "


@pytest.mark.parametrize("debut,fin,attendu", [
    # Cas courant : la cagnotte progresse pendant la session.
    (500_000.0, 600_000.0, f"+100{_FINE}000 €"),
    # Relevé aberrant — la cagnotte semble avoir reculé : on plancher à zéro
    # plutôt que d'afficher un gain négatif.
    (600_000.0, 500_000.0, "+0 €"),
    # Un seul relevé : début et fin confondus.
    (750_000.0, 750_000.0, "+0 €"),
])
def test_gain_de_cagnotte(debut, fin, attendu):
    texte = recap.render_text(_resume(donation_start=debut, donation_end=fin))
    assert attendu in texte
    assert "Cagnotte :" in texte


def test_cagnotte_absente_si_jamais_relevee():
    """donation_end à zéro veut dire « l'API n'a rien donné », pas « zéro euro »."""
    assert "Cagnotte" not in recap.render_text(_resume(donation_end=0.0))


def test_pic_d_audience_separe_les_milliers_par_des_espaces():
    texte = recap.render_text(_resume(viewers_peak=123_456))
    assert f"Pic d'audience : 123{_FINE}456 viewers" in texte


def test_pic_d_audience_nul_non_affiche():
    assert "Pic d'audience" not in recap.render_text(_resume(viewers_peak=0))


def test_regarde_plafonne_a_dix_chaines():
    """Au-delà, le récapitulatif devient une liste et ne se lit plus."""
    watch = [(f"chaine{i}", float(100 - i)) for i in range(30)]
    texte = recap.render_text(_resume(watch=watch))
    assert "- chaine9 — 1 min" in texte
    assert "chaine10" not in texte


def test_moments_forts_gardent_les_vingt_derniers():
    """Les pics de chat se comptent par dizaines : seuls les derniers importent."""
    hype = [_Moment(_TS, "zerator", f"pic {i}", "50%") for i in range(25)]
    texte = recap.render_text(_resume(hype=hype))
    assert "## Moments forts (25)" in texte, "le compte total reste annoncé"
    assert "pic 24" in texte
    assert "pic 4" not in texte


def test_toutes_les_sections_d_une_session_pleine():
    texte = recap.render_text(_resume_complet())
    h = _heure_locale()
    assert "## Regardé" in texte and "- zerator — 40 min" in texte
    assert f"- {h} — 1 M€" in texte
    assert f"- {h} — domingo : Piment" in texte
    assert f"- {h} — zerator : Ça s'emballe (92%)" in texte
    assert f"- {h} — mistermv : /tmp/clips/moment.ts" in texte


# ── save_summary ─────────────────────────────────────────────────────────────

@pytest.fixture
def dossier_sessions(tmp_path, monkeypatch):
    """Détourne le dossier d'écriture : jamais dans le vrai ~/.zlink."""
    cible = tmp_path / "sessions"
    monkeypatch.setattr(recap, "_SESSION_DIR", cible)
    return cible


def test_session_sans_evenement_n_ecrit_rien(dossier_sessions):
    """Un fichier par lancement raté encombrerait le dossier pour rien."""
    assert recap.save_summary(_resume(duration_s=12.0)) is None
    assert not dossier_sessions.exists()


@pytest.mark.parametrize("champ,valeur", [
    ("watch", [("zerator", 60.0)]),
    ("hype", [_Moment(_TS, "zerator", "pic", "50%")]),
    ("goals", [_Moment(_TS, "domingo", "Piment")]),
    ("milestones", [_Moment(_TS, "", "1 M€")]),
    ("clips", [_Moment(_TS, "mistermv", "/tmp/c.ts")]),
])
def test_un_seul_evenement_suffit_a_declencher_l_ecriture(
        dossier_sessions, champ, valeur):
    dest = recap.save_summary(_resume(**{champ: valeur}))
    assert dest is not None and dest.exists()


def test_le_nom_du_fichier_porte_la_date_de_debut(dossier_sessions):
    dest = recap.save_summary(_resume(milestones=[_Moment(_TS, "", "1 M€")]))
    horodatage = datetime.datetime.fromtimestamp(_TS).strftime("%Y%m%d_%H%M")
    assert dest.name == f"session_{horodatage}.md"
    assert dest.read_text(encoding="utf-8").startswith("# Session ZLink")


def test_ecriture_impossible_ne_leve_pas(tmp_path, monkeypatch):
    """Le récapitulatif s'écrit à la fermeture : y planter perdrait la sortie."""
    obstacle = tmp_path / "obstacle"
    obstacle.write_text("je ne suis pas un dossier", encoding="utf-8")
    monkeypatch.setattr(recap, "_SESSION_DIR", obstacle / "sessions")
    assert recap.save_summary(_resume(milestones=[_Moment(_TS, "", "x")])) is None


def test_sans_argument_le_journal_global_est_relu(dossier_sessions, monkeypatch):
    resume = _resume(clips=[_Moment(_TS, "zerator", "/tmp/c.ts")])
    monkeypatch.setattr(recap, "SESSION",
                        type("FauxJournal", (), {"summary": lambda self: resume})())
    assert recap.save_summary() is not None


# ── fenêtre ──────────────────────────────────────────────────────────────────

@pytest.fixture
def journal(monkeypatch):
    """Remplace le journal global par un résumé au choix du test."""
    def _poser(resume: SessionSummary) -> None:
        monkeypatch.setattr(
            recap, "SESSION",
            type("FauxJournal", (), {"summary": lambda self: resume})())
    return _poser


def _textes(dialogue) -> list[str]:
    from PyQt6.QtWidgets import QLabel
    return [lbl.text() for lbl in dialogue.findChildren(QLabel)]


def test_fenetre_vide_explique_qu_il_n_y_a_rien_a_dire(qtbot, journal):
    """Une fenêtre blanche laisserait croire à un bug d'affichage."""
    journal(_resume(duration_s=42.0))
    d = recap.RecapDialog()
    qtbot.addWidget(d)
    textes = _textes(d)
    assert any("Rien à raconter" in t for t in textes)
    assert not any(t == "CHIFFRES" for t in textes)


def test_fenetre_pleine_montre_chaque_section(qtbot, journal):
    journal(_resume_complet())
    d = recap.RecapDialog()
    qtbot.addWidget(d)
    textes = _textes(d)
    for titre in ("CHIFFRES", "REGARDÉ", "PALIERS DE CAGNOTTE",
                  "OBJECTIFS ATTEINTS", "MOMENTS FORTS (1)", "CLIPS ENREGISTRÉS"):
        assert titre in textes
    # Le clip est désigné par son nom de fichier, pas par son chemin complet :
    # une colonne de /home/…/zlink/clips/ ne dit rien de plus.
    assert any(t == "mistermv — moment.ts" for t in textes)
    assert any(f"1{_FINE}154{_FINE}212 €" in t for t in textes)
    assert any(t == "123 456" for t in textes)


def test_enregistrer_affiche_le_chemin_obtenu(qtbot, journal, dossier_sessions):
    journal(_resume_complet())
    d = recap.RecapDialog()
    qtbot.addWidget(d)
    d._on_save()
    assert str(dossier_sessions) in d._saved_lbl.text()


def test_enregistrer_une_session_vide_le_dit(qtbot, journal, dossier_sessions):
    journal(_resume(duration_s=5.0))
    d = recap.RecapDialog()
    qtbot.addWidget(d)
    d._on_save()
    assert d._saved_lbl.text() == "Rien à enregistrer"
