# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Cession du premier plan à l'application qu'on vient de lancer (Windows).

QDesktopServices.openUrl() passe par ShellExecute, qui remet l'URL au navigateur
DÉJÀ OUVERT plutôt que d'en démarrer un. Ce navigateur appelle alors
SetForegroundWindow pour se montrer — et Windows le refuse : le verrou de
premier plan réserve ce droit au processus qui détient le premier plan, ici
ZLink. Le navigateur se contente de faire clignoter son bouton dans la barre
des tâches, et la page de don reste derrière le plein écran.

AllowSetForegroundWindow(ASFW_ANY) est la porte de sortie prévue : le processus
au premier plan cède explicitement son tour au suivant qui le demandera. À
appeler juste avant d'ouvrir l'URL — l'autorisation est à usage unique et
expire au prochain changement de premier plan.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys

logger = logging.getLogger(__name__)

#: N'importe quel processus peut prendre le premier plan.
_ASFW_ANY = 0xFFFFFFFF


def ceder_premier_plan() -> bool:
    """Autorise le prochain processus à passer devant. Sans effet hors Windows.

    Ne lève jamais : au pire le navigateur reste derrière, ce qui était déjà le
    cas. Rend False quand Windows a refusé la cession — le cas se produit si
    ZLink n'est pas l'application au premier plan — pour que l'appelant sache
    qu'il devra insister.
    """
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        user32.AllowSetForegroundWindow.argtypes = [ctypes.c_uint]
        user32.AllowSetForegroundWindow.restype = ctypes.c_int
        if user32.AllowSetForegroundWindow(_ASFW_ANY):
            return True
        logger.debug("AllowSetForegroundWindow refusé : ZLink n'a pas la main")
    except (AttributeError, OSError) as exc:
        logger.debug("AllowSetForegroundWindow a échoué : %s", exc)
    return False


# ── Second volet : forcer, quand la cession ne suffit pas ────────────────────
#
# AllowSetForegroundWindow ne fait que LEVER l'interdit ; c'est au navigateur
# de demander le premier plan, et il ne le fait pas toujours — une fenêtre déjà
# ouverte qui reçoit une URL par la ligne de commande se contente parfois de
# créer l'onglet et de clignoter dans la barre des tâches. On va alors la
# chercher : on repère la fenêtre du navigateur PAR DÉFAUT (celle qui vient de
# recevoir l'URL) et on la remonte nous-mêmes.
#
# AttachThreadInput est le détour obligé : SetForegroundWindow n'obéit qu'au
# processus qui détient l'entrée. En joignant notre file d'entrée à celle de la
# fenêtre visée, on parle avec son autorité, le temps de l'appel.

#: Repli quand la base de registre ne dit rien : les navigateurs courants.
_NAVIGATEURS = frozenset({
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "opera_gx.exe", "vivaldi.exe", "chromium.exe", "librewolf.exe",
    "floorp.exe", "waterfox.exe", "zen.exe", "thorium.exe", "arc.exe",
})

_GW_OWNER = 4
_SW_RESTORE = 9
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _exe_du_navigateur_par_defaut() -> str:
    """Nom d'exécutable du navigateur associé à https, en minuscules.

    Passer par l'association du système plutôt que par une liste en dur : la
    liste vieillit, l'association est celle que Windows vient réellement
    d'utiliser pour ouvrir l'URL.
    """
    try:
        import winreg
        cle = (r"Software\Microsoft\Windows\Shell\Associations"
               r"\UrlAssociations\https\UserChoice")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cle) as k:
            prog_id = winreg.QueryValueEx(k, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                            rf"{prog_id}\shell\open\command") as k:
            commande = winreg.QueryValueEx(k, "")[0]
    except (ImportError, OSError, ValueError, IndexError):
        return ""
    # La commande ressemble à "C:\...\firefox.exe" -osint -url "%1".
    chemin = commande.strip()
    if chemin.startswith('"'):
        chemin = chemin[1:].split('"', 1)[0]
    else:
        chemin = chemin.split(" ", 1)[0]
    return os.path.basename(chemin).lower()


def _nom_du_processus(pid: int) -> str:
    """Nom d'exécutable d'un PID, en minuscules, ou '' si hors de portée."""
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        taille = ctypes.c_uint32(260)
        tampon = ctypes.create_unicode_buffer(taille.value)
        k32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        if not k32.QueryFullProcessImageNameW(h, 0, tampon,
                                              ctypes.byref(taille)):
            return ""
        return os.path.basename(tampon.value).lower()
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


def _est_navigateur(nom: str, attendu: str) -> bool:
    """Ce processus est-il celui qui a reçu l'URL ?"""
    if not nom:
        return False
    return nom == attendu if attendu else nom in _NAVIGATEURS


def _fenetre_du_navigateur(attendu: str) -> int:
    """HWND de la fenêtre de navigateur la plus haute dans l'ordre Z, ou 0.

    EnumWindows parcourt du dessus vers le dessous : la première trouvée est
    celle que le navigateur a lui-même mise en avant chez lui, donc celle qui
    porte l'onglet qui vient de s'ouvrir.
    """
    user32 = ctypes.windll.user32
    trouve = ctypes.c_void_p(0)

    proto = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

    def visiter(hwnd, _param):
        if not user32.IsWindowVisible(ctypes.c_void_p(hwnd)):
            return 1
        # Les boîtes de dialogue et pop-ups appartiennent à une autre fenêtre :
        # remonter la principale évite de mettre en avant un bandeau vide.
        user32.GetWindow.restype = ctypes.c_void_p
        if user32.GetWindow(ctypes.c_void_p(hwnd), _GW_OWNER):
            return 1
        if user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd)) <= 0:
            return 1
        pid = ctypes.c_uint32(0)
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd),
                                        ctypes.byref(pid))
        if not _est_navigateur(_nom_du_processus(pid.value), attendu):
            return 1
        trouve.value = hwnd
        return 0        # on tient la plus haute : inutile de continuer

    user32.EnumWindows(proto(visiter), None)
    return int(trouve.value or 0)


def _forcer_devant(hwnd: int) -> bool:
    """Remonte `hwnd` au premier plan en empruntant sa file d'entrée."""
    user32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    cible = ctypes.c_void_p(hwnd)
    fil_cible = user32.GetWindowThreadProcessId(cible, None)
    fil_nous = k32.GetCurrentThreadId()
    attache = False
    if fil_cible and fil_cible != fil_nous:
        attache = bool(user32.AttachThreadInput(fil_nous, fil_cible, True))
    try:
        if user32.IsIconic(cible):
            user32.ShowWindow(cible, _SW_RESTORE)
        user32.BringWindowToTop(cible)
        return bool(user32.SetForegroundWindow(cible))
    finally:
        if attache:
            user32.AttachThreadInput(fil_nous, fil_cible, False)


def remonter_navigateur() -> bool:
    """Met la fenêtre du navigateur devant, si elle n'y est pas déjà.

    À appeler quelques centaines de millisecondes après l'ouverture de l'URL :
    avant, la fenêtre visée peut ne pas exister encore. Ne lève jamais, et rend
    False quand il n'y avait rien à remonter — ou rien à trouver.
    """
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        attendu = _exe_du_navigateur_par_defaut()
        devant = user32.GetForegroundWindow()
        if devant:
            pid = ctypes.c_uint32(0)
            user32.GetWindowThreadProcessId(ctypes.c_void_p(devant),
                                            ctypes.byref(pid))
            if _est_navigateur(_nom_du_processus(pid.value), attendu):
                return False          # déjà devant : rien à forcer
        hwnd = _fenetre_du_navigateur(attendu)
        if not hwnd:
            logger.debug("Aucune fenêtre de navigateur à remonter (%s)",
                         attendu or "navigateur inconnu")
            return False
        return _forcer_devant(hwnd)
    except (AttributeError, OSError, ValueError) as exc:
        logger.debug("Remontée du navigateur impossible : %s", exc)
        return False
