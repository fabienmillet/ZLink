# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Câblage de main.py : ce qui se passe quand une donnée ou un clic arrive.

Ces fonctions sont le point de rencontre des trois fenêtres. Elles doivent
toutes supporter qu'une fenêtre soit ABSENTE — en mode un ou deux écrans, le
panel ou la grille n'existent pas — sans quoi un mode d'affichage entier casse.
C'est ce que ces tests verrouillent.
"""

from __future__ import annotations

import pytest

import main
from core.api_client import StreamerInfo


def _streamer(login="zerator", **kw) -> StreamerInfo:
    base = dict(
        twitch_login=login, display=login, online=True, game="Minecraft",
        location="LAN", viewers=1000, donation=0.0, donation_formatted="",
        profile_url="", donation_url="https://zevent.fr/dons",
    )
    base.update(kw)
    return StreamerInfo(**base)


class _FauxFullscreen:
    def __init__(self):
        self.current_login = ""
        self.streams: list[tuple] = []
        self.viewers: list[int] = []
        self.vidages = 0
        #: Liste transmise à la palette de commandes du plein écran.
        self.pour_la_palette: list = []

    def set_streamers(self, streamers):
        self.pour_la_palette = list(streamers)

    def set_stream(self, login, game="", viewers=0, donation_url=""):
        self.streams.append((login, game, viewers, donation_url))
        self.current_login = login

    def update_viewers(self, n):
        self.viewers.append(n)

    def clear_stream(self):
        self.vidages += 1


class _FauxGrille:
    def __init__(self):
        self.grid = self
        self.recus: list[tuple] = []
        self.cache = False
        self.pour_la_palette: list = []

    def update_streamers(self, streamers, sel):
        self.recus.append((list(streamers), sel))

    def set_streamers(self, streamers):
        self.pour_la_palette = list(streamers)

    def hide(self):
        self.cache = True


class _FauxPanel:
    def __init__(self):
        self.recus: list[tuple] = []

    def update_streamers(self, streamers, sel):
        self.recus.append((list(streamers), sel))


class _FauxDataManager:
    def __init__(self, connus=None):
        self._connus = connus or {}

    def get_streamer(self, login):
        return self._connus.get(login)


class _FauxStreamManager:
    def __init__(self):
        self.joues: list[str] = []

    def play(self, login):
        self.joues.append(login)


class _FauxSelection:
    def __init__(self, selection=None):
        self._sel = list(selection or [])
        self.recus: list[list[str]] = []

    def get_selected(self):
        return list(self._sel)

    def set_all(self, logins):
        self.recus.append(list(logins))
        self._sel = list(logins)


# ── sélection d'un stream ────────────────────────────────────────────────────

def test_stream_connu_transmet_ses_metadonnees():
    fs, sm = _FauxFullscreen(), _FauxStreamManager()
    dm = _FauxDataManager({"zerator": _streamer(viewers=4200)})
    main._on_stream_selected("zerator", fs, dm, sm)
    assert fs.streams == [("zerator", "Minecraft", 4200, "https://zevent.fr/dons")]
    assert sm.joues == ["zerator"]


def test_stream_inconnu_bascule_quand_meme():
    """La grille peut demander une chaîne que le DataManager n'a pas encore vue.

    Refuser la bascule laisserait l'utilisateur devant un clic sans effet ;
    on part donc sur des valeurs neutres.
    """
    fs, sm = _FauxFullscreen(), _FauxStreamManager()
    main._on_stream_selected("inconnu", fs, _FauxDataManager(), sm)
    assert fs.streams == [("inconnu", "Just Chatting", 0, "")]
    assert sm.joues == ["inconnu"]


# ── arrivée de données ───────────────────────────────────────────────────────

def test_le_cache_est_remplace_pas_accumule():
    """Le cache sert de source aux rafraîchissements suivants.

    S'il accumulait, les chaînes disparues reviendraient à chaque cycle.
    """
    cache = [_streamer("vieux")]
    fs = _FauxFullscreen()
    main._on_streamers_updated_cb([_streamer("a"), _streamer("b")], None, None,
                                  fs, cache, _FauxSelection())
    assert [s.twitch_login for s in cache] == ["a", "b"]


def test_les_donnees_atteignent_panel_et_grille():
    panel, grille, fs = _FauxPanel(), _FauxGrille(), _FauxFullscreen()
    sel = _FauxSelection(["a"])
    main._on_streamers_updated_cb([_streamer("a")], panel, grille, fs, [], sel)
    assert panel.recus and panel.recus[0][1] == ["a"]
    assert grille.recus and grille.recus[0][1] == ["a"]


@pytest.mark.parametrize("panel,grille", [
    (None, None),                    # mode un écran
    (_FauxPanel(), None),            # pas de grille
    (None, _FauxGrille()),           # pas de panel
])
def test_une_fenetre_absente_ne_casse_rien(panel, grille):
    main._on_streamers_updated_cb([_streamer()], panel, grille,
                                  _FauxFullscreen(), [], _FauxSelection())


def test_selection_vide_transmise_comme_none():
    """`None` et liste vide ne veulent pas dire la même chose en aval."""
    panel, fs = _FauxPanel(), _FauxFullscreen()
    main._on_streamers_updated_cb([_streamer()], panel, None, fs, [],
                                  _FauxSelection([]))
    assert panel.recus[0][1] is None


# ── compteur de spectateurs du plein écran ───────────────────────────────────

def test_le_compteur_suit_la_chaine_affichee():
    fs = _FauxFullscreen()
    fs.current_login = "domingo"
    main._refresh_fullscreen_viewers(
        [_streamer("zerator", viewers=1), _streamer("domingo", viewers=999)], fs)
    assert fs.viewers == [999]


def test_pas_de_compteur_sans_chaine_affichee():
    fs = _FauxFullscreen()
    main._refresh_fullscreen_viewers([_streamer(viewers=999)], fs)
    assert fs.viewers == []


def test_chaine_affichee_absente_du_lot():
    fs = _FauxFullscreen()
    fs.current_login = "absent"
    main._refresh_fullscreen_viewers([_streamer("zerator")], fs)
    assert fs.viewers == []


# ── changement de sélection ──────────────────────────────────────────────────

def test_la_selection_est_enregistree_et_propagee():
    grille, fs = _FauxGrille(), _FauxFullscreen()
    sel = _FauxSelection()
    main._on_grid_selection_changed_cb(["a", "b"], grille, fs,
                                       [_streamer("a")], sel)
    assert sel.recus == [["a", "b"]]
    assert grille.recus[0][1] == ["a", "b"]


def test_vider_la_selection_vide_le_plein_ecran():
    """Plus aucune chaîne choisie : garder la dernière à l'écran serait trompeur."""
    fs = _FauxFullscreen()
    main._on_grid_selection_changed_cb([], None, fs, [], _FauxSelection())
    assert fs.vidages == 1


def test_sans_cache_la_grille_n_est_pas_rafraichie():
    """Rafraîchir avec un cache vide effacerait la grille pour rien."""
    grille = _FauxGrille()
    main._on_grid_selection_changed_cb(["a"], grille, _FauxFullscreen(), [],
                                       _FauxSelection())
    assert grille.recus == []


def test_selection_sans_grille_ne_casse_rien():
    main._on_grid_selection_changed_cb(["a"], None, _FauxFullscreen(),
                                       [_streamer()], _FauxSelection())


# ── icône ────────────────────────────────────────────────────────────────────

def test_l_icone_charge_plusieurs_definitions(qapp):
    """Les PNG sont chargés par taille pour que la barre des tâches ait le rendu
    exact plutôt qu'un rééchantillonnage."""
    icone = main._icone_application()
    assert not icone.isNull()
    assert len(icone.availableSizes()) > 1
