# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
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

# mpv n'implémente --wid que sur X11, win32 et Android : en session Wayland
# native, la vidéo s'ouvrirait dans des fenêtres séparées du reste de l'UI. On
# bascule donc Qt sur XWayland. À poser AVANT le premier import PyQt6 : Qt lit
# la variable à la construction de QApplication.
# QT_QPA_PLATFORM est souvent une LISTE de repli posée par la distribution
# ("wayland;xcb") : ce n'est pas un choix explicite de l'utilisateur, et Qt en
# retiendrait la première entrée, donc Wayland. On ne respecte le réglage que
# s'il ne mène pas à Wayland (xcb, offscreen, minimal… restent intacts).
# On ne force rien non plus si aucun serveur X n'est joignable : sans XWayland,
# xcb échouerait à se connecter et l'application ne démarrerait pas du tout.
_qt_platform = os.environ.get("QT_QPA_PLATFORM", "")
if (
    sys.platform.startswith("linux")
    and os.environ.get("XDG_SESSION_TYPE") == "wayland"
    and os.environ.get("DISPLAY")
    and (not _qt_platform or _qt_platform.split(";")[0].strip().startswith("wayland"))
):
    os.environ["QT_QPA_PLATFORM"] = "xcb"

from PyQt6.QtCore import Qt, QSize, QTimer
from PyQt6.QtGui import QFont, QIcon, QSurfaceFormat
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
from core.paths import RESOURCE_ROOT
from core.version import display_version as _display_version

verify_libmpv()

from core.data_manager import DataManager
from core.mock_injector import MockInjector
from core.models import DisplayMode, WindowRole
from core.monitors import build_layout
from core.selection_store import SelectionStore
from core.sous_processus import interdire_les_consoles
from core.win_fullscreen import mark_fullscreen
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
        mark_fullscreen(panel)
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
        grid.set_streamers(streamers)
    fullscreen.set_streamers(streamers)
    _refresh_fullscreen_viewers(streamers, fullscreen)


def _on_grid_selection_changed_cb(
    logins: list[str],
    grid: GridWindow | None,
    fullscreen: FullscreenWindow,
    streamer_cache: list[object],
    selection_store: SelectionStore,
) -> None:
    selection_store.set_all(logins)
    sel = selection_store.get_selected() or None
    if not logins:
        fullscreen.clear_stream()
    if streamer_cache and grid is not None:
        grid.grid.update_streamers(streamer_cache, sel)


def _icone_application() -> QIcon:
    """Icône de l'application, dans toutes les tailles disponibles.

    Charger les PNG plutôt que le SVG : Qt choisit alors la définition exacte
    demandée par la barre des tâches au lieu de rééchantillonner, et le rendu
    à 16 pixels reste net. Le SVG reste la source, voir scripts/gen_icons.py.
    """
    icone = QIcon()
    dossier = RESOURCE_ROOT / "assets" / "icons"
    for taille in (16, 24, 32, 48, 64, 128, 256, 512, 1024):
        chemin = dossier / f"zlink-{taille}.png"
        if chemin.is_file():
            icone.addFile(str(chemin), QSize(taille, taille))
    if icone.isNull():
        # Paquet incomplet : une icône générique vaut mieux qu'un plantage.
        logger.warning("icônes absentes de %s", dossier)
    return icone


def _configurer_contextes_opengl() -> None:
    """Format OpenGL par défaut et partage des contextes.

    À appeler AVANT la création de QApplication.
    """
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


def _declarer_identite_windows() -> None:
    """Identité de l'application auprès du shell Windows.

    À appeler AVANT la création de la moindre fenêtre. Sans effet ailleurs.
    """
    # Windows regroupe les boutons de la barre des tâches par AppUserModelID, et
    # en tire l'icône affichée. Lancé par « python main.py », le processus hérite
    # de celui de python.exe : la barre des tâches montrait donc le logo Python,
    # setWindowIcon ou pas. On déclare une identité propre à ZLink, ce qui vaut
    # aussi pour l'exécutable empaqueté (épinglage et regroupement cohérents).
    # À faire AVANT la création de la moindre fenêtre.
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("dev.zlink.ZLink")
        except (AttributeError, OSError) as exc:  # API absente : sans gravité
            logger.debug("AppUserModelID non posé : %s", exc)


def _installer_arret_propre(app: QApplication, x11_guard) -> QTimer:
    """Branche la sortie propre et les signaux système.

    Renvoie le QTimer qui rend la main à l'interpréteur : l'appelant DOIT
    en garder une référence, sans quoi il est ramassé et les signaux
    redeviennent muets pendant app.exec().
    """
    # On ne démonte PAS les lecteurs mpv pour quitter, et c'est délibéré.
    #
    # Chaque lecteur tient sa propre connexion X et y présente ses images depuis
    # ses threads. mpv_terminate_destroy() détruit la fenêtre alors qu'une
    # requête Present peut encore être en vol : le serveur répond BadWindow ou
    # BadPixmap, Xlib appelle son gestionnaire par défaut, qui fait exit() —
    # depuis un thread de rendu. Avec dix-huit lecteurs arrêtés de front, autant
    # de sorties de processus concurrentes déroulaient le tas en même temps et
    # glibc abandonnait sur « corrupted double-linked list », laissant
    # l'application figée que ni le Ctrl+C ni le chien de garde ne rattrapaient.
    #
    # Ce démontage n'apporte rien ici : le processus se termine juste après. Le
    # serveur X libère fenêtres et pixmaps à la fermeture des connexions, et
    # rien n'est en attente d'écriture (config et cache d'avatars sont écrits de
    # façon atomique et synchrone, au fil de l'eau). MpvWidget.shutdown() reste
    # employé quand un lecteur donné disparaît en cours de session — un seul
    # lecteur, sans course avec dix-sept autres.
    from widgets.bigscreen_widget import shutdown_avatar_pool as _shutdown_avatars
    app.aboutToQuit.connect(_shutdown_avatars)

    # Ctrl+C : pendant app.exec() la boucle est tenue par du code C++ et Python
    # n'exécute aucun bytecode, donc son gestionnaire de signal ne se déclenche
    # jamais — le SIGINT était purement et simplement avalé. Le QTimer rend la
    # main à l'interpréteur régulièrement pour que le handler puisse partir.
    import signal as _signal

    _quit_asked = {"n": 0}

    def _on_signal(_sig, _frm) -> None:
        _quit_asked["n"] += 1
        if _quit_asked["n"] == 1:
            logger.info("Signal reçu — arrêt de ZLink")
            # mpv restaure le gestionnaire d'erreur Xlib PAR DÉFAUT quand il
            # démonte un lecteur, et celui-là termine le processus. On reprend
            # la main avant la phase d'arrêt.
            x11_guard.install()
            # Chien de garde : sous xcb, terminer 25 lecteurs mpv peut traîner
            # et l'application paraissait alors ignorer le Ctrl+C. Quoi qu'il
            # arrive, on sort.
            import threading as _th
            _wd = _th.Timer(4.0, lambda: os._exit(130))
            _wd.daemon = True
            _wd.start()
            app.quit()
            return
        # Deuxième Ctrl+C : l'arrêt propre traîne (téléchargement en cours,
        # sous-processus streamlink…). On sort sans attendre.
        logger.warning("Second signal — arrêt immédiat")
        os._exit(130)

    for _sig in (_signal.SIGINT, _signal.SIGTERM):
        try:
            _signal.signal(_sig, _on_signal)
        except (ValueError, OSError):
            pass  # pas le thread principal, ou signal indisponible
    _sig_timer = QTimer()
    _sig_timer.setInterval(200)
    _sig_timer.timeout.connect(lambda: None)
    _sig_timer.start()
    return _sig_timer


def _installer_polices_de_repli() -> None:
    """Chaînes de substitution pour les polices absentes hors Windows."""
    # L'UI est dessinée avec "Segoe UI Variable" et "Cascadia Code", absentes
    # hors Windows : Qt y substituerait une police par défaut arbitraire, d'où
    # une typographie incohérente sur Linux et macOS. On déclare des chaînes de
    # repli explicites ; la substitution s'applique aussi bien aux QFont() qu'aux
    # font-family des feuilles de style, donc à tous les widgets sans les toucher.
    if sys.platform != "win32":
        QFont.insertSubstitutions("Segoe UI Variable", [
            "Segoe UI", "Inter", "SF Pro Text", "Cantarell",
            "Noto Sans", "DejaVu Sans",
        ])
        # Consolas sert aux placeholders d'avatar et au compteur de cagnotte.
        QFont.insertSubstitutions("Consolas", [
            "Cascadia Mono", "SF Mono", "JetBrains Mono", "Fira Code",
            "Source Code Pro", "Noto Sans Mono", "Liberation Mono",
        ])
        QFont.insertSubstitutions("Cascadia Code", [
            "Cascadia Mono", "SF Mono", "JetBrains Mono", "Fira Code",
            "Source Code Pro", "Noto Sans Mono", "Liberation Mono",
        ])


def _creer_fenetres(
    layout, fs_assignment, _startup_config, data_manager, stream_manager,
) -> tuple[FullscreenWindow, PanelWindow | None, GridWindow | None,
           SingleModeShell | None]:
    """Instancie les fenêtres selon le mode d'affichage détecté.

    Le coordinateur du mode 1 écran fait partie du retour, et ce n'est pas un
    détail : il détient la barre de navigation et le minuteur qui la révèle,
    et rien d'autre ne les référence. En variable locale, il était ramassé
    dès la sortie de cette fonction — les trois fenêtres survivaient, étant
    rendues, mais la barre disparaissait avec lui et ne pouvait plus jamais
    s'afficher.
    """
    panel: PanelWindow | None = None
    grid: GridWindow | None = None
    shell: SingleModeShell | None = None

    if layout.mode == DisplayMode.SINGLE:
        # ── Mode 1 écran : tout dans une fenêtre unique ──────────────────
        shell = SingleModeShell(fs_assignment.screen)
        fullscreen = shell.fullscreen
        panel      = shell.panel
        grid       = shell.grid
        fullscreen.set_clip_config(_startup_config)
        grid.grid.set_max_streams(_startup_config.get("max_active_streams", 20))
        grid.grid.set_sort_mode(_startup_config.get("grid_sort", "viewers"))
        grid.set_clip_config(_startup_config)
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
            grid.grid.set_sort_mode(_startup_config.get("grid_sort", "viewers"))
            grid.set_clip_config(_startup_config)
            grid.stream_selected.connect(
                lambda login: _on_stream_selected(login, fullscreen, data_manager, stream_manager)
            )
            if is_dual:
                _setup_dual_grid(grid, panel)
            else:
                # Mode triple — Echap ferme la fenêtre
                grid.back_to_panel.connect(grid.close)
    return fullscreen, panel, grid, shell


def _brancher_panel(
    panel, grid, fullscreen, data_manager, stream_manager,
    selection_store, streamer_cache,
) -> None:
    """Câble le panel aux données, aux réglages et au plein écran."""
    if panel is None:
        return
    # Importés ici comme avant le découpage : ces modules se configurent
    # au démarrage et sont rebranchés à chaque enregistrement des réglages.
    from core import alerts as _alerts
    from core import sounds as _sounds

    data_manager.global_stats_updated.connect(panel.update_stats)
    data_manager.events_updated.connect(panel.update_events)
    data_manager.history_updated.connect(panel.update_history)
    data_manager.goals_updated.connect(panel.update_goals)
    data_manager.goals_raw_updated.connect(panel.update_goals_cache)
    panel.grid_selection_changed.connect(
        lambda logins: _on_grid_selection_changed_cb(
            logins, grid, fullscreen, streamer_cache, selection_store
        )
    )
    panel.settings_changed.connect(data_manager.reload_config)
    panel.settings_changed.connect(stream_manager.reload_config)
    panel.settings_changed.connect(fullscreen.set_clip_config)
    panel.settings_changed.connect(_alerts.configure)
    panel.settings_changed.connect(_sounds.configure)
    # Un show du programme démarre : proposer d'y aller, sans imposer.
    panel.show_started.connect(fullscreen.show_show_started)
    # Le plein écran est la source principale de la console de mixage.
    panel.main_volume_changed.connect(fullscreen.set_volume)
    panel.main_mute_changed.connect(fullscreen.set_muted)
    fullscreen.stream_changed.connect(panel.set_main_stream)
    # Retour de la console : le curseur du mixer suit le son réel du direct,
    # y compris quand il est réglé au clavier depuis le plein écran.
    fullscreen.volume_changed.connect(panel.set_main_volume)
    fullscreen.mute_changed.connect(panel.set_main_muted)
    panel.set_main_stream(fullscreen.current_login)
    panel.action_requested.connect(fullscreen.run_action)
    # La palette existe aussi en plein écran et dans la grille : elles ne
    # tiennent pas la sélection, le panel si.
    fullscreen.grid_add_requested.connect(panel.ajouter_a_la_grille)
    if grid is not None:
        grid.grid_add_requested.connect(panel.ajouter_a_la_grille)
        grid.action_requested.connect(fullscreen.run_action)
    if grid is not None:
        panel.settings_changed.connect(
            lambda cfg: (
                grid.grid.set_max_streams(cfg.get("max_active_streams", 20)),
                grid.grid.set_sort_mode(cfg.get("grid_sort", "viewers")),
                grid.set_clip_config(cfg),
            )
        )


def _brancher_grille_streams(
    grid, panel, fullscreen, data_manager, stream_manager,
) -> None:
    """Flux, qualité adaptative, épinglages audio et replay de la grille."""
    if grid is None:
        return
    # Bug 3: contour vert dans la grille suit le stream fullscreen
    # Bug 2: relance des streams grille quand la qualité change
    # HypeWatcher : synchronise les cellules surveillées après chaque màj streamers
    fullscreen.stream_changed.connect(grid.grid.set_active_stream)
    stream_manager.grid_quality_changed.connect(grid.grid.restart_all_streams)
    # Qualité adaptative : le nombre de flux joués pilote le palier de qualité
    grid.grid.active_streams_changed.connect(stream_manager.set_active_grid_count)
    grid.grid.set_quality_provider(stream_manager.resolve_grid_quality)
    data_manager.streamers_updated.connect(lambda _: grid.refresh_hype_cells())
    grid.hype_alert.connect(fullscreen.show_hype_alert)
    # L'objectif accompli s'annonce LÀ OÙ ON REGARDE, c'est-à-dire le direct.
    # Le toast de la grille recouvrait une cellule — donc un flux — dans la
    # seule fenêtre entièrement faite de vidéo, et la cellule concernée n'y
    # est même pas forcément affichée. La cellule, elle, réagit toujours par
    # son liseré : c'est ce qui désigne QUI, sans rien masquer.
    data_manager.goal_accomplished.connect(
        lambda login, nom, f=fullscreen: f.annoncer(
            f"✓  {login} — objectif accompli : {nom}", "#00ff87", 5.0)
    )
    data_manager.goal_accomplished.connect(grid.grid.goal_achieved_flash)
    # Un palier n'appartient à aucun streamer : toute la grille réagit.
    data_manager.milestone_reached.connect(
        lambda _amount, _label, g=grid: g.grid.pulse_all()
    )
    # Un afflux de dons concerne UNE chaîne : sa cellule seule réagit.
    data_manager.big_donation.connect(
        # Un bombardement dure : sa cellule clignote plus longtemps, le
        # temps qu'on ait la chance de la voir.
        lambda login, _d, _a, nature, g=grid: g.grid.pulse_cell(
            login, "#f5c518", 10.0 if nature == "bombardement" else 6.0)
    )
    # L'audio de la grille vient de cellules qu'on ne regarde pas : le plein
    # écran affiche qui l'on entend.
    grid.grid.audio_pins_changed.connect(fullscreen.set_pinned_audio)
    if panel is not None:
        # La console de mixage ne pilote QUE les chaînes épinglées.
        grid.grid.audio_pins_changed.connect(panel.set_pinned_audio)
        panel.cell_volume_changed.connect(grid.grid.set_cell_volume)
        panel.unpin_requested.connect(grid.grid.unpin_audio)
        panel.cell_mute_changed.connect(grid.grid.set_cell_muted)
        # La liste du plein écran doit dire ce qu'on entend VRAIMENT.
        panel.cell_mute_changed.connect(fullscreen.set_pinned_muted)
    # Le moment s'est produit sur une cellule, mais c'est le plein écran
    # qu'on regarde : c'est là que le replay doit s'afficher.
    grid.grid.replay_requested.connect(fullscreen.start_replay)


def _brancher_raids(grid, panel, fullscreen, data_manager) -> None:
    """Annonce des raids entre participants du ZEvent."""
    if grid is None:
        return
    def _raid_zevent(source: str, cible: str, viewers: int) -> None:
        """N'annoncer qu'un raid ENTRE participants du ZEvent.

        N'importe quelle chaîne peut raider un participant : un ami, un
        petit streamer de passage. Ces raids-là n'ont rien à voir avec
        l'événement — d'où les annonces à quatre spectateurs. On exige donc
        que la source figure dans la liste des participants.
        """
        if source.lower() not in data_manager.participant_logins():
            logger.debug("Raid ignoré : %s n'est pas un participant", source)
            return
        fullscreen.show_raid(source, cible, viewers)
        if panel is not None:
            panel.add_feed_event(
                "event", cible,
                f"{source} raide {cible}"
                + (f" avec {viewers:,} spectateurs".replace(",", "\u202f")
                   if viewers else ""))

    grid.raid_detected.connect(_raid_zevent)


def _brancher_top_audiences(grid, panel, fullscreen, data_manager) -> None:
    """Annonce l'entrée dans le top des audiences."""
    if grid is None:
        return
    def _top_entree(login, display, viewers, rang, g=grid) -> None:
        """N'annoncer que ce qu'on ne voit pas déjà.

        Signaler l'entrée dans le top d'une chaîne déjà à l'écran
        n'apprendrait rien : on la regarde.
        """
        affichees = {c.twitch_login for c in g.grid._cells if c.twitch_login}
        if login in affichees or login == fullscreen.current_login:
            return
        fullscreen.show_top_entry(login, display, viewers, rang)
        if panel is not None:
            panel.add_feed_event(
                "live", login,
                f"{display} entre dans le top {rang} des audiences "
                + f"({viewers:,} viewers)".replace(",", "\u202f"))

    data_manager.top_stream_entered.connect(_top_entree)


#: La télécommande, maintenue en vie pour la durée de la session. Un objet Qt
#: sans référence Python est ramassé, et son serveur se ferme avec lui.
_TELECOMMANDE: list = []


def _logins_a_dater(grid, fullscreen, selection_store, streamers) -> list[str]:
    """Les chaînes dont on veut savoir depuis quand elles émettent.

    Celles qu'on regarde d'abord — plein écran, grille, sélection — puis
    TOUTES celles en direct : le classement des stats en montre trois cents,
    et une colonne « Depuis » trouée sur les trois quarts des lignes passe
    pour cassée plutôt que pour économe.

    Le coût reste modeste : une soixantaine de chaînes en direct un soir
    d'event, vingt-cinq par requête, et un relevé vaut cinq minutes.
    """
    logins = [fullscreen.current_login] if fullscreen.current_login else []
    logins += list(selection_store.get_selected() or [])
    if grid is not None:
        logins += [c.twitch_login for c in grid.grid._cells if c.twitch_login]
    logins += [s.twitch_login for s in (streamers or [])
               if getattr(s, "online", False) and s.twitch_login]
    return logins


def _brancher_domotique(grid, data_manager) -> None:
    """Fait sortir vers Home Assistant ce que ZLink repère déjà.

    Aucune détection n'est ajoutée : on se branche sur les quatre signaux
    existants. Sans URL configurée, `annonce` rend False sans rien envoyer —
    inutile de tester ici ce que le module sait déjà refuser.
    """
    from core import domotique

    data_manager.milestone_reached.connect(
        lambda montant, libelle: domotique.annonce(
            "palier", {"montant": float(montant), "libelle": str(libelle)}))
    data_manager.big_donation.connect(
        lambda login, display, montant, nature: domotique.annonce(
            "don", {"login": login, "streamer": display,
                    "montant": float(montant), "nature": nature}))
    data_manager.goal_imminent.connect(
        lambda login, display, objectif, reste, _url: domotique.annonce(
            "objectif", {"login": login, "streamer": display,
                         "objectif": objectif, "reste": float(reste)}))
    if grid is not None:
        grid.hype_alert.connect(
            lambda login, libelle, score, couleur, extrait: domotique.annonce(
                "hype", {"login": login, "libelle": libelle,
                         "score": round(float(score), 3),
                         "couleur": couleur, "extrait": extrait}))


def _brancher_telecommande(grid, panel, fullscreen, data_manager):
    """Ouvre la télécommande locale et la relie à ce qui existe déjà.

    Elle n'apporte aucun geste nouveau : elle rejoue ceux du clavier et de la
    console de mixage, depuis un boîtier posé sur le bureau. Rend l'objet, ou
    None si l'écoute a échoué — auquel cas ZLink tourne sans, exactement comme
    avant.

    L'état est publié à chaque changement qui se voit sur les touches : la
    liste des cellules, la chaîne affichée en grand. Rien n'est envoyé en
    boucle — une touche qui ne change pas n'a pas besoin d'être redessinée.
    """
    from core.remote_api import RemoteAPI

    telecommande = RemoteAPI()
    if not telecommande.demarrer():
        return None

    telecommande.slot_demande.connect(fullscreen.slot_requested)
    telecommande.voisin_demande.connect(fullscreen.neighbour_requested)
    telecommande.action_demandee.connect(fullscreen.run_action)
    telecommande.chaine_demandee.connect(fullscreen.stream_change_requested)
    telecommande.volume_demande.connect(fullscreen.set_volume)
    telecommande.muet_demande.connect(fullscreen.set_muted)
    # Par la console de mixage quand elle existe, et seulement à défaut par la
    # grille : la console tient le niveau de chaque tranche, et c'est ce niveau
    # que la télécommande relit avant chaque cran. La court-circuiter laissait
    # la molette repartir du même point indéfiniment.
    if panel is not None:
        telecommande.volume_chaine_demande.connect(panel.regler_mixage)
        telecommande.muet_chaine_demande.connect(panel.couper_mixage)
    elif grid is not None:
        telecommande.volume_chaine_demande.connect(grid.grid.set_cell_volume)
        telecommande.muet_chaine_demande.connect(grid.grid.set_cell_muted)

    def _publier(*_args) -> None:
        # Une lecture d'etat qui echoue ne doit pas emporter le signal qui l'a
        # declenchee : c'est la mise a jour des streamers, dont depend toute
        # l'interface. La telecommande garde alors l'etat precedent.
        try:
            etat = _etat_pour_telecommande(grid, panel, fullscreen)
        except Exception:                                     # noqa: BLE001
            logger.exception("Telecommande : etat illisible, publication sautee")
            return
        telecommande.publier_etat(etat)

    data_manager.streamers_updated.connect(_publier)
    fullscreen.stream_changed.connect(_publier)
    fullscreen.volume_changed.connect(_publier)
    fullscreen.etat_bascule.connect(_publier)
    if grid is not None:
        grid.grid.audio_pins_changed.connect(_publier)
    if panel is not None:
        # Une tranche qui bouge change ce qu'affiche une molette : sans cela,
        # la télécommande garderait l'ancien niveau et repartirait de lui.
        panel.cell_volume_changed.connect(_publier)
        panel.cell_mute_changed.connect(_publier)
        panel.main_volume_changed.connect(_publier)
        # L'étoile posée depuis le panel : la touche « Favori » du boîtier
        # l'affiche, elle doit donc l'apprendre.
        panel.favori_change.connect(_publier)
        # Et l'inverse : posée depuis le plein écran, le clavier ou le
        # boîtier, elle doit apparaître sur la carte du panel. Sans ce
        # second sens, les deux moitiés affichaient l'inverse l'une de
        # l'autre selon l'endroit où l'on avait cliqué.
        fullscreen.favori_change.connect(
            lambda login, _favori: panel.rafraichir_favori(login))
    _publier()
    return telecommande


def _etat_pour_telecommande(grid, panel, fullscreen) -> dict:
    """Ce qu'une touche a besoin de savoir, et rien de plus.

    L'AVATAR est envoyé comme une URL, jamais comme une image : le plugin la
    télécharge une fois et garde le résultat. Réémettre quelques dizaines de
    kilo-octets par chaîne à chaque changement d'audience — toutes les trente
    secondes — n'apporterait rien.
    """
    niveaux = panel.niveaux_de_mixage() if panel is not None else {}
    cellules = []
    if grid is not None:
        avatars = {s.twitch_login: getattr(s, "profile_url", "")
                   for s in (grid.grid._last_streamers or [])}
        vues = [c for c in grid.grid._cells if c.twitch_login]
        for cellule in grid.grid._ordered_for_display(vues):
            login = cellule.twitch_login
            volume, muet = niveaux.get(login, (100, False))
            cellules.append({
                "login": login,
                "viewers": int(getattr(cellule, "_viewers", 0) or 0),
                "online": bool(cellule.is_online),
                "epingle": bool(getattr(cellule, "_audio_pinned", False)),
                "avatar": avatars.get(login, ""),
                "volume": volume,
                "muet": muet,
            })
    return {
        "actif": fullscreen.current_login,
        "volume": int(getattr(fullscreen, "_volume", 0) or 0),
        "muet": bool(getattr(fullscreen, "_muted", False)),
        # Deux états BASCULABLES : une touche « Chat » ou « Favori » doit
        # montrer si elle est engagée, sinon elle ne dit que ce qu'elle fait,
        # jamais où l'on en est.
        "chat": bool(getattr(fullscreen, "chat_ouvert", False)),
        "favori": bool(getattr(fullscreen, "favori_courant", False)),
        "cellules": cellules,
    }


def _brancher_raccourcis_grille(grid, fullscreen) -> None:
    """Touches du plein écran qui dépendent de l'ordre de la grille."""
    if grid is None:
        return
    # Raccourcis clavier du plein écran qui portent sur la grille : elle
    # seule connaît l'ordre d'affichage courant.
    def _cellule(n: int, g=grid) -> str:
        cells = [c for c in g.grid._cells if c.twitch_login and c.is_online]
        cells = g.grid._ordered_for_display(cells)
        return cells[n].twitch_login if 0 <= n < len(cells) else ""

    def _aller_a(n: int) -> None:
        login = _cellule(n)
        if login:
            fullscreen.stream_change_requested.emit(login)

    def _voisin(pas: int, g=grid) -> None:
        cells = [c for c in g.grid._cells if c.twitch_login and c.is_online]
        cells = g.grid._ordered_for_display(cells)
        logins = [c.twitch_login for c in cells]
        if not logins:
            return
        courant = fullscreen.current_login
        i = logins.index(courant) if courant in logins else -1
        fullscreen.stream_change_requested.emit(
            logins[(i + pas) % len(logins)])

    fullscreen.slot_requested.connect(_aller_a)
    fullscreen.neighbour_requested.connect(_voisin)


def _brancher_fil_evenements(panel, grid, data_manager) -> None:
    """Archive les alertes dans le fil d'événements de l'Accueil.

    Les alertes n'existaient qu'en toast éphémère : le fil leur donne une
    trace consultable après coup.
    """
    if grid is None or panel is None:
        return
    # Les alertes n'existaient qu'en toast éphémère : on les archive
    # aussi dans le fil d'événements de l'Accueil.
    grid.hype_alert.connect(
        lambda login, label, _score, _color, excerpt, p=panel:
            p.add_feed_event(
                "hype", login,
                # Le libellé seul (« Bravo », « Moment fort ») ne dit
                # pas ce qui s'est passé : on joint le message du chat
                # qui a déclenché l'alerte.
                f"{login} — {label}" + (f" · « {excerpt} »" if excerpt else ""),
            )
    )
    data_manager.goal_accomplished.connect(
        lambda login, goal, p=panel:
            p.add_feed_event("goal", login, f"{login} — objectif atteint : {goal}")
    )
    data_manager.favorite_live.connect(
        lambda login, display, p=panel:
            p.add_feed_event("live", login,
                             f"{display or login} vient de passer en direct")
    )
    data_manager.goal_imminent.connect(
        # goal_imminent émet CINQ arguments : l'URL de don ferme la liste.
        # Sans ce paramètre, PyQt la plaçait dans `p` — le fil recevait une
        # chaîne au lieu du panel, et l'alerte n'arrivait jamais.
        lambda login, display, goal, reste, _url, p=panel:
            p.add_feed_event(
                "goal", login,
                f"{display} est à {reste:,.0f} € de son objectif "
                .replace(",", "\u202f") + f"« {goal} »")
    )
    data_manager.big_donation.connect(
        lambda login, display, amount, nature, p=panel:
            p.add_feed_event(
                "money", login,
                (f"Bombardement de dons sur {display} — "
                 if nature == "bombardement"
                 else f"{display} vient de recevoir ")
                + f"{amount:,.0f} €".replace(",", "\u202f"))
    )
    data_manager.milestone_reached.connect(
        lambda amount, label, p=panel:
            p.add_feed_event("money", "",
                             f"La cagnotte vient de dépasser {label}")
    )
    data_manager.programme_added.connect(
        lambda name, when, p=panel:
            p.add_feed_event("event", "",
                             f"Nouveau au programme : {name}"
                             + (f" — {when}" if when else ""))
    )


def _brancher_surveillance_ressources(app, fullscreen, panel):
    """Prévient quand le poste sature et que ZLink en est la cause.

    Le conseil — baisser le nombre de flux — n'a de sens que si ZLink pèse
    vraiment dans la saturation : un export vidéo lancé à côté ne se corrige
    pas en fermant des cellules. Les deux conditions sont vérifiées dans
    core/resource_watch.
    """
    from core import alerts as _alerts
    from core.resource_watch import ResourceWatch, message_conseil

    if not _alerts.enabled("ressources"):
        return None

    veille = ResourceWatch(app)

    def _sur_saturation(ressource: str, total: float, part: float) -> None:
        fullscreen.show_resource_alert(ressource, total, part)
        if panel is not None:
            panel.add_feed_event("event", "",
                                 message_conseil(ressource, total, part))

    veille.saturation.connect(_sur_saturation)
    app.aboutToQuit.connect(veille.stop)
    veille.start()
    return veille


def _brancher_journal_session(app, data_manager, fullscreen, grid) -> None:
    """Journal de session et récapitulatif de fin."""
    from core import sounds as _sounds

    # Journal de session : tout est signalé quelque part au moment où ça arrive,
    # mais rien n'y survit. Voir core/session_log.
    data_manager.milestone_reached.connect(
        lambda _a, _l: _sounds.play("milestone"))
    data_manager.goal_accomplished.connect(
        lambda _lg, _g: _sounds.play("goal"))

    from core.session_log import SESSION as _SESSION
    fullscreen.stream_changed.connect(_SESSION.set_current_stream)
    data_manager.goal_accomplished.connect(_SESSION.add_goal)
    data_manager.milestone_reached.connect(
        lambda _amount, label: _SESSION.add_milestone(label))
    data_manager.global_stats_updated.connect(
        lambda st: _SESSION.observe_stats(
            getattr(st, "donation_total", 0.0), getattr(st, "viewers_total", 0)))
    if grid is not None:
        grid.hype_alert.connect(
            lambda login, label, score, _c, _e: _SESSION.add_hype(login, label, score))
        grid.grid.clip_saved.connect(_SESSION.add_clip)

    # Le récapitulatif est écrit à la fermeture : os._exit court-circuite tout
    # ce qui suit la boucle Qt, aboutToQuit est le dernier moment utile.
    from windows.recap import save_summary as _save_recap
    app.aboutToQuit.connect(lambda: _save_recap())


def _demarrer_donnees(app, data_manager, _mock_mode: bool) -> None:
    """Lance le polling réel, ou l'injecteur de données simulées."""
    # --- Démarrage du polling ---
    if _mock_mode:
        # Mode mock : pas de polling réseau, seulement historique + avatars
        import threading as _threading
        _threading.Thread(target=data_manager._history_worker, daemon=True).start()
        _injector = MockInjector(data_manager, parent=app)
        # MockInjector émet streamers_updated directement, sans repasser par le
        # chemin DataManager qui précharge les avatars : on le déclenche ici.
        data_manager._prefetch_avatars(_injector.streamers)
        _injector.start()
    else:
        data_manager.start()


def _brancher_updater(panel) -> None:
    """Vérification de mise à jour — notification seulement."""
    if panel is None:
        return
    # --- Vérification de mise à jour (notification seulement) ---
    from core.updater import UpdateChecker

    _updater = UpdateChecker()

    def _on_update(version: str, url: str, p=panel) -> None:
        p.add_feed_event(
            "event", "",
            f"ZLink {version} est disponible — ouvrir la page de release",
        )
        # Le fil d'événements défile ; le badge de l'en-tête, lui, reste.
        p.set_update_available(version, url)
        logger.info("Mise à jour disponible : %s — %s", version, url)

    _updater.update_available.connect(_on_update)
    # Après le démarrage : ne pas retarder l'affichage pour une requête réseau.
    QTimer.singleShot(5000, _updater.check)


def main() -> int:
    _mock_mode = "--mock" in sys.argv
    # --setup rejoue l'assistant de première configuration, même déjà passé.
    _force_setup = "--setup" in sys.argv
    _clean_argv = [a for a in sys.argv if a not in ("--mock", "--setup")]
    _configurer_contextes_opengl()

    _declarer_identite_windows()

    app = QApplication(_clean_argv)

    # Avant tout lecteur mpv : une erreur Xlib non gérée termine le processus
    # depuis le thread qui l'a provoquée. Voir core/x11_guard.
    from core import x11_guard
    # Réinstallée en continu : mpv reprend le gestionnaire quand son affichage
    # démarre, et nos poses ponctuelles laissaient des trous pendant lesquels
    # une erreur X terminait le processus.
    # Le QTimer a  pour parent : Qt le maintient en vie.
    x11_guard.start_watchdog(app, 1000)

    # Menus, infobulles et listes déroulantes ne sont pas peints par nous : ils
    # suivent la palette du thème du bureau, qui donnait ici du texte noir sur
    # fond sombre. Voir core/ui_theme.
    from core.ui_theme import apply_dark_palette
    apply_dark_palette(app)

    # Référence gardée volontairement : un QTimer sans parent ni référence
    # est ramassé, et les Ctrl+C redeviendraient muets pendant app.exec().
    _sig_timer = _installer_arret_propre(app, x11_guard)  # noqa: F841

    _installer_polices_de_repli()

    # QApplication applique la locale système (setlocale(LC_ALL, "")). libmpv exige
    # LC_NUMERIC="C" et plante sinon — segfault immédiat avec une locale à virgule
    # décimale (fr_FR, de_DE…). À faire APRÈS la création de QApplication, qui
    # écraserait le réglage, et avant toute instance MPV.
    locale.setlocale(locale.LC_NUMERIC, "C")

    app.setApplicationName("ZLink")
    # Pas de setApplicationDisplayName : Qt le concatène à CHAQUE titre de fenêtre
    # (« <titre> - <displayName> »), et toutes les fenêtres portent déjà leur
    # préfixe « ZLink — ». On obtenait « ZLink — première configuration - ZLink —
    # ZEvent Viewer » dans la barre de titre.
    app.setApplicationVersion(_display_version())
    # Sous Wayland, l'icône d'une fenêtre ne vient pas de setWindowIcon mais du
    # fichier .desktop portant ce nom : sans cette ligne, Hyprland et GNOME
    # affichent l'icône générique quoi qu'on fasse.
    app.setDesktopFileName("zlink")
    app.setWindowIcon(_icone_application())

    # Sur Windows, Qt hérite parfois d'une police système en "pixel size" uniquement.
    # Quand il tente de la convertir en point size il obtient -1 et logue un warning.
    # On force un point size explicite dès le départ pour couper ce cascade.
    _app_font = app.font()
    if _app_font.pointSize() <= 0:
        _app_font.setPointSize(10)
        app.setFont(_app_font)

    # --- Première configuration ---
    # AVANT build_layout : c'est l'assistant qui fixe le nombre d'écrans, et la
    # disposition est décidée une fois pour toutes au démarrage.
    from windows.wizard import run_first_run_wizard
    run_first_run_wizard(force=_force_setup)

    # --- Détection écrans et assignation des rôles ---
    layout = build_layout(app)
    logger.info("=== ZLink démarré en mode %s ===", layout.mode.name)

    # --- Config ---
    from core.paths import CONFIG_PATH as _cfg_path
    try:
        _startup_config: dict = json.loads(_cfg_path.read_text(encoding="utf-8")) if _cfg_path.exists() else {}
    except Exception:
        _startup_config = {}

    # Sons d'alerte, coupés par défaut. Importé ICI : la connexion de
    # settings_changed plus bas s'en sert, et un import placé après produisait
    # un NameError au premier enregistrement des réglages.
    from core import alerts as _alerts
    _alerts.configure(_startup_config)
    from core import sounds as _sounds
    _sounds.configure(_startup_config)

    # --- DataManager ---
    data_manager = DataManager()

    # --- StreamManager ---
    stream_manager = StreamManager()

    # --- Création des fenêtres ---
    fs_assignment = layout.get_screen(WindowRole.FULLSCREEN)
    if fs_assignment is None:
        logger.error("Aucun écran assigné pour le fullscreen — abandon")
        return 1

    # `_shell` n'est utilisé nulle part ailleurs, et doit pourtant être retenu :
    # il porte la barre de navigation du mode 1 écran. main() ne rend la main
    # qu'à la fermeture, cette variable locale suffit donc à le garder en vie.
    fullscreen, panel, grid, _shell = _creer_fenetres(
        layout, fs_assignment, _startup_config, data_manager, stream_manager,
    )

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
    # Durée des directs : demandée pour ce qui est AFFICHÉ seulement, et
    # redessinée quand elle arrive. Trois cents chaînes en direct feraient
    # douze requêtes toutes les cinq minutes pour des durées que personne ne
    # regarde.
    # Mémoire par chaîne : c'est elle qui donne « +320 viewers cette heure ».
    # Aucune API ne rend de série, on garde donc ce qui passe.
    from core import tendances
    data_manager.streamers_updated.connect(tendances.noter)
    data_manager.streamers_updated.connect(
        lambda streamers: data_manager.rafraichir_durees(
            _logins_a_dater(grid, fullscreen, selection_store, streamers)))
    data_manager.durees_updated.connect(fullscreen.rafraichir_duree)

    _brancher_panel(
        panel, grid, fullscreen, data_manager, stream_manager,
        selection_store, streamer_cache,
    )

    # --- Connexions StreamManager → FullscreenWindow ---
    stream_manager.stream_ready.connect(fullscreen.on_stream_ready)
    stream_manager.stream_error.connect(fullscreen.on_stream_error)
    stream_manager.stream_stopped.connect(fullscreen.on_stream_stopped)
    fullscreen.stream_change_requested.connect(
        lambda login: _on_stream_selected(login, fullscreen, data_manager, stream_manager)
    )
    _brancher_grille_streams(grid, panel, fullscreen, data_manager, stream_manager)
    _brancher_raids(grid, panel, fullscreen, data_manager)

    _brancher_top_audiences(grid, panel, fullscreen, data_manager)

    _brancher_raccourcis_grille(grid, fullscreen)
    _brancher_fil_evenements(panel, grid, data_manager)

    # Télécommande locale (Stream Deck). Gardée dans une variable : sans
    # référence, Python la ramasserait et l'écoute se fermerait aussitôt.
    _TELECOMMANDE.append(
        _brancher_telecommande(grid, panel, fullscreen, data_manager))
    _brancher_domotique(grid, data_manager)

    # Un favori qui lance son direct : annoncé aussi sur le plein écran, où
    # l'utilisateur a les yeux — le fil du panel peut être sur un autre écran.
    data_manager.favorite_live.connect(fullscreen.show_favorite_live)
    data_manager.milestone_reached.connect(fullscreen.show_milestone)
    data_manager.big_donation.connect(fullscreen.show_big_donation)
    data_manager.goal_imminent.connect(fullscreen.show_goal_imminent)

    _brancher_journal_session(app, data_manager, fullscreen, grid)

    # Surveillance du poste : voir core/resource_watch. Gardee dans une
    # variable, son fil est demon mais l'objet Qt doit survivre.
    _ressources = _brancher_surveillance_ressources(app, fullscreen, panel)

    _demarrer_donnees(app, data_manager, _mock_mode)

    _brancher_updater(panel)

    count = 1 + (1 if panel else 0) + (1 if grid else 0)
    logger.info("%d fenêtre(s) créée(s)", count)

    code = app.exec()

    # Sortie franche, sans démontage. La boucle Qt est terminée ; les lecteurs
    # mpv tournent encore et c'est voulu (voir plus haut). os._exit court-circuite
    # aussi la jonction des threads non-daemon, qui faisait traîner la fermeture.
    # Rien n'est en attente d'écriture : config.json et le cache d'avatars sont
    # écrits de façon atomique et synchrone.
    _xerr = x11_guard.error_count()
    if _xerr:
        logger.info("%d erreur(s) X11 absorbée(s) pendant la session", _xerr)
    logger.info("ZLink arrêté")
    logging.shutdown()
    os._exit(code)


def _refresh_fullscreen_viewers(
    streamers: list[object], fullscreen: FullscreenWindow
) -> None:
    """Met à jour le compteur viewers du stream fullscreen en cours."""
    # AVANT tout le reste : aucun sous-processus ne doit ouvrir de console.
    # Sous Windows, chaque appel à streamlink en faisait surgir une, qui
    # volait le premier plan au passage — une par cellule, une de plus par
    # reprise. Voir core/sous_processus.py.
    interdire_les_consoles()

    login = fullscreen.current_login
    if not login:
        return
    for s in streamers:
        if getattr(s, "twitch_login", None) == login:
            fullscreen.update_viewers(getattr(s, "viewers", 0))
            return


if __name__ == "__main__":
    sys.exit(main())
