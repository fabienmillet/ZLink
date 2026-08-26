# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Assistant de première configuration : écrans, rôles, étapes, point d'entrée.

L'assistant décide de la disposition des fenêtres avant qu'aucune ne soit
créée : une erreur ici ne se rattrape qu'au redémarrage. Ces tests visent la
logique — géométrie des écrans, attribution des rôles, ce que chaque étape
écrit dans la config — et laissent le dessin de côté.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QEnterEvent, QMouseEvent, QResizeEvent

from core import config_store
from widgets import screen_picker as SP
from windows import wizard as W


# ── outils ───────────────────────────────────────────────────────────────────

def _clic(widget, point, bouton=Qt.MouseButton.LeftButton) -> None:
    """Relâchement de bouton sur `point`, sans passer par une vraie fenêtre.

    L'assistant n'ouvre rien pendant les tests : on lui livre l'événement que
    Qt lui aurait transmis.
    """
    pos = QPointF(point)
    widget.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, pos, pos, bouton,
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
    ))


#: Deux écrans côte à côte, le second à droite du premier.
_DEUX = [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)]


# ── _ChoiceCard / _ChoiceList ────────────────────────────────────────────────

def test_une_carte_cliquee_emet_sa_valeur(qapp):
    """Toute la surface est cliquable : c'est ce qui remplace la puce radio."""
    card = W._ChoiceCard("manual", "Manuel", "au glisser-déposer", badge="NOUVEAU")
    recu = []
    card.clicked.connect(recu.append)
    _clic(card, card.rect().center())
    assert recu == ["manual"]


def test_le_survol_d_une_carte_se_voit_puis_se_retire(qapp):
    """Rien n'indiquait qu'il fallait viser la carte : le survol le dit."""
    from PyQt6.QtCore import QPointF as _P
    card = W._ChoiceCard("a", "A", "")
    repos = card.styleSheet()
    card.enterEvent(QEnterEvent(_P(1, 1), _P(1, 1), _P(1, 1)))
    assert card.styleSheet() != repos
    card.leaveEvent(QEvent(QEvent.Type.Leave))
    assert card.styleSheet() == repos


def test_une_carte_selectionnee_ignore_le_survol(qapp):
    """La sélection prime : son liseré vert ne doit pas s'effacer au passage."""
    from PyQt6.QtCore import QPointF as _P
    card = W._ChoiceCard("a", "A", "")
    card.set_selected(True)
    choisie = card.styleSheet()
    card.enterEvent(QEnterEvent(_P(1, 1), _P(1, 1), _P(1, 1)))
    assert card.styleSheet() == choisie


def test_le_bouton_droit_ne_choisit_pas_une_carte(qapp):
    card = W._ChoiceCard("a", "A", "")
    recu = []
    card.clicked.connect(recu.append)
    _clic(card, card.rect().center(), Qt.MouseButton.RightButton)
    assert recu == []


def test_reselectionner_la_meme_carte_ne_la_repeint_pas(qapp):
    card = W._ChoiceCard("a", "A", "")
    card.set_selected(True)
    avant = card.styleSheet()
    card.set_selected(True)
    assert card.styleSheet() == avant


def test_aucune_carte_selectionnee_au_depart(qapp):
    liste = W._ChoiceList()
    liste.add("a", "A", "")
    assert liste.selected() is None


def test_la_selection_est_exclusive(qapp):
    liste = W._ChoiceList()
    for v in ("a", "b", "c"):
        liste.add(v, v.upper(), "")
    recu = []
    liste.changed.connect(recu.append)
    liste.select("b")
    liste.select("c")
    assert liste.selected() == "c"
    assert [c.is_selected() for c in liste._cards] == [False, False, True]
    assert recu == ["b", "c"]


def test_selectionner_une_valeur_inconnue_ne_retient_rien(qapp):
    liste = W._ChoiceList()
    liste.add("a", "A", "")
    liste.select("a")
    liste.select("inexistant")
    assert liste.selected() is None


# ── étapes ───────────────────────────────────────────────────────────────────

def test_l_etape_de_bienvenue_n_ecrit_rien(qapp):
    config = {"intact": 1}
    W._StepWelcome({}).collect(config)
    assert config == {"intact": 1}


def test_l_etape_ecrans_ecrit_les_roles(qapp):
    etape = W._StepScreens({})
    config: dict = {}
    etape.collect(config)
    assert etape.valid() is True
    # Un seul écran hors ligne de commande : c'est le direct qui l'occupe.
    assert config["screen_assignments"] == {"0": "fullscreen"}


def test_l_etape_ecrans_restaure_l_attribution_enregistree(qapp):
    """Rouvrir l'assistant doit repartir de ce qui était choisi, pas du défaut."""
    etape = W._StepScreens({"screen_assignments": {"0": "panel"}})
    # Un seul écran hors ligne de commande : le direct lui revient de force,
    # sans quoi l'assistant proposerait une disposition qui ne démarre pas.
    assert etape._picker.roles() == ["fullscreen"]


def test_l_etape_ecrans_decrit_ce_que_donne_la_configuration(qapp):
    """La note sous le schéma dit où retrouver les vues qu'on ne voit pas."""
    etape = W._StepScreens({})
    assert etape._note.text() == SP.PLAN_NOTES[1]


def test_sans_ecran_actif_l_etape_bloque_et_n_ecrit_rien(qapp):
    """Le schéma l'interdit ; le garde-fou de l'étape doit tenir quand même."""
    etape = W._StepScreens({})
    etape._picker._roles = ["" for _ in etape._picker.roles()]
    etape._refresh()
    config: dict = {}
    etape.collect(config)
    assert etape.valid() is False
    assert config == {}


@pytest.mark.parametrize("assigne,attendu", [
    (None, 0),
    ({}, 0),
    ({"0": "fullscreen"}, 1),
    ({"0": "panel", "1": "fullscreen"}, 2),
    # Un écran désactivé ou sans rôle ne compte pas.
    ({"0": "panel", "1": "disabled"}, 1),
    ({"0": "panel", "1": ""}, 1),
])
def test_comptage_des_ecrans_configures(assigne, attendu):
    assert W._count_from_config({"screen_assignments": assigne}) == attendu


@pytest.mark.parametrize("enregistre,attendu", [
    (None, 16),      # absent : la valeur de repli
    (0, 16),         # zéro est faux, on retombe sur le repli
    (4, 4),
    (25, 25),
    (99, 25),        # le curseur borne à son maximum
    (-3, 1),         # et à son minimum
])
def test_l_etape_grille_charge_le_nombre_de_flux(qapp, enregistre, attendu):
    config = {} if enregistre is None else {"max_active_streams": enregistre}
    assert W._StepGrid(config)._slider.value() == attendu


@pytest.mark.parametrize("valeur,extrait", [
    (1, "Qualité maximale"),
    (4, "Qualité maximale"),
    (5, "Bon compromis"),
    (9, "Bon compromis"),
    (10, "Confortable"),
    (16, "Confortable"),
    (17, "Exigeant"),
    (25, "Exigeant"),
])
def test_le_conseil_suit_le_nombre_de_flux(qapp, valeur, extrait):
    """Le chiffre seul ne dit rien du coût : l'assistant le traduit."""
    etape = W._StepGrid({})
    etape._slider.setValue(valeur)
    assert etape._value_lbl.text() == str(valeur)
    assert extrait in etape._hint_lbl.text()


@pytest.mark.parametrize("enregistre,attendu", [
    ("viewers", "viewers"),
    ("manual", "manual"),
    ("favorites", "favorites"),
    ("n_importe_quoi", "viewers"),   # config bricolée à la main
    (None, "viewers"),
])
def test_l_etape_grille_ecrit_la_disposition(qapp, enregistre, attendu):
    config = {} if enregistre is None else {"grid_sort": enregistre}
    etape = W._StepGrid(config)
    etape._slider.setValue(7)
    ecrit: dict = {}
    etape.collect(ecrit)
    assert ecrit == {"max_active_streams": 7, "grid_sort": attendu}


@pytest.mark.parametrize("enregistre,attendu", [
    (None, True),                        # activé par défaut
    ({}, True),                          # section présente mais vide
    ({"enabled": False}, False),
    ({"enabled": True}, True),
])
def test_l_etape_hypewatcher(qapp, enregistre, attendu):
    config = {} if enregistre is None else {"hypewatcher": enregistre}
    etape = W._StepHype(config)
    assert etape._check.isChecked() is attendu
    ecrit: dict = {}
    etape.collect(ecrit)
    assert ecrit == {"hypewatcher": {"enabled": attendu}}


def test_l_etape_hypewatcher_conserve_les_autres_reglages(qapp):
    """La section porte d'autres clés : `collect` ne doit pas les écraser."""
    etape = W._StepHype({})
    config = {"hypewatcher": {"threshold": 3.5}}
    etape.collect(config)
    assert config["hypewatcher"] == {"threshold": 3.5, "enabled": True}


def test_le_recapitulatif_relit_ce_que_les_etapes_ont_ecrit(qapp):
    etape = W._StepSummary({})
    etape.refresh({
        "screen_assignments": {"0": "panel", "1": "fullscreen"},
        "max_active_streams": 12,
        "grid_sort": "favorites",
        "hypewatcher": {"enabled": False},
    })
    texte = etape._recap.text()
    assert "2 écrans" in texte
    assert "écran 1 → Panel" in texte and "écran 2 → Plein écran" in texte
    assert "Jusqu'à 12 flux" in texte
    assert "favoris puis manuel" in texte
    assert "HypeWatcher : désactivé" in texte


def test_le_recapitulatif_d_une_config_vide_reste_lisible(qapp):
    """Bouton « Passer » : le récapitulatif doit décrire les valeurs de repli."""
    etape = W._StepSummary({})
    etape.refresh({})
    texte = etape._recap.text()
    assert "0 écran :" in texte
    assert "Jusqu'à 16 flux" in texte
    assert "par audience" in texte
    assert "HypeWatcher : activé" in texte


def test_l_etape_de_base_ne_bloque_jamais(qapp):
    assert W._Step().valid() is True


# ── FirstRunWizard ───────────────────────────────────────────────────────────

def test_l_assistant_avance_etape_par_etape_et_collecte(qtbot):
    wiz = W.FirstRunWizard({})
    qtbot.addWidget(wiz)
    assert wiz._stack.currentIndex() == 0
    assert wiz._prev.isEnabled() is False
    assert wiz._dots.text().startswith("●")

    for attendu in range(1, len(wiz._steps)):
        wiz._go_next()
        assert wiz._stack.currentIndex() == attendu
    assert wiz._next.text() == "Terminer"
    assert wiz._skip.isVisible() is False, "on ne passe plus une étape terminale"
    # Les étapes traversées ont écrit ce qu'elles avaient à écrire.
    assert wiz.result_config["screen_assignments"] == {"0": "fullscreen"}
    assert wiz.result_config["max_active_streams"] == 16
    assert wiz.result_config["hypewatcher"] == {"enabled": True}


def test_revenir_en_arriere_puis_repartir(qtbot):
    wiz = W.FirstRunWizard({})
    qtbot.addWidget(wiz)
    wiz._go_next()
    wiz._go_prev()
    assert wiz._stack.currentIndex() == 0
    wiz._go_prev()
    assert wiz._stack.currentIndex() == 0, "pas d'étape avant la première"
    assert wiz._prev.isEnabled() is False


def test_terminer_accepte_la_fenetre(qtbot):
    from PyQt6.QtWidgets import QDialog
    wiz = W.FirstRunWizard({})
    qtbot.addWidget(wiz)
    for _ in range(len(wiz._steps)):
        wiz._go_next()
    assert wiz.result() == QDialog.DialogCode.Accepted


def test_passer_rejette_la_fenetre(qtbot):
    from PyQt6.QtWidgets import QDialog
    wiz = W.FirstRunWizard({})
    qtbot.addWidget(wiz)
    wiz._on_skip()
    assert wiz.result() == QDialog.DialogCode.Rejected


def test_une_etape_invalide_bloque_l_avancement(qtbot):
    wiz = W.FirstRunWizard({})
    qtbot.addWidget(wiz)
    wiz._go_next()                       # → étape des écrans
    ecrans = wiz._steps[1]
    ecrans._picker._roles = ["" for _ in ecrans._picker.roles()]
    wiz._sync()
    assert wiz._next.isEnabled() is False
    wiz._go_next()
    assert wiz._stack.currentIndex() == 1, "on reste sur l'étape des écrans"
    assert "screen_assignments" not in wiz.result_config


# ── point d'entrée ───────────────────────────────────────────────────────────

@pytest.fixture
def config(tmp_path, monkeypatch):
    """config.json neuf : l'assistant écrit vraiment, mais dans tmp_path."""
    cible = tmp_path / "config.json"
    monkeypatch.setattr(config_store, "CONFIG_PATH", cible)
    return cible


@pytest.fixture
def faux_assistant(monkeypatch):
    """Remplace la fenêtre par un objet qui répond sans rien afficher."""
    journal: dict = {"ouvertures": 0}

    def _poser(accepte: bool, ajout: dict | None = None):
        from PyQt6.QtWidgets import QDialog

        class _Faux:
            def __init__(self, config, parent=None):
                journal["ouvertures"] += 1
                journal["recu"] = dict(config)
                self.result_config = dict(config)
                self.result_config.update(ajout or {})

            def exec(self):
                return (QDialog.DialogCode.Accepted if accepte
                        else QDialog.DialogCode.Rejected)

        monkeypatch.setattr(W, "FirstRunWizard", _Faux)
        return journal

    return _poser


@pytest.mark.parametrize("stocke,attendu", [
    ({}, True),
    ({"setup_done": False}, True),
    ({"setup_done": True}, False),
])
def test_besoin_de_l_assistant(config, stocke, attendu):
    config_store.save_merge(stocke)
    assert W.needs_first_run() is attendu


def test_l_assistant_ne_se_rouvre_pas_une_fois_passe(config, faux_assistant):
    journal = faux_assistant(accepte=True)
    config_store.save_merge({"setup_done": True, "max_active_streams": 9})
    obtenu = W.run_first_run_wizard()
    assert journal["ouvertures"] == 0
    assert obtenu["max_active_streams"] == 9


def test_force_rouvre_l_assistant(config, faux_assistant):
    """C'est ce que fait `--setup`."""
    journal = faux_assistant(accepte=True, ajout={"max_active_streams": 4})
    config_store.save_merge({"setup_done": True, "max_active_streams": 9})
    obtenu = W.run_first_run_wizard(force=True)
    assert journal["ouvertures"] == 1
    assert journal["recu"]["max_active_streams"] == 9, "l'existant est proposé"
    assert obtenu["max_active_streams"] == 4


def test_assistant_termine_enregistre_la_config_retenue(config, faux_assistant):
    faux_assistant(accepte=True, ajout={"grid_sort": "manual"})
    obtenu = W.run_first_run_wizard()
    assert obtenu["grid_sort"] == "manual"
    assert obtenu["setup_done"] is True
    assert config_store.load()["grid_sort"] == "manual"


def test_assistant_passe_marque_quand_meme_setup_done(config, faux_assistant):
    """Un assistant écarté qui revient à chaque lancement serait pénible."""
    faux_assistant(accepte=False, ajout={"grid_sort": "manual"})
    obtenu = W.run_first_run_wizard()
    assert "grid_sort" not in obtenu, "les choix écartés ne s'appliquent pas"
    assert obtenu["setup_done"] is True
    assert config_store.load() == {"setup_done": True}
