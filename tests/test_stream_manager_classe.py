# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""StreamManager : rechargement de config, paliers adaptatifs, anti-rebond.

Complète `test_stream_manager.py`, qui éprouve les fonctions de validation de
qualité. Ici c'est la CLASSE qui est en jeu, et sa seule décision réellement
coûteuse : changer la qualité de la grille émet `grid_quality_changed`, ce qui
arrête, re-résout et recharge TOUTES les cellules — une dizaine de secondes de
grille noire. Chaque test vérifie donc autant ce qui est émis que ce qui ne
l'est pas.

Aucun test ne lance streamlink ni de thread : seules les méthodes de décision
sont appelées. L'horloge est figée, l'anti-rebond ne se mesurant pas autrement.
"""

from __future__ import annotations

import pytest

from core.stream_manager import (
    QUALITY_FULLSCREEN,
    QUALITY_GRID,
    StreamManager,
    _DEFAULT_ADAPTIVE_TIERS,
    _QUALITY_DEBOUNCE_S,
)

# Empruntés au module plutôt que recopiés : ces échelles ont déjà changé une
# fois — Twitch nomme ses rendus « 360p » sur certaines chaînes et « 360p30 »
# sur d'autres — et des tests qui figent l'ancienne graphie empêchent de
# corriger la vraie.
_PALIER_1, _PALIER_4, _PALIER_9 = (q for _seuil, q in _DEFAULT_ADAPTIVE_TIERS)


@pytest.fixture
def manager(qapp):
    """StreamManager neuf — `qapp` est requis, c'est un QObject à signaux."""
    return StreamManager()


@pytest.fixture
def horloge(monkeypatch):
    """Fige `time.monotonic` : c'est elle que consulte l'anti-rebond."""
    etat = {"t": 5_000.0}
    monkeypatch.setattr("core.stream_manager.time.monotonic", lambda: etat["t"])
    return etat


@pytest.fixture
def relances(manager):
    """Liste des qualités annoncées à la grille, dans l'ordre."""
    recues: list[str] = []
    manager.grid_quality_changed.connect(recues.append)
    return recues


# ── quality_for_count ────────────────────────────────────────────────────────

@pytest.mark.parametrize("compte,attendu", [
    # Un flux seul : toute la bande passante lui revient.
    (0, _PALIER_1),
    (1, _PALIER_1),
    (2, _PALIER_4),
    (4, _PALIER_4),
    (5, _PALIER_9),
    (9, _PALIER_9),
    # Au-delà du dernier palier, la qualité de grille par défaut.
    (10, QUALITY_GRID),
    (25, QUALITY_GRID),
])
def test_le_premier_palier_qui_couvre_le_nombre_de_flux_gagne(manager, compte, attendu):
    """Budget visé : environ 50 Mbps et un encodeur sous les 50 %."""
    assert manager.quality_for_count(compte) == attendu


# ── reload_config ────────────────────────────────────────────────────────────

def test_le_mode_adaptatif_aligne_la_grille_sur_le_nombre_de_flux(manager, relances):
    """Grille encore vide : on prépare le palier d'un flux, pas celui de zéro."""
    manager.reload_config({"grid_adaptive": True})
    assert manager.grid_quality == _PALIER_1
    assert relances == [_PALIER_1]


def test_le_mode_manuel_prend_la_qualite_demandee(manager):
    manager.reload_config({"grid_adaptive": False, "grid_quality": "480p30,360p30"})
    assert manager.grid_quality == "480p30,360p30"


def test_le_mode_manuel_migre_un_selecteur_herite(manager):
    """« 360p » n'existe pas chez Twitch : la config d'hier retombait sur
    « worst », soit 284x160, sans que l'utilisateur en soit averti."""
    manager.reload_config({"grid_adaptive": False, "grid_quality": "360p,worst"})
    assert manager.grid_quality == QUALITY_GRID


@pytest.mark.parametrize("brut", [
    "", None, "   ",
    "360p30;rm -rf /",      # la qualité part en argument de sous-processus
    "--plugin-dirs=/tmp",
])
def test_une_qualite_de_grille_douteuse_retombe_sur_le_defaut(manager, brut):
    manager.reload_config({"grid_adaptive": False, "grid_quality": brut})
    assert manager.grid_quality == QUALITY_GRID


def test_une_qualite_inchangee_ne_relance_pas_la_grille(manager, relances):
    """Émettre pour rien coûterait une dizaine de secondes de grille noire."""
    manager.reload_config({"grid_adaptive": False, "grid_quality": QUALITY_GRID})
    assert relances == []
    assert manager.grid_quality == QUALITY_GRID


def test_des_paliers_personnalises_remplacent_les_defauts(manager):
    manager.reload_config({
        "grid_adaptive": True,
        "grid_adaptive_tiers": [[2, "best"], [6, "480p30"]],
    })
    assert manager.quality_for_count(2) == "best"
    assert manager.quality_for_count(6) == "480p30"
    assert manager.quality_for_count(7) == QUALITY_GRID


def test_des_paliers_inexploitables_laissent_les_precedents_en_place(manager):
    """Une table douteuse ne doit pas laisser la grille sans aucun palier :
    elle jouerait alors tout en qualité par défaut, y compris un flux seul."""
    manager.reload_config({"grid_adaptive": True,
                           "grid_adaptive_tiers": [[2, "best"]]})
    manager.reload_config({"grid_adaptive": True,
                           "grid_adaptive_tiers": "n'importe quoi"})
    assert manager.quality_for_count(2) == "best"


def test_le_mode_adaptatif_reprend_le_nombre_de_flux_deja_declare(manager, horloge,
                                                                  relances):
    """Recharger la config au milieu d'une session ne doit pas repartir de
    l'hypothèse d'un flux unique alors que la grille en joue neuf."""
    manager.set_active_grid_count(9)
    manager.reload_config({"grid_adaptive": True})
    assert manager.grid_quality == _PALIER_9
    assert relances == [_PALIER_9]


def test_qualite_plein_ecran_et_plafond_de_flux(manager):
    manager.reload_config({"fullscreen_quality": "1080p60,best",
                           "max_active_streams": 12})
    assert manager._quality_fullscreen == "1080p60,best"
    assert manager._max_active_streams == 12


def test_une_qualite_plein_ecran_douteuse_retombe_sur_best(manager):
    manager.reload_config({"fullscreen_quality": "best; echo coucou"})
    assert manager._quality_fullscreen == QUALITY_FULLSCREEN


# ── set_active_grid_count : anti-rebond ──────────────────────────────────────

def test_un_nouveau_palier_doit_se_confirmer_avant_de_relancer(manager, horloge,
                                                               relances):
    """Changer de palier relance TOUTES les cellules : on exige que le nouveau
    nombre de flux tienne, plutôt que de céder au premier sondage."""
    manager.set_active_grid_count(1)
    assert relances == []
    assert manager.grid_quality == QUALITY_GRID, "rien n'a encore bougé"

    horloge["t"] += _QUALITY_DEBOUNCE_S - 1
    manager.set_active_grid_count(1)
    assert relances == [], "le palier n'a pas encore tenu assez longtemps"

    horloge["t"] += 2
    manager.set_active_grid_count(1)
    assert relances == [_PALIER_1]
    assert manager.grid_quality == _PALIER_1
    assert manager._pending_quality is None, "l'attente est soldée"


def test_une_oscillation_autour_du_seuil_ne_relance_rien(manager, horloge, relances):
    """Un soir d'event, un streamer qui passe et repasse le seuil déclencherait
    la tempête à chaque sondage. Chaque nouvelle cible remet le chrono à zéro."""
    for _ in range(10):
        manager.set_active_grid_count(1)     # vise le palier d'un flux
        horloge["t"] += _QUALITY_DEBOUNCE_S
        manager.set_active_grid_count(5)     # vise celui de neuf
        horloge["t"] += _QUALITY_DEBOUNCE_S
    assert relances == []
    assert manager.grid_quality == QUALITY_GRID


def test_le_retour_au_palier_courant_annule_l_attente(manager, horloge, relances):
    """La grille est revenue d'elle-même là où on était : il n'y a plus rien à
    confirmer, et laisser l'attente en cours ferait basculer au sondage suivant."""
    manager.set_active_grid_count(1)
    assert manager._pending_quality == _PALIER_1

    manager.set_active_grid_count(15)        # au-delà du dernier palier
    assert manager._pending_quality is None
    assert relances == []


@pytest.mark.parametrize("compte", [0, -5])
def test_une_grille_vide_ne_change_pas_de_palier(manager, horloge, relances, compte):
    """Sans flux joué, il n'y a ni bande passante à répartir ni rien à relancer."""
    manager.set_active_grid_count(compte)
    assert manager._active_grid_count == 0, "un compte négatif est ramené à zéro"
    assert relances == []


def test_le_mode_manuel_ignore_le_nombre_de_flux(manager, horloge):
    """Une qualité choisie à la main est un choix : la grille ne le révise pas."""
    manager.reload_config({"grid_adaptive": False, "grid_quality": "480p30"})
    recues: list[str] = []
    manager.grid_quality_changed.connect(recues.append)

    for _ in range(5):
        manager.set_active_grid_count(1)
        horloge["t"] += _QUALITY_DEBOUNCE_S * 2

    assert recues == []
    assert manager.grid_quality == "480p30"


# ── resolve_grid_quality ─────────────────────────────────────────────────────

def test_la_qualite_est_connue_avant_meme_de_lancer_les_cellules(manager, horloge,
                                                                 relances):
    """La grille interroge avant de démarrer, ce qui évite de lancer les
    cellules dans une qualité pour les relancer aussitôt dans une autre."""
    assert manager.resolve_grid_quality(1) == _PALIER_1
    # Sans effet de bord : ni bascule, ni attente ouverte, ni relance annoncée.
    assert manager.grid_quality == QUALITY_GRID
    assert manager._pending_quality is None
    assert manager._active_grid_count == 0
    assert relances == []


@pytest.mark.parametrize("compte", [0, -1])
def test_sans_flux_c_est_la_qualite_courante_qui_fait_foi(manager, compte):
    assert manager.resolve_grid_quality(compte) == manager.grid_quality


def test_en_mode_manuel_la_qualite_ne_depend_pas_du_nombre_de_flux(manager):
    manager.reload_config({"grid_adaptive": False, "grid_quality": "480p30"})
    assert manager.resolve_grid_quality(1) == "480p30"
    assert manager.resolve_grid_quality(20) == "480p30"


def test_la_qualite_de_grille_est_lisible_mais_pas_modifiable(manager):
    """Elle ne doit changer que par les chemins qui préviennent la grille."""
    assert manager.grid_quality == QUALITY_GRID
    with pytest.raises(AttributeError):
        manager.grid_quality = "best"
