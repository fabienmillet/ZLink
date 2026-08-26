# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Plein écran réel sous Windows.

Windows ne propose pas de plein écran *exclusif* à une application de bureau :
c'est un mode du swapchain DXGI, réservé au chemin de présentation 3D. Qt
implémente donc showFullScreen() en retirant le cadre et en dimensionnant la
fenêtre à l'écran — du « borderless windowed », et il n'y a rien au-dessus.

Reste la seule différence réellement visible avec le plein écran d'un jeu : le
shell ne rétracte la barre des tâches que pour la fenêtre plein écran AU PREMIER
PLAN, et il n'y en a qu'une. ZLink en ouvre trois ; les deux autres se faisaient
recouvrir par la barre des tâches sur leur écran, d'où l'impression d'une simple
fenêtre maximisée.

ITaskbarList2::MarkFullscreenWindow déclare la fenêtre comme plein écran auprès
du shell, indépendamment du focus — la barre des tâches se rétracte alors sur
les trois écrans, pas seulement celui qui a la main. On joint l'interface par
ctypes plutôt que par comtypes, qui serait une dépendance de plus pour trois
entrées de vtable.

Ça ne suffit pas quand chaque écran a sa propre barre des tâches, toutes
`WS_EX_TOPMOST` et sans masquage automatique : le shell n'en rétracte de façon
fiable que celle de l'écran dont la fenêtre a le focus, et il n'y en a qu'une.
Les deux autres barres restent donc visibles par-dessus leur fenêtre.

ON S'EN ACCOMMODE, ET C'EST DÉLIBÉRÉ.

Ce module a porté un second volet : les trois fenêtres passaient en
HWND_TOPMOST tant que ZLink était l'application active, et étaient relâchées au
départ du focus. Sur le papier, couverture totale pendant l'usage et Alt+Tab
intact. À l'usage, trois pannes distinctes :

- un Alt+Tab produit plusieurs bascules d'activation en quelques dizaines de
  millisecondes, et l'épinglage se reposait par-dessus l'application qu'on
  venait de choisir ;
- la page de don s'ouvrait DERRIÈRE les trois fenêtres — le navigateur montait
  bien en tête, mais de la bande non-topmost ;
- le moindre toast rendait ZLink actif et ramenait les trois écrans devant,
  alors qu'on travaillait ailleurs.

Trois barres des tâches visibles coûtent moins cher que ça. Si le besoin
revient, la piste à suivre n'est pas l'ordre Z mais l'auto-masquage du shell
(`ABM_SETSTATE`), qui s'adresse aux barres elles-mêmes plutôt que de passer
au-dessus d'elles.

Note qui reste valable si quelqu'un y retouche : l'ordre Z se change par
SetWindowPos et jamais par setWindowFlags — changer les drapeaux d'une fenêtre
déjà affichée détruit et recrée son handle natif, or c'est ce HWND que mpv
reçoit en --wid pour incruster la vidéo. Le recréer couperait l'image.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PyQt6.QtWidgets import QWidget

logger = logging.getLogger(__name__)

_WINDOWS = sys.platform == "win32"

# Indices dans la vtable : IUnknown occupe 0-2, ITaskbarList 3-7,
# MarkFullscreenWindow est la seule méthode ajoutée par ITaskbarList2.
_VT_HRINIT = 3
_VT_MARK_FULLSCREEN = 8

_CLSID_TASKBARLIST = "{56FDF344-FD6D-11D0-958A-006097C9A090}"
_IID_ITASKBARLIST2 = "{602D4995-B13A-429B-A66E-1935E44F4317}"

_CLSCTX_INPROC_SERVER = 1
_COINIT_APARTMENTTHREADED = 0x2

#: Instance unique, ou False si la création a échoué — on ne réessaie pas à
#: chaque fenêtre. None tant qu'on n'a pas tenté.
_mark_fn = None


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _taskbar_mark_fn():
    """Pointeur vers ITaskbarList2::MarkFullscreenWindow, ou False."""
    global _mark_fn
    if _mark_fn is not None:
        return _mark_fn
    _mark_fn = False
    try:
        ole32 = ctypes.windll.ole32
        # Qt initialise déjà COM sur le thread GUI ; un second appel renvoie
        # S_FALSE (ou RPC_E_CHANGED_MODE), sans conséquence. On l'appelle quand
        # même au cas où ce module servirait avant la création de QApplication.
        ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)

        clsid, iid = _GUID(), _GUID()
        ole32.CLSIDFromString(ctypes.c_wchar_p(_CLSID_TASKBARLIST), ctypes.byref(clsid))
        ole32.IIDFromString(ctypes.c_wchar_p(_IID_ITASKBARLIST2), ctypes.byref(iid))

        obj = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(clsid), None, _CLSCTX_INPROC_SERVER,
            ctypes.byref(iid), ctypes.byref(obj),
        )
        if hr < 0 or not obj:
            logger.debug("ITaskbarList2 indisponible (HRESULT 0x%08X)", hr & 0xFFFFFFFF)
            return _mark_fn

        vtable = ctypes.cast(
            obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        ).contents
        proto_init = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
        proto_mark = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int
        )
        # HrInit() est obligatoire avant tout autre appel de l'interface.
        hr = proto_init(vtable[_VT_HRINIT])(obj)
        if hr < 0:
            logger.debug("ITaskbarList2::HrInit a échoué (0x%08X)", hr & 0xFFFFFFFF)
            return _mark_fn

        mark = proto_mark(vtable[_VT_MARK_FULLSCREEN])
        _mark_fn = lambda hwnd, actif: mark(obj, ctypes.c_void_p(hwnd), int(actif))
    except (AttributeError, OSError) as exc:
        logger.debug("ITaskbarList2 inaccessible : %s", exc)
    return _mark_fn


def mark_fullscreen(widget: "QWidget", actif: bool = True) -> None:
    """Déclare (ou retire) le plein écran de `widget` auprès du shell Windows.

    À appeler APRÈS showFullScreen() : la fenêtre doit déjà avoir son handle
    natif. Sans effet hors Windows, et ne lève jamais — au pire la barre des
    tâches reste visible, ce qui était déjà le cas.
    """
    if not _WINDOWS:
        return
    fn = _taskbar_mark_fn()
    if not fn:
        return
    try:
        hwnd = int(widget.winId())
    except (RuntimeError, TypeError, ValueError):
        return  # fenêtre pas encore créée, ou déjà détruite
    if not hwnd:
        return
    try:
        fn(hwnd, actif)
    except OSError as exc:
        logger.debug("MarkFullscreenWindow a échoué : %s", exc)
