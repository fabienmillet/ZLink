# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Coordinateur mode 1 écran — barre de navigation escamotable.

Sur un seul moniteur les trois vues se superposent, et une barre en haut fait
passer de l'une à l'autre. Elle se rétractait au-dessus du bord dès qu'on s'en
éloignait, façon RDP ou VMware : élégant pour qui sait qu'elle existe, mais
rien ne l'annonçait, et un écran sans le moindre repère ne donne aucune raison
d'aller survoler quatre pixels en haut de l'écran.

D'où l'épingle, à gauche de la barre et ENGAGÉE PAR DÉFAUT : au premier
lancement la barre est simplement là. La décrocher rend l'escamotage
automatique, et ce choix est retenu d'une session à l'autre.

Épinglée ne veut pas dire indélogeable : la barre reste au-dessus de toutes
les fenêtres, elle se retire donc quand ZLink n'est plus l'application au
premier plan, et revient avec lui.
"""

from __future__ import annotations

import logging
from typing import Callable

from PyQt6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, Qt, QTimer
from PyQt6.QtGui import (QColor, QCursor, QFont, QKeySequence, QPainter,
                         QPainterPath, QScreen, QShortcut)
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QPushButton,
    QWidget,
)

try:
    import qtawesome as qta
    _QTA_OK = True
except Exception:  # noqa: BLE001
    # Pas seulement ImportError : qtawesome charge des polices et peut échouer
    # autrement. Un except trop étroit laisserait _QTA_OK non défini.
    qta = None  # type: ignore[assignment]
    _QTA_OK = False

from core import config_store
from core.win_fullscreen import mark_fullscreen
from windows.fullscreen import FullscreenWindow
from windows.grid import GridWindow
from windows.panel import PanelWindow

logger = logging.getLogger(__name__)

_NAV_W: int = 348      # largeur de la pilule
_NAV_H: int = 44       # hauteur de la pilule
_HOVER_Y: int = 4      # zone de déclenchement (pixels depuis le haut de l'écran)
_HIDE_DELAY_MS: int = 1800  # délai avant masquage après que le curseur s'éloigne

#: État de l'épingle dans config.json. Engagée tant que personne n'a dit le
#: contraire : une barre qu'on ne sait pas chercher n'existe pas.
_CLE_EPINGLE = "single_bar_pinned"

_BTN_SS = """
QPushButton {
    background: transparent;
    color: #888888;
    border: none;
    border-bottom: 2px solid transparent;
    padding: 0px 14px;
    font-family: Consolas;
    font-size: 12px;
    height: 44px;
}
QPushButton:checked { color: #00ff87; border-bottom: 2px solid #00ff87; }
QPushButton:hover:!checked { color: #ffffff; }
"""

_CLOSE_SS = """
QPushButton {
    background: transparent;
    color: #555555;
    font-size: 14px;
    border: none;
    padding: 0px 10px;
    height: 44px;
}
QPushButton:hover { color: #ff4444; }
"""

_PIN_SS = """
QPushButton {
    background: transparent;
    color: #555555;
    font-size: 13px;
    border: none;
    padding: 0px 6px;
    height: 44px;
}
QPushButton:checked { color: #00ff87; }
QPushButton:hover:!checked { color: #ffffff; }
"""


def charger_epingle() -> bool:
    """État de l'épingle au démarrage. Engagée tant que rien ne dit l'inverse.

    Une valeur illisible — le fichier s'édite à la main — vaut mieux qu'elle
    soit ignorée : rendre la barre invisible sur une faute de frappe, ce
    serait exactement la panne qu'on cherche à supprimer.
    """
    valeur = config_store.load().get(_CLE_EPINGLE, True)
    return valeur if isinstance(valeur, bool) else True


def enregistrer_epingle(epingle: bool) -> None:
    """Retient le choix pour la prochaine session. N'échoue jamais bruyamment."""
    if not config_store.save_merge({_CLE_EPINGLE: bool(epingle)}):
        logger.warning("epingle de la barre non enregistree")


# ---------------------------------------------------------------------------
# _NavPill — barre de navigation
# ---------------------------------------------------------------------------

class _NavPill(QWidget):
    """Barre centrée en haut : épinglée elle reste, décrochée elle s'escamote."""

    def __init__(
        self,
        screen: QScreen,
        on_switch: Callable[[int], None],
        on_close: Callable[[], None],
        pinned: bool | None = None,
    ) -> None:
        super().__init__()
        self._pinned = charger_epingle() if pinned is None else bool(pinned)
        g = screen.geometry()
        self._center_x  = g.x() + (g.width() - _NAV_W) // 2
        self._shown_y   = g.y()            # position visible : collée au bord haut
        self._hidden_y  = g.y() - _NAV_H  # position cachée : juste au-dessus
        self._screen_rect = g

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool   # pas d'icône dans la barre des tâches
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(_NAV_W, _NAV_H)
        self.move(self._center_x, self._depart_y())

        self._btns: list[QPushButton] = []
        hl = QHBoxLayout(self)
        hl.setContentsMargins(4, 0, 6, 0)
        hl.setSpacing(0)

        self._pin_btn = QPushButton()
        self._pin_btn.setCheckable(True)
        self._pin_btn.setChecked(self._pinned)
        self._pin_btn.setFixedSize(32, _NAV_H)
        self._pin_btn.setFont(QFont("Consolas", 12))
        self._pin_btn.setStyleSheet(_PIN_SS)
        self._pin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pin_btn.clicked.connect(self.set_pinned)
        hl.addWidget(self._pin_btn)
        self._peindre_epingle()

        for label in ("Panel", "Fullscreen", "Grille"):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(_NAV_H)
            btn.setFont(QFont("Consolas", 11))
            btn.setStyleSheet(_BTN_SS)
            idx = len(self._btns)
            btn.clicked.connect(lambda _c, i=idx: on_switch(i))
            hl.addWidget(btn)
            self._btns.append(btn)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(30, _NAV_H)
        close_btn.setFont(QFont("Consolas", 12))
        close_btn.setStyleSheet(_CLOSE_SS)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(on_close)
        hl.addWidget(close_btn)

        # Animation slide
        self._anim = QPropertyAnimation(self, b"pos")
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # Timer auto-masquage
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._slide_up)

        self.show()  # crée le handle natif
        # Associer la fenêtre à l'écran cible AVANT de la déplacer
        handle = self.windowHandle()
        if handle is not None:
            handle.setScreen(screen)
        self.move(self._center_x, self._depart_y())

    # ── dessin (fond arrondi uniquement en bas) ───────────────────────────────

    def paintEvent(self, event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect()
        radius = 10
        path = QPainterPath()
        path.moveTo(r.left(), r.top())
        path.lineTo(r.right(), r.top())
        path.lineTo(r.right(), r.bottom() - radius)
        path.arcTo(r.right() - radius * 2, r.bottom() - radius * 2, radius * 2, radius * 2, 0, -90)
        path.lineTo(r.left() + radius, r.bottom())
        path.arcTo(r.left(), r.bottom() - radius * 2, radius * 2, radius * 2, 270, -90)
        path.lineTo(r.left(), r.top())
        p.fillPath(path, QColor("#111111"))
        p.end()

    # ── API publique ──────────────────────────────────────────────────────────

    def set_active(self, idx: int) -> None:
        for i, btn in enumerate(self._btns):
            btn.setChecked(i == idx)

    # ── épingle ───────────────────────────────────────────────────────────────

    def is_pinned(self) -> bool:
        return self._pinned

    def set_pinned(self, epingle: bool) -> None:
        """Engage ou décroche l'épingle, et retient le choix."""
        self._pinned = bool(epingle)
        self._pin_btn.setChecked(self._pinned)
        self._peindre_epingle()
        enregistrer_epingle(self._pinned)
        if self._pinned:
            self.reveal()
        else:
            self.start_hide()

    def _depart_y(self) -> int:
        """Où la barre se place à l'ouverture, avant toute animation."""
        return self._shown_y if self._pinned else self._hidden_y

    def _peindre_epingle(self) -> None:
        """L'épingle doit se lire d'un coup d'œil : les deux états diffèrent.

        Sans qtawesome, deux glyphes que Consolas possède à coup sûr — une
        icône manquante rendrait le bouton invisible, donc introuvable.
        """
        if self._pinned:
            nom, glyphe, bulle = "mdi6.pin", "●", "Barre épinglée — cliquer pour la masquer automatiquement"
        else:
            nom, glyphe, bulle = "mdi6.pin-off", "○", "Barre escamotable — cliquer pour la garder affichée"
        self._pin_btn.setToolTip(bulle)
        if _QTA_OK:
            couleur = "#00ff87" if self._pinned else "#555555"
            self._pin_btn.setText("")
            self._pin_btn.setIcon(qta.icon(nom, color=couleur))
        else:
            self._pin_btn.setText(glyphe)

    def reveal(self) -> None:
        """Glisse vers le bas pour révéler la pilule."""
        self._hide_timer.stop()
        if self.y() == self._shown_y:
            return
        self._anim.stop()
        self._anim.setStartValue(QPoint(self._center_x, self.y()))
        self._anim.setEndValue(QPoint(self._center_x, self._shown_y))
        self._anim.start()

    def start_hide(self) -> None:
        """Lance le timer d'auto-masquage (re-démarre si déjà actif)."""
        if self._pinned:
            return
        if not self._hide_timer.isActive():
            self._hide_timer.start(_HIDE_DELAY_MS)

    def hide_now(self) -> None:
        """Retire la barre SANS attendre, épingle ou pas.

        La barre est au-dessus de toutes les fenêtres : la laisser épinglée
        par-dessus une AUTRE application ferait un bandeau flottant que
        personne n'a demandé. Elle revient dès que ZLink reprend la main.
        """
        self._hide_timer.stop()
        if self.y() != self._hidden_y:
            self._slide_up()

    def cancel_hide(self) -> None:
        """Annule le timer si le curseur revient sur la pilule."""
        self._hide_timer.stop()

    def _slide_up(self) -> None:
        self._anim.stop()
        self._anim.setStartValue(QPoint(self._center_x, self.y()))
        self._anim.setEndValue(QPoint(self._center_x, self._hidden_y))
        self._anim.start()

    # Annuler le masquage quand la souris entre sur la pilule
    def enterEvent(self, event) -> None:  # type: ignore[override]
        self.cancel_hide()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self.start_hide()
        super().leaveEvent(event)


# ---------------------------------------------------------------------------
# SingleModeShell
# ---------------------------------------------------------------------------

class SingleModeShell:
    """Coordinateur mode 1 écran : 3 fenêtres fullscreen + pilule nav auto-hide."""

    _IDX_PANEL      = 0
    _IDX_FULLSCREEN = 1
    _IDX_GRID       = 2

    def __init__(self, screen: QScreen) -> None:
        g = screen.geometry()
        self._screen = screen
        self._screen_rect = g

        # ── 3 fenêtres (couvrent tout l'écran) ───────────────────────
        self.panel = PanelWindow(screen, show_on_init=False)
        self.panel.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.panel.setGeometry(g)

        self.fullscreen = FullscreenWindow(screen, show_on_init=False)
        self.fullscreen.setGeometry(g)

        self.grid = GridWindow(screen, show_on_init=False)
        self.grid.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.grid.setGeometry(g)

        # ── Pilule nav auto-hide ──────────────────────────────────────
        self._pill = _NavPill(
            screen,
            on_switch=self._switch,
            on_close=QApplication.quit,
        )
        self._poser_le_rappel_de_la_barre()

        # ── Polling curseur toutes les 80 ms ──────────────────────────
        self._poll = QTimer()
        self._poll.setInterval(80)
        self._poll.timeout.connect(self._check_cursor)
        self._poll.start()

        logger.info("SingleModeShell initialisé sur %s", screen.name())
        self._switch(self._IDX_FULLSCREEN)

    # ── navigation ───────────────────────────────────────────────────────────

    def _switch(self, idx: int) -> None:
        g = self._screen_rect
        for i, win in enumerate([self.panel, self.fullscreen, self.grid]):
            if i == idx:
                win.setGeometry(g)
                win.show()  # crée le handle natif à la bonne position
                handle = win.windowHandle()
                if handle is not None:
                    handle.setScreen(self._screen)
                win.showFullScreen()
                mark_fullscreen(win)
                win.raise_()
                win.activateWindow()
            else:
                win.hide()
        self._pill.set_active(idx)
        self._pill.raise_()

    def _poser_le_rappel_de_la_barre(self) -> None:
        """F1 fait revenir la barre et l'y laisse, depuis n'importe quelle page.

        La barre ne se montrait que par le survol d'une zone en haut de
        l'écran. Cela suppose un pointeur qu'on promène — vrai sur un bureau,
        faux sur un Steam Deck, où l'on navigue au pavé et aux boutons. Un
        utilisateur s'est retrouvé bloqué sur la page plein écran sans aucun
        moyen d'en sortir.

        La touche ÉPINGLE plutôt qu'elle ne révèle : révéler laisserait la
        barre repartir deux secondes plus tard, et l'on serait bloqué de
        nouveau. Une seconde pression la décroche.

        `QShortcut` sur les trois fenêtres, et non un `keyPressEvent` : la
        frappe arrive d'abord à la fenêtre qui a la main, et il n'y a pas de
        fenêtre parente commune à ces trois-là.
        """
        for fenetre in (self.panel, self.fullscreen, self.grid):
            raccourci = QShortcut(QKeySequence("F1"), fenetre)
            raccourci.activated.connect(self._basculer_l_epingle)

    def _basculer_l_epingle(self) -> None:
        """Épingle ou décroche la barre, et la remet devant si elle revient."""
        epinglee = not self._pill.is_pinned()
        self._pill.set_pinned(epinglee)
        if epinglee:
            self._pill.reveal()
            self._pill.raise_()

    @staticmethod
    def _zlink_au_premier_plan() -> bool:
        """ZLink a-t-il la main ? Deux avis plutôt qu'un, et le doute lui profite.

        `activeWindow()` seul rendait `None` en permanence sous gamescope, le
        compositeur de SteamOS : la barre de navigation y était donc masquée
        DÉFINITIVEMENT dès le lancement, et comme elle est topmost, elle
        continuait de flotter par-dessus les autres applications. Signalé sur
        Steam Deck, avec les deux symptômes à la fois.

        `applicationState` répond à la même question par un autre chemin. Tant
        que l'un des deux dit que ZLink est devant, on le croit : au pire la
        barre reste un instant de trop, au pire de l'autre côté elle devient
        introuvable — et il n'y a pas de symétrie entre ces deux torts.
        """
        if QApplication.activeWindow() is not None:
            return True
        instance = QApplication.instance()
        if instance is None:
            return False
        return instance.applicationState() == Qt.ApplicationState.ApplicationActive

    # ── détection zone de déclenchement ──────────────────────────────────────

    def _check_cursor(self) -> None:
        # Ne pas interférer si une autre application est au premier plan. Même
        # épinglée : la barre est topmost, elle flotterait par-dessus.
        if not self._zlink_au_premier_plan():
            self._pill.hide_now()
            return
        pos = QCursor.pos()
        g = self._screen_rect
        in_hotzone = (
            pos.y() <= g.y() + _HOVER_Y
            and g.x() <= pos.x() <= g.x() + g.width()
        )
        if in_hotzone or self._pill.is_pinned():
            self._pill.reveal()
        else:
            self._pill.start_hide()

