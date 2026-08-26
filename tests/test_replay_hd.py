# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Reprise d'un moment chez Twitch, en pleine qualité.

Le point le plus piégeux tient en une ligne : Twitch sert du MP4 FRAGMENTÉ.
Sans le segment d'initialisation écrit en premier, le fichier obtenu commence
par un fragment, ne porte ni `ftyp` ni `moov`, et aucun lecteur ne l'ouvre —
alors que le téléchargement, lui, s'est parfaitement déroulé.
"""

from __future__ import annotations

import logging
import pathlib
import subprocess

import httpx
import pytest

from core import replay_hd as rh
from core.replay_hd import (
    REPLAY_SECS,
    duree_disponible,
    segment_initial,
    segments_a_prendre,
)

BASE = "https://cdn.test/v1/playlist.m3u8"

#: Playlist fMP4 telle que Twitch en sert : fenêtre glissante et initialisation.
PLAYLIST = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:2
#EXT-X-MAP:URI="init.mp4"
#EXTINF:2.000,
seg1.mp4
#EXTINF:2.000,
seg2.mp4
#EXTINF:2.000,
seg3.mp4
#EXTINF:2.000,
seg4.mp4
"""


# ── doublures : aucun réseau, aucun sous-processus ───────────────────────────

def _repondeur(contenus):
    """Transforme un plan {nom de fichier: contenu | code HTTP} en gestionnaire.

    La clé est le DERNIER élément du chemin : les URL sont fabriquées par
    urljoin depuis la base, et un test n'a pas à réécrire l'arborescence du CDN.
    """
    if callable(contenus):
        return contenus

    def repondre(requete: httpx.Request) -> httpx.Response:
        valeur = contenus.get(requete.url.path.rsplit("/", 1)[-1])
        if valeur is None:
            return httpx.Response(404)
        if isinstance(valeur, int):
            return httpx.Response(valeur)
        return httpx.Response(200, content=valeur)

    return repondre


def _client(contenus) -> httpx.Client:
    """Client httpx qui ne sort jamais de la machine."""
    return httpx.Client(transport=httpx.MockTransport(_repondeur(contenus)))


def _interdit(*args, **kwargs):
    """Doublure d'un appel qui NE DOIT PAS avoir lieu."""
    raise AssertionError(f"appel imprévu : {args} {kwargs}")


def _leve(exception):
    def run(*args, **kwargs):
        raise exception
    return run


def _rendu(code: int, sortie: str = ""):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, code, sortie, "")
    return run


@pytest.fixture
def twitch_simule(monkeypatch):
    """Court-circuite streamlink et le réseau pour dérouler `recuperer` en entier.

    `recuperer` fabrique son propre client httpx : la seule façon de le tenir
    hors connexion est de remplacer la fabrique.
    """
    # La vraie classe est capturée AVANT d'être remplacée : sans cela, la
    # fabrique s'appellerait elle-même et le test finirait en RecursionError.
    vraie_classe = httpx.Client

    def poser(contenus, url: str = BASE) -> None:
        monkeypatch.setattr(rh, "_resoudre", lambda login: url)
        repondre = _repondeur(contenus)

        def fabrique(**kwargs) -> httpx.Client:
            kwargs.pop("transport", None)
            return vraie_classe(transport=httpx.MockTransport(repondre), **kwargs)

        monkeypatch.setattr(rh.httpx, "Client", fabrique)

    return poser


# ── segment d'initialisation ─────────────────────────────────────────────────

def test_l_initialisation_est_reperee():
    """Sans elle, le fichier n'a ni ftyp ni moov et reste injouable."""
    assert segment_initial(PLAYLIST, BASE) == "https://cdn.test/v1/init.mp4"


def test_une_playlist_mpeg_ts_n_en_declare_pas():
    """Les vieux flux se collent bout à bout : pas d'initialisation à écrire."""
    assert segment_initial("#EXTM3U\n#EXTINF:2.0,\nseg1.ts\n") == ""


def test_une_url_absolue_d_initialisation_est_gardee_telle_quelle():
    p = '#EXT-X-MAP:URI="https://autre.test/init.mp4"\n'
    assert segment_initial(p, BASE) == "https://autre.test/init.mp4"


# ── sélection des segments ───────────────────────────────────────────────────

def test_on_remonte_depuis_la_fin():
    """C'est le passé IMMÉDIAT qui intéresse : le moment vient de se produire."""
    urls = segments_a_prendre(PLAYLIST, 4.0, BASE)
    assert urls == ["https://cdn.test/v1/seg3.mp4",
                    "https://cdn.test/v1/seg4.mp4"]


def test_l_ordre_reste_chronologique():
    """On sélectionne en remontant, mais on écrit dans l'ordre de lecture."""
    urls = segments_a_prendre(PLAYLIST, 8.0, BASE)
    assert urls == [f"https://cdn.test/v1/seg{i}.mp4" for i in (1, 2, 3, 4)]


def test_une_demande_partielle_prend_le_segment_qui_deborde():
    """Mieux vaut un peu trop que de couper le début du moment."""
    assert len(segments_a_prendre(PLAYLIST, 3.0, BASE)) == 2


def test_une_demande_plus_longue_que_la_fenetre_rend_tout():
    """La playlist d'un direct est une fenêtre glissante — mesurée à 28 s.

    Demander 60 s ne fait pas apparaître du passé qui n'existe plus côté
    serveur : on rend ce qu'il y a, un replay court valant mieux que rien.
    """
    assert len(segments_a_prendre(PLAYLIST, 600.0, BASE)) == 4


@pytest.mark.parametrize("secondes", [0, -1, -30.5])
def test_une_duree_nulle_ou_negative_ne_demande_rien(secondes):
    assert segments_a_prendre(PLAYLIST, secondes, BASE) == []


def test_une_playlist_vide_ne_leve_pas():
    assert segments_a_prendre("", 30.0, BASE) == []
    assert segments_a_prendre("#EXTM3U\n", 30.0, BASE) == []


def test_les_lignes_de_commentaire_ne_sont_pas_prises_pour_des_segments():
    p = "#EXTM3U\n#EXT-X-DISCONTINUITY\n#EXTINF:2.0,\nseg1.mp4\n"
    assert segments_a_prendre(p, 2.0, BASE) == ["https://cdn.test/v1/seg1.mp4"]


def test_sans_base_les_url_restent_relatives():
    assert segments_a_prendre(PLAYLIST, 2.0) == ["seg4.mp4"]


def test_une_duree_illisible_ne_bloque_pas_la_selection():
    p = "#EXTINF:pas un nombre,\nseg1.mp4\n#EXTINF:2.0,\nseg2.mp4\n"
    assert segments_a_prendre(p, 2.0, BASE) == ["https://cdn.test/v1/seg2.mp4"]


# ── fenêtre disponible ───────────────────────────────────────────────────────

def test_duree_disponible():
    assert duree_disponible(PLAYLIST) == pytest.approx(8.0)
    assert duree_disponible("") == 0.0


def test_la_duree_de_replay_tient_dans_ce_que_twitch_garde():
    """Mesuré sur un direct réel : 14 segments de 2 s, soit 28 secondes.

    Annoncer une minute promettrait ce que la source ne peut pas fournir.
    """
    assert REPLAY_SECS <= 30


def test_une_duree_qui_ressemble_a_un_nombre_sans_en_etre_un_ne_casse_pas_tout():
    """Le garde-fou de `_EXTINF` laisse passer « 1.2.3 », et float() explose.

    La sélection se protège déjà d'un `#EXTINF:` illisible (voir
    test_une_duree_illisible_ne_bloque_pas_la_selection), mais uniquement pour
    ce qui NE correspond PAS au motif `[\\d.]+`. Une durée à deux points y
    correspond, n'est pas un flottant, et lève ValueError — depuis une fonction
    que le module présente comme de la logique pure sans panne possible.

    Côté `recuperer` la conséquence est masquée par le `except Exception`, mais
    elle change un replay récupérable en aucun replay, sans qu'on sache pourquoi.
    """
    playlist = "#EXTM3U\n#EXTINF:1.2.3,\nseg1.mp4\n#EXTINF:2.0,\nseg2.mp4\n"
    assert segments_a_prendre(playlist, 30.0, BASE) == [
        "https://cdn.test/v1/seg1.mp4", "https://cdn.test/v1/seg2.mp4"]
    assert duree_disponible(playlist) == pytest.approx(2.0)


# ── résolution de l'URL par streamlink ───────────────────────────────────────

def test_sans_streamlink_installe_aucun_replay_n_est_tente(monkeypatch):
    """Un dépôt cloné n'a pas toujours streamlink : ce n'est pas une panne, et
    surtout il ne faut pas lancer un exécutable dont le chemin est vide."""
    monkeypatch.setattr(rh, "_streamlink_exe", lambda: "")
    monkeypatch.setattr(rh.subprocess, "run", _interdit)

    assert rh._resoudre("zerator") == ""


@pytest.mark.parametrize("panne", [
    OSError("exécutable introuvable"),
    subprocess.TimeoutExpired("streamlink", 15.0),
    subprocess.SubprocessError("erreur interne"),
])
def test_un_streamlink_qui_ne_repond_pas_ne_fait_pas_remonter_l_erreur(
        monkeypatch, panne):
    """Un replay est un agrément : son échec ne doit pas casser la cellule.

    Le délai d'attente est le cas réel — une chaîne lente met plus de quinze
    secondes à résoudre, et `subprocess` lève alors TimeoutExpired.
    """
    monkeypatch.setattr(rh, "_streamlink_exe", lambda: "streamlink")
    monkeypatch.setattr(rh.subprocess, "run", _leve(panne))

    assert rh._resoudre("zerator") == ""


def test_une_chaine_hors_ligne_ne_donne_pas_d_url(monkeypatch):
    """streamlink sort en code 1 en écrivant son message sur stderr : seul le
    code de retour fait foi, jamais le fait que stdout soit non vide."""
    monkeypatch.setattr(rh, "_streamlink_exe", lambda: "streamlink")
    monkeypatch.setattr(rh.subprocess, "run",
                        _rendu(1, "error: No playable streams found"))

    assert rh._resoudre("zerator") == ""


def test_l_url_resolue_perd_son_retour_a_la_ligne(monkeypatch):
    """streamlink termine sa sortie par un saut de ligne : collé à l'URL, il
    donnerait une requête vers un chemin qui n'existe pas."""
    monkeypatch.setattr(rh, "_streamlink_exe", lambda: "streamlink")
    monkeypatch.setattr(rh.subprocess, "run", _rendu(0, f"{BASE}\n"))

    assert rh._resoudre("zerator") == BASE


def test_la_resolution_demande_un_flux_sans_publicite(monkeypatch):
    """Sans `--twitch-disable-ads`, la playlist rendue peut être celle d'une
    coupure publicitaire : le replay montrerait la réclame, pas le moment."""
    vu: dict = {}
    monkeypatch.setattr(rh, "_streamlink_exe", lambda: "streamlink")

    def espion(cmd, **kwargs):
        vu["cmd"] = list(cmd)
        vu["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, BASE, "")

    monkeypatch.setattr(rh.subprocess, "run", espion)
    rh._resoudre("zerator")

    assert "--twitch-disable-ads" in vu["cmd"]
    assert "twitch.tv/zerator" in vu["cmd"]
    assert "--stream-url" in vu["cmd"]
    # La qualité passe par safe_quality : jamais une option déguisée en « -- ».
    assert rh.QUALITE in vu["cmd"]
    assert vu["kwargs"]["timeout"] == rh.TIMEOUT_S


# ── récupération complète ────────────────────────────────────────────────────

@pytest.mark.parametrize("login, secondes", [
    ("", 30.0), ("zerator", 0), ("zerator", -5.0),
])
def test_une_demande_vide_n_appelle_meme_pas_streamlink(monkeypatch, login,
                                                        secondes):
    """Lancer un sous-processus pour rien coûte une seconde d'attente et,
    sous Windows, le risque d'une console qui vole le premier plan."""
    monkeypatch.setattr(rh, "_resoudre", _interdit)

    assert rh.recuperer(login, secondes) == ("", 0.0)


def test_une_chaine_injoignable_rend_un_echec_sans_fichier(monkeypatch, tmp_path):
    """Rien ne doit traîner dans le dossier : l'appelant se rabat sur le
    tampon local, et un fichier orphelin passerait pour un replay valide."""
    monkeypatch.setattr(rh, "_resoudre", lambda login: "")

    assert rh.recuperer("zerator", 30.0, dossier=str(tmp_path)) == ("", 0.0)
    assert list(tmp_path.iterdir()) == []


def test_l_initialisation_est_ecrite_avant_les_fragments(twitch_simule, tmp_path):
    """LE piège du module : Twitch sert du MP4 fragmenté.

    Un fichier qui commence par un fragment n'a ni `ftyp` ni `moov`, et aucun
    lecteur ne l'ouvre — alors que le téléchargement, lui, s'est bien passé.
    """
    twitch_simule({"playlist.m3u8": PLAYLIST, "init.mp4": b"INIT",
                   "seg3.mp4": b"SEG3", "seg4.mp4": b"SEG4"})

    chemin, obtenue = rh.recuperer("zerator", 4.0, dossier=str(tmp_path))

    assert pathlib.Path(chemin).read_bytes() == b"INITSEG3SEG4"
    assert obtenue == pytest.approx(4.0)


def test_le_fichier_porte_le_prefixe_le_login_et_l_extension_du_conteneur(
        twitch_simule, tmp_path):
    """Un clip se garde et se retrouve dans un dossier six mois plus tard : le
    nom doit dire de qui il est, l'extension doit dire ce qu'il contient."""
    twitch_simule({"playlist.m3u8": PLAYLIST, "init.mp4": b"I",
                   "seg4.mp4": b"S"})

    chemin, _ = rh.recuperer("zerator", 2.0, dossier=str(tmp_path),
                             prefixe="clip")
    nom = pathlib.Path(chemin).name

    assert nom.startswith("clip_zerator_")
    assert nom.endswith(".mp4")


def test_un_flux_mpeg_ts_donne_un_fichier_ts(twitch_simule, tmp_path):
    """Sans #EXT-X-MAP, les segments se collent bout à bout : le conteneur
    n'est pas du MP4, et le nommer .mp4 égarerait le diagnostic."""
    playlist = "#EXTM3U\n#EXTINF:2.0,\nseg1.ts\n"
    twitch_simule({"playlist.m3u8": playlist, "seg1.ts": b"TS"})

    chemin, _ = rh.recuperer("zerator", 2.0, dossier=str(tmp_path))

    assert chemin.endswith(".ts")
    assert pathlib.Path(chemin).read_bytes() == b"TS"


def test_la_duree_annoncee_ne_depasse_pas_ce_que_la_playlist_contient(
        twitch_simule, tmp_path):
    """La fenêtre glissante plafonne le replay : annoncer 30 s alors que 8 s
    ont été obtenues ferait afficher une barre de lecture qui ment."""
    twitch_simule({"playlist.m3u8": PLAYLIST, "init.mp4": b"I",
                   "seg1.mp4": b"1", "seg2.mp4": b"2",
                   "seg3.mp4": b"3", "seg4.mp4": b"4"})

    chemin, obtenue = rh.recuperer("zerator", 30.0, dossier=str(tmp_path))

    assert obtenue == pytest.approx(8.0)
    assert pathlib.Path(chemin).read_bytes() == b"I1234"


def test_une_playlist_sans_segment_ne_laisse_pas_de_fichier(twitch_simule,
                                                            tmp_path):
    """Une playlist réduite à ses en-têtes arrive quand le direct vient de
    couper : il n'y a rien à écrire, donc rien à créer."""
    twitch_simule({"playlist.m3u8": "#EXTM3U\n#EXT-X-TARGETDURATION:2\n"})

    assert rh.recuperer("zerator", 30.0, dossier=str(tmp_path)) == ("", 0.0)
    assert list(tmp_path.iterdir()) == []


def test_un_fichier_sans_aucun_fragment_est_supprime_plutot_que_rendu(
        twitch_simule, tmp_path):
    """Le CDN a expiré les segments entre la lecture de la playlist et leur
    téléchargement.

    Le fichier existe alors mais fait zéro octet : le rendre ferait ouvrir à
    mpv un fichier vide — écran noir, aucun message — au lieu du repli sur le
    tampon local.
    """
    twitch_simule({"playlist.m3u8": PLAYLIST, "init.mp4": b"INIT"})

    assert rh.recuperer("zerator", 4.0, dossier=str(tmp_path)) == ("", 0.0)
    assert list(tmp_path.iterdir()) == []


def test_une_panne_reseau_ne_remonte_pas_mais_se_journalise(twitch_simule,
                                                            tmp_path, caplog):
    """Aucune panne ne doit remonter à l'appelant — et aucune ne doit
    disparaître : sans trace, un replay muet est indiagnosticable."""
    def couper(_requete):
        raise httpx.ConnectError("réseau coupé")

    twitch_simule(couper)

    with caplog.at_level(logging.ERROR, logger="core.replay_hd"):
        assert rh.recuperer("zerator", 30.0, dossier=str(tmp_path)) == ("", 0.0)

    assert any("zerator" in enr.getMessage() for enr in caplog.records)


def test_le_dossier_de_destination_est_cree_s_il_manque(twitch_simule, tmp_path):
    """Le dossier de clips vient des réglages et peut n'avoir jamais servi :
    le premier replay ne doit pas échouer sur son absence."""
    cible = tmp_path / "clips" / "2026"
    twitch_simule({"playlist.m3u8": PLAYLIST, "init.mp4": b"I", "seg4.mp4": b"S"})

    chemin, _ = rh.recuperer("zerator", 2.0, dossier=str(cible))

    assert pathlib.Path(chemin).parent == cible


def test_sans_dossier_le_replay_va_dans_le_temporaire(twitch_simule, tmp_path,
                                                      monkeypatch):
    """Appel sans destination : un replay jetable n'a pas à imposer un dossier."""
    monkeypatch.setattr(rh.tempfile, "gettempdir", lambda: str(tmp_path))
    twitch_simule({"playlist.m3u8": PLAYLIST, "init.mp4": b"I", "seg4.mp4": b"S"})

    chemin, _ = rh.recuperer("zerator", 2.0)

    assert pathlib.Path(chemin).parent == tmp_path


# ── téléchargement segment par segment ───────────────────────────────────────

def test_une_initialisation_illisible_condamne_le_replay(tmp_path):
    """Les fragments seuls sont injouables : rendre 0 fait supprimer le
    fichier, plutôt que livrer un MP4 que personne n'ouvrira."""
    fichier = tmp_path / "replay.mp4"
    with _client({"init.mp4": 500, "seg1.mp4": b"S1",
                  "seg2.mp4": b"S2"}) as client:
        ecrits = rh._telecharger(
            client, ["https://cdn.test/v1/seg1.mp4",
                     "https://cdn.test/v1/seg2.mp4"],
            fichier, init="https://cdn.test/v1/init.mp4")

    assert ecrits == 0
    assert fichier.read_bytes() == b""


def test_un_fragment_perdu_ne_condamne_pas_le_replay(tmp_path):
    """Un trou de deux secondes reste regardable ; un replay refusé, non.

    Le CDN rend un 404 sur un segment que la fenêtre glissante vient
    d'expirer : c'est le cas courant, pas l'exception.
    """
    fichier = tmp_path / "replay.mp4"
    urls = [f"https://cdn.test/v1/seg{i}.mp4" for i in (1, 2, 3)]
    with _client({"seg1.mp4": b"S1", "seg2.mp4": 404,
                  "seg3.mp4": b"S3"}) as client:
        ecrits = rh._telecharger(client, urls, fichier)

    assert ecrits == 2
    assert fichier.read_bytes() == b"S1S3"


def test_une_taille_anormale_arrete_le_telechargement(tmp_path, monkeypatch):
    """Garde-fou : plusieurs centaines de mégaoctets ne sont pas un segment de
    deux secondes, et rempliraient le disque sans que rien ne le signale."""
    monkeypatch.setattr(rh, "MAX_OCTETS", 5)
    fichier = tmp_path / "replay.ts"
    urls = [f"https://cdn.test/v1/seg{i}.ts" for i in (1, 2, 3)]
    with _client({"seg1.ts": b"AAA", "seg2.ts": b"BBB",
                  "seg3.ts": b"CCC"}) as client:
        ecrits = rh._telecharger(client, urls, fichier)

    # Le segment qui fait déborder n'est PAS écrit : on s'arrête avant lui.
    assert ecrits == 1
    assert fichier.read_bytes() == b"AAA"


def test_sans_initialisation_le_fichier_commence_par_le_premier_fragment(tmp_path):
    """Chaîne vide = flux MPEG-TS : aucune requête d'initialisation à faire,
    et surtout pas une requête vers l'URL vide."""
    fichier = tmp_path / "replay.ts"
    with _client({"seg1.ts": b"S1", "seg2.ts": b"S2"}) as client:
        ecrits = rh._telecharger(
            client, ["https://cdn.test/v1/seg1.ts",
                     "https://cdn.test/v1/seg2.ts"], fichier)

    assert ecrits == 2
    assert fichier.read_bytes() == b"S1S2"
