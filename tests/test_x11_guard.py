# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Garde contre les erreurs Xlib fatales des lecteurs mpv.

Le gestionnaire par défaut de Xlib appelle exit() ; avec dix-neuf lecteurs
arrêtés de front, le processus abandonne sur « corrupted double-linked list ».
Trois propriétés protègent de cette rechute, et ce sont elles qu'on teste :

- le gestionnaire compte et rend la main, sans jamais journaliser — il s'exécute
  sur un thread de rendu de mpv, où prendre le verrou du module `logging`
  pourrait bloquer ;
- la référence au trampoline ctypes est conservée, sinon Xlib saute dans de la
  mémoire libérée ;
- rien de tout cela ne s'exécute hors Linux, où il n'y a pas de libX11.

Les tests tournent hors Linux : les cas Linux sont joués avec une plateforme et
une libX11 simulées, ce qui permet de les couvrir sans serveur X.
"""

from __future__ import annotations

import ctypes
import sys

import pytest

from core import x11_guard


class _FausseFonction:
    """Fonction de bibliothèque espionnée : enregistre ses appels."""

    def __init__(self, leve: BaseException | None = None) -> None:
        self.appels: list = []
        self._leve = leve

    def __call__(self, *args):
        self.appels.append(args)
        if self._leve is not None:
            raise self._leve
        return None


class _FauxEspace:
    def __init__(self, **membres) -> None:
        self.__dict__.update(membres)


class _FauxCtypes:
    """Le vrai ctypes, sauf les attributs explicitement remplacés.

    Le module se sert des types ctypes réels (`CFUNCTYPE`, `cast`, `c_void_p`) :
    seuls le chargement de la bibliothèque et sa recherche sont détournés.
    """

    def __init__(self, **remplacements) -> None:
        self.__dict__.update(remplacements)

    def __getattr__(self, nom):
        return getattr(ctypes, nom)


@pytest.fixture(autouse=True)
def garde_neuve(monkeypatch):
    """Remet l'état de module à zéro : il est global au processus.

    Sans cela, une libX11 simulée par un test resterait chargée pour le suivant,
    et le compteur d'erreurs cumulerait d'un test à l'autre.
    """
    monkeypatch.setattr(x11_guard, "_LIB", None)
    monkeypatch.setattr(x11_guard, "_HANDLER_REF", None)
    monkeypatch.setattr(x11_guard, "_COUNT", [0])


@pytest.fixture
def libx11(monkeypatch):
    """Simule une plateforme Linux munie d'une libX11 chargeable."""
    lib = _FauxEspace(XSetErrorHandler=_FausseFonction())
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(x11_guard, "ctypes", _FauxCtypes(
        util=_FauxEspace(find_library=lambda _nom: "libX11.so.6"),
        CDLL=lambda _nom: lib,
    ))
    return lib


# ── inertie hors Linux ───────────────────────────────────────────────────────

@pytest.mark.parametrize("plateforme", ["win32", "darwin", "cygwin"])
def test_install_ne_fait_rien_hors_linux(monkeypatch, plateforme):
    """Aucun autre système n'a de gestionnaire d'erreurs Xlib à remplacer."""
    monkeypatch.setattr(sys, "platform", plateforme)
    assert x11_guard.install() is False
    assert x11_guard._LIB is None
    assert x11_guard._HANDLER_REF is None


def test_install_reel_hors_linux_ne_leve_pas():
    """Appel sans rustine : la plateforme de test n'est pas Linux."""
    assert x11_guard.install() is False


def test_install_reste_inoffensif_repete(monkeypatch):
    """Le module est rappelé après la création de CHAQUE lecteur."""
    monkeypatch.setattr(sys, "platform", "win32")
    assert [x11_guard.install() for _ in range(3)] == [False, False, False]


@pytest.mark.parametrize("plateforme", ["win32", "darwin"])
def test_watchdog_ne_demarre_pas_hors_linux(monkeypatch, plateforme):
    """Pas de QTimer créé non plus : il n'y aurait rien à réinstaller."""
    monkeypatch.setattr(sys, "platform", plateforme)
    assert x11_guard.start_watchdog() is None


# ── le gestionnaire lui-même ─────────────────────────────────────────────────

def test_le_gestionnaire_compte_et_rend_la_main():
    """Rendre la main est tout l'enjeu : le défaut de Xlib appelle exit()."""
    gestionnaire = x11_guard._make_handler()
    assert x11_guard.error_count() == 0
    assert gestionnaire(None, None) == 0
    assert x11_guard.error_count() == 1


def test_le_compteur_cumule():
    gestionnaire = x11_guard._make_handler()
    for _ in range(5):
        gestionnaire(None, None)
    assert x11_guard.error_count() == 5


def test_le_gestionnaire_ne_journalise_rien(caplog):
    """Il s'exécute sur un thread de rendu de mpv, souvent pendant l'arrêt :
    prendre le verrou du module `logging` pourrait bloquer.
    """
    gestionnaire = x11_guard._make_handler()
    with caplog.at_level(0):
        gestionnaire(None, None)
    assert caplog.records == []


# ── installation sur une libX11 simulée ──────────────────────────────────────

def test_installation_pose_notre_gestionnaire(libx11):
    assert x11_guard.install() is True
    assert x11_guard._LIB is libx11
    assert len(libx11.XSetErrorHandler.appels) == 1


def test_la_reference_au_trampoline_est_conservee(libx11):
    """Sans cette référence, le trampoline ctypes est collecté et Xlib saute
    dans de la mémoire libérée."""
    x11_guard.install()
    assert x11_guard._HANDLER_REF is not None


def test_reinstaller_reutilise_la_bibliotheque_et_le_trampoline(libx11):
    """La libX11 ne doit être chargée qu'une fois, et le trampoline rester le
    même objet — c'est lui qui est référencé côté Xlib."""
    x11_guard.install()
    trampoline = x11_guard._HANDLER_REF
    assert x11_guard.install() is True
    assert x11_guard._HANDLER_REF is trampoline
    assert x11_guard._LIB is libx11


def test_reinstaller_repose_le_gestionnaire_a_chaque_fois(libx11):
    """mpv pose le SIEN quand son affichage démarre : le nôtre doit repasser
    en dernier, sinon une erreur X entre-temps termine le processus."""
    for _ in range(3):
        x11_guard.install()
    assert len(libx11.XSetErrorHandler.appels) == 3


def test_libx11_introuvable_desactive_la_garde(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(x11_guard, "ctypes", _FauxCtypes(
        util=_FauxEspace(find_library=lambda _nom: None)))
    assert x11_guard.install() is False
    assert x11_guard._LIB is None


@pytest.mark.parametrize("erreur", [OSError("ELF illisible"),
                                    AttributeError("symbole absent")])
def test_libx11_inchargeable_desactive_la_garde(monkeypatch, erreur):
    """`_LIB` doit revenir à None, sans quoi l'appel suivant s'en servirait."""
    def _cdll(_nom):
        raise erreur

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(x11_guard, "ctypes", _FauxCtypes(
        util=_FauxEspace(find_library=lambda _nom: "libX11.so.6"), CDLL=_cdll))
    assert x11_guard.install() is False
    assert x11_guard._LIB is None


def test_echec_de_pose_du_gestionnaire_ne_leve_pas(monkeypatch):
    """Ne jamais bloquer : au pire on repart sans garde."""
    lib = _FauxEspace(
        XSetErrorHandler=_FausseFonction(leve=RuntimeError("appel refusé")))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(x11_guard, "ctypes", _FauxCtypes(
        util=_FauxEspace(find_library=lambda _nom: "libX11.so.6"),
        CDLL=lambda _nom: lib))
    assert x11_guard.install() is False


# ── chien de garde ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("demande,attendu", [
    (2000, 2000), (500, 500), (100, 500), (0, 500), (-5, 500),
])
def test_l_intervalle_du_watchdog_a_un_plancher(qapp, libx11, demande, attendu):
    """Un intervalle trop court n'apporte rien : XSetErrorHandler n'échange
    qu'un pointeur, mais réveiller la boucle d'évènements a un coût."""
    timer = x11_guard.start_watchdog(interval_ms=demande)
    try:
        assert timer.interval() == attendu
        assert timer.isActive()
    finally:
        timer.stop()


def test_le_watchdog_installe_la_garde_sans_attendre(qapp, libx11):
    """Le premier lecteur peut démarrer avant le premier tour de minuterie."""
    timer = x11_guard.start_watchdog()
    try:
        assert libx11.XSetErrorHandler.appels != []
    finally:
        timer.stop()


def test_le_watchdog_suit_son_parent(qapp, libx11):
    """La minuterie doit mourir avec la fenêtre qui l'a créée."""
    from PyQt6.QtCore import QObject

    parent = QObject()
    timer = x11_guard.start_watchdog(parent=parent)
    try:
        assert timer.parent() is parent
    finally:
        timer.stop()


def test_le_watchdog_reinstalle_a_chaque_battement(qapp, libx11):
    """C'est sa raison d'être : mpv laisse le gestionnaire par DÉFAUT derrière
    lui, et une erreur X entre deux installations termine le processus."""
    timer = x11_guard.start_watchdog()
    try:
        avant = len(libx11.XSetErrorHandler.appels)
        timer.timeout.emit()
        timer.timeout.emit()
        assert len(libx11.XSetErrorHandler.appels) == avant + 2
    finally:
        timer.stop()


# ── compteur ─────────────────────────────────────────────────────────────────

def test_le_compteur_part_de_zero():
    assert x11_guard.error_count() == 0
