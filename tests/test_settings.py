# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Fenêtre de réglages : lecture de config.json, validation, écriture.

Ces pages sont le seul endroit où l'utilisateur écrit dans config.json depuis
l'interface. Deux choses comptent donc, et ce sont elles qui sont testées ici :

- ce qui est LU est ramené à quelque chose de tenable, parce que config.json
  est un fichier texte que la documentation invite explicitement à éditer à la
  main — une valeur aberrante ne doit pas se propager telle quelle ;
- ce qui est ÉCRIT l'est par fusion, sans effacer les clés que d'autres
  chemins (favoris, assistant, rappels) ont posées entre-temps.

Rien ici ne vérifie la mise en page : seulement les valeurs qui transitent.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from core import config_store
from core.stream_manager import QUALITY_GRID
from windows import settings


# ── outillage ────────────────────────────────────────────────────────────────

@pytest.fixture
def config_fichier(tmp_path, monkeypatch):
    """Détourne config.json vers tmp_path, pour les tests qui sauvegardent."""
    cible = tmp_path / "config.json"
    monkeypatch.setattr(config_store, "CONFIG_PATH", cible)
    return cible


def _page(qtbot, classe, config=None):
    """Construit une page de réglages et la confie à qtbot pour destruction."""
    page = classe(dict(config or {}))
    qtbot.addWidget(page)
    return page


def _collecte(page, config=None):
    """Raccourci : appelle collect() sur un dict neuf et le rend."""
    cible = dict(config or {})
    page.collect(cible)
    return cible


# ── page Streams ─────────────────────────────────────────────────────────────

def test_streams_valeurs_par_defaut(qtbot):
    """Une configuration vide doit produire un jeu de valeurs complet.

    C'est le cas du tout premier lancement : rien à lire, et pourtant la
    sauvegarde doit écrire des réglages exploitables.
    """
    assert _collecte(_page(qtbot, settings._PageStreams)) == {
        "grid_adaptive": True,
        "grid_quality": QUALITY_GRID,
        "fullscreen_quality": "best",
        "max_active_streams": 20,
        "grid_sort": "viewers",
    }


#: Les quatre échelles proposées par la fenêtre des réglages.
_ECHELLES = (
    "160p,160p30,worst",
    QUALITY_GRID,
    "480p,480p30,360p,360p30,160p,160p30,worst",
    "720p60,720p,720p30,480p,480p30,360p,360p30,worst",
)


@pytest.mark.parametrize("enregistre,attendu", [
    # Première génération : les graphies nues.
    ("360p,worst",      QUALITY_GRID),
    ("480p,360p,worst", _ECHELLES[2]),
    ("720p,480p,worst", _ECHELLES[3]),
    # Deuxième génération : les graphies suffixées SEULES. Elles échouaient sur
    # toute chaîne dont le transcodage n'expose que « 360p ».
    ("160p30,worst",         _ECHELLES[0]),
    ("360p30,160p30,worst",  QUALITY_GRID),
    ("480p30,360p30,160p30", _ECHELLES[2]),
    ("720p60,480p30,360p30", _ECHELLES[3]),
])
def test_streams_migre_les_qualites_heritees(qtbot, enregistre, attendu):
    page = _page(qtbot, settings._PageStreams, {"grid_quality": enregistre})
    assert _collecte(page)["grid_quality"] == attendu


@pytest.mark.parametrize("aberrant", ["1440p", "", "n'importe quoi", "best"])
def test_streams_qualite_inconnue_retombe_sur_la_premiere_entree(qtbot, aberrant):
    """Une qualité absente de la liste ne doit pas être réécrite telle quelle.

    findText() rend -1 et l'index reste à 0 : la valeur sauvegardée est donc
    toujours l'une des échelles que Twitch sert réellement.
    """
    page = _page(qtbot, settings._PageStreams, {"grid_quality": aberrant})
    assert _collecte(page)["grid_quality"] in set(_ECHELLES)


@pytest.mark.parametrize("echelle", _ECHELLES)
def test_chaque_echelle_proposee_finit_par_un_repli_garanti(echelle):
    """Sans « worst » en fin de liste, un palier absent = une cellule noire.

    Relevé sur des chaînes de l'event : « 480p30,360p30,160p30 » ne trouvait
    RIEN sur une chaîne qui n'expose que « 160p, 360p, 480p », et streamlink
    sortait en code 1.
    """
    assert echelle.split(",")[-1] in ("worst", "best")


@pytest.mark.parametrize("echelle", _ECHELLES)
def test_chaque_echelle_propose_les_deux_graphies(echelle):
    """Twitch nomme le même palier « 360p » ici et « 360p30 » là."""
    paliers = echelle.split(",")
    for nom in paliers:
        if nom in ("worst", "best") or nom.endswith("60"):
            continue
        jumeau = nom[:-2] if nom.endswith("30") else nom + "30"
        assert jumeau in paliers, f"{nom} sans sa graphie jumelle {jumeau}"


@pytest.mark.parametrize("enregistre,attendu", [
    (999, 25),      # au-delà du plafond du QSpinBox
    (0, 1),         # zéro stream actif n'a pas de sens
    (-40, 1),
    (7, 7),         # valeur raisonnable : conservée
])
def test_streams_borne_le_nombre_de_flux(qtbot, enregistre, attendu):
    page = _page(qtbot, settings._PageStreams,
                 {"max_active_streams": enregistre})
    assert _collecte(page)["max_active_streams"] == attendu


def test_streams_supporte_un_nombre_ecrit_en_texte(qtbot):
    """config.json est éditable à la main : « 20 » y est un lapsus banal."""
    _page(qtbot, settings._PageStreams, {"max_active_streams": "20"})


@pytest.mark.parametrize("enregistre,attendu", [
    ("manual", "manual"),
    ("favorites", "favorites"),
    ("viewers", "viewers"),
    ("inconnu", "viewers"),   # repli sur le tri par audience
    (None, "viewers"),
])
def test_streams_disposition_de_grille(qtbot, enregistre, attendu):
    """La disposition est portée par itemData, pas par le libellé affiché."""
    page = _page(qtbot, settings._PageStreams, {"grid_sort": enregistre})
    assert _collecte(page)["grid_sort"] == attendu


def test_streams_qualite_fixe_desactivee_quand_adaptatif(qtbot):
    """Choisir une qualité fixe n'a pas de sens si l'adaptatif décide."""
    page = _page(qtbot, settings._PageStreams, {"grid_adaptive": True})
    assert page._grid_quality.isEnabled() is False
    page._adaptive.setChecked(False)
    assert page._grid_quality.isEnabled() is True
    page._adaptive.setChecked(True)
    assert page._grid_quality.isEnabled() is False


def test_streams_qualite_fixe_active_si_adaptatif_coupe_au_chargement(qtbot):
    page = _page(qtbot, settings._PageStreams, {"grid_adaptive": False})
    assert page._grid_quality.isEnabled() is True
    assert _collecte(page)["grid_adaptive"] is False


# ── page Écrans ──────────────────────────────────────────────────────────────

def test_ecrans_exige_au_moins_un_fullscreen(qtbot):
    """Sans écran en Fullscreen, ZLink n'a nulle part où jouer un flux.

    collect() doit alors refuser, expliquer, et surtout ne rien écrire dans la
    configuration — un refus qui laisserait des traces serait pire que rien.
    """
    page = _page(qtbot, settings._PageScreens)
    for combo in page._screen_combos:
        combo.setCurrentIndex(0)          # « — Désactivé » partout
    config = {"autre_cle": "intacte"}
    assert page.collect(config) is False
    assert "screen_assignments" not in config
    assert config["autre_cle"] == "intacte"
    assert page._error_lbl.text() != ""


def test_ecrans_accepte_et_efface_le_message_precedent(qtbot):
    page = _page(qtbot, settings._PageScreens)
    for combo in page._screen_combos:
        combo.setCurrentIndex(0)
    page.collect({})                       # provoque le message d'erreur
    page._screen_combos[0].setCurrentIndex(2)   # Fullscreen
    config: dict = {}
    assert page.collect(config) is True
    assert page._error_lbl.text() == ""
    assert config["screen_assignments"]["0"] == "fullscreen"


def test_ecrans_desactives_absents_de_la_configuration(qtbot):
    """Un écran désactivé s'écrit par son ABSENCE, pas par un rôle « disabled ».

    Sans quoi la relecture au démarrage devrait connaître ce rôle sentinelle.
    """
    page = _page(qtbot, settings._PageScreens)
    page._screen_combos[0].setCurrentIndex(2)
    for combo in page._screen_combos[1:]:
        combo.setCurrentIndex(0)
    config: dict = {}
    page.collect(config)
    assert all(role != "disabled"
               for role in config["screen_assignments"].values())


@pytest.mark.parametrize("role,index", [
    ("panel", 1), ("fullscreen", 2), ("grid", 3),
    ("rôle inventé", 0),   # rôle inconnu : désactivé, pas un index au hasard
])
def test_ecrans_relit_le_role_enregistre(qtbot, role, index):
    page = _page(qtbot, settings._PageScreens,
                 {"screen_assignments": {"0": role}})
    assert page._screen_combos[0].currentIndex() == index


def test_ecrans_un_seul_moniteur_est_fullscreen_par_defaut(qtbot):
    """Avec un seul écran, le défaut doit rester utilisable sans réglage."""
    page = _page(qtbot, settings._PageScreens)
    if len(page._screen_combos) == 1:
        assert page.collect({}) is True


# ── page Alertes ─────────────────────────────────────────────────────────────

def test_alertes_toutes_les_familles_sont_ecrites(qtbot):
    """Une famille absente du dict écrit ne serait jamais réactivable."""
    from core.alerts import FAMILLES
    collecte = _collecte(_page(qtbot, settings._PageHype))
    assert set(collecte["alerts"]) == {cle for cle, _l, _d, _a in FAMILLES}


def test_alertes_une_famille_coupee_le_reste(qtbot):
    page = _page(qtbot, settings._PageHype, {"alerts": {"raid": False}})
    collecte = _collecte(page)
    assert collecte["alerts"]["raid"] is False
    # Les familles non citées gardent leur défaut, elles ne suivent pas.
    assert collecte["alerts"]["hype"] is True


@pytest.mark.parametrize("enregistre,attendu", [
    (0.70, 0.70),
    (0.95, 0.95),
    (0.99, 0.95),   # au-delà du curseur : ramené au plafond
    (0.10, 0.50),   # en deçà : ramené au plancher
])
def test_alertes_seuil_immediat_borne(qtbot, enregistre, attendu):
    """Le seuil est stocké en fraction et manipulé en pourcents entiers.

    L'aller-retour /100 puis ×100 doit rendre la valeur d'origine, bornée aux
    limites du curseur — un seuil à 0 ou à 1 rendrait l'alerte inutilisable.
    """
    page = _page(qtbot, settings._PageHype,
                 {"hypewatcher": {"score_high": enregistre}})
    assert _collecte(page)["hypewatcher"]["score_high"] == pytest.approx(attendu)


@pytest.mark.parametrize("enregistre,attendu", [
    (0.50, 0.50), (0.20, 0.20), (0.01, 0.20), (0.90, 0.70),
])
def test_alertes_seuil_confirme_borne(qtbot, enregistre, attendu):
    page = _page(qtbot, settings._PageHype,
                 {"hypewatcher": {"score_medium": enregistre}})
    assert (_collecte(page)["hypewatcher"]["score_medium"]
            == pytest.approx(attendu))


@pytest.mark.parametrize("cle,enregistre,attendu", [
    ("cooldown_s", 600, 600),
    ("cooldown_s", 1, 30),          # un cooldown d'une seconde n'en est pas un
    ("cooldown_s", 99999, 3600),
    ("alerts_per_hour", 8, 8),
    ("alerts_per_hour", 0, 1),
    ("alerts_per_hour", 999, 60),   # plafond : pas d'alerte en continu
])
def test_alertes_cadence_bornee(qtbot, cle, enregistre, attendu):
    page = _page(qtbot, settings._PageHype, {"hypewatcher": {cle: enregistre}})
    assert _collecte(page)["hypewatcher"][cle] == attendu


@pytest.mark.parametrize("cle,enregistre,attendu", [
    ("threshold", 1000, 1000),
    ("threshold", 1, 50),           # un don de 1 € n'est pas un afflux
    ("threshold", 10 ** 9, 100_000),
    ("per_hour", 12, 12),
    ("per_hour", 0, 1),
    ("per_hour", 500, 120),
])
def test_dons_seuils_bornes(qtbot, cle, enregistre, attendu):
    page = _page(qtbot, settings._PageHype, {"donations": {cle: enregistre}})
    assert _collecte(page)["donations"][cle] == attendu


def test_alertes_section_avancee_suit_l_interrupteur(qtbot):
    """Les seuils fins n'ont pas à rester visibles si la détection est coupée.

    isHidden() plutôt que isVisible() : la page n'est pas affichée pendant le
    test, donc isVisible() rendrait False quoi qu'il arrive. isHidden() rapporte
    bien le masquage explicite demandé par le code.
    """
    page = _page(qtbot, settings._PageHype, {"hypewatcher": {"enabled": False}})
    assert page._advanced.isHidden() is True
    page._enabled_cb.setChecked(True)
    assert page._advanced.isHidden() is False
    page._enabled_cb.setChecked(False)
    assert page._advanced.isHidden() is True


def test_alertes_detection_coupee_reste_coupee_apres_collecte(qtbot):
    page = _page(qtbot, settings._PageHype, {"hypewatcher": {"enabled": False}})
    assert _collecte(page)["hypewatcher"]["enabled"] is False


def test_son_desactive_par_defaut(qtbot):
    """Un son non demandé se superpose au direct qu'on écoute : jamais d'office."""
    collecte = _collecte(_page(qtbot, settings._PageHype))
    assert collecte["sounds"]["enabled"] is False


def test_son_volume_et_ligne_liee_a_la_case(qtbot):
    page = _page(qtbot, settings._PageHype,
                 {"sounds": {"enabled": True, "volume": 35}})
    assert page._son_row_widget.isEnabled() is True
    assert _collecte(page)["sounds"]["volume"] == 35
    page._son_actif.setChecked(False)
    assert page._son_row_widget.isEnabled() is False


@pytest.mark.parametrize("enregistre,attendu", [(5, 10), (200, 100), (60, 60)])
def test_son_volume_borne(qtbot, enregistre, attendu):
    page = _page(qtbot, settings._PageHype, {"sounds": {"volume": enregistre}})
    assert _collecte(page)["sounds"]["volume"] == attendu


def test_alertes_collect_conserve_les_autres_cles_de_hypewatcher(qtbot):
    """collect() complète le sous-dict existant au lieu de le remplacer.

    HypeWatcher lit dans ce même sous-dict des réglages que la page n'expose
    pas : les écraser les ferait disparaître à la première sauvegarde.
    """
    config = {"hypewatcher": {"reglage_interne": 42}}
    page = _page(qtbot, settings._PageHype, config)
    resultat = _collecte(page, config)
    assert resultat["hypewatcher"]["reglage_interne"] == 42
    assert "enabled" in resultat["hypewatcher"]


# ── page Clips ───────────────────────────────────────────────────────────────

def test_clips_dossier_par_defaut_si_absent(qtbot):
    """Sans dossier choisi, la page en propose un plutôt que d'écrire du vide."""
    collecte = _collecte(_page(qtbot, settings._PageClips))
    assert collecte["clips"]["directory"].endswith("ZLink")


def test_clips_dossier_saisi_est_deshabille(qtbot, tmp_path):
    """Les espaces autour d'un chemin collé produiraient un dossier fantôme."""
    page = _page(qtbot, settings._PageClips)
    page._directory.setText(f"  {tmp_path}  ")
    assert _collecte(page)["clips"]["directory"] == str(tmp_path)


@pytest.mark.parametrize("enregistre,attendu", [
    (60, 60), (5, 10), (10_000, 300), (300, 300),
])
def test_clips_duree_bornee(qtbot, enregistre, attendu):
    """Un clip de 3 s ou d'une heure ne correspond à aucun usage réel."""
    page = _page(qtbot, settings._PageClips,
                 {"clips": {"duration_secs": enregistre}})
    assert _collecte(page)["clips"]["duration_secs"] == attendu


def test_clips_supporte_une_duree_ecrite_en_texte(qtbot):
    _page(qtbot, settings._PageClips, {"clips": {"duration_secs": "60"}})


def test_clips_automatique_desactive_par_defaut(qtbot):
    """Une alerte n'est pas forcément un moment à garder : jamais d'office.

    Le plafond horaire doit exister quand même, il sert dès l'activation.
    """
    collecte = _collecte(_page(qtbot, settings._PageClips))
    assert collecte["clips"]["auto_on_alert"] is False
    assert collecte["clips"]["auto_max_per_hour"] == 6


def test_clips_plafond_editable_seulement_si_automatisme_actif(qtbot):
    page = _page(qtbot, settings._PageClips)
    assert page._auto_max.isEnabled() is False
    page._auto_clip.setChecked(True)
    assert page._auto_max.isEnabled() is True
    page._auto_clip.setChecked(False)
    assert page._auto_max.isEnabled() is False


@pytest.mark.parametrize("enregistre,attendu", [
    (6, 6), (0, 1), (999, 60),
])
def test_clips_plafond_horaire_borne(qtbot, enregistre, attendu):
    page = _page(qtbot, settings._PageClips,
                 {"clips": {"auto_max_per_hour": enregistre}})
    assert _collecte(page)["clips"]["auto_max_per_hour"] == attendu


def test_clips_collect_conserve_les_autres_cles(qtbot):
    config = {"clips": {"reglage_inconnu": "gardé"}}
    page = _page(qtbot, settings._PageClips, config)
    assert _collecte(page, config)["clips"]["reglage_inconnu"] == "gardé"


# ── page Crédits ─────────────────────────────────────────────────────────────

def test_credits_n_ecrit_rien(qtbot):
    """Page purement informative : collect() ne doit toucher à rien."""
    page = _page(qtbot, settings._PageCredits)
    config = {"intact": True}
    page.collect(config)
    assert config == {"intact": True}


# ── SettingsPanel ────────────────────────────────────────────────────────────

@pytest.fixture
def panneau(qtbot, config_fichier):
    p = settings.SettingsPanel()
    qtbot.addWidget(p)
    return p


def test_panneau_navigation(panneau):
    """Un seul élément actif à la fois, et la pile suit ce choix."""
    for index, (libelle, _icone) in enumerate(settings._NAV_ITEMS):
        panneau._switch_page(libelle)
        assert panneau._pages_stack.currentIndex() == index
        actifs = [i for i, item in enumerate(panneau._nav_items)
                  if item._active]
        assert actifs == [index]


def test_panneau_ouvre_sur_la_premiere_page(panneau):
    assert panneau._pages_stack.currentIndex() == 0


def test_sauvegarde_ecrit_le_fichier_et_previent(panneau, config_fichier, qtbot):
    """La sauvegarde doit à la fois persister ET notifier le reste de l'appli."""
    recu: list[dict] = []
    panneau.settings_changed.connect(recu.append)
    panneau._page_streams._max_streams.setValue(9)

    panneau._on_save()

    ecrit = json.loads(config_fichier.read_text(encoding="utf-8"))
    assert ecrit["max_active_streams"] == 9
    assert len(recu) == 1
    assert recu[0]["max_active_streams"] == 9
    assert panneau._footer_error.text() == ""


def test_sauvegarde_ne_remplace_pas_les_cles_ecrites_ailleurs(
        panneau, config_fichier):
    """Le panneau lit la config à son ouverture ; il ne doit pas la figer.

    Favoris, rappels du programme et assistant écrivent le même fichier
    pendant que la fenêtre est ouverte. Une écriture non fusionnée les
    rétablirait à leur valeur d'il y a dix minutes.
    """
    config_fichier.write_text(
        json.dumps({"favorites": ["zerator"]}), encoding="utf-8")
    panneau._on_save()
    ecrit = json.loads(config_fichier.read_text(encoding="utf-8"))
    assert ecrit["favorites"] == ["zerator"]
    assert "grid_adaptive" in ecrit


def test_sauvegarde_bloquee_par_un_ecran_invalide(panneau, config_fichier):
    """Un réglage d'écrans intenable doit arrêter TOUTE la sauvegarde.

    Écrire les autres pages et abandonner celle-là laisserait une
    configuration à moitié appliquée, sans que rien ne le dise.
    """
    recu: list[dict] = []
    panneau.settings_changed.connect(recu.append)
    for combo in panneau._page_screens._screen_combos:
        combo.setCurrentIndex(0)

    panneau._on_save()

    assert config_fichier.exists() is False
    assert recu == []
    assert panneau._footer_error.text() != ""
    # L'utilisateur est renvoyé sur la page fautive, pas laissé à deviner.
    index_ecrans = [libelle for libelle, _i in settings._NAV_ITEMS].index(
        settings._TITRE_ECRANS)
    assert panneau._pages_stack.currentIndex() == index_ecrans


def test_bouton_sauvegarder_se_verrouille_puis_revient(panneau):
    """Le double-clic sur « Sauvegarder » ne doit pas écrire deux fois."""
    panneau._on_save()
    assert panneau._save_btn.isEnabled() is False
    panneau._reset_save_btn()
    assert panneau._save_btn.isEnabled() is True
    assert panneau._save_btn.text() == "Sauvegarder"


def test_refresh_config_relit_le_fichier(panneau, config_fichier):
    """Rouvrir le panneau après une modification externe doit la voir."""
    config_fichier.write_text(
        json.dumps({"venu_d_ailleurs": 1}), encoding="utf-8")
    assert "venu_d_ailleurs" not in panneau._config
    panneau.refresh_config()
    assert panneau._config["venu_d_ailleurs"] == 1


def test_fermer_emet_close_requested(panneau, qtbot):
    recu: list[int] = []
    panneau.close_requested.connect(lambda: recu.append(1))
    panneau.close_requested.emit()
    assert recu == [1]


def test_config_illisible_n_empeche_pas_l_ouverture(qtbot, config_fichier):
    """Un config.json corrompu ne doit pas rendre les réglages inaccessibles.

    C'est justement là qu'on en a besoin pour réparer.
    """
    config_fichier.write_text("{ pas du json", encoding="utf-8")
    p = settings.SettingsPanel()
    qtbot.addWidget(p)
    assert p._config == {}


# ── éléments de navigation ───────────────────────────────────────────────────

def test_navitem_emet_au_clic_gauche_seulement(qtbot):
    """Un clic droit sur la barre latérale ne doit pas changer de page."""
    from PyQt6.QtCore import QPoint, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent

    item = settings._NavItem("Streams", "")
    qtbot.addWidget(item)
    recu: list[int] = []
    item.clicked.connect(lambda: recu.append(1))

    for bouton, attendu in ((Qt.MouseButton.RightButton, 0),
                            (Qt.MouseButton.LeftButton, 1)):
        item.mousePressEvent(QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            QPointF(QPoint(5, 5)), bouton, bouton,
            Qt.KeyboardModifier.NoModifier))
        assert len(recu) == attendu


def test_navitem_change_de_style_quand_actif(qtbot):
    item = settings._NavItem("Streams", "")
    qtbot.addWidget(item)
    inactif = item.styleSheet()
    item.set_active(True)
    assert item._active is True
    assert item.styleSheet() != inactif
    item.set_active(False)
    assert item.styleSheet() == inactif


# ── attribution par défaut selon le nombre de moniteurs ──────────────────────

class _FauxEcran:
    """Écran simulé : seule sa géométrie intéresse la page.

    L'environnement de test n'a qu'un moniteur virtuel ; les défauts à deux et
    trois écrans — la vraie logique d'attribution — resteraient sinon hors de
    portée.
    """

    def __init__(self, x: int) -> None:
        from PyQt6.QtCore import QRect
        self._g = QRect(x, 0, 1920, 1080)

    def geometry(self):     # noqa: N802 (API Qt)
        return self._g


def _page_ecrans(qtbot, nombre: int, config=None):
    """Construit la page Écrans en lui faisant croire à `nombre` moniteurs.

    Le remplacement de QApplication.instance() ne dure QUE la construction :
    la page ne relit jamais les écrans ensuite, et pytest-qt a besoin du vrai
    QApplication pour son ménage de fin de test.
    """
    faux = type("FauxApp", (), {
        "screens": lambda self: [_FauxEcran(i * 1920) for i in range(nombre)],
    })()
    with mock.patch.object(settings.QApplication, "instance",
                           staticmethod(lambda: faux)):
        page = settings._PageScreens(dict(config or {}))
    qtbot.addWidget(page)
    return page


@pytest.mark.parametrize("nombre,attendu", [
    # Un seul moniteur : il doit jouer le flux, sinon ZLink n'affiche rien.
    (1, {"0": "fullscreen"}),
    # Deux : le panel d'un côté, le direct de l'autre.
    (2, {"0": "panel", "1": "fullscreen"}),
    # Trois et plus : la grille prend le troisième, les suivants restent libres.
    (3, {"0": "panel", "1": "fullscreen", "2": "grid"}),
    (4, {"0": "panel", "1": "fullscreen", "2": "grid"}),
])
def test_attribution_par_defaut_selon_le_nombre_d_ecrans(
        qtbot, nombre, attendu):
    page = _page_ecrans(qtbot, nombre)
    config: dict = {}
    assert page.collect(config) is True
    assert config["screen_assignments"] == attendu


def test_un_combo_par_ecran(qtbot):
    """Un moniteur absent de la liste ne serait attribuable par personne."""
    assert len(_page_ecrans(qtbot, 3)._screen_combos) == 3


def test_une_attribution_enregistree_prime_sur_le_defaut(qtbot):
    """Un choix explicite ne doit jamais être écrasé par la règle du défaut."""
    page = _page_ecrans(qtbot, 3, {"screen_assignments": {"0": "fullscreen"}})
    config: dict = {}
    page.collect(config)
    assert config["screen_assignments"] == {"0": "fullscreen"}


# ── actions annexes ──────────────────────────────────────────────────────────

def test_parcourir_remplace_le_dossier_choisi(qtbot, monkeypatch, tmp_path):
    """Le sélecteur de dossier est modal : on ne l'ouvre pas dans un test."""
    monkeypatch.setattr(settings.QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: str(tmp_path)))
    page = _page(qtbot, settings._PageClips)
    page._browse()
    assert _collecte(page)["clips"]["directory"] == str(tmp_path)


def test_parcourir_annule_conserve_le_dossier_precedent(qtbot, monkeypatch):
    """Annuler rend une chaîne vide : l'écrire effacerait le choix existant."""
    monkeypatch.setattr(settings.QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: ""))
    page = _page(qtbot, settings._PageClips,
                 {"clips": {"directory": "D:/clips"}})
    page._browse()
    assert page._directory.text() == "D:/clips"


def test_ecouter_joue_les_deux_sons_meme_si_coupes(qtbot, monkeypatch):
    """Le bouton sert à choisir un volume : il doit s'entendre même quand
    l'option est décochée, d'où le `force`."""
    from core import sounds
    joues: list[tuple] = []
    reglages: list[dict] = []
    monkeypatch.setattr(sounds, "configure", reglages.append)
    monkeypatch.setattr(sounds, "play",
                        lambda nom, force=False: joues.append((nom, force)))

    page = _page(qtbot, settings._PageHype, {"sounds": {"volume": 42}})
    page._on_son_test()

    assert reglages[0]["sounds"] == {"enabled": True, "volume": 42}
    assert joues == [("milestone", True)]
    # Le second son part en différé pour ne pas se superposer au premier.
    qtbot.wait(1300)
    assert ("goal", True) in joues
