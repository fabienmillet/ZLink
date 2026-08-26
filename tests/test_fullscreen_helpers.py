# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Helpers du plein écran, et l'ouverture d'une page de don.

`ouvrir_page_de_don` est la fonction la plus sensible du fichier : elle décide
d'envoyer l'utilisateur, dans son navigateur, vers une URL venue de l'API. Une
allowlist trouée y enverrait n'importe où — sans barre d'adresse pour s'en
apercevoir dans la vue intégrée.
"""

from __future__ import annotations

import json
import tempfile

import pytest
from PyQt6.QtGui import QColor, QPixmap

from core.replay_hd import REPLAY_SECS
from windows import fullscreen


# ── audiences ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("n,attendu", [
    (0, ""),                 # zéro : rien plutôt qu'un « 0 » qui n'apprend rien
    (1, "1"), (999, "999"),
    (1000, "1.0k"), (42000, "42.0k"),
    (1_000_000, "1.0M"), (16_172_355, "16.2M"),
])
def test_audiences_abregees(n, attendu):
    assert fullscreen._fmt_viewers(n) == attendu


# ── infobulles ───────────────────────────────────────────────────────────────

def test_infobulle_neutralise_le_texte_riche():
    """Qt rend le texte riche dans les infobulles : une balise y serait active."""
    sortie = fullscreen._infobulle('<img src="https://pisteur.test/x.png">')
    assert "<img" not in sortie and "&lt;img" in sortie
    assert sortie.startswith("<qt>") and sortie.endswith("</qt>")


# ── ouverture d'une page de don ──────────────────────────────────────────────

@pytest.fixture
def navigateur(monkeypatch):
    """Capture les URL au lieu de les ouvrir réellement."""
    ouvertes: list[str] = []
    monkeypatch.setattr(fullscreen.QDesktopServices, "openUrl",
                        lambda url: ouvertes.append(url.toString()))
    monkeypatch.setattr(fullscreen, "ceder_premier_plan", lambda: None)
    return ouvertes


def test_une_url_zevent_est_ouverte(navigateur):
    assert fullscreen.ouvrir_page_de_don("https://zevent.fr/dons") is True
    assert navigateur == ["https://zevent.fr/dons"]


@pytest.mark.parametrize("url", [
    "", None,
    "http://zevent.fr/dons",              # en clair
    "https://evil.test/dons",             # hors allowlist
    "https://zevent.fr.evil.test/dons",   # suffixe trompeur
    "https://zevent.fr@evil.test/dons",   # l'hôte réel est evil.test
    "file:///etc/passwd",
    "javascript:alert(1)",
])
def test_une_url_douteuse_n_ouvre_rien(navigateur, url):
    """La vue de don n'a pas de barre d'adresse : un détournement y serait
    indétectable pour l'utilisateur."""
    assert fullscreen.ouvrir_page_de_don(url) is False
    assert navigateur == []


def test_un_sous_domaine_de_zevent_reste_accepte(navigateur):
    assert fullscreen.ouvrir_page_de_don("https://www.zevent.fr/dons") is True


def test_le_premier_plan_est_cede_avant_d_ouvrir(monkeypatch):
    """Windows refuse le premier plan au navigateur si ZLink ne le cède pas.

    Sans cet appel, la page de don s'ouvrait derrière le plein écran.
    """
    ordre: list[str] = []
    monkeypatch.setattr(fullscreen, "ceder_premier_plan",
                        lambda: ordre.append("cession"))
    monkeypatch.setattr(fullscreen.QDesktopServices, "openUrl",
                        lambda url: ordre.append("ouverture"))
    fullscreen.ouvrir_page_de_don("https://zevent.fr/dons")
    assert ordre == ["cession", "ouverture"]


# ── préférences de lecture ───────────────────────────────────────────────────

@pytest.fixture
def config(tmp_path, monkeypatch):
    cible = tmp_path / "config.json"
    monkeypatch.setattr(fullscreen, "CONFIG_PATH", cible)
    return cible


def test_preference_absente_rend_le_defaut(config):
    assert fullscreen._load_setting("volume", 80) == 80


def test_preference_aller_retour(config):
    fullscreen._save_settings({"volume": 42})
    assert fullscreen._load_setting("volume", 80) == 42


def test_enregistrer_une_preference_n_efface_pas_le_reste(config):
    """config.json est partagé avec les favoris, les rappels et les réglages."""
    config.write_text(json.dumps({"favorite_logins": ["zerator"]}),
                      encoding="utf-8")
    fullscreen._save_settings({"volume": 42})
    reste = json.loads(config.read_text(encoding="utf-8"))
    assert reste["favorite_logins"] == ["zerator"]
    assert reste["volume"] == 42


def test_config_corrompue_rend_le_defaut(config):
    config.write_text("{pas du json", encoding="utf-8")
    assert fullscreen._load_setting("volume", 80) == 80


# ── vignette ronde ───────────────────────────────────────────────────────────

def test_la_vignette_ronde_a_la_taille_demandee(qapp):
    source = QPixmap(200, 120)
    source.fill(QColor("#00ff87"))
    ronde = fullscreen._circle_pixmap(source, 48)
    assert ronde.width() == 48 and ronde.height() == 48


def test_vignette_ronde_sur_source_vide_ne_leve_pas(qapp):
    fullscreen._circle_pixmap(QPixmap(), 48)


# ── qualité du replay ────────────────────────────────────────────────────────

class _FauxLecteur:
    """Lecteur plein écran factice : on n'observe que l'appel à save_clip."""

    def __init__(self, rendu="/tmp/best.ts"):
        self.rendu = rendu
        self.appels: list[tuple[int, str]] = []

    def save_clip(self, secs, directory):
        self.appels.append((secs, directory))
        return self.rendu


class _FenetreFactice:
    """Porte les seuls attributs que _meilleur_tampon consulte.

    Pas une vraie FullscreenWindow, même créée par __new__ : `current_login`
    y est une propriété en LECTURE SEULE, impossible à poser sur l'instance.
    Les deux méthodes sont empruntées telles quelles, donc c'est bien le code
    de production qui est exercé.
    """

    _meilleur_tampon = fullscreen.FullscreenWindow._meilleur_tampon
    # staticmethod() explicite : recuperee via la classe, la fonction nue
    # serait re-attachee comme methode d'instance et recevrait self.
    _replay_secs_demandes = staticmethod(
        fullscreen.FullscreenWindow._replay_secs_demandes)

    def __init__(self, login, lecteur, duree=60):
        self.current_login = login
        self._mpv = lecteur
        self._clip_config = {"duration_secs": duree}


def _fenetre_factice(login, lecteur, duree=60):
    return _FenetreFactice(login, lecteur, duree)


def test_le_replay_de_la_chaine_affichee_vient_du_plein_ecran():
    """La grille joue en 360p, le plein écran en `best`.

    Pour la chaîne affichée en grand, son tampon est disponible et bien
    meilleur : c'est lui qu'on veut, pas le 360p de la cellule.
    """
    lecteur = _FauxLecteur("/tmp/best.ts")
    f = _fenetre_factice("zerator", lecteur)
    assert f._meilleur_tampon("zerator", "/tmp/grille_360p.ts") == "/tmp/best.ts"
    assert lecteur.appels == [(REPLAY_SECS, tempfile.gettempdir())]


def test_le_replay_d_une_autre_chaine_garde_le_fichier_de_la_grille():
    """Le plein écran ne contient pas cette chaîne : son tampon ne sert à rien."""
    lecteur = _FauxLecteur()
    f = _fenetre_factice("domingo", lecteur)
    assert f._meilleur_tampon("zerator", "/tmp/grille.ts") == "/tmp/grille.ts"
    assert lecteur.appels == [], "aucun dump inutile"


def test_repli_sur_la_grille_si_le_tampon_du_plein_ecran_est_vide():
    """Tampon trop court, lecture qui vient de démarrer : la grille reste utile."""
    f = _fenetre_factice("zerator", _FauxLecteur(rendu=None))
    assert f._meilleur_tampon("zerator", "/tmp/grille.ts") == "/tmp/grille.ts"


def test_repli_sur_la_grille_si_le_dump_leve():
    class _Casse:
        def save_clip(self, secs, directory):
            raise OSError("disque plein")

    f = _fenetre_factice("zerator", _Casse())
    assert f._meilleur_tampon("zerator", "/tmp/grille.ts") == "/tmp/grille.ts"


def test_sans_lecteur_plein_ecran_on_garde_la_grille():
    f = _fenetre_factice("zerator", None)
    assert f._meilleur_tampon("zerator", "/tmp/grille.ts") == "/tmp/grille.ts"


def test_la_duree_du_replay_ne_suit_PAS_celle_des_clips():
    """Un clip vient du tampon local et peut être long ; un replay est plafonné
    par la fenêtre que Twitch garde en ligne — mesurée à 28 s.

    Les faire dépendre l'un de l'autre promettait une minute de replay que la
    source ne peut pas fournir.
    """
    lecteur = _FauxLecteur()
    f = _fenetre_factice("zerator", lecteur, duree=600)   # clips très longs
    f._meilleur_tampon("zerator", "/tmp/g.ts")
    assert lecteur.appels[0][0] == REPLAY_SECS


def test_la_duree_de_replay_tient_dans_la_fenetre_de_twitch():
    """Garde-fou : annoncer plus que ce que la source garde serait mentir."""
    assert REPLAY_SECS <= 30, "mesuré : 14 segments de 2 s, soit 28 s"


# ── Barre de progression du replay ───────────────────────────────────────────
#
# La progression est FOURNIE par l'appelant, jamais déduite de la durée du
# fichier : un MP4 fragmenté repris chez Twitch porte l'horodatage du direct,
# soit des heures, et le rapport serait à 99 % dès la première image.

def test_la_barre_borne_le_rapport(qtbot):
    barre = fullscreen._ReplayProgress(None)
    qtbot.addWidget(barre)
    barre.set_ratio(-3.0)
    assert barre._ratio == 0.0
    barre.set_ratio(42.0)
    assert barre._ratio == 1.0


def test_la_barre_ignore_les_variations_invisibles(qtbot):
    """Trois pixels de large : repeindre pour un millième ne montre rien."""
    barre = fullscreen._ReplayProgress(None)
    qtbot.addWidget(barre)
    barre.set_ratio(0.5)
    barre.set_ratio(0.5001)
    assert barre._ratio == 0.5


def test_la_barre_reste_fine(qtbot):
    """Un repère, pas un lecteur : elle ne doit rien voler à l'image."""
    barre = fullscreen._ReplayProgress(None)
    qtbot.addWidget(barre)
    assert barre.height() == fullscreen._ReplayProgress.HAUTEUR <= 4


def test_le_badge_de_replay_propose_de_sortir(qtbot):
    """Échap ferme aussi, mais rien ne le disait — et une sortie qu'on ne
    devine pas revient à ne pas en avoir."""
    badge = fullscreen._ReplayBadge("zerator", 30, None)
    qtbot.addWidget(badge)
    textes = [lbl.text() for lbl in badge.findChildren(fullscreen.QLabel)]
    assert any("Échap" in t for t in textes)


# ── Bandeau d'attente ────────────────────────────────────────────────────────
#
# La reprise en pleine qualité demande quelques secondes de réseau. Sans
# bandeau, le clic sur « Revoir les dernières secondes » ne produisait rien de
# visible et on le refaisait en croyant l'avoir manqué.

def test_le_bandeau_dit_ce_qu_on_attend(qtbot):
    chargeur = fullscreen._ReplayLoader("zerator", 30, None)
    qtbot.addWidget(chargeur)
    textes = " ".join(lbl.text()
                      for lbl in chargeur.findChildren(fullscreen.QLabel))
    assert "30" in textes and "zerator" in textes


def test_le_bandeau_propose_de_renoncer(qtbot):
    """Une attente qu'on ne peut pas interrompre est une attente qu'on subit."""
    chargeur = fullscreen._ReplayLoader("zerator", 30, None)
    qtbot.addWidget(chargeur)
    recu: list[int] = []
    chargeur.annulation_demandee.connect(lambda: recu.append(1))
    textes = " ".join(lbl.text()
                      for lbl in chargeur.findChildren(fullscreen.QLabel))
    assert "Échap" in textes
    chargeur.mousePressEvent(_ClicFactice())
    assert recu == [1]


class _ClicFactice:
    """Le minimum qu'attend mousePressEvent : de quoi accepter l'événement."""

    def accept(self) -> None:
        pass


def test_l_anneau_tourne(qtbot):
    """Un texte figé est indiscernable d'une application bloquée."""
    anneau = fullscreen._PetitAnneau(16)
    qtbot.addWidget(anneau)
    depart = anneau._angle
    anneau._tourner()
    assert anneau._angle != depart
    assert 0 <= anneau._angle < 360


def test_l_anneau_boucle_sans_deborder(qtbot):
    anneau = fullscreen._PetitAnneau(16)
    qtbot.addWidget(anneau)
    for _ in range(400):
        anneau._tourner()
    assert 0 <= anneau._angle < 360


# ── Cycle de vie du bandeau d'attente ────────────────────────────────────────
#
# Même procédé que _FenetreFactice : les méthodes sont EMPRUNTÉES à la classe
# de production, donc c'est bien son code qui est exercé. Construire une vraie
# FullscreenWindow réclamerait libmpv et QtWebEngine.

class _ChatFactice:
    _visible = False
    _width = 0


class _FenetreDeReplay:
    """Le strict nécessaire au cycle de vie du bandeau d'attente."""

    _montrer_chargeur = fullscreen.FullscreenWindow._montrer_chargeur
    _placer_chargeur = fullscreen.FullscreenWindow._placer_chargeur
    _cacher_chargeur = fullscreen.FullscreenWindow._cacher_chargeur
    _annuler_replay = fullscreen.FullscreenWindow._annuler_replay
    _sur_replay_hd = fullscreen.FullscreenWindow._sur_replay_hd

    def __init__(self, parent):
        self._parent = parent
        self._chat_panel = _ChatFactice()
        self._replay_loader = None
        self._replay_active = False
        self._replay_annule = False
        self._replay_wait = None
        self._replay_path = ""
        self._replay_secs = 30
        self.nettoyages = 0
        self.engages: list[str] = []

    def centralWidget(self):
        return self._parent

    def width(self):
        return 1920

    def _cleanup_replay(self):
        self.nettoyages += 1

    def _engager_replay(self, chemin):
        self.engages.append(chemin)


@pytest.fixture
def fenetre_replay(qtbot):
    from PyQt6.QtWidgets import QWidget
    parent = QWidget()
    parent.resize(1920, 1080)
    qtbot.addWidget(parent)
    f = _FenetreDeReplay(parent)
    f._parent_du_test = parent      # garde le parent en vie
    return f


def test_le_bandeau_apparait_a_la_demande(fenetre_replay):
    fenetre_replay._montrer_chargeur("zerator", 30)
    assert fenetre_replay._replay_loader is not None


def test_une_seconde_demande_ne_empile_pas_les_bandeaux(fenetre_replay):
    fenetre_replay._montrer_chargeur("zerator", 30)
    premier = fenetre_replay._replay_loader
    fenetre_replay._montrer_chargeur("domingo", 30)
    assert fenetre_replay._replay_loader is not premier


def test_le_bandeau_est_centre_sur_la_zone_video(fenetre_replay):
    fenetre_replay._montrer_chargeur("zerator", 30)
    chargeur = fenetre_replay._replay_loader
    attendu = (1920 - chargeur.width()) // 2
    assert chargeur.pos().x() == attendu
    assert chargeur.pos().y() == 24, "à la place qu'occupera le badge REPLAY"


def test_le_chat_ouvert_recentre_le_bandeau(fenetre_replay):
    """Le chat ne rejoue pas : le bandeau appartient à la zone vidéo."""
    fenetre_replay._montrer_chargeur("zerator", 30)
    fenetre_replay._chat_panel._visible = True
    fenetre_replay._chat_panel._width = 400
    fenetre_replay._placer_chargeur()
    chargeur = fenetre_replay._replay_loader
    assert chargeur.pos().x() == (1520 - chargeur.width()) // 2


def test_renoncer_retire_le_bandeau_et_le_fichier(fenetre_replay):
    fenetre_replay._montrer_chargeur("zerator", 30)
    fenetre_replay._annuler_replay()
    assert fenetre_replay._replay_loader is None
    assert fenetre_replay._replay_annule is True
    assert fenetre_replay.nettoyages == 1


def test_on_ne_renonce_pas_a_un_replay_deja_lance(fenetre_replay):
    """Échap ferme alors le replay, ce n'est plus la même action."""
    fenetre_replay._replay_active = True
    fenetre_replay._annuler_replay()
    assert fenetre_replay._replay_annule is False


def test_un_verdict_arrive_apres_l_abandon_ne_joue_rien(fenetre_replay):
    """Le fil de téléchargement va jusqu'au bout : il rend dans le vide.

    L'interrompre en cours laisserait un fichier tronqué ; on le laisse finir
    et on supprime son résultat comme n'importe quel temporaire.
    """
    fenetre_replay._montrer_chargeur("zerator", 30)
    fenetre_replay._annuler_replay()
    fenetre_replay._sur_replay_hd("/tmp/hd.mp4", 28.0)
    assert fenetre_replay.engages == []
    assert fenetre_replay.nettoyages == 2, "le fichier obtenu est supprimé"


def test_un_verdict_sans_source_retire_le_bandeau(fenetre_replay):
    """Ni reprise ni repli : laisser l'anneau tourner serait pire que rien."""
    fenetre_replay._montrer_chargeur("zerator", 30)
    fenetre_replay._sur_replay_hd("", 0.0)
    assert fenetre_replay._replay_loader is None
    assert fenetre_replay.engages == []


def test_un_verdict_valide_lance_la_lecture(fenetre_replay):
    fenetre_replay._montrer_chargeur("zerator", 30)
    fenetre_replay._sur_replay_hd("/tmp/hd.mp4", 28.0)
    assert fenetre_replay.engages == ["/tmp/hd.mp4"]
    assert fenetre_replay._replay_secs == 28


# ── Les trois dispositions de la fenêtre ─────────────────────────────────────
#
# Replay, page de don et direct seul s'excluent. Elles vivaient dans une seule
# fonction dont on ne lisait plus la branche qui nous intéressait ; le découpage
# ne doit rien avoir changé au résultat.

class _Faux:
    """Note ce qu'on lui demande, sans rien dessiner."""

    def __init__(self, visible=False, width=0):
        self.geometrie = None
        self.position = None
        self.remontes = 0
        self.abaisse = 0
        self._visible = visible
        self._width = width
        self.cache = 0

    def setGeometry(self, x, y, w, h):
        self.geometrie = (x, y, w, h)

    def geometry(self):
        return self.geometrie

    def move(self, x, y):
        self.position = (x, y)

    def raise_(self):
        self.remontes += 1

    def lower(self):
        self.abaisse += 1

    def hide(self):
        self.cache += 1

    def isVisible(self):
        return self._visible

    def width(self):
        return self._width

    def x(self):
        return 0

    def reposition(self, *_a):
        pass

    def _update_geometry(self):
        pass

    def sizeHint(self):
        return self

    def setToolTip(self, _t):
        pass


class _FenetreGeometrie:
    """Les trois méthodes de disposition, empruntées à la classe réelle."""

    _geometrie_replay = fullscreen.FullscreenWindow._geometrie_replay
    _geometrie_donation = fullscreen.FullscreenWindow._geometrie_donation
    _geometrie_direct = fullscreen.FullscreenWindow._geometrie_direct

    def __init__(self):
        self._stack = _Faux()
        self._overlay = _Faux()
        self._remote_btn = _Faux()
        self._remote_menu = _Faux()
        self._pinned_audio = _Faux()
        self._chat_panel = _Faux()
        self._donate_view = _Faux()
        self._donate_close_btn = _Faux(width=80)
        self._replay_player = _Faux()
        self._replay_badge = _Faux(width=300)
        self._replay_progress = _Faux()


@pytest.fixture
def geom():
    return _FenetreGeometrie()


def test_le_replay_occupe_la_zone_video(geom):
    geom._geometrie_replay(1920, 1080)
    assert geom._replay_player.geometrie == (0, 0, 1920, 1080)
    assert geom._replay_player.abaisse == 1, "le direct passe devant"


def test_le_direct_se_refugie_en_incrustation_pendant_le_replay(geom):
    """On veut voir l'action en grand ; le direct sert à ne pas perdre le fil."""
    geom._geometrie_replay(1920, 1080)
    x, y, w, h = geom._stack.geometrie
    assert (w, h) == (fullscreen._REPLAY_PIP_W, fullscreen._REPLAY_PIP_H)
    assert x + w < 1920 and y + h < 1080, "dans le coin, pas hors écran"
    assert geom._stack.remontes >= 1


def test_la_barre_de_progression_longe_le_bord_haut(geom):
    geom._geometrie_replay(1520, 1080)
    assert geom._replay_progress.geometrie == (
        0, 0, 1520, fullscreen._ReplayProgress.HAUTEUR), \
        "le chat, quand il est ouvert, n'a pas à porter la barre"


def test_le_badge_est_centre_sur_la_video(geom):
    geom._geometrie_replay(1920, 1080)
    assert geom._replay_badge.position == ((1920 - 300) // 2, 24)


def test_un_replay_sans_badge_ni_barre_ne_leve_pas(geom):
    """Les deux sont détruits à la fermeture, la géométrie peut passer après."""
    geom._replay_badge = None
    geom._replay_progress = None
    geom._replay_player = None
    geom._geometrie_replay(1920, 1080)


def test_l_overlay_disparait_pendant_le_replay(geom):
    geom._geometrie_replay(1920, 1080)
    assert geom._overlay.cache == 1


def test_la_page_de_don_prend_toute_la_fenetre(geom):
    """Y compris sous le chat : c'est une page web, pas une vidéo."""
    geom._geometrie_donation(1920, 1080, 1520)
    assert geom._donate_view.geometrie == (0, 0, 1920, 1080)


def test_le_direct_se_reduit_derriere_la_page_de_don(geom):
    geom._geometrie_donation(1920, 1080, 1920)
    x, y, w, h = geom._stack.geometrie
    assert (w, h) == (320, 180)
    assert geom._stack.remontes >= 1, "l'incrustation reste visible"


def test_le_direct_seul_occupe_la_zone_video(geom):
    geom._geometrie_direct(1920, 1080, 1520)
    assert geom._stack.geometrie == (0, 0, 1520, 1080)
    assert geom._overlay.geometrie == (0, 1080 - 56, 1520, 56)


def test_le_menu_telecommande_ferme_n_est_pas_redimensionne(geom):
    geom._geometrie_direct(1920, 1080, 1920)
    assert geom._remote_menu.geometrie is None


def test_le_menu_telecommande_ouvert_prend_toute_la_hauteur(geom):
    geom._remote_menu._visible = True
    geom._geometrie_direct(1920, 1080, 1920)
    assert geom._remote_menu.geometrie == (
        0, 0, fullscreen.REMOTE_MENU_WIDTH, 1080)


# ── run_action : la surface unique des gestes ────────────────────────────────
#
# La palette du panel et la télécommande Stream Deck passent toutes deux par
# là. Une clé inconnue doit être ignorée sans bruit : le panel en traite
# d'autres de son côté, et une télécommande peut être plus récente que
# l'application qu'elle pilote.

class _FenetreGestes:
    """Compte les gestes appelés, sans construire de fenêtre."""

    run_action = fullscreen.FullscreenWindow.run_action
    ACTIONS = fullscreen.FullscreenWindow.ACTIONS

    def __init__(self) -> None:
        self.appels: list[str] = []
        for nom in ("_save_clip", "_replay_current", "_toggle_chat",
                    "_open_donate_view", "_toggle_favorite_current",
                    "_toggle_mute"):
            setattr(self, nom, lambda n=nom: self.appels.append(n))


@pytest.mark.parametrize("cle,methode", [
    ("clip", "_save_clip"),
    ("replay", "_replay_current"),
    ("chat", "_toggle_chat"),
    ("don", "_open_donate_view"),
    ("favori", "_toggle_favorite_current"),
    ("muet", "_toggle_mute"),
])
def test_chaque_action_appelle_son_geste(cle, methode):
    f = _FenetreGestes()
    f.run_action(cle)
    assert f.appels == [methode]


@pytest.mark.parametrize("cle", ["recap", "", "inconnue", "CLIP"])
def test_une_action_inconnue_est_ignoree_sans_bruit(cle):
    f = _FenetreGestes()
    f.run_action(cle)
    assert f.appels == []


def test_la_liste_publiee_correspond_aux_gestes_reels():
    """`ACTIONS` sert de catalogue à la télécommande : si elle mentait, une
    touche du Stream Deck ne ferait rien sans que personne ne le sache."""
    f = _FenetreGestes()
    for cle in _FenetreGestes.ACTIONS:
        f.appels.clear()
        f.run_action(cle)
        assert f.appels, f"« {cle} » est annoncée mais ne fait rien"


# ── Replay : la durée annoncée doit être celle qu'on joue ────────────────────
#
# Le bandeau annonçait la durée DEMANDÉE. Or le tampon du direct peut être
# plus court, et une reprise chez Twitch est plafonnée par ce que la
# plateforme garde en ligne — vingt-huit secondes. On lisait donc « 60
# dernières secondes » sur vingt-huit secondes de vidéo, et la barre de
# progression s'arrêtait à mi-course.

class _LecteurDeReplay:
    def __init__(self, restant):
        self._restant = restant

    def restant(self):
        return self._restant


class _FauxPleinEcran:
    """Le strict nécessaire pour éprouver `_mesurer_duree` sans fenêtre."""

    def __init__(self, demandee=60, origine=0.0):
        self._replay_secs = demandee
        self._replay_origine = origine
        self._replay_badge = None

    mesurer = fullscreen.FullscreenWindow._mesurer_duree


def test_la_duree_reellement_obtenue_remplace_celle_demandee():
    ecran = _FauxPleinEcran(demandee=60)
    ecran.mesurer(_LecteurDeReplay(restant=28.0), 0.0)
    assert ecran._replay_secs == 28


def test_un_horodatage_absolu_n_est_pas_pris_pour_une_duree():
    """Un fragment Twitch démarre à l'heure du direct : des milliers de secondes."""
    ecran = _FauxPleinEcran(demandee=60, origine=42_000.0)
    ecran.mesurer(_LecteurDeReplay(restant=9_000.0), 42_000.0)
    assert ecran._replay_secs == 60, "la durée demandée devait être conservée"


def test_une_duree_nulle_est_ignoree():
    ecran = _FauxPleinEcran(demandee=60)
    ecran.mesurer(_LecteurDeReplay(restant=0.0), 0.0)
    assert ecran._replay_secs == 60


def test_sans_temps_restant_rien_ne_change():
    """mpv ne le donne pas tant que le fichier n'est pas ouvert."""
    ecran = _FauxPleinEcran(demandee=60)
    ecran.mesurer(_LecteurDeReplay(restant=None), 0.0)
    assert ecran._replay_secs == 60


def test_l_origine_est_retranchee_de_la_mesure():
    """Position et reste sont absolus ; seule leur distance à l'origine compte."""
    ecran = _FauxPleinEcran(demandee=60, origine=42_000.0)
    ecran.mesurer(_LecteurDeReplay(restant=10.0), 42_020.0)
    assert ecran._replay_secs == 30


def test_le_bandeau_corrige_ce_qu_il_annonce(qtbot):
    from PyQt6.QtWidgets import QWidget

    parent = QWidget()
    qtbot.addWidget(parent)
    badge = fullscreen._ReplayBadge("samueletienne", 60, parent)
    assert "60 dernières secondes" in badge._who.text()
    badge.set_secs(28)
    assert "28 dernières secondes" in badge._who.text()


# ── Clip : un geste sans retour visible est un geste raté ────────────────────

def test_l_annonce_laisse_passer_les_clics(qtbot):
    """Elle couvre la vidéo : avaler un clic trois secondes serait pire."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QWidget

    parent = QWidget()
    qtbot.addWidget(parent)
    annonce = fullscreen._Annonce("Clip sauvegardé · 60 s", "#00ff87", parent)
    assert annonce.testAttribute(
        Qt.WidgetAttribute.WA_TransparentForMouseEvents)


def test_l_annonce_porte_le_texte_demande(qtbot):
    from PyQt6.QtWidgets import QLabel, QWidget

    parent = QWidget()
    qtbot.addWidget(parent)
    annonce = fullscreen._Annonce("Clip sauvegardé · 60 s", "#00ff87", parent)
    textes = [w.text() for w in annonce.findChildren(QLabel)]
    assert textes == ["Clip sauvegardé · 60 s"]
