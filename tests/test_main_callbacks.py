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
from PyQt6.QtCore import QObject, pyqtSignal

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


# ── Télécommande : le son passe par la console, pas par la grille ────────────
#
# Une molette de Stream Deck relit le niveau publié avant chaque cran. Quand
# la télécommande appelait la grille directement, la console gardait l'ancienne
# valeur : chaque cran repartait du même point et renvoyait la même consigne.
# Le son sautait une fois, puis la molette tournait dans le vide.

class _PleinEcranSignaux(QObject):
    stream_changed = pyqtSignal(str)
    volume_changed = pyqtSignal(int)
    slot_requested = pyqtSignal(int)
    neighbour_requested = pyqtSignal(int)
    stream_change_requested = pyqtSignal(str)
    etat_bascule = pyqtSignal()
    favori_change = pyqtSignal(str, bool)

    def __init__(self):
        super().__init__()
        self.current_login = "zerator"
        self._volume, self._muted = 50, False
        self.chat_ouvert = False
        self.favori_courant = False

    def run_action(self, cle: str) -> None:
        pass

    def set_volume(self, valeur: int) -> None:
        self._volume = valeur

    def set_muted(self, muet: bool) -> None:
        self._muted = muet


class _CelluleFactice:
    def __init__(self, login):
        self.twitch_login = login
        self.is_online = True
        self._viewers = 1000
        self._audio_pinned = True


class _GrilleSignaux(QObject):
    audio_pins_changed = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self.grid = self
        self._cells = [_CelluleFactice("theguill84")]
        self._last_streamers: list = []
        self.volumes: list[tuple] = []
        self.muets: list[tuple] = []

    def _ordered_for_display(self, cellules):
        return list(cellules)

    def set_cell_volume(self, login, valeur):
        self.volumes.append((login, valeur))

    def set_cell_muted(self, login, muet):
        self.muets.append((login, muet))


class _ConsoleSignaux(QObject):
    cell_volume_changed = pyqtSignal(str, int)
    cell_mute_changed = pyqtSignal(str, bool)
    main_volume_changed = pyqtSignal(int)
    favori_change = pyqtSignal(str, bool)

    def __init__(self):
        super().__init__()
        self.reglages: list[tuple] = []
        self.repeints: list[str] = []
        self._niveaux: dict = {}

    def regler_mixage(self, login, valeur):
        self.reglages.append((login, valeur))
        self._niveaux[login] = (valeur, False)
        self.cell_volume_changed.emit(login, valeur)

    def couper_mixage(self, login, muet):
        niveau = self._niveaux.get(login, (100, False))[0]
        self._niveaux[login] = (niveau, muet)
        self.cell_mute_changed.emit(login, muet)

    def niveaux_de_mixage(self):
        return dict(self._niveaux)

    def rafraichir_favori(self, login):
        self.repeints.append(login)


class _DonneesSignaux(QObject):
    streamers_updated = pyqtSignal(list)


@pytest.fixture
def telecommande_branchee(monkeypatch):
    """La télécommande câblée sur des doublures, sans ouvrir de port."""
    from core import remote_api

    monkeypatch.setattr(remote_api.RemoteAPI, "demarrer",
                        lambda self, port=0: True)
    pieces = {"grille": _GrilleSignaux(), "console": _ConsoleSignaux(),
              "plein": _PleinEcranSignaux(), "donnees": _DonneesSignaux()}

    def brancher(avec_console=True):
        pieces["api"] = main._brancher_telecommande(
            pieces["grille"], pieces["console"] if avec_console else None,
            pieces["plein"], pieces["donnees"])
        return pieces

    return brancher


def test_le_son_d_une_chaine_passe_par_la_console(telecommande_branchee):
    pieces = telecommande_branchee()
    pieces["api"].volume_chaine_demande.emit("theguill84", 40)

    assert pieces["console"].reglages == [("theguill84", 40)]
    assert pieces["grille"].volumes == [], \
        "la console a été court-circuitée : elle garderait l'ancien niveau"


def test_sans_console_la_grille_reprend_la_main(telecommande_branchee):
    """Mode un écran : il n'y a pas de panel, donc pas de console."""
    pieces = telecommande_branchee(avec_console=False)
    pieces["api"].volume_chaine_demande.emit("theguill84", 40)

    assert pieces["grille"].volumes == [("theguill84", 40)]


def test_le_niveau_regle_est_republie(telecommande_branchee):
    """C'est ce niveau que la molette relit avant le cran suivant."""
    pieces = telecommande_branchee()
    pieces["api"].volume_chaine_demande.emit("theguill84", 40)

    cellules = pieces["api"]._dernier_etat["cellules"]
    niveaux = {c["login"]: c["volume"] for c in cellules}
    assert niveaux["theguill84"] == 40


def test_deux_crans_de_suite_descendent_bien_deux_fois(telecommande_branchee):
    """La régression telle qu'elle se voyait sur le boîtier."""
    pieces = telecommande_branchee()
    for _cran in range(2):
        etat = pieces["api"]._dernier_etat["cellules"]
        niveau = next(c["volume"] for c in etat if c["login"] == "theguill84")
        pieces["api"].volume_chaine_demande.emit("theguill84", niveau - 5)

    assert [v for _lg, v in pieces["console"].reglages] == [95, 90]


def test_couper_une_chaine_passe_aussi_par_la_console(telecommande_branchee):
    pieces = telecommande_branchee()
    pieces["api"].muet_chaine_demande.emit("theguill84", True)

    cellules = pieces["api"]._dernier_etat["cellules"]
    assert next(c["muet"] for c in cellules if c["login"] == "theguill84") is True
    assert pieces["grille"].muets == []


def test_l_etat_porte_le_chat_et_le_favori(telecommande_branchee):
    """Deux touches du boîtier les affichent ; sans eux elles restent fixes."""
    pieces = telecommande_branchee()
    plein = pieces["plein"]
    plein.chat_ouvert, plein.favori_courant = True, True
    plein.etat_bascule.emit()

    etat = pieces["api"]._dernier_etat
    assert etat["chat"] is True
    assert etat["favori"] is True


def test_une_bascule_republie_aussitot(telecommande_branchee):
    """Sans republication, la touche garderait l'état d'avant le clic."""
    pieces = telecommande_branchee()
    assert pieces["api"]._dernier_etat["favori"] is False

    pieces["plein"].favori_courant = True
    pieces["plein"].etat_bascule.emit()
    assert pieces["api"]._dernier_etat["favori"] is True


def test_l_etoile_posee_au_boitier_revient_sur_la_carte(telecommande_branchee):
    """Sans ce second sens, les deux moitiés affichaient l'inverse.

    Le favori se pose aussi au clavier et depuis le Stream Deck ; la carte du
    panel gardait alors son étoile creuse.
    """
    pieces = telecommande_branchee()
    pieces["plein"].favori_change.emit("theguill84", True)

    assert pieces["console"].repeints == ["theguill84"]


# ── Quelles chaînes on date ─────────────────────────────────────────────────
#
# Le classement des stats en montre trois cents : une colonne « Depuis » trouée
# sur les trois quarts des lignes passe pour cassée, pas pour économe.

def test_toutes_les_chaines_en_direct_sont_datees():
    plein = _PleinEcranSignaux()
    plein.current_login = "zerator"
    grille = _GrilleSignaux()

    class _Selection:
        @staticmethod
        def get_selected():
            return ["moman"]

    en_ligne = _streamer("aypierre")
    hors_ligne = _streamer("dart0is", online=False)
    logins = main._logins_a_dater(grille, plein, _Selection(),
                                  [en_ligne, hors_ligne])

    assert "aypierre" in logins, "une chaîne en direct doit être datée"
    assert "dart0is" not in logins, "une chaîne éteinte n'a pas de durée"
    assert "zerator" in logins and "moman" in logins


def test_sans_streamers_on_date_quand_meme_ce_qu_on_regarde():
    """Le premier sondage n'a pas encore répondu : la grille, elle, est là."""
    plein = _PleinEcranSignaux()
    plein.current_login = "zerator"

    class _Selection:
        @staticmethod
        def get_selected():
            return []

    assert main._logins_a_dater(None, plein, _Selection(), None) == ["zerator"]
