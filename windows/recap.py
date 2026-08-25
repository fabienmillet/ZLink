# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Récapitulatif de la session : ce qu'il s'est passé pendant qu'on regardait.

Le fil d'événements montre les choses au moment où elles arrivent puis les
laisse défiler ; rien ne survit à la fermeture. Cette fenêtre relit le journal
de session et en fait un résumé lisible, qu'on peut enregistrer.
"""

from __future__ import annotations

import datetime
import logging
import pathlib

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.session_log import SESSION, SessionSummary, fmt_duration, fmt_euros

logger = logging.getLogger(__name__)

_C_BG      = "#111111"
_C_SURFACE = "#1a1a1a"
_C_BORDER  = "#2a2a2a"
_C_TEXT    = "#cccccc"
_C_MUTED   = "#6a6a6a"
_C_GREEN   = "#00ff87"
_C_GOLD    = "#f5c518"
_FONT = "Segoe UI Variable"
_MONO = "Cascadia Code"

_SESSION_DIR = pathlib.Path.home() / ".zlink" / "sessions"


def _heure(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")


def render_text(s: SessionSummary) -> str:
    """Version texte du récapitulatif, pour l'enregistrement."""
    debut = datetime.datetime.fromtimestamp(s.started_at)
    lignes = [
        f"# Session ZLink — {debut.strftime('%d/%m/%Y %H:%M')}",
        "",
        f"Durée : {fmt_duration(s.duration_s)}",
    ]
    if s.donation_end > 0:
        gagne = max(0.0, s.donation_end - s.donation_start)
        lignes.append(
            f"Cagnotte : {fmt_euros(s.donation_start)} → {fmt_euros(s.donation_end)} "
            f"(+{fmt_euros(gagne)} pendant la session)")
    if s.viewers_peak:
        lignes.append(f"Pic d'audience : {s.viewers_peak:,}".replace(",", " ")
                      + " viewers")
    if s.watch:
        lignes += ["", "## Regardé"]
        lignes += [f"- {lg} — {fmt_duration(sec)}" for lg, sec in s.watch[:10]]
    if s.milestones:
        lignes += ["", "## Paliers de cagnotte"]
        lignes += [f"- {_heure(m.ts)} — {m.text}" for m in s.milestones]
    if s.goals:
        lignes += ["", "## Objectifs atteints"]
        lignes += [f"- {_heure(m.ts)} — {m.login} : {m.text}" for m in s.goals]
    if s.hype:
        lignes += ["", f"## Moments forts ({len(s.hype)})"]
        lignes += [f"- {_heure(m.ts)} — {m.login} : {m.text} ({m.extra})"
                   for m in s.hype[-20:]]
    if s.clips:
        lignes += ["", "## Clips enregistrés"]
        lignes += [f"- {_heure(m.ts)} — {m.login} : {m.text}" for m in s.clips]
    return "\n".join(lignes) + "\n"


def save_summary(s: SessionSummary | None = None) -> pathlib.Path | None:
    """Écrit le récapitulatif dans ~/.zlink/sessions/. None si rien à dire."""
    s = s or SESSION.summary()
    # Une session sans le moindre événement ne mérite pas un fichier.
    if not (s.watch or s.hype or s.goals or s.milestones or s.clips):
        return None
    try:
        _SESSION_DIR.mkdir(parents=True, exist_ok=True)
        nom = datetime.datetime.fromtimestamp(s.started_at).strftime("%Y%m%d_%H%M")
        dest = _SESSION_DIR / f"session_{nom}.md"
        dest.write_text(render_text(s), encoding="utf-8")
        logger.info("Récapitulatif de session écrit : %s", dest)
        return dest
    except OSError as exc:
        logger.error("Récapitulatif non enregistré — %s", exc)
        return None


class RecapDialog(QDialog):
    """Fenêtre du récapitulatif."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ZLink — récapitulatif de session")
        self.setMinimumSize(520, 560)
        self.setStyleSheet(f"QDialog {{ background: {_C_BG}; }}")
        self._summary = SESSION.summary()

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 16)
        root.setSpacing(14)

        debut = datetime.datetime.fromtimestamp(self._summary.started_at)
        titre = QLabel("Récapitulatif de session")
        titre.setFont(QFont(_FONT, 17, QFont.Weight.Bold))
        titre.setStyleSheet("color: #ffffff; background: transparent;")
        root.addWidget(titre)
        sous = QLabel(f"Depuis {debut.strftime('%H:%M')} · "
                      f"{fmt_duration(self._summary.duration_s)}")
        sous.setFont(QFont(_FONT, 10))
        sous.setStyleSheet(f"color: {_C_MUTED}; background: transparent;")
        root.addWidget(sous)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent;")
        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        self._v = QVBoxLayout(inner)
        self._v.setContentsMargins(0, 0, 6, 0)
        self._v.setSpacing(10)
        self._fill()
        self._v.addStretch()
        scroll.setWidget(inner)
        root.addWidget(scroll, stretch=1)

        bar = QHBoxLayout()
        self._saved_lbl = QLabel("")
        self._saved_lbl.setFont(QFont(_FONT, 9))
        self._saved_lbl.setStyleSheet(f"color: {_C_MUTED}; background: transparent;")
        bar.addWidget(self._saved_lbl, stretch=1)
        save_btn = self._button("Enregistrer", accent=False)
        save_btn.clicked.connect(self._on_save)
        bar.addWidget(save_btn)
        close_btn = self._button("Fermer", accent=True)
        close_btn.clicked.connect(self.accept)
        bar.addWidget(close_btn)
        root.addLayout(bar)

    # -- construction ----------------------------------------------------

    def _button(self, text: str, accent: bool) -> QPushButton:
        b = QPushButton(text)
        b.setFixedHeight(30)
        b.setMinimumWidth(100)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        if accent:
            b.setStyleSheet(
                f"QPushButton {{ background: {_C_GREEN}; color: #08130d;"
                f" border: none; border-radius: 6px; font-weight: bold; }}"
                f"QPushButton:hover {{ background: #4dffab; }}")
        else:
            b.setStyleSheet(
                f"QPushButton {{ background: {_C_SURFACE}; color: {_C_TEXT};"
                f" border: 1px solid {_C_BORDER}; border-radius: 6px; }}"
                f"QPushButton:hover {{ background: #232323; }}")
        return b

    def _section(self, titre: str, lignes: list[tuple[str, str]],
                 couleur: str = _C_GREEN) -> None:
        if not lignes:
            return
        cap = QLabel(titre.upper())
        cap.setFont(QFont(_FONT, 8, QFont.Weight.Bold))
        cap.setStyleSheet(
            f"color: {couleur}; background: transparent; letter-spacing: 1px;")
        self._v.addWidget(cap)

        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background: {_C_SURFACE}; border: 1px solid {_C_BORDER};"
            f" border-radius: 8px; }}")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(12, 10, 12, 10)
        cv.setSpacing(5)
        for gauche, droite in lignes:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            g = QLabel(gauche)
            # Texte brut : noms de chaînes et d'objectifs viennent d'APIs tierces.
            g.setTextFormat(Qt.TextFormat.PlainText)
            g.setFont(QFont(_FONT, 10))
            g.setStyleSheet(f"color: {_C_TEXT}; background: transparent; border: none;")
            row.addWidget(g, stretch=1)
            d = QLabel(droite)
            d.setTextFormat(Qt.TextFormat.PlainText)
            d.setFont(QFont(_MONO, 10))
            d.setStyleSheet(f"color: {_C_MUTED}; background: transparent; border: none;")
            row.addWidget(d)
            cv.addLayout(row)
        self._v.addWidget(card)

    def _fill(self) -> None:
        s = self._summary
        chiffres: list[tuple[str, str]] = []
        if s.donation_end > 0:
            gagne = max(0.0, s.donation_end - s.donation_start)
            chiffres.append(("Cagnotte à l'arrivée", fmt_euros(s.donation_end)))
            chiffres.append(("Récolté pendant la session", "+" + fmt_euros(gagne)))
        if s.viewers_peak:
            chiffres.append(("Pic d'audience",
                             f"{s.viewers_peak:,}".replace(",", " ")))
        self._section("Chiffres", chiffres, _C_GOLD)

        self._section("Regardé", [(lg, fmt_duration(sec))
                                  for lg, sec in s.watch[:8]])
        self._section("Paliers de cagnotte",
                      [(m.text, _heure(m.ts)) for m in reversed(s.milestones)],
                      _C_GOLD)
        self._section("Objectifs atteints",
                      [(f"{m.login} — {m.text}", _heure(m.ts))
                       for m in reversed(s.goals)])
        self._section(f"Moments forts ({len(s.hype)})",
                      [(f"{m.login} — {m.text}", _heure(m.ts))
                       for m in reversed(s.hype[-12:])], "#ff6b00")
        self._section("Clips enregistrés",
                      [(f"{m.login} — {pathlib.Path(m.text).name}", _heure(m.ts))
                       for m in reversed(s.clips)], "#38bdf8")

        if not (s.watch or s.hype or s.goals or s.milestones or s.clips):
            vide = QLabel("Rien à raconter pour l'instant — la session vient de "
                          "commencer.")
            vide.setWordWrap(True)
            vide.setFont(QFont(_FONT, 10))
            vide.setStyleSheet(f"color: {_C_MUTED}; background: transparent;")
            self._v.addWidget(vide)

    # -- actions ---------------------------------------------------------

    def _on_save(self) -> None:
        dest = save_summary(self._summary)
        self._saved_lbl.setText(
            f"Enregistré dans {dest}" if dest else "Rien à enregistrer")
