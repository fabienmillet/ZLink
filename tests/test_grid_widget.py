# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Grille de flux : disposition, classement, placement des cellules.

Aucun lecteur mpv n'est instancié ici — `MpvWidget` est remplacé par un faux
pour toute la durée du module. Ce qui est testé est la logique qui décide *quoi*
mettre *où* : le calcul de disposition, le tri (audience, favoris, manuel),
l'arrivée et le départ des streamers, et le plafond de flux simultanés.
"""

from __future__ import annotations

import math

import pytest
from PyQt6.QtCore import QEvent, QMimeData, QPointF, Qt
from PyQt6.QtGui import QDragLeaveEvent, QMouseEvent, QResizeEvent
from PyQt6.QtWidgets import QWidget

from core import favorites
from core.replay_hd import REPLAY_SECS
from widgets import grid_widget as G
from widgets.grid_widget import (
    GridWidget,
    StreamCell,
    _compute_grid_dims,
    _distribute,
    _format_viewers,
)


# ── doublures ────────────────────────────────────────────────────────────────

class _FauxMpv(QWidget):
    """Lecteur factice : même surface d'appel, aucun flux, aucun libmpv."""

    from PyQt6.QtCore import pyqtSignal
    playback_started = pyqtSignal()
    playback_ended = pyqtSignal()
    resolution_failed = pyqtSignal(str)
    playback_requested = pyqtSignal(str)

    def __init__(self, parent=None, grid_mode: bool = False,
                 clip_buffer_secs: int = 0) -> None:
        super().__init__(parent)
        self.joue: tuple[str, str] | None = None
        self.muet: bool | None = None
        self.volume: int | None = None
        self.tampon = clip_buffer_secs

    def play_stream(self, login: str, quality: str) -> None:
        self.joue = (login, quality)

    def stop(self) -> None:
        self.joue = None

    def set_mute(self, muet: bool) -> None:
        self.muet = muet

    def set_volume(self, volume: int) -> None:
        self.volume = volume

    def set_clip_buffer(self, secs: int) -> None:
        self.tampon = secs

    def save_clip(self, secs: int, directory: str) -> str | None:
        return f"{directory}/faux_clip.ts" if directory else None


class _FauxStreamer:
    """Ce que la grille attend d'un StreamerInfo : trois attributs."""

    def __init__(self, login: str, viewers: int = 0, online: bool = True) -> None:
        self.twitch_login = login
        self.viewers = viewers
        self.online = online


class _QTimerSansDifferer(G.QTimer):
    """QTimer dont `singleShot` ne rappelle jamais.

    Les rappels différés de la grille (création du lecteur à 200 ms, reprise à
    4 s, pulsations) se réveilleraient pendant un test suivant, sur des widgets
    déjà détruits. On garde le côté synchrone, on jette le différé.
    """

    @staticmethod
    def singleShot(*_args, **_kwargs) -> None:
        return


@pytest.fixture(autouse=True)
def sans_mpv(monkeypatch):
    """Aucun test de ce module ne doit pouvoir ouvrir un flux réel."""
    monkeypatch.setattr(G, "MpvWidget", _FauxMpv)
    monkeypatch.setattr(G, "QTimer", _QTimerSansDifferer)


@pytest.fixture(autouse=True)
def sans_favoris(monkeypatch):
    """Neutralise les favoris : ils réordonnent la grille et fausseraient tout."""
    monkeypatch.setattr(favorites, "get", lambda: set())


@pytest.fixture
def favoris(monkeypatch):
    """Déclare un jeu de favoris pour le test qui en a besoin."""
    def _poser(*logins: str):
        monkeypatch.setattr(favorites, "get", lambda: {lg.lower() for lg in logins})
    return _poser


@pytest.fixture
def horloge(monkeypatch):
    """Horloge monotone pilotée : l'hystérésis de classement en dépend."""
    etat = {"t": 1000.0}
    monkeypatch.setattr(G.time, "monotonic", lambda: etat["t"])
    return etat


@pytest.fixture
def grille(qtbot):
    g = GridWidget()
    qtbot.addWidget(g)
    g.resize(1600, 900)
    return g


def _peupler(grille, *couples, online: bool = True) -> None:
    """Remplit la grille avec des (login, viewers), sans lancer de flux."""
    grille.set_streams([
        {"login": lg, "viewers": v, "online": online} for lg, v in couples
    ])


def _logins(grille) -> list[str]:
    return [c.twitch_login for c in grille._cells if c.twitch_login]


def _clic(widget, point, bouton=Qt.MouseButton.LeftButton) -> None:
    pos = QPointF(point)
    widget.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, pos, pos, bouton,
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
    ))


# ── fonctions pures ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("compte,attendu", [
    (0, "0"), (450, "450"), (999, "999"),
    (1_000, "1.0k"), (12_345, "12.3k"), (999_999, "1000.0k"),
    (1_000_000, "1.0M"), (1_250_000, "1.2M"),
])
def test_audience_abregee(compte, attendu):
    assert _format_viewers(compte) == attendu


@pytest.mark.parametrize("n,attendu", [
    (1, (1, 1)),
    (2, (1, 2)),
    (3, (1, 3)),
    (4, (2, 2)),
    (6, (2, 3)),
    (9, (3, 3)),
    (12, (3, 4)),
    (16, (4, 4)),
    (25, (5, 5)),
])
def test_disposition_retenue(n, attendu):
    """Sur un écran 16:9, une grille carrée donne des cellules 16:9."""
    assert _compute_grid_dims(n) == attendu


@pytest.mark.parametrize("n", [-4, 0, 30, 999])
def test_disposition_bornee(n):
    """Le nombre de cellules physiques est fixe : rien au-delà de 25, ni sous 1."""
    rows, cols = _compute_grid_dims(n)
    assert 1 <= rows * cols <= 25


@pytest.mark.parametrize("n", range(1, 26))
def test_la_disposition_loge_tout_le_monde_sans_case_noire_evitable(n):
    rows, cols = _compute_grid_dims(n)
    assert rows * cols >= n
    assert rows == math.ceil(n / cols), "aucune rangée entièrement vide"


@pytest.mark.parametrize("n", [0, -1])
def test_repartition_sans_segment(n):
    assert _distribute(100, n) == []


@pytest.mark.parametrize("total,n,gutter", [
    (1600, 1, 0), (1600, 3, 2), (900, 4, 2), (100, 7, 2), (1919, 5, 2),
])
def test_la_repartition_consomme_exactement_les_pixels_disponibles(total, n, gutter):
    """Un pixel perdu à chaque rangée finit par se voir en bas de la grille."""
    segments = _distribute(total, n, gutter)
    assert len(segments) == n
    dernier_x, derniere_taille = segments[-1]
    assert dernier_x + derniere_taille == total
    # Les segments se suivent, séparés du gutter demandé, sans se chevaucher.
    for (x1, w1), (x2, _) in zip(segments, segments[1:]):
        assert x2 == x1 + w1 + gutter


def test_le_reste_de_l_arrondi_va_aux_premiers_segments():
    tailles = [w for _, w in _distribute(10, 3, 0)]
    assert tailles == [4, 3, 3]


# ── disposition des cellules ─────────────────────────────────────────────────

def test_seules_les_cellules_occupees_sont_visibles(grille):
    """Une case noire dans la mosaïque passerait pour un flux planté."""
    _peupler(grille, ("a", 10), ("b", 20))
    visibles = [c for c in grille._cells if not c.isHidden()]
    assert {c.twitch_login for c in visibles} == {"a", "b"}


def test_les_cellules_se_partagent_toute_la_surface(grille):
    _peupler(grille, ("a", 30), ("b", 20), ("c", 10))
    geos = {c.twitch_login: c.geometry() for c in grille._cells if c.twitch_login}
    assert len(geos) == 3
    assert max(g.right() for g in geos.values()) == 1599
    for g in geos.values():
        assert g.height() == 900, "une seule rangée pour trois flux"


def test_une_grille_vide_ne_positionne_rien(grille):
    grille._reposition_cells()
    assert _logins(grille) == []


def test_une_grille_sans_dimension_ne_positionne_rien(grille):
    """Au démarrage la grille existe avant d'avoir reçu sa taille."""
    _peupler(grille, ("a", 10))
    avant = grille._cells[0].geometry()
    grille.resize(0, 0)
    grille._reposition_cells()
    assert grille._cells[0].geometry() == avant


# ── classement ───────────────────────────────────────────────────────────────

def test_tri_par_audience_decroissante(grille):
    _peupler(grille, ("petit", 100), ("gros", 9000), ("moyen", 3000))
    assert grille._applied_order == ["gros", "moyen", "petit"]


def test_les_favoris_passent_devant(grille, favoris):
    """C'est tout l'intérêt de marquer une chaîne."""
    favoris("petit")
    _peupler(grille, ("petit", 100), ("gros", 9000), ("moyen", 3000))
    assert grille._applied_order == ["petit", "gros", "moyen"]


def test_l_hysteresis_retient_un_reclassement_trop_rapide(grille, horloge):
    """Deux audiences proches échangeraient leur place à chaque sondage."""
    _peupler(grille, ("a", 9000), ("b", 8000))
    assert grille._applied_order == ["a", "b"]

    horloge["t"] += 5.0
    for cell in grille._cells:
        if cell.twitch_login == "b":
            cell._viewers = 99_000
    grille._reposition_cells()
    assert grille._applied_order == ["a", "b"], "le classement attend"

    horloge["t"] += G.GridWidget._REORDER_MIN_INTERVAL_S
    grille._reposition_cells()
    assert grille._applied_order == ["b", "a"]


def test_un_arrivant_est_classe_sans_attendre(grille, horloge):
    """L'hystérésis ne vaut que pour un simple échange de places."""
    _peupler(grille, ("a", 9000), ("b", 8000))
    horloge["t"] += 1.0
    _peupler(grille, ("a", 9000), ("b", 8000), ("c", 50_000))
    assert grille._applied_order == ["c", "a", "b"]


def test_le_mode_manuel_ignore_l_audience(grille):
    _peupler(grille, ("a", 100), ("b", 9000))
    grille.set_sort_mode("manual")
    grille.move_cell("a", "b")
    ordre = list(grille._applied_order)
    # Un rafraîchissement d'audience ne doit rien déplacer.
    for cell in grille._cells:
        if cell.twitch_login == "b":
            cell._viewers = 1_000_000
    grille._reposition_cells()
    assert grille._applied_order == ordre == ["a", "b"]


def test_le_mode_manuel_range_les_inconnus_a_la_fin(grille):
    grille.set_sort_mode("manual")
    _peupler(grille, ("a", 10), ("b", 20))
    grille.move_cell("b", "a")
    _peupler(grille, ("a", 10), ("b", 20), ("neuf", 99_999))
    assert grille._applied_order == ["b", "a", "neuf"]


def test_le_mode_favoris_remonte_les_favoris_sans_casser_l_ordre_manuel(
        grille, favoris):
    favoris("fav")
    grille.set_sort_mode("favorites")
    _peupler(grille, ("a", 30), ("b", 20), ("fav", 1))
    grille.move_cell("b", "a")
    grille._reposition_cells()
    assert grille._applied_order == ["fav", "b", "a"]


@pytest.mark.parametrize("mode,attendu,glissable", [
    ("viewers", "viewers", False),
    ("manual", "manual", True),
    ("favorites", "favorites", True),
    ("nawak", "viewers", False),
    ("", "viewers", False),
])
def test_modes_de_tri_acceptes(grille, mode, attendu, glissable):
    grille.set_sort_mode("manual")     # pour que « viewers » soit un changement
    grille.set_sort_mode(mode)
    assert grille._sort_mode == attendu
    assert grille.is_draggable() is glissable


def test_changer_de_mode_reclasse_immediatement(grille, horloge):
    """Sans cela l'hystérésis retiendrait le nouveau classement."""
    grille.set_sort_mode("manual")
    _peupler(grille, ("a", 10), ("b", 9000))
    assert grille._applied_order == ["a", "b"]
    horloge["t"] += 1.0
    grille.set_sort_mode("viewers")
    assert grille._applied_order == ["b", "a"]


def test_reposer_le_mode_en_cours_ne_fait_rien(grille, horloge):
    _peupler(grille, ("a", 10))
    grille._last_reorder = 4242.0
    grille.set_sort_mode("viewers")
    assert grille._last_reorder == 4242.0


# ── déplacement manuel ───────────────────────────────────────────────────────

@pytest.mark.parametrize("deplace,cible,attendu", [
    # Vers la droite : le défaut historique. L'index de destination est relevé
    # AVANT le retrait, sinon le dépôt atterrit une case trop à gauche.
    ("a", "c", ["b", "c", "a", "d"]),
    ("a", "d", ["b", "c", "d", "a"]),
    # Vers la gauche, le retrait ne décale rien.
    ("d", "b", ["a", "d", "b", "c"]),
    ("c", "a", ["c", "a", "b", "d"]),
    # Cible vide : on envoie en fin de grille.
    ("b", "", ["a", "c", "d", "b"]),
    # Se déposer sur soi-même revient à passer dernier.
    ("a", "a", ["b", "c", "d", "a"]),
])
def test_deplacement_d_une_cellule(grille, deplace, cible, attendu):
    grille.set_sort_mode("manual")
    _peupler(grille, ("a", 40), ("b", 30), ("c", 20), ("d", 10))
    grille.move_cell(deplace, cible)
    assert grille._applied_order == attendu


def test_deplacer_une_cellule_jamais_placee_l_insere(grille):
    """Une arrivée récente n'est pas encore dans l'ordre voulu par l'utilisateur."""
    grille.set_sort_mode("manual")
    _peupler(grille, ("a", 10), ("b", 20))
    grille._applied_order = ["b"]          # « a » vient d'arriver
    grille.move_cell("a", "b")
    assert grille._applied_order == ["a", "b"]


# ── vidage ───────────────────────────────────────────────────────────────────

def test_vider_la_grille_libere_tout(grille):
    _peupler(grille, ("a", 10), ("b", 20))
    grille._vider_grille()
    assert _logins(grille) == []
    assert grille._cell_map == {}
    assert all(c.isHidden() for c in grille._cells)


# ── sélection des flux en direct ─────────────────────────────────────────────

def test_seuls_les_selectionnes_en_direct_sont_retenus(grille):
    tous = [
        _FauxStreamer("a", 100), _FauxStreamer("b", 900),
        _FauxStreamer("hors_ligne", 5000, online=False),
        _FauxStreamer("non_selectionne", 8000),
    ]
    live = grille._flux_en_direct(tous, {"a", "b", "hors_ligne"})
    assert [s.twitch_login for s in live] == ["b", "a"]


def test_une_liste_illisible_laisse_la_grille_tranquille(grille):
    """Mieux vaut garder l'affichage précédent que de le vider sur une erreur."""
    bancal = _FauxStreamer("a")
    bancal.viewers = "beaucoup"
    assert grille._flux_en_direct([bancal], {"a"}) is None


def test_un_flux_termine_est_ecarte_pendant_le_delai(grille, horloge):
    """L'API met des minutes à basculer un streamer en « offline »."""
    grille._ended["a"] = horloge["t"]
    horloge["t"] += 10.0
    live = grille._flux_en_direct([_FauxStreamer("a", 100),
                                   _FauxStreamer("b", 50)], {"a", "b"})
    assert [s.twitch_login for s in live] == ["b"]


def test_le_delai_expire_laisse_le_flux_revenir(grille, horloge):
    """Le streamer a pu relancer son direct."""
    grille._ended["a"] = horloge["t"]
    horloge["t"] += GridWidget._ENDED_COOLDOWN_S + 1
    live = grille._flux_en_direct([_FauxStreamer("a", 100)], {"a"})
    assert [s.twitch_login for s in live] == ["a"]
    assert grille._ended == {}


def test_un_streamer_passe_hors_ligne_quitte_la_quarantaine(grille, horloge):
    """L'API a confirmé : la mise à l'écart n'a plus de raison d'être."""
    grille._ended["a"] = horloge["t"]
    grille._flux_en_direct([_FauxStreamer("a", 10, online=False)], {"a"})
    assert grille._ended == {}


# ── update_streamers ─────────────────────────────────────────────────────────

def test_sans_selection_la_grille_se_vide(grille):
    _peupler(grille, ("a", 10))
    grille.update_streamers([_FauxStreamer("a", 10)], [])
    assert _logins(grille) == []


def test_une_liste_non_iterable_ne_touche_a_rien(grille):
    _peupler(grille, ("a", 10))
    grille.update_streamers(42, ["a"])
    assert _logins(grille) == ["a"]


def test_le_plafond_de_flux_simultanes_est_respecte(grille):
    grille.set_max_streams(3)
    grille.update_streamers(
        [_FauxStreamer(f"s{i}", 1000 - i) for i in range(10)],
        [f"s{i}" for i in range(10)],
    )
    assert sorted(_logins(grille)) == ["s0", "s1", "s2"], "les plus regardés"


def test_le_fournisseur_de_qualite_recoit_le_nombre_de_flux(grille):
    """En mode adaptatif, la qualité se décide AVANT de peupler les cellules."""
    vus: list[int] = []

    def _provider(n: int) -> str:
        vus.append(n)
        return "480p"

    grille.set_quality_provider(_provider)
    grille.update_streamers([_FauxStreamer("a", 1), _FauxStreamer("b", 2)],
                            ["a", "b"])
    assert vus == [2]
    assert grille._grid_quality == "480p"


def test_un_streamer_parti_libere_sa_cellule(grille):
    grille.update_streamers([_FauxStreamer("a", 10), _FauxStreamer("b", 20)],
                            ["a", "b"])
    grille.update_streamers([_FauxStreamer("a", 10)], ["a"])
    assert _logins(grille) == ["a"]
    assert set(grille._cell_map) == {"a"}


def test_un_present_garde_sa_cellule_et_voit_son_audience_maj(grille):
    """Déplacer une cellule déplace la fenêtre native de mpv : à éviter."""
    grille.update_streamers([_FauxStreamer("a", 10)], ["a"])
    cellule = grille._cell_map["a"]
    grille.update_streamers([_FauxStreamer("a", 12_345)], ["a"])
    assert grille._cell_map["a"] is cellule
    assert cellule._viewers == 12_345


def test_un_arrivant_prend_une_case_libre(grille):
    grille.update_streamers([_FauxStreamer("a", 10), _FauxStreamer("b", 20)],
                            ["a", "b"])
    place_de_a = grille._cells.index(grille._cell_map["a"])
    grille.update_streamers([_FauxStreamer("a", 10), _FauxStreamer("c", 5)],
                            ["a", "c"])
    assert grille._cells.index(grille._cell_map["a"]) == place_de_a
    assert set(_logins(grille)) == {"a", "c"}


# ── plafond de flux ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("demande,attendu", [
    (1, 1), (9, 9), (25, 25), (30, 25), (0, 1),
])
def test_le_plafond_est_borne(grille, demande, attendu):
    grille.set_max_streams(demande)
    assert grille._max_active_streams == attendu


def test_reduire_le_plafond_libere_les_cellules_en_trop(grille):
    _peupler(grille, *[(f"s{i}", 100 - i) for i in range(6)])
    grille.set_max_streams(2)
    assert len(_logins(grille)) == 2


def test_un_plafond_negatif_devrait_vider_la_grille_comme_zero(grille):
    _peupler(grille, *[(f"s{i}", 100 - i) for i in range(6)])
    grille.set_max_streams(-2)
    assert len(_logins(grille)) <= grille._max_active_streams


# ── comptage des flux actifs ─────────────────────────────────────────────────

def test_le_compte_de_flux_n_est_publie_qu_au_changement(grille):
    recu: list[int] = []
    grille.active_streams_changed.connect(recu.append)
    _peupler(grille, ("a", 10), ("b", 20))
    grille._emit_active_count()
    grille._emit_active_count()
    assert recu == [2]
    _peupler(grille, ("a", 10))
    grille._emit_active_count()
    assert recu == [2, 1]


def test_les_cellules_hors_ligne_ne_comptent_pas(grille):
    recu: list[int] = []
    grille.active_streams_changed.connect(recu.append)
    _peupler(grille, ("a", 10), ("b", 20), online=False)
    grille._emit_active_count()
    assert recu == [0]


# ── fin de flux ──────────────────────────────────────────────────────────────

def test_un_flux_termine_libere_sa_cellule_et_entre_en_quarantaine(
        grille, horloge):
    grille.update_streamers([_FauxStreamer("a", 10), _FauxStreamer("b", 20)],
                            ["a", "b"])
    grille._on_stream_ended("a")
    assert "a" not in _logins(grille)
    assert grille._ended["a"] == horloge["t"]
    assert "a" not in grille._applied_order
    assert "a" not in grille._insertion_order
    assert "a" not in grille._cell_map


def test_une_fin_de_flux_laisse_monter_un_autre_streamer(grille):
    """Sinon la cellule reste noire pendant que d'autres directs attendent."""
    grille.set_max_streams(1)
    grille.update_streamers([_FauxStreamer("a", 900), _FauxStreamer("b", 100)],
                            ["a", "b"])
    assert _logins(grille) == ["a"]
    grille._on_stream_ended("a")
    assert _logins(grille) == ["b"]


def test_une_fin_de_flux_sans_historique_repositionne_seulement(grille):
    _peupler(grille, ("a", 10))
    grille._on_stream_ended("a")
    assert _logins(grille) == []


# ── audio épinglé ────────────────────────────────────────────────────────────

def test_l_epinglage_audio_bascule(grille):
    _peupler(grille, ("a", 10), ("b", 20))
    grille._on_audio_pin_requested("a")
    assert grille.pinned_audio_logins() == ["a"]
    grille._on_audio_pin_requested("a")
    assert grille.pinned_audio_logins() == []


def test_plusieurs_chaines_peuvent_etre_epinglees(grille):
    """Suivre deux commentaires à la fois est un usage courant en régie."""
    _peupler(grille, ("petit", 10), ("gros", 900))
    grille._on_audio_pin_requested("petit")
    grille._on_audio_pin_requested("gros")
    # L'ordre suit celui de la grille, pas celui des clics.
    assert grille.pinned_audio_logins() == ["gros", "petit"]


def test_un_login_vide_n_epingle_rien(grille):
    grille._on_audio_pin_requested("")
    assert grille._audio_pinned_logins == set()


def test_une_chaine_partie_perd_son_epingle(grille):
    """Une entrée sans son dans la console de mixage serait trompeuse."""
    _peupler(grille, ("a", 10), ("b", 20))
    grille._on_audio_pin_requested("a")
    _peupler(grille, ("b", 20))
    recu: list[list[str]] = []
    grille.audio_pins_changed.connect(recu.append)
    grille._apply_audio_pin()
    assert grille._audio_pinned_logins == set()
    assert recu == [[]]


def test_desepingler_une_chaine_absente_ne_fait_rien(grille):
    recu: list[list[str]] = []
    grille.audio_pins_changed.connect(recu.append)
    grille.unpin_audio("inconnu")
    assert recu == []


def test_desepingler_signale_la_nouvelle_liste(grille):
    _peupler(grille, ("a", 10))
    grille._on_audio_pin_requested("a")
    recu: list[list[str]] = []
    grille.audio_pins_changed.connect(recu.append)
    grille.unpin_audio("a")
    assert recu == [[]]


def test_volume_et_coupure_ne_touchent_que_la_cellule_visee(grille):
    _peupler(grille, ("a", 10), ("b", 20))
    cellule = grille._cell_map["a"]
    cellule._mpv = _FauxMpv()
    grille._on_audio_pin_requested("a")
    grille.set_cell_volume("a", 42)
    assert cellule._mpv.volume == 42
    grille.set_cell_muted("a", True)
    assert cellule._mpv.muet is True
    # Une chaîne absente ne doit pas lever.
    grille.set_cell_volume("inconnu", 10)
    grille.set_cell_muted("inconnu", True)


# ── clips ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("clips,secs,tampon_attendu", [
    (None, 60, 60),                                     # section absente
    ({"duration_secs": 30}, 30, 30),
    ({"duration_secs": 5}, 10, 10),                     # plancher
    ({"duration_secs": 0}, 60, 60),                     # zéro est faux → repli
    ({"grid_enabled": False, "duration_secs": 45}, 45, 0),
])
def test_configuration_des_clips(grille, clips, secs, tampon_attendu):
    grille.set_clip_config({"clips": clips} if clips is not None else {})
    assert grille._clip_secs == secs
    assert all(c._clip_secs == tampon_attendu for c in grille._cells)


def test_un_clip_sans_cellule_signale_l_echec(grille):
    """L'appelant doit apprendre l'échec : le toast attend une réponse."""
    recu: list[tuple[str, str]] = []
    grille.clip_saved.connect(lambda lg, p: recu.append((lg, p)))
    assert grille.save_clip("inconnu") is False
    assert recu == [("inconnu", "")]


def test_un_clip_part_sur_un_fil_et_ne_bloque_pas_la_grille(grille, monkeypatch):
    """Reprendre trente secondes chez Twitch prend plusieurs secondes.

    Les faire attendre au fil graphique figerait les vingt flux de la grille
    le temps du téléchargement.
    """
    _peupler(grille, ("a", 10))
    lances: list[str] = []
    monkeypatch.setattr(type(grille), "_lancer_clip",
                        lambda self, lg, cell: lances.append(lg))
    assert grille.save_clip("a") is True, "la demande est partie"
    assert lances == ["a"]


def test_un_clip_est_repris_en_pleine_qualite(grille, tmp_path, monkeypatch):
    """La cellule joue en 360p ; le clic garde la source, pas la vignette."""
    _peupler(grille, ("a", 10))
    cellule = grille._cell_map["a"]
    cellule._clip_secs = 60
    cellule._mpv = _FauxMpv()
    grille._clip_dir = str(tmp_path)
    demandes: list[tuple[str, int, str]] = []

    def _faux_recuperer(login, secondes, dossier="", prefixe=""):
        demandes.append((login, secondes, dossier, prefixe))
        return f"{dossier}/hd.mp4", float(secondes)

    monkeypatch.setattr("core.replay_hd.recuperer", _faux_recuperer)
    recu: list[tuple[str, str]] = []
    grille._clip_ecrit.connect(lambda lg, p: recu.append((lg, p)))
    grille._ecrire_clip("a", cellule)
    assert demandes == [("a", G.REPLAY_SECS, str(tmp_path), "clip")],         "un clip se nomme comme un clip, pas comme un replay"
    assert recu == [("a", f"{tmp_path}/hd.mp4")]


def test_un_clip_retombe_sur_le_tampon_local_si_twitch_ne_repond_pas(
        grille, tmp_path, monkeypatch):
    """Trente secondes de 360p valent mieux qu'un clic sans effet."""
    _peupler(grille, ("a", 10))
    cellule = grille._cell_map["a"]
    cellule._clip_secs = 60
    cellule._mpv = _FauxMpv()
    grille._clip_dir = str(tmp_path)
    monkeypatch.setattr("core.replay_hd.recuperer",
                        lambda *a, **k: ("", 0.0))
    recu: list[tuple[str, str]] = []
    grille._clip_ecrit.connect(lambda lg, p: recu.append((lg, p)))
    grille._ecrire_clip("a", cellule)
    assert recu == [("a", f"{tmp_path}/faux_clip.ts")]


def test_une_reprise_qui_leve_ne_fait_pas_perdre_le_clip(
        grille, tmp_path, monkeypatch):
    """Le repli existe pour ça : une exception ne doit pas remonter au fil."""
    _peupler(grille, ("a", 10))
    cellule = grille._cell_map["a"]
    cellule._clip_secs = 60
    cellule._mpv = _FauxMpv()
    grille._clip_dir = str(tmp_path)

    def _explose(*_a, **_k):
        raise RuntimeError("réseau coupé")

    monkeypatch.setattr("core.replay_hd.recuperer", _explose)
    recu: list[tuple[str, str]] = []
    grille._clip_ecrit.connect(lambda lg, p: recu.append((lg, p)))
    grille._ecrire_clip("a", cellule)
    assert recu == [("a", f"{tmp_path}/faux_clip.ts")]


def test_un_clip_sans_tampon_ni_reprise_signale_l_echec(
        grille, tmp_path, monkeypatch):
    """L'appelant doit apprendre l'échec : le toast attend une réponse."""
    _peupler(grille, ("a", 10))
    cellule = grille._cell_map["a"]
    grille._clip_dir = str(tmp_path)
    monkeypatch.setattr("core.replay_hd.recuperer",
                        lambda *a, **k: ("", 0.0))
    recu: list[tuple[str, str]] = []
    grille._clip_ecrit.connect(lambda lg, p: recu.append((lg, p)))
    grille._ecrire_clip("a", cellule)     # cellule sans mpv : tampon coupé
    assert recu == [("a", "")]


def test_un_replay_sans_cellule_ne_signale_rien(grille):
    recu: list = []
    grille.replay_requested.connect(lambda *a: recu.append(a))
    grille.request_replay("inconnu")
    assert recu == []


def test_un_replay_sans_tampon_ne_signale_rien(grille):
    _peupler(grille, ("a", 10))
    recu: list = []
    grille.replay_requested.connect(lambda *a: recu.append(a))
    grille.request_replay("a")
    assert recu == []


def test_un_replay_reussi_porte_le_chemin_et_la_duree(grille):
    _peupler(grille, ("a", 10))
    cellule = grille._cell_map["a"]
    cellule._clip_secs = 60
    cellule._mpv = _FauxMpv()
    recu: list = []
    grille.replay_requested.connect(lambda *a: recu.append(a))
    grille.request_replay("a")
    assert len(recu) == 1
    login, chemin, secondes = recu[0]
    # La durée du replay est décorrélée de celle des clips : elle est plafonnée
    # par la fenêtre que Twitch garde en ligne, mesurée à 28 s.
    assert (login, secondes) == ("a", REPLAY_SECS)
    assert chemin.endswith("faux_clip.ts")


# ── divers ───────────────────────────────────────────────────────────────────

def test_la_cellule_active_porte_le_contour_vert(grille):
    _peupler(grille, ("a", 10), ("b", 20))
    grille.set_active("b")
    assert [c._is_active for c in grille._cells if c.twitch_login] == \
        [c.twitch_login == "b" for c in grille._cells if c.twitch_login]


def test_aucune_cellule_active_quand_le_plein_ecran_est_vide(grille):
    _peupler(grille, ("a", 10))
    grille.set_active_stream(None)
    assert not any(c._is_active for c in grille._cells)


def test_un_clic_sur_une_cellule_la_designe(grille):
    _peupler(grille, ("a", 10), ("b", 20))
    recu: list[str] = []
    grille.stream_selected.connect(recu.append)
    grille._on_cell_clicked("b")
    assert recu == ["b"] and grille._active_login == "b"


def test_mise_a_jour_ciblee_d_une_cellule(grille):
    _peupler(grille, ("a", 10))
    grille.update_cell("a", 4_200)
    assert grille._cell_map["a"]._viewers == 4_200
    grille.update_cell("inconnu", 10)      # ne doit pas lever


def test_rafraichir_les_audiences_ne_deplace_personne(grille):
    _peupler(grille, ("a", 10), ("b", 20))
    places = {c.twitch_login: grille._cells.index(c)
              for c in grille._cells if c.twitch_login}
    grille.refresh_viewers([_FauxStreamer("a", 555), _FauxStreamer("b", 111)])
    assert grille._cell_map["a"]._viewers == 555
    assert {c.twitch_login: grille._cells.index(c)
            for c in grille._cells if c.twitch_login} == places


def test_un_streamer_passe_hors_ligne_voit_son_flux_coupe(grille):
    _peupler(grille, ("a", 10))
    cellule = grille._cell_map["a"]
    cellule._streaming_login = "a"
    grille.refresh_viewers([_FauxStreamer("a", 10, online=False)])
    assert cellule._streaming_login == ""


def test_relancer_avec_la_meme_qualite_ne_coupe_rien(grille):
    """Une relance inutile, c'est dix secondes de noir sur chaque cellule."""
    _peupler(grille, ("a", 10))
    cellule = grille._cell_map["a"]
    cellule._streaming_login = "a"
    grille.restart_all_streams(grille._grid_quality)
    assert cellule._streaming_login == "a"


def test_relancer_avec_une_autre_qualite_redemarre_les_cellules(grille):
    _peupler(grille, ("a", 10))
    grille.restart_all_streams("720p")
    assert grille._grid_quality == "720p"
    assert grille._cell_map["a"]._streaming_login == "a"


def test_la_cellule_active_est_retrouvee_apres_un_remplacement(grille):
    """Les cellules changent de streamer : le contour vert doit suivre."""
    _peupler(grille, ("a", 10), ("b", 20))
    grille.set_active("b")
    _peupler(grille, ("b", 20), ("c", 30))
    actives = [c.twitch_login for c in grille._cells if c._is_active]
    assert actives == ["b"]


def test_une_liste_de_streamers_illisible_ne_vide_pas_la_grille(grille):
    """Mieux vaut un affichage périmé qu'une grille noire sur une erreur d'API."""
    _peupler(grille, ("a", 10))
    bancal = _FauxStreamer("a")
    bancal.viewers = "beaucoup"
    grille.update_streamers([bancal], ["a"])
    assert _logins(grille) == ["a"]


def test_le_redimensionnement_replace_les_cellules(grille):
    from PyQt6.QtCore import QSize
    _peupler(grille, ("a", 30), ("b", 20))
    grille.resize(800, 600)
    grille.resizeEvent(QResizeEvent(QSize(800, 600), QSize(1600, 900)))
    assert max(c.geometry().right()
               for c in grille._cells if c.twitch_login) == 799


def test_pulser_une_cellule_presente(grille):
    _peupler(grille, ("a", 10))
    grille.pulse_cell("a")
    assert grille._cell_map["a"]._pulse_gen == 1


def test_le_jeu_de_donnees_de_demonstration_remplit_la_grille(grille):
    grille._load_test_data()
    assert len(_logins(grille)) == 20
    assert grille._active_login == "zerator"


def test_la_fermeture_arrete_tous_les_flux(grille):
    _peupler(grille, ("a", 10))
    cellule = grille._cell_map["a"]
    cellule._streaming_login = "a"
    grille.close()
    assert cellule._streaming_login == ""


# ── notifications ────────────────────────────────────────────────────────────

def test_un_objectif_accompli_fait_pulser_la_cellule(grille):
    _peupler(grille, ("a", 10))
    grille.goal_achieved_flash("a", "Manger un piment")
    assert grille._cell_map["a"]._pulse_gen == 1


def _toasts(grille):
    return grille.findChildren(G._GoalAchievedToast)


def test_une_chaine_affichee_pulse_sans_toast(grille):
    """Le toast recouvrait une cellule — donc un flux — dans la seule fenêtre
    entièrement faite de vidéo, pour dire ce que le liseré disait déjà, et
    mieux : lui désigne QUI."""
    _peupler(grille, ("a", 10))
    grille.goal_achieved_flash("a", "Manger un piment")
    assert _toasts(grille) == []


def test_une_chaine_absente_de_la_grille_garde_son_toast(grille):
    """Aucune cellule ne peut la signaler : sans toast, rien ne le dirait."""
    _peupler(grille, ("a", 10))
    grille.goal_achieved_flash("inconnu", "Objectif")
    assert len(_toasts(grille)) == 1


def test_un_objectif_sur_une_grille_vide_ne_leve_pas(grille):
    grille.goal_achieved_flash("inconnu", "Objectif")


def test_le_toast_de_hype_propose_de_garder_le_moment(grille, monkeypatch):
    _peupler(grille, ("a", 10))
    index = grille._cells.index(grille._cell_map["a"])
    lances: list[str] = []
    monkeypatch.setattr(type(grille), "_lancer_clip",
                        lambda self, lg, cell: lances.append(lg))
    grille.show_hype_toast(index, "Ça s'emballe", 0.92)
    toast = grille.findChildren(G._HypeToast)[-1]
    toast._on_keep()
    assert lances == ["a"], "le toast déclenche bien la sauvegarde"


def test_un_toast_de_hype_hors_grille_reste_anonyme(grille):
    grille.show_hype_toast(99, "Pic", 0.5)
    assert grille.findChildren(G._HypeToast)[-1]._login == ""


def test_le_toast_de_hype_peut_demander_un_replay(grille):
    _peupler(grille, ("a", 10))
    cellule = grille._cell_map["a"]
    cellule._clip_secs = 60
    cellule._mpv = _FauxMpv()
    recu: list = []
    grille.replay_requested.connect(lambda *a: recu.append(a))
    grille.show_hype_toast(grille._cells.index(cellule), "Pic", 0.7)
    grille.findChildren(G._HypeToast)[-1]._on_replay()
    assert len(recu) == 1


def test_pulser_toutes_les_cellules(grille):
    """Un palier de cagnotte n'appartient à personne en particulier."""
    _peupler(grille, ("a", 10), ("b", 20))
    grille.pulse_all()
    assert all(c._pulse_gen == 1 for c in grille._cells if c.twitch_login)
    assert all(c._pulse_gen == 0 for c in grille._cells if not c.twitch_login)


def test_pulser_une_chaine_absente_ne_leve_pas(grille):
    grille.pulse_cell("inconnu")


# ── StreamCell ───────────────────────────────────────────────────────────────

@pytest.fixture
def cellule(qtbot):
    c = StreamCell()
    qtbot.addWidget(c)
    return c


def test_une_cellule_neuve_est_vide(cellule):
    assert cellule.twitch_login == "" and cellule.is_online is False
    assert cellule.is_streaming is False


def test_une_cellule_en_ligne_affiche_son_audience(cellule):
    cellule.set_stream("zerator", 12_345)
    assert cellule.twitch_login == "zerator"
    assert cellule._viewers_lbl.text() == "12.3k"
    assert cellule._viewers_lbl.isHidden() is False


def test_une_cellule_hors_ligne_masque_son_audience_et_coupe_le_flux(cellule):
    cellule.set_stream("zerator", 12_345)
    cellule._streaming_login = "zerator"
    cellule.set_stream("zerator", 12_345, online=False)
    assert cellule._streaming_login == "", "le flux est coupé"
    assert cellule._viewers_lbl.isHidden() is True


def test_changer_de_streamer_autorise_un_nouveau_depart(cellule):
    cellule.set_stream("a", 10)
    cellule._streaming_login = "a"
    cellule.set_stream("b", 10)
    assert cellule._streaming_login == "", "b doit pouvoir démarrer"


def test_un_streamer_revenu_en_ligne_repart(cellule):
    cellule.set_stream("a", 10)
    cellule._streaming_login = "a"
    cellule.set_stream("a", 10, online=False)
    cellule.set_stream("a", 10, online=True)
    assert cellule._streaming_login == ""


def test_le_meme_streamer_toujours_en_ligne_n_est_pas_relance(cellule):
    """Relancer, c'est couper l'image : à ne pas faire sur un simple refresh."""
    cellule.set_stream("a", 10)
    cellule._streaming_login = "a"
    cellule.set_stream("a", 999)
    assert cellule._streaming_login == "a"
    assert cellule.is_streaming is True


def test_une_cellule_hors_ligne_ne_demarre_pas(cellule):
    cellule.set_stream("a", 10, online=False)
    cellule.start_stream()
    assert cellule._streaming_login == ""


def test_demarrer_une_cellule_arme_la_lecture(cellule):
    """Le lecteur est créé en différé : seul l'état est posé tout de suite."""
    cellule.set_stream("a", 10)
    cellule.start_stream("360p")
    assert cellule._streaming_login == "a"
    assert cellule._mpv is None, "aucun lecteur instancié dans l'immédiat"


def test_le_lecteur_n_est_cree_qu_a_la_demande(cellule):
    """Instancier vingt-cinq lecteurs au démarrage coûterait une fortune."""
    assert cellule._mpv is None
    lecteur = cellule._ensure_mpv()
    assert isinstance(lecteur, _FauxMpv)
    assert cellule._ensure_mpv() is lecteur, "un seul lecteur par cellule"


def test_le_lecteur_cree_sur_une_cellule_epinglee_n_est_pas_muet(cellule):
    """Sinon une cellule épinglée restait silencieuse jusqu'au prochain clic."""
    cellule._audio_pinned = True
    assert cellule._ensure_mpv().muet is False


def test_le_tampon_de_clip_survit_a_la_creation_du_lecteur(cellule):
    cellule.set_clip_buffer(45)
    assert cellule._ensure_mpv().tampon == 45


def test_vider_une_cellule_arrete_son_lecteur(cellule):
    lecteur = cellule._ensure_mpv()
    lecteur.joue = ("a", "360p")
    cellule.set_empty()
    assert lecteur.joue is None


def test_le_voile_de_chargement_tourne_puis_s_arrete(cellule):
    voile = cellule._overlay
    voile.show_overlay("zerator")
    assert voile._timer.isActive() and voile._login == "zerator"
    voile._tick()
    assert voile._angle == G._SPINNER_STEP
    voile.hide_overlay()
    assert voile._timer.isActive() is False


def test_le_voile_couvre_la_video_sans_manger_la_barre_d_info(cellule):
    from PyQt6.QtCore import QSize
    cellule.resize(400, 300)
    cellule.resizeEvent(QResizeEvent(QSize(400, 300), QSize(0, 0)))
    voile = cellule._overlay.geometry()
    assert (voile.width(), voile.height()) == (400, 300 - G._BAR_H)


def test_vider_une_cellule_remet_tout_a_zero(cellule):
    cellule.set_stream("a", 10)
    cellule._streaming_login = "a"
    cellule.set_placeholder()
    assert cellule.twitch_login == "" and cellule._streaming_login == ""
    assert cellule._viewers == 0


@pytest.mark.parametrize("prepare,attendu", [
    (lambda c: None, G.CELL_EMPTY),
    (lambda c: c.set_stream("a", 10), G.CELL_NORMAL),
    (lambda c: (c.set_stream("a", 10), c.set_active(True)), G.CELL_ACTIVE),
    (lambda c: c.set_stream("a", 10, online=False), G.CELL_OFFLINE),
])
def test_le_contour_dit_l_etat_de_la_cellule(cellule, prepare, attendu):
    prepare(cellule)
    cellule._refresh_style()
    assert cellule.styleSheet() == attendu


def test_toutes_les_bordures_font_deux_pixels(cellule):
    """Une largeur variable déplacerait la fenêtre native de mpv à chaque état."""
    for feuille in (G.CELL_EMPTY, G.CELL_NORMAL, G.CELL_ACTIVE, G.CELL_OFFLINE):
        assert "border: 2px solid" in feuille


def test_le_survol_de_depot_se_signale_puis_se_retire(cellule):
    cellule.set_stream("a", 10)
    cellule._set_drop_target(True)
    assert "dashed" in cellule.styleSheet()
    cellule._set_drop_target(False)
    assert cellule.styleSheet() == G.CELL_NORMAL


def test_l_audio_suit_l_epinglage_et_la_console(cellule):
    cellule.set_stream("a", 10)
    cellule._mpv = _FauxMpv()
    cellule.set_audio_pinned(True)
    assert cellule._mpv.muet is False
    # Coupure depuis la console : le mute prime, le volume garde sa valeur.
    cellule.set_mix_muted(True)
    assert cellule._mpv.muet is True
    cellule.set_mix_muted(False)
    assert cellule._mpv.muet is False
    cellule.set_audio_pinned(False)
    assert cellule._mpv.muet is True


def test_le_volume_ne_s_applique_qu_a_une_cellule_epinglee(cellule):
    cellule._mpv = _FauxMpv()
    cellule.set_volume(50)
    assert cellule._mpv.volume is None
    cellule.set_audio_pinned(True)
    cellule.set_volume(50)
    assert cellule._mpv.volume == 50


def test_l_audio_sans_lecteur_ne_leve_pas(cellule):
    cellule.set_audio_pinned(True)
    cellule.set_mix_muted(True)
    cellule.set_volume(10)


@pytest.mark.parametrize("demande,attendu", [(0, 0), (30, 30), (-5, 0)])
def test_tampon_de_clip(cellule, demande, attendu):
    cellule._mpv = _FauxMpv()
    cellule.set_clip_buffer(demande)
    assert cellule._clip_secs == attendu
    assert cellule._mpv.tampon == attendu


def test_reposer_le_meme_tampon_ne_touche_pas_au_lecteur(cellule):
    cellule.set_clip_buffer(30)
    cellule._mpv = _FauxMpv()
    cellule.set_clip_buffer(30)
    assert cellule._mpv.tampon == 0, "le lecteur n'a pas été sollicité"


def test_pas_de_clip_sans_lecteur_ni_tampon(cellule, tmp_path):
    assert cellule.save_clip(30, str(tmp_path)) is None
    cellule._mpv = _FauxMpv()
    assert cellule.save_clip(30, str(tmp_path)) is None, "tampon coupé"


def test_une_nouvelle_pulsation_annule_la_precedente(cellule):
    """Deux animations entrelacées clignotaient à contretemps bien après la fin."""
    cellule.set_stream("a", 10)
    cellule.pulse_hype("#ff6b00", pulses=3)
    ancienne = cellule._pulse_gen
    cellule.pulse_hype("#00ff87", pulses=2)
    assert cellule._pulse_gen == ancienne + 1
    # La suite de l'ancienne chaîne de minuteries ne doit plus rien peindre.
    cellule.setStyleSheet(G.CELL_NORMAL)
    cellule._pulse_on("#ff6b00", 2, ancienne)
    cellule._pulse_off("#ff6b00", 2, ancienne)
    assert cellule.styleSheet() == G.CELL_NORMAL


def test_une_pulsation_en_secondes_se_traduit_en_cycles(cellule):
    cellule.set_stream("a", 10)
    cellule.pulse_hype("#00ff87", seconds=10.0)
    assert "#00ff87" in cellule.styleSheet()


def test_l_extinction_d_une_pulsation_rend_le_contour_normal(cellule):
    cellule.set_stream("a", 10)
    cellule.pulse_hype("#ff6b00", pulses=2)
    cellule._pulse_off("#ff6b00", 2, cellule._pulse_gen)
    assert cellule.styleSheet() == G.CELL_NORMAL


def test_la_derniere_pulsation_rend_son_contour_a_la_cellule(cellule):
    cellule.set_stream("a", 10)
    cellule._pulse_on("#ff6b00", 0, cellule._pulse_gen)
    assert cellule.styleSheet() == G.CELL_NORMAL


def test_une_coupure_passagere_donne_droit_a_une_reprise(cellule):
    """Un flux Twitch hoquette régulièrement : abandonner trop vite serait pire."""
    recu: list[str] = []
    cellule.stream_ended.connect(recu.append)
    cellule.set_stream("a", 10)
    cellule._streaming_login = "a"

    cellule._on_playback_ended()
    assert recu == [], "on retente d'abord"
    assert cellule._end_retried is True

    cellule._streaming_login = "a"
    cellule._on_playback_ended()
    assert recu == ["a"], "la cellule est enfin libérée"


def test_une_cellule_vide_n_annonce_pas_de_fin_de_flux(cellule):
    recu: list[str] = []
    cellule.stream_ended.connect(recu.append)
    cellule._on_playback_ended()
    assert recu == []


def test_la_reprise_ne_repart_que_sur_le_meme_streamer(cellule):
    cellule.set_stream("b", 10)
    cellule._retry_stream("a")
    assert cellule._streaming_login == ""


def test_la_reprise_d_une_cellule_sans_grille_reprend_le_defaut(cellule):
    cellule.set_stream("a", 10)
    cellule._retry_stream("a")
    assert cellule._streaming_login == "a"


def test_la_reprise_conserve_la_qualite_de_la_grille(qtbot):
    """Sans elle, une cellule qui hoquette retombait en 160p sans remonter."""
    g = GridWidget()
    qtbot.addWidget(g)
    g._grid_quality = "720p"
    _peupler(g, ("a", 10))
    cellule = g._cell_map["a"]
    cellule._retry_stream("a")
    assert cellule._streaming_login == "a"


# ── glisser-déposer ──────────────────────────────────────────────────────────

class _FauxDepot:
    """Le strict nécessaire de QDropEvent : dropEvent n'en demande pas plus."""

    def __init__(self, source: str) -> None:
        self._mime = QMimeData()
        self._mime.setData(StreamCell._MIME, source.encode("utf-8"))
        self.accepte = False

    def mimeData(self) -> QMimeData:
        return self._mime

    def acceptProposedAction(self) -> None:
        self.accepte = True


def test_un_depot_reordonne_la_grille(grille):
    grille.set_sort_mode("manual")
    _peupler(grille, ("a", 40), ("b", 30), ("c", 20))
    cible = grille._cell_map["c"]
    evenement = _FauxDepot("a")
    cible.dropEvent(evenement)
    assert grille._applied_order == ["b", "c", "a"], "« a » prend la case de « c »"
    assert evenement.accepte is True


def test_un_depot_sur_soi_meme_ne_reordonne_rien(grille):
    grille.set_sort_mode("manual")
    _peupler(grille, ("a", 40), ("b", 30))
    grille._cell_map["a"].dropEvent(_FauxDepot("a"))
    assert grille._applied_order == ["a", "b"]


def test_un_depot_d_un_autre_format_est_ignore(grille):
    _peupler(grille, ("a", 40), ("b", 30))
    evenement = _FauxDepot("a")
    evenement._mime = QMimeData()      # plus rien au format attendu
    grille._cell_map["b"].dropEvent(evenement)
    assert evenement.accepte is False


def test_le_survol_d_une_cible_de_depot_se_voit(grille):
    _peupler(grille, ("a", 40), ("b", 30))
    cible = grille._cell_map["b"]
    evenement = _FauxDepot("a")
    cible.dragEnterEvent(evenement)
    assert "dashed" in cible.styleSheet()
    cible.dragLeaveEvent(QDragLeaveEvent())
    assert cible.styleSheet() == G.CELL_NORMAL


def test_une_cellule_se_survolant_elle_meme_ne_se_souligne_pas(grille):
    _peupler(grille, ("a", 40))
    cellule = grille._cell_map["a"]
    cellule.dragEnterEvent(_FauxDepot("a"))
    assert "dashed" not in cellule.styleSheet()


def test_une_cellule_retrouve_sa_grille(grille):
    _peupler(grille, ("a", 40))
    assert grille._cell_map["a"]._grid() is grille


def test_une_cellule_orpheline_n_a_pas_de_grille(cellule):
    assert cellule._grid() is None


def test_mettre_en_favori_sans_grille_ne_leve_pas(cellule, monkeypatch):
    monkeypatch.setattr(favorites, "toggle", lambda lg: True)
    cellule.set_stream("a", 10)
    cellule._toggle_favorite()


def test_l_appui_memorise_le_point_sans_rien_emettre(cellule):
    """Le clic ne part qu'au relâchement : sinon amorcer un glissement
    basculerait aussi en plein écran."""
    from PyQt6.QtCore import QPointF as _P
    cellule.set_stream("a", 10)
    recu: list[str] = []
    cellule.clicked.connect(recu.append)
    point = _P(cellule.rect().center())
    cellule.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, point, point, Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    ))
    assert recu == []
    assert cellule._press_pos is not None and cellule._dragging is False


def test_un_glissement_survolant_la_cellule_est_accepte(grille):
    """Sans acceptation, Qt refuse le dépôt et la cellule ne bouge jamais."""
    _peupler(grille, ("a", 40), ("b", 30))
    evenement = _FauxDepot("a")
    grille._cell_map["b"].dragMoveEvent(evenement)
    assert evenement.accepte is True


def test_mettre_en_favori_depuis_la_cellule_reclasse_la_grille(
        grille, monkeypatch, horloge):
    _peupler(grille, ("a", 10), ("b", 900))
    horloge["t"] += GridWidget._REORDER_MIN_INTERVAL_S + 1
    bascules: list[str] = []
    monkeypatch.setattr(favorites, "toggle", bascules.append)
    monkeypatch.setattr(favorites, "get", lambda: {"a"})
    grille._cell_map["a"]._toggle_favorite()
    assert bascules == ["a"]
    assert grille._applied_order == ["a", "b"]


# ── clic ─────────────────────────────────────────────────────────────────────

def test_un_clic_sur_une_cellule_en_ligne_la_selectionne(cellule):
    cellule.set_stream("a", 10)
    recu: list[str] = []
    cellule.clicked.connect(recu.append)
    _clic(cellule, cellule.rect().center())
    assert recu == ["a"]


@pytest.mark.parametrize("prepare", [
    lambda c: None,                                    # cellule vide
    lambda c: c.set_stream("a", 10, online=False),     # streamer hors ligne
])
def test_un_clic_sans_flux_ne_selectionne_rien(cellule, prepare):
    prepare(cellule)
    recu: list[str] = []
    cellule.clicked.connect(recu.append)
    _clic(cellule, cellule.rect().center())
    assert recu == []


def test_un_glissement_en_cours_annule_le_clic(cellule):
    """Sinon amorcer un déplacement basculerait aussi en plein écran."""
    cellule.set_stream("a", 10)
    cellule._dragging = True
    recu: list[str] = []
    cellule.clicked.connect(recu.append)
    _clic(cellule, cellule.rect().center())
    assert recu == []
    assert cellule._dragging is False, "l'état est bien remis à zéro"


# ── Une cellule qui ne démarre pas ───────────────────────────────────────────
#
# L'anneau de chargement tournait indéfiniment quand streamlink ne rendait rien
# ou quand l'URL obtenue restait muette : rien n'émettait d'événement, donc rien
# n'interrompait l'attente. Une cellule bloquée occupe une place que d'autres
# chaînes en direct attendaient.

def _cellule_lancee(grille, login="a"):
    _peupler(grille, (login, 10))
    cellule = grille._cell_map[login]
    cellule._streaming_login = login
    return cellule


def test_une_resolution_qui_echoue_relance_une_fois(grille):
    """Le premier échec est souvent passager : la cellule garde sa chaîne."""
    cellule = _cellule_lancee(grille)
    liberees: list[str] = []
    cellule.stream_ended.connect(liberees.append)
    cellule._sur_echec_resolution("a")
    assert cellule._echecs_demarrage == 1
    assert not cellule._streaming_login, "la cellule n'est plus en lecture"
    assert liberees == [], "on retente avant d'abandonner la place"


def test_la_cellule_n_est_rendue_qu_apres_toutes_les_relances(grille):
    """Abandonner au deuxième essai faisait défiler les cellules.

    Libérée, remplacée, échouée à nouveau — sans jamais laisser au flux le
    temps de revenir. Les attentes croissent, et la place n'est rendue qu'une
    fois la table épuisée.
    """
    cellule = _cellule_lancee(grille)
    liberees: list[str] = []
    cellule.stream_ended.connect(liberees.append)
    for essai in range(len(cellule._RELANCES_MS)):
        cellule._streaming_login = "a"
        cellule._sur_echec_resolution("a")
        if essai < len(cellule._RELANCES_MS) - 1:
            assert liberees == [], f"essai {essai + 1} : on retente encore"
    assert liberees == ["a"]


def test_les_attentes_entre_essais_augmentent(grille):
    """Réessayer aussitôt reproduit presque toujours le même hoquet."""
    attentes = _cellule_lancee(grille)._RELANCES_MS
    assert list(attentes) == sorted(attentes)
    assert attentes[0] < attentes[-1]
    assert sum(attentes) >= 60_000, "au moins une minute avant d'abandonner"


def test_une_image_recue_efface_l_ardoise(grille):
    """Un flux qui hoquette puis repart garde droit à une reprise plus tard."""
    cellule = _cellule_lancee(grille)
    cellule._sur_echec_resolution("a")
    assert cellule._echecs_demarrage == 1
    cellule._sur_premiere_image()
    assert cellule._echecs_demarrage == 0
    assert cellule._attente_image is None


def test_un_echec_qui_concerne_une_autre_chaine_est_ignore(grille):
    """La cellule a pu changer de chaîne pendant la résolution."""
    cellule = _cellule_lancee(grille)
    cellule._sur_echec_resolution("quelqu-un-d-autre")
    assert cellule._echecs_demarrage == 0


def test_l_attente_est_armee_a_la_remise_de_l_url(grille):
    """Et pas au clic : la résolution passe par un sémaphore à trois places.

    La vingtième cellule d'une grille attend son tour plusieurs dizaines de
    secondes sans que rien n'aille mal ; compter depuis le clic la déclarerait
    morte alors qu'elle n'a même pas commencé.
    """
    cellule = _cellule_lancee(grille)
    cellule._armer_attente("a")
    assert cellule._attente_image is not None
    assert cellule._attente_image.isActive()
    cellule._desarmer_attente()
    assert cellule._attente_image is None


def test_arreter_une_cellule_desarme_l_attente(grille):
    cellule = _cellule_lancee(grille)
    cellule._armer_attente("a")
    cellule.stop_stream()
    assert cellule._attente_image is None


def test_l_attente_expiree_sur_une_cellule_arretee_ne_fait_rien(grille):
    cellule = _cellule_lancee(grille)
    cellule._streaming_login = ""
    cellule._sur_attente_expiree()
    assert cellule._echecs_demarrage == 0


def test_changer_de_chaine_rend_son_credit_a_la_cellule(grille):
    cellule = _cellule_lancee(grille)
    cellule._echecs_demarrage = 2
    cellule.set_stream("b", 5, online=True)
    assert cellule._echecs_demarrage == 0


# ── L'audio du plein écran ne s'épingle pas ─────────────────────────────────
#
# Le plein écran porte déjà le son de sa chaîne : l'épingler dans la grille la
# faisait entendre DEUX FOIS, les deux lecteurs jouant le même flux avec
# quelques secondes d'écart.

def test_on_n_epingle_pas_l_audio_du_plein_ecran(grille):
    _peupler(grille, ("a", 10), ("b", 5))
    grille.set_active("a")
    grille._on_audio_pin_requested("a")
    assert grille.pinned_audio_logins() == []


def test_les_autres_chaines_restent_epinglables(grille):
    _peupler(grille, ("a", 10), ("b", 5))
    grille.set_active("a")
    grille._on_audio_pin_requested("b")
    assert grille.pinned_audio_logins() == ["b"]


def test_passer_une_chaine_epinglee_en_plein_ecran_la_desepingle(grille):
    """Le cas qui produisait le doublon : on épingle, puis on clique la cellule."""
    _peupler(grille, ("a", 10), ("b", 5))
    grille._on_audio_pin_requested("a")
    assert grille.pinned_audio_logins() == ["a"]
    grille.set_active("a")
    assert grille.pinned_audio_logins() == []


def test_le_plein_ecran_change_par_une_autre_route_est_bien_suivi(grille):
    """set_active_stream ne mettait pas _active_login à jour.

    La grille croyait donc encore active la dernière cellule CLIQUÉE, alors que
    le plein écran avait changé au clavier, à la télécommande ou à la palette.
    """
    _peupler(grille, ("a", 10), ("b", 5))
    grille.set_active_stream("b")
    assert grille._active_login == "b"
    grille._on_audio_pin_requested("b")
    assert grille.pinned_audio_logins() == []


def test_vider_le_plein_ecran_rend_tout_epinglable(grille):
    _peupler(grille, ("a", 10))
    grille.set_active_stream("a")
    grille.set_active_stream(None)
    grille._on_audio_pin_requested("a")
    assert grille.pinned_audio_logins() == ["a"]
