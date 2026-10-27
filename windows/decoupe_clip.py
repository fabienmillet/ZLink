# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Revoir un moment gardé, en rogner les bords, puis l'enregistrer.

Un clip est pris à chaud : on appuie quand ça se produit, donc toujours un peu
tard, et le tampon rend les dernières secondes en entier. Il commence donc
presque toujours par du remplissage et finit sur autre chose. Cette fenêtre
sert à couper ces deux bouts avant de partager — et surtout à VOIR ce qu'on
s'apprête à partager, ce qui n'était possible nulle part.

**Le fichier existe déjà quand la fenêtre s'ouvre.** `dump-cache` a écrit le
`.ts` à l'instant du clic, et c'est heureux : ce tampon n'aurait pas survécu au
temps de la relecture. On ne décide donc pas s'il faut enregistrer, mais ce
qu'on garde de ce qui est déjà sur le disque. Renoncer laisse le `.ts` en
place ; il n'est jamais effacé sans qu'un fichier valide l'ait remplacé.

**Deux poignées, pas un montage.** Un début, une fin, et rien d'autre : ce
qu'on fait à ce moment-là, c'est retirer l'avant et l'après, pas monter. La
découpe elle-même est une copie de flux, donc instantanée et sans perte — au
prix d'une coupe qui tombe sur l'image-clé la plus proche, à environ deux
secondes près sur un flux Twitch.
"""

from __future__ import annotations

import logging
import pathlib
import threading

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor, QFont
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from core import conversion_clip

logger = logging.getLogger(__name__)

_POLICE_UI = "Segoe UI Variable"
_POLICE_MONO = "Cascadia Code"

#: Cadence de suivi de la lecture. Cinq fois par seconde suffit à ce que le
#: curseur ne saute pas, et ne coûte rien.
_SUIVI_MS = 200

#: Précision des poignées : le centième de seconde. Un curseur entier en
#: secondes rendrait impossible de couper juste avant un mot.
_PAS = 100


def disponible() -> tuple[bool, str]:
    """Peut-on proposer la découpe ? Sinon, pourquoi.

    Elle demande DEUX choses, et l'absence de l'une la rend trompeuse plutôt
    qu'inutile : libmpv pour revoir le clip — sans lui la fenêtre s'ouvre sur
    du noir et des curseurs à zéro — et ffmpeg pour écrire la portion gardée.
    Mieux vaut ne pas ouvrir la fenêtre que d'ouvrir celle-là.
    """
    from widgets.mpv_widget import _MPV_AVAILABLE

    if not _MPV_AVAILABLE:
        return False, "libmpv est absent : le clip ne peut pas être revu."
    if not conversion_clip.disponible():
        return False, "ffmpeg est absent : la découpe est impossible."
    return True, ""


def _mmss(secondes: float) -> str:
    """12.4 → « 0:12 ». Les clips durent une minute, pas une heure."""
    secondes = max(0, int(secondes))
    return f"{secondes // 60}:{secondes % 60:02d}"


class FenetreDecoupe(QDialog):
    """Prévisualise un clip et en rogne les bords avant enregistrement."""

    #: (chemin final, durée gardée) une fois le fichier écrit.
    enregistre = pyqtSignal(str, float)
    #: Interne. La découpe tourne dans un fil, qui ne peut pas toucher aux
    #: widgets : le résultat repasse par Qt pour revenir sur le fil graphique.
    _decoupe = pyqtSignal(str, str, float)

    def __init__(self, chemin: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._source = pathlib.Path(chemin)
        self._duree = 0.0
        self._tenu = False
        self._lecteur = None
        self._decoupe.connect(self._sur_decoupe)
        self.setWindowTitle(f"Découper — {self._source.name}")
        self.resize(940, 620)
        self.setStyleSheet("background: #0a0a0a; color: #ffffff;")
        self._construire()

        self._suivi = QTimer(self)
        self._suivi.setInterval(_SUIVI_MS)
        self._suivi.timeout.connect(self._rafraichir)
        self._suivi.start()
        self._lire()

    # ── construction ────────────────────────────────────────────────────────

    def _construire(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        from widgets.mpv_widget import MpvWidget

        self._lecteur = MpvWidget(self)
        v.addWidget(self._lecteur, stretch=1)
        v.addLayout(self._barre_de_lecture())
        v.addLayout(self._poignees())
        v.addLayout(self._boutons())

    def _barre_de_lecture(self) -> QHBoxLayout:
        h = QHBoxLayout()
        h.setContentsMargins(12, 10, 12, 4)
        h.setSpacing(10)

        self._pause = QPushButton("⏸")
        self._pause.setFixedSize(34, 30)
        self._pause.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._pause.setToolTip("Lecture / pause")
        self._pause.clicked.connect(self._basculer_pause)
        h.addWidget(self._pause)

        self._tete = QSlider(Qt.Orientation.Horizontal)
        self._tete.setRange(0, 0)
        self._tete.setToolTip("Se déplacer dans le clip")
        # Le curseur suit la lecture SAUF pendant qu'on le tient : sans ce
        # drapeau, la position relue cinq fois par seconde le ramènerait
        # sous le doigt.
        self._tete.sliderPressed.connect(lambda: setattr(self, "_tenu", True))
        self._tete.sliderReleased.connect(self._relacher)
        h.addWidget(self._tete, stretch=1)

        self._horloge = QLabel("0:00 / 0:00")
        self._horloge.setFont(QFont(_POLICE_MONO, 10))
        self._horloge.setStyleSheet("color: #888888;")
        h.addWidget(self._horloge)
        return h

    def _poignees(self) -> QHBoxLayout:
        h = QHBoxLayout()
        h.setContentsMargins(12, 0, 12, 4)
        h.setSpacing(10)

        self._debut = self._poignee("Début", h, self._sur_debut)
        self._fin = self._poignee("Fin", h, self._sur_fin)

        self._garde = QLabel("—")
        self._garde.setFont(QFont(_POLICE_MONO, 10))
        self._garde.setStyleSheet("color: #00ff87;")
        h.addWidget(self._garde)
        return h

    def _poignee(self, libelle: str, rangee: QHBoxLayout, sur_change) -> QSlider:
        etiquette = QLabel(libelle)
        etiquette.setFont(QFont(_POLICE_UI, 10))
        etiquette.setStyleSheet("color: #888888;")
        rangee.addWidget(etiquette)

        curseur = QSlider(Qt.Orientation.Horizontal)
        curseur.setRange(0, 0)
        curseur.valueChanged.connect(sur_change)
        rangee.addWidget(curseur, stretch=1)
        return curseur

    def _boutons(self) -> QHBoxLayout:
        h = QHBoxLayout()
        h.setContentsMargins(12, 4, 12, 12)
        h.setSpacing(8)

        self._note = QLabel("Lecture du clip…")
        self._note.setFont(QFont(_POLICE_UI, 10))
        self._note.setStyleSheet("color: #888888;")
        h.addWidget(self._note, stretch=1)

        renoncer = QPushButton("Garder tel quel")
        renoncer.setToolTip(
            "Ferme sans découper. Le clip reste enregistré tel qu'il a été pris.")
        renoncer.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        renoncer.clicked.connect(self.reject)
        h.addWidget(renoncer)

        self._valider = QPushButton("Enregistrer la découpe")
        self._valider.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._valider.setStyleSheet(
            "QPushButton { background: #00ff87; color: #0a0a0a; border: none;"
            " border-radius: 4px; padding: 6px 14px; font-weight: bold; }")
        self._valider.clicked.connect(self._enregistrer)
        # Rien à découper tant que la durée est inconnue : le bouton
        # proposerait une coupe de zéro seconde, refusée plus bas de toute
        # façon, mais après un aller-retour et un message d'erreur.
        self._valider.setEnabled(False)
        h.addWidget(self._valider)
        return h

    # ── lecture ─────────────────────────────────────────────────────────────

    def _lire(self) -> None:
        if self._lecteur is None:
            return
        self._lecteur.play(str(self._source))

    def _basculer_pause(self) -> None:
        if self._lecteur is None:
            return
        en_pause = not self._lecteur.en_pause()
        self._lecteur.set_pause(en_pause)
        self._pause.setText("▶" if en_pause else "⏸")

    def _relacher(self) -> None:
        self._tenu = False
        if self._lecteur is not None:
            self._lecteur.chercher(self._tete.value() / _PAS)

    def _rafraichir(self) -> None:
        """Suit la lecture, et arme les poignées dès que la durée est connue."""
        if self._lecteur is None:
            return
        duree = self._lecteur.duree()
        if duree and duree != self._duree:
            self._armer(duree)
        position = self._lecteur.position()
        if position is None:
            return
        if not self._tenu:
            self._tete.setValue(int(position * _PAS))
        self._horloge.setText(f"{_mmss(position)} / {_mmss(self._duree)}")
        # Rejouer la portion gardée en boucle : c'est ce qu'on veut vérifier,
        # et sortir des bornes ne montrerait que ce qu'on a décidé de jeter.
        if position >= self._fin.value() / _PAS:
            self._lecteur.chercher(self._debut.value() / _PAS)

    def _armer(self, duree: float) -> None:
        """Fixe l'échelle des trois curseurs, une fois la durée connue."""
        self._duree = duree
        pas_max = int(duree * _PAS)
        for curseur in (self._tete, self._debut, self._fin):
            curseur.setRange(0, pas_max)
        self._debut.setValue(0)
        self._fin.setValue(pas_max)
        self._note.setText(
            "La coupe tombe sur l'image-clé la plus proche, à environ deux "
            "secondes près.")

    # ── poignées ────────────────────────────────────────────────────────────

    def _sur_debut(self, valeur: int) -> None:
        # Les poignées ne se croisent pas : celle qu'on ne touche pas cède.
        if valeur >= self._fin.value():
            self._fin.setValue(min(self._fin.maximum(), valeur + _PAS))
        if self._lecteur is not None:
            self._lecteur.chercher(valeur / _PAS)
        self._dire_ce_qui_reste()

    def _sur_fin(self, valeur: int) -> None:
        if valeur <= self._debut.value():
            self._debut.setValue(max(0, valeur - _PAS))
        self._dire_ce_qui_reste()

    def _dire_ce_qui_reste(self) -> None:
        garde = (self._fin.value() - self._debut.value()) / _PAS
        self._garde.setText(f"gardé : {garde:.1f} s")
        self._valider.setEnabled(garde >= 1.0)

    def bornes(self) -> tuple[float, float]:
        """(début, fin) en secondes, telles que les poignées les posent."""
        return self._debut.value() / _PAS, self._fin.value() / _PAS

    # ── enregistrement ──────────────────────────────────────────────────────

    def _enregistrer(self) -> None:
        """Découpe en tâche de fond, sans figer la fenêtre."""
        debut, fin = self.bornes()
        self._valider.setEnabled(False)
        self._valider.setText("Découpe…")

        def _travail() -> None:
            chemin, raison = conversion_clip.decouper(self._source, debut, fin)
            # `_sur_decoupe` touche aux widgets : elle doit repartir sur le fil
            # graphique, d'où le signal plutôt qu'un appel direct.
            self._decoupe.emit(chemin, raison, fin - debut)

        threading.Thread(target=_travail, daemon=True,
                         name="decoupe-clip").start()

    def _sur_decoupe(self, chemin: str, raison: str, duree: float) -> None:
        if not chemin:
            logger.warning("Découpe impossible : %s", raison)
            self._note.setText(raison or "Découpe impossible.")
            self._valider.setText("Enregistrer la découpe")
            self._valider.setEnabled(True)
            return
        logger.info("Clip découpé : %s (%.1f s)", chemin, duree)
        self.enregistre.emit(chemin, duree)
        self.accept()

    def done(self, resultat: int) -> None:  # type: ignore[override]
        """Arrête le lecteur, quelle que soit la porte de sortie.

        `done` et non `closeEvent` : `accept()` et `reject()` ne ferment pas
        la fenêtre, ils la MASQUENT, et le rappel de fermeture n'était donc
        jamais appelé. Le son du clip continuait après l'enregistrement — il
        n'y avait plus de fenêtre, mais mpv jouait toujours.

        Les trois sorties passent ici : le bouton d'enregistrement, « Garder
        tel quel », et la croix, dont l'implémentation par défaut de QDialog
        appelle `reject()`.
        """
        self._suivi.stop()
        if self._lecteur is not None:
            # `shutdown` et non `stop` : la fenêtre ne resservira pas, et il
            # libère aussi le contexte de rendu et le processus mpv.
            self._lecteur.shutdown()
            self._lecteur = None
        super().done(resultat)
