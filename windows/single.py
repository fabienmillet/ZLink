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

from PyQt6.QtCore import (QEasingCurve, QEvent, QPoint, QPropertyAnimation,
                          Qt, QTimer)
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
    """Barre centrée en haut : épinglée elle reste, décrochée elle s'escamote.

    **Un widget ENFANT, jamais une fenêtre.** Elle était top-level avec un
    drapeau « toujours au-dessus », ce qui la place au-dessus de tout au
    niveau du COMPOSITEUR — pas seulement au-dessus des fenêtres de ZLink.
    Sous Wayland et XWayland, elle s'échappait de l'application : visible
    par-dessus les autres programmes, et inatteignable depuis ZLink, dont
    l'empilement n'est pas garanti. Signalé sur Steam Deck.

    Enfant de la fenêtre affichée, elle ne peut plus sortir de l'application,
    et `raise_()` entre frères et sœurs est fiable partout. Le masquage la
    glisse au-dessus du bord haut : un enfant est découpé par son parent, elle
    disparaît donc au lieu de déborder.

    Corollaire : la garde qui l'escamotait quand une autre application passait
    devant n'a plus d'objet, et elle est retirée. C'est elle qui, sous
    gamescope, la faisait disparaître pour de bon.
    """

    def __init__(
        self,
        screen: QScreen,
        on_switch: Callable[[int], None],
        on_close: Callable[[], None],
        pinned: bool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._pinned = charger_epingle() if pinned is None else bool(pinned)
        self._screen_rect = screen.geometry()
        # Coordonnées relatives au PARENT. Les trois fenêtres occupent l'écran
        # entier, donc les valeurs coïncident avec celles d'avant — mais elles
        # ne peuvent plus dériver si une fenêtre cesse d'être plein écran.
        self._center_x = max(0, (self._screen_rect.width() - _NAV_W) // 2)
        self._shown_y  = 0          # visible : collée au bord haut du parent
        self._hidden_y = -_NAV_H    # cachée : glissée au-dessus, donc découpée

        # FENÊTRE NATIVE, comme le lecteur mpv qu'elle doit surmonter. Sans
        # cela elle disparaissait purement et simplement : `MpvWidget` pose
        # `WA_NativeWindow` — il le faut pour `--wid` — et une fenêtre native
        # est composée par le serveur graphique, donc dessinée AU-DESSUS de
        # tout widget Qt frère, quel que soit `raise_()`. La pilule, devenue
        # un simple enfant, passait dessous. Native elle aussi, les deux
        # s'empilent enfin selon le même ordre, que `raise_()` gouverne.
        #
        # `WA_DontCreateNativeAncestors` évite de rendre natifs tous ses
        # parents au passage : c'est la paire exacte qu'utilise mpv_widget.
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
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

        # Ni `show()` ni association d'écran : sans fenêtre native à créer,
        # c'est `attacher()` qui la fait apparaître dans la vue courante.
        self.move(self._center_x, self._depart_y())

    def attacher(self, fenetre: QWidget) -> None:
        """Passe la barre dans la fenêtre affichée, à sa place et devant.

        `setParent` masque toujours le widget : le `show()` qui suit n'est pas
        une précaution mais une obligation.
        """
        if self.parent() is not fenetre:
            self.setParent(fenetre)
            # Le passage en plein écran redispose la fenêtre APRÈS
            # l'attachement, et une redisposition peut remettre le lecteur
            # devant. On se relève à chaque fois plutôt qu'une seule.
            fenetre.installEventFilter(self)
        self._recadrer(fenetre)
        self.show()
        self.raise_()

    def eventFilter(self, objet, evenement):  # type: ignore[override]
        """Se replace et repasse devant quand la fenêtre change de taille."""
        if evenement.type() == QEvent.Type.Resize and objet is self.parent():
            self._recadrer(objet)
            self.raise_()
        return super().eventFilter(objet, evenement)

    def _recadrer(self, fenetre: QWidget) -> None:
        """Recentre la barre sur la largeur réelle du parent."""
        largeur = fenetre.width() or self._screen_rect.width()
        self._center_x = max(0, (largeur - _NAV_W) // 2)
        self.move(self._center_x,
                  self.y() if self.y() in (self._shown_y, self._hidden_y)
                  else self._depart_y())

    # ── dessin (fond arrondi uniquement en bas) ───────────────────────────────

    def paintEvent(self, event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect()
        # Le rectangle ENTIER d'abord, au noir de la page. Une fenêtre native
        # ne laisse rien transparaître de ce qu'il y a derrière : les pixels
        # hors des coins arrondis resteraient indéfinis, et la barre se
        # découperait sur un fond aléatoire. On les peint donc nous-mêmes de
        # la couleur qu'ils auraient eue.
        p.fillRect(r, QColor("#0a0a0a"))
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
        """Retire la barre SANS attendre, épingle ou pas."""
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
        # La barre SUIT la vue : elle est enfant de la fenêtre affichée, et
        # celle qu'on quitte vient d'être masquée avec tout ce qu'elle porte.
        self._pill.attacher([self.panel, self.fullscreen, self.grid][idx])

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

    # ── détection zone de déclenchement ──────────────────────────────────────

    def _check_cursor(self) -> None:
        # Plus de garde de premier plan ici. Elle existait parce que la barre
        # était topmost et flottait par-dessus les autres applications ; elle
        # est maintenant ENFANT de la fenêtre affichée et ne peut plus en
        # sortir. C'est cette garde qui, sous gamescope — où `activeWindow()`
        # rend toujours None — l'escamotait définitivement.
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

