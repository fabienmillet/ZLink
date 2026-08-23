"""Fenêtre grille standalone — mode triple écran uniquement."""

from __future__ import annotations

import logging

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QCursor, QFont, QScreen
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from widgets.grid_widget import GridWidget

logger = logging.getLogger(__name__)

GRID_WINDOW_STYLE = """
QMainWindow {
    background-color: #0a0a0a;
}
"""


class GridWindow(QMainWindow):
    """Wrapper fullscreen autour de GridWidget (triple: écran dédié, dual: même écran que panel)."""

    stream_selected = pyqtSignal(str)   # proxy du signal GridWidget
    back_to_panel   = pyqtSignal()      # Echap ou bouton "← Panel"
    hype_alert      = pyqtSignal(str, str, float, str)  # login, label, score, color

    def __init__(
        self,
        screen: QScreen,
        *,
        show_back_button: bool = False,
        show_on_init: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._target_screen = screen
        self._show_back_button = show_back_button
        self.setWindowTitle("ZLink — Grid")
        self.setStyleSheet(GRID_WINDOW_STYLE)

        self._build()
        if show_on_init:
            self._move_to_screen(screen)

    def _build(self) -> None:
        container = QWidget()
        vl = QVBoxLayout(container)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        # Barre retour — visible uniquement en mode dual
        self._back_bar = QWidget()
        self._back_bar.setFixedHeight(30)
        self._back_bar.setStyleSheet("background-color: #111111;")
        bl = QHBoxLayout(self._back_bar)
        bl.setContentsMargins(8, 0, 8, 0)
        bl.setSpacing(0)
        back_btn = QPushButton("← Panel")
        back_btn.setFont(QFont("Consolas", 10))
        back_btn.setStyleSheet(
            "QPushButton { color: #888888; background: transparent; border: none; }"
            "QPushButton:hover { color: #ffffff; }"
        )
        back_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        back_btn.clicked.connect(self.back_to_panel)
        bl.addWidget(back_btn)
        bl.addStretch()
        self._back_bar.setVisible(self._show_back_button)
        vl.addWidget(self._back_bar)

        self.grid = GridWidget()
        self.grid.stream_selected.connect(self.stream_selected)
        vl.addWidget(self.grid, stretch=1)

        self.setCentralWidget(container)
        self._start_hype_watcher()

    def _move_to_screen(self, screen: QScreen) -> None:
        g = screen.geometry()
        self.setGeometry(g)
        self.show()  # crée le handle natif à la bonne position
        handle = self.windowHandle()
        if handle is not None:
            handle.setScreen(screen)
        self.showFullScreen()
        logger.info("Grid ouverte sur %s (%dx%d)", screen.name(), g.width(), g.height())

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() == Qt.Key.Key_Escape:
            self.back_to_panel.emit()
        else:
            super().keyPressEvent(event)

    # ── HypeWatcher ————————————————————————————————————————————————

    def _start_hype_watcher(self) -> None:
        """Démarre le HypeWatcher (appelé en fin de _build)."""
        try:
            from core.hype_watcher import HypeWatcher
            import json
            from pathlib import Path

            cfg: dict = {}
            from core.paths import CONFIG_PATH as p
            if p.exists():
                cfg = json.loads(p.read_text(encoding="utf-8"))

            self._hype_watcher = HypeWatcher(cfg)
            self._hype_watcher.alert_triggered.connect(self._on_hype_alert)
            self._hype_watcher.start()
            logger.info("HypeWatcher démarré")
        except Exception as exc:
            logger.warning("HypeWatcher: impossible de démarrer — %s", exc)
            self._hype_watcher = None

    def refresh_hype_cells(self) -> None:
        """Synchronise les cellules surveillées avec l'état courant de la grille.

        Appelé depuis le main thread après chaque mise à jour des streamers.
        """
        watcher = getattr(self, "_hype_watcher", None)
        if watcher is None:
            return
        infos: list[tuple[int, str, object | None]] = []
        for idx, cell in enumerate(self.grid._cells):
            if cell.twitch_login and cell.is_online:
                # On passe le MpvWidget (et non mpv.MPV) pour accéder à get_audio_rms_db()
                mpv_widget = cell._mpv if cell._mpv is not None else None
                infos.append((idx, cell.twitch_login, mpv_widget))
        watcher.update_cells(infos)

    def _on_hype_alert(self, cell_idx: int, packed: str, score: float) -> None:
        """Reçoit une alerte du HypeWatcher et pulse la cellule + affiche le toast."""
        color, label = (
            packed.split("|", 1) if "|" in packed else ("#ff6b00", packed)
        )
        cells = self.grid._cells
        login = cells[cell_idx].twitch_login if 0 <= cell_idx < len(cells) else ""
        if 0 <= cell_idx < len(cells):
            cells[cell_idx].pulse_hype(color)
        self.grid.show_hype_toast(cell_idx, label, score, color)
        self.hype_alert.emit(login, label, score, color)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        watcher = getattr(self, "_hype_watcher", None)
        if watcher is not None:
            watcher.stop()
        super().closeEvent(event)
