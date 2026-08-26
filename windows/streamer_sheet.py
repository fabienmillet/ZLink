# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Fiche d'un participant : tout ce que ZLink sait de lui, en un endroit.

Ces données transitent déjà toutes — cagnotte, audience, objectifs, passages au
programme, moments forts de la session — mais elles sont éparpillées dans cinq
onglets et un fil qui défile. La fiche les rassemble pour une seule personne.

La courbe est bâtie pendant la session : l'API ne publie pas d'historique par
streamer, seulement un cumul instantané. Elle commence donc au lancement de
l'application, ce que la fiche dit explicitement plutôt que de laisser croire à
un historique complet.
"""

from __future__ import annotations

import logging
import time

from PyQt6.QtCore import Qt, pyqtSignal
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

#: Points de cagnotte relevés par streamer pendant la session. Alimenté par le
#: panel à chaque sondage ; borné, une session peut durer quatre jours.
_HISTORIQUE: dict[str, list[tuple[float, float]]] = {}
_MAX_POINTS = 600


def note_donation(login: str, montant: float) -> None:
    """Enregistre un point de cagnotte pour `login`."""
    if not login:
        return
    serie = _HISTORIQUE.setdefault(login, [])
    if serie and abs(serie[-1][1] - montant) < 0.5:
        return          # rien de neuf : ne pas gonfler la série pour rien
    serie.append((time.time(), float(montant)))
    del serie[:-_MAX_POINTS]


def historique(login: str) -> list[tuple[float, float]]:
    return list(_HISTORIQUE.get(login, []))


class _Courbe(QWidget):
    """Tracé compact de la progression de la cagnotte pendant la session."""

    def __init__(self, points: list[tuple[float, float]],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._points = points
        self.setMinimumHeight(90)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        from PyQt6.QtGui import QColor, QPainter, QPen
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#141414"))
        pts = self._points
        if len(pts) < 2:
            p.setPen(QColor(_C_MUTED))
            p.setFont(QFont(_FONT, 9))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "Pas encore assez de points")
            p.end()
            return
        t0, t1 = pts[0][0], pts[-1][0]
        v0 = min(v for _, v in pts)
        v1 = max(v for _, v in pts)
        w, h = self.width() - 16, self.height() - 16
        dt = max(1e-6, t1 - t0)
        dv = max(1e-6, v1 - v0)
        pen = QPen(QColor(_C_GREEN))
        pen.setWidth(2)
        p.setPen(pen)
        prev = None
        for t, v in pts:
            x = 8 + (t - t0) / dt * w
            y = 8 + h - (v - v0) / dv * h
            if prev is not None:
                p.drawLine(int(prev[0]), int(prev[1]), int(x), int(y))
            prev = (x, y)
        p.end()


class StreamerSheet(QDialog):
    """Fenêtre de fiche. Émet ce que l'utilisateur demande d'en faire."""

    stream_requested = pyqtSignal(str)
    grid_requested   = pyqtSignal(str)

    def __init__(self, streamer, goals: list | None = None,
                 events: list | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._s = streamer
        self.setWindowTitle(f"ZLink — {streamer.display}")
        self.setMinimumSize(480, 560)
        self.setStyleSheet(f"QDialog {{ background: {_C_BG}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 16)
        root.setSpacing(12)
        root.addLayout(self._entete())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent;")
        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        self._v = QVBoxLayout(inner)
        self._v.setContentsMargins(0, 0, 6, 0)
        self._v.setSpacing(10)
        self._remplir(goals or [], events or [])
        self._v.addStretch()
        scroll.setWidget(inner)
        root.addWidget(scroll, stretch=1)

        barre = QHBoxLayout()
        barre.addStretch()
        if getattr(streamer, "donation_url", ""):
            don = self._bouton("Donner", accent=False)
            don.clicked.connect(self._on_donate)
            barre.addWidget(don)
        grille = self._bouton("Ajouter à la grille", accent=False)
        grille.clicked.connect(lambda: (self.grid_requested.emit(self._s.twitch_login),
                                        self.accept()))
        barre.addWidget(grille)
        voir = self._bouton("Regarder", accent=True)
        voir.clicked.connect(lambda: (self.stream_requested.emit(self._s.twitch_login),
                                      self.accept()))
        barre.addWidget(voir)
        root.addLayout(barre)

    # -- construction ----------------------------------------------------

    def _bouton(self, texte: str, accent: bool) -> QPushButton:
        b = QPushButton(texte)
        b.setFixedHeight(30)
        b.setMinimumWidth(110)
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

    def _entete(self) -> QHBoxLayout:
        s = self._s
        h = QHBoxLayout()
        h.setSpacing(14)
        from windows.panel import _make_person_avatar
        h.addWidget(_make_person_avatar(s.display, s.twitch_login, 52))
        col = QVBoxLayout()
        col.setSpacing(2)
        nom = QLabel(s.display)
        nom.setTextFormat(Qt.TextFormat.PlainText)
        nom.setFont(QFont(_FONT, 17, QFont.Weight.Bold))
        nom.setStyleSheet("color: #ffffff; background: transparent;")
        col.addWidget(nom)
        etat = "en direct" if s.online else "hors ligne"
        detail = f"{etat} · {s.twitch_login}"
        if s.online and s.game:
            detail += f" · {s.game}"
        if s.online:
            from core import live_uptime
            depuis = live_uptime.texte(s.twitch_login)
            if depuis:
                detail += f" · {depuis}"
        sous = QLabel(detail)
        sous.setTextFormat(Qt.TextFormat.PlainText)
        sous.setFont(QFont(_FONT, 10))
        sous.setStyleSheet(
            f"color: {_C_GREEN if s.online else _C_MUTED}; background: transparent;")
        col.addWidget(sous)
        h.addLayout(col, stretch=1)
        return h

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
            g.setTextFormat(Qt.TextFormat.PlainText)
            g.setFont(QFont(_FONT, 10))
            g.setWordWrap(True)
            g.setStyleSheet(
                f"color: {_C_TEXT}; background: transparent; border: none;")
            row.addWidget(g, stretch=1)
            d = QLabel(droite)
            d.setTextFormat(Qt.TextFormat.PlainText)
            d.setFont(QFont(_MONO, 10))
            d.setStyleSheet(
                f"color: {_C_MUTED}; background: transparent; border: none;")
            row.addWidget(d)
            cv.addLayout(row)
        self._v.addWidget(card)

    def _remplir(self, goals: list, events: list) -> None:
        s = self._s
        chiffres = [("Cagnotte", s.donation_formatted or f"{s.donation:,.0f} €".replace(",", "\u202f"))]
        if s.online:
            chiffres.append(("Audience", f"{s.viewers:,}".replace(",", " ")))
        if s.location:
            chiffres.append(("Lieu", s.location))
        self._section("Chiffres", chiffres, _C_GOLD)

        pts = historique(s.twitch_login)
        cap = QLabel("CAGNOTTE DEPUIS LE LANCEMENT")
        cap.setFont(QFont(_FONT, 8, QFont.Weight.Bold))
        cap.setStyleSheet(
            f"color: {_C_GREEN}; background: transparent; letter-spacing: 1px;")
        self._v.addWidget(cap)
        self._v.addWidget(_Courbe(pts))
        if len(pts) >= 2:
            gagne = pts[-1][1] - pts[0][1]
            note = QLabel(f"+{gagne:,.0f} € depuis l'ouverture de ZLink".replace(",", " "))
            note.setFont(QFont(_FONT, 9))
            note.setStyleSheet(f"color: {_C_MUTED}; background: transparent;")
            self._v.addWidget(note)

        self._section("Objectifs", [
            (g.name, ("atteint" if g.accomplished
                      else f"{g.amount:,.0f} €".replace(",", " ")))
            for g in goals[:12]
        ])

        self._section("Au programme", [
            (ev.name or "Événement", f"{ev.day} · {ev.start_local}")
            for ev in events[:10]
        ], "#a855f7")

        from core.session_log import SESSION
        moments = [m for m in SESSION.summary().hype
                   if m.login == s.twitch_login]
        if moments:
            import datetime
            self._section(
                f"Moments forts de votre session ({len(moments)})",
                [(m.text, datetime.datetime.fromtimestamp(m.ts).strftime("%H:%M"))
                 for m in reversed(moments[-8:])], "#ff6b00")

    def _on_donate(self) -> None:
        from windows.fullscreen import ouvrir_page_de_don
        ouvrir_page_de_don(getattr(self._s, "donation_url", ""))
