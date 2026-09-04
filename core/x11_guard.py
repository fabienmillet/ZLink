# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Rend non fatales les erreurs Xlib émises par les connexions de mpv.

Chaque lecteur mpv ouvre SA PROPRE connexion X et y présente ses images. À
l'arrêt, mpv détruit sa fenêtre alors qu'une requête Present est encore en vol :
le serveur répond BadWindow, et Xlib appelle son gestionnaire par défaut, lequel
imprime « X Error of failed request » puis **appelle exit()**.

C'est fatal à deux titres. D'abord exit() part d'un thread de rendu, pas du
thread principal. Ensuite, avec dix-neuf lecteurs arrêtés de front, dix-neuf
threads entrent en même temps dans la sortie du processus et déroulent
simultanément les destructeurs et le tas : glibc s'en aperçoit et abandonne sur
« corrupted double-linked list ».

Le gestionnaire installé ici se contente de compter et de rendre la main. Une
erreur X sur une fenêtre déjà détruite, pendant qu'on quitte, n'a rien à
signaler ; le serveur libère de toute façon toutes les ressources à la fermeture
de la connexion.

Le gestionnaire Xlib est GLOBAL au processus : il couvre aussi Mesa et les
autres bibliothèques. Qt, lui, parle xcb et n'est pas concerné.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys
import threading

logger = logging.getLogger(__name__)

# Le CFUNCTYPE doit rester référencé : sans cela le trampoline est collecté et
# Xlib saute dans de la mémoire libérée.
_HANDLER_REF: object = None
_LIB: ctypes.CDLL | None = None
_COUNT = [0]
_LOCK = threading.Lock()


def _make_handler() -> object:
    proto = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

    def _on_error(_display: object, _event: object) -> int:
        # Volontairement minimal : ce code s'exécute sur un thread de rendu de
        # mpv, souvent pendant l'arrêt. Journaliser ici prendrait le verrou du
        # module logging et pourrait bloquer. On compte, rien de plus.
        _COUNT[0] += 1
        return 0

    return proto(_on_error)


def install() -> bool:
    """Installe le gestionnaire. Idempotent, sans effet hors Linux/X11.

    À rappeler après la création de chaque lecteur : si une bibliothèque tierce
    installe le sien entre-temps, le nôtre doit rester le dernier.
    """
    global _HANDLER_REF, _LIB
    if not sys.platform.startswith("linux"):
        return False
    with _LOCK:
        if _LIB is None:
            name = ctypes.util.find_library("X11")
            if not name:
                logger.debug("libX11 introuvable — garde X11 non installée")
                return False
            try:
                _LIB = ctypes.CDLL(name)
                _LIB.XSetErrorHandler.restype = ctypes.c_void_p
                _LIB.XSetErrorHandler.argtypes = [ctypes.c_void_p]
            except (OSError, AttributeError) as exc:
                logger.debug("Garde X11 indisponible — %s", exc)
                _LIB = None
                return False
        if _HANDLER_REF is None:
            _HANDLER_REF = _make_handler()
        try:
            _LIB.XSetErrorHandler(
                ctypes.cast(_HANDLER_REF, ctypes.c_void_p)
            )
        except Exception as exc:  # jamais bloquant  # noqa: BLE001
            logger.debug("Installation de la garde X11 échouée — %s", exc)
            return False
    return True


def start_watchdog(parent=None, interval_ms: int = 2000):
    """Réinstalle le gestionnaire à intervalle régulier. Retourne le QTimer.

    Ce n'est pas de la précaution excessive : mpv pose SON gestionnaire quand
    son affichage démarre — vérifié — et laisse le gestionnaire par DÉFAUT
    derrière lui. Entre deux de nos installations ponctuelles, une erreur X
    terminait donc le processus, ce qu'un « BadWindow (Present) » observé en
    cours de session a confirmé.

    Le coût est nul : XSetErrorHandler échange un pointeur de fonction, sans
    aucun aller-retour avec le serveur X.
    """
    if not sys.platform.startswith("linux"):
        return None
    from PyQt6.QtCore import QTimer

    install()
    timer = QTimer(parent)
    timer.setInterval(max(500, int(interval_ms)))
    timer.timeout.connect(install)
    timer.start()
    logger.info("Garde X11 : réinstallation toutes les %d ms", timer.interval())
    return timer


def error_count() -> int:
    """Nombre d'erreurs Xlib absorbées depuis le démarrage."""
    return _COUNT[0]
