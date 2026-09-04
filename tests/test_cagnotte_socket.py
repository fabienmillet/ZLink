# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Flux temps réel de la cagnotte.

Aucun navigateur n'est lancé ici : `_appliquer` est le seul point où le monde
extérieur entre dans l'objet, et c'est une chaîne JSON. On l'appelle
directement avec ce que le vrai flux envoie — les trois formes ont été
relevées sur le serveur, elles ne sont pas inventées.
"""

from __future__ import annotations

import json

import pytest

from core.cagnotte_socket import FluxCagnotte, _js_ouverture


@pytest.fixture
def flux(qtbot):
    f = FluxCagnotte()
    return f


def _etat(ouvert=True, total=None, dons=(), hist=()):
    """Ce que le vidage JavaScript rend."""
    return json.dumps({"ouvert": ouvert, "total": total,
                       "dons": list(dons), "hist": list(hist)})


# ── cagnotte ─────────────────────────────────────────────────────────────────

def test_la_cagnotte_est_emise(flux, qtbot):
    with qtbot.waitSignal(flux.cagnotte_changee) as bloc:
        flux._appliquer(_etat(total=880074.03))
    assert bloc.args == [880074.03]
    assert flux.total == 880074.03


def test_une_cagnotte_inchangee_n_est_pas_reemise(flux):
    """Le flux répète le total à chaque don : repeindre le panel vingt fois
    par seconde pour la même valeur ne sert personne."""
    vus: list[float] = []
    flux.cagnotte_changee.connect(vus.append)
    flux._appliquer(_etat(total=1000.0))
    flux._appliquer(_etat(total=1000.0))
    flux._appliquer(_etat(total=1001.0))
    assert vus == [1000.0, 1001.0]


@pytest.mark.parametrize("total", [None, 0, -5, "beaucoup"])
def test_une_cagnotte_absurde_est_ignoree(flux, total):
    vus: list[float] = []
    flux.cagnotte_changee.connect(vus.append)
    flux._appliquer(_etat(total=total))
    assert vus == []
    assert flux.total is None


# ── dons ─────────────────────────────────────────────────────────────────────

def test_chaque_don_est_emis(flux):
    dons = [{"donor": "Yodinou", "amount": 100, "streamer": "Joueur_du_Grenier"},
            {"donor": "Cedo", "amount": 1, "streamer": "MiiOrca"}]
    vus: list[dict] = []
    flux.don_recu.connect(vus.append)
    flux._appliquer(_etat(dons=dons))
    assert vus == dons


def test_ce_qui_n_est_pas_un_don_est_jete(flux):
    """La file vient d'un navigateur : on ne suppose rien de sa forme."""
    vus: list[object] = []
    flux.don_recu.connect(vus.append)
    flux._appliquer(_etat(dons=["texte", None, 42, {"donor": "ok"}]))
    assert vus == [{"donor": "ok"}]


# ── état du socket ───────────────────────────────────────────────────────────

def test_l_ouverture_et_la_chute_sont_signalees(flux):
    vus: list[bool] = []
    flux.etat_change.connect(vus.append)
    flux._appliquer(_etat(ouvert=True))
    flux._appliquer(_etat(ouvert=True))     # inchangé : rien de plus
    flux._appliquer(_etat(ouvert=False))
    assert vus == [True, False]
    assert flux.ouvert is False


# ── robustesse du vidage ─────────────────────────────────────────────────────

@pytest.mark.parametrize("brut", [
    "", "{}", "pas du json", None, 42, "[]", '"une chaîne"',
])
def test_un_vidage_illisible_ne_leve_pas(flux, brut):
    """C'est un rappel Qt : une exception ici ne serait rattrapée par personne."""
    vus: list[object] = []
    flux.cagnotte_changee.connect(vus.append)
    flux.don_recu.connect(vus.append)
    flux._appliquer(brut)
    assert vus == []


def test_une_page_absente_ne_fait_rien(flux):
    """`_vider` tourne sur un timer qui peut survivre d'un tour à `arreter`."""
    flux._vider()          # ne doit pas lever


# ── le script injecté ────────────────────────────────────────────────────────

def test_l_url_du_socket_est_echappee():
    """Elle est interpolée dans du JavaScript : `json.dumps`, jamais une
    concaténation — c'est une URL de configuration, pas une constante."""
    js = _js_ouverture('wss://x/y"; alert(1); //')
    assert '"wss://x/y\\"; alert(1); //"' in js


def test_le_script_est_reentrant():
    """La page est rechargée par le challenge Cloudflare lui-même : le script
    est réinjecté à chaque chargement et ne doit pas rouvrir un second socket."""
    js = _js_ouverture("wss://exemple/flux")
    assert "if (window.__zlinkFlux) return" in js


def test_le_snapshot_ne_passe_pas_par_la_file_du_direct():
    """Le rejouer comme du direct ferait sonner toutes les alertes de ZLink
    d'un coup à chaque reconnexion. Il part sur `hist`, pas sur `file`."""
    js = _js_ouverture("wss://exemple/flux")
    debut = js.index('m.type === "snapshot"')
    assert "empiler" not in js[debut:]
    assert "etat.hist" in js[debut:]


def test_l_historique_sort_par_son_propre_canal(flux):
    """Le fil des dons le veut — sans lui l'onglet s'ouvre vide. Une alerte,
    elle, le prendrait pour des dons qui viennent d'arriver."""
    passes, directs = [], []
    flux.historique_recu.connect(passes.append)
    flux.don_recu.connect(directs.append)
    flux._appliquer(_etat(hist=[{"donor": "a"}, {"donor": "b"}],
                          dons=[{"donor": "c"}]))
    assert passes == [[{"donor": "a"}, {"donor": "b"}]]
    assert directs == [{"donor": "c"}]


def test_un_historique_vide_n_emet_rien(flux):
    """Chaque vidage rend `hist` : émettre une liste vide deux fois par
    seconde ferait repeindre le fil pour rien."""
    vus = []
    flux.historique_recu.connect(vus.append)
    flux._appliquer(_etat(hist=[]))
    assert vus == []
