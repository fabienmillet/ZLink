# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Ce que le plein écran MONTRE, et ce qu'il fait des gestes qu'on lui adresse.

La fenêtre est construite pour de vrai — barre d'information, surcouches,
menu télécommande, palette — avec un seul remplacement : le lecteur vidéo.
`MpvWidget` ouvrirait libmpv et un flux réseau ; son double note ce qu'on lui
demande et rend la main. Tout le reste est le code de production.

C'est délibéré : les régressions de ce fichier ne sont pas venues d'un calcul
faux, mais d'un câblage — un bouton qui ne rappelle plus l'overlay, une
surcouche posée là où une autre l'attendait, une touche qui n'atteint plus
personne. Rien de tout cela ne se voit sur des doublures.

Périmètre : affichage et gestes. Le replay, les clips, le chat et la vue de
don ont leur propre fichier.
"""

from __future__ import annotations

import json

import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QEnterEvent, QFontMetrics, QKeyEvent, QMouseEvent
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

from windows import fullscreen


# ── doubles ──────────────────────────────────────────────────────────────────

class _LecteurFactice(QWidget):
    """Double de `MpvWidget` : note les ordres au lieu de décoder.

    Même surface publique que le vrai widget pour ce que le plein écran lui
    demande. Rien n'est joué, rien n'est ouvert, aucun sous-processus.
    """

    uses_render_backend = False

    def __init__(self, *_a, **_k) -> None:
        super().__init__()
        self.lu: list[str] = []
        self.volumes: list[int] = []
        self.coupures: list[bool] = []
        self.arrets: int = 0

    def play(self, url: str) -> None:
        self.lu.append(url)

    def stop(self) -> None:
        self.arrets += 1

    def set_volume(self, valeur: int) -> None:
        self.volumes.append(int(valeur))

    def set_mute(self, muet: bool) -> None:
        self.coupures.append(bool(muet))

    def save_clip(self, _secs: int, _dossier: str) -> str:
        return ""


class _Chaine:
    """Le strict nécessaire d'un `StreamerInfo` pour le menu télécommande."""

    def __init__(self, login: str, online: bool = True, viewers: int = 1200) -> None:
        self.twitch_login = login
        self.display = login.title()
        self.online = online
        self.viewers = viewers
        self.game = "Minecraft"
        self.profile_url = ""
        self.title = "Un titre de direct"


def _survol(widget: QWidget) -> None:
    """Fait entrer le curseur dans un widget, comme Qt le ferait."""
    widget.enterEvent(QEnterEvent(QPointF(1, 1), QPointF(1, 1), QPointF(1, 1)))


def _sortie(widget: QWidget) -> None:
    widget.leaveEvent(QEvent(QEvent.Type.Leave))


def _frappe(fenetre, touche: Qt.Key,
            modificateurs: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier) -> None:
    """Envoie une touche à la fenêtre, sans passer par le focus clavier."""
    fenetre.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, touche, modificateurs))


def _clic(fenetre, x: int, y: int) -> None:
    fenetre.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, y), QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _textes(widget: QWidget) -> str:
    """Tout le texte affiché sous un widget, concaténé."""
    return " ".join(lbl.text() for lbl in widget.findChildren(QLabel))


def _fenetres_visibles() -> list[QWidget]:
    return [w for w in QApplication.instance().topLevelWidgets() if w.isVisible()]


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def fabrique(qtbot, monkeypatch, tmp_path):
    """Construit de vraies fenêtres plein écran, sans lecteur ni écran dédié.

    Les préférences sont redirigées vers un fichier neuf : sans cela le volume
    retenu d'un test se retrouverait dans le suivant.
    """
    monkeypatch.setattr(fullscreen, "MpvWidget", _LecteurFactice)
    reglages = tmp_path / "config.json"
    monkeypatch.setattr(fullscreen, "CONFIG_PATH", reglages)
    ouvertes: list = []

    def _construire(afficher: bool = True, **preferences):
        if preferences:
            reglages.write_text(json.dumps(preferences), encoding="utf-8")
        f = fullscreen.FullscreenWindow(
            QApplication.instance().primaryScreen(), show_on_init=False)
        qtbot.addWidget(f)
        ouvertes.append(f)
        f.resize(1920, 1080)
        if afficher:
            f.show()
            QApplication.processEvents()
        return f

    _construire.reglages = reglages
    yield _construire

    # Refermée explicitement : une fenêtre de PREMIER NIVEAU laissée visible
    # est comptée comme fenêtre parasite par les tests qui suivent, et la
    # destruction différée de qtbot n'a pas encore eu lieu quand ils regardent.
    for f in ouvertes:
        f.hide()
        f.close()
    QApplication.processEvents()


@pytest.fixture
def fenetre(fabrique):
    """Fenêtre affichée, aucun direct choisi."""
    return fabrique()


@pytest.fixture
def direct(fabrique):
    """Fenêtre affichée qui regarde « zerator »."""
    f = fabrique()
    f.set_stream("zerator", "Minecraft", 4200, "https://zevent.fr/dons")
    QApplication.processEvents()
    return f


@pytest.fixture
def favoris_vierges(monkeypatch, tmp_path):
    """Favoris isolés : le module les garde en mémoire entre deux appels."""
    from core import favorites
    monkeypatch.setattr(favorites, "CONFIG_PATH", tmp_path / "favoris.json")
    monkeypatch.setattr(favorites, "_cache", set())
    return favorites


# ─────────────────────────────────────────────────────────────────────────────
# La barre d'information du bas
# ─────────────────────────────────────────────────────────────────────────────
#
# Trois données tenues par une seule ligne de texte riche, dont deux viennent
# d'APIs tierces. C'est le seul endroit de la fenêtre où du HTML est fabriqué
# à la main.

def test_la_barre_annonce_la_chaine_et_son_jeu(direct):
    assert "zerator" in direct._ov_info.text()
    assert "Minecraft" in direct._ov_info.text()


def test_un_jeu_inconnu_ne_laisse_pas_de_separateur_orphelin(fenetre):
    """L'API rend un jeu vide hors direct : « zerator · » ne veut rien dire."""
    fenetre.set_stream("zerator", "")
    assert "·" not in fenetre._ov_info.text()


def test_le_nom_de_chaine_ne_peut_pas_injecter_de_balise(fenetre):
    """La barre est du texte RICHE : un pseudo venu de l'API y serait du code.

    Les logins Twitch sont contraints, mais rien dans ZLink ne le vérifie —
    et la même ligne sert aux noms d'affichage, qui ne le sont pas.
    """
    fenetre.set_stream("<img src=x onerror=alert(1)>", "")
    assert "<img" not in fenetre._ov_info.text()
    assert "&lt;img" in fenetre._ov_info.text()


def test_le_nom_de_jeu_ne_peut_pas_injecter_de_balise(fenetre):
    fenetre.set_stream("zerator", "<script>alert(1)</script>")
    assert "<script>" not in fenetre._ov_info.text()


def test_la_duree_du_direct_complete_la_ligne(fenetre, monkeypatch):
    from core import live_uptime
    monkeypatch.setattr(live_uptime, "texte", lambda _l: "depuis 4 h 12 min")
    fenetre.set_stream("zerator", "Minecraft")
    assert "depuis 4 h 12 min" in fenetre._ov_info.text()


def test_une_duree_inconnue_n_ajoute_rien(fenetre, monkeypatch):
    """L'interface de Twitch qui la fournit n'est pas documentée : son absence
    ne doit se voir qu'à une ligne plus courte, jamais à un « depuis  »."""
    from core import live_uptime
    monkeypatch.setattr(live_uptime, "texte", lambda _l: "")
    fenetre.set_stream("zerator", "Minecraft")
    assert "depuis" not in fenetre._ov_info.text()


def test_une_duree_relevee_apres_coup_est_repeinte(fenetre, monkeypatch):
    """Les durées arrivent par lots, plusieurs secondes après l'ouverture du
    flux : sans ce rafraîchissement la ligne resterait sans durée jusqu'au
    prochain changement de chaîne."""
    from core import live_uptime
    monkeypatch.setattr(live_uptime, "texte", lambda _l: "")
    fenetre.set_stream("zerator", "Minecraft")
    monkeypatch.setattr(live_uptime, "texte", lambda _l: "depuis 2 h 00 min")
    fenetre.rafraichir_duree()
    assert "depuis 2 h 00 min" in fenetre._ov_info.text()


def test_une_duree_relevee_pour_une_autre_chaine_ne_repeint_rien(fenetre, monkeypatch):
    """Le relevé est asynchrone : il peut arriver après un changement de flux,
    et repeindrait alors la ligne avec le nom de la chaîne précédente."""
    from core import live_uptime
    monkeypatch.setattr(live_uptime, "texte", lambda _l: "")
    fenetre.set_stream("zerator", "Minecraft")
    fenetre._info_courante = ("domingo", "Jeu")
    monkeypatch.setattr(live_uptime, "texte", lambda _l: "depuis 9 h 00 min")
    fenetre.rafraichir_duree()
    assert "domingo" not in fenetre._ov_info.text()
    assert "zerator" in fenetre._ov_info.text()


def test_l_audience_est_abregee_au_centre_de_la_barre(direct):
    assert direct._ov_viewers.text() == "4.2k viewers"


def test_une_audience_nulle_n_affiche_rien(fenetre):
    """« 0 viewers » sur un direct qui démarre est faux plus souvent que juste :
    l'API met une minute à publier le compte."""
    fenetre.set_stream("zerator", "Minecraft", 0)
    assert fenetre._ov_viewers.text() == ""


def test_l_audience_se_met_a_jour_sans_toucher_au_reste(direct):
    """Le compte est rafraîchi toutes les 30 s ; repeindre la ligne entière
    referait le travail de `_peindre_info` pour rien."""
    avant = direct._ov_info.text()
    direct.update_viewers(1_500_000)
    assert direct._ov_viewers.text() == "1.5M viewers"
    assert direct._ov_info.text() == avant


def test_le_bouton_de_don_n_apparait_qu_avec_une_adresse(fabrique):
    """Un bouton qui n'ouvre rien est pire que pas de bouton."""
    f = fabrique()
    f.set_stream("zerator", "Minecraft", 10, "")
    assert f._donate_btn.isHidden()
    f.set_stream("zerator", "Minecraft", 10, "https://zevent.fr/dons")
    assert not f._donate_btn.isHidden()


def test_couper_le_flux_range_la_barre_et_le_bouton(direct):
    direct.clear_stream()
    assert direct._overlay.isHidden()
    assert direct._remote_btn.isHidden()
    assert direct._donate_btn.isHidden()
    assert "Sélectionnez" in direct._hint_lbl.text()


def test_couper_le_flux_arrete_le_lecteur(direct):
    direct.clear_stream()
    assert direct._mpv.arrets == 1
    assert direct.current_login == ""


# ─────────────────────────────────────────────────────────────────────────────
# Apparition et effacement de l'overlay
# ─────────────────────────────────────────────────────────────────────────────

def test_choisir_un_direct_montre_la_barre(direct):
    assert not direct._overlay.isHidden()
    assert not direct._remote_btn.isHidden()


def test_aucune_barre_tant_qu_aucun_direct_n_est_choisi(fenetre):
    """L'écran d'accueil porte déjà son propre texte ; une barre vide par
    dessus n'apprendrait rien."""
    fenetre._show_overlay()
    assert fenetre._overlay.isHidden()


def test_bouger_la_souris_rappelle_la_barre(direct):
    direct._hide_overlay()
    direct.mouseMoveEvent(QMouseEvent(
        QEvent.Type.MouseMove, QPointF(10, 10), QPointF(10, 10),
        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))
    assert not direct._overlay.isHidden()


def test_la_barre_ne_revient_pas_pendant_une_publicite(direct):
    """Le bandeau de pub occupe exactement le même bas d'écran."""
    direct._ad_active = True
    direct._hide_overlay()
    direct._show_overlay()
    assert direct._overlay.isHidden()


def test_la_barre_ne_revient_pas_derriere_la_page_de_don(direct):
    direct._pip_active = True
    direct._hide_overlay()
    direct._show_overlay()
    assert direct._overlay.isHidden()


def test_chaque_apparition_relance_le_compte_a_rebours(direct):
    """Sans cette relance la barre s'effaçait au milieu d'un réglage de volume."""
    direct._hide_timer.stop()
    direct._show_overlay()
    assert direct._hide_timer.isActive()


def test_le_fondu_ne_masque_qu_une_fois_eteint(direct):
    """`finished` arrive aussi quand l'animation est INTERROMPUE par un
    mouvement de souris : masquer sans regarder l'opacité faisait clignoter
    la barre à chaque va-et-vient du curseur."""
    direct._opacity_fx.setOpacity(0.5)
    direct._on_fade_finished()
    assert not direct._overlay.isHidden()
    direct._opacity_fx.setOpacity(0.0)
    direct._on_fade_finished()
    assert direct._overlay.isHidden()


def test_effacer_une_barre_deja_absente_ne_fait_rien(direct):
    direct._hide_overlay()
    direct._start_fade_out()
    assert direct._overlay.isHidden()


def test_la_barre_ne_revient_pas_pendant_un_replay(direct):
    """La page de don et la publicité sont gardées, le replay ne l'est pas.

    `_geometrie_replay` masque la barre — c'est même ce que vérifie
    `test_l_overlay_disparait_pendant_le_replay`. Mais le moindre mouvement
    de souris la rappelle, et elle se rouvre sur les informations du direct,
    qui n'est plus qu'une incrustation dans un coin.
    """
    direct._replay_active = True
    direct._update_mpv_geometry()
    direct._show_overlay()
    assert direct._overlay.isHidden()


# ─────────────────────────────────────────────────────────────────────────────
# Bandeau passager d'un geste abouti
# ─────────────────────────────────────────────────────────────────────────────

def test_une_annonce_apparait_sur_la_video(direct):
    direct.annoncer("Ajouté aux favoris", "#f5c518")
    assert direct._annonce is not None
    assert "Ajouté aux favoris" in _textes(direct._annonce)


def test_une_annonce_est_centree_sur_la_zone_video(direct):
    direct.annoncer("Clip sauvegardé")
    annonce = direct._annonce
    assert annonce.pos().x() == (1920 - annonce.width()) // 2
    assert annonce.pos().y() == 24


def test_le_chat_ouvert_recentre_l_annonce(direct):
    """Le bandeau appartient à la vidéo : centré sur la fenêtre entière, il
    glissait sous le panneau de chat."""
    direct.annoncer("Clip sauvegardé")
    direct._chat_panel._visible = True
    direct._chat_panel._width = 400
    direct._placer_annonce()
    annonce = direct._annonce
    assert annonce.pos().x() == (1520 - annonce.width()) // 2


def test_l_annonce_descend_sous_le_badge_de_replay(direct):
    """Les deux visent le même haut d'écran ; superposées, aucune ne se lit."""
    direct.annoncer("Clip sauvegardé")
    haut_seul = direct._annonce.pos().y()
    direct._replay_active = True
    direct._placer_annonce()
    assert direct._annonce.pos().y() > haut_seul


def test_deux_annonces_coup_sur_coup_ne_s_empilent_pas(direct):
    """Deux clips à la suite : la seconde remplace la première, sinon elles se
    recouvrent et le minuteur de la première efface la seconde."""
    direct.annoncer("Premier")
    premiere = direct._annonce
    direct.annoncer("Second")
    assert direct._annonce is not premiere
    assert "Second" in _textes(direct._annonce)


def test_le_minuteur_de_la_premiere_annonce_est_arrete(direct):
    """Sinon il retirerait la SECONDE annonce au bout du délai de la première."""
    direct.annoncer("Premier", secondes=0.1)
    premier_minuteur = direct._annonce_timer
    direct.annoncer("Second", secondes=5)
    assert not premier_minuteur.isActive()


def test_retirer_l_annonce_ne_laisse_pas_de_fenetre_sur_le_bureau(direct):
    """`_retirer_annonce` détache le bandeau : détaché ET visible, ce serait
    une fenêtre nue sur le bureau — le défaut que `tests/
    test_pas_de_fenetres_parasites.py` interdit."""
    avant = _fenetres_visibles()
    direct.annoncer("Clip sauvegardé")
    direct._retirer_annonce()
    QApplication.processEvents()
    assert _fenetres_visibles() == avant
    assert direct._annonce is None


def test_retirer_une_annonce_absente_ne_leve_pas(direct):
    """Appelé par le minuteur ET par l'annonce suivante : la course arrive."""
    direct._retirer_annonce()
    direct._retirer_annonce()


# ─────────────────────────────────────────────────────────────────────────────
# Toast « quelqu'un vient de… » — proposer sans imposer
# ─────────────────────────────────────────────────────────────────────────────

def _toasts(fenetre) -> list:
    return fenetre.centralWidget().findChildren(fullscreen._FavoriteLiveToast)


def test_un_favori_qui_passe_en_direct_est_annonce(direct):
    direct.show_favorite_live("domingo", "Domingo")
    assert len(_toasts(direct)) == 1
    assert "Domingo" in _textes(_toasts(direct)[0])


def test_on_n_annonce_pas_le_direct_qu_on_regarde_deja(direct):
    """L'information serait redondante, et le toast masque le haut de l'image."""
    direct.show_favorite_live("zerator", "ZeratoR")
    assert _toasts(direct) == []


def test_un_favori_sans_login_n_annonce_rien(direct):
    direct.show_favorite_live("", "")
    assert _toasts(direct) == []


def test_le_toast_propose_de_basculer_sans_le_faire(direct, qtbot):
    """Une bascule automatique arracherait l'utilisateur à ce qu'il regarde ;
    c'est le bouton qui décide, et lui seul."""
    direct.show_favorite_live("domingo", "Domingo")
    with qtbot.waitSignal(direct.stream_change_requested, timeout=1000) as attente:
        _toasts(direct)[0]._on_watch()
    assert attente.args == ["domingo"]


def test_le_toast_dit_combien_de_temps_il_reste(direct):
    """La barre qui se vide remplace une disparition sans préavis."""
    direct.show_favorite_live("domingo", "Domingo")
    toast = _toasts(direct)[0]
    plein = toast._bar.value()
    toast._tick()
    assert toast._bar.value() < plein


def test_le_decompte_s_arrete_sous_le_curseur(direct):
    """Viser un bouton qui s'échappe est le meilleur moyen de le rater."""
    direct.show_favorite_live("domingo", "Domingo")
    toast = _toasts(direct)[0]
    _survol(toast)
    assert not toast._timer.isActive()
    _sortie(toast)
    assert toast._timer.isActive()


def test_le_decompte_ne_repart_pas_une_fois_epuise(direct):
    """Le toast est déjà en train de disparaître : le relancer le figerait."""
    direct.show_favorite_live("domingo", "Domingo")
    toast = _toasts(direct)[0]
    toast._left = 0
    _survol(toast)
    _sortie(toast)
    assert not toast._timer.isActive()


def test_le_toast_se_range_sous_la_liste_des_audios_epingles(direct):
    """Les deux occupent le coin haut-droit ; superposés, le toast masque la
    liste qui dit ce qu'on entend."""
    direct.set_pinned_audio(["kamet0", "mistermv"])
    QApplication.processEvents()
    direct.show_favorite_live("domingo", "Domingo")
    toast = _toasts(direct)[0]
    assert toast.pos().y() >= 16 + direct._pinned_audio.height_hint()


def test_un_show_du_programme_qui_demarre_est_annonce(direct):
    direct.show_show_started("mistermv", "Le Zbeul")
    toast = _toasts(direct)[0]
    assert "commence maintenant" in _textes(toast)
    assert "Le Zbeul" in _textes(toast)


def test_on_n_annonce_pas_le_show_de_la_chaine_affichee(direct):
    direct.show_show_started("zerator", "Lancement ZEVENT")
    assert _toasts(direct) == []


def test_un_objectif_imminent_dit_ce_qui_manque(direct):
    """Sans le montant restant, l'alerte ne dit pas si l'objectif est à portée."""
    direct.show_goal_imminent("aypierre", "Aypierre", "Rasage de crâne", 1200.0)
    # Le message se replie sur plusieurs lignes : c'est la phrase qui compte,
    # pas l'endroit où le repli tombe.
    texte = _textes(_toasts(direct)[0]).replace("\n", " ")
    assert "Rasage de crâne" in texte
    assert "1" in texte and "200" in texte


def test_un_objectif_imminent_propose_de_le_faire_tomber(direct):
    """Regarder ne fait pas tomber l'objectif : sans ce bouton, l'alerte ne
    mène nulle part."""
    direct.show_goal_imminent("aypierre", "Aypierre", "Un objectif", 500.0,
                              "https://zevent.fr/dons")
    boutons = [b.text() for b in _toasts(direct)[0].findChildren(QPushButton)]
    assert "Donner" in boutons


def test_sans_adresse_de_don_le_toast_ne_promet_rien(direct):
    direct.show_goal_imminent("aypierre", "Aypierre", "Un objectif", 500.0, "")
    boutons = [b.text() for b in _toasts(direct)[0].findChildren(QPushButton)]
    assert "Donner" not in boutons


def test_le_bouton_donner_elargit_le_toast_sans_le_faire_deborder(direct):
    """Le toast est calé sur le bord droit : élargi sans être redéplacé, il
    sortait de l'écran."""
    direct.show_favorite_live("domingo", "Domingo")
    toast = _toasts(direct)[0]
    toast.add_donate_button("https://zevent.fr/dons")
    assert toast.pos().x() + toast.width() <= 1920


def test_une_entree_dans_le_top_donne_le_rang_et_l_audience(direct):
    direct.show_top_entry("mistermv", "MisterMV", 42000, 3)
    texte = _textes(_toasts(direct)[0])
    assert "n°3" in texte
    assert "42" in texte


# ─────────────────────────────────────────────────────────────────────────────
# Ce que le toast a la place de dire
# ─────────────────────────────────────────────────────────────────────────────
#
# Relevé à l'écran : « plus que 22 622 € — « Je repein ». Le message vivait
# dans la colonne du nom, entre l'avatar et les boutons — 159 px des 394 du
# toast — et Qt le coupait au pixel, sans le moindre signe. Une phrase
# tranchée en plein mot n'apprend rien : c'est comme si elle n'était pas là.

_BUT_LONG = ("Je repeins intégralement le décor du plateau en rose bonbon "
             "avec des paillettes et un manteau de fourrure")


def _lignes_qui_debordent(lbl) -> list:
    """Les lignes affichées qui ne tiennent PAS dans la place du label."""
    fm = QFontMetrics(lbl.font())
    largeur = lbl.contentsRect().width()
    return [ligne for ligne in lbl.text().split("\n")
            if fm.horizontalAdvance(ligne) > largeur + 1]


def test_le_message_d_un_objectif_imminent_tient_dans_le_toast(direct):
    """Le défaut d'origine : la phrase dépassait, donc elle était rognée."""
    direct.show_goal_imminent("aypierre", "Aypierre",
                              "Je repeins le décor en rose", 22622.0,
                              "https://zevent.fr/dons")
    sub = _toasts(direct)[0]._sub
    assert _lignes_qui_debordent(sub) == []


def _ligne_pincee(qtbot, texte, lignes_max, tiers=3):
    """Un `_LigneRepliee` large d'un TIERS de son texte, quelle que soit la police.

    Passer par le vrai toast liait ces tests aux métriques du poste : sa
    largeur est fixe, celle du texte non. Sans base de polices, Linux mesure la
    même phrase plus étroite — elle tenait sur une ligne, le repli n'avait
    jamais lieu, et la CI tombait là où Windows passait. Une largeur en pixels
    codée en dur aurait le même défaut à l'envers.

    On mesure donc le texte AVEC LA POLICE DU WIDGET, et on prend une fraction
    de cette largeur : il faut alors le même nombre de lignes partout, et c'est
    `lignes_max` seul qui décide s'il faut couper.
    """
    lbl = fullscreen._LigneRepliee(texte, lignes_max=lignes_max)
    qtbot.addWidget(lbl)
    entier = QFontMetrics(lbl.font()).horizontalAdvance(texte)
    # `contentsRect` retire les marges : on vise la largeur UTILE.
    marges = lbl.width() - lbl.contentsRect().width()
    lbl.resize(max(40, entier // tiers) + marges, 200)
    return lbl


def test_un_message_replie_ne_perd_pas_un_seul_mot(qtbot):
    """Se replier n'est pas se couper : la phrase doit rester entière.

    Dix lignes autorisées pour un tiers de largeur : le repli est certain — il
    en faut au moins trois — et la coupe impossible, on en permet dix.
    """
    texte = "plus que 22 622 € — « Je repeins le décor en rose »"
    sub = _ligne_pincee(qtbot, texte, lignes_max=10)
    assert sub.text().replace("\n", " ") == sub.texte_complet()
    assert "\n" in sub.text(), "sans repli, le test ne prouverait rien"
    assert _lignes_qui_debordent(sub) == []
    assert sub.toolTip() == "", "rien n'a été perdu : rien à promettre"


def test_un_objectif_au_nom_interminable_coupe_entre_deux_mots(qtbot):
    """Au-delà du compte de lignes il faut bien couper — mais jamais en plein
    mot, et jamais sans annoncer qu'il manque quelque chose.

    Une seule ligne autorisée pour un texte qui en réclame trois : la coupe est
    certaine, quelle que soit la police.
    """
    sub = _ligne_pincee(qtbot, _BUT_LONG, lignes_max=1)
    assert _lignes_qui_debordent(sub) == []
    assert sub.text().endswith("…")
    dernier = sub.text().replace("\n", " ").rstrip("…").split()[-1]
    assert dernier in sub.texte_complet().split(), "coupé en plein mot"


def test_un_objectif_au_nom_interminable_se_lit_en_entier_en_infobulle(qtbot):
    """Couper sans laisser de quoi lire la suite, c'est ne rien dire du tout.

    Le survol arrête aussi le décompte du toast : on a le temps de lire.
    """
    sub = _ligne_pincee(qtbot, _BUT_LONG, lignes_max=1)
    assert _BUT_LONG in sub.toolTip()


def test_le_toast_reel_ne_perd_ni_ne_deborde(direct):
    """Le même contrat, mais sur le vrai toast : ses dimensions dépendent de la
    police, donc on n'y éprouve que ce qui en est indépendant."""
    direct.show_goal_imminent("aypierre", "Aypierre",
                              "Je repeins le décor en rose", 22622.0,
                              "https://zevent.fr/dons")
    sub = _toasts(direct)[0]._sub
    assert _lignes_qui_debordent(sub) == []
    rendu = sub.text().replace("\n", " ").rstrip("…").strip()
    assert sub.texte_complet().startswith(rendu), "aucun mot inventé ni mutilé"


def test_un_message_qui_tient_sur_une_ligne_ne_promet_pas_de_suite(direct):
    """Une infobulle sur un texte complet ferait chercher ce qui n'existe pas."""
    direct.show_favorite_live("domingo", "Domingo")
    sub = _toasts(direct)[0]._sub
    assert "\n" not in sub.text()
    assert sub.toolTip() == ""


def test_un_message_court_ne_fait_pas_grandir_le_toast(direct):
    """La hauteur ne varie que lorsqu'elle sert : les annonces d'un mot
    gardent la carte compacte qu'on connaît."""
    direct.show_favorite_live("domingo", "Domingo")
    assert _toasts(direct)[0].height() == fullscreen._FavoriteLiveToast._H


def test_un_message_long_fait_grandir_le_toast(direct):
    """Il grandit vers le BAS depuis un coin haut fixe : rien ne se décale."""
    direct.show_goal_imminent("aypierre", "Aypierre", _BUT_LONG, 22622.0,
                              "https://zevent.fr/dons")
    toast = _toasts(direct)[0]
    assert toast.height() > fullscreen._FavoriteLiveToast._H
    assert toast.pos().y() == 16 + direct._pinned_audio.height_hint()


def test_la_barre_de_temps_reste_au_bas_d_un_toast_agrandi(direct):
    """Elle dit le temps qui reste : recouverte par le message, elle ne dirait
    plus rien."""
    direct.show_goal_imminent("aypierre", "Aypierre", _BUT_LONG, 22622.0,
                              "https://zevent.fr/dons")
    toast = _toasts(direct)[0]
    QApplication.processEvents()
    bas_du_message = toast._sub.y() + toast._sub.height()
    assert toast._bar.y() >= bas_du_message
    assert toast._bar.y() + toast._bar.height() <= toast.height()


def test_le_nom_du_streamer_s_elide_au_lieu_d_etre_coupe(direct):
    """« Samuel Etienne » dépasse la colonne laissée par les deux boutons.

    Coupé au pixel il devenait « Samuel Etienn », sans rien pour le signaler ;
    élidé, les « … » le disent et l'infobulle rend le nom entier.
    """
    direct.show_goal_imminent("samuel", "Samuel Etienne", "Un objectif",
                              500.0, "https://zevent.fr/dons")
    titre = _toasts(direct)[0]._title
    assert _lignes_qui_debordent(titre) == []
    if titre.text() != "Samuel Etienne":
        assert titre.text().endswith("…")
        assert "Samuel Etienne" in titre.toolTip()


def test_le_nom_du_streamer_n_ecrase_pas_les_boutons(direct):
    """Un QLabel réclame de quoi tout afficher, et le layout prenait cette
    place sur les boutons : « Regarder » s'affichait « EGARDE »."""
    direct.show_goal_imminent("samuel", "Samuel Etienne", "Un objectif",
                              500.0, "https://zevent.fr/dons")
    boutons = _toasts(direct)[0].findChildren(QPushButton)
    assert boutons
    for bouton in boutons:
        assert bouton.width() >= bouton.sizeHint().width()


# ─────────────────────────────────────────────────────────────────────────────
# Toasts discrets — raid, gros don, palier, saturation
# ─────────────────────────────────────────────────────────────────────────────

def _toasts_hype(fenetre) -> list:
    return fenetre.centralWidget().findChildren(fullscreen._FsHypeToast)


def test_un_raid_nomme_sa_source_et_sa_cible(direct):
    direct.show_raid("kamet0", "zerator", 4200)
    texte = _textes(_toasts_hype(direct)[0])
    assert "kamet0" in texte and "zerator" in texte


def test_un_raid_sans_cible_n_annonce_rien(direct):
    direct.show_raid("kamet0", "", 4200)
    assert _toasts_hype(direct) == []


def test_un_bombardement_est_nomme_comme_tel(direct):
    """Un bombardement n'est pas un don : la somme vient de dizaines de
    personnes d'un coup, et c'est ce qui rend le moment intéressant."""
    direct.show_big_donation("zerator", "ZeratoR", 5000.0, "bombardement")
    assert "bombardement" in _textes(_toasts_hype(direct)[0])


def test_un_gros_don_affiche_la_somme_signee(direct):
    direct.show_big_donation("zerator", "ZeratoR", 5000.0)
    assert "+5" in _textes(_toasts_hype(direct)[0])


def test_un_palier_de_cagnotte_est_annonce_la_ou_on_regarde(direct):
    """Le panel est sur un autre écran, souvent hors du champ de vision."""
    direct.show_milestone(1_000_000.0, "1 million")
    assert "1 million" in _textes(_toasts_hype(direct)[0])


def test_un_palier_sans_libelle_n_annonce_rien(direct):
    direct.show_milestone(1_000_000.0, "")
    assert _toasts_hype(direct) == []


def test_la_saturation_du_poste_dit_quoi_faire(direct):
    """Rien n'est cassé : l'utilisateur doit comprendre que c'est le nombre de
    flux qui va dégrader l'image, pas une panne."""
    direct.show_resource_alert("gpu", 96.0, 0.5)
    texte = _textes(_toasts_hype(direct)[0])
    assert "96" in texte and "flux" in texte


def test_les_toasts_discrets_se_rangent_sous_les_audios_epingles(direct):
    direct.set_pinned_audio(["kamet0"])
    QApplication.processEvents()
    direct.show_hype_alert("zerator", "ça s'emballe", 1.0, "#00ff87")
    toast = _toasts_hype(direct)[0]
    assert toast.pos().y() >= 8 + direct._pinned_audio.height_hint()


def test_le_texte_d_un_toast_hype_reste_du_texte_brut(direct):
    """Le libellé est produit par un modèle de langage à partir du CHAT : du
    texte riche y ferait exécuter ce que le chat a dicté."""
    direct.show_hype_alert("zerator", "<b>gras</b>", 1.0, "#00ff87")
    toast = _toasts_hype(direct)[0]
    porteurs = [lbl for lbl in toast.findChildren(QLabel)
                if lbl.text() in ("<b>gras</b>", "zerator")]
    assert len(porteurs) == 2, "le nom et le libellé doivent être affichés"
    assert all(lbl.textFormat() == Qt.TextFormat.PlainText for lbl in porteurs)


# ─────────────────────────────────────────────────────────────────────────────
# Bandeau de publicité
# ─────────────────────────────────────────────────────────────────────────────

def test_une_publicite_pose_son_bandeau_et_range_la_barre(direct):
    """Les deux occupent le bas de l'écran ; le bandeau est l'information la
    plus utile du moment."""
    direct._on_ad_detected("zerator")
    assert direct._ad_banner is not None
    assert direct._overlay.isHidden()


def test_une_publicite_sur_une_autre_chaine_ne_pose_rien(direct):
    """Le guetteur surveille aussi les cellules de la grille."""
    direct._on_ad_detected("domingo")
    assert direct._ad_banner is None


def test_deux_detections_ne_posent_pas_deux_bandeaux(direct):
    direct._on_ad_detected("zerator")
    premier = direct._ad_banner
    direct._on_ad_detected("zerator")
    assert direct._ad_banner is premier


def test_le_bandeau_de_publicite_compte_le_temps_ecoule(direct):
    """Sans durée, rien ne dit si la coupure dure depuis dix secondes ou deux
    minutes — donc s'il vaut la peine d'aller voir ailleurs."""
    direct._on_ad_detected("zerator")
    bandeau = direct._ad_banner
    assert "0:00" in bandeau._msg.text()
    bandeau._tick()
    assert "0:01" in bandeau._msg.text()


def test_le_bandeau_confirme_l_inscription_a_la_notification(direct):
    """Un bouton qui ne change pas d'aspect se reclique, et l'inscription
    n'est faite qu'une fois."""
    direct._on_ad_detected("zerator")
    bandeau = direct._ad_banner
    bandeau._on_notify()
    assert "zerator" in direct._ad_notify_logins
    assert not bandeau._notify_btn.isEnabled()


def test_la_fin_de_publicite_retire_le_bandeau(direct):
    direct._on_ad_detected("zerator")
    direct._on_ad_ended("zerator")
    assert direct._ad_banner is None
    assert direct._ad_active is False


def test_la_fin_de_publicite_previent_seulement_qui_l_a_demande(direct):
    """Le toast propose de revenir : non demandé, il interrompt pour rien."""
    direct._on_ad_ended("domingo")
    assert direct.centralWidget().findChildren(fullscreen._AdEndToast) == []
    direct._on_ad_notify_requested("domingo")
    direct._on_ad_ended("domingo")
    assert len(direct.centralWidget().findChildren(fullscreen._AdEndToast)) == 1


def test_le_toast_de_fin_de_publicite_propose_de_revenir(direct, qtbot):
    direct._on_ad_notify_requested("domingo")
    direct._on_ad_ended("domingo")
    toast = direct.centralWidget().findChildren(fullscreen._AdEndToast)[0]
    boutons = toast.findChildren(QPushButton)
    with qtbot.waitSignal(direct.stream_change_requested, timeout=1000) as attente:
        boutons[0].click()
    assert attente.args == ["domingo"]


def test_changer_de_chaine_efface_le_bandeau_de_publicite(direct):
    """Il porte le nom de la chaîne précédente : le laisser serait mentir."""
    direct._on_ad_detected("zerator")
    direct.set_stream("domingo", "Jeu", 100)
    assert direct._ad_banner is None
    assert direct._ad_active is False


# ─────────────────────────────────────────────────────────────────────────────
# Liste des audios épinglés
# ─────────────────────────────────────────────────────────────────────────────

def test_aucun_audio_epingle_n_affiche_aucune_boite(fenetre):
    assert fenetre._pinned_audio.isHidden()
    assert fenetre._pinned_audio.height_hint() == 0


def test_chaque_chaine_epinglee_a_sa_ligne(direct):
    """Le son de la grille vient de cellules qu'on ne regarde pas : sans cette
    liste, on ne sait pas ce qu'on entend."""
    direct.set_pinned_audio(["kamet0", "mistermv"])
    QApplication.processEvents()
    liste = direct._pinned_audio
    assert not liste.isHidden()
    assert "kamet0" in _textes(liste) and "mistermv" in _textes(liste)


def test_la_boite_grandit_avec_le_nombre_de_lignes(direct):
    """La hauteur est CALCULÉE et non mesurée : `sizeHint()` interrogé dans la
    foulée rend encore celle du bandeau seul, et la boîte se figeait à 31 px
    en tronquant toute la liste."""
    direct.set_pinned_audio(["a"])
    une = direct._pinned_audio.height()
    direct.set_pinned_audio(["a", "b", "c"])
    assert direct._pinned_audio.height() > une


def test_depingler_la_derniere_chaine_range_la_boite(direct):
    direct.set_pinned_audio(["kamet0"])
    direct.set_pinned_audio([])
    assert direct._pinned_audio.isHidden()


def test_une_liste_inchangee_n_est_pas_reconstruite(direct):
    """Elle est repoussée à chaque tour de rafraîchissement ; reconstruire
    ferait clignoter les avatars toutes les trente secondes."""
    direct.set_pinned_audio(["kamet0"])
    lignes = dict(direct._pinned_audio._elements)
    direct.set_pinned_audio(["kamet0"])
    assert direct._pinned_audio._elements == lignes


def test_une_chaine_coupee_depuis_la_console_est_grisee(direct):
    """La liste affirmerait sinon qu'on entend une chaîne silencieuse —
    exactement l'information qu'elle est censée donner."""
    direct.set_pinned_audio(["kamet0", "mistermv"])
    direct.set_pinned_muted("kamet0", True)
    _, nom_coupe = direct._pinned_audio._elements["kamet0"]
    _, nom_actif = direct._pinned_audio._elements["mistermv"]
    assert nom_coupe.styleSheet() != nom_actif.styleSheet()


def test_retablir_le_son_d_une_chaine_la_reveille(direct):
    direct.set_pinned_audio(["kamet0"])
    direct.set_pinned_muted("kamet0", True)
    coupe = direct._pinned_audio._elements["kamet0"][1].styleSheet()
    direct.set_pinned_muted("kamet0", False)
    assert direct._pinned_audio._elements["kamet0"][1].styleSheet() != coupe


def test_l_etat_coupe_survit_a_une_reconstruction_de_la_liste(direct):
    """Les lignes sont recréées à chaque changement de liste : sans réapplique,
    une chaîne coupée redevenait visuellement active en épinglant une autre."""
    direct.set_pinned_audio(["kamet0"])
    direct.set_pinned_muted("kamet0", True)
    coupe = direct._pinned_audio._elements["kamet0"][1].styleSheet()
    direct.set_pinned_audio(["kamet0", "mistermv"])
    assert direct._pinned_audio._elements["kamet0"][1].styleSheet() == coupe


def test_la_boite_reste_calee_en_haut_a_droite(direct):
    direct.set_pinned_audio(["kamet0"])
    liste = direct._pinned_audio
    assert liste.pos().x() + liste.width() <= 1920
    assert liste.pos().y() == 8


def test_la_boite_laisse_passer_les_clics(direct):
    """Purement informative : elle recouvre la vidéo, et avalerait les clics
    destinés à l'image."""
    assert direct._pinned_audio.testAttribute(
        Qt.WidgetAttribute.WA_TransparentForMouseEvents)


# ─────────────────────────────────────────────────────────────────────────────
# Menu télécommande
# ─────────────────────────────────────────────────────────────────────────────

def _ouvrir_menu(fenetre, qtbot=None) -> None:
    """Ouvre le menu ; avec `qtbot`, attend la fin du glissement d'entrée.

    Le menu part de -320 px et rejoint 0 en 200 ms : tant que l'animation
    court, sa géométrie n'est pas encore celle qu'un clic rencontrera.
    """
    fenetre._toggle_remote_menu()
    QApplication.processEvents()
    if qtbot is not None:
        qtbot.waitUntil(lambda: fenetre._remote_menu.geometry().x() == 0,
                        timeout=2000)


def test_le_menu_separe_les_chaines_choisies_des_autres(direct):
    """Vingt-quatre cellules et trois cents chaînes en direct : sans sections,
    retrouver ce qu'on a mis dans la grille demande de tout parcourir."""
    direct.update_remote_menu(
        [_Chaine("kamet0"), _Chaine("mistermv"), _Chaine("domingo")], ["kamet0"])
    _ouvrir_menu(direct)
    menu = direct._remote_menu
    assert isinstance(menu._items[0], fullscreen.RemoteItemLarge)
    assert all(isinstance(i, fullscreen.RemoteItemSmall) for i in menu._items[1:])
    assert menu._login_list[0] == "kamet0"


def test_les_chaines_hors_ligne_ne_sont_pas_listees(direct):
    """Le menu sert à CHANGER de flux : proposer un flux mort ne mène nulle part."""
    direct.update_remote_menu(
        [_Chaine("kamet0"), _Chaine("domingo", online=False)], [])
    _ouvrir_menu(direct)
    assert direct._remote_menu._login_list == ["kamet0"]


def test_le_menu_annonce_le_nombre_de_directs(direct):
    direct.update_remote_menu(
        [_Chaine("a"), _Chaine("b"), _Chaine("c", online=False)], [])
    _ouvrir_menu(direct)
    assert direct._remote_menu._count_lbl.text() == "2 en live"


def test_une_liste_recue_menu_ferme_n_est_pas_reconstruite_pour_rien(direct):
    """Elle arrive toutes les trente secondes ; reconstruire trois cents lignes
    invisibles coûte, et repartait à zéro la sélection au clavier."""
    direct.update_remote_menu([_Chaine("kamet0")], [])
    assert direct._remote_menu._items == []
    assert direct._remote_menu._needs_rebuild is True


def test_ouvrir_le_menu_rattrape_la_liste_differee(direct):
    direct.update_remote_menu([_Chaine("kamet0")], [])
    _ouvrir_menu(direct)
    assert direct._remote_menu._login_list == ["kamet0"]
    assert direct._remote_menu._needs_rebuild is False


def test_la_selection_au_clavier_ne_deborde_pas_de_la_liste(direct):
    direct.update_remote_menu([_Chaine("a"), _Chaine("b")], [])
    _ouvrir_menu(direct)
    menu = direct._remote_menu
    for _ in range(5):
        menu.select_next()
    assert menu._keyboard_idx == 1
    for _ in range(5):
        menu.select_previous()
    assert menu._keyboard_idx == 0


def test_naviguer_dans_un_menu_vide_ne_leve_pas(direct):
    """Avant le premier rafraîchissement de l'API, la liste est vide."""
    _ouvrir_menu(direct)
    direct._remote_menu.select_next()
    direct._remote_menu.select_previous()
    assert direct._remote_menu._keyboard_idx == -1


def test_valider_la_selection_demande_le_changement_de_flux(direct, qtbot):
    direct.update_remote_menu([_Chaine("kamet0"), _Chaine("domingo")], [])
    _ouvrir_menu(direct)
    direct._remote_menu.select_next()
    with qtbot.waitSignal(direct.stream_change_requested, timeout=1000) as attente:
        direct._remote_menu.confirm_selection()
    assert attente.args == ["kamet0"]


def test_valider_sans_rien_avoir_choisi_ne_change_pas_de_flux(direct):
    direct.update_remote_menu([_Chaine("kamet0")], [])
    _ouvrir_menu(direct)
    recus: list[str] = []
    direct.stream_change_requested.connect(recus.append)
    direct._remote_menu.confirm_selection()
    assert recus == []


def test_cliquer_une_ligne_demande_le_changement_de_flux(direct, qtbot):
    direct.update_remote_menu([_Chaine("kamet0")], [])
    _ouvrir_menu(direct)
    with qtbot.waitSignal(direct.stream_change_requested, timeout=1000) as attente:
        direct._remote_menu._items[0].clicked.emit("kamet0")
    assert attente.args == ["kamet0"]


def test_changer_de_flux_deplace_la_marque_sans_tout_reconstruire(direct):
    """Un simple changement de flux ne doit pas faire clignoter trois cents
    lignes ni perdre la position de défilement."""
    direct.update_remote_menu([_Chaine("kamet0"), _Chaine("domingo")], [])
    _ouvrir_menu(direct)
    lignes = list(direct._remote_menu._items)
    direct._remote_menu.set_current_login("domingo")
    assert direct._remote_menu._items == lignes
    assert [i._is_current for i in lignes] == [
        i._login == "domingo" for i in lignes]


def test_ouvrir_le_menu_efface_le_bouton_qu_il_recouvre(direct):
    """Le bouton est à (8, 8), donc DANS l'emprise du menu une fois ouvert —
    et il se posait par-dessus son titre."""
    _ouvrir_menu(direct)
    assert direct._remote_btn.isHidden()


def test_refermer_le_menu_rend_le_bouton(direct):
    _ouvrir_menu(direct)
    direct._close_remote_menu()
    assert not direct._remote_btn.isHidden()


def test_un_clic_a_cote_referme_le_menu(direct, qtbot):
    """Sans cela il fallait viser le bouton pour sortir, dans un menu qui
    recouvre justement l'image qu'on voulait cliquer."""
    _ouvrir_menu(direct, qtbot)
    _clic(direct, 1200, 500)
    assert direct._remote_menu._hiding is True
    assert not direct._remote_btn.isHidden()


def test_un_clic_dans_le_menu_ne_le_referme_pas(direct, qtbot):
    """Viser une ligne ne doit pas escamoter le menu sous le curseur."""
    _ouvrir_menu(direct, qtbot)
    _clic(direct, 100, 300)
    assert direct._remote_menu._hiding is False


def test_la_ligne_du_flux_affiche_se_distingue_des_autres(direct):
    direct.update_remote_menu([_Chaine("zerator"), _Chaine("domingo")], ["zerator"])
    _ouvrir_menu(direct)
    courante = direct._remote_menu._items[0]
    autre = direct._remote_menu._items[1]
    assert courante._is_current is True
    assert courante.styleSheet() != autre.styleSheet()


def test_survoler_une_ligne_revele_son_bouton_regarder(direct):
    """Un bouton visible sur trois cents lignes ferait un mur de boutons."""
    direct.update_remote_menu([_Chaine("kamet0")], ["kamet0"])
    _ouvrir_menu(direct)
    ligne = direct._remote_menu._items[0]
    assert ligne._watch_btn.isHidden()
    _survol(ligne)
    assert not ligne._watch_btn.isHidden()
    _sortie(ligne)
    assert ligne._watch_btn.isHidden()


# ─────────────────────────────────────────────────────────────────────────────
# Gestes : run_action, palette, raccourcis clavier
# ─────────────────────────────────────────────────────────────────────────────

def test_le_geste_muet_coupe_reellement_le_son(direct):
    direct.run_action("muet")
    assert direct._muted is True
    assert direct._mpv.coupures[-1] is True


def test_le_geste_favori_pose_l_etoile_et_le_fait_savoir(direct, favoris_vierges, qtbot):
    """Le panel affiche la même étoile sur ses cartes : sans ce signal, les
    deux moitiés de l'application divergent."""
    with qtbot.waitSignal(direct.favori_change, timeout=1000) as attente:
        direct.run_action("favori")
    assert attente.args == ["zerator", True]
    assert favoris_vierges.is_favorite("zerator") is True


def test_le_geste_favori_le_dit_a_l_ecran(direct, favoris_vierges):
    """Le geste vient souvent du clavier ou du Stream Deck : sans annonce,
    rien ne distingue une étoile posée d'une touche qui n'a pas répondu."""
    direct.run_action("favori")
    assert direct._annonce is not None
    assert "favoris" in _textes(direct._annonce)


def test_retirer_le_favori_l_annonce_aussi(direct, favoris_vierges):
    direct.run_action("favori")
    direct.run_action("favori")
    assert "Retiré des favoris" in _textes(direct._annonce)
    assert favoris_vierges.is_favorite("zerator") is False


def test_sans_flux_affiche_le_geste_favori_ne_fait_rien(fenetre, favoris_vierges):
    """Il n'y a pas de chaîne à mettre en favori — et `""` en serait une."""
    fenetre.run_action("favori")
    assert favoris_vierges.get() == set()


def test_un_etat_basculable_est_publie_pour_le_stream_deck(direct, favoris_vierges, qtbot):
    """Les touches du boîtier portent ces états ; sans le signal, elles
    resteraient allumées sur l'ancien."""
    with qtbot.waitSignal(direct.etat_bascule, timeout=1000):
        direct.run_action("favori")


def test_le_geste_chat_bascule_le_panneau_et_son_bouton(fenetre, qtbot):
    """Le bouton porte l'état : décoché sur un chat ouvert, il invite à
    l'ouvrir une seconde fois."""
    with qtbot.waitSignal(fenetre.etat_bascule, timeout=1000):
        fenetre.run_action("chat")
    assert fenetre.chat_ouvert is True
    assert fenetre._chat_btn.isChecked() is True
    fenetre.run_action("chat")
    assert fenetre.chat_ouvert is False
    assert fenetre._chat_btn.isChecked() is False


def test_la_palette_s_ouvre_au_raccourci_clavier(direct, qtbot):
    """La frappe arrive d'abord au widget qui a le focus, et la vidéo est une
    fenêtre native qui ne fait pas remonter les touches : il a fallu un
    raccourci de portée FENÊTRE pour que Ctrl+K réponde."""
    qtbot.keyClick(direct, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
    QApplication.processEvents()
    assert direct._palette.isVisible()


def test_la_palette_relaie_ses_actions_a_la_fenetre(direct):
    direct._palette.action_requested.emit("muet")
    assert direct._muted is True


def test_la_palette_connait_les_chaines_qu_on_lui_donne(direct):
    """Sans cet alimentation, taper trois lettres ne proposait rien."""
    chaines = [_Chaine("kamet0"), _Chaine("mistermv")]
    direct.set_streamers(chaines)
    assert direct._palette._streamers == chaines


def test_toute_action_proposee_par_la_palette_est_executable(direct):
    """Une commande listée qui ne fait rien est indiscernable d'une panne.

    `CommandPalette._ACTIONS` est le catalogue COMMUN aux deux palettes ;
    `_actions_de_l_hote` est ce que celle-ci propose vraiment, filtré sur ce que son
    hôte sait exécuter. `FullscreenWindow.ACTIONS` est cette liste-là.

    Le panel intercepte « recap » avant de relayer le reste ; le plein écran
    branche `action_requested` directement sur `run_action`, où une clé
    inconnue meurt dans un `logger.debug`. Sans filtre, la commande était
    listée, cliquable, et sans effet.
    """
    proposees = {cle for cle, _libelle in direct._palette._actions_de_l_hote}
    assert proposees <= set(direct.ACTIONS)
    assert "recap" not in proposees, "seul le panel sait montrer le récapitulatif"


def test_les_chiffres_designent_une_cellule_de_la_grille(direct, qtbot):
    """Numérotées à partir de 1 à l'écran, indexées à partir de 0 dans le code."""
    with qtbot.waitSignal(direct.slot_requested, timeout=1000) as attente:
        _frappe(direct, Qt.Key.Key_1)
    assert attente.args == [0]
    with qtbot.waitSignal(direct.slot_requested, timeout=1000) as attente:
        _frappe(direct, Qt.Key.Key_9)
    assert attente.args == [8]


def test_les_fleches_horizontales_changent_de_voisin(direct, qtbot):
    with qtbot.waitSignal(direct.neighbour_requested, timeout=1000) as attente:
        _frappe(direct, Qt.Key.Key_Left)
    assert attente.args == [-1]
    with qtbot.waitSignal(direct.neighbour_requested, timeout=1000) as attente:
        _frappe(direct, Qt.Key.Key_Right)
    assert attente.args == [1]


@pytest.mark.parametrize("touche", [Qt.Key.Key_Plus, Qt.Key.Key_Equal])
def test_deux_touches_montent_le_volume(direct, touche):
    """« + » demande Maj sur un clavier AZERTY : la touche nue vaut « = »."""
    direct.set_volume(60)
    _frappe(direct, touche)
    assert direct._volume == 65


def test_la_touche_moins_baisse_le_volume(direct):
    direct.set_volume(60)
    _frappe(direct, Qt.Key.Key_Minus)
    assert direct._volume == 55


def test_regler_le_son_au_clavier_rappelle_la_barre(direct):
    """Un volume qu'on modifie sans voir le curseur se règle à l'aveugle."""
    direct._hide_overlay()
    _frappe(direct, Qt.Key.Key_Minus)
    assert not direct._overlay.isHidden()


def test_la_touche_m_coupe_le_son(direct):
    _frappe(direct, Qt.Key.Key_M)
    assert direct._muted is True


def test_la_touche_f_bascule_le_favori(direct, favoris_vierges):
    _frappe(direct, Qt.Key.Key_F)
    assert favoris_vierges.is_favorite("zerator") is True


def test_les_fleches_verticales_pilotent_le_menu(direct):
    direct.update_remote_menu([_Chaine("a"), _Chaine("b")], [])
    _ouvrir_menu(direct)
    _frappe(direct, Qt.Key.Key_Down)
    assert direct._remote_menu._keyboard_idx == 0
    _frappe(direct, Qt.Key.Key_Down)
    assert direct._remote_menu._keyboard_idx == 1
    _frappe(direct, Qt.Key.Key_Up)
    assert direct._remote_menu._keyboard_idx == 0


def test_entree_valide_la_ligne_du_menu(direct, qtbot):
    direct.update_remote_menu([_Chaine("kamet0")], [])
    _ouvrir_menu(direct)
    _frappe(direct, Qt.Key.Key_Down)
    with qtbot.waitSignal(direct.stream_change_requested, timeout=1000) as attente:
        _frappe(direct, Qt.Key.Key_Return)
    assert attente.args == ["kamet0"]


def test_une_touche_sans_geste_ne_declenche_rien(direct):
    """Elle doit poursuivre sa route : Qt s'en sert pour ses propres
    raccourcis, et l'avaler couperait Alt+F4."""
    avant = (direct._volume, direct._muted)
    _frappe(direct, Qt.Key.Key_Z)
    assert (direct._volume, direct._muted) == avant


def test_la_table_des_touches_n_est_construite_qu_une_fois(direct):
    """Ses entrées sont des méthodes LIÉES : reconstruire à chaque frappe
    referait autant de fermetures pour rien."""
    assert direct._touches is None
    premiere = direct._carte_des_touches()
    assert direct._carte_des_touches() is premiere


def test_echap_ferme_la_palette_avant_tout(direct):
    """C'est la surcouche la plus récente, donc celle qu'on veut quitter."""
    direct._palette.open()
    _frappe(direct, Qt.Key.Key_Escape)
    assert not direct._palette.isVisible()


def test_echap_referme_ensuite_le_menu_telecommande(direct):
    _ouvrir_menu(direct)
    _frappe(direct, Qt.Key.Key_Escape)
    assert direct._remote_menu._hiding is True


def test_echap_sans_rien_d_ouvert_ferme_la_fenetre(direct, monkeypatch):
    fermetures: list[int] = []
    monkeypatch.setattr(type(direct), "close",
                        lambda self: fermetures.append(1))
    _frappe(direct, Qt.Key.Key_Escape)
    assert fermetures == [1]


# ─────────────────────────────────────────────────────────────────────────────
# Volume et coupure du son
# ─────────────────────────────────────────────────────────────────────────────

def test_le_volume_retenu_est_celui_de_la_fenetre_pas_du_flux(fabrique):
    """Changer de streamer remettait le son à fond alors que la barre affichait
    toujours le réglage précédent."""
    f = fabrique(volume=35)
    f.set_stream("zerator", "Jeu", 10)
    f.on_stream_ready("zerator", "http://exemple.invalide/flux.m3u8")
    assert f._mpv.volumes[-1] == 35


def test_couper_le_son_ne_perd_pas_le_reglage(direct):
    """Rétablir doit rendre le volume d'avant, pas un défaut."""
    direct.set_volume(70)
    direct._toggle_mute()
    assert direct._mpv.volumes[-1] == 0
    assert direct._volume == 70
    direct._toggle_mute()
    assert direct._mpv.volumes[-1] == 70


def test_bouger_le_curseur_retablit_le_son(direct):
    """Chercher pourquoi on n'entend rien alors qu'on vient de monter le
    volume est le pire des retours."""
    direct._toggle_mute()
    direct._vol_slider.setValue(45)
    assert direct._muted is False
    assert direct._mpv.volumes[-1] == 45


def test_le_volume_venu_de_l_exterieur_est_borne(direct):
    """La télécommande envoie ce qu'on lui donne, y compris n'importe quoi."""
    direct.set_volume(500)
    assert direct._volume == 100
    direct.set_volume(-20)
    assert direct._volume == 0


def test_la_console_de_mixage_est_prevenue_de_chaque_reglage(direct, qtbot):
    """Régler le son en plein écran laissait la tranche du mixer sur sa valeur
    d'avant — la régression qui a motivé ce signal."""
    with qtbot.waitSignal(direct.volume_changed, timeout=1000) as attente:
        direct.set_volume(42)
    assert attente.args == [42]
    with qtbot.waitSignal(direct.mute_changed, timeout=1000) as attente:
        direct._toggle_mute()
    assert attente.args == [True]


def test_couper_depuis_la_console_ne_repeint_pas_pour_rien(direct):
    """La console republie son état à chaque tour : réappliquer une coupure
    déjà en place renverrait le signal et ferait boucler les deux fenêtres."""
    direct.set_muted(True)
    avant = len(direct._mpv.coupures)
    direct.set_muted(True)
    assert len(direct._mpv.coupures) == avant


@pytest.mark.parametrize("volume,muet,attendu", [
    (0, False, "\U0001f507"),
    (30, False, "\U0001f509"),
    (80, False, "\U0001f50a"),
    (80, True, "\U0001f507"),
])
def test_l_icone_du_son_montre_l_etat_reel(direct, volume, muet, attendu):
    """Un seul bouton pour deux états — coupé, et fort ou faible : sans
    l'icône, rien ne distingue un son coupé d'un flux muet."""
    direct.set_volume(volume)
    direct._muted = muet
    direct._apply_volume()
    assert direct._vol_btn.text() == attendu


def test_le_reglage_du_son_n_est_pas_ecrit_a_chaque_cran(direct, fabrique):
    """Le curseur émet à chaque pixel : écrire config.json à chaque cran ferait
    des centaines d'accès disque pour un seul geste."""
    direct.set_volume(42)
    assert direct._vol_save_timer.isActive()
    assert not fabrique.reglages.exists() or \
        json.loads(fabrique.reglages.read_text(encoding="utf-8")).get("volume") != 42


def test_le_reglage_finit_par_etre_ecrit(direct, fabrique):
    direct.set_volume(42)
    direct._toggle_mute()
    direct._persist_volume()
    garde = json.loads(fabrique.reglages.read_text(encoding="utf-8"))
    assert garde["volume"] == 42 and garde["muted"] is True


def test_le_reglage_du_son_n_ecrase_pas_les_autres_preferences(direct, fabrique):
    """config.json porte aussi les favoris et les réglages de clips."""
    fabrique.reglages.write_text(json.dumps({"favorite_logins": ["zerator"]}),
                                 encoding="utf-8")
    direct._persist_volume()
    garde = json.loads(fabrique.reglages.read_text(encoding="utf-8"))
    assert garde["favorite_logins"] == ["zerator"]


def test_l_icone_du_son_reflete_le_reglage_retenu_des_l_ouverture(fabrique):
    """La barre s'affiche dès `set_stream`, avant la première image.

    Le réglage retenu est pourtant connu au moment de la construction : le
    curseur le porte déjà. Seule l'icône reste muette — un bouton vide, à
    côté d'un curseur qui, lui, affiche 40.
    """
    f = fabrique(volume=40, muted=True)
    f.set_stream("zerator", "Minecraft", 10)
    assert f._vol_btn.text() == "\U0001f507"


# ─────────────────────────────────────────────────────────────────────────────
# Géométrie : qui occupe quelle surface
# ─────────────────────────────────────────────────────────────────────────────

def test_sans_chat_la_video_occupe_toute_la_fenetre(direct):
    direct._update_mpv_geometry()
    assert direct._stack.geometry().width() == 1920


def test_le_chat_ouvert_retrecit_la_video_d_autant(direct):
    """La vidéo est une fenêtre NATIVE posée par-dessus le rendu Qt : elle ne
    se laisse pas recouvrir, il faut lui retirer la place."""
    direct._chat_panel._visible = True
    direct._chat_panel._width = 400
    direct._update_mpv_geometry()
    assert direct._stack.geometry().width() == 1520


def test_la_barre_d_information_colle_au_bas_de_la_video(direct):
    direct._update_mpv_geometry()
    barre = direct._overlay.geometry()
    assert barre.height() == 56
    assert barre.y() + barre.height() == 1080
    assert barre.width() == 1920


def test_la_barre_d_information_suit_le_retrecissement(direct):
    direct._chat_panel._visible = True
    direct._chat_panel._width = 400
    direct._update_mpv_geometry()
    assert direct._overlay.geometry().width() == 1520


def test_la_page_de_don_relegue_le_direct_dans_un_coin(direct):
    """Trois dispositions s'excluent ; c'est ici qu'on choisit laquelle."""
    direct._pip_active = True
    direct._update_mpv_geometry()
    coin = direct._stack.geometry()
    assert (coin.width(), coin.height()) == (320, 180)


def test_le_replay_relegue_le_direct_dans_un_coin(direct):
    direct._replay_active = True
    direct._update_mpv_geometry()
    coin = direct._stack.geometry()
    assert (coin.width(), coin.height()) == (
        fullscreen._REPLAY_PIP_W, fullscreen._REPLAY_PIP_H)


def test_le_replay_l_emporte_sur_la_page_de_don(direct):
    """Les deux drapeaux peuvent être levés en même temps — la page de don
    reste ouverte derrière — et une seule disposition peut s'appliquer."""
    direct._replay_active = True
    direct._pip_active = True
    direct._update_mpv_geometry()
    coin = direct._stack.geometry()
    assert (coin.width(), coin.height()) == (
        fullscreen._REPLAY_PIP_W, fullscreen._REPLAY_PIP_H)


def test_le_bandeau_de_publicite_suit_la_fenetre(direct):
    """Il est posé à la largeur du moment ; sans recalage, un changement de
    résolution le laissait à cheval hors de l'écran."""
    direct._on_ad_detected("zerator")
    direct.resize(1280, 720)
    QApplication.processEvents()
    bandeau = direct._ad_banner.geometry()
    assert bandeau.width() == 1280
    assert bandeau.y() + bandeau.height() == 720


def test_l_annonce_se_replace_avec_la_fenetre(direct):
    direct.annoncer("Clip sauvegardé")
    direct.resize(1280, 720)
    QApplication.processEvents()
    assert direct._annonce.pos().x() == (1280 - direct._annonce.width()) // 2


def test_le_bouton_du_menu_reste_dans_le_coin_haut_gauche(direct):
    direct._update_mpv_geometry()
    assert (direct._remote_btn.x(), direct._remote_btn.y()) == (8, 8)


# ── Un volume absolu doit s'appliquer, même s'il n'a pas changé ─────────────
#
# `set_volume` passait par le curseur, qui n'émet rien quand on lui repose sa
# propre valeur. Une molette de Stream Deck envoie un volume ABSOLU : couper le
# son au clavier puis demander « 50 » alors que le curseur affiche déjà 50 ne
# rétablissait donc rien.

def test_un_volume_identique_retablit_quand_meme_le_son(direct):
    direct.set_volume(50)
    direct._toggle_mute()
    assert direct._muted is True

    direct.set_volume(50)

    assert direct._muted is False, "le son devait revenir"
    assert direct._volume == 50


def test_un_volume_venu_de_l_exterieur_ne_boucle_pas(direct):
    """Le curseur ne doit pas renvoyer ce qu'on vient de lui poser."""
    recus: list[int] = []
    direct.volume_changed.connect(recus.append)
    direct.set_volume(37)
    assert recus == [37], "une seule notification, pas deux"


@pytest.mark.parametrize("demande,attendu", [(-10, 0), (0, 0), (150, 100)])
def test_un_volume_hors_bornes_est_ramene(direct, demande, attendu):
    direct.set_volume(demande)
    assert direct._volume == attendu


# ── « Préviens-moi quand la pub sera finie » ────────────────────────────────
#
# C'était le départ vers une autre chaîne — l'unique cas d'usage — qui coupait
# la veille sur celle qu'on venait de quitter. Le toast ne pouvait jamais
# arriver.

def test_une_chaine_attendue_reste_surveillee_apres_le_depart(direct, monkeypatch):
    # La fixture regarde déjà « zerator » : on part de là.
    arretees: list[str] = []
    monkeypatch.setattr(direct._ad_watcher, "unwatch", arretees.append)

    direct._on_ad_notify_requested("zerator")
    direct.set_stream("mistermv")

    assert "zerator" not in arretees, "on attend la fin de sa pub"


def test_une_chaine_quittee_sans_attente_cesse_d_etre_surveillee(direct, monkeypatch):
    """La veille coûte une requête toutes les trois secondes : la laisser
    tourner sur tout ce qu'on a regardé finirait par les accumuler."""
    arretees: list[str] = []
    monkeypatch.setattr(direct._ad_watcher, "unwatch", arretees.append)

    direct.set_stream("mistermv")

    assert arretees == ["zerator"]


def test_la_veille_se_relache_une_fois_le_toast_montre(direct, monkeypatch):
    direct._on_ad_notify_requested("zerator")
    direct.set_stream("mistermv")
    arretees: list[str] = []
    monkeypatch.setattr(direct._ad_watcher, "unwatch", arretees.append)

    direct._on_ad_ended("zerator")

    assert arretees == ["zerator"], "plus personne n'attend cette chaîne"


# ── horloge du coin ──────────────────────────────────────────────────────────

def test_l_heure_s_affiche_dans_le_coin_du_plein_ecran(direct):
    """Le plein écran rétracte la barre des tâches : plus aucune horloge."""
    from PyQt6.QtCore import QTime

    direct.rafraichir_heure(QTime(21, 47))
    assert direct._ov_heure.text() == "21:47"


def test_l_horloge_du_plein_ecran_bat_a_la_seconde(direct):
    assert direct._horloge.interval() == 1000
    assert direct._horloge.isActive()


def test_l_heure_du_plein_ecran_n_est_reecrite_que_si_elle_change(direct):
    from PyQt6.QtCore import QTime

    direct.rafraichir_heure(QTime(9, 5))
    ecritures = []
    direct._ov_heure.setText = ecritures.append
    direct.rafraichir_heure(QTime(9, 5))
    assert ecritures == []
    direct.rafraichir_heure(QTime(9, 6))
    assert ecritures == ["09:06"]
