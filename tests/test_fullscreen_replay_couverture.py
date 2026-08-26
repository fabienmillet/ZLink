# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Replay, clips et vues incrustées du plein écran, exercés sur une VRAIE fenêtre.

`tests/test_fullscreen_helpers.py` emprunte les méthodes à la classe et les
pose sur des doubles : c'est ce qu'il faut pour éprouver une formule isolée,
mais ça ne dit rien du câblage. Or les trois régressions de la semaine sont
nées là — dans l'enchaînement, pas dans le calcul :

- deux `dump-cache` lancés dans la même seconde visaient le même fichier, et
  le replay repartait chercher chez Twitch vingt-huit secondes alors que
  soixante étaient déjà sur le disque ;
- la barre de progression se rapportait à la durée DEMANDÉE, et s'arrêtait
  donc à mi-course sur une reprise plafonnée par la plateforme ;
- un clip enregistré au clavier ne se voyait nulle part, le seul retour étant
  le libellé d'un bouton dans une barre qui s'efface après deux secondes.

On construit donc la fenêtre pour de bon. Seul libmpv est remplacé — la
machine de test n'en a pas, et une vraie instance poserait un lecteur sur un
`winId()` hors écran. QtWebEngine, lui, est réel : c'est lui qui porte le chat
et la vue de don.
"""

from __future__ import annotations

import types

import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication, QWidget

from windows import fullscreen


# ── doubles ──────────────────────────────────────────────────────────────────

class FauxLecteur(QWidget):
    """Double de `MpvWidget` : enregistre ce qu'on lui demande, ne décode rien.

    Il hérite de QWidget parce que le code de production le pose dans une
    disposition, le montre et le détache — un simple objet Python ne
    reproduirait pas le piège des fenêtres orphelines qu'on veut surveiller.
    """

    playback_ended = fullscreen.pyqtSignal()

    #: Le plein écran ne repeint la surface après un déplacement que sur le
    #: backend de rendu (macOS). Ici on est en mode `wid`, comme sous Windows.
    uses_render_backend = False

    def __init__(self, parent: QWidget | None = None, **_kw) -> None:
        super().__init__(parent)
        #: Chemin rendu par save_clip. `None` = le tampon n'a rien à offrir.
        self.rendu: str | None = ""
        self.dumps: list[tuple[int, str]] = []
        self.lus: list[str] = []
        self.muets: list[bool] = []
        self.volumes: list[int] = []
        self.arrets = 0
        self.terminaisons = 0
        self._position: float | None = None
        self._restant: float | None = None

    def save_clip(self, secs: int, directory: str = "") -> str | None:
        self.dumps.append((secs, directory))
        return self.rendu

    def play(self, url: str) -> None:
        self.lus.append(url)

    def set_mute(self, muted: bool) -> None:
        self.muets.append(bool(muted))

    def set_volume(self, volume: int) -> None:
        self.volumes.append(int(volume))

    def stop(self) -> None:
        self.arrets += 1

    def shutdown(self) -> None:
        self.terminaisons += 1

    def position(self) -> float | None:
        return self._position

    def restant(self) -> float | None:
        return self._restant


class _WebEspion:
    """Note les URL au lieu de les charger.

    Le vrai QWebEngineView irait sur le réseau, ce qu'aucun test n'a le droit
    de faire — et le chat Twitch ouvrirait une session au passage.
    """

    def __init__(self) -> None:
        self.urls: list[str] = []

    def setUrl(self, url) -> None:
        self.urls.append(url.toString())


class _Horloge:
    """Remplace `time` dans le module : le temps n'avance que si on le pousse."""

    def __init__(self) -> None:
        self.maintenant = 1_000.0

    def monotonic(self) -> float:
        return self.maintenant

    def avancer(self, secondes: float) -> None:
        self.maintenant += secondes


def _flottantes(connues) -> list:
    """Widgets de premier niveau VISIBLES qui ne sont pas les nôtres.

    Un widget détaché par `setParent(None)` alors qu'il est encore visible
    devient une fenêtre du bureau. Le replay en crée et en détruit quatre à
    chaque ouverture : c'est exactement le terrain de ce bug.
    """
    return [w for w in QApplication.instance().topLevelWidgets()
            if w.isVisible() and w not in connues]


def _clic(type_, global_x, bouton, boutons) -> QMouseEvent:
    """Un événement souris dont seule la position GLOBALE compte.

    Le redimensionnement du chat se calcule en coordonnées écran : la position
    locale changerait à mesure que la poignée se déplace.
    """
    return QMouseEvent(type_, QPointF(0.0, 0.0), QPointF(float(global_x), 300.0),
                       bouton, boutons, Qt.KeyboardModifier.NoModifier)


# ── la fenêtre sous test ─────────────────────────────────────────────────────

@pytest.fixture
def lecteurs(monkeypatch):
    """Chaque `MpvWidget` construit est un double, et on les garde tous.

    Le replay en crée un SECOND — c'est tout son principe : le direct ne peut
    pas se rembobiner sans décrocher. Il faut donc pouvoir désigner l'un et
    l'autre.
    """
    crees: list[FauxLecteur] = []

    def _fabriquer(parent=None, **kw):
        lecteur = FauxLecteur(parent, **kw)
        crees.append(lecteur)
        return lecteur

    monkeypatch.setattr(fullscreen, "MpvWidget", _fabriquer)
    return crees


@pytest.fixture
def differes(monkeypatch):
    """Retient les rappels de `QTimer.singleShot` au lieu de les programmer.

    Le retour d'un clip remet le libellé du bouton trois secondes plus tard.
    Laisser ce minuteur courir le ferait tirer bien après la fin du test, sur
    un bouton que Qt a déjà détruit — et pytest-qt impute alors l'exception au
    test SUIVANT, qui n'y est pour rien.

    Seule la forme statique est neutralisée : `QTimer(self)`, dont le module
    se sert pour l'effacement des annonces, continue de fonctionner.
    """
    rappels: list[tuple[int, object]] = []

    class _SansDiffere(fullscreen.QTimer):
        @staticmethod
        def singleShot(ms, rappel):  # type: ignore[override]
            rappels.append((ms, rappel))

    monkeypatch.setattr(fullscreen, "QTimer", _SansDiffere)
    return rappels


@pytest.fixture
def fenetre(qtbot, qapp, lecteurs, differes):
    """Une FullscreenWindow réelle, en 1920×1080, jamais montrée à l'écran.

    `show_on_init=False` : l'afficher la poserait sur un moniteur, et aucun
    test n'y gagne. La zone centrale est dimensionnée à la main parce que la
    disposition de QMainWindow ne s'active qu'au premier affichage, et que
    toutes les géométries en dépendent.
    """
    win = fullscreen.FullscreenWindow(
        qapp.primaryScreen(), show_on_init=False,
        clip_config={"duration_secs": 45, "directory": ""},
    )
    qtbot.addWidget(win)
    win.resize(1920, 1080)
    win.centralWidget().resize(1920, 1080)
    win._current_login = "zerator"
    yield win
    win.hide()


@pytest.fixture
def horloge(monkeypatch):
    """Fige `time` dans le module plein écran, et lui seul."""
    faux = _Horloge()
    monkeypatch.setattr(fullscreen, "time", faux)
    return faux


def _lancer_replay(fenetre, horloge, chemin, secs=30, taille=4096) -> None:
    """Amène la fenêtre en replay actif, fichier réputé complet et stable.

    On court-circuite l'attente du fichier : ce qu'elle vérifie a ses propres
    tests, et la refaire ici ne ferait que rendre les autres illisibles.
    """
    fenetre._replay_login = "zerator"
    fenetre._replay_secs = secs
    fenetre._engager_replay(chemin)
    fenetre._replay_size = taille
    fenetre._replay_wait.stop()
    fenetre._begin_replay()


# ── start_replay : ne jamais redemander deux fois le même tampon ─────────────

def test_une_demande_de_replay_montre_le_bandeau_d_attente_sans_attendre(fenetre):
    """Le chemin le plus court passe encore par une attente de fichier.

    Sans retour immédiat, le clic sur « Revoir les dernières secondes » ne
    produisait rien de visible et on le refaisait en croyant l'avoir manqué.
    """
    fenetre.start_replay("zerator", "/tmp/tampon.ts", 30, tampon_optimal=True)
    assert isinstance(fenetre._replay_loader, fullscreen._ReplayLoader)
    # `isHidden` plutôt que `isVisible` : la fenêtre porteuse n'est pas
    # affichée dans un test, et Qt déclare invisible tout enfant d'une
    # fenêtre cachée. Ce qu'on vérifie ici, c'est que `show()` a bien été
    # appelé sur le bandeau.
    assert not fenetre._replay_loader.isHidden()


def test_un_tampon_deja_optimal_n_est_pas_redemande_au_lecteur(fenetre, lecteurs):
    """LE bug des vingt-huit secondes.

    Quand le chemin sort déjà du plein écran, redemander un `dump-cache` au
    même lecteur dans la même seconde produisait un fichier au nom identique —
    nommé à la seconde près. La comparaison de chemins concluait « rien de
    mieux », et le replay partait chercher chez Twitch les vingt-huit secondes
    que la plateforme garde en ligne, alors que soixante étaient sur le disque.
    """
    direct = lecteurs[0]
    fenetre.start_replay("zerator", "/tmp/deja_best.ts", 60, tampon_optimal=True)
    assert direct.dumps == [], "le tampon fourni était déjà le bon"
    assert fenetre._replay_path == "/tmp/deja_best.ts"


def test_un_tampon_optimal_ne_passe_pas_par_twitch(fenetre, monkeypatch):
    """Corollaire du même bug : rien à retélécharger, donc aucun fil réseau."""
    reprises: list[tuple] = []
    monkeypatch.setattr(fenetre, "_reprendre_chez_twitch",
                        lambda *a: reprises.append(a))
    fenetre.start_replay("zerator", "/tmp/deja_best.ts", 60, tampon_optimal=True)
    assert reprises == []


def test_sans_indication_le_tampon_du_plein_ecran_est_prefere(fenetre, lecteurs):
    """La grille joue en 360p, le plein écran en `best` : pour la chaîne
    affichée, son tampon est disponible ET meilleur."""
    direct = lecteurs[0]
    direct.rendu = "/tmp/best_du_direct.ts"
    fenetre.start_replay("zerator", "/tmp/grille_360p.ts", 30)
    assert fenetre._replay_path == "/tmp/best_du_direct.ts"
    assert direct.dumps, "le tampon du plein écran devait être sollicité"


def test_un_tampon_de_grille_seul_declenche_une_reprise_chez_twitch(
        fenetre, monkeypatch):
    """Autre chaîne que celle affichée : le seul tampon local est le 360p de
    la cellule, et on préfère retélécharger le moment en pleine qualité."""
    reprises: list[tuple] = []
    monkeypatch.setattr(fenetre, "_reprendre_chez_twitch",
                        lambda *a: reprises.append(a))
    fenetre.start_replay("domingo", "/tmp/grille_360p.ts", 30)
    assert reprises == [("domingo", "/tmp/grille_360p.ts", 30)]


def test_un_replay_deja_lance_ignore_une_seconde_demande(fenetre):
    """Deux lecteurs de replay superposés se disputeraient l'écran ET le son."""
    fenetre._replay_active = True
    fenetre.start_replay("zerator", "/tmp/autre.ts", 30, tampon_optimal=True)
    assert fenetre._replay_loader is None
    assert fenetre._replay_path == ""


# ── _replay_current : le geste « R » et la touche du Stream Deck ─────────────

def test_le_replay_du_direct_declare_son_tampon_comme_optimal(fenetre, lecteurs,
                                                              monkeypatch):
    """La moitié manquante du bug des vingt-huit secondes.

    `_replay_current` vient d'obtenir le tampon DU PLEIN ÉCRAN : il n'y a rien
    de mieux à chercher. Omettre le drapeau relançait un second `dump-cache`
    sur le même fichier, et le replay finissait chez Twitch.
    """
    lecteurs[0].rendu = "/tmp/frais.ts"
    vus: list[dict] = []
    monkeypatch.setattr(fenetre, "start_replay",
                        lambda *a, **k: vus.append({"args": a, "kw": k}))
    fenetre._replay_current()
    assert vus and vus[0]["kw"].get("tampon_optimal") is True


def test_le_replay_du_direct_reprend_la_duree_des_clips(fenetre, lecteurs,
                                                        monkeypatch):
    """Le tampon local, lui, N'EST PAS plafonné par Twitch : sa taille est
    celle qu'on a réservée pour les clips, et c'est cette durée-là qu'on
    demande — pas les trente secondes du chemin réseau."""
    lecteurs[0].rendu = "/tmp/frais.ts"
    vus: list[tuple] = []
    monkeypatch.setattr(fenetre, "start_replay", lambda *a, **k: vus.append(a))
    fenetre._replay_current()
    assert lecteurs[0].dumps[0][0] == 45, "la durée configurée pour les clips"
    assert vus[0][2] == 45


def test_sans_chaine_affichee_le_replay_ne_fait_rien(fenetre, lecteurs):
    """L'état vide n'a pas de tampon : demander un replay n'a pas de sens."""
    fenetre._current_login = ""
    fenetre._replay_current()
    assert lecteurs[0].dumps == []
    assert fenetre._replay_loader is None


def test_un_tampon_vide_ne_lance_aucun_replay(fenetre, lecteurs):
    """Lecture qui vient de démarrer : le cache n'a encore rien à rendre."""
    lecteurs[0].rendu = None
    fenetre._replay_current()
    assert fenetre._replay_loader is None
    assert fenetre._replay_active is False


def test_r_pendant_un_replay_le_referme(fenetre, horloge):
    """La même touche ouvre et ferme : c'est une bascule, pas un empilement."""
    _lancer_replay(fenetre, horloge, "/tmp/x.ts")
    assert fenetre._replay_active is True
    fenetre._replay_current()
    assert fenetre._replay_active is False


# ── attente du fichier : dump-cache écrit en arrière-plan ────────────────────

def test_la_lecture_attend_que_le_fichier_cesse_de_grossir(fenetre, horloge,
                                                           tmp_path):
    """mpv ne signale pas la fin de son `dump-cache` : lire trop tôt donnait
    un tronçon, parfois une seule image."""
    cible = tmp_path / "dump.ts"
    cible.write_bytes(b"a" * 100)
    fenetre._replay_login = "zerator"
    fenetre._engager_replay(str(cible))

    fenetre._check_replay_file()            # première mesure : 100 octets
    assert fenetre._replay_active is False, "une seule mesure ne prouve rien"

    cible.write_bytes(b"a" * 400)           # ça grossit encore
    fenetre._check_replay_file()
    assert fenetre._replay_active is False

    fenetre._check_replay_file()            # deux mesures identiques : c'est fini
    assert fenetre._replay_active is True


def test_un_fichier_absent_puis_stable_est_finalement_joue(fenetre, horloge,
                                                           tmp_path):
    """Le dump peut mettre un instant à créer le fichier : zéro octet n'est
    pas une taille stable, c'est une absence."""
    cible = tmp_path / "tard.ts"
    fenetre._replay_login = "zerator"
    fenetre._engager_replay(str(cible))
    fenetre._check_replay_file()
    fenetre._check_replay_file()
    assert fenetre._replay_active is False, "0 == 0 ne doit pas lancer la lecture"
    cible.write_bytes(b"a" * 64)
    fenetre._check_replay_file()
    fenetre._check_replay_file()
    assert fenetre._replay_active is True


def test_un_fichier_partiel_est_joue_plutot_que_rien_a_l_echeance(fenetre,
                                                                  horloge,
                                                                  tmp_path):
    """Six secondes d'attente : au-delà, un replay tronqué vaut mieux que
    l'anneau qui tourne indéfiniment."""
    cible = tmp_path / "partiel.ts"
    cible.write_bytes(b"a" * 10)
    fenetre._replay_login = "zerator"
    fenetre._montrer_chargeur("zerator", 30)
    fenetre._engager_replay(str(cible))
    horloge.avancer(fullscreen._REPLAY_DUMP_TIMEOUT_S + 1)
    fenetre._check_replay_file()
    assert fenetre._replay_active is True
    assert fenetre._replay_loader is None


def test_aucun_fichier_a_l_echeance_retire_le_bandeau(fenetre, horloge,
                                                      tmp_path):
    """Laisser l'anneau tourner sur un replay qui n'arrivera jamais serait
    pire que de n'avoir rien montré."""
    fenetre._replay_login = "zerator"
    fenetre._montrer_chargeur("zerator", 30)
    fenetre._engager_replay(str(tmp_path / "jamais.ts"))
    horloge.avancer(fullscreen._REPLAY_DUMP_TIMEOUT_S + 1)
    fenetre._check_replay_file()
    assert fenetre._replay_active is False
    assert fenetre._replay_loader is None
    assert fenetre._replay_path == "", "le temporaire est oublié"


def test_renoncer_pendant_l_attente_arrete_la_surveillance(fenetre, horloge,
                                                           tmp_path):
    """Le minuteur laissé en marche rouvrirait le replay abandonné."""
    cible = tmp_path / "dump.ts"
    cible.write_bytes(b"a" * 100)
    fenetre._montrer_chargeur("zerator", 30)
    fenetre._engager_replay(str(cible))
    fenetre._annuler_replay()
    assert fenetre._replay_wait.isActive() is False
    fenetre._check_replay_file()
    fenetre._check_replay_file()
    assert fenetre._replay_active is False


def test_un_chemin_vide_n_arme_aucune_attente(fenetre, horloge):
    """`recuperer` peut ne rien rendre : surveiller la chaîne vide ferait
    tourner un minuteur jusqu'à l'échéance pour rien."""
    fenetre._engager_replay("")
    assert getattr(fenetre, "_replay_wait", None) is None


# ── la reprise chez Twitch, sur un fil séparé ────────────────────────────────

def test_la_reprise_reseau_rend_son_verdict_sur_le_fil_graphique(fenetre, qtbot,
                                                                 monkeypatch):
    """Quelques secondes de réseau sur le fil graphique gèleraient la vidéo.

    Le verdict revient par signal, donc sur le fil Qt : c'est ce qui autorise
    `_sur_replay_hd` à toucher aux widgets.
    """
    import core.replay_hd
    monkeypatch.setattr(core.replay_hd, "recuperer",
                        lambda login, secs: ("/tmp/hd.mp4", 28.0))
    with qtbot.waitSignal(fenetre._replay_hd_pret, timeout=5000) as verdict:
        fenetre._reprendre_chez_twitch("zerator", "/tmp/repli.ts", 30)
    assert verdict.args == ["/tmp/hd.mp4", 28.0]


def test_une_reprise_qui_echoue_retombe_sur_le_tampon_de_la_grille(
        fenetre, qtbot, monkeypatch):
    """Un direct terminé ou une panne réseau ne doivent pas laisser
    l'utilisateur sans rien : le 360p de la cellule reste jouable."""
    import core.replay_hd

    def _casse(login, secs):
        raise OSError("réseau coupé")

    monkeypatch.setattr(core.replay_hd, "recuperer", _casse)
    with qtbot.waitSignal(fenetre._replay_hd_pret, timeout=5000) as verdict:
        fenetre._reprendre_chez_twitch("zerator", "/tmp/repli.ts", 30)
    assert verdict.args == ["/tmp/repli.ts", 30.0]


# ── ouverture et fermeture du replay ─────────────────────────────────────────

def test_le_direct_se_tait_pendant_le_replay(fenetre, lecteurs, horloge):
    """Deux sources simultanées ne s'écoutent pas, et c'est l'action rejouée
    qu'on veut entendre."""
    _lancer_replay(fenetre, horloge, "/tmp/x.ts")
    assert lecteurs[0].muets[-1] is True


def test_fermer_le_replay_rend_le_son_au_direct(fenetre, lecteurs, horloge):
    """Sans cette restitution, la régie se retrouvait muette sans savoir
    pourquoi — le curseur de volume, lui, n'avait pas bougé."""
    fenetre._muted = False
    fenetre._volume = 70
    _lancer_replay(fenetre, horloge, "/tmp/x.ts")
    fenetre.stop_replay()
    assert lecteurs[0].muets[-1] is False
    assert lecteurs[0].volumes[-1] == 70


def test_un_direct_coupe_avant_le_replay_le_reste_apres(fenetre, lecteurs,
                                                        horloge):
    """Le replay ne doit pas servir de bouton « rétablir le son » caché."""
    fenetre._muted = True
    _lancer_replay(fenetre, horloge, "/tmp/x.ts")
    fenetre.stop_replay()
    assert lecteurs[0].muets[-1] is True


def test_le_replay_pose_son_badge_sa_barre_et_retire_le_bandeau(fenetre,
                                                                horloge):
    """Le bandeau d'attente et le badge occupent la MÊME place : les laisser
    coexister superposerait deux textes contradictoires."""
    fenetre._montrer_chargeur("zerator", 30)
    _lancer_replay(fenetre, horloge, "/tmp/x.ts")
    assert fenetre._replay_loader is None
    assert isinstance(fenetre._replay_badge, fullscreen._ReplayBadge)
    assert isinstance(fenetre._replay_progress, fullscreen._ReplayProgress)
    assert not fenetre._replay_badge.isHidden()


def test_le_replay_joue_le_fichier_obtenu_dans_un_second_lecteur(fenetre,
                                                                 lecteurs,
                                                                 horloge):
    """Le direct ne peut pas se rembobiner lui-même : reculer dans son cache
    le mettrait en pause et ferait décrocher le flux."""
    _lancer_replay(fenetre, horloge, "/tmp/moment.ts")
    assert lecteurs[-1].lus == ["/tmp/moment.ts"]
    assert lecteurs[-1] is not lecteurs[0]
    assert lecteurs[0].lus == [], "le direct n'a rien rejoué"


def test_la_fin_de_la_lecture_referme_le_replay_toute_seule(fenetre, lecteurs,
                                                            horloge):
    """Rester sur la dernière image d'un replay terminé donnait l'impression
    d'un direct figé."""
    _lancer_replay(fenetre, horloge, "/tmp/x.ts")
    lecteurs[-1].playback_ended.emit()
    assert fenetre._replay_active is False


def test_le_lecteur_de_replay_est_bien_arrete_a_la_fermeture(fenetre, lecteurs,
                                                             horloge):
    """Un libmpv laissé vivant garde une session de décodage GPU : vingt-cinq
    cellules plus le direct, le budget VCN n'a pas de marge pour un oublié."""
    _lancer_replay(fenetre, horloge, "/tmp/x.ts")
    rejoueur = lecteurs[-1]
    fenetre.stop_replay()
    assert rejoueur.terminaisons == 1
    assert fenetre._replay_player is None


def test_le_fichier_temporaire_disparait_a_la_fermeture(fenetre, horloge,
                                                        tmp_path):
    """Un replay de soixante secondes en `best` pèse plusieurs dizaines de
    mégaoctets : les accumuler remplirait le disque en une soirée."""
    cible = tmp_path / "moment.ts"
    cible.write_bytes(b"a" * 32)
    _lancer_replay(fenetre, horloge, str(cible))
    fenetre.stop_replay()
    assert not cible.exists()


def test_un_temporaire_deja_disparu_ne_fait_pas_echouer_la_fermeture(
        fenetre, horloge, tmp_path):
    """Le nettoyeur de %TEMP% de Windows passe aussi, et parfois pendant le
    replay."""
    cible = tmp_path / "volatil.ts"
    cible.write_bytes(b"a")
    _lancer_replay(fenetre, horloge, str(cible))
    cible.unlink()
    fenetre.stop_replay()
    assert fenetre._replay_active is False


def test_fermer_un_replay_inexistant_ne_fait_rien(fenetre, lecteurs):
    """Échap arrive de partout, y compris quand il n'y a pas de replay."""
    fenetre.stop_replay()
    assert lecteurs[0].muets == [], "le direct n'a pas été touché"


def test_le_suivi_de_progression_s_arrete_avec_le_replay(fenetre, horloge):
    """Un minuteur à 200 ms laissé en marche interroge un lecteur détruit."""
    _lancer_replay(fenetre, horloge, "/tmp/x.ts")
    suivi = fenetre._replay_suivi
    fenetre.stop_replay()
    assert fenetre._replay_suivi is None
    assert suivi.isActive() is False


def test_ouvrir_puis_fermer_un_replay_ne_laisse_aucune_fenetre_sur_le_bureau(
        fenetre, horloge, qapp):
    """Le replay détache quatre widgets : lecteur, badge, barre, bandeau.

    Détaché ALORS QU'IL EST ENCORE VISIBLE, chacun devient une fenêtre de
    premier niveau posée sur le bureau. C'est le bug qui avait fait surgir
    1 340 fenêtres en dix rafraîchissements côté panel.
    """
    connues = list(QApplication.instance().topLevelWidgets())
    for _ in range(3):
        fenetre._montrer_chargeur("zerator", 30)
        _lancer_replay(fenetre, horloge, "/tmp/x.ts")
        fenetre.annoncer("Clip sauvegardé · 45 s")
        fenetre.stop_replay()
        qapp.processEvents()
    fenetre._retirer_annonce()
    qapp.processEvents()
    assert _flottantes(connues) == []


# ── la barre de progression : la durée OBTENUE, pas la demandée ─────────────

def test_la_barre_se_rapporte_a_la_duree_reellement_obtenue(fenetre, lecteurs,
                                                            horloge):
    """LE bug de la barre à mi-course.

    On demande soixante secondes, Twitch n'en garde que vingt-huit. Rapportée
    à la durée DEMANDÉE, la barre plafonnait sous la moitié et le replay se
    terminait sur une jauge à demi pleine — indiscernable d'une lecture
    interrompue.
    """
    _lancer_replay(fenetre, horloge, "/tmp/x.ts", secs=60)
    rejoueur = lecteurs[-1]
    rejoueur._position, rejoueur._restant = 0.0, 28.0
    fenetre._suivre_progression()               # première image : on mesure
    assert fenetre._replay_secs == 28

    rejoueur._position, rejoueur._restant = 14.0, 14.0
    fenetre._suivre_progression()
    assert fenetre._replay_progress._ratio == pytest.approx(0.5, abs=0.01)


def test_la_barre_atteint_le_bout_a_la_derniere_image(fenetre, lecteurs,
                                                      horloge):
    """Une jauge qui s'arrête à 47 % raconte une panne qui n'a pas eu lieu."""
    _lancer_replay(fenetre, horloge, "/tmp/x.ts", secs=60)
    rejoueur = lecteurs[-1]
    rejoueur._position, rejoueur._restant = 0.0, 28.0
    fenetre._suivre_progression()
    rejoueur._position, rejoueur._restant = 28.0, 0.0
    fenetre._suivre_progression()
    assert fenetre._replay_progress._ratio == pytest.approx(1.0, abs=0.01)


def test_la_progression_part_de_la_premiere_image_lue(fenetre, lecteurs,
                                                      horloge):
    """Un MP4 fragmenté repris chez Twitch démarre à l'horodatage du DIRECT,
    soit des heures. Rapporter la position brute à la durée mettrait la barre
    au bout dès la première image."""
    _lancer_replay(fenetre, horloge, "/tmp/x.ts", secs=30)
    rejoueur = lecteurs[-1]
    rejoueur._position, rejoueur._restant = 42_000.0, 30.0
    fenetre._suivre_progression()
    assert fenetre._replay_origine == 42_000.0
    assert fenetre._replay_progress._ratio == pytest.approx(0.0, abs=0.01)

    rejoueur._position = 42_015.0
    fenetre._suivre_progression()
    assert fenetre._replay_progress._ratio == pytest.approx(0.5, abs=0.02)


def test_le_badge_annonce_la_duree_corrigee_des_la_premiere_image(fenetre,
                                                                  lecteurs,
                                                                  horloge):
    """« 60 dernières secondes » sur vingt-huit secondes de vidéo : le bandeau
    mentait, et c'est le seul texte que l'utilisateur peut lire."""
    _lancer_replay(fenetre, horloge, "/tmp/x.ts", secs=60)
    assert "60 dernières secondes" in fenetre._replay_badge._who.text()
    rejoueur = lecteurs[-1]
    rejoueur._position, rejoueur._restant = 0.0, 28.0
    fenetre._suivre_progression()
    assert "28 dernières secondes" in fenetre._replay_badge._who.text()


def test_sans_position_connue_la_barre_ne_bouge_pas(fenetre, lecteurs,
                                                    horloge):
    """mpv ne rend `time-pos` qu'une fois le fichier ouvert : quelques ticks
    passent à vide, et l'origine ne doit surtout pas être fixée à zéro."""
    _lancer_replay(fenetre, horloge, "/tmp/x.ts")
    lecteurs[-1]._position = None
    fenetre._suivre_progression()
    assert fenetre._replay_origine is None
    assert fenetre._replay_progress._ratio == 0.0


def test_le_suivi_apres_fermeture_ne_leve_pas(fenetre, horloge):
    """Un tick de 200 ms peut être en vol quand le replay se ferme."""
    _lancer_replay(fenetre, horloge, "/tmp/x.ts")
    fenetre.stop_replay()
    fenetre._suivre_progression()


def test_l_origine_est_remise_a_zero_entre_deux_replays(fenetre, lecteurs,
                                                        horloge):
    """Garder l'origine du précédent ferait démarrer la barre au milieu, ou
    dans les négatifs si le second fichier commence plus tôt."""
    _lancer_replay(fenetre, horloge, "/tmp/un.ts")
    lecteurs[-1]._position, lecteurs[-1]._restant = 42_000.0, 30.0
    fenetre._suivre_progression()
    fenetre.stop_replay()
    assert fenetre._replay_origine is None


# ── clip : un geste sans retour visible est un geste raté ────────────────────

def test_un_clip_enregistre_s_annonce_sur_la_video(fenetre, lecteurs):
    """LE bug du clip invisible.

    Le seul retour tenait dans le libellé du bouton de la barre d'outils —
    laquelle s'efface après deux secondes d'immobilité, et n'existe pas du
    tout quand le geste vient du clavier ou du Stream Deck. On appuyait sans
    jamais savoir si quelque chose avait été enregistré.
    """
    lecteurs[0].rendu = "/tmp/clip.mp4"
    fenetre._save_clip()
    assert isinstance(fenetre._annonce, fullscreen._Annonce)
    assert not fenetre._annonce.isHidden()
    textes = " ".join(w.text()
                      for w in fenetre._annonce.findChildren(fullscreen.QLabel))
    assert "45" in textes, "la durée réellement demandée au tampon"


def test_un_clip_impossible_le_dit_aussi(fenetre, lecteurs):
    """Un échec silencieux est pire qu'un échec : on refait le geste."""
    lecteurs[0].rendu = None
    fenetre._save_clip()
    textes = " ".join(w.text()
                      for w in fenetre._annonce.findChildren(fullscreen.QLabel))
    assert "impossible" in textes.lower()


def test_le_clip_utilise_la_duree_et_le_dossier_configures(fenetre, lecteurs,
                                                           tmp_path):
    """Ce réglage vient de la fenêtre de configuration : l'ignorer écrirait
    les clips dans %TEMP%, où le nettoyeur de Windows les efface."""
    fenetre.set_clip_config({"clips": {"duration_secs": 90,
                                       "directory": str(tmp_path)}})
    fenetre._save_clip()
    assert lecteurs[0].dumps == [(90, str(tmp_path))]


def test_le_libelle_du_bouton_de_clip_ne_reintroduit_pas_de_pictogramme(
        fenetre, lecteurs):
    """U+23FA s'affichait en carré bleu faute de police d'emoji, EN PLUS de
    l'icône qtawesome que le bouton porte déjà.

    Le libellé rendu APRÈS le retour transitoire est le piège : c'est lui
    qu'on relit trois secondes plus tard, et il restait à l'écran.
    """
    lecteurs[0].rendu = "/tmp/clip.mp4"
    fenetre._save_clip()
    assert "sauvé" in fenetre._clip_btn.text()

    # Le minuteur est désormais PARENTÉ à la fenêtre — il meurt avec elle au
    # lieu de tirer sur un bouton détruit — et gardé sur l'instance, donc
    # déclenchable sans attendre trois secondes pour de vrai.
    assert fenetre._clip_timer.interval() == 3000
    fenetre._clip_timer.timeout.emit()
    assert fenetre._clip_btn.text() == "Clip"
    assert "⏺" not in fenetre._clip_btn.text()


def test_deux_clips_coup_sur_coup_laissent_voir_le_second(fenetre):
    """Un minuteur par annonce, empilé, faisait disparaître la seconde au bout
    du délai de la PREMIÈRE — parfois immédiatement."""
    fenetre.annoncer("premier", secondes=3.0)
    premier = fenetre._annonce_timer
    fenetre.annoncer("second", secondes=3.0)
    assert fenetre._annonce_timer is not premier
    assert premier.isActive() is False
    textes = " ".join(w.text()
                      for w in fenetre._annonce.findChildren(fullscreen.QLabel))
    assert textes == "second"


def test_l_annonce_s_efface_d_elle_meme(fenetre, qtbot):
    """Un bandeau permanent finirait par masquer l'action qu'il commente."""
    fenetre.annoncer("Clip sauvegardé · 45 s", secondes=0.05)
    qtbot.waitUntil(lambda: fenetre._annonce is None, timeout=2000)


def test_l_annonce_descend_sous_le_badge_pendant_un_replay(fenetre, horloge):
    """Les deux visent le même haut d'écran : superposés, aucun des deux ne se
    lit."""
    fenetre.annoncer("Clip sauvegardé")
    haut_seul = fenetre._annonce.pos().y()
    _lancer_replay(fenetre, horloge, "/tmp/x.ts")
    fenetre._placer_annonce()
    assert fenetre._annonce.pos().y() > haut_seul


def test_l_annonce_est_centree_sur_la_zone_video(fenetre):
    """Le chat, quand il est ouvert, ne rejoue pas : le bandeau appartient à
    la vidéo, pas à la fenêtre."""
    fenetre.annoncer("Clip sauvegardé")
    fenetre._chat_panel._visible = True
    fenetre._chat_panel._width = 400
    fenetre._placer_annonce()
    attendu = (1920 - 400 - fenetre._annonce.width()) // 2
    assert fenetre._annonce.pos().x() == attendu


def test_retirer_une_annonce_absente_est_sans_effet(fenetre):
    """Le minuteur et une nouvelle annonce peuvent arriver dans n'importe quel
    ordre."""
    fenetre._retirer_annonce()
    fenetre._retirer_annonce()
    assert fenetre._annonce is None


# ── vue de don incrustée ─────────────────────────────────────────────────────

def test_le_don_part_dans_le_navigateur_et_non_dans_la_vue_integree(
        fenetre, monkeypatch):
    """La vue intégrée n'a pas de barre d'adresse, et l'utilisateur va y saisir
    des coordonnées de paiement : il doit voir l'URL réelle et son cadenas.

    Le bouton du plein écran avait sa PROPRE copie du contrôle d'allowlist ;
    elle n'a pas suivi quand on a corrigé celui du panel.
    """
    ouvertes: list[str] = []
    monkeypatch.setattr(fullscreen, "ouvrir_page_de_don",
                        lambda url: bool(ouvertes.append(url)) or True)
    fenetre._current_donation_url = "https://zevent.fr/dons/zerator"
    fenetre._open_donate_view()
    assert ouvertes == ["https://zevent.fr/dons/zerator"]
    assert fenetre._pip_active is False, "rien ne s'ouvre DANS la fenêtre"


def test_fermer_la_page_de_don_rend_tout_l_ecran_au_direct(fenetre):
    """La page de don laissait le direct en timbre-poste dans un coin après sa
    fermeture, faute de recalcul."""
    fenetre._pip_active = True
    fenetre._donate_view.show()
    fenetre._donate_close_btn.show()
    fenetre._close_donate_view()
    assert fenetre._pip_active is False
    assert not fenetre._donate_view.isVisible()
    assert not fenetre._donate_close_btn.isVisible()
    assert fenetre._stack.geometry().width() == 1920


def test_la_page_de_don_reduit_le_direct_en_incrustation(fenetre):
    """On veut garder un œil sur le direct pendant qu'on donne."""
    fenetre._pip_active = True
    fenetre._update_mpv_geometry()
    incrustation = fenetre._stack.geometry()
    assert (incrustation.width(), incrustation.height()) == (320, 180)
    assert fenetre._donate_view.geometry().width() == 1920


def test_changer_de_chaine_referme_la_page_de_don(fenetre):
    """La page ouverte appartenait au streamer précédent : la garder enverrait
    le don sur la mauvaise cagnotte."""
    fenetre._pip_active = True
    fenetre.set_stream("domingo", "Minecraft", 1000, "https://zevent.fr/d")
    assert fenetre._pip_active is False


# ── panneau de chat ──────────────────────────────────────────────────────────

def test_le_chat_est_ferme_au_demarrage(fenetre):
    """Le plein écran sert d'abord à voir l'image."""
    assert fenetre.chat_ouvert is False
    assert not fenetre._chat_panel.isVisible()


def test_basculer_le_chat_alterne_son_etat(fenetre):
    fenetre._toggle_chat()
    assert fenetre.chat_ouvert is True
    fenetre._toggle_chat()
    assert fenetre.chat_ouvert is False


def test_ouvrir_le_chat_retrecit_la_zone_video(fenetre):
    """Sans recalcul, l'image restait sous le panneau et le chat masquait
    l'action."""
    fenetre._toggle_chat()
    assert fenetre._stack.geometry().width() == 1920 - fenetre._chat_panel._width


def test_le_bouton_de_chat_suit_l_etat_du_panneau(fenetre):
    """Le raccourci clavier et le bouton commandent la même chose : le bouton
    doit refléter ce que la touche a fait."""
    fenetre._toggle_chat()
    assert fenetre._chat_btn.isChecked() is True
    fenetre._toggle_chat()
    assert fenetre._chat_btn.isChecked() is False


def test_le_chat_occupe_le_bord_droit_sur_toute_la_hauteur(fenetre):
    """Un panneau flottant au milieu de l'image serait pire que pas de chat."""
    fenetre._chat_panel.show_chat()
    geo = fenetre._chat_panel.geometry()
    largeur = fenetre._chat_panel._width
    assert (geo.x(), geo.y()) == (1920 - largeur, 0)
    assert (geo.width(), geo.height()) == (largeur, 1080)


def test_le_chat_ferme_ne_charge_aucune_page(fenetre):
    """Un chat Twitch invisible consommerait du réseau et du GPU pour rien."""
    espion = _WebEspion()
    fenetre._chat_panel._web = espion
    fenetre._chat_panel.set_stream("zerator")
    assert espion.urls == []


@pytest.mark.skipif(not fullscreen._WEBENGINE_OK,
                    reason="sans PyQt6-WebEngine le panneau est un texte de repli")
def test_ouvrir_le_chat_charge_la_chaine_affichee(fenetre):
    """Le chat suit le direct : l'ouvrir sur la chaîne précédente serait pire
    que de ne rien ouvrir."""
    espion = _WebEspion()
    fenetre._chat_panel._web = espion
    fenetre._chat_panel._login = "zerator"
    fenetre._chat_panel.show_chat()
    assert len(espion.urls) == 1
    assert "twitch.tv/embed/zerator/chat" in espion.urls[0]


@pytest.mark.skipif(not fullscreen._WEBENGINE_OK,
                    reason="sans PyQt6-WebEngine le panneau est un texte de repli")
def test_changer_de_chaine_recharge_le_chat_ouvert(fenetre):
    """Le chat de la chaîne précédente resterait affiché sous la nouvelle
    vidéo, et les messages n'auraient plus aucun rapport avec l'image."""
    espion = _WebEspion()
    fenetre._chat_panel._web = espion
    fenetre._chat_panel.show_chat()
    espion.urls.clear()
    fenetre.set_stream("domingo")
    assert espion.urls and "domingo" in espion.urls[0]


def test_glisser_la_poignee_vers_la_gauche_elargit_le_chat(fenetre):
    """La poignée fait quatre pixels : sans elle, la largeur du chat n'est
    réglable nulle part."""
    chat = fenetre._chat_panel
    chat.show_chat()
    depart = chat._width
    chat.eventFilter(chat._handle, _clic(QEvent.Type.MouseButtonPress, 800,
                                         Qt.MouseButton.LeftButton,
                                         Qt.MouseButton.LeftButton))
    chat.eventFilter(chat._handle, _clic(QEvent.Type.MouseMove, 700,
                                         Qt.MouseButton.NoButton,
                                         Qt.MouseButton.LeftButton))
    assert chat._width == depart + 100


def test_la_largeur_du_chat_reste_entre_ses_bornes(fenetre):
    """Sous 250 px le chat devient illisible ; au-delà de 600 il mange l'image,
    qui est la raison d'être de cette fenêtre."""
    chat = fenetre._chat_panel
    chat.show_chat()
    chat.eventFilter(chat._handle, _clic(QEvent.Type.MouseButtonPress, 800,
                                         Qt.MouseButton.LeftButton,
                                         Qt.MouseButton.LeftButton))
    chat.eventFilter(chat._handle, _clic(QEvent.Type.MouseMove, -5000,
                                         Qt.MouseButton.NoButton,
                                         Qt.MouseButton.LeftButton))
    assert chat._width == 600
    chat.eventFilter(chat._handle, _clic(QEvent.Type.MouseMove, 5000,
                                         Qt.MouseButton.NoButton,
                                         Qt.MouseButton.LeftButton))
    assert chat._width == 250


def test_elargir_le_chat_recale_la_video_dans_la_foulee(fenetre):
    """Sans ça, l'image gardait l'ancienne largeur jusqu'au prochain
    redimensionnement de la fenêtre — donc, en plein écran, jamais."""
    chat = fenetre._chat_panel
    chat.show_chat()
    chat.eventFilter(chat._handle, _clic(QEvent.Type.MouseButtonPress, 800,
                                         Qt.MouseButton.LeftButton,
                                         Qt.MouseButton.LeftButton))
    chat.eventFilter(chat._handle, _clic(QEvent.Type.MouseMove, 700,
                                         Qt.MouseButton.NoButton,
                                         Qt.MouseButton.LeftButton))
    assert fenetre._stack.geometry().width() == 1920 - chat._width


def test_un_survol_de_la_poignee_ne_redimensionne_rien(fenetre):
    """Passer la souris au-dessus sans cliquer arrive tout le temps : la
    poignée borde la zone où l'on lit les messages."""
    chat = fenetre._chat_panel
    chat.show_chat()
    depart = chat._width
    chat.eventFilter(chat._handle, _clic(QEvent.Type.MouseMove, 400,
                                         Qt.MouseButton.NoButton,
                                         Qt.MouseButton.NoButton))
    assert chat._width == depart


def test_un_evenement_venu_d_ailleurs_traverse_le_filtre(fenetre):
    """Le filtre est posé sur la poignée seule ; tout le reste doit suivre son
    chemin normal, à commencer par les clics dans le chat lui-même."""
    chat = fenetre._chat_panel
    depart = chat._width
    chat.eventFilter(chat._web, _clic(QEvent.Type.MouseButtonPress, 800,
                                      Qt.MouseButton.LeftButton,
                                      Qt.MouseButton.LeftButton))
    assert chat._width == depart


def test_le_chat_conserve_sa_largeur_d_une_ouverture_a_l_autre(fenetre):
    """Le réglage est un geste manuel : le perdre à chaque bascule le rendrait
    inutile."""
    chat = fenetre._chat_panel
    chat._width = 480
    chat.show_chat()
    chat.hide_chat()
    chat.show_chat()
    assert chat.geometry().width() == 480


# ── Échap : le replay passe avant tout le reste ──────────────────────────────

def test_echap_ferme_le_replay_avant_le_menu(fenetre, horloge):
    """C'est l'état le plus transitoire, et le plus susceptible d'être
    interrompu."""
    _lancer_replay(fenetre, horloge, "/tmp/x.ts")
    fenetre._echapper()
    assert fenetre._replay_active is False


def test_echap_renonce_a_une_reprise_pas_encore_lancee(fenetre):
    """Pendant l'attente réseau il n'y a rien à fermer, mais tout à annuler."""
    fenetre._montrer_chargeur("zerator", 30)
    fenetre._echapper()
    assert fenetre._replay_loader is None
    assert fenetre._replay_annule is True


# ── les petits widgets, jusqu'à leur peinture ────────────────────────────────

def test_l_anneau_se_dessine_sans_lever(qtbot):
    """Le rendu tourne en seizièmes de degré, avec un angle NÉGATIF pour aller
    dans le sens des aiguilles : une erreur de conversion ne se voit qu'à la
    peinture."""
    anneau = fullscreen._PetitAnneau(16)
    qtbot.addWidget(anneau)
    for _ in range(5):
        anneau._tourner()
        assert not anneau.grab().isNull()


def test_la_barre_remplit_la_part_annoncee(qtbot):
    """La peinture est la seule chose que l'utilisateur voit de cette classe :
    un `_ratio` correct mal dessiné ne vaut rien."""
    barre = fullscreen._ReplayProgress(None)
    qtbot.addWidget(barre)
    barre.resize(100, fullscreen._ReplayProgress.HAUTEUR)
    barre.set_ratio(0.5)
    image = barre.grab().toImage()
    rempli = image.pixelColor(10, 1)
    vide = image.pixelColor(90, 1)
    assert rempli != vide, "la part parcourue doit se distinguer du reste"
    assert rempli.red() > rempli.blue(), "orange ZLink"


def test_une_barre_pleine_est_uniforme(qtbot):
    """Bout de course : plus rien ne doit rester en attente."""
    barre = fullscreen._ReplayProgress(None)
    qtbot.addWidget(barre)
    barre.resize(100, fullscreen._ReplayProgress.HAUTEUR)
    barre.set_ratio(1.0)
    image = barre.grab().toImage()
    assert image.pixelColor(10, 1) == image.pixelColor(90, 1)


def test_cliquer_le_badge_de_replay_demande_sa_fermeture(fenetre, horloge,
                                                         qtbot):
    """Échap ferme aussi, mais rien ne le disait : une sortie qu'on ne devine
    pas revient à ne pas en avoir."""
    _lancer_replay(fenetre, horloge, "/tmp/x.ts")
    qtbot.mouseClick(fenetre._replay_badge, Qt.MouseButton.LeftButton)
    assert fenetre._replay_active is False


def test_cliquer_le_bandeau_d_attente_renonce_a_la_reprise(fenetre, qtbot):
    """Une attente qu'on ne peut pas interrompre est une attente qu'on subit."""
    fenetre._montrer_chargeur("zerator", 30)
    chargeur = fenetre._replay_loader
    qtbot.mouseClick(chargeur, Qt.MouseButton.LeftButton)
    assert fenetre._replay_loader is None
    assert fenetre._replay_annule is True


# ── défaut latent, verrouillé ici pour qu'il ne devienne pas visible ─────────

def test_une_origine_a_zero_est_une_vraie_origine():
    """Un `dump-cache` local commence à 0,0 s — c'est la valeur NORMALE, pas
    une absence de valeur. Les distinguer demande `is None`, comme le fait
    déjà `_suivre_progression` deux lignes plus haut."""
    ecran = types.SimpleNamespace(_replay_secs=60, _replay_origine=0.0,
                                  _replay_badge=None)
    lecteur = types.SimpleNamespace(restant=lambda: 10.0)
    fullscreen.FullscreenWindow._mesurer_duree(ecran, lecteur, 5.0)
    assert ecran._replay_secs == 15, "5 s lues + 10 s restantes depuis zéro"


# ── habillage du chat Twitch ─────────────────────────────────────────────────

class _PageEspion:
    """Retient le JavaScript soumis au lieu de l'exécuter."""

    def __init__(self) -> None:
        self.scripts: list[str] = []

    def runJavaScript(self, js: str) -> None:
        self.scripts.append(js)


class _WebAvecPage(_WebEspion):
    """Vue web factice qui expose une `page()`, comme QWebEngineView."""

    def __init__(self) -> None:
        super().__init__()
        self.page_espion = _PageEspion()

    def page(self) -> _PageEspion:
        return self.page_espion


def test_le_chat_integre_masque_la_zone_de_saisie(fenetre):
    """ZLink n'est connecté à aucun compte Twitch : la boîte de saisie invite
    à écrire un message qui ne partira jamais.

    Même chose pour la bannière RGPD, le prompt de connexion et l'en-tête —
    dans un panneau de 350 px, chacun mange une portion du seul contenu qu'on
    soit venu lire.
    """
    espion = _WebAvecPage()
    fenetre._chat_panel._web = espion
    fenetre._chat_panel._inject_chat_css()
    assert len(espion.page_espion.scripts) == 1
    css = espion.page_espion.scripts[0]
    for selecteur in ("chat-input", "consent-banner", "chat-login-overlay",
                      "chat-room__header"):
        assert selecteur in css


def test_l_habillage_du_chat_ne_s_applique_qu_une_fois_par_page(fenetre):
    """`loadFinished` se déclenche à chaque navigation interne de Twitch :
    empiler cent balises <style> identiques finirait par se voir."""
    espion = _WebAvecPage()
    fenetre._chat_panel._web = espion
    fenetre._chat_panel._inject_chat_css()
    assert "zlink-chat-css" in espion.page_espion.scripts[0], \
        "le script doit se reconnaître pour ne pas se rejouer"


def test_l_habillage_sans_moteur_web_ne_leve_pas(fenetre):
    """Sans PyQt6-WebEngine le panneau est un simple texte : il n'a pas de
    `page()`, et `loadFinished` ne l'atteindra jamais — mais l'appel direct
    doit rester inoffensif."""
    fenetre._chat_panel._web = fullscreen.QLabel("repli")
    fenetre._chat_panel._inject_chat_css()


# ── les chemins de repli, quand la fenêtre n'a plus de zone centrale ─────────

def test_un_verdict_qui_arrive_apres_le_debut_de_la_lecture_est_ignore(fenetre,
                                                                       horloge):
    """Le fil de reprise et le tampon local peuvent aboutir tous les deux.

    Le second à parler ne doit pas relancer un replay par-dessus celui qui
    joue déjà : deux lecteurs se disputeraient l'écran.
    """
    _lancer_replay(fenetre, horloge, "/tmp/local.ts")
    fenetre._sur_replay_hd("/tmp/reseau.mp4", 28.0)
    assert fenetre._replay_path == "/tmp/local.ts"


def test_un_temporaire_verrouille_ne_fait_pas_echouer_le_nettoyage(fenetre,
                                                                   monkeypatch):
    """Windows refuse de supprimer un fichier encore ouvert par un lecteur qui
    n'a pas fini de se fermer. Ce n'est pas une raison pour propager l'erreur
    jusqu'au geste de l'utilisateur."""
    def _refus(self, missing_ok=False):
        raise OSError("fichier utilisé par un autre processus")

    monkeypatch.setattr(fullscreen.pathlib.Path, "unlink", _refus)
    fenetre._replay_path = "/tmp/verrouille.ts"
    fenetre._cleanup_replay()
    assert fenetre._replay_path == "", "on l'oublie quand même"


def test_une_fermeture_a_moitie_defaite_ne_leve_pas(fenetre, horloge):
    """`stop_replay` peut trouver le lecteur ou le badge déjà détruits — une
    fermeture concurrente, un `deleteLater` déjà passé. Il doit finir son
    travail plutôt que laisser l'écran en incrustation."""
    _lancer_replay(fenetre, horloge, "/tmp/x.ts")
    fenetre._replay_player = None
    fenetre._replay_suivi = None
    fenetre._replay_badge = None
    fenetre._replay_progress = None
    fenetre.stop_replay()
    assert fenetre._replay_active is False
    assert fenetre._stack.geometry().width() == 1920, "le direct reprend l'écran"
