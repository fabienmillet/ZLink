"""ZLink — Point d'entrée de l'application."""

from __future__ import annotations

import locale
import logging
import json
import os
import pathlib
import sys

# Ajoute le dossier du projet au PATH pour que libmpv-2.dll soit trouvée
# quelle que soit la façon dont Python est lancé (venv activé ou non).
_PROJECT_DIR = str(pathlib.Path(__file__).resolve().parent)
if _PROJECT_DIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _PROJECT_DIR + os.pathsep + os.environ.get("PATH", "")

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QSurfaceFormat
from PyQt6.QtWidgets import QApplication

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("zlink")

# Avant tout import qui charge libmpv (windows.fullscreen → widgets.mpv_widget) :
# la DLL est du code natif non signé, on contrôle son empreinte.
from core.libmpv_check import verify_libmpv

verify_libmpv()

from core.data_manager import DataManager
from core.mock_injector import MockInjector
from core.models import DisplayMode, WindowRole
from core.monitors import build_layout
from core.selection_store import SelectionStore
from core.stream_manager import StreamManager
from windows.fullscreen import FullscreenWindow
from windows.grid import GridWindow
from windows.panel import PanelWindow
from windows.single import SingleModeShell

def _on_stream_selected(
    login: str,
    fullscreen: FullscreenWindow,
    data_manager: DataManager,
    stream_manager: StreamManager,
) -> None:
    """Callback quand un stream est sélectionné dans une grille."""
    streamer = data_manager.get_streamer(login)
    game = streamer.game if streamer else "Just Chatting"
    viewers = streamer.viewers if streamer else 0
    donation_url = streamer.donation_url if streamer else ""
    fullscreen.set_stream(login, game=game, viewers=viewers, donation_url=donation_url)
    stream_manager.play(login)
    logger.info("Stream switch → %s (%s)", login, game)


def _on_back_to_panel_cb(grid: GridWindow, panel: PanelWindow | None) -> None:
    grid.hide()
    if panel is not None:
        panel.showFullScreen()
        panel.switch_to_tab("Accueil")


def _on_grid_stream_selected_dual_cb(
    _login: str, grid: GridWindow, panel: PanelWindow | None
) -> None:
    # En mode dual, sélectionner un stream dans la grille ne ferme PAS la grille.
    # Le changement de stream fullscreen est déjà géré par le signal stream_selected
    # connecté à _on_stream_selected dans main().
    pass


def _setup_dual_grid(grid: GridWindow, panel: PanelWindow | None) -> None:
    """Configure les callbacks pour le mode DUAL (grid caché, panel principal)."""
    grid.hide()
    if panel is not None:
        panel.set_grid_window(grid)
    grid.back_to_panel.connect(lambda: _on_back_to_panel_cb(grid, panel))
    grid.stream_selected.connect(
        lambda login: _on_grid_stream_selected_dual_cb(login, grid, panel)
    )


def _on_streamers_updated_cb(
    streamers: list[object],
    panel: PanelWindow | None,
    grid: GridWindow | None,
    fullscreen: FullscreenWindow,
    streamer_cache: list[object],
    selection_store: SelectionStore,
) -> None:
    streamer_cache.clear()
    streamer_cache.extend(streamers)
    sel = selection_store.get_selected() or None
    if panel is not None:
        panel.update_streamers(streamers, sel)  # type: ignore[arg-type]
    if grid is not None:
        grid.grid.update_streamers(streamers, sel)
    _refresh_fullscreen_viewers(streamers, fullscreen)


def _on_grid_selection_changed_cb(
    logins: list[str],
    panel: PanelWindow | None,
    grid: GridWindow | None,
    fullscreen: FullscreenWindow,
    streamer_cache: list[object],
    selection_store: SelectionStore,
) -> None:
    selection_store.set_all(logins)
    sel = selection_store.get_selected() or None
    if not logins:
        fullscreen.clear_stream()
    if streamer_cache:
        if grid is not None:
            grid.grid.update_streamers(streamer_cache, sel)


def main() -> int:
    _mock_mode = "--mock" in sys.argv
    _clean_argv = [a for a in sys.argv if a != "--mock"]
    # Obligatoire dès qu'il y a plusieurs QOpenGLWidget dans l'application (les
    # cellules de la grille) et pour QtWebEngine : sans contextes partagés, Qt
    # retombe sur des contextes isolés et chaque widget compose le contenu de
    # toute la fenêtre — d'où des flux imbriqués les uns dans les autres.
    # À poser AVANT la création de QApplication.
    # Le partage de contextes impose un format identique partout : le profil core
    # 3.3 exigé par le rendu libmpv doit donc être le format PAR DÉFAUT de
    # l'application, pas un format posé sur le seul widget vidéo.
    if sys.platform == "darwin":
        _fmt = QSurfaceFormat()
        _fmt.setVersion(3, 3)
        _fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        QSurfaceFormat.setDefaultFormat(_fmt)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)

    app = QApplication(_clean_argv)

    # QApplication applique la locale système (setlocale(LC_ALL, "")). libmpv exige
    # LC_NUMERIC="C" et plante sinon — segfault immédiat avec une locale à virgule
    # décimale (fr_FR, de_DE…). À faire APRÈS la création de QApplication, qui
    # écraserait le réglage, et avant toute instance MPV.
    locale.setlocale(locale.LC_NUMERIC, "C")

    app.setApplicationName("ZLink")
    app.setApplicationDisplayName("ZLink — ZEvent Viewer")

    # Sur Windows, Qt hérite parfois d'une police système en "pixel size" uniquement.
    # Quand il tente de la convertir en point size il obtient -1 et logue un warning.
    # On force un point size explicite dès le départ pour couper ce cascade.
    _app_font = app.font()
    if _app_font.pointSize() <= 0:
        _app_font.setPointSize(10)
        app.setFont(_app_font)

    # --- Détection écrans et assignation des rôles ---
    layout = build_layout(app)
    logger.info("=== ZLink démarré en mode %s ===", layout.mode.name)

    # --- Config ---
    from core.paths import CONFIG_PATH as _cfg_path
    try:
        _startup_config: dict = json.loads(_cfg_path.read_text(encoding="utf-8")) if _cfg_path.exists() else {}
    except Exception:
        _startup_config = {}

    # --- DataManager ---
    data_manager = DataManager()

    # --- StreamManager ---
    stream_manager = StreamManager()

    # --- Création des fenêtres ---
    fs_assignment = layout.get_screen(WindowRole.FULLSCREEN)
    if fs_assignment is None:
        logger.error("Aucun écran assigné pour le fullscreen — abandon")
        return 1

    panel: PanelWindow | None = None
    grid: GridWindow | None = None

    if layout.mode == DisplayMode.SINGLE:
        # ── Mode 1 écran : tout dans une fenêtre unique ──────────────────
        _shell = SingleModeShell(fs_assignment.screen)
        fullscreen = _shell.fullscreen
        panel      = _shell.panel
        grid       = _shell.grid
        fullscreen.set_clip_config(_startup_config)
        grid.grid.set_max_streams(_startup_config.get("max_active_streams", 20))
        panel.stream_selected.connect(
            lambda login: _on_stream_selected(login, fullscreen, data_manager, stream_manager)
        )
        grid.stream_selected.connect(
            lambda login: _on_stream_selected(login, fullscreen, data_manager, stream_manager)
        )
    else:
        # ── Mode 2 ou 3 écrans ───────────────────────────────────────────
        fullscreen = FullscreenWindow(screen=fs_assignment.screen, clip_config=_startup_config.get("clips", {}))

        panel_assignment = layout.get_screen(WindowRole.PANEL)
        if panel_assignment is not None:
            panel = PanelWindow(
                screen=panel_assignment.screen,
                show_grid_tab=layout.mode == DisplayMode.DUAL,
            )
            panel.stream_selected.connect(
                lambda login: _on_stream_selected(login, fullscreen, data_manager, stream_manager)
            )

        grid_assignment = layout.get_screen(WindowRole.GRID)
        if grid_assignment is not None:
            is_dual = layout.mode == DisplayMode.DUAL
            grid = GridWindow(screen=grid_assignment.screen, show_back_button=is_dual)
            grid.grid.set_max_streams(_startup_config.get("max_active_streams", 20))
            grid.stream_selected.connect(
                lambda login: _on_stream_selected(login, fullscreen, data_manager, stream_manager)
            )
            if is_dual:
                _setup_dual_grid(grid, panel)
            else:
                # Mode triple — Echap ferme la fenêtre
                grid.back_to_panel.connect(grid.close)

    # --- SelectionStore ---
    selection_store = SelectionStore()
    streamer_cache: list[object] = []

    # --- Connexions DataManager → fenêtres ---
    data_manager.streamers_updated.connect(
        lambda streamers: _on_streamers_updated_cb(
            streamers, panel, grid, fullscreen, streamer_cache, selection_store
        )
    )
    data_manager.streamers_updated.connect(
        lambda streamers: fullscreen.update_remote_menu(
            streamers, selection_store.get_selected()
        )
    )

    if panel is not None:
        data_manager.global_stats_updated.connect(panel.update_stats)
        data_manager.events_updated.connect(panel.update_events)
        data_manager.history_updated.connect(panel.update_history)
        data_manager.goals_updated.connect(panel.update_goals)
        data_manager.goals_raw_updated.connect(panel.update_goals_cache)
        panel.grid_selection_changed.connect(
            lambda logins: _on_grid_selection_changed_cb(
                logins, panel, grid, fullscreen, streamer_cache, selection_store
            )
        )
        panel.settings_changed.connect(data_manager.reload_config)
        panel.settings_changed.connect(stream_manager.reload_config)
        panel.settings_changed.connect(fullscreen.set_clip_config)
        if grid is not None:
            panel.settings_changed.connect(
                lambda cfg: grid.grid.set_max_streams(cfg.get("max_active_streams", 20))
            )

    # --- Connexions StreamManager → FullscreenWindow ---
    stream_manager.stream_ready.connect(fullscreen.on_stream_ready)
    stream_manager.stream_error.connect(fullscreen.on_stream_error)
    stream_manager.stream_stopped.connect(fullscreen.on_stream_stopped)
    fullscreen.stream_change_requested.connect(
        lambda login: _on_stream_selected(login, fullscreen, data_manager, stream_manager)
    )
    # Bug 3: contour vert dans la grille suit le stream fullscreen
    # Bug 2: relance des streams grille quand la qualité change
    # HypeWatcher : synchronise les cellules surveillées après chaque màj streamers
    if grid is not None:
        fullscreen.stream_changed.connect(grid.grid.set_active_stream)
        stream_manager.grid_quality_changed.connect(grid.grid.restart_all_streams)
        # Qualité adaptative : le nombre de flux joués pilote le palier de qualité
        grid.grid.active_streams_changed.connect(stream_manager.set_active_grid_count)
        grid.grid.set_quality_provider(stream_manager.resolve_grid_quality)
        data_manager.streamers_updated.connect(lambda _: grid.refresh_hype_cells())
        grid.hype_alert.connect(fullscreen.show_hype_alert)
        data_manager.goal_accomplished.connect(grid.grid.goal_achieved_flash)

    # --- Démarrage du polling ---
    if _mock_mode:
        # Mode mock : pas de polling réseau, seulement historique + avatars
        import threading as _threading
        _threading.Thread(target=data_manager._history_worker, daemon=True).start()
        data_manager._start_avatars_fetch()
        _injector = MockInjector(data_manager, parent=app)
        _injector.start()
    else:
        data_manager.start()

    count = 1 + (1 if panel else 0) + (1 if grid else 0)
    logger.info("%d fenêtre(s) créée(s)", count)
    return app.exec()


def _refresh_fullscreen_viewers(
    streamers: list[object], fullscreen: FullscreenWindow
) -> None:
    """Met à jour le compteur viewers du stream fullscreen en cours."""
    login = fullscreen.current_login
    if not login:
        return
    for s in streamers:
        if getattr(s, "twitch_login", None) == login:
            fullscreen.update_viewers(getattr(s, "viewers", 0))
            return


if __name__ == "__main__":
    sys.exit(main())
