# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Les chemins que les tests d'origine laissaient de côté.

Ce fichier complète `test_grid_widget.py`, `test_mpv_widget.py`,
`test_command_palette.py`, `test_single.py`, `test_windows_grid.py` et les deux
fichiers du HypeWatcher. Il ne rejoue rien de ce qu'ils couvrent : il prend
uniquement ce qui restait dans l'ombre, et ce qui restait dans l'ombre est
presque toujours du même genre — le geste qui échoue, l'entrée mal formée, la
plateforme sur laquelle on ne teste pas, le repli quand une dépendance
optionnelle manque.

Les précautions valent pour tout le fichier :

- aucun lecteur mpv réel, aucun sous-processus, aucun socket, aucun thread ;
- `HypeWatcher` est un QThread : seules ses méthodes sont appelées, il n'est
  jamais démarré, et son horloge est figée ;
- un widget n'est jamais détaché de son parent alors qu'il est encore visible :
  ce serait une fenêtre de plus sur le bureau (cf.
  `test_pas_de_fenetres_parasites.py`).

Note d'import : `windows.single` tire `windows.panel`, qui importe
QtWebEngineWidgets — Qt exige que ce module soit chargé avant la création du
QApplication. L'import reste donc en tête de fichier.
"""

from __future__ import annotations

import builtins
import importlib.util
import os
import pathlib
import socket
import sys
import types

import pytest
from PyQt6.QtCore import QEvent, QMimeData, QPoint, QPointF, QSize, Qt
from PyQt6.QtGui import (
    QDragEnterEvent,
    QDragMoveEvent,
    QKeyEvent,
    QMouseEvent,
    QResizeEvent,
)
from PyQt6.QtWidgets import QMenu, QWidget

import core.hype_watcher as hw
from core import favorites
from widgets import command_palette as cp
from widgets import grid_widget as G
from widgets import mpv_widget
from windows import grid as grid_module
from windows import single

RACINE = pathlib.Path(__file__).resolve().parent.parent


# =============================================================================
# Outillage commun à la grille
# =============================================================================

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
        self.arrets = 0
        self.tampon = clip_buffer_secs

    def play_stream(self, login: str, quality: str) -> None:
        self.joue = (login, quality)

    def stop(self) -> None:
        self.joue = None
        self.arrets += 1

    def set_mute(self, muet: bool) -> None:
        pass

    def set_volume(self, volume: int) -> None:
        pass

    def set_clip_buffer(self, secs: int) -> None:
        self.tampon = secs

    def save_clip(self, secs: int, directory: str) -> str | None:
        return f"{directory}/faux_clip.ts" if directory else None


class _QTimerSansDifferer(G.QTimer):
    """QTimer dont `singleShot` ne rappelle jamais.

    Les rappels différés de la grille se réveilleraient pendant un test suivant,
    sur des widgets déjà détruits.
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
def grille(qtbot):
    g = G.GridWidget()
    qtbot.addWidget(g)
    g.resize(1600, 900)
    return g


@pytest.fixture
def cellule(qtbot):
    c = G.StreamCell()
    qtbot.addWidget(c)
    c.resize(320, 180)
    return c


def _peupler(grille, *couples, online: bool = True) -> None:
    grille.set_streams([
        {"login": lg, "viewers": v, "online": online} for lg, v in couples
    ])


def _souris(type_, point, bouton=Qt.MouseButton.LeftButton,
            boutons=Qt.MouseButton.NoButton) -> QMouseEvent:
    pos = QPointF(point)
    return QMouseEvent(type_, pos, pos, bouton, boutons,
                       Qt.KeyboardModifier.NoModifier)


# =============================================================================
# widgets/grid_widget.py — repli sans qtawesome
# =============================================================================

def test_sans_qtawesome_le_toast_garde_des_boutons_lisibles(grille, monkeypatch):
    """qtawesome est optionnel : sans lui, deux boutons vides et muets.

    Un `QPushButton` sans icône ni texte occupe sa place mais ne montre rien —
    on ne saurait plus lequel garde le moment et lequel le rejoue.
    """
    monkeypatch.setattr(G, "_QTA_OK", False)
    _peupler(grille, ("a", 10))
    grille._cell_map["a"]._clip_secs = 60
    grille.show_hype_toast(grille._cells.index(grille._cell_map["a"]),
                           "Ça s'emballe", 0.9)
    toast = grille.findChildren(G._HypeToast)[-1]
    textes = [b.text() for b in toast.findChildren(G.QPushButton)]
    assert "●" in textes and "↺" in textes


def test_sans_qtawesome_l_epingle_audio_reste_visible(qtbot, monkeypatch):
    """U+1F50A ne rend aucun glyphe hors Windows : l'indicateur occupait zéro
    pixel et plus rien ne signalait la cellule dont on entend le son."""
    monkeypatch.setattr(G, "_QTA_OK", False)
    c = G.StreamCell()
    qtbot.addWidget(c)
    assert c._pin_lbl.text() == "●"


def test_sans_qtawesome_le_menu_garde_ses_entrees(qtbot, monkeypatch):
    """L'icône est un agrément ; le libellé, lui, ne doit jamais disparaître."""
    monkeypatch.setattr(G, "_QTA_OK", False)
    menu = QMenu()
    qtbot.addWidget(menu)
    action = G.StreamCell._ajouter_action(menu, "Épingler l'audio",
                                          G._ICONE_SON, "#e0e0e0")
    assert action.text() == "Épingler l'audio"
    assert action.icon().isNull(), "aucune icône inventée sans qtawesome"


# =============================================================================
# widgets/grid_widget.py — l'anneau d'attente
# =============================================================================

def test_l_anneau_d_attente_annonce_la_chaine_qu_on_attend(qtbot):
    """Vingt-cinq cellules noires se ressemblent toutes.

    Sans le login peint sous l'anneau, on ne sait pas laquelle des chaînes
    demandées est encore en train de se résoudre.
    """
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.resize(320, 180)
    voile = G.LoadingOverlay(parent)
    voile.setGeometry(0, 0, 320, 180)
    voile.show_overlay("zerator")
    assert voile._login == "zerator"
    voile.paintEvent(None)      # ne doit pas lever : le peintre est complet
    voile.hide_overlay()


def test_l_anneau_d_attente_se_peint_aussi_sans_login(qtbot):
    """L'anneau apparaît avant qu'on sache quelle chaîne il attend."""
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.resize(200, 120)
    voile = G.LoadingOverlay(parent)
    voile.setGeometry(0, 0, 200, 120)
    voile.paintEvent(None)


def test_un_anneau_sans_surface_ne_se_peint_pas(qtbot):
    """Une cellule pas encore disposée a une taille nulle : QPainter y échoue
    bruyamment, et le message d'erreur revient à chaque rafraîchissement."""
    parent = QWidget()
    qtbot.addWidget(parent)
    voile = G.LoadingOverlay(parent)
    voile.setGeometry(0, 0, 0, 0)
    voile.paintEvent(None)      # sort avant de créer le peintre


# =============================================================================
# widgets/grid_widget.py — l'attente de la première image
# =============================================================================

def test_une_attente_armee_pour_une_autre_chaine_est_ignoree(cellule):
    """mpv signale la résolution d'une URL demandée AVANT le dernier changement
    de cellule : armer l'attente sur ce retard tuerait le flux courant."""
    cellule.set_stream("b", 10)
    cellule._armer_attente("a")
    assert cellule._attente_image is None


def test_une_attente_expiree_sans_flux_en_cours_se_tait(cellule):
    """La cellule a déjà été libérée : abandonner une seconde fois remettrait
    le compteur d'échecs en marche pour une chaîne qui n'y est plus."""
    cellule.set_stream("a", 10)
    cellule._streaming_login = ""
    cellule._echecs_demarrage = 0
    cellule._sur_attente_expiree()
    assert cellule._echecs_demarrage == 0


def test_une_attente_expiree_abandonne_le_demarrage(cellule):
    """Un anneau qui tourne indéfiniment affirme qu'il se passe quelque chose."""
    cellule.set_stream("a", 10)
    cellule._streaming_login = "a"
    cellule._sur_attente_expiree()
    assert cellule._streaming_login == ""
    assert cellule._echecs_demarrage == 1


def test_abandonner_une_cellule_vide_ne_fait_rien(cellule):
    """Une résolution en retard peut arriver après la libération de la case."""
    cellule._echecs_demarrage = 0
    cellule._abandonner_demarrage()
    assert cellule._echecs_demarrage == 0


def test_abandonner_coupe_le_lecteur_deja_cree(cellule):
    """Le lecteur continuerait sinon à tirer sur le réseau pour rien."""
    lecteur = _FauxMpv()
    cellule.set_stream("a", 10)
    cellule._streaming_login = "a"
    cellule._mpv = lecteur
    cellule._abandonner_demarrage()
    assert lecteur.arrets == 1


# =============================================================================
# widgets/grid_widget.py — glisser-déposer
# =============================================================================

def test_une_cellule_hors_grille_ne_trouve_personne_a_qui_parler(qtbot):
    """La cellule remonte ses parents jusqu'à en trouver un qui sache déplacer.

    Sortie de la grille — dans un test, ou dans une disposition future — la
    remontée doit s'arrêter sur None plutôt que sur une exception.
    """
    grand_parent = QWidget()
    qtbot.addWidget(grand_parent)
    intermediaire = QWidget(grand_parent)
    c = G.StreamCell(intermediaire)
    assert c._grid() is None


def test_un_clic_droit_n_amorce_aucun_glissement(cellule):
    """Le bouton droit ouvre le menu : mémoriser l'appui ferait aussi partir
    un glisser-déposer dès le premier mouvement."""
    cellule.mousePressEvent(_souris(QEvent.Type.MouseButtonPress, QPoint(5, 5),
                                    Qt.MouseButton.RightButton))
    assert cellule._press_pos is None


@pytest.mark.parametrize("motif", ["sans_bouton", "sans_appui", "deja_en_cours",
                                   "cellule_vide", "grille_figee"])
def test_le_glissement_ne_part_pas_hors_de_ses_conditions(grille, motif):
    """Cinq verrous, chacun pour une raison distincte.

    Un glissement parti par erreur remplace une chaîne par une autre : le
    geste est destructeur, il ne doit se déclencher que délibérément.
    """
    grille.set_sort_mode("manual")
    _peupler(grille, ("a", 10), ("b", 20))
    c = grille._cell_map["a"]
    c._press_pos = QPoint(0, 0)
    boutons = Qt.MouseButton.LeftButton
    if motif == "sans_bouton":
        boutons = Qt.MouseButton.NoButton
    elif motif == "sans_appui":
        c._press_pos = None
    elif motif == "deja_en_cours":
        c._dragging = True
    elif motif == "cellule_vide":
        c._twitch_login = ""
    elif motif == "grille_figee":
        grille.set_sort_mode("viewers")

    c.mouseMoveEvent(_souris(QEvent.Type.MouseMove, QPoint(400, 400),
                             Qt.MouseButton.NoButton, boutons))
    assert c._dragging is (motif == "deja_en_cours")


def test_un_fremissement_de_souris_n_est_pas_un_glissement(grille):
    """En dessous du seuil système, l'utilisateur a cliqué, pas glissé."""
    grille.set_sort_mode("manual")
    _peupler(grille, ("a", 10), ("b", 20))
    c = grille._cell_map["a"]
    c._press_pos = QPoint(10, 10)
    c.mouseMoveEvent(_souris(QEvent.Type.MouseMove, QPoint(11, 11),
                             Qt.MouseButton.NoButton,
                             Qt.MouseButton.LeftButton))
    assert c._dragging is False


def test_le_glissement_emporte_le_login_et_une_vignette(grille, monkeypatch):
    """Sans point d'accroche, la vignette colle son coin au curseur et masque
    la case visée : on ne sait plus ce qu'on désigne."""
    poses: list = []

    class _FauxDrag:
        def __init__(self, source) -> None:
            self.source = source
            self.mime = None
            self.accroche = None

        def setMimeData(self, mime) -> None:      # noqa: N802 (API Qt)
            self.mime = mime

        def setPixmap(self, pm) -> None:          # noqa: N802
            self.pixmap = pm

        def setHotSpot(self, point) -> None:      # noqa: N802
            self.accroche = point

        def exec(self, action):
            poses.append(self)
            return action

    monkeypatch.setattr(G, "QDrag", _FauxDrag)
    grille.set_sort_mode("manual")
    _peupler(grille, ("a", 10), ("b", 20))
    c = grille._cell_map["a"]
    c.resize(320, 180)
    c._press_pos = QPoint(160, 90)
    c.mouseMoveEvent(_souris(QEvent.Type.MouseMove, QPoint(300, 170),
                             Qt.MouseButton.NoButton,
                             Qt.MouseButton.LeftButton))
    assert len(poses) == 1
    assert bytes(poses[0].mime.data(G.StreamCell._MIME)) == b"a"
    assert poses[0].accroche is not None, "vignette accrochée au point saisi"
    assert c._dragging is True


def test_un_glissement_venu_d_ailleurs_ne_souligne_rien(cellule):
    """La grille accepte aussi des dépôts venus du bureau (fichiers, texte) :
    les mettre en évidence promettrait une action qui n'existe pas."""
    autre = QMimeData()
    autre.setText("https://twitch.tv/zerator")
    cellule.set_stream("a", 10)
    style_avant = cellule.styleSheet()
    cellule.dragEnterEvent(QDragEnterEvent(
        QPoint(5, 5), Qt.DropAction.MoveAction, autre,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    assert cellule.styleSheet() == style_avant


def test_un_survol_venu_d_ailleurs_n_est_pas_accepte(cellule):
    """Accepter le survol afficherait un curseur de dépôt là où rien ne tombera."""
    autre = QMimeData()
    autre.setText("bonjour")
    evenement = QDragMoveEvent(
        QPoint(5, 5), Qt.DropAction.MoveAction, autre,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    evenement.setAccepted(False)
    cellule.dragMoveEvent(evenement)
    assert evenement.isAccepted() is False


# =============================================================================
# widgets/grid_widget.py — menu contextuel
# =============================================================================

@pytest.fixture
def menu_sans_boucle(monkeypatch):
    """Empêche `QMenu.exec` d'ouvrir une boucle d'événements bloquante.

    Un menu contextuel réel attendrait un clic qui ne viendra jamais : le test
    resterait suspendu jusqu'au délai de garde.
    """
    ouverts: list = []
    monkeypatch.setattr(QMenu, "exec",
                        lambda self, *a, **k: ouverts.append(self))
    return ouverts


def _menu_contextuel(cellule) -> QEvent:
    from PyQt6.QtGui import QContextMenuEvent
    return QContextMenuEvent(QContextMenuEvent.Reason.Mouse,
                             QPoint(5, 5), QPoint(500, 500))


@pytest.mark.parametrize("login,en_ligne", [("", True), ("a", False)])
def test_pas_de_menu_sur_une_cellule_sans_flux(cellule, menu_sans_boucle,
                                               login, en_ligne):
    """Toutes les entrées du menu portent sur une chaîne : sans chaîne en
    direct, elles n'auraient aucune cible."""
    if login:
        cellule.set_stream(login, 10, online=en_ligne)
    cellule.contextMenuEvent(_menu_contextuel(cellule))
    assert menu_sans_boucle == []


def test_le_menu_propose_d_epingler_l_audio(cellule, menu_sans_boucle):
    cellule.set_stream("a", 10)
    recus: list[str] = []
    cellule.audio_pin_requested.connect(recus.append)
    cellule.contextMenuEvent(_menu_contextuel(cellule))
    assert len(menu_sans_boucle) == 1
    libelles = [a.text() for a in menu_sans_boucle[0].actions()]
    assert "Épingler l'audio" in libelles
    next(a for a in menu_sans_boucle[0].actions()
         if a.text() == "Épingler l'audio").trigger()
    assert recus == ["a"]


def test_le_menu_propose_de_couper_un_audio_deja_epingle(cellule,
                                                         menu_sans_boucle):
    """Le libellé doit décrire ce que le clic VA faire, pas l'état courant."""
    cellule.set_stream("a", 10)
    cellule._audio_pinned = True
    cellule.contextMenuEvent(_menu_contextuel(cellule))
    libelles = [a.text() for a in menu_sans_boucle[0].actions()]
    assert "Couper l'audio" in libelles


def test_la_cellule_du_plein_ecran_n_offre_pas_d_epingler_son_audio(
        cellule, menu_sans_boucle):
    """Ce serait un doublon inaudible : le son sort déjà du plein écran.

    L'entrée reste affichée mais désactivée, pour expliquer pourquoi elle
    manque plutôt que de la faire disparaître sans un mot.
    """
    cellule.set_stream("a", 10)
    cellule.set_active(True)
    cellule.contextMenuEvent(_menu_contextuel(cellule))
    entree = menu_sans_boucle[0].actions()[0]
    assert entree.isEnabled() is False
    assert "plein écran" in entree.text()


def test_le_menu_bascule_le_favori(cellule, menu_sans_boucle, monkeypatch):
    bascules: list[str] = []
    monkeypatch.setattr(G.favorites, "is_favorite", lambda lg: False)
    monkeypatch.setattr(G.favorites, "toggle", bascules.append)
    cellule.set_stream("a", 10)
    cellule.contextMenuEvent(_menu_contextuel(cellule))
    next(a for a in menu_sans_boucle[0].actions()
         if a.text() == "Mettre en favori").trigger()
    assert bascules == ["a"]


def test_le_menu_propose_de_retirer_un_favori_deja_pose(cellule,
                                                        menu_sans_boucle,
                                                        monkeypatch):
    monkeypatch.setattr(G.favorites, "is_favorite", lambda lg: True)
    cellule.set_stream("a", 10)
    cellule.contextMenuEvent(_menu_contextuel(cellule))
    libelles = [a.text() for a in menu_sans_boucle[0].actions()]
    assert "Retirer des favoris" in libelles


def test_sans_clips_actives_le_menu_n_en_parle_pas(cellule, menu_sans_boucle):
    """Proposer de garder un moment sans tampon rendrait un fichier vide."""
    cellule.set_stream("a", 10)
    cellule._clip_secs = 0
    cellule.contextMenuEvent(_menu_contextuel(cellule))
    libelles = " ".join(a.text() for a in menu_sans_boucle[0].actions())
    assert "dernières secondes" not in libelles


def test_avec_les_clips_le_menu_offre_garder_et_revoir(cellule,
                                                       menu_sans_boucle):
    clips: list[str] = []
    replays: list[str] = []
    cellule.clip_requested.connect(clips.append)
    cellule.replay_requested.connect(replays.append)
    cellule.set_stream("a", 10)
    cellule._clip_secs = 60
    cellule.contextMenuEvent(_menu_contextuel(cellule))
    for action in menu_sans_boucle[0].actions():
        if action.text().startswith("Garder les"):
            action.trigger()
        elif action.text().startswith("Revoir les"):
            action.trigger()
    assert clips == ["a"] and replays == ["a"]


# =============================================================================
# widgets/grid_widget.py — divers
# =============================================================================

def test_le_toast_d_objectif_s_efface_de_lui_meme(grille):
    """Sans effacement, les toasts s'empilent : un palier de cagnotte en
    déclenche des dizaines en quelques minutes."""
    _peupler(grille, ("a", 10))
    grille.goal_achieved_flash("a", "Manger un piment")
    toast = grille.findChildren(G._GoalAchievedToast)[-1]
    toast._start_fade()
    assert toast.graphicsEffect() is not None


def test_la_fin_d_un_flux_inconnu_est_enregistree_sans_toucher_aux_cellules(
        grille):
    """mpv peut signaler la fin d'une chaîne déjà retirée de la grille.

    Le délai de garde doit tout de même être posé : sinon le placement suivant
    la remettrait aussitôt, et elle échouerait de nouveau.
    """
    _peupler(grille, ("a", 10))
    grille._on_stream_ended("jamais-affiche")
    assert "jamais-affiche" in grille._ended
    assert grille._cell_map["a"].twitch_login == "a"


def test_un_streamer_retenu_mais_absent_des_donnees_est_laisse_en_place(grille):
    """La sélection et les données arrivent de deux sources distinctes.

    Une chaîne encore sélectionnée mais absente du dernier relevé ne doit ni
    faire lever, ni écraser les autres mises à jour de la même passe.
    """
    _peupler(grille, ("a", 10), ("b", 20))

    class _S:
        def __init__(self, login, viewers):
            self.twitch_login = login
            self.viewers = viewers
            self.online = True

    grille._appliquer_cellules({"b": _S("b", 999)}, {"a", "b"}, "360p")
    assert grille._cell_map["a"].twitch_login == "a"
    assert grille._cell_map["b"].twitch_login == "b"


def test_la_sauvegarde_d_un_clip_part_hors_du_fil_graphique(grille,
                                                            monkeypatch):
    """Écrire le clip dans le fil graphique fige l'interface le temps du
    téléchargement — plusieurs secondes, vingt-cinq lecteurs en cours."""
    lances: list[dict] = []

    class _FauxFil:
        def __init__(self, *, target, args, daemon, name) -> None:
            lances.append({"cible": target, "args": args,
                           "daemon": daemon, "nom": name})

        def start(self) -> None:
            pass

    monkeypatch.setattr(G, "threading",
                        types.SimpleNamespace(Thread=_FauxFil))
    _peupler(grille, ("a", 10))
    grille._lancer_clip("a", grille._cell_map["a"])
    assert len(lances) == 1
    assert lances[0]["daemon"] is True
    assert lances[0]["nom"] == "clip-a"


def test_une_rangee_vide_arrete_le_placement(grille, monkeypatch):
    """Garde-fou : la disposition et le nombre de cellules doivent concorder.

    `_compute_grid_dims` promet `rows == ceil(n / cols)`. Si cette promesse
    venait à se rompre, la boucle de placement lirait `row_ys[row_idx]` pour une
    rangée sans occupant — et le calcul de hauteur suivant partirait sur une
    liste plus courte qu'annoncé.
    """
    monkeypatch.setattr(G, "_compute_grid_dims", lambda n: (5, 1))
    _peupler(grille, ("a", 10), ("b", 20))
    grille._reposition_cells()      # ne doit ni lever, ni sortir de la liste
    assert grille._cell_map["a"].width() > 0


def test_une_cellule_sans_surface_glisse_sans_point_d_accroche(grille,
                                                               monkeypatch):
    """Une cellule pas encore disposée mesure zéro : la mise à l'échelle de la
    vignette diviserait par cette largeur."""
    accroches: list = []

    class _FauxDrag:
        def __init__(self, source) -> None:
            pass

        def setMimeData(self, mime) -> None:      # noqa: N802 (API Qt)
            pass

        def setPixmap(self, pm) -> None:          # noqa: N802
            pass

        def setHotSpot(self, point) -> None:      # noqa: N802
            accroches.append(point)

        def exec(self, action):
            return action

    monkeypatch.setattr(G, "QDrag", _FauxDrag)
    grille.set_sort_mode("manual")
    _peupler(grille, ("a", 10), ("b", 20))
    c = grille._cell_map["a"]
    c.setFixedSize(0, 0)
    c._press_pos = QPoint(0, 0)
    c.mouseMoveEvent(_souris(QEvent.Type.MouseMove, QPoint(300, 300),
                             Qt.MouseButton.NoButton,
                             Qt.MouseButton.LeftButton))
    assert accroches == [], "aucune accroche calculée sur une surface nulle"


def test_sans_qtawesome_la_grille_se_charge_quand_meme(monkeypatch):
    """qtawesome ne se contente pas d'échouer à l'import : il charge des
    polices et peut casser autrement.

    Un `except ImportError` trop étroit laissait `_QTA_OK` non défini, et le
    démarrage tombait sur un NameError une fois sur six.
    """
    vrai_import = builtins.__import__

    def _refuse_qtawesome(nom, *a, **k):
        if nom == "qtawesome":
            raise RuntimeError("police introuvable")
        return vrai_import(nom, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _refuse_qtawesome)
    module = _charger_a_neuf("grid_widget_sans_qtawesome",
                             "widgets/grid_widget.py")
    assert module._QTA_OK is False
    assert module.qta is None


# =============================================================================
# widgets/mpv_widget.py — méthodes d'instance sans lecteur
# =============================================================================

@pytest.fixture
def widget_inerte(qtbot, monkeypatch):
    """MpvWidget construit sans libmpv : aucun lecteur, aucune fenêtre native."""
    monkeypatch.setattr(mpv_widget, "_MPV_AVAILABLE", False)
    w = mpv_widget.MpvWidget()
    qtbot.addWidget(w)
    return w


class _LecteurMinimal:
    """Le strict nécessaire pour interroger position et temps restant."""

    def __init__(self, time_pos=None, time_remaining=None,
                 casse: bool = False) -> None:
        self._time_pos = time_pos
        self._time_remaining = time_remaining
        self._casse = casse

    @property
    def time_pos(self):
        if self._casse:
            raise RuntimeError("lecteur en cours de démontage")
        return self._time_pos

    @property
    def time_remaining(self):
        if self._casse:
            raise RuntimeError("lecteur en cours de démontage")
        return self._time_remaining


def test_sans_lecteur_aucun_observateur_n_est_pose(widget_inerte):
    """Poser un observateur sur None lèverait à chaque cellule vide de la
    grille, soit vingt-cinq fois au démarrage."""
    widget_inerte._player = None
    widget_inerte._brancher_observateurs()
    assert widget_inerte._time_pos_cb is None


@pytest.mark.parametrize("valeur", [None, 0.0, -1.0])
def test_une_position_nulle_ne_declare_pas_la_lecture_commencee(widget_inerte,
                                                                valeur):
    """mpv publie `time-pos` avant la première image, à zéro ou à None :
    y croire ferait disparaître l'anneau d'attente trop tôt."""
    lecteur = _FauxLecteurObservable()
    widget_inerte._player = lecteur
    widget_inerte._brancher_observateurs()
    dict(lecteur.observes)["time-pos"](None, valeur)
    assert widget_inerte._time_pos_started is False


def test_la_premiere_image_ne_se_signale_qu_une_fois(widget_inerte):
    """mpv émet cette propriété à CHAQUE image : sur vingt-cinq cellules cela
    faisait des milliers d'appels Python par seconde pour une seule
    information."""
    lecteur = _FauxLecteurObservable()
    widget_inerte._player = lecteur
    widget_inerte._brancher_observateurs()
    rappel = dict(lecteur.observes)["time-pos"]
    rappel(None, 1.0)
    rappel(None, 2.0)
    assert lecteur.desabonnes, "on se désabonne dès la première image"


@pytest.mark.parametrize("valeur,attendu", [(42.5, 42.5), (0.0, 0.0),
                                            (None, None)])
def test_la_position_de_lecture_est_rendue_telle_quelle(widget_inerte,
                                                        valeur, attendu):
    """Elle ne vaut que comparée à elle-même : sur un fragment repris chez
    Twitch elle part de l'horodatage absolu du direct."""
    widget_inerte._player = _LecteurMinimal(time_pos=valeur)
    assert widget_inerte.position() == attendu


def test_sans_lecteur_la_position_est_inconnue(widget_inerte):
    assert widget_inerte.position() is None


def test_un_lecteur_en_cours_de_demontage_ne_fait_pas_lever(widget_inerte):
    """Le replay interroge la position juste après un `stop()` : la propriété
    peut avoir disparu sous les pieds de l'appelant."""
    widget_inerte._player = _LecteurMinimal(casse=True)
    assert widget_inerte.position() is None
    assert widget_inerte.restant() is None


@pytest.mark.parametrize("valeur,attendu", [(12.0, 12.0), (None, None)])
def test_le_temps_restant_est_rendu_tel_quel(widget_inerte, valeur, attendu):
    """`duration` ne peut pas servir : sur un fragment repris chez Twitch il
    porte l'horodatage absolu du direct. `time-remaining` est un écart."""
    widget_inerte._player = _LecteurMinimal(time_remaining=valeur)
    assert widget_inerte.restant() == attendu


def test_sans_lecteur_le_temps_restant_est_inconnu(widget_inerte):
    assert widget_inerte.restant() is None


def test_hors_macos_le_redimensionnement_ne_force_pas_de_repaint(widget_inerte,
                                                                 monkeypatch):
    """Sous Windows et X11, c'est mpv qui dessine dans sa propre fenêtre :
    un repaint Qt de plus par redimensionnement serait payé pour rien."""
    monkeypatch.setattr(mpv_widget, "_RENDER_API", False)
    peintures: list[int] = []
    monkeypatch.setattr(widget_inerte, "update", lambda: peintures.append(1))
    widget_inerte.resizeEvent(QResizeEvent(QSize(320, 180), QSize(0, 0)))
    assert peintures == []


class _FauxLecteurObservable:
    """Enregistre les observations posées par le widget."""

    def __init__(self) -> None:
        self.observes: list[tuple[str, object]] = []
        self.desabonnes: list[tuple[str, object]] = []

    def observe_property(self, nom: str, cb) -> None:
        self.observes.append((nom, cb))

    def unobserve_property(self, nom: str, cb) -> None:
        self.desabonnes.append((nom, cb))


# =============================================================================
# widgets/mpv_widget.py — le module lui-même, sur d'autres plateformes
# =============================================================================

def _charger_a_neuf(nom: str = "mpv_widget_rejoue",
                    source: str = "widgets/mpv_widget.py"):
    """Réexécute un fichier du dépôt dans un module SÉPARÉ.

    Le préambule d'un module décide, à l'import, du socle graphique, de la
    présence des dépendances optionnelles et du mode dégradé. Ces décisions ne
    se rejouent pas sur le module déjà chargé — et le recharger en place
    remplacerait ses classes sous les pieds de tous les autres tests. On en
    fabrique donc une copie indépendante, jamais publiée dans `sys.modules`.
    """
    chemin = RACINE.joinpath(*source.split("/"))
    spec = importlib.util.spec_from_file_location(nom, chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sur_macos_la_video_passe_par_notre_propre_rendu(monkeypatch):
    """mpv n'implémente `--wid` que sur X11, win32 et Android.

    Sur macOS il ouvrirait sa PROPRE fenêtre, hors de la grille : le widget
    doit y devenir un QOpenGLWidget dans lequel nous dessinons les images.
    """
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget

    monkeypatch.setattr(sys, "platform", "darwin")
    module = _charger_a_neuf("mpv_widget_macos")
    assert module._RENDER_API is True
    assert issubclass(module.MpvWidget, QOpenGLWidget)


def test_une_libmpv_livree_avec_l_application_est_annoncee_a_ctypes(
        monkeypatch, tmp_path):
    """python-mpv appelle `ctypes.util.find_library('mpv')`, qui ne regarde QUE
    les emplacements système.

    Une libmpv livrée dans le paquet lui est donc invisible, et une application
    signée ne peut pas compter sur DYLD_LIBRARY_PATH, que macOS efface pour les
    processus durcis. Le module répond lui-même pour ce seul nom.
    """
    import ctypes.util as cu

    (tmp_path / "libmpv.2.dylib").write_bytes(b"")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("core.paths.RESOURCE_ROOT", tmp_path)
    # Enregistré avant le chargement : monkeypatch remettra l'originale, quoi
    # que le module ait posé à la place.
    monkeypatch.setattr(cu, "find_library", cu.find_library)

    module = _charger_a_neuf("mpv_widget_libmpv_livree")
    assert module._LIBMPV_EMBARQUEE == str(tmp_path / "libmpv.2.dylib")
    assert cu.find_library("mpv") == str(tmp_path / "libmpv.2.dylib")
    assert cu.find_library("c") != str(tmp_path / "libmpv.2.dylib"), \
        "les autres bibliothèques restent résolues normalement"


def test_sans_libmpv_le_widget_se_met_en_mode_degrade(monkeypatch):
    """`mpv.py` lève OSError au niveau du module quand la DLL manque.

    Laisser remonter l'erreur empêcherait ZLink de démarrer, alors que tout ce
    qui n'est pas la vidéo — panel, dons, programme — reste utilisable.
    """
    vrai_import = builtins.__import__

    def _refuse_mpv(nom, *a, **k):
        if nom == "mpv":
            raise OSError("libmpv-2.dll introuvable")
        return vrai_import(nom, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _refuse_mpv)
    module = _charger_a_neuf("mpv_widget_sans_libmpv")
    assert module._MPV_AVAILABLE is False
    assert module._mpv_module is None


def test_une_session_wayland_forcee_est_signalee(monkeypatch, caplog):
    """main.py bascule normalement sur xcb tout seul.

    Si ce garde-fou a été contourné, `--wid` ne fonctionne pas et la vidéo
    s'ouvre dans des fenêtres séparées : un avertissement est la seule chance
    de comprendre ce qui se passe.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(os.environ, "XDG_SESSION_TYPE", "wayland")
    monkeypatch.setitem(os.environ, "QT_QPA_PLATFORM", "offscreen")
    with caplog.at_level("WARNING"):
        _charger_a_neuf("mpv_widget_wayland")
    assert any("Wayland" in enr.message for enr in caplog.records)


def test_un_dossier_de_dll_refuse_n_empeche_pas_le_demarrage(monkeypatch):
    """`os.add_dll_directory` échoue si le dossier n'existe pas — c'est le cas
    d'une exécution depuis une archive ou un montage réseau capricieux.

    Ce n'est pas fatal : la DLL peut encore être trouvée par le PATH.
    """
    refus: list[str] = []

    def _refuse(chemin: str):
        refus.append(chemin)
        raise OSError("dossier inaccessible")

    monkeypatch.setattr(os, "add_dll_directory", _refuse, raising=False)
    module = _charger_a_neuf("mpv_widget_dll_refusee")
    assert refus, "le module a bien tenté d'enregistrer son dossier"
    assert module.MpvWidget is not None


def test_sans_add_dll_directory_le_module_se_charge_quand_meme(monkeypatch):
    """La fonction n'existe que sous Windows : ailleurs, le préambule doit
    l'ignorer plutôt que de lever un AttributeError à l'import."""
    monkeypatch.delattr(os, "add_dll_directory", raising=False)
    module = _charger_a_neuf("mpv_widget_sans_dll_dir")
    assert module.MpvWidget is not None


# =============================================================================
# widgets/command_palette.py
# =============================================================================

class _FauxStreamer:
    def __init__(self, login: str, viewers: int = 0, online: bool = True,
                 game: str = "") -> None:
        self.twitch_login = login
        self.display = login
        self.viewers = viewers
        self.online = online
        self.game = game
        self.profile_url = ""


@pytest.fixture
def palette(qtbot):
    parent = QWidget()
    parent.resize(800, 600)
    qtbot.addWidget(parent)
    p = cp.CommandPalette(parent, ["Accueil", "Stats"])
    # Le parent est local : sans cette référence, Python le collecte à la
    # sortie de la fixture et Qt détruit la palette avec lui.
    p._parent_du_test = parent
    p.set_streamers([
        _FauxStreamer("zerator", 5000, game="Just Chatting"),
        _FauxStreamer("domingo", 3000),
    ])
    return p


def _touche_palette(palette, key, modificateurs=Qt.KeyboardModifier.NoModifier):
    return palette.eventFilter(
        palette._input,
        QKeyEvent(QEvent.Type.KeyPress, key, modificateurs))


def test_echap_referme_la_palette(palette):
    """C'est le seul moyen d'en sortir sans rien choisir : la palette recouvre
    le direct."""
    palette.open()
    assert _touche_palette(palette, Qt.Key.Key_Escape) is True
    assert palette.isVisible() is False


def test_les_fleches_deplacent_la_selection(palette):
    """Toute la palette se pilote au clavier : c'est sa raison d'être."""
    palette._refilter("")
    palette._list.setCurrentRow(0)
    assert _touche_palette(palette, Qt.Key.Key_Down) is True
    assert palette._list.currentRow() == 1
    assert _touche_palette(palette, Qt.Key.Key_Up) is True
    assert palette._list.currentRow() == 0


@pytest.mark.parametrize("touche,depart,attendu", [
    (Qt.Key.Key_Up, 0, 0),          # déjà en haut
    (Qt.Key.Key_Down, 1, 1),        # déjà en bas
])
def test_la_selection_ne_sort_pas_de_la_liste(palette, touche, depart,
                                              attendu):
    """Un enroulement ferait sauter du premier au dernier résultat sans qu'on
    l'ait demandé, et l'appui suivant validerait le mauvais."""
    palette._refilter("")
    palette._list.setCurrentRow(depart)
    _touche_palette(palette, touche)
    assert palette._list.currentRow() == attendu


@pytest.mark.parametrize("touche", [Qt.Key.Key_Return, Qt.Key.Key_Enter])
def test_entree_ouvre_le_resultat_selectionne(palette, touche):
    """Entrée du pavé numérique comprise : rien ne dit laquelle on utilise."""
    recus: list[str] = []
    palette.stream_requested.connect(recus.append)
    palette._refilter("zera")
    palette._list.setCurrentRow(0)
    assert _touche_palette(palette, touche) is True
    assert recus == ["zerator"]


def test_ctrl_entree_passe_par_le_filtre_vers_la_grille(palette):
    """Le modificateur doit être lu sur l'événement, pas sur l'état du clavier
    au moment où l'action s'exécute."""
    recus: list[str] = []
    palette.grid_requested.connect(recus.append)
    palette._refilter("zera")
    palette._list.setCurrentRow(0)
    _touche_palette(palette, Qt.Key.Key_Return,
                    Qt.KeyboardModifier.ControlModifier)
    assert recus == ["zerator"]


def test_une_touche_ordinaire_reste_a_la_ligne_de_saisie(palette):
    """Intercepter les lettres empêcherait purement et simplement de taper."""
    assert _touche_palette(palette, Qt.Key.Key_A) is False


def test_un_evenement_d_un_autre_widget_n_est_pas_intercepte(palette):
    """Le filtre est posé sur la seule ligne de saisie : la liste doit garder
    ses propres touches."""
    assert palette.eventFilter(
        palette._list,
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                  Qt.KeyboardModifier.NoModifier)) is False


def test_une_action_choisie_est_relayee_telle_quelle(palette):
    """La palette ne sait pas clipper : elle nomme le geste, la fenêtre l'exécute."""
    recus: list[str] = []
    palette.action_requested.connect(recus.append)
    palette._refilter("moment")
    rang = next(i for i, (kind, _c, _l) in enumerate(palette._results)
                if kind == "action")
    palette._list.setCurrentRow(rang)
    palette._activate()
    assert recus == ["clip"]


def test_une_palette_sans_parent_ne_cherche_pas_a_se_centrer(qtbot):
    """Elle se place par rapport à la fenêtre qui l'héberge.

    Détachée — pendant un démontage d'interface — il n'y a plus rien par
    rapport à quoi se centrer, et lire `parent.width()` lèverait.
    """
    parent = QWidget()
    qtbot.addWidget(parent)
    p = cp.CommandPalette(parent, [])
    assert p.isVisible() is False, "détacher un widget VISIBLE en ferait une fenêtre"
    p.setParent(None)
    qtbot.addWidget(p)
    position = p.pos()
    p._se_placer()
    assert p.pos() == position


def test_la_boite_se_recentre_pendant_la_frappe(palette):
    """Sa hauteur suit le nombre de résultats : sans recentrage, elle
    grandirait vers le bas et finirait hors de l'écran."""
    palette._parent_du_test.show()
    palette.open()
    palette._refilter("zera")
    assert palette.isVisible() is True
    assert palette.pos().y() >= 0
    palette._parent_du_test.hide()


def test_la_photo_d_un_inconnu_est_une_image_vide(palette):
    """La liste des résultats et celle des streamers peuvent se désynchroniser
    entre deux rafraîchissements : demander la photo d'un absent ne doit pas
    lever au milieu du peuplement de la liste."""
    assert palette._photo("jamais-vu").isNull()


def test_une_photo_arrivee_apres_coup_ne_repeint_que_les_streamers(palette):
    """Les onglets et les actions n'ont pas d'icône : leur en poser une
    décalerait leur libellé sans rien montrer."""
    palette._parent_du_test.show()
    palette.set_streamers([_FauxStreamer("statman", 10)])
    palette._refilter("stat")
    palette.show()
    assert palette.isVisible() is True
    palette._redessiner()
    rangs_onglets = [i for i, (kind, _c, _l) in enumerate(palette._results)
                     if kind == "tab"]
    rangs_streamers = [i for i, (kind, _c, _l) in enumerate(palette._results)
                       if kind == "streamer"]
    assert rangs_onglets and rangs_streamers, "le jeu de test mêle les deux"
    assert all(palette._list.item(i).icon().isNull() for i in rangs_onglets)
    palette._parent_du_test.hide()


# =============================================================================
# windows/grid.py
# =============================================================================

@pytest.fixture
def fenetre_grille(qtbot, qapp, monkeypatch):
    """GridWindow sans HypeWatcher, sans réseau et sans plein écran."""
    monkeypatch.setattr(
        grid_module.GridWindow, "_start_hype_watcher",
        lambda self: setattr(self, "_hype_watcher", None))
    w = grid_module.GridWindow(qapp.primaryScreen(), show_on_init=False)
    qtbot.addWidget(w)
    return w


def test_la_palette_alimente_sa_liste_de_streamers(fenetre_grille):
    """Sans cet aiguillage, Ctrl+K devant la grille n'ouvrait qu'une liste vide."""
    streamers = [_FauxStreamer("zerator", 100)]
    fenetre_grille.set_streamers(streamers)
    assert fenetre_grille._palette._streamers == streamers


def test_echap_referme_d_abord_la_palette(fenetre_grille, qtbot):
    """Une seule touche pour deux gestes : tant que la palette est ouverte,
    Échap la ferme ; ce n'est qu'ensuite qu'il quitte la grille."""
    retours: list[int] = []
    fenetre_grille.back_to_panel.connect(lambda: retours.append(1))
    fenetre_grille.show()
    fenetre_grille._palette.open()
    fenetre_grille.keyPressEvent(QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Escape,
        Qt.KeyboardModifier.NoModifier))
    assert fenetre_grille._palette.isVisible() is False
    assert retours == [], "le retour au panel n'est pas déclenché du même coup"
    fenetre_grille.hide()


def test_la_grille_s_ouvre_en_plein_ecran_sur_l_ecran_demande(qtbot, qapp,
                                                              monkeypatch):
    """Sans `setScreen()`, Windows replace la fenêtre sur le moniteur principal.

    La géométrie est posée AVANT `show()` pour que le handle natif naisse au
    bon endroit : l'inverse produit un aller-retour visible d'un écran à
    l'autre.
    """
    monkeypatch.setattr(
        grid_module.GridWindow, "_start_hype_watcher",
        lambda self: setattr(self, "_hype_watcher", None))
    marquees: list = []
    monkeypatch.setattr(grid_module, "mark_fullscreen", marquees.append)
    ecran = qapp.primaryScreen()
    w = grid_module.GridWindow(ecran, show_on_init=True)
    qtbot.addWidget(w)
    assert w.geometry().size() == ecran.geometry().size()
    assert marquees == [w], "la fenêtre est signalée comme plein écran"
    w.hide()


def test_une_grille_sans_handle_natif_ne_leve_pas(qtbot, qapp, monkeypatch):
    """`windowHandle()` rend None tant que la fenêtre n'a pas de support
    natif — c'est le cas sous une plateforme Qt sans fenêtrage."""
    monkeypatch.setattr(
        grid_module.GridWindow, "_start_hype_watcher",
        lambda self: setattr(self, "_hype_watcher", None))
    monkeypatch.setattr(grid_module, "mark_fullscreen", lambda win: None)
    monkeypatch.setattr(grid_module.GridWindow, "windowHandle",
                        lambda self: None)
    w = grid_module.GridWindow(qapp.primaryScreen(), show_on_init=False)
    qtbot.addWidget(w)
    w._move_to_screen(qapp.primaryScreen())
    w.hide()


# =============================================================================
# windows/single.py
# =============================================================================

class _FausseFenetre:
    """Note les appels que le coordinateur adresse à une fenêtre."""

    def __init__(self, nom: str = "") -> None:
        self.nom = nom
        self.visible = False
        self.ecrans: list = []
        self.drapeaux: list = []

    def setWindowFlags(self, flags):     # noqa: N802 (API Qt)
        self.drapeaux.append(flags)

    def setGeometry(self, g):            # noqa: N802
        self.geometrie = g

    def show(self):
        self.visible = True

    def showFullScreen(self):            # noqa: N802
        self.visible = True

    def hide(self):
        self.visible = False

    def windowHandle(self):              # noqa: N802
        return self

    def setScreen(self, screen):         # noqa: N802
        self.ecrans.append(screen)

    def raise_(self):
        pass

    def activateWindow(self):            # noqa: N802
        pass


class _FenetreSansHandle(_FausseFenetre):
    def windowHandle(self):              # noqa: N802
        return None


def test_le_mode_un_ecran_monte_les_trois_vues_et_demarre_sur_le_direct(
        qapp, monkeypatch):
    """Ce qu'on veut voir en ouvrant ZLink, c'est un direct.

    Les trois fenêtres sont construites d'emblée : les créer au premier clic
    ferait attendre plusieurs secondes à chaque bascule. Le panel et la grille
    perdent leur décoration — en mode un écran elles se superposent au plein
    écran, une barre de titre trancherait au milieu de l'image.
    """
    construites: list[str] = []

    def _fabrique(nom):
        def _creer(screen, *, show_on_init=True, **kw):
            construites.append(nom)
            return _FausseFenetre(nom)
        return _creer

    monkeypatch.setattr(single, "PanelWindow", _fabrique("panel"))
    monkeypatch.setattr(single, "FullscreenWindow", _fabrique("fullscreen"))
    monkeypatch.setattr(single, "GridWindow", _fabrique("grid"))
    monkeypatch.setattr(single, "mark_fullscreen", lambda win: None)

    pilules: list = []

    class _FaussePilule:
        def __init__(self, screen, on_switch, on_close) -> None:
            self.actif: int | None = None
            self.on_switch = on_switch
            self.on_close = on_close
            pilules.append(self)

        def set_active(self, idx):
            self.actif = idx

        def raise_(self):
            pass

    monkeypatch.setattr(single, "_NavPill", _FaussePilule)

    coquille = single.SingleModeShell(qapp.primaryScreen())
    # Le polling interroge le curseur toutes les 80 ms : le laisser courir
    # ferait tourner ce coordinateur pendant les tests suivants.
    coquille._poll.stop()

    assert construites == ["panel", "fullscreen", "grid"]
    assert coquille.fullscreen.visible is True
    assert coquille.panel.visible is False and coquille.grid.visible is False
    assert pilules[-1].actif == single.SingleModeShell._IDX_FULLSCREEN
    assert coquille.panel.drapeaux and coquille.grid.drapeaux, \
        "panel et grille perdent leur décoration"
    assert coquille._poll.interval() <= 100, "la zone de survol doit répondre"


def test_une_fenetre_sans_handle_natif_est_quand_meme_affichee(qapp,
                                                               monkeypatch):
    """`windowHandle()` rend None tant que le support natif n'existe pas.

    Y appeler `setScreen()` lèverait et laisserait la bascule à mi-chemin :
    l'ancienne vue masquée, la nouvelle jamais montrée.
    """
    monkeypatch.setattr(single, "mark_fullscreen", lambda win: None)
    coquille = single.SingleModeShell.__new__(single.SingleModeShell)
    ecran = qapp.primaryScreen()
    coquille._screen = ecran
    coquille._screen_rect = ecran.geometry()
    coquille.panel = _FenetreSansHandle("panel")
    coquille.fullscreen = _FenetreSansHandle("fullscreen")
    coquille.grid = _FenetreSansHandle("grid")
    coquille._pill = types.SimpleNamespace(
        set_active=lambda idx: None, raise_=lambda: None)

    coquille._switch(single.SingleModeShell._IDX_PANEL)
    assert coquille.panel.visible is True
    assert coquille.panel.ecrans == [], "aucun setScreen sur un handle absent"


def test_une_pilule_sans_handle_natif_se_construit_quand_meme(qtbot, qapp,
                                                              monkeypatch):
    """La pilule naît hors écran, donc sans support natif garanti.

    Sans le garde-fou, le mode un écran ne démarrerait pas du tout sur une
    plateforme Qt sans fenêtrage.
    """
    monkeypatch.setattr(single._NavPill, "windowHandle", lambda self: None)
    p = single._NavPill(qapp.primaryScreen(),
                        on_switch=lambda _i: None,
                        on_close=lambda: None)
    qtbot.addWidget(p)
    assert p.y() == p._hidden_y


# =============================================================================
# core/hype_watcher.py
# =============================================================================

@pytest.fixture
def horloge(monkeypatch):
    """Fige `time.monotonic` et rend le cadran, réglable par le test."""
    etat = {"t": 10_000.0}
    monkeypatch.setattr("core.hype_watcher.time.monotonic", lambda: etat["t"])
    return etat


@pytest.fixture
def watcher(qapp):
    """Un HypeWatcher jamais démarré — on n'appelle que ses méthodes.

    `qapp` est requis parce que HypeWatcher est un QThread : ses pyqtSignal
    n'existent qu'avec une application Qt.
    """
    return hw.HypeWatcher({})


class _EvenementScripte:
    """Double de `threading.Event` dont les réponses sont écrites d'avance.

    Le vrai `wait()` bloquerait deux secondes par tour d'évaluation et cinq
    secondes par tour de boucle IRC à vide.
    """

    def __init__(self, reponses: list[bool]) -> None:
        self._reponses = list(reponses)
        self.poses = 0
        self.attentes: list[float | None] = []

    def is_set(self) -> bool:
        return self._reponses.pop(0) if self._reponses else True

    def wait(self, timeout=None) -> bool:
        self.attentes.append(timeout)
        return True

    def set(self) -> None:
        self.poses += 1

    def clear(self) -> None:
        pass


class _FilFactice:
    """Thread qui n'existe pas : on note ce qu'on lui aurait demandé."""

    derniers: list["_FilFactice"] = []

    def __init__(self, *, target=None, daemon=False, name="") -> None:
        self.target = target
        self.daemon = daemon
        self.name = name
        self.demarrages = 0
        self.jointures: list[float | None] = []
        _FilFactice.derniers.append(self)

    def start(self) -> None:
        self.demarrages += 1

    def join(self, timeout=None) -> None:
        self.jointures.append(timeout)


def test_l_arret_pose_le_drapeau_et_attend_le_fil(watcher):
    """Sans attente, le fil IRC survivrait à la fermeture de la fenêtre et
    garderait la connexion Twitch ouverte jusqu'à la fin du processus."""
    watcher.stop()
    assert watcher._stop_event.is_set() is True


def test_la_boucle_principale_lance_l_irc_puis_evalue_periodiquement(
        watcher, monkeypatch):
    """Deux rythmes cohabitent : l'IRC est bloquant et vit dans son propre fil,
    l'évaluation tourne au tempo fixe de la boucle."""
    _FilFactice.derniers = []
    monkeypatch.setattr(hw, "threading",
                        types.SimpleNamespace(Thread=_FilFactice,
                                              Lock=hw.threading.Lock,
                                              Event=hw.threading.Event))
    evaluations: list[int] = []
    monkeypatch.setattr(watcher, "_evaluate_all",
                        lambda: evaluations.append(1))
    # False → le corps s'exécute ; False → l'évaluation a lieu ; True → sortie.
    watcher._stop_event = _EvenementScripte([False, False, True])

    watcher.run()

    fil = _FilFactice.derniers[-1]
    assert fil.target == watcher._irc_loop
    assert fil.daemon is True and fil.demarrages == 1
    assert evaluations == [1]
    assert watcher._stop_event.poses == 1, "le fil IRC est prévenu de l'arrêt"
    assert fil.jointures == [2.0], "on ne s'attarde pas si l'IRC ne répond plus"


def test_la_boucle_principale_n_evalue_pas_apres_un_arret_demande(
        watcher, monkeypatch):
    """L'attente dure deux secondes : un arrêt demandé pendant ce laps ne doit
    pas se solder par une évaluation de plus, sur des cellules déjà détruites."""
    _FilFactice.derniers = []
    monkeypatch.setattr(hw, "threading",
                        types.SimpleNamespace(Thread=_FilFactice,
                                              Lock=hw.threading.Lock,
                                              Event=hw.threading.Event))
    evaluations: list[int] = []
    monkeypatch.setattr(watcher, "_evaluate_all",
                        lambda: evaluations.append(1))
    watcher._stop_event = _EvenementScripte([False, True])

    watcher.run()
    assert evaluations == []


def test_la_boucle_irc_attend_plutot_que_de_tourner_a_vide(watcher):
    """Aucune cellule à surveiller : se connecter à Twitch pour ne rien lire
    ferait une reconnexion par tour, en boucle serrée."""
    sessions: list[list[str]] = []
    watcher._irc_session = sessions.append          # type: ignore[assignment]
    watcher._stop_event = _EvenementScripte([False, True])
    dirty = _EvenementScripte([False])
    watcher._channels_dirty = dirty                 # type: ignore[assignment]

    watcher._irc_loop()

    assert sessions == []
    assert dirty.attentes == [5.0]


def test_la_boucle_irc_reconnecte_apres_une_session_perdue(watcher,
                                                           monkeypatch):
    """Une coupure réseau ne doit pas laisser la grille sans chat jusqu'au
    redémarrage — mais une reconnexion immédiate martèlerait le serveur."""
    pauses: list[float] = []
    monkeypatch.setattr(hw.time, "sleep", pauses.append)

    def _echoue(logins):
        raise OSError("connexion réinitialisée")

    watcher._cells["zerator"] = hw._CellInfo(0, "zerator", None)
    watcher._irc_session = _echoue                  # type: ignore[assignment]
    watcher._stop_event = _EvenementScripte([False, False, True])
    watcher._channels_dirty = _EvenementScripte([])  # type: ignore[assignment]

    watcher._irc_loop()
    assert pauses == [5.0]


def test_un_arret_pendant_une_session_perdue_n_attend_pas_cinq_secondes(
        watcher, monkeypatch):
    """Fermer ZLink pendant une coupure réseau ne doit pas retarder la sortie."""
    pauses: list[float] = []
    monkeypatch.setattr(hw.time, "sleep", pauses.append)

    def _echoue(logins):
        raise OSError("connexion réinitialisée")

    watcher._cells["zerator"] = hw._CellInfo(0, "zerator", None)
    watcher._irc_session = _echoue                  # type: ignore[assignment]
    watcher._stop_event = _EvenementScripte([False, True])
    watcher._channels_dirty = _EvenementScripte([])  # type: ignore[assignment]

    watcher._irc_loop()
    assert pauses == []


# -- la session IRC elle-même -------------------------------------------------

class _SocketScripte:
    """Socket TLS factice : rend des blocs écrits d'avance, note les envois."""

    def __init__(self, blocs: list[bytes] | None = None) -> None:
        self.blocs = list(blocs or [])
        self.envois: list[str] = []
        self.delais: list[float] = []
        self.fermetures = 0

    def sendall(self, donnees: bytes) -> None:
        self.envois.append(donnees.decode("utf-8"))

    def settimeout(self, valeur: float) -> None:
        self.delais.append(valeur)

    def recv(self, taille: int) -> bytes:
        if not self.blocs:
            return b""
        return self.blocs.pop(0)

    def close(self) -> None:
        self.fermetures += 1


@pytest.fixture
def reseau_factice(monkeypatch):
    """Remplace la pile socket/TLS du HypeWatcher par une doublure.

    Aucun test n'ouvre de connexion : `irc.chat.twitch.tv` n'a pas à répondre
    pour que la logique de session soit vérifiable.
    """
    etat: dict = {}

    def _poser(blocs=None):
        sock = _SocketScripte(blocs)
        etat["sock"] = sock
        monkeypatch.setattr(hw.socket, "create_connection",
                            lambda adresse, timeout=None: sock)
        contexte = types.SimpleNamespace(
            minimum_version=None,
            wrap_socket=lambda brut, server_hostname=None: sock,
        )
        monkeypatch.setattr(hw.ssl, "create_default_context",
                            lambda: contexte)
        etat["contexte"] = contexte
        return sock

    _poser.etat = etat      # type: ignore[attr-defined]
    return _poser


def test_la_session_irc_s_annonce_en_lecture_seule_et_rejoint_les_canaux(
        watcher, reseau_factice):
    """Le pseudo justinfan est le mode anonyme attendu par l'IRC Twitch.

    Les capacités tags et commands ne sont pas décoratives : sans `commands`
    aucun USERNOTICE n'arrive, donc aucun raid ; sans `tags` un USERNOTICE ne
    dit pas de quel type il est.
    """
    sock = reseau_factice([b""])
    watcher._stop_event = _EvenementScripte([False])
    watcher._channels_dirty = _EvenementScripte([False])  # type: ignore[assignment]

    watcher._irc_session(["zerator", "domingo"])

    envoye = "".join(sock.envois)
    assert "PASS SCHMOOPIIE" in envoye
    assert "NICK justinfan" in envoye
    assert "CAP REQ :twitch.tv/tags twitch.tv/commands" in envoye
    assert "JOIN #zerator,#domingo" in envoye
    assert sock.fermetures == 1, "la socket est refermée quoi qu'il arrive"


def test_un_login_mal_forme_n_est_jamais_envoye_sur_le_fil(watcher,
                                                            reseau_factice,
                                                            caplog):
    """Un login contenant \\r\\n injecterait des commandes IRC arbitraires.

    Les logins viennent d'une API tierce : ils ne sont pas dignes de confiance.
    """
    sock = reseau_factice([b""])
    watcher._stop_event = _EvenementScripte([False])
    watcher._channels_dirty = _EvenementScripte([False])  # type: ignore[assignment]

    with caplog.at_level("ERROR"):
        watcher._irc_session(["zerator", "sale\r\nQUIT"])

    envoye = "".join(sock.envois)
    assert "QUIT" not in envoye
    assert "JOIN #zerator" in envoye
    assert any("écarté" in enr.message for enr in caplog.records)


def test_une_liste_entierement_invalide_n_ouvre_aucun_canal(watcher,
                                                             reseau_factice):
    """Rejoindre zéro canal laisserait une connexion muette ouverte : mieux
    vaut rendre la main et laisser la boucle réessayer."""
    sock = reseau_factice([b""])
    watcher._stop_event = _EvenementScripte([False])

    watcher._irc_session(["#pas-un-login"])

    assert not any(e.startswith("JOIN") for e in sock.envois)
    assert sock.fermetures == 1


def test_la_session_lit_les_lignes_completes_du_fil(watcher, reseau_factice):
    """Le fil arrive par morceaux de 4 Ko : une ligne peut être coupée en
    deux. Ne traiter que ce qui se termine par CRLF est la seule lecture juste."""
    reseau_factice([
        b"@a=b :nick!nick@nick PRIVMSG #zerator :salut\r\n:tmi PING",
        b" :tmi.twitch.tv\r\n",
        b"",
    ])
    watcher._cells["zerator"] = hw._CellInfo(0, "zerator", None)
    watcher._stop_event = _EvenementScripte([False, False, False])
    watcher._channels_dirty = _EvenementScripte([False, False, False])  # type: ignore[assignment]

    watcher._irc_session(["zerator"])

    assert len(watcher._cells["zerator"].chat_events) == 1


def test_un_changement_de_canaux_met_fin_a_la_session(watcher, reseau_factice):
    """La grille a changé : la session en cours écoute des chaînes qu'on
    n'affiche plus et manque celles qu'on affiche."""
    sock = reseau_factice([b"@a=b :n!n@n PRIVMSG #zerator :ignore\r\n"])
    watcher._cells["domingo"] = hw._CellInfo(0, "domingo", None)
    watcher._stop_event = _EvenementScripte([False, False])
    watcher._channels_dirty = _EvenementScripte([True])  # type: ignore[assignment]

    watcher._irc_session(["zerator"])

    assert sock.blocs, "on quitte sans même lire le morceau suivant"


def test_un_silence_prolonge_ne_ferme_pas_la_session(watcher, reseau_factice,
                                                     monkeypatch):
    """Une chaîne peu bavarde ne produit rien pendant quinze secondes : la
    lecture expire, sans que cela signifie une déconnexion."""
    sock = reseau_factice()
    appels = {"n": 0}

    def _recv(taille):
        appels["n"] += 1
        if appels["n"] == 1:
            raise socket.timeout("délai dépassé")
        return b""

    sock.recv = _recv
    watcher._stop_event = _EvenementScripte([False, False, False])
    watcher._channels_dirty = _EvenementScripte([False, False])  # type: ignore[assignment]

    watcher._irc_session(["zerator"])
    assert appels["n"] == 2, "la lecture reprend après le délai"


def test_un_arret_demande_ferme_la_session_sans_rien_lire(watcher,
                                                          reseau_factice):
    """Fermer ZLink pendant que l'IRC attend un message ne doit pas rester
    suspendu sur une lecture de quinze secondes."""
    sock = reseau_factice([b"@a=b :n!n@n PRIVMSG #zerator :trop tard"])
    watcher._stop_event = _EvenementScripte([True])

    watcher._irc_session(["zerator"])

    assert sock.blocs, "le fil n'est même pas lu"
    assert sock.fermetures == 1


def test_une_grille_reagencee_sans_changer_de_chaines_ne_reconnecte_pas(
        watcher, reseau_factice):
    """Déplacer une cellule marque les canaux « à revoir » sans en changer un
    seul : rouvrir la connexion coûterait quelques secondes de chat perdues
    pour rien."""
    reseau_factice([b""])
    watcher._cells["zerator"] = hw._CellInfo(0, "zerator", None)
    watcher._stop_event = _EvenementScripte([False, False])
    watcher._channels_dirty = _EvenementScripte([True])  # type: ignore[assignment]

    watcher._irc_session(["zerator"])   # sort sur un fil vide, pas sur la liste


def test_un_envoi_sur_une_socket_morte_est_absorbe(watcher):
    """`_send` est appelé depuis la lecture du fil (PONG) : y laisser remonter
    une erreur ferait tomber la session à chaque déconnexion, alors que la
    boucle la rouvre juste après."""
    class _SocketMorte:
        def sendall(self, donnees: bytes) -> None:
            raise OSError("tuyau rompu")

    hw.HypeWatcher._send(_SocketMorte(), "PONG :tmi.twitch.tv")


def test_une_socket_deja_fermee_ne_fait_pas_lever_a_la_sortie(watcher,
                                                               reseau_factice):
    """Le `finally` s'exécute aussi quand la connexion est déjà tombée."""
    sock = reseau_factice([b""])

    def _close():
        raise OSError("descripteur invalide")

    sock.close = _close
    watcher._stop_event = _EvenementScripte([False])
    watcher._channels_dirty = _EvenementScripte([False])  # type: ignore[assignment]

    watcher._irc_session(["zerator"])


# -- lecture des tags et des raids --------------------------------------------

def test_un_tag_sans_cle_est_ignore(watcher):
    """Twitch peut envoyer un point-virgule de trop.

    Une clé vide écraserait une entrée légitime dans le dictionnaire de tags,
    et c'est `msg-id` qui décide si la ligne est un raid.
    """
    tags = watcher._parse_tags("@;msg-id=raid;login=zerator :tmi USERNOTICE")
    assert tags == {"msg-id": "raid", "login": "zerator"}


def test_un_raid_sans_canal_lisible_est_ignore(watcher):
    """Un raid n'est annoncé que dans le chat de la chaîne QUI LE REÇOIT :
    sans nom de canal, on ne saurait pas quelle cellule faire réagir."""
    recus: list = []
    watcher.raid_detected.connect(lambda *a: recus.append(a))
    watcher._process_usernotice("@msg-id=raid;login=zerator :tmi USERNOTICE")
    assert recus == []


# -- fusion des signaux -------------------------------------------------------

def test_une_audience_qui_bouge_sans_ligne_de_base_ne_donne_pas_de_score(
        watcher, horloge):
    """La ligne de base demande quatre-vingt-dix secondes d'observation.

    Avant cela, aucun des trois signaux n'a de normale à laquelle se comparer :
    rendre un score serait inventer une valeur, et alerter dessus reviendrait à
    signaler chaque cellule dans sa première minute et demie.
    """
    info = hw._CellInfo(0, "zerator", None)
    info.prev_viewers = 1000
    info.viewers = 3000
    assert info.viewers_growth() is not None, "la croissance est bien mesurée"
    assert watcher._score(info, horloge["t"], 2.0) is None
