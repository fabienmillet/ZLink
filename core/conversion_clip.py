# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Clips `.ts` convertis en `.mp4`, pour pouvoir les partager.

ZLink écrit ses clips avec `dump-cache`, qui rend du MPEG-TS : c'est le
conteneur du flux Twitch, et le seul qu'on puisse découper en plein milieu sans
rien réparer. Il se lit très bien, mais il ne se partage pas — Discord ne
l'affiche pas en ligne, un téléphone ne l'ouvre pas, et l'envoyer revient à
demander à quelqu'un d'installer VLC.

**Une copie, pas un ré-encodage.** Les pistes d'un `.ts` Twitch sont déjà du
H.264 et de l'AAC, exactement ce qu'un `.mp4` transporte : il n'y a rien à
recalculer, seulement à ré-emballer. `-c copy` fait ça en une fraction de
seconde et sans perdre une image. Ré-encoder prendrait des minutes par clip et
dégraderait ce qu'on veut montrer.

**Pourquoi ffmpeg et pas libmpv.** La libmpv embarquée sait encoder, mais pas
recopier : `--ovc=copy` lui répond `codec 'copy' not found` (vérifié). Le
remux demande donc le binaire ffmpeg. Il n'est pas livré avec ZLink : quand il
manque, la conversion est simplement indisponible et le `.ts` reste lisible
dans l'application — c'est un agrément de partage, pas une fonction vitale.
"""

from __future__ import annotations

import logging
import pathlib
import shutil
import subprocess
import threading

from core.paths import RESOURCE_ROOT
from core.sous_processus import sans_fenetre

logger = logging.getLogger(__name__)

#: Un remux ne relit pas la vidéo, il recopie des paquets : même un clip de
#: plusieurs minutes tient en quelques secondes. Au-delà, quelque chose est
#: bloqué — mieux vaut rendre la main que laisser un processus en plan.
TIMEOUT_S = 120


def _ffmpeg() -> str:
    """Chemin du binaire ffmpeg, ou '' s'il n'y en a pas.

    Le dossier de l'application d'abord : si un jour ffmpeg est livré avec
    ZLink, c'est là qu'il sera, et il doit primer sur celui du système.

    Jamais un nom nu : sous Windows, `CreateProcess` résout un nom sans chemin
    depuis le dossier courant AVANT le PATH — c'est la même précaution que
    pour streamlink.
    """
    for nom in ("ffmpeg.exe", "ffmpeg"):
        candidat = RESOURCE_ROOT / nom
        if candidat.is_file():
            return str(candidat)
    return shutil.which("ffmpeg") or ""


def disponible() -> bool:
    """La conversion est-elle possible sur cette machine ?"""
    return bool(_ffmpeg())


def destination(source: str | pathlib.Path) -> pathlib.Path:
    """Le `.mp4` correspondant, sans écraser un fichier déjà là.

    `clip_193012.ts` donne `clip_193012.mp4`, puis `clip_193012-2.mp4` si le
    premier existe. Convertir deux fois le même clip ne doit pas effacer
    silencieusement la conversion précédente — le suffixe coûte moins cher
    qu'un enregistrement perdu.
    """
    src = pathlib.Path(source)
    cible = src.with_suffix(".mp4")
    rang = 2
    while cible.exists():
        cible = src.with_name(f"{src.stem}-{rang}.mp4")
        rang += 1
    return cible


def convertir(source: str | pathlib.Path,
              cible: str | pathlib.Path | None = None) -> tuple[str, str]:
    """Ré-emballe `source` en MP4. Rend (chemin, '') ou ('', raison de l'échec).

    Bloquant — à appeler depuis un fil, jamais depuis le fil graphique : même
    rapide, un remux n'est pas instantané et figerait l'affichage.

    Ne lève jamais : la raison est rendue en clair, pour être montrée telle
    quelle à qui a cliqué.
    """
    src = pathlib.Path(source)
    if not src.is_file():
        return "", f"Fichier introuvable : {src.name}"

    exe = _ffmpeg()
    if not exe:
        return "", ("ffmpeg est introuvable. Il est nécessaire pour convertir "
                    "un clip en MP4 — le .ts reste lisible dans ZLink.")

    sortie = pathlib.Path(cible) if cible is not None else destination(src)
    try:
        sortie.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return "", f"Dossier de destination inaccessible : {exc}"

    resultat = subprocess.run(
        [
            exe, "-hide_banner", "-loglevel", "error",
            # -y : la destination a été choisie libre juste au-dessus, mais
            # entre-temps un autre clip a pu la prendre. Sans -y, ffmpeg
            # attendrait une réponse sur une entrée standard qui n'existe pas.
            "-y", "-i", str(src),
            "-c", "copy",
            # Les flux Twitch portent parfois des pistes de données que le
            # conteneur MP4 refuse : les écarter plutôt que faire échouer
            # toute la conversion pour une piste dont personne ne veut.
            "-dn", "-map", "0:v:0", "-map", "0:a?",
            # L'index déplacé en tête : sans lui, un MP4 doit être téléchargé
            # en entier avant de commencer à jouer. C'est toute la différence
            # entre un clip qui se lit dans Discord et un clip qu'on télécharge.
            "-movflags", "+faststart",
            str(sortie),
        ],
        capture_output=True, text=True, timeout=TIMEOUT_S,
        **sans_fenetre(),
    )

    if resultat.returncode != 0 or not sortie.is_file():
        detail = (resultat.stderr or "").strip().splitlines()
        raison = detail[-1] if detail else f"ffmpeg a rendu {resultat.returncode}"
        logger.error("Conversion de %s : %s", src.name, raison)
        # Un fichier partiel serait pris pour une conversion réussie par
        # `destination()` au prochain essai, et resterait sur le disque.
        try:
            sortie.unlink(missing_ok=True)
        except OSError as menage:
            logger.debug("Sortie partielle non effacée — %s", menage)
        return "", raison

    logger.info("Clip converti : %s → %s", src.name, sortie.name)
    return str(sortie), ""


def convertir_en_arriere_plan(source: str | pathlib.Path,
                              supprimer_source: bool = False) -> None:
    """Lance la conversion dans un fil et rend la main tout de suite.

    Pensée pour le rappel `clip_saved` : celui-ci part du fil graphique, en
    pleine soirée, souvent avec vingt-cinq flux en cours. Y bloquer, même une
    demi-seconde, se verrait à l'écran.

    Le `.ts` d'origine n'est effacé QUE si le MP4 existe vraiment. Un
    remux raté ne doit jamais coûter l'enregistrement.
    """
    def _travail() -> None:
        chemin, raison = convertir(source)
        if not chemin:
            logger.warning("Clip non converti (%s) — le .ts est conservé", raison)
            return
        if supprimer_source:
            try:
                pathlib.Path(source).unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("Source .ts non effacée — %s", exc)

    threading.Thread(target=_travail, daemon=True,
                     name="conversion-clip").start()
