# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Conversion des clips .ts en .mp4.

ffmpeg n'est pas lancé : `subprocess.run` est remplacé par un double qui écrit
le fichier attendu, ou échoue à la demande. Ce qui est vérifié, c'est le choix
de la destination, le refus d'écraser, et surtout qu'un remux raté ne coûte
jamais le .ts d'origine.
"""

from __future__ import annotations

import subprocess

import pytest

from core import conversion_clip as C


@pytest.fixture
def clip(tmp_path):
    """Un .ts sur le disque, avec du contenu pour le distinguer d'un vide."""
    source = tmp_path / "clip_193012.ts"
    source.write_bytes(b"\x47" * 512)          # 0x47 : octet de tête MPEG-TS
    return source


@pytest.fixture
def ffmpeg(monkeypatch):
    """Détourne l'appel à ffmpeg. Rend un contrôleur du faux binaire."""
    monkeypatch.setattr(C, "_ffmpeg", lambda: "/usr/bin/ffmpeg")
    etat = {"code": 0, "stderr": "", "ecrire": True, "argv": None}

    def _run(argv, **_kw):
        etat["argv"] = argv
        if etat["ecrire"]:
            import pathlib
            pathlib.Path(argv[-1]).write_bytes(b"mp4")
        return subprocess.CompletedProcess(argv, etat["code"], "", etat["stderr"])

    monkeypatch.setattr(C.subprocess, "run", _run)
    return etat


# ── choix de la destination ──────────────────────────────────────────────────

def test_le_mp4_prend_le_nom_du_ts(clip):
    assert C.destination(clip).name == "clip_193012.mp4"


def test_une_conversion_deja_faite_n_est_pas_ecrasee(clip):
    """Convertir deux fois le même clip ne doit pas effacer le premier MP4."""
    clip.with_suffix(".mp4").write_bytes(b"deja la")
    assert C.destination(clip).name == "clip_193012-2.mp4"
    (clip.parent / "clip_193012-2.mp4").write_bytes(b"aussi")
    assert C.destination(clip).name == "clip_193012-3.mp4"


# ── conversion ───────────────────────────────────────────────────────────────

def test_la_conversion_rend_le_chemin_du_mp4(clip, ffmpeg):
    chemin, raison = C.convertir(clip)
    assert raison == ""
    assert chemin.endswith("clip_193012.mp4")


def test_les_flux_sont_recopies_jamais_reencodes(clip, ffmpeg):
    """Ré-encoder prendrait des minutes et dégraderait ce qu'on veut montrer."""
    C.convertir(clip)
    argv = ffmpeg["argv"]
    assert "-c" in argv and argv[argv.index("-c") + 1] == "copy"


def test_l_index_est_place_en_tete(clip, ffmpeg):
    """Sans faststart, un MP4 doit être téléchargé en entier avant de jouer —
    c'est la différence entre un clip qui se lit dans Discord et un qu'on
    télécharge."""
    C.convertir(clip)
    argv = ffmpeg["argv"]
    assert argv[argv.index("-movflags") + 1] == "+faststart"


# ── échecs ───────────────────────────────────────────────────────────────────

def test_sans_ffmpeg_la_raison_est_dite_en_clair(clip, monkeypatch):
    monkeypatch.setattr(C, "_ffmpeg", lambda: "")
    chemin, raison = C.convertir(clip)
    assert chemin == ""
    assert "ffmpeg" in raison


def test_un_fichier_absent_est_refuse_avant_ffmpeg(tmp_path, ffmpeg):
    chemin, raison = C.convertir(tmp_path / "fantome.ts")
    assert chemin == "" and "introuvable" in raison
    assert ffmpeg["argv"] is None, "ffmpeg n'avait pas à être lancé"


def test_un_echec_de_ffmpeg_remonte_sa_derniere_ligne(clip, ffmpeg):
    ffmpeg.update(code=1, ecrire=False, stderr="bruit\nInvalid data found")
    chemin, raison = C.convertir(clip)
    assert chemin == "" and raison == "Invalid data found"


def test_une_sortie_partielle_est_effacee(clip, ffmpeg):
    """Sinon `destination()` la prendrait pour une conversion réussie au
    prochain essai, et elle resterait sur le disque."""
    ffmpeg.update(code=1, ecrire=True)
    C.convertir(clip)
    assert not clip.with_suffix(".mp4").exists()


def test_un_code_zero_sans_fichier_reste_un_echec(clip, ffmpeg):
    """ffmpeg peut rendre 0 en n'écrivant rien : c'est le fichier qui tranche."""
    ffmpeg.update(code=0, ecrire=False)
    chemin, _ = C.convertir(clip)
    assert chemin == ""


# ── le .ts d'origine ─────────────────────────────────────────────────────────

def _attendre_le_fil():
    import threading
    for fil in threading.enumerate():
        if fil.name == "conversion-clip":
            fil.join(timeout=10)


def test_le_ts_est_efface_apres_une_conversion_reussie(clip, ffmpeg):
    C.convertir_en_arriere_plan(clip, supprimer_source=True)
    _attendre_le_fil()
    assert clip.with_suffix(".mp4").exists()
    assert not clip.exists()


def test_un_remux_rate_ne_coute_jamais_l_enregistrement(clip, ffmpeg):
    """C'est la garantie qui compte : le clip est irremplaçable, pas le MP4."""
    ffmpeg.update(code=1, ecrire=False)
    C.convertir_en_arriere_plan(clip, supprimer_source=True)
    _attendre_le_fil()
    assert clip.exists()


def test_sans_demande_le_ts_reste(clip, ffmpeg):
    C.convertir_en_arriere_plan(clip, supprimer_source=False)
    _attendre_le_fil()
    assert clip.exists() and clip.with_suffix(".mp4").exists()


# ── le clip est-il seulement signalé ? ───────────────────────────────────────

def test_le_plein_ecran_annonce_ses_clips():
    """Le bug qu'on ne voit pas : `_save_clip` écrivait le fichier sans rien
    émettre. Les clips du plein écran — la touche « C », le geste le plus
    courant en régie — n'étaient donc ni convertis, ni comptés dans le récap.

    Un test de source : instancier la fenêtre ouvrirait un plein écran et un
    lecteur mpv sur la machine de qui lance les tests.
    """
    import ast
    import pathlib as _p

    arbre = ast.parse((_p.Path(__file__).parent.parent
                       / "windows" / "fullscreen.py").read_text(encoding="utf-8"))
    classe = next(n for n in ast.walk(arbre)
                  if isinstance(n, ast.ClassDef) and n.name == "FullscreenWindow")
    assert any(isinstance(n, ast.Assign)
               and any(getattr(c, "id", "") == "clip_saved" for c in n.targets)
               for n in classe.body), "FullscreenWindow n'a plus de signal clip_saved"

    methode = next(n for n in classe.body
                   if isinstance(n, ast.FunctionDef) and n.name == "_save_clip")
    emissions = [n for n in ast.walk(methode)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "emit"]
    assert emissions, "_save_clip écrit un clip sans l'annoncer à personne"


def test_les_deux_sources_de_clips_sont_branchees():
    """Grille ET plein écran : oublier l'une des deux est exactement ce qui
    s'est produit."""
    import pathlib as _p

    source = (_p.Path(__file__).parent.parent / "main.py").read_text(encoding="utf-8")
    for emetteur in ("fullscreen.clip_saved", "grid.grid.clip_saved"):
        assert f"{emetteur}.connect(_convertir_si_demande)" in source, emetteur
        assert f"{emetteur}.connect(_SESSION.add_clip)" in source, emetteur
