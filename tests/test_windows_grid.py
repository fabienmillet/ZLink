# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Fenêtre grille : raccourcis clavier, relais d'alertes, clip automatique.

GridWindow n'affiche rien par elle-même : elle enveloppe GridWidget et fait la
traduction entre le monde extérieur (touches, HypeWatcher, raids) et les
cellules. C'est cette traduction qui est testée — pas la grille elle-même.

Deux précautions dans tout ce fichier :

- le vrai HypeWatcher n'est jamais construit : il ouvre des connexions IRC, et
  un test n'a rien à faire sur le réseau. Il est soit court-circuité, soit
  remplacé par un double dans la dernière section.
- la fenêtre est construite avec `show_on_init=False` : sans ce drapeau, la
  construction passerait en plein écran sur le moniteur réel.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent

from widgets import grid_widget
from windows import grid as grid_module


# ── outillage ────────────────────────────────────────────────────────────────

@pytest.fixture
def fenetre(qtbot, qapp, monkeypatch):
    """GridWindow sans HypeWatcher, sans réseau et sans plein écran."""
    monkeypatch.setattr(
        grid_module.GridWindow, "_start_hype_watcher",
        lambda self: setattr(self, "_hype_watcher", None))
    # Les favoris passent devant dans l'ordre d'affichage : un favori hérité
    # d'un autre test rendrait l'ordre attendu imprévisible.
    monkeypatch.setattr(grid_widget.favorites, "get", lambda: set())
    w = grid_module.GridWindow(qapp.primaryScreen(), show_on_init=False)
    qtbot.addWidget(w)
    return w


def _peupler(fenetre, logins: list[str]) -> None:
    """Remplit les premières cellules, audience décroissante.

    On écrit directement dans les attributs de la cellule : `set_stream()`
    lancerait un flux mpv, ce qu'aucun test ne doit faire.
    """
    for i, cellule in enumerate(fenetre.grid._cells):
        if i < len(logins):
            cellule._twitch_login = logins[i]
            cellule._is_online = True
            cellule._viewers = 10_000 - i * 100
        else:
            cellule._twitch_login = ""
            cellule._is_online = False
            cellule._viewers = 0


def _touche(key) -> QKeyEvent:
    return QKeyEvent(QKeyEvent.Type.KeyPress, key,
                     Qt.KeyboardModifier.NoModifier)


# ── raccourcis clavier ───────────────────────────────────────────────────────

def test_echap_demande_le_retour_au_panel(fenetre):
    recu: list[int] = []
    fenetre.back_to_panel.connect(lambda: recu.append(1))
    fenetre.keyPressEvent(_touche(Qt.Key.Key_Escape))
    assert recu == [1]


@pytest.mark.parametrize("touche,attendu", [
    (Qt.Key.Key_1, "zerator"),
    (Qt.Key.Key_2, "domingo"),
    (Qt.Key.Key_3, "etoiles"),
])
def test_les_chiffres_ouvrent_la_cellule_correspondante(
        fenetre, touche, attendu):
    """1-9 désignent la position AFFICHÉE, pas l'index brut de la cellule."""
    _peupler(fenetre, ["zerator", "domingo", "etoiles"])
    recu: list[str] = []
    fenetre.stream_selected.connect(recu.append)
    fenetre.keyPressEvent(_touche(touche))
    assert recu == [attendu]


def test_les_chiffres_suivent_l_ordre_d_affichage_pas_la_position(fenetre):
    """La grille se réordonne par audience : la touche 1 doit suivre.

    Sinon le raccourci ouvrirait une autre chaîne que celle qu'on voit en
    première position, ce qui est exactement le piège que l'ordre affiché
    est censé éviter.
    """
    _peupler(fenetre, ["petit", "gros"])
    # « gros » double l'audience de « petit » : il doit passer devant.
    fenetre.grid._cells[0]._viewers = 10
    fenetre.grid._cells[1]._viewers = 90_000
    recu: list[str] = []
    fenetre.stream_selected.connect(recu.append)
    fenetre.keyPressEvent(_touche(Qt.Key.Key_1))
    assert recu == ["gros"]


@pytest.mark.parametrize("touche", [Qt.Key.Key_5, Qt.Key.Key_9])
def test_un_chiffre_sans_cellule_ne_fait_rien(fenetre, touche):
    """Neuf touches pour deux flux : les sept autres doivent rester muettes."""
    _peupler(fenetre, ["zerator", "domingo"])
    recu: list[str] = []
    fenetre.stream_selected.connect(recu.append)
    fenetre.keyPressEvent(_touche(touche))
    assert recu == []


def test_grille_vide_aucun_chiffre_n_emet(fenetre):
    _peupler(fenetre, [])
    recu: list[str] = []
    fenetre.stream_selected.connect(recu.append)
    for touche in (Qt.Key.Key_1, Qt.Key.Key_4, Qt.Key.Key_9):
        fenetre.keyPressEvent(_touche(touche))
    assert recu == []


@pytest.mark.parametrize("touche", [
    Qt.Key.Key_0,        # hors de la plage 1-9
    Qt.Key.Key_A,
    Qt.Key.Key_Space,
    Qt.Key.Key_F11,
])
def test_les_autres_touches_ne_declenchent_rien(fenetre, touche):
    _peupler(fenetre, ["zerator", "domingo"])
    recu: list[str] = []
    fenetre.stream_selected.connect(recu.append)
    fenetre.back_to_panel.connect(lambda: recu.append("retour"))
    fenetre.keyPressEvent(_touche(touche))
    assert recu == []


def test_une_cellule_vide_n_est_jamais_selectionnee(fenetre):
    """Les cellules sans login sont écartées AVANT le classement.

    Sans ce filtre, la touche 1 pourrait demander l'ouverture d'un login vide.
    """
    _peupler(fenetre, ["zerator"])
    recu: list[str] = []
    fenetre.stream_selected.connect(recu.append)
    for touche in (Qt.Key.Key_1, Qt.Key.Key_2):
        fenetre.keyPressEvent(_touche(touche))
    assert recu == ["zerator"]


# ── barre de retour ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("demande", [True, False])
def test_barre_retour_visible_seulement_en_mode_dual(
        qtbot, qapp, monkeypatch, demande):
    """En triple écran la grille a son moniteur : pas de bouton « ← Panel »."""
    monkeypatch.setattr(
        grid_module.GridWindow, "_start_hype_watcher",
        lambda self: setattr(self, "_hype_watcher", None))
    w = grid_module.GridWindow(qapp.primaryScreen(), show_on_init=False,
                               show_back_button=demande)
    qtbot.addWidget(w)
    assert w._back_bar.isHidden() is not demande


def test_le_bouton_retour_emet_back_to_panel(fenetre):
    recu: list[int] = []
    fenetre.back_to_panel.connect(lambda: recu.append(1))
    fenetre.back_to_panel.emit()
    assert recu == [1]


# ── alertes HypeWatcher ──────────────────────────────────────────────────────

@pytest.mark.parametrize("paquet,couleur,libelle,extrait", [
    # Format complet : couleur|libellé|extrait de chat.
    ("#ff0000|Pic de chat|aaaa", "#ff0000", "Pic de chat", "aaaa"),
    # Sans extrait : les deux premiers champs suffisent.
    ("#ff0000|Pic de chat", "#ff0000", "Pic de chat", ""),
    # Format d'origine, sans séparateur : tout est le libellé.
    ("Pic de chat", "#ff6b00", "Pic de chat", ""),
    # L'extrait peut lui-même contenir des « | » : il ne doit pas être coupé.
    ("#ff0000|Pic|a|b|c", "#ff0000", "Pic", "a|b|c"),
])
def test_alerte_hype_decodee(fenetre, paquet, couleur, libelle, extrait):
    """Le paquet vient d'un signal Qt à trois champs, tassés dans une chaîne.

    Les formats plus courts sont tolérés : un HypeWatcher d'une version
    antérieure ne doit pas faire perdre l'alerte.
    """
    _peupler(fenetre, ["zerator"])
    recu: list[tuple] = []
    fenetre.hype_alert.connect(lambda *a: recu.append(a))
    fenetre._on_hype_alert(0, paquet, 0.82)
    assert recu == [("zerator", libelle, pytest.approx(0.82), couleur, extrait)]


@pytest.mark.parametrize("index", [-1, 25, 999])
def test_alerte_hype_sur_une_cellule_inexistante_ne_plante_pas(fenetre, index):
    """La grille peut avoir changé entre l'analyse et l'alerte.

    L'index reçu est alors périmé : l'alerte doit partir sans login plutôt
    que de lever IndexError depuis un slot Qt.
    """
    _peupler(fenetre, ["zerator"])
    recu: list[tuple] = []
    fenetre.hype_alert.connect(lambda *a: recu.append(a))
    fenetre._on_hype_alert(index, "#ff0000|Pic|", 0.5)
    assert recu[0][0] == ""


# ── raids ────────────────────────────────────────────────────────────────────

def test_raid_relaye_quand_la_famille_est_active(fenetre, monkeypatch):
    from core import alerts
    monkeypatch.setattr(alerts, "enabled", lambda famille: True)
    _peupler(fenetre, ["cible"])
    recu: list[tuple] = []
    fenetre.raid_detected.connect(lambda *a: recu.append(a))
    fenetre._on_raid("source", "cible", 4200)
    assert recu == [("source", "cible", 4200)]


def test_raid_ignore_si_la_famille_est_coupee(fenetre, monkeypatch):
    """Une alerte désactivée n'est pas seulement masquée : elle n'existe pas.

    La cellule ne doit pas pulser non plus — sinon l'alerte reste visible
    alors que l'utilisateur l'a coupée.
    """
    from core import alerts
    monkeypatch.setattr(alerts, "enabled", lambda famille: False)
    pulses: list[tuple] = []
    monkeypatch.setattr(fenetre.grid, "pulse_cell",
                        lambda *a, **k: pulses.append(a))
    recu: list[tuple] = []
    fenetre.raid_detected.connect(lambda *a: recu.append(a))
    fenetre._on_raid("source", "cible", 4200)
    assert recu == []
    assert pulses == []


# ── configuration des clips ──────────────────────────────────────────────────

@pytest.mark.parametrize("cfg,actif,plafond", [
    ({}, False, 6),
    ({"clips": {}}, False, 6),
    ({"clips": {"auto_on_alert": True}}, True, 6),
    ({"clips": {"auto_on_alert": True, "auto_max_per_hour": 3}}, True, 3),
    # 0 ou None diraient « aucun clip » alors que l'option vient d'être
    # activée : le `or 6` les traite comme une absence de réglage.
    ({"clips": {"auto_on_alert": True, "auto_max_per_hour": 0}}, True, 6),
    ({"clips": {"auto_on_alert": True, "auto_max_per_hour": None}}, True, 6),
    ({"clips": {"auto_on_alert": True, "auto_max_per_hour": -3}}, True, 1),
    ({"clips": None}, False, 6),
])
def test_set_clip_config(fenetre, cfg, actif, plafond):
    fenetre.set_clip_config(cfg)
    assert fenetre._auto_clip is actif
    assert fenetre._auto_clip_max == plafond


def test_set_clip_config_tolere_none(fenetre):
    """L'appelant peut passer la configuration telle qu'il l'a lue, même vide."""
    fenetre.set_clip_config(None)
    assert fenetre._auto_clip is False


# ── clip automatique sur alerte ──────────────────────────────────────────────

@pytest.fixture
def clips_enregistres(fenetre, monkeypatch):
    """Remplace l'enregistrement réel par un compteur : aucun fichier écrit."""
    faits: list[str] = []

    def _save(login):
        faits.append(login)
        # La sauvegarde est asynchrone : elle rend « la demande est partie »,
        # pas un chemin — le fichier n'existe pas encore.
        return True

    monkeypatch.setattr(fenetre.grid, "save_clip", _save)
    return faits


def test_clip_automatique_inactif_par_defaut(fenetre, clips_enregistres):
    """Un event génère de quoi remplir un disque : jamais sans le demander."""
    fenetre._maybe_auto_clip("zerator")
    assert clips_enregistres == []


def test_clip_automatique_actif(fenetre, clips_enregistres):
    fenetre.set_clip_config({"clips": {"auto_on_alert": True}})
    fenetre._maybe_auto_clip("zerator")
    assert clips_enregistres == ["zerator"]


def test_clip_automatique_ignore_un_login_vide(fenetre, clips_enregistres):
    """Une alerte sur une cellule périmée n'a pas de login à enregistrer."""
    fenetre.set_clip_config({"clips": {"auto_on_alert": True}})
    fenetre._maybe_auto_clip("")
    assert clips_enregistres == []


def test_clip_automatique_plafonne_par_heure(fenetre, clips_enregistres):
    """Le plafond porte sur toute la grille, pas sur une chaîne.

    HypeWatcher peut signaler plusieurs moments par heure ; sans plafond, le
    dossier se remplirait en silence.
    """
    fenetre.set_clip_config(
        {"clips": {"auto_on_alert": True, "auto_max_per_hour": 2}})
    for _ in range(5):
        fenetre._maybe_auto_clip("zerator")
    assert len(clips_enregistres) == 2


def test_le_plafond_glisse_sur_une_heure(fenetre, clips_enregistres,
                                         monkeypatch):
    """Fenêtre glissante : passé 3600 s, le quota se libère à nouveau."""
    horloge = {"t": 1000.0}
    monkeypatch.setattr(grid_module.time, "monotonic", lambda: horloge["t"])
    fenetre.set_clip_config(
        {"clips": {"auto_on_alert": True, "auto_max_per_hour": 1}})

    fenetre._maybe_auto_clip("zerator")
    horloge["t"] += 60
    fenetre._maybe_auto_clip("zerator")     # dans l'heure : refusé
    assert len(clips_enregistres) == 1

    horloge["t"] += 3600
    fenetre._maybe_auto_clip("zerator")     # l'ancien point est sorti
    assert len(clips_enregistres) == 2


def test_un_enregistrement_rate_ne_consomme_pas_le_quota(fenetre, monkeypatch):
    """save_clip rend None quand rien n'a pu être écrit.

    Décompter ce non-clip du plafond ferait perdre le suivant, celui qui
    aurait peut-être marché.
    """
    monkeypatch.setattr(fenetre.grid, "save_clip", lambda login: None)
    fenetre.set_clip_config(
        {"clips": {"auto_on_alert": True, "auto_max_per_hour": 1}})
    fenetre._maybe_auto_clip("zerator")
    assert fenetre._auto_clip_times == []


# ── synchronisation avec le HypeWatcher ──────────────────────────────────────

class _FauxWatcher:
    """Enregistre ce que GridWindow lui transmet, sans rien surveiller."""

    def __init__(self) -> None:
        self.cells: list = []
        self.viewers: list = []
        self.arrete = False

    def update_cells(self, infos):
        self.cells.append(infos)

    def update_viewers(self, viewers):
        self.viewers.append(viewers)

    def stop(self):
        self.arrete = True


def test_refresh_sans_watcher_ne_plante_pas(fenetre):
    """Le HypeWatcher peut avoir échoué à démarrer : l'appli continue."""
    fenetre._hype_watcher = None
    fenetre.refresh_hype_cells()   # ne doit rien lever


def test_refresh_ne_transmet_que_les_cellules_en_direct(fenetre):
    """Une cellule hors ligne ou vide n'a pas de chat à surveiller."""
    _peupler(fenetre, ["zerator", "domingo", "etoiles"])
    fenetre.grid._cells[1]._is_online = False
    watcher = _FauxWatcher()
    fenetre._hype_watcher = watcher

    fenetre.refresh_hype_cells()

    logins = [login for _idx, login, _mpv in watcher.cells[0]]
    assert logins == ["zerator", "etoiles"]


def test_refresh_conserve_l_index_reel_de_la_cellule(fenetre):
    """L'index sert à retrouver la cellule à faire pulser : c'est l'index BRUT.

    Le renuméroter en sautant les cellules éteintes ferait pulser la mauvaise.
    """
    _peupler(fenetre, ["zerator", "domingo", "etoiles"])
    fenetre.grid._cells[0]._is_online = False
    watcher = _FauxWatcher()
    fenetre._hype_watcher = watcher

    fenetre.refresh_hype_cells()

    assert [idx for idx, _l, _m in watcher.cells[0]] == [1, 2]


def test_refresh_transmet_les_audiences(fenetre):
    """L'accélération de l'audience est un troisième signal, déjà disponible."""
    _peupler(fenetre, ["zerator", "domingo"])
    watcher = _FauxWatcher()
    fenetre._hype_watcher = watcher
    fenetre.refresh_hype_cells()
    assert watcher.viewers[0] == {"zerator": 10_000, "domingo": 9_900}


def test_refresh_sans_audience_n_appelle_pas_update_viewers(fenetre):
    """Aucune audience connue : envoyer un dict vide n'apprendrait rien."""
    _peupler(fenetre, ["zerator"])
    fenetre.grid._cells[0]._viewers = 0
    watcher = _FauxWatcher()
    fenetre._hype_watcher = watcher
    fenetre.refresh_hype_cells()
    assert watcher.viewers == []


def test_fermer_arrete_le_watcher(fenetre):
    """Sans arrêt explicite, les connexions IRC survivraient à la fenêtre."""
    watcher = _FauxWatcher()
    fenetre._hype_watcher = watcher
    fenetre.close()
    assert watcher.arrete is True


def test_fermer_sans_watcher_ne_plante_pas(fenetre):
    fenetre._hype_watcher = None
    fenetre.close()


# ── démarrage du HypeWatcher ─────────────────────────────────────────────────

class _FauxSignal:
    """Signal minimal : GridWindow ne fait que s'y abonner."""

    def __init__(self) -> None:
        self.slots: list = []

    def connect(self, slot):
        self.slots.append(slot)


class _FauxHypeWatcher:
    """HypeWatcher de remplacement : il note sa configuration, sans réseau."""

    dernier: "_FauxHypeWatcher | None" = None

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.demarre = False
        self.alert_triggered = _FauxSignal()
        self.raid_detected = _FauxSignal()
        _FauxHypeWatcher.dernier = self

    def start(self):
        self.demarre = True

    def stop(self):
        """Rien à arrêter : ce double n'a jamais rien ouvert."""


@pytest.fixture
def grille_avec_watcher(qtbot, qapp, monkeypatch, tmp_path):
    """Construit une GridWindow en laissant _start_hype_watcher s'exécuter.

    Le vrai HypeWatcher est remplacé et la configuration détournée vers
    tmp_path : le démarrage est bien exercé, mais rien ne sort du processus.
    """
    import core.hype_watcher
    import core.paths

    def _construire(contenu: str | None, classe=_FauxHypeWatcher):
        chemin = tmp_path / "config.json"
        if contenu is not None:
            chemin.write_text(contenu, encoding="utf-8")
        monkeypatch.setattr(core.paths, "CONFIG_PATH", chemin)
        monkeypatch.setattr(core.hype_watcher, "HypeWatcher", classe)
        w = grid_module.GridWindow(qapp.primaryScreen(), show_on_init=False)
        qtbot.addWidget(w)
        return w

    return _construire


def test_le_watcher_recoit_la_configuration_du_fichier(grille_avec_watcher):
    """Les seuils réglés dans la fenêtre de réglages doivent l'atteindre."""
    fenetre = grille_avec_watcher('{"hypewatcher": {"cooldown_s": 42}}')
    assert fenetre._hype_watcher.cfg["hypewatcher"]["cooldown_s"] == 42
    assert fenetre._hype_watcher.demarre is True


def test_sans_fichier_le_watcher_demarre_avec_ses_defauts(grille_avec_watcher):
    """Premier lancement : pas de config.json, et pourtant la grille s'ouvre."""
    fenetre = grille_avec_watcher(None)
    assert fenetre._hype_watcher.cfg == {}


@pytest.mark.parametrize("contenu", ["{ pas du json", "[]"])
def test_une_configuration_illisible_n_empeche_pas_la_grille(
        grille_avec_watcher, contenu):
    """La grille est la vue principale : elle doit s'ouvrir quoi qu'il arrive.

    Un HypeWatcher absent ne coûte que les alertes ; une exception ici
    coûterait la fenêtre entière.
    """
    fenetre = grille_avec_watcher(contenu)
    assert fenetre._hype_watcher is None or fenetre._hype_watcher.cfg == []


def test_un_watcher_qui_refuse_de_demarrer_est_oublie(grille_avec_watcher):
    """`_hype_watcher` doit valoir None, pas rester non défini.

    refresh_hype_cells() et closeEvent() interrogent cet attribut : le laisser
    absent transformerait une panne d'alertes en AttributeError.
    """
    class _Cassee:
        def __init__(self, cfg):
            raise RuntimeError("pas de réseau")

    fenetre = grille_avec_watcher("{}", classe=_Cassee)
    assert fenetre._hype_watcher is None
    fenetre.refresh_hype_cells()   # ne doit rien lever


def test_la_fenetre_ne_fait_plus_clignoter_elle_meme(fenetre, monkeypatch):
    """Elle relaie, elle ne décide pas.

    Le clignotement se faisait ici, avant tout contrôle : la cellule s'allumait
    pour n'importe quel raid, y compris venu d'une chaîne étrangère au ZEvent,
    quand la bannière et le fil d'événements ne retenaient que ceux entre
    participants. La fenêtre ne connaît pas cette liste ; main.py, si.
    """
    from core import alerts
    monkeypatch.setattr(alerts, "enabled", lambda famille: True)
    _peupler(fenetre, ["cible"])
    pulses: list[tuple] = []
    monkeypatch.setattr(fenetre.grid, "pulse_cell",
                        lambda *a, **k: pulses.append(a))
    fenetre._on_raid("source", "cible", 4200)
    assert pulses == []
