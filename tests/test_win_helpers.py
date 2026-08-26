# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Les deux béquilles Windows : cession du premier plan et plein écran réel.

Ces modules parlent à Win32 par ctypes. Deux exigences les résument, et ce sont
elles qu'on teste :

- ils ne lèvent JAMAIS — l'échec d'une de ces API dégrade l'affichage, il ne
  doit pas remonter dans le code appelant ni interrompre l'ouverture d'une
  fenêtre ;
- ils sont totalement inertes hors Windows, où l'application tourne aussi.

Aucun test ne touche une vraie fenêtre plein écran : les appels Win32 sont soit
remplacés par des espions, soit faits sur un handle nul, que l'API rejette sans
effet de bord. Les fenêtres Qt réelles restent hors écran et jamais affichées —
sous la plateforme `offscreen`, leur `winId()` n'est de toute façon pas un HWND.
"""

from __future__ import annotations

import ctypes
import importlib
import sys

import pytest
from PyQt6.QtCore import QObject

from core import win_foreground, win_fullscreen


# ── outillage commun ─────────────────────────────────────────────────────────

class _FausseFonction:
    """Fonction Win32 espionnée : enregistre ses appels, ou échoue à volonté."""

    def __init__(self, leve: BaseException | None = None) -> None:
        self.appels: list = []
        self._leve = leve

    def __call__(self, *args):
        self.appels.append(args)
        if self._leve is not None:
            raise self._leve
        return 1


class _FauxEspace:
    """Porteur d'attributs, façon `windll` ou `user32`."""

    def __init__(self, **membres) -> None:
        self.__dict__.update(membres)


class _FauxCtypes:
    """Le vrai ctypes, sauf `windll`.

    Les modules posent `argtypes`/`restype` et se servent des types ctypes
    réels : seul l'aiguillage vers les DLL du système doit être détourné.
    """

    def __init__(self, windll) -> None:
        self.windll = windll

    def __getattr__(self, nom):
        return getattr(ctypes, nom)


class _FausseFenetre:
    """Fenêtre réduite à ce que `win_fullscreen` lui demande.

    Une QWidget suffirait, mais son `winId()` hors écran n'est pas un HWND :
    autant contrôler explicitement le handle rendu, y compris quand l'appel
    échoue parce que la fenêtre est détruite.
    """

    def __init__(self, hwnd: int = 0x1234, visible: bool = True,
                 leve: BaseException | None = None) -> None:
        self._hwnd = hwnd
        self._visible = visible
        self._leve = leve

    def winId(self) -> int:
        if self._leve is not None:
            raise self._leve
        return self._hwnd

    def isVisible(self) -> bool:
        return self._visible


# ═══ core.win_foreground ═════════════════════════════════════════════════════

@pytest.fixture
def api_premier_plan(monkeypatch):
    """Espionne AllowSetForegroundWindow au lieu de l'appeler pour de vrai."""
    fonction = _FausseFonction()
    monkeypatch.setattr(win_foreground, "ctypes", _FauxCtypes(
        _FauxEspace(user32=_FauxEspace(AllowSetForegroundWindow=fonction))
    ))
    return fonction


def test_ceder_premier_plan_autorise_n_importe_quel_processus(
        monkeypatch, api_premier_plan):
    """ASFW_ANY, sinon le navigateur relancé reste derrière le plein écran."""
    monkeypatch.setattr(sys, "platform", "win32")
    win_foreground.ceder_premier_plan()
    assert api_premier_plan.appels == [(0xFFFFFFFF,)]


@pytest.mark.parametrize("plateforme", ["linux", "darwin", "freebsd13"])
def test_ceder_premier_plan_est_inerte_hors_windows(
        monkeypatch, api_premier_plan, plateforme):
    """Le verrou de premier plan n'existe que sous Windows."""
    monkeypatch.setattr(sys, "platform", plateforme)
    win_foreground.ceder_premier_plan()
    assert api_premier_plan.appels == []


@pytest.mark.parametrize("erreur", [OSError("refusé"), AttributeError("absente")])
def test_ceder_premier_plan_avale_les_erreurs(monkeypatch, erreur):
    """Au pire le navigateur reste derrière, ce qui était déjà le cas."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(win_foreground, "ctypes", _FauxCtypes(
        _FauxEspace(user32=_FauxEspace(
            AllowSetForegroundWindow=_FausseFonction(leve=erreur)))
    ))
    win_foreground.ceder_premier_plan()   # ne doit pas lever


def test_ceder_premier_plan_sans_user32_ne_leve_pas(monkeypatch):
    """`windll.user32` peut manquer : l'AttributeError doit rester interne."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(win_foreground, "ctypes", _FauxCtypes(_FauxEspace()))
    win_foreground.ceder_premier_plan()


def test_ceder_premier_plan_sur_l_api_reelle_ne_leve_pas():
    """Appel véritable : AllowSetForegroundWindow n'a aucun effet de bord ici."""
    win_foreground.ceder_premier_plan()


# ═══ core.win_fullscreen ═════════════════════════════════════════════════════

class _Espion:
    """Relevé des appels détournés de `mark_fullscreen`."""

    def __init__(self) -> None:
        self.fabrications: list = []
        self.marques: list = []


@pytest.fixture
def espion(monkeypatch):
    """Détourne les deux sorties Win32 du module.

    Il n'en reste que deux : le module ne touche plus à l'ordre Z. Voir le
    docstring de core/win_fullscreen.py — l'épinglage a été retiré après trois
    pannes distinctes, dont un Alt+Tab que ZLink reprenait à son compte.
    """
    releve = _Espion()

    def _fabrique():
        releve.fabrications.append(True)
        return lambda hwnd, actif: releve.marques.append((hwnd, bool(actif)))

    monkeypatch.setattr(win_fullscreen, "_WINDOWS", True)
    monkeypatch.setattr(win_fullscreen, "_taskbar_mark_fn", _fabrique)
    return releve


# ── mark_fullscreen ──────────────────────────────────────────────────────────

def test_mark_fullscreen_declare_la_fenetre_au_shell(espion):
    fenetre = _FausseFenetre(hwnd=0xABC)
    win_fullscreen.mark_fullscreen(fenetre)
    assert espion.marques == [(0xABC, True)]


def test_le_retrait_est_declare_lui_aussi(espion):
    fenetre = _FausseFenetre(hwnd=0xABC)
    win_fullscreen.mark_fullscreen(fenetre, True)
    win_fullscreen.mark_fullscreen(fenetre, False)
    assert espion.marques == [(0xABC, True), (0xABC, False)]


def test_retrait_d_une_fenetre_jamais_declaree_ne_leve_pas(espion):
    win_fullscreen.mark_fullscreen(_FausseFenetre(), False)


@pytest.mark.parametrize("erreur", [
    RuntimeError("objet C++ détruit"), TypeError("pas un widget"),
    ValueError("handle illisible"),
])
def test_widget_sans_handle_natif_est_ignore(espion, erreur):
    """Fenêtre pas encore créée, ou déjà détruite : rien à déclarer au shell."""
    fenetre = _FausseFenetre(leve=erreur)
    win_fullscreen.mark_fullscreen(fenetre)
    assert espion.marques == []


def test_handle_nul_est_ignore(espion):
    win_fullscreen.mark_fullscreen(_FausseFenetre(hwnd=0))
    assert espion.marques == []


def test_mark_fullscreen_est_inerte_hors_windows(monkeypatch, espion):
    """Aucun autre système n'a d'ITaskbarList2 à qui parler."""
    monkeypatch.setattr(win_fullscreen, "_WINDOWS", False)
    win_fullscreen.mark_fullscreen(_FausseFenetre())
    assert espion.fabrications == []
    assert espion.marques == []


def test_taskbar_indisponible_ne_declare_rien(monkeypatch, espion):
    monkeypatch.setattr(win_fullscreen, "_taskbar_mark_fn", lambda: False)
    win_fullscreen.mark_fullscreen(_FausseFenetre())
    assert espion.marques == []


def test_echec_de_l_api_shell_ne_leve_pas(monkeypatch, espion):
    """Au pire la barre des tâches reste visible : ce n'est pas fatal."""
    def _fabrique():
        def _echoue(hwnd, actif):
            raise OSError("MarkFullscreenWindow a échoué")
        return _echoue

    monkeypatch.setattr(win_fullscreen, "_taskbar_mark_fn", _fabrique)
    win_fullscreen.mark_fullscreen(_FausseFenetre())


def test_widget_qt_hors_ecran_est_accepte(qapp, espion):
    """Un vrai QWidget, jamais affiché : `winId()` doit suffire au module."""
    from PyQt6.QtWidgets import QWidget

    widget = QWidget()
    try:
        win_fullscreen.mark_fullscreen(widget)
        assert [hwnd for hwnd, _ in espion.marques] == [int(widget.winId())]
    finally:
        widget.deleteLater()


# ── obtention de l'interface COM ─────────────────────────────────────────────

@pytest.fixture
def sans_interface_memorisee(monkeypatch):
    """Oublie l'instance COM déjà obtenue, et la restaure à la sortie."""
    monkeypatch.setattr(win_fullscreen, "_mark_fn", None)


def test_l_interface_est_obtenue_une_seule_fois(sans_interface_memorisee):
    """« On ne réessaie pas à chaque fenêtre » : trois fenêtres, une instance."""
    premier = win_fullscreen._taskbar_mark_fn()
    assert win_fullscreen._taskbar_mark_fn() is premier
    assert win_fullscreen._taskbar_mark_fn() is premier


def test_l_interface_reelle_est_appelable_ou_absente(sans_interface_memorisee):
    """CoCreateInstance peut échouer ; le module doit alors rendre False."""
    fn = win_fullscreen._taskbar_mark_fn()
    assert fn is False or callable(fn)


def test_sans_ole32_l_interface_est_declaree_absente(monkeypatch,
                                                    sans_interface_memorisee):
    monkeypatch.setattr(win_fullscreen, "ctypes", _FauxCtypes(_FauxEspace()))
    assert win_fullscreen._taskbar_mark_fn() is False


@pytest.mark.parametrize("hresult", [
    -2147221164,   # REGDB_E_CLASSNOTREG : le shell n'expose pas la barre
    0,             # succès annoncé, mais aucun objet rendu
])
def test_interface_com_non_creee_rend_false(monkeypatch,
                                            sans_interface_memorisee, hresult):
    """Sans ITaskbarList2, on abandonne le marquage plutôt que de déréférencer
    un pointeur nul — la barre des tâches restera visible, c'est tout."""
    monkeypatch.setattr(win_fullscreen, "ctypes", _FauxCtypes(_FauxEspace(
        ole32=_FauxEspace(
            CoInitializeEx=_FausseFonction(), CLSIDFromString=_FausseFonction(),
            IIDFromString=_FausseFonction(),
            CoCreateInstance=lambda *_args: hresult,
        ))))
    assert win_fullscreen._taskbar_mark_fn() is False


# ── constante de plateforme ──────────────────────────────────────────────────

def test_windows_est_deduit_de_la_plateforme():
    assert win_fullscreen._WINDOWS == (sys.platform == "win32")


def test_le_module_recharge_ailleurs_est_entierement_inerte(monkeypatch):
    """Vérification de bout en bout de l'inertie : sous une autre plateforme,
    le module ne doit rien tenter, pas même de charger ctypes.windll.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    recharge = importlib.reload(win_fullscreen)
    try:
        assert recharge._WINDOWS is False
        recharge.mark_fullscreen(_FausseFenetre())
        recharge.mark_fullscreen(_FausseFenetre(), False)
    finally:
        monkeypatch.undo()
        importlib.reload(win_fullscreen)


# ── Remontée du navigateur ───────────────────────────────────────────────────
#
# Céder le premier plan ne fait que LEVER l'interdit ; une fenêtre de navigateur
# déjà ouverte qui reçoit une URL se contente parfois de clignoter dans la barre
# des tâches. On va donc la chercher — et le tri des candidates est la seule
# partie qu'on puisse éprouver sans vraies fenêtres.

def test_le_navigateur_par_defaut_prime_sur_la_liste_en_dur():
    """L'association du système est la vérité : la liste vieillit, pas elle."""
    from core.win_foreground import _est_navigateur
    assert _est_navigateur("navigateur-maison.exe", "navigateur-maison.exe")
    assert not _est_navigateur("chrome.exe", "firefox.exe"), \
        "ce n'est pas lui qui a reçu l'URL"


def test_sans_association_connue_on_retombe_sur_les_navigateurs_courants():
    from core.win_foreground import _est_navigateur
    assert _est_navigateur("firefox.exe", "")
    assert not _est_navigateur("explorer.exe", "")


def test_un_processus_illisible_n_est_jamais_pris_pour_un_navigateur():
    """OpenProcess échoue sur les processus protégés : rendre '' est normal."""
    from core.win_foreground import _est_navigateur
    assert not _est_navigateur("", "firefox.exe")
    assert not _est_navigateur("", "")


@pytest.mark.skipif(sys.platform != "win32", reason="ctypes Win32")
def test_la_remontee_ne_leve_jamais(monkeypatch):
    """Au pire la page reste derrière — ce qui était déjà le cas."""
    import core.win_foreground as wf
    monkeypatch.setattr(wf, "_exe_du_navigateur_par_defaut",
                        lambda: (_ for _ in ()).throw(OSError("registre")))
    assert wf.remonter_navigateur() is False


def test_la_remontee_est_inerte_hors_windows(monkeypatch):
    import core.win_foreground as wf
    monkeypatch.setattr(wf.sys, "platform", "linux")
    assert wf.remonter_navigateur() is False


def test_la_cession_est_inerte_hors_windows(monkeypatch):
    import core.win_foreground as wf
    monkeypatch.setattr(wf.sys, "platform", "linux")
    assert wf.ceder_premier_plan() is False
