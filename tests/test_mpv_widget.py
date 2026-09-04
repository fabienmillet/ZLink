# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Widget MPV : options du lecteur, journal, résolution streamlink.

Aucun de ces tests n'instancie libmpv ni n'ouvre de flux. Ce qui est vérifié
ici, c'est la couche Python qui ENTOURE le lecteur : le calcul des options
passées à mpv_create(), le filtrage du journal, l'appel à streamlink et l'état
audio voulu. Le lecteur lui-même est soit absent (`_MPV_AVAILABLE` à False,
mode dégradé), soit remplacé par un double qui enregistre ce qu'on lui demande.
"""

from __future__ import annotations

import logging
import pathlib
import subprocess
import threading
import types

import pytest

from widgets import mpv_widget


# ── doubles ──────────────────────────────────────────────────────────────────

class FauxLecteur:
    """Double de `mpv.MPV` : enregistre au lieu de décoder.

    Seules les propriétés et commandes réellement utilisées par MpvWidget sont
    implémentées ; toute autre sollicitation lèverait, ce qui est le but.
    """

    def __init__(self, *, time_pos: float | None = 42.0,
                 idle_active: bool = False) -> None:
        self.time_pos = time_pos
        self.idle_active = idle_active
        self.volume: int | None = None
        self.mute: bool | None = None
        self.demuxer_max_back_bytes: int | None = None
        self.demuxer_lavf_o: str | None = None
        self.lu: list[str] = []
        self.commandes: list[tuple] = []
        self.observes: list[tuple[str, object]] = []
        self.desabonnes: list[tuple[str, object]] = []
        self.arrets = 0
        self.terminaisons = 0
        self.meta: dict | None = None

    def play(self, url: str) -> None:
        self.lu.append(url)

    def command(self, *args) -> None:
        self.commandes.append(args)

    def observe_property(self, nom: str, cb) -> None:
        self.observes.append((nom, cb))

    def unobserve_property(self, nom: str, cb) -> None:
        self.desabonnes.append((nom, cb))

    def stop(self) -> None:
        self.arrets += 1

    def terminate(self) -> None:
        self.terminaisons += 1

    def _get_property(self, nom: str):
        return self.meta


@pytest.fixture
def widget_inerte(qtbot, monkeypatch):
    """MpvWidget construit sans libmpv : aucun lecteur, aucune fenêtre native.

    C'est le seul moyen sûr de tester les méthodes d'instance : la machine de
    test a bien une libmpv, et une construction normale poserait un vrai
    lecteur sur un `winId()` offscreen.
    """
    monkeypatch.setattr(mpv_widget, "_MPV_AVAILABLE", False)
    w = mpv_widget.MpvWidget()
    qtbot.addWidget(w)
    return w


# ── plafond du tampon arrière ────────────────────────────────────────────────

@pytest.mark.parametrize("secondes,attendu", [
    (15, 6_000_000),
    (90, 36_000_000),
    (180, 72_000_000),
    # Bornes : un plafond de 2 s ne permettrait aucun clip, et un plafond de
    # 10 min ramènerait la fuite mémoire que ce calcul existe pour éviter.
    (0, 6_000_000),
    (-30, 6_000_000),
    (600, 72_000_000),
    (32.9, 12_800_000),      # int() tronque avant le calcul
])
def test_plafond_du_tampon_arriere(secondes, attendu):
    assert mpv_widget._grid_back_bytes(secondes) == attendu


# ── expurgation des jetons signés ────────────────────────────────────────────

@pytest.mark.parametrize("param", [
    "sig", "token", "Signature", "Policy", "Key-Pair-Id", "hdnts", "dna",
    "SIG", "ToKeN",          # l'expression est insensible à la casse
])
def test_les_jetons_signes_sont_expurges(param):
    """Une URL HLS complète vaut accès au flux : elle ne doit pas fuiter."""
    ligne = f"https://x.tv/p.m3u8?{param}=SECRET123&autre=ok"
    expurge = mpv_widget._redact(ligne)
    assert "SECRET123" not in expurge
    assert "[expurgé]" in expurge
    assert "autre=ok" in expurge, "les paramètres anodins restent lisibles"


def test_l_expurgation_s_arrete_au_parametre_suivant():
    ligne = "?token=abc&channel=zerator"
    assert mpv_widget._redact(ligne) == "?token=[expurgé]&channel=zerator"


def test_une_ligne_sans_jeton_n_est_pas_modifiee():
    assert mpv_widget._redact("vo/gpu: reconfig") == "vo/gpu: reconfig"


# ── journal mpv ──────────────────────────────────────────────────────────────

@pytest.fixture
def journal(tmp_path, monkeypatch):
    """Redirige le journal mpv hors de ~/.zlink."""
    cible = tmp_path / "mpv.log"
    monkeypatch.setattr(mpv_widget, "_MPV_LOG_PATH", cible)
    return cible


@pytest.mark.parametrize("niveau,prefixe", [
    ("error", "ffmpeg"),     # niveau important, module quelconque
    ("warn", "ffmpeg"),
    ("fatal", "ffmpeg"),
    ("debug", "vo"),         # module surveillé, niveau quelconque
    ("v", "x11"),
    ("info", "gpu/context"), # le module est la partie avant le « / »
    ("trace", "cplayer"),
])
def test_les_messages_utiles_sont_ecrits(journal, niveau, prefixe):
    handler = mpv_widget._mpv_log_handler(types.SimpleNamespace(_grid_mode=True))
    handler(niveau, prefixe, "quelque chose")
    assert "quelque chose" in journal.read_text(encoding="utf-8")


@pytest.mark.parametrize("niveau,prefixe", [
    ("debug", "ffmpeg"),
    ("v", "demux"),
    ("info", "cache"),
    ("trace", "hls"),
])
def test_le_bruit_du_demultiplexeur_est_ecarte(journal, niveau, prefixe):
    """570 000 lignes sur 646 000 dans un relevé réel : sans filtre, illisible."""
    handler = mpv_widget._mpv_log_handler(types.SimpleNamespace(_grid_mode=False))
    handler(niveau, prefixe, "segment téléchargé")
    assert not journal.exists()


@pytest.mark.parametrize("grille,etiquette", [
    (True, "[grille]"), (False, "[plein-écran]"),
])
def test_l_origine_de_la_ligne_est_identifiable(journal, grille, etiquette):
    """Vingt-cinq cellules écrivent dans le même fichier."""
    handler = mpv_widget._mpv_log_handler(
        types.SimpleNamespace(_grid_mode=grille))
    handler("error", "vo", "boum")
    assert etiquette in journal.read_text(encoding="utf-8")


def test_le_journal_expurge_les_jetons(journal):
    handler = mpv_widget._mpv_log_handler(types.SimpleNamespace(_grid_mode=True))
    handler("error", "vo", "https://x.tv/p.m3u8?sig=SECRET")
    assert "SECRET" not in journal.read_text(encoding="utf-8")


def test_le_journal_est_plafonne(journal, monkeypatch):
    """Un fichier de diagnostic ne doit jamais remplir le disque."""
    monkeypatch.setattr(mpv_widget, "_MPV_LOG_MAX_BYTES", 200)
    journal.write_bytes(b"A" * 500)
    handler = mpv_widget._mpv_log_handler(types.SimpleNamespace(_grid_mode=True))
    handler("error", "vo", "apres-rotation")
    contenu = journal.read_bytes()
    # 100 octets conservés (la moitié du plafond) + la ligne qu'on vient
    # d'écrire : très en dessous des 500 de départ.
    assert len(contenu) < 500
    assert b"apres-rotation" in contenu
    assert contenu.startswith(b"A"), "c'est la FIN du journal qui est gardée"


def test_le_journal_accumule_les_lignes(journal):
    handler = mpv_widget._mpv_log_handler(types.SimpleNamespace(_grid_mode=True))
    handler("error", "vo", "premiere")
    handler("error", "vo", "seconde")
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 2


def test_un_journal_inecrivable_ne_fait_pas_tomber_le_lecteur(monkeypatch,
                                                              tmp_path):
    """Le handler tourne sur un thread de mpv : y lever tuerait la lecture."""
    # Un dossier là où le journal attend un fichier : l'ouverture lève OSError.
    monkeypatch.setattr(mpv_widget, "_MPV_LOG_PATH", tmp_path / "d" / "mpv.log")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "mpv.log").mkdir()
    handler = mpv_widget._mpv_log_handler(types.SimpleNamespace(_grid_mode=True))
    handler("error", "vo", "peu importe")   # ne doit pas lever


@pytest.mark.parametrize("niveau,prefixe", [(None, None), ("", "")])
def test_niveau_et_prefixe_absents_ne_font_pas_lever(journal, niveau, prefixe):
    handler = mpv_widget._mpv_log_handler(types.SimpleNamespace(_grid_mode=True))
    handler(niveau, prefixe, "texte")
    assert not journal.exists()


# ── localisation de streamlink ───────────────────────────────────────────────

def test_streamlink_du_venv_courant_est_prioritaire(tmp_path, monkeypatch):
    """Le venv de l'application avant tout : c'est lui qui a la bonne version."""
    faux = tmp_path / "Scripts"
    faux.mkdir()
    binaire = faux / "streamlink.exe"
    binaire.write_text("", encoding="utf-8")
    monkeypatch.setattr(mpv_widget.sys, "executable", str(faux / "python.exe"))
    assert mpv_widget._find_streamlink() == str(binaire)


def test_repli_sur_le_PATH(monkeypatch, tmp_path):
    # Aucun candidat de venv ne répond : seul le PATH reste.
    monkeypatch.setattr(mpv_widget.pathlib.Path, "is_file", lambda self: False)
    monkeypatch.setattr(mpv_widget.shutil, "which",
                        lambda nom: str(tmp_path / "streamlink"))
    assert mpv_widget._find_streamlink() == str(tmp_path / "streamlink")


def test_streamlink_introuvable_rend_une_chaine_vide(monkeypatch, caplog):
    """Chaîne vide, jamais un nom nu : CreateProcess résoudrait un nom nu
    depuis le dossier de l'application avant le PATH."""
    monkeypatch.setattr(mpv_widget.pathlib.Path, "is_file", lambda self: False)
    monkeypatch.setattr(mpv_widget.shutil, "which", lambda nom: None)
    with caplog.at_level(logging.ERROR, logger=mpv_widget.logger.name):
        assert mpv_widget._find_streamlink() == ""
    assert "introuvable" in caplog.text


# ── options de base ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("option", [
    "ytdl", "osc", "load_scripts", "load_console", "load_stats_overlay",
    "load_select", "load_positioning", "load_commands", "load_context_menu",
    "load_auto_profiles", "input_default_bindings", "input_vo_keyboard",
    "input_cursor",
])
def test_tout_ce_qui_coute_est_desactive(option):
    """Six interpréteurs Lua par cellule, soit 150 sur une grille de 25."""
    assert mpv_widget.MpvWidget._options_de_base()[option] is False


def test_le_lecteur_est_silencieux_par_defaut():
    assert mpv_widget.MpvWidget._options_de_base()["really_quiet"] is True


def test_les_options_de_base_ne_sont_pas_partagees():
    """Chaque appelant mute le dictionnaire reçu : le partager les mélangerait."""
    a = mpv_widget.MpvWidget._options_de_base()
    b = mpv_widget.MpvWidget._options_de_base()
    a["marqueur"] = 1
    assert "marqueur" not in b


# ── options de tampon ────────────────────────────────────────────────────────

def test_tampon_de_grille():
    """Faible latence, muet, et un tampon arrière PLAFONNÉ (pas nul)."""
    opts: dict = {}
    mpv_widget.MpvWidget._appliquer_options_tampon(opts, True, 90)
    assert opts["mute"] is True
    assert opts["demuxer_readahead_secs"] == 2
    assert opts["cache_pause"] is False
    assert opts["demuxer_max_back_bytes"] == mpv_widget._grid_back_bytes(90)


def test_sans_clip_le_tampon_arriere_de_grille_est_nul():
    """C'est la fuite de 8,2 Go d'origine : rien ne doit s'accumuler."""
    opts: dict = {}
    mpv_widget.MpvWidget._appliquer_options_tampon(opts, True, 0)
    assert opts["demuxer_max_back_bytes"] == 0


@pytest.mark.parametrize("secondes,attendu", [
    (0, 90),        # plancher
    (30, 90),
    (60, 90),
    (90, 120),      # au-delà, secs + 30
    (180, 210),
])
def test_tampon_de_plein_ecran(secondes, attendu):
    opts: dict = {}
    mpv_widget.MpvWidget._appliquer_options_tampon(opts, False, secondes)
    assert opts["demuxer_readahead_secs"] == attendu
    assert opts["demuxer_max_bytes"] == "200MiB"
    assert "mute" not in opts, "le plein écran a le son"


def test_le_filtre_audio_est_absent_par_defaut():
    """~2,9 % d'un cœur et 12 threads PAR FLUX : jamais par défaut."""
    opts: dict = {}
    mpv_widget.MpvWidget._appliquer_options_tampon(opts, True, 90)
    assert "af" not in opts


def test_le_filtre_audio_est_etiquete_quand_il_est_actif(monkeypatch):
    """La propriété n'existe que sous la forme af-metadata/<label>."""
    monkeypatch.setattr(mpv_widget, "_AUDIO_SIGNAL", True)
    opts: dict = {}
    mpv_widget.MpvWidget._appliquer_options_tampon(opts, True, 90)
    assert opts["af"].startswith("@zl:")
    assert "astats" in opts["af"]


def test_le_filtre_audio_ne_vise_pas_le_plein_ecran(monkeypatch):
    monkeypatch.setattr(mpv_widget, "_AUDIO_SIGNAL", True)
    opts: dict = {}
    mpv_widget.MpvWidget._appliquer_options_tampon(opts, False, 90)
    assert "af" not in opts


# ── basse latence ────────────────────────────────────────────────────────────

def test_sans_basse_latence_ffmpeg_garde_sa_marge():
    """Trois segments d'avance, c'est la marge qui absorbe les à-coups réseau."""
    opts: dict = {}
    mpv_widget.MpvWidget._appliquer_options_latence(
        types.SimpleNamespace(_low_latency=False), opts)
    assert "demuxer_lavf_o" not in opts


def test_la_basse_latence_garde_un_segment_de_reserve():
    """Pas `-1` : le dernier segment est encore en cours d'écriture chez Twitch,
    et démarrer dessus fait bafouiller le flux."""
    opts: dict = {}
    mpv_widget.MpvWidget._appliquer_options_latence(
        types.SimpleNamespace(_low_latency=True), opts)
    assert opts["demuxer_lavf_o"] == "live_start_index=-2"


def test_la_basse_latence_se_pose_sur_un_lecteur_vivant(widget_inerte):
    """Cocher la case ne doit pas demander de relancer l'application."""
    lecteur = FauxLecteur()
    widget_inerte._player = lecteur
    widget_inerte.set_low_latency(True)
    assert lecteur.demuxer_lavf_o == "live_start_index=-2"


def test_couper_la_basse_latence_vide_l_option(widget_inerte):
    """Une option laissée en place rendrait le décochage sans effet."""
    lecteur = FauxLecteur()
    widget_inerte._player = lecteur
    widget_inerte.set_low_latency(True)
    widget_inerte.set_low_latency(False)
    assert lecteur.demuxer_lavf_o == ""


def test_la_basse_latence_inchangee_ne_touche_a_rien(widget_inerte):
    """Chaque sauvegarde des réglages rappelle ce point d'entrée."""
    lecteur = FauxLecteur()
    widget_inerte._player = lecteur
    widget_inerte.set_low_latency(False)
    assert lecteur.demuxer_lavf_o is None


def test_un_lecteur_qui_refuse_l_option_ne_fait_pas_tomber_le_reglage(widget_inerte):
    """Réglage d'agrément : son échec ne doit pas remonter à l'appelant."""
    class Refus:
        def __setattr__(self, nom, valeur):
            raise RuntimeError("propriété inconnue")

    widget_inerte._player = Refus()
    widget_inerte.set_low_latency(True)


# ── options de journal ───────────────────────────────────────────────────────

def test_le_journal_mpv_est_ferme_par_defaut(monkeypatch):
    monkeypatch.delenv("ZLINK_MPV_LOG", raising=False)
    opts = {"really_quiet": True}
    mpv_widget.MpvWidget._appliquer_options_journal(
        types.SimpleNamespace(_grid_mode=False), opts)
    assert "log_handler" not in opts
    assert opts["really_quiet"] is True


def test_le_journal_mpv_s_active_par_l_environnement(monkeypatch):
    """really_quiet doit sauter, sinon mpv n'émet aucun message à relayer."""
    monkeypatch.setenv("ZLINK_MPV_LOG", "1")
    opts = {"really_quiet": True}
    mpv_widget.MpvWidget._appliquer_options_journal(
        types.SimpleNamespace(_grid_mode=False), opts)
    assert callable(opts["log_handler"])
    assert opts["loglevel"] == "v"
    assert "really_quiet" not in opts


@pytest.mark.parametrize("valeur", ["0", "", "oui", "true"])
def test_seul_1_active_le_journal_mpv(monkeypatch, valeur):
    monkeypatch.setenv("ZLINK_MPV_LOG", valeur)
    opts: dict = {}
    mpv_widget.MpvWidget._appliquer_options_journal(
        types.SimpleNamespace(_grid_mode=False), opts)
    assert opts == {}


# ── résolution d'URL par streamlink ──────────────────────────────────────────

@pytest.fixture
def streamlink(monkeypatch):
    """Détourne subprocess.run et fournit un chemin streamlink factice."""
    monkeypatch.setattr(mpv_widget, "_STREAMLINK", "/faux/streamlink")
    appels: list[list[str]] = []

    def poser(stdout="", stderr="", rc=0, effet=None):
        def faux_run(cmd, **kwargs):
            appels.append(cmd)
            if effet is not None:
                effet()
            return subprocess.CompletedProcess(cmd, rc, stdout, stderr)
        monkeypatch.setattr(mpv_widget.subprocess, "run", faux_run)
        return appels

    return poser


def test_url_resolue(streamlink):
    appels = streamlink(stdout="https://video.tv/flux.m3u8\n")
    url = mpv_widget.MpvWidget._url_streamlink(
        "zerator", "480p", threading.Event())
    assert url == "https://video.tv/flux.m3u8", "le retour à la ligne est retiré"
    assert appels[0][:2] == ["/faux/streamlink", "twitch.tv/zerator"]
    assert "--stream-url" in appels[0]
    assert "--twitch-disable-ads" in appels[0]


def test_la_qualite_est_validee_avant_d_atteindre_le_shell(streamlink):
    """`quality` vient de la configuration : une valeur exotique est refusée."""
    appels = streamlink(stdout="https://x/f.m3u8")
    mpv_widget.MpvWidget._url_streamlink(
        "zerator", "480p; rm -rf /", threading.Event())
    assert "480p; rm -rf /" not in appels[0]
    # Le repli est celui du module, pas une chaîne recopiée : l'échelle a déjà
    # changé une fois, et un test qui la fige empêche de corriger la vraie.
    assert mpv_widget.QUALITE_GRILLE in appels[0]


def test_une_annulation_anterieure_evite_le_lancement(streamlink):
    """Le processus streamlink coûte plusieurs secondes : autant ne pas le lancer."""
    appels = streamlink(stdout="https://x/f.m3u8")
    drapeau = threading.Event()
    drapeau.set()
    assert mpv_widget.MpvWidget._url_streamlink("zerator", "480p", drapeau) is None
    assert appels == []


def test_une_annulation_pendant_la_resolution_jette_le_resultat(streamlink):
    """La cellule a changé de chaîne : l'ancien flux ne doit pas démarrer."""
    drapeau = threading.Event()
    streamlink(stdout="https://x/f.m3u8", effet=drapeau.set)
    assert mpv_widget.MpvWidget._url_streamlink("zerator", "480p", drapeau) is None


@pytest.mark.parametrize("stdout,rc", [
    ("", 0),                    # succès annoncé mais rien à jouer
    ("", 1),
    ("https://x/f.m3u8", 1),    # URL présente mais streamlink a échoué
    ("   \n", 0),
])
def test_une_resolution_ratee_rend_none(streamlink, stdout, rc):
    streamlink(stdout=stdout, stderr="offline", rc=rc)
    assert mpv_widget.MpvWidget._url_streamlink(
        "zerator", "480p", threading.Event()) is None


# ── résolution des symboles OpenGL ───────────────────────────────────────────

def test_sans_contexte_opengl_aucune_adresse(monkeypatch):
    """Renvoyer autre chose que 0 ferait sauter libmpv dans le vide."""
    monkeypatch.setattr(mpv_widget, "QOpenGLContext",
                        types.SimpleNamespace(currentContext=lambda: None))
    assert mpv_widget._gl_proc_address(None, b"glClear") == 0


def test_l_adresse_vient_du_contexte_courant(monkeypatch):
    contexte = types.SimpleNamespace(getProcAddress=lambda nom: 0xDEADBEEF)
    monkeypatch.setattr(mpv_widget, "QOpenGLContext",
                        types.SimpleNamespace(currentContext=lambda: contexte))
    assert mpv_widget._gl_proc_address(None, b"glClear") == 0xDEADBEEF


# ── widget sans libmpv ───────────────────────────────────────────────────────

def test_sans_libmpv_le_widget_est_inerte(widget_inerte):
    """Mode dégradé : l'application reste utilisable, sans vidéo."""
    assert widget_inerte._player is None
    assert widget_inerte.is_playing is False
    assert widget_inerte.uses_render_backend is False
    assert widget_inerte.save_clip() is None
    assert widget_inerte.get_audio_rms_db() is None
    # Aucun de ces appels ne doit lever.
    widget_inerte.play("https://x/f.m3u8")
    widget_inerte.play_stream("zerator")
    widget_inerte.stop()
    widget_inerte.set_clip_buffer(60)
    widget_inerte.set_low_latency(True)
    widget_inerte._reapply_audio()
    widget_inerte.shutdown()


def test_le_widget_est_inventorie(qtbot, monkeypatch):
    """L'inventaire des lecteurs vivants sert au diagnostic."""
    monkeypatch.setattr(mpv_widget, "_MPV_AVAILABLE", False)
    w = mpv_widget.MpvWidget()
    qtbot.addWidget(w)
    assert w in mpv_widget._LIVE_PLAYERS


# ── état audio voulu ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("demande,attendu", [
    (-10, 0), (0, 0), (50, 50), (100, 100), (150, 100), (73.9, 73),
])
def test_le_volume_est_borne(widget_inerte, demande, attendu):
    widget_inerte.set_volume(demande)
    assert widget_inerte._want_volume == attendu


def test_le_volume_et_la_coupure_atteignent_le_lecteur(widget_inerte):
    widget_inerte._player = FauxLecteur()
    widget_inerte.set_volume(30)
    widget_inerte.set_mute(True)
    assert widget_inerte._player.volume == 30
    assert widget_inerte._player.mute is True


def test_l_etat_audio_voulu_est_repose_apres_une_relance(widget_inerte):
    """Relancer une cellule ramenait le son à fond, console inchangée."""
    widget_inerte.set_volume(30)
    widget_inerte.set_mute(True)
    lecteur = FauxLecteur()          # lecteur NEUF, volume par défaut
    widget_inerte._player = lecteur
    widget_inerte._reapply_audio()
    assert lecteur.volume == 30 and lecteur.mute is True


def test_un_lecteur_recalcitrant_ne_fait_pas_echouer_la_lecture(widget_inerte):
    """Réglage d'agrément : mieux vaut du son mal réglé que pas de vidéo."""
    class Butee(FauxLecteur):
        def __setattr__(self, nom, valeur):
            if nom == "volume" and valeur is not None:
                raise RuntimeError("propriété verrouillée")
            super().__setattr__(nom, valeur)

    widget_inerte.set_volume(30)
    widget_inerte._player = Butee()
    widget_inerte._reapply_audio()   # ne doit pas lever


def test_play_reinitialise_le_marqueur_de_premiere_image(widget_inerte):
    widget_inerte._player = FauxLecteur()
    widget_inerte.set_volume(80)
    widget_inerte.set_mute(False)
    widget_inerte._time_pos_started = True
    widget_inerte.play("https://video.tv/flux.m3u8")
    assert widget_inerte._time_pos_started is False
    assert widget_inerte._player.lu == ["https://video.tv/flux.m3u8"]
    assert widget_inerte._player.volume == 80, "l'audio est réappliqué"


# ── observateur de première image ────────────────────────────────────────────

def test_l_observateur_est_pose_puis_retire(widget_inerte):
    """mpv émet time-pos à CHAQUE image : ~7 500 appels/s sur 25 cellules."""
    lecteur = FauxLecteur()
    widget_inerte._player = lecteur
    widget_inerte._observe_time_pos()
    assert lecteur.observes[0][0] == "time-pos"

    cb = widget_inerte._time_pos_cb
    cb("time-pos", 1.5)              # première image
    assert widget_inerte._time_pos_started is True
    assert lecteur.desabonnes == [("time-pos", cb)]
    assert widget_inerte._time_pos_cb is None


@pytest.mark.parametrize("valeur", [None, 0, 0.0, -1.0])
def test_avant_la_premiere_image_rien_n_est_signale(widget_inerte, valeur):
    widget_inerte._player = FauxLecteur()
    widget_inerte._observe_time_pos()
    widget_inerte._time_pos_cb("time-pos", valeur)
    assert widget_inerte._time_pos_started is False
    assert widget_inerte._time_pos_cb is not None, "on reste à l'écoute"


def test_le_signal_de_demarrage_est_emis(widget_inerte, qtbot):
    widget_inerte._player = FauxLecteur()
    widget_inerte._observe_time_pos()
    with qtbot.waitSignal(widget_inerte.playback_started, timeout=1000):
        widget_inerte._time_pos_cb("time-pos", 0.5)


def test_poser_deux_fois_l_observateur_n_en_pose_qu_un(widget_inerte):
    lecteur = FauxLecteur()
    widget_inerte._player = lecteur
    widget_inerte._observe_time_pos()
    widget_inerte._observe_time_pos()
    assert len(lecteur.observes) == 1


def test_le_desabonnement_est_idempotent(widget_inerte):
    widget_inerte._player = FauxLecteur()
    widget_inerte._observe_time_pos()
    widget_inerte._unobserve_time_pos()
    widget_inerte._unobserve_time_pos()      # ne doit pas lever
    assert len(widget_inerte._player.desabonnes) == 1


def test_un_lecteur_qui_refuse_l_observation_laisse_le_widget_sain(widget_inerte):
    class Refus(FauxLecteur):
        def observe_property(self, nom, cb):
            raise RuntimeError("propriété inconnue")

    widget_inerte._player = Refus()
    widget_inerte._observe_time_pos()
    assert widget_inerte._time_pos_cb is None, "pas de rappel fantôme"


def test_les_observateurs_de_lecture_couvrent_debut_et_fin(widget_inerte, qtbot):
    """idle-active ne vaut fin de lecture QUE si une lecture a commencé."""
    lecteur = FauxLecteur()
    widget_inerte._player = lecteur
    widget_inerte._brancher_observateurs()
    observes = dict(lecteur.observes)
    assert set(observes) == {"time-pos", "idle-active"}

    # Repos AVANT toute lecture : la cellule n'a jamais démarré, on se tait.
    observes["idle-active"](None, True)
    assert widget_inerte._time_pos_started is False

    observes["time-pos"](None, 1.0)
    with qtbot.waitSignal(widget_inerte.playback_ended, timeout=1000):
        observes["idle-active"](None, True)
    assert widget_inerte._time_pos_started is False


# ── arrêt et annulation ──────────────────────────────────────────────────────

def test_stop_annule_la_resolution_en_cours(widget_inerte):
    lecteur = FauxLecteur()
    widget_inerte._player = lecteur
    drapeau = threading.Event()
    widget_inerte._stop_flag = drapeau
    widget_inerte._time_pos_started = True

    widget_inerte.stop()
    assert drapeau.is_set(), "le thread de résolution doit renoncer"
    assert widget_inerte._stop_flag is None
    assert widget_inerte._time_pos_started is False
    assert lecteur.arrets == 1


def test_un_lecteur_qui_refuse_de_s_arreter_ne_bloque_pas(widget_inerte):
    class Recalcitrant(FauxLecteur):
        def stop(self):
            raise RuntimeError("déjà mort")

    widget_inerte._player = Recalcitrant()
    widget_inerte.stop()             # ne doit pas lever


def test_shutdown_est_idempotent(widget_inerte):
    lecteur = FauxLecteur()
    widget_inerte._player = lecteur
    widget_inerte.shutdown()
    widget_inerte.shutdown()
    assert lecteur.terminaisons == 1
    assert widget_inerte._player is None


def test_shutdown_libere_le_contexte_de_rendu_avant_le_lecteur(widget_inerte):
    """L'ordre compte : le contexte de rendu référence le lecteur."""
    ordre: list[str] = []

    class Contexte:
        def free(self):
            ordre.append("contexte")

    class Lecteur(FauxLecteur):
        def terminate(self):
            ordre.append("lecteur")

    widget_inerte._render_ctx = Contexte()
    widget_inerte._player = Lecteur()
    widget_inerte.shutdown()
    assert ordre == ["contexte", "lecteur"]
    assert widget_inerte._render_ctx is None


def test_un_contexte_de_rendu_recalcitrant_n_empeche_pas_l_arret(widget_inerte):
    class Contexte:
        def free(self):
            raise RuntimeError("déjà libéré")

    lecteur = FauxLecteur()
    widget_inerte._render_ctx = Contexte()
    widget_inerte._player = lecteur
    widget_inerte.shutdown()
    assert lecteur.terminaisons == 1


def test_un_terminate_qui_leve_est_absorbe(widget_inerte):
    class Lecteur(FauxLecteur):
        def terminate(self):
            raise RuntimeError("mpv est déjà parti")

    widget_inerte._player = Lecteur()
    widget_inerte.shutdown()
    assert widget_inerte._player is None


# ── lecture de grille ────────────────────────────────────────────────────────

def test_sans_streamlink_aucun_thread_n_est_lance(widget_inerte, monkeypatch):
    monkeypatch.setattr(mpv_widget, "_STREAMLINK", "")
    widget_inerte._player = FauxLecteur()
    widget_inerte.play_stream("zerator")
    assert widget_inerte._stop_flag is None


def test_play_stream_delegue_la_resolution_a_un_thread(widget_inerte, monkeypatch):
    """Non bloquant : la résolution dure plusieurs secondes."""
    monkeypatch.setattr(mpv_widget, "_STREAMLINK", "/faux/streamlink")
    fait = threading.Event()
    recu: list[tuple] = []

    def espion(self, login, quality, stop_flag, player):
        recu.append((login, quality, stop_flag, player))
        fait.set()

    monkeypatch.setattr(mpv_widget.MpvWidget, "_resoudre_et_jouer", espion)
    lecteur = FauxLecteur()
    widget_inerte._player = lecteur
    widget_inerte.play_stream("zerator", "480p")

    assert fait.wait(5), "le thread de résolution n'a pas démarré"
    login, quality, drapeau, player = recu[0]
    assert (login, quality) == ("zerator", "480p")
    assert player is lecteur, "le lecteur est figé, pas relu sur self"
    assert widget_inerte._stop_flag is drapeau


def test_play_stream_repose_l_observateur(widget_inerte, monkeypatch):
    """Nouveau flux, nouvelle première image à détecter."""
    monkeypatch.setattr(mpv_widget, "_STREAMLINK", "")
    lecteur = FauxLecteur()
    widget_inerte._player = lecteur
    widget_inerte._observe_time_pos()
    widget_inerte._unobserve_time_pos()
    widget_inerte.play_stream("zerator")
    assert widget_inerte._time_pos_cb is not None


def test_la_resolution_lance_la_lecture(widget_inerte):
    lecteur = FauxLecteur()
    widget_inerte.set_volume(40)
    widget_inerte.set_mute(True)
    widget_inerte._player = lecteur
    widget_inerte._url_streamlink = lambda *a: "https://video.tv/f.m3u8"
    widget_inerte._resoudre_et_jouer("zerator", "480p", threading.Event(),
                                     lecteur)
    assert lecteur.lu == ["https://video.tv/f.m3u8"]
    assert lecteur.volume == 40 and lecteur.mute is True


@pytest.mark.parametrize("url,annule", [
    (None, False),                       # résolution en échec
    ("https://video.tv/f.m3u8", True),   # cellule changée entre-temps
])
def test_rien_n_est_joue_si_la_resolution_echoue_ou_est_annulee(
        widget_inerte, url, annule):
    lecteur = FauxLecteur()
    widget_inerte._player = lecteur
    drapeau = threading.Event()
    if annule:
        drapeau.set()
    widget_inerte._url_streamlink = lambda *a: url
    widget_inerte._resoudre_et_jouer("zerator", "480p", drapeau, lecteur)
    assert lecteur.lu == []


@pytest.mark.parametrize("panne", [
    FileNotFoundError("streamlink"),
    subprocess.TimeoutExpired("streamlink", 25),
    RuntimeError("imprévu"),
])
def test_une_panne_de_resolution_reste_dans_le_thread(widget_inerte, panne):
    """Le thread est un daemon : une exception y serait perdue et bruyante."""
    def tombe(*a):
        raise panne

    widget_inerte._player = FauxLecteur()
    widget_inerte._url_streamlink = tombe
    widget_inerte._resoudre_et_jouer("zerator", "480p", threading.Event(),
                                     widget_inerte._player)


# ── clips ────────────────────────────────────────────────────────────────────

def test_le_clip_puise_dans_le_tampon_arriere(widget_inerte, tmp_path):
    lecteur = FauxLecteur(time_pos=100.0)
    widget_inerte._player = lecteur
    chemin = widget_inerte.save_clip(60, str(tmp_path / "clips"))
    nom, debut, fin, sortie = lecteur.commandes[0]
    assert nom == "dump-cache"
    assert (debut, fin) == (40.0, 100.0)
    assert sortie == chemin and chemin.endswith(".ts")
    assert (tmp_path / "clips").is_dir(), "le dossier est créé au besoin"


def test_deux_clips_dans_la_meme_seconde_ne_se_marchent_pas_dessus(
        widget_inerte, tmp_path):
    """Le nom du fichier ne tenait qu'à la seconde.

    Deux « dump-cache » lancés coup sur coup visaient le même fichier : le
    second écrasait le premier pendant que mpv y écrivait encore. C'est ce qui
    faisait repartir le replay du plein écran chercher chez Twitch, croyant
    qu'aucun tampon local ne valait mieux.
    """
    widget_inerte._player = FauxLecteur(time_pos=100.0)
    premier = widget_inerte.save_clip(60, str(tmp_path))
    second = widget_inerte.save_clip(30, str(tmp_path))
    assert premier != second


def test_un_clip_plus_long_que_la_lecture_part_de_zero(widget_inerte, tmp_path):
    lecteur = FauxLecteur(time_pos=10.0)
    widget_inerte._player = lecteur
    widget_inerte.save_clip(60, str(tmp_path))
    assert lecteur.commandes[0][1] == 0.0


def test_sans_position_de_lecture_aucun_clip(widget_inerte, tmp_path):
    """time-pos est None tant que rien n'a commencé : le fichier serait vide."""
    widget_inerte._player = FauxLecteur(time_pos=None)
    assert widget_inerte.save_clip(60, str(tmp_path)) is None


def test_un_clip_impossible_a_ecrire_rend_none(widget_inerte, tmp_path):
    cible = tmp_path / "fichier"
    cible.write_text("", encoding="utf-8")
    widget_inerte._player = FauxLecteur(time_pos=100.0)
    # mkdir() sur un chemin dont le parent est un fichier lève.
    assert widget_inerte.save_clip(60, str(cible / "clips")) is None


@pytest.mark.parametrize("secondes,attendu", [
    (0, 0), (-1, 0), (60, 24_000_000), (90, 36_000_000),
])
def test_le_tampon_de_clip_s_ajuste_a_chaud(widget_inerte, secondes, attendu):
    """Modifiable sans couper les vingt-cinq flux."""
    lecteur = FauxLecteur()
    widget_inerte._player = lecteur
    widget_inerte.set_clip_buffer(secondes)
    assert lecteur.demuxer_max_back_bytes == attendu


def test_un_tampon_refuse_ne_fait_pas_lever(widget_inerte):
    class Butee(FauxLecteur):
        def __setattr__(self, nom, valeur):
            if nom == "demuxer_max_back_bytes" and valeur:
                raise RuntimeError("propriété verrouillée")
            super().__setattr__(nom, valeur)

    widget_inerte._player = Butee()
    widget_inerte.set_clip_buffer(60)    # ne doit pas lever


# ── signal audio ─────────────────────────────────────────────────────────────

def test_sans_signal_audio_la_mesure_est_indisponible(widget_inerte):
    widget_inerte._player = FauxLecteur()
    assert widget_inerte.get_audio_rms_db() is None


@pytest.fixture
def widget_audio(widget_inerte, monkeypatch):
    """Cellule de grille avec le filtre astats activé."""
    monkeypatch.setattr(mpv_widget, "_AUDIO_SIGNAL", True)
    widget_inerte._grid_mode = True
    widget_inerte._player = FauxLecteur()
    return widget_inerte


def test_le_niveau_rms_est_lu_sur_la_propriete_etiquetee(widget_audio):
    """player["af-metadata"] lisait une OPTION : la fonction rendait toujours None."""
    widget_audio._player.meta = {"lavfi.astats.Overall.RMS_level": "-23.5"}
    assert widget_audio.get_audio_rms_db() == pytest.approx(-23.5)


@pytest.mark.parametrize("meta", [
    None, {}, {"lavfi.astats.Overall.RMS_level": "-inf"},   # silence pur
    {"lavfi.astats.Overall.RMS_level": ""},
    {"autre": "1"},
    {"lavfi.astats.Overall.RMS_level": "pas un nombre"},
])
def test_un_niveau_rms_inexploitable_rend_none(widget_audio, meta):
    widget_audio._player.meta = meta
    assert widget_audio.get_audio_rms_db() is None


def test_le_niveau_rms_ignore_le_plein_ecran(widget_audio):
    """Le filtre n'est posé que sur les cellules de grille."""
    widget_audio._grid_mode = False
    widget_audio._player.meta = {"lavfi.astats.Overall.RMS_level": "-10"}
    assert widget_audio.get_audio_rms_db() is None


# ── cadence de repaint (backend rendu) ───────────────────────────────────────

def test_sans_plafond_chaque_image_est_peinte(widget_inerte, monkeypatch):
    """Le plein écran garde la cadence native du flux."""
    peintures = []
    monkeypatch.setattr(widget_inerte, "update", lambda: peintures.append(1))
    widget_inerte._min_frame_interval = 0.0
    for _ in range(5):
        widget_inerte._on_frame_ready()
    assert len(peintures) == 5


def test_les_vignettes_sont_plafonnees_en_cadence(widget_inerte, monkeypatch):
    """Chaque repaint déclenche une passe de composition : à 24 cellules,
    la cadence native ferait des dizaines de passes par seconde."""
    horloge = {"t": 1000.0}
    monkeypatch.setattr("widgets.mpv_widget.time.monotonic", lambda: horloge["t"])
    peintures = []
    monkeypatch.setattr(widget_inerte, "update", lambda: peintures.append(1))
    widget_inerte._min_frame_interval = mpv_widget._GRID_FRAME_INTERVAL

    widget_inerte._on_frame_ready()          # première image : peinte
    horloge["t"] += 0.01                     # bien avant l'échéance
    widget_inerte._on_frame_ready()
    horloge["t"] += 0.01
    widget_inerte._on_frame_ready()
    assert len(peintures) == 1

    horloge["t"] += mpv_widget._GRID_FRAME_INTERVAL
    widget_inerte._on_frame_ready()
    assert len(peintures) == 2


def test_le_plafond_de_grille_vaut_quinze_images_par_seconde():
    assert mpv_widget._GRID_FRAME_INTERVAL == pytest.approx(1 / 15)


# ── backend de rendu ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("api_rendu,contexte,attendu", [
    (False, None, False),
    (False, object(), False),    # hors macOS, jamais notre rendu
    (True, None, False),         # contexte pas encore créé
    (True, object(), True),
])
def test_le_backend_de_rendu_exige_les_deux_conditions(
        widget_inerte, monkeypatch, api_rendu, contexte, attendu):
    monkeypatch.setattr(mpv_widget, "_RENDER_API", api_rendu)
    widget_inerte._render_ctx = contexte
    assert widget_inerte.uses_render_backend is attendu


def test_initializeGL_ne_fait_rien_hors_macos(widget_inerte, monkeypatch):
    monkeypatch.setattr(mpv_widget, "_RENDER_API", False)
    widget_inerte._player = FauxLecteur()
    widget_inerte.initializeGL()
    assert widget_inerte._render_ctx is None


def test_un_contexte_de_rendu_indisponible_laisse_le_widget_sain(
        widget_inerte, monkeypatch):
    """Mieux vaut un widget noir qu'une exception dans initializeGL."""
    monkeypatch.setattr(mpv_widget, "_RENDER_API", True)

    class Module:
        @staticmethod
        def MpvRenderContext(*a, **kw):
            raise RuntimeError("pas de contexte OpenGL")

        @staticmethod
        def MpvGlGetProcAddressFn(fn):
            return fn

    monkeypatch.setattr(mpv_widget, "_mpv_module", Module)
    widget_inerte._player = FauxLecteur()
    widget_inerte.initializeGL()
    assert widget_inerte._render_ctx is None


def test_paintGL_sans_contexte_ne_leve_pas(widget_inerte):
    widget_inerte._render_ctx = None
    widget_inerte.paintGL()


def test_une_erreur_de_rendu_ne_tue_pas_la_peinture(widget_inerte):
    class Contexte:
        def render(self, **kw):
            raise RuntimeError("FBO invalide")

    widget_inerte._render_ctx = Contexte()
    widget_inerte.paintGL()          # ne doit pas lever


# ── locale ───────────────────────────────────────────────────────────────────

def test_une_locale_a_virgule_est_ramenee_a_C(monkeypatch, caplog):
    """Avec une virgule décimale, mpv_create() part en segfault."""
    appels: list[tuple] = []
    monkeypatch.setattr(mpv_widget.locale, "getlocale",
                        lambda cat=None: ("fr_FR", "UTF-8"))
    monkeypatch.setattr(mpv_widget.locale, "setlocale",
                        lambda cat, val: appels.append((cat, val)))
    with caplog.at_level(logging.WARNING, logger=mpv_widget.logger.name):
        mpv_widget.MpvWidget._garantir_locale_c()
    assert appels == [(mpv_widget.locale.LC_NUMERIC, "C")]
    assert "LC_NUMERIC" in caplog.text


@pytest.mark.parametrize("actuelle", [None, "C", "POSIX"])
def test_une_locale_deja_saine_est_laissee_tranquille(monkeypatch, actuelle):
    appels: list[tuple] = []
    monkeypatch.setattr(mpv_widget.locale, "getlocale",
                        lambda cat=None: (actuelle, None))
    monkeypatch.setattr(mpv_widget.locale, "setlocale",
                        lambda cat, val: appels.append((cat, val)))
    mpv_widget.MpvWidget._garantir_locale_c()
    assert appels == []


# ── garde-fou Xlib ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("valeur,attendu", [(True, 1), (1, 1), (False, 0), (None, 0)])
def test_le_gestionnaire_X_est_repris_a_la_configuration_de_l_affichage(
        monkeypatch, valeur, attendu):
    """C'est à ce moment précis que mpv pose SON gestionnaire d'erreur X."""
    poses = []
    monkeypatch.setattr(mpv_widget._x11_guard, "install",
                        lambda: poses.append(1))
    mpv_widget.MpvWidget._on_vo_configured("vo-configured", valeur)
    assert len(poses) == attendu


def test_l_arret_reprend_le_gestionnaire_X_avant_et_apres(widget_inerte,
                                                          monkeypatch):
    """mpv le laisse derrière lui : un BadWindow en vol tuerait le processus."""
    poses: list[str] = []
    monkeypatch.setattr(mpv_widget._x11_guard, "install",
                        lambda: poses.append("garde"))

    class Lecteur(FauxLecteur):
        def terminate(self):
            poses.append("terminate")

    widget_inerte._player = Lecteur()
    widget_inerte.shutdown()
    assert poses == ["garde", "terminate", "garde"]


def test_le_chemin_de_libmpv_embarquee_est_vide_sans_fichier(monkeypatch,
                                                             tmp_path):
    monkeypatch.setattr(mpv_widget, "_RES_ROOT", tmp_path)
    assert mpv_widget._bundled_libmpv() == ""


@pytest.mark.parametrize("nom", [
    "libmpv.2.dylib", "libmpv.dylib", "libmpv.so.2", "libmpv.so",
])
def test_la_libmpv_embarquee_est_trouvee(monkeypatch, tmp_path, nom):
    """find_library('mpv') ne regarde QUE les emplacements système."""
    monkeypatch.setattr(mpv_widget, "_RES_ROOT", tmp_path)
    (tmp_path / nom).write_bytes(b"")
    assert mpv_widget._bundled_libmpv() == str(tmp_path / nom)


def test_l_ordre_de_recherche_de_libmpv_privilegie_le_dylib(monkeypatch,
                                                            tmp_path):
    monkeypatch.setattr(mpv_widget, "_RES_ROOT", tmp_path)
    for nom in ("libmpv.so", "libmpv.2.dylib"):
        (tmp_path / nom).write_bytes(b"")
    assert mpv_widget._bundled_libmpv() == str(tmp_path / "libmpv.2.dylib")


# ── fermeture ────────────────────────────────────────────────────────────────

def test_fermer_le_widget_termine_le_lecteur(widget_inerte, qtbot):
    """QApplication.quit() ne délivre pas closeEvent : ici c'est une vraie
    fermeture, et le lecteur doit partir avant que Qt ne démonte la fenêtre."""
    lecteur = FauxLecteur()
    widget_inerte._player = lecteur
    widget_inerte.close()
    assert lecteur.terminaisons == 1
    assert widget_inerte._player is None


def test_le_redimensionnement_ne_leve_pas(widget_inerte):
    widget_inerte.resize(640, 360)
    assert (widget_inerte.width(), widget_inerte.height()) == (640, 360)


def test_le_chemin_du_journal_est_prive():
    """Même expurgé, ce journal décrit ce que la personne regarde."""
    assert mpv_widget._MPV_LOG_PATH.parent == pathlib.Path.home() / ".zlink"


# ── construction du lecteur ──────────────────────────────────────────────────

def faux_module(lecteur=None, *, panne: Exception | None = None):
    """Double de python-mpv : construit sans jamais toucher à libmpv."""
    def MPV(**kwargs):               # noqa: N802 - nom imposé par python-mpv
        if panne is not None:
            raise panne
        MPV.recu.update(kwargs)
        return lecteur

    MPV.recu = {}
    return types.SimpleNamespace(MPV=MPV)


def test_le_lecteur_recoit_les_options_calculees(widget_inerte, monkeypatch):
    lecteur = FauxLecteur()
    module = faux_module(lecteur)
    monkeypatch.setattr(mpv_widget, "_mpv_module", module)
    widget_inerte._creer_lecteur({"vo": "gpu", "osc": False})
    assert widget_inerte._player is lecteur
    assert module.MPV.recu == {"vo": "gpu", "osc": False}


def test_la_configuration_de_l_affichage_est_surveillee(widget_inerte,
                                                        monkeypatch):
    """C'est là que mpv pose SON gestionnaire d'erreur X, pas à la construction."""
    lecteur = FauxLecteur()
    monkeypatch.setattr(mpv_widget, "_mpv_module", faux_module(lecteur))
    widget_inerte._creer_lecteur({})
    assert lecteur.observes == [("vo-configured", widget_inerte._on_vo_configured)]


def test_un_vo_configured_non_observable_ne_bloque_pas(widget_inerte,
                                                       monkeypatch):
    class Sourd(FauxLecteur):
        def observe_property(self, nom, cb):
            raise RuntimeError("propriété inconnue de cette version")

    monkeypatch.setattr(mpv_widget, "_mpv_module", faux_module(Sourd()))
    widget_inerte._creer_lecteur({})
    assert widget_inerte._player is not None, "l'agrément ne bloque pas la vidéo"


def test_un_echec_de_creation_laisse_le_widget_inerte(widget_inerte,
                                                      monkeypatch, caplog):
    monkeypatch.setattr(mpv_widget, "_mpv_module",
                        faux_module(panne=OSError("libmpv trop ancienne")))
    with caplog.at_level(logging.ERROR, logger=mpv_widget.logger.name):
        widget_inerte._creer_lecteur({})
    assert widget_inerte._player is None
    assert "impossible d'initialiser MPV" in caplog.text


@pytest.mark.parametrize("grille,muet", [(True, True), (False, False)])
def test_construction_complete(qtbot, monkeypatch, grille, muet):
    """Une cellule de grille démarre muette, le plein écran non."""
    lecteur = FauxLecteur()
    monkeypatch.setattr(mpv_widget, "_MPV_AVAILABLE", True)
    # La plateforme est FIXÉE : ce test porte sur la construction, pas sur
    # l'embarquement. Sous Linux hors X11 — le cas de l'intégration continue —
    # le widget se rend inerte à dessein, et aucun lecteur n'est créé ; c'est
    # le test voisin qui vérifie ce refus.
    monkeypatch.setattr(mpv_widget.sys, "platform", "win32")
    monkeypatch.setattr(mpv_widget, "_mpv_module", faux_module(lecteur))
    w = mpv_widget.MpvWidget(grid_mode=grille)
    qtbot.addWidget(w)
    assert w._player is lecteur
    assert w._want_muted is muet
    assert w._want_volume == 100
    assert {nom for nom, _ in lecteur.observes} == {
        "vo-configured", "time-pos", "idle-active"}


def test_une_plateforme_sans_affichage_sur_laisse_le_widget_inerte(
        qtbot, monkeypatch):
    """Sans fenêtre X11, winId() ne désigne rien pour mpv : le moindre
    BadWindow terminerait le processus depuis un thread de rendu."""
    monkeypatch.setattr(mpv_widget, "_MPV_AVAILABLE", True)
    monkeypatch.setattr(mpv_widget.sys, "platform", "linux")
    monkeypatch.setattr(mpv_widget, "_mpv_module",
                        faux_module(panne=AssertionError(
                            "aucun lecteur ne doit être créé")))
    w = mpv_widget.MpvWidget()
    qtbot.addWidget(w)
    assert w._player is None


# ── choix du backend d'affichage ─────────────────────────────────────────────

def test_backend_windows(widget_inerte, monkeypatch):
    monkeypatch.setattr(mpv_widget, "_RENDER_API", False)
    monkeypatch.setattr(mpv_widget.sys, "platform", "win32")
    opts: dict = {}
    assert widget_inerte._appliquer_options_affichage(opts, False) is True
    assert opts["hwdec"] == "d3d11va" and opts["gpu_api"] == "d3d11"
    assert opts["wid"] == str(int(widget_inerte.winId()))


def test_backend_x11(widget_inerte, monkeypatch):
    """gpu-context EXPLICITE : en autodétection, mpv voit WAYLAND_DISPLAY et
    ouvre sa propre fenêtre au lieu de se greffer sur le wid X11."""
    monkeypatch.setattr(mpv_widget, "_RENDER_API", False)
    monkeypatch.setattr(mpv_widget.sys, "platform", "linux")
    monkeypatch.setattr(mpv_widget, "QApplication", types.SimpleNamespace(
        instance=lambda: object(), platformName=lambda: "xcb"))
    opts: dict = {}
    assert widget_inerte._appliquer_options_affichage(opts, True) is True
    assert opts["gpu_context"] == "x11egl"
    assert opts["hwdec"] == mpv_widget._HWDEC_LINUX


def test_sans_cuda_nvdec_n_est_pas_propose(monkeypatch):
    """Le dlopen raté écrit « Cannot load libcuda.so.1 » HORS du journal mpv :
    une ligne par cellule, et mpv retenait vaapi de toute façon."""
    def _refuse(_nom):
        raise OSError("libcuda.so.1: cannot open shared object file")

    monkeypatch.setattr(mpv_widget.ctypes, "CDLL", _refuse)
    assert mpv_widget._hwdec_linux() == "vaapi,vaapi-copy,no"


def test_avec_cuda_on_laisse_mpv_choisir(monkeypatch):
    """Sur une machine NVIDIA, écarter nvdec coûterait le décodage matériel."""
    monkeypatch.setattr(mpv_widget.ctypes, "CDLL", lambda _nom: object())
    assert mpv_widget._hwdec_linux() == "auto-safe"


def test_le_repli_logiciel_ferme_la_liste(monkeypatch):
    """Sans `no`, une machine sans VA-API du tout n'aurait plus de repli."""
    monkeypatch.setattr(mpv_widget.ctypes, "CDLL",
                        lambda _nom: (_ for _ in ()).throw(OSError()))
    assert mpv_widget._hwdec_linux().split(",")[-1] == "no"


def test_backend_wayland_refuse(widget_inerte, monkeypatch, caplog):
    monkeypatch.setattr(mpv_widget, "_RENDER_API", False)
    monkeypatch.setattr(mpv_widget.sys, "platform", "linux")
    monkeypatch.setattr(mpv_widget, "QApplication", types.SimpleNamespace(
        instance=lambda: object(), platformName=lambda: "wayland"))
    opts: dict = {}
    with caplog.at_level(logging.ERROR, logger=mpv_widget.logger.name):
        assert widget_inerte._appliquer_options_affichage(opts, False) is False
    assert "wid" not in opts
    assert "xcb" in caplog.text, "le message dit comment s'en sortir"


@pytest.mark.parametrize("grille,plafond", [
    (True, mpv_widget._GRID_FRAME_INTERVAL),
    (False, 0.0),                   # le plein écran garde la cadence du flux
])
def test_backend_de_rendu_macos(widget_inerte, monkeypatch, grille, plafond):
    monkeypatch.setattr(mpv_widget, "_RENDER_API", True)
    opts: dict = {}
    assert widget_inerte._appliquer_options_affichage(opts, grille) is True
    assert opts["vo"] == "libmpv" and opts["hwdec"] == "videotoolbox"
    assert "wid" not in opts, "mpv n'implémente pas --wid sur macOS"
    assert widget_inerte._min_frame_interval == pytest.approx(plafond)


def test_le_contexte_de_rendu_est_cree_une_seule_fois(widget_inerte,
                                                      monkeypatch):
    """initializeGL est rappelé à chaque recréation de surface OpenGL."""
    contextes: list[object] = []

    class Contexte:
        def __init__(self, *a, **kw):
            contextes.append(self)
            self.update_cb = None

    monkeypatch.setattr(mpv_widget, "_RENDER_API", True)
    monkeypatch.setattr(mpv_widget, "_mpv_module", types.SimpleNamespace(
        MpvRenderContext=Contexte, MpvGlGetProcAddressFn=lambda fn: fn))
    widget_inerte._player = FauxLecteur()
    widget_inerte.initializeGL()
    widget_inerte.initializeGL()
    assert len(contextes) == 1
    assert widget_inerte._render_ctx is contextes[0]
    assert callable(widget_inerte._render_ctx.update_cb)


def test_sans_lecteur_aucun_contexte_de_rendu(widget_inerte, monkeypatch):
    monkeypatch.setattr(mpv_widget, "_RENDER_API", True)
    widget_inerte.initializeGL()
    assert widget_inerte._render_ctx is None


def test_le_redimensionnement_force_un_repaint_sur_macos(widget_inerte,
                                                         monkeypatch):
    """Sans cela, le FBO garde la frame de l'ancienne taille après un PiP."""
    from PyQt6.QtCore import QSize
    from PyQt6.QtGui import QResizeEvent

    monkeypatch.setattr(mpv_widget, "_RENDER_API", True)
    peintures: list[int] = []
    monkeypatch.setattr(widget_inerte, "update", lambda: peintures.append(1))
    # Qt diffère les QResizeEvent d'un widget caché : on le délivre nous-mêmes.
    widget_inerte.resizeEvent(QResizeEvent(QSize(320, 180), QSize(0, 0)))
    assert peintures


# ── divers ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("au_repos,en_lecture", [(True, False), (False, True)])
def test_l_etat_de_lecture_vient_de_mpv(widget_inerte, au_repos, en_lecture):
    widget_inerte._player = FauxLecteur(idle_active=au_repos)
    assert widget_inerte.is_playing is en_lecture


def test_un_desabonnement_refuse_est_absorbe(widget_inerte):
    class Sourd(FauxLecteur):
        def unobserve_property(self, nom, cb):
            raise RuntimeError("plus rien à désabonner")

    widget_inerte._player = Sourd()
    widget_inerte._observe_time_pos()
    widget_inerte._unobserve_time_pos()      # ne doit pas lever
    assert widget_inerte._time_pos_cb is None


def test_position_n_est_definie_qu_une_fois():
    """Une seconde définition, plus haut dans la classe, était silencieusement
    écrasée par la première.

    Le lecteur de clips recevait donc None là où la version du haut promettait
    un flottant — « unsupported operand type(s) for *: NoneType and int »,
    cinq fois par seconde, à chaque battement du transport.
    """
    import ast
    import pathlib

    source = (pathlib.Path(__file__).resolve().parent.parent
              / "widgets" / "mpv_widget.py").read_text(encoding="utf-8")
    classe = next(n for n in ast.parse(source).body
                  if isinstance(n, ast.ClassDef) and n.name == "MpvWidget")
    noms = [n.name for n in classe.body if isinstance(n, ast.FunctionDef)]
    doublons = {n for n in noms if noms.count(n) > 1}
    assert not doublons, f"méthodes définies deux fois : {sorted(doublons)}"


# ── options refusées par une libmpv plus ancienne ────────────────────────────

def _refus(nom: bytes):
    """L'exception que python-mpv lève sur une option inconnue.

    La forme est relevée sur le vrai module : les octets sont IMBRIQUÉS dans
    un tuple avec la poignée et la valeur, pas posés à plat.
    """
    # TROIS arguments, pas un tuple unique : c'est la forme que python-mpv
    # produit réellement, et Python l'affiche comme un tuple à l'écran — de
    # quoi s'y tromper en la recopiant depuis un message d'erreur.
    return AttributeError(
        "mpv option does not exist", -5, (object(), nom, b"no"))


@pytest.mark.parametrize("brut,attendu", [
    (b"load-context-menu", "load_context_menu"),
    (b"load-select", "load_select"),
])
def test_le_nom_de_l_option_refusee_est_retrouve(brut, attendu):
    assert mpv_widget._nom_de_l_option_refusee(_refus(brut)) == attendu


@pytest.mark.parametrize("exc", [
    AttributeError("autre chose"),
    AttributeError("mpv option does not exist", -5, (object(),)),
    ValueError("pas une AttributeError"),
])
def test_une_exception_etrangere_ne_donne_aucun_nom(exc):
    assert mpv_widget._nom_de_l_option_refusee(exc) == ""


def test_une_option_inconnue_est_abandonnee_et_le_lecteur_naît(monkeypatch):
    """SteamOS livre mpv 0.40, où `load-context-menu` n'existe pas encore :
    python-mpv refusait la construction ENTIÈRE et ZLink ne démarrait pas,
    pour une option qui ne fait que désactiver un menu inatteignable ici."""
    essais: list[dict] = []

    class _Fabrique:
        def MPV(self, **options):                       # noqa: N802 — API mpv
            essais.append(dict(options))
            if "load_context_menu" in options:
                raise _refus(b"load-context-menu")
            return "lecteur"

    monkeypatch.setattr(mpv_widget, "_mpv_module", _Fabrique())
    rendu = mpv_widget._instancier_mpv(
        {"vo": "gpu", "load_context_menu": False, "load_select": False})
    assert rendu == "lecteur"
    assert "load_context_menu" not in essais[-1]
    assert essais[-1]["vo"] == "gpu", "le reste de la configuration est gardé"


def test_plusieurs_options_inconnues_sont_abandonnees_une_a_une(monkeypatch):
    manquantes = {"load_context_menu", "load_select"}

    class _Fabrique:
        def MPV(self, **options):                       # noqa: N802 — API mpv
            for nom in options:
                if nom in manquantes:
                    raise _refus(nom.replace("_", "-").encode())
            return "lecteur"

    monkeypatch.setattr(mpv_widget, "_mpv_module", _Fabrique())
    assert mpv_widget._instancier_mpv(
        {"vo": "gpu", "load_context_menu": False, "load_select": False}
    ) == "lecteur"


def test_une_erreur_qui_n_est_pas_une_option_inconnue_remonte(monkeypatch):
    """Abandonner à l'aveugle viderait la configuration option par option."""
    class _Fabrique:
        def MPV(self, **_options):                      # noqa: N802 — API mpv
            raise AttributeError("libmpv est cassée")

    monkeypatch.setattr(mpv_widget, "_mpv_module", _Fabrique())
    with pytest.raises(AttributeError):
        mpv_widget._instancier_mpv({"vo": "gpu"})


def test_l_abandon_est_borne(monkeypatch):
    """Une configuration devenue absurde doit finir par se signaler."""
    class _Fabrique:
        def MPV(self, **options):                       # noqa: N802 — API mpv
            if options:
                nom = next(iter(options))
                raise _refus(nom.replace("_", "-").encode())
            return "lecteur"

    monkeypatch.setattr(mpv_widget, "_mpv_module", _Fabrique())
    trop = {f"opt_{i}": False for i in range(mpv_widget._MAX_OPTIONS_ABANDONNEES + 5)}
    with pytest.raises(AttributeError):
        mpv_widget._instancier_mpv(trop)
