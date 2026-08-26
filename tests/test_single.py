# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Mode un seul écran : bascule entre les trois fenêtres et pilule de navigation.

Sur un seul moniteur, les trois vues de ZLink se superposent au lieu de se
répartir. Deux mécanismes en découlent, et ce sont les seuls testés ici :

- `_switch` : exactement une fenêtre visible à la fois, sur le bon écran ;
- `_check_cursor` : la pilule de navigation ne se montre qu'au bord haut, et
  s'efface dès que ZLink n'est plus l'application au premier plan.

Les vraies PanelWindow / FullscreenWindow / GridWindow ne sont jamais
construites : elles passeraient en plein écran sur le moniteur de la personne
qui lance les tests, et instancieraient des lecteurs mpv. `_switch` ne demande
qu'une poignée de méthodes, remplacées ici par un double qui les note.

Note d'import : `windows.single` tire `windows.panel`, qui importe
QtWebEngineWidgets. Qt exige que ce module soit chargé AVANT la création du
QApplication ; l'import en tête de fichier a lieu pendant la collecte, donc
avant que la fixture `qapp` n'existe. Ne pas le déplacer dans une fonction.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QPoint

from windows import single


# ── doubles ──────────────────────────────────────────────────────────────────

class _FausseFenetre:
    """Note les appels que `_switch` adresse à une fenêtre."""

    def __init__(self, nom: str) -> None:
        self.nom = nom
        self.visible = False
        self.geometries: list = []
        self.ecrans: list = []
        self.premier_plan = 0

    def setGeometry(self, g):        # noqa: N802 (API Qt)
        self.geometries.append(g)

    def show(self):
        self.visible = True

    def showFullScreen(self):        # noqa: N802
        self.visible = True

    def hide(self):
        self.visible = False

    def windowHandle(self):          # noqa: N802
        return self

    def setScreen(self, screen):     # noqa: N802
        self.ecrans.append(screen)

    def raise_(self):
        self.premier_plan += 1

    def activateWindow(self):        # noqa: N802
        pass


class _FaussePilule:
    def __init__(self) -> None:
        self.actif: int | None = None
        self.revelations = 0
        self.masquages = 0
        self.premier_plan = 0

    def set_active(self, idx):
        self.actif = idx

    def raise_(self):
        self.premier_plan += 1

    def reveal(self):
        self.revelations += 1

    def start_hide(self):
        self.masquages += 1


@pytest.fixture
def coquille(qapp, monkeypatch):
    """SingleModeShell dont les fenêtres sont des doubles.

    `__new__` sans `__init__` : la classe est un simple coordinateur Python,
    pas un QObject, et son `__init__` construirait les trois vraies fenêtres.
    """
    monkeypatch.setattr(single, "mark_fullscreen", lambda win: None)
    shell = single.SingleModeShell.__new__(single.SingleModeShell)
    ecran = qapp.primaryScreen()
    shell._screen = ecran
    shell._screen_rect = ecran.geometry()
    shell.panel = _FausseFenetre("panel")
    shell.fullscreen = _FausseFenetre("fullscreen")
    shell.grid = _FausseFenetre("grid")
    shell._pill = _FaussePilule()
    return shell


def _visibles(coquille) -> list[str]:
    return [w.nom for w in (coquille.panel, coquille.fullscreen, coquille.grid)
            if w.visible]


# ── bascule entre les trois vues ─────────────────────────────────────────────

@pytest.mark.parametrize("idx,attendu", [
    (single.SingleModeShell._IDX_PANEL, "panel"),
    (single.SingleModeShell._IDX_FULLSCREEN, "fullscreen"),
    (single.SingleModeShell._IDX_GRID, "grid"),
])
def test_une_seule_fenetre_visible(coquille, idx, attendu):
    """Les trois vues se recouvrent : deux visibles, c'est une vue perdue."""
    coquille._switch(idx)
    assert _visibles(coquille) == [attendu]


def test_la_bascule_masque_la_precedente(coquille):
    coquille._switch(single.SingleModeShell._IDX_PANEL)
    coquille._switch(single.SingleModeShell._IDX_GRID)
    assert _visibles(coquille) == ["grid"]


def test_rebasculer_sur_la_meme_vue_la_laisse_visible(coquille):
    """Cliquer deux fois sur le même onglet ne doit pas éteindre la vue."""
    coquille._switch(single.SingleModeShell._IDX_FULLSCREEN)
    coquille._switch(single.SingleModeShell._IDX_FULLSCREEN)
    assert _visibles(coquille) == ["fullscreen"]


def test_la_fenetre_affichee_est_placee_sur_l_ecran_cible(coquille, qapp):
    """Sans setScreen(), Windows replace la fenêtre sur le moniteur principal.

    La géométrie est posée AVANT show() pour que le handle natif naisse au bon
    endroit : l'inverse produit un aller-retour visible d'un écran à l'autre.
    """
    coquille._switch(single.SingleModeShell._IDX_GRID)
    assert coquille.grid.geometries[-1] == coquille._screen_rect
    assert coquille.grid.ecrans == [qapp.primaryScreen()]


def test_la_fenetre_affichee_passe_au_premier_plan(coquille):
    coquille._switch(single.SingleModeShell._IDX_PANEL)
    assert coquille.panel.premier_plan == 1
    assert coquille.fullscreen.premier_plan == 0


def test_la_pilule_suit_la_vue_et_reste_au_dessus(coquille):
    """La pilule doit rester cliquable : elle est relevée après la fenêtre."""
    coquille._switch(single.SingleModeShell._IDX_GRID)
    assert coquille._pill.actif == single.SingleModeShell._IDX_GRID
    assert coquille._pill.premier_plan == 1


def test_une_fenetre_masquee_n_est_ni_placee_ni_relevee(coquille):
    """Le placement d'une fenêtre cachée coûte pour rien et peut la faire clignoter."""
    coquille._switch(single.SingleModeShell._IDX_PANEL)
    assert coquille.grid.geometries == []
    assert coquille.grid.ecrans == []
    assert coquille.grid.premier_plan == 0


# ── zone de déclenchement de la pilule ───────────────────────────────────────

@pytest.fixture
def curseur(monkeypatch):
    """Pilote la position rendue par QCursor.pos()."""
    position = {"p": QPoint(0, 0)}
    monkeypatch.setattr(single.QCursor, "pos",
                        staticmethod(lambda: position["p"]))
    # _check_cursor s'abstient si ZLink n'est pas au premier plan : par
    # défaut on se place dans le cas où elle l'est.
    monkeypatch.setattr(single.QApplication, "activeWindow",
                        staticmethod(lambda: object()))
    return position


def test_le_bord_haut_revele_la_pilule(coquille, curseur):
    g = coquille._screen_rect
    curseur["p"] = QPoint(g.x() + g.width() // 2, g.y())
    coquille._check_cursor()
    assert coquille._pill.revelations == 1
    assert coquille._pill.masquages == 0


@pytest.mark.parametrize("decalage_y", [0, 1, single._HOVER_Y])
def test_la_zone_de_declenchement_a_quelques_pixels_de_haut(
        coquille, curseur, decalage_y):
    """Viser une ligne d'un pixel serait intenable à la souris."""
    g = coquille._screen_rect
    curseur["p"] = QPoint(g.x() + 10, g.y() + decalage_y)
    coquille._check_cursor()
    assert coquille._pill.revelations == 1


def test_juste_sous_la_zone_la_pilule_se_masque(coquille, curseur):
    g = coquille._screen_rect
    curseur["p"] = QPoint(g.x() + 10, g.y() + single._HOVER_Y + 1)
    coquille._check_cursor()
    assert coquille._pill.revelations == 0
    assert coquille._pill.masquages == 1


@pytest.mark.parametrize("dx", [-1, 1])
def test_le_bord_haut_d_un_autre_ecran_ne_compte_pas(coquille, curseur, dx):
    """La zone est bornée en largeur à l'écran piloté par cette coquille.

    En multi-écrans, le bord haut du moniteur voisin ne doit pas faire
    apparaître une pilule sur celui-ci.
    """
    g = coquille._screen_rect
    x = g.x() - 1 if dx < 0 else g.x() + g.width() + 1
    curseur["p"] = QPoint(x, g.y())
    coquille._check_cursor()
    assert coquille._pill.revelations == 0
    assert coquille._pill.masquages == 1


def test_pas_de_pilule_si_zlink_n_est_pas_au_premier_plan(
        coquille, curseur, monkeypatch):
    """Une pilule qui surgit par-dessus une autre application est une nuisance.

    Le curseur est ici exactement dans la zone : seul le premier plan décide.
    """
    monkeypatch.setattr(single.QApplication, "activeWindow",
                        staticmethod(lambda: None))
    g = coquille._screen_rect
    curseur["p"] = QPoint(g.x() + 10, g.y())
    coquille._check_cursor()
    assert coquille._pill.revelations == 0
    assert coquille._pill.masquages == 1


# ── pilule de navigation ─────────────────────────────────────────────────────

@pytest.fixture
def pilule(qtbot, qapp):
    """_NavPill réelle, sur l'écran principal (fenêtre de 310x44, hors champ)."""
    bascules: list[int] = []
    fermetures: list[int] = []
    p = single._NavPill(qapp.primaryScreen(),
                        on_switch=bascules.append,
                        on_close=lambda: fermetures.append(1))
    qtbot.addWidget(p)
    p.bascules = bascules
    p.fermetures = fermetures
    return p


def test_la_pilule_nait_hors_ecran(pilule):
    """Elle doit être invisible au démarrage, sinon elle masque le direct."""
    assert pilule._hidden_y == pilule._shown_y - single._NAV_H
    assert pilule.y() == pilule._hidden_y


def test_la_pilule_est_centree_horizontalement(pilule, qapp):
    g = qapp.primaryScreen().geometry()
    assert pilule._center_x == g.x() + (g.width() - single._NAV_W) // 2


@pytest.mark.parametrize("idx", [0, 1, 2])
def test_un_seul_onglet_coche(pilule, idx):
    pilule.set_active(idx)
    coches = [i for i, b in enumerate(pilule._btns) if b.isChecked()]
    assert coches == [idx]


@pytest.mark.parametrize("idx,libelle", [
    (0, "Panel"), (1, "Fullscreen"), (2, "Grille"),
])
def test_chaque_onglet_demande_sa_vue(pilule, idx, libelle):
    """L'index transmis doit correspondre au libellé, pas à l'ordre de clic."""
    assert pilule._btns[idx].text() == libelle
    pilule._btns[idx].click()
    assert pilule.bascules[-1] == idx


def test_reveler_annule_le_masquage_en_attente(pilule):
    """Revenir dans la zone pendant le compte à rebours doit l'arrêter."""
    pilule.start_hide()
    assert pilule._hide_timer.isActive() is True
    pilule.reveal()
    assert pilule._hide_timer.isActive() is False


def test_reveler_deux_fois_ne_relance_pas_l_animation(pilule):
    """Le polling appelle reveal() toutes les 80 ms tant qu'on survole le bord.

    Sans ce court-circuit, l'animation repartirait de zéro à chaque tour et la
    pilule resterait figée en cours de descente.
    """
    pilule.move(pilule._center_x, pilule._shown_y)
    pilule.reveal()
    assert pilule._anim.state() != pilule._anim.State.Running


def test_le_masquage_ne_se_reamorce_pas_a_chaque_tour(pilule):
    """start_hide() est appelé toutes les 80 ms : redémarrer le timer à chaque
    appel repousserait le masquage indéfiniment."""
    pilule.start_hide()
    reste = pilule._hide_timer.remainingTime()
    pilule.start_hide()
    assert pilule._hide_timer.remainingTime() <= reste


def test_annuler_le_masquage(pilule):
    pilule.start_hide()
    pilule.cancel_hide()
    assert pilule._hide_timer.isActive() is False


def test_le_delai_de_masquage_laisse_le_temps_de_viser(pilule):
    """Trop court, la pilule fuirait sous le curseur qui vient la chercher."""
    pilule.start_hide()
    assert single._HIDE_DELAY_MS >= 1000
    assert pilule._hide_timer.remainingTime() > 0


def test_le_bouton_fermer_demande_la_fermeture(pilule):
    """La croix est le seul moyen de quitter en mode un écran : sans elle, les
    trois fenêtres sont sans décoration et sans barre des tâches."""
    croix = pilule.findChildren(type(pilule._btns[0]))[-1]
    assert croix.text() == "✕"
    croix.click()
    assert pilule.fermetures == [1]


def test_glisser_vers_le_haut_vise_la_position_cachee(pilule):
    pilule.move(pilule._center_x, pilule._shown_y)
    pilule._slide_up()
    assert pilule._anim.endValue() == QPoint(pilule._center_x,
                                             pilule._hidden_y)


def test_reveler_vise_la_position_visible(pilule):
    pilule.reveal()
    assert pilule._anim.endValue() == QPoint(pilule._center_x,
                                             pilule._shown_y)


def test_le_curseur_sur_la_pilule_annule_le_masquage(pilule):
    """Le polling continue de réclamer le masquage pendant qu'on vise un
    onglet : c'est enterEvent qui doit avoir le dernier mot."""
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QEnterEvent

    pilule.start_hide()
    pilule.enterEvent(QEnterEvent(QPointF(5.0, 5.0), QPointF(5.0, 5.0),
                                  QPointF(5.0, 5.0)))
    assert pilule._hide_timer.isActive() is False


def test_quitter_la_pilule_relance_le_masquage(pilule):
    from PyQt6.QtCore import QEvent

    pilule.cancel_hide()
    pilule.leaveEvent(QEvent(QEvent.Type.Leave))
    assert pilule._hide_timer.isActive() is True
