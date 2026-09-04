# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Mégaphone du ZEvent — la voix commune du plateau, en fond de régie.

Le mégaphone est le canal audio que l'organisation ouvre pour parler à tout le
monde à la fois : annonces, lancements de shows, passages de relais. En régie,
c'est ce qu'on veut entendre par-dessus les flux qu'on regarde, et sans avoir à
ouvrir un onglet de navigateur à côté.

**Rien qu'un son.** Le flux ne porte qu'une piste Opus de quelques kilobits
(relevé sur le manifeste), pas d'image. Le lecteur est donc créé avec
`video=False` et `vo=null` : mpv ne s'attache à aucune fenêtre, ne demande
aucun contexte graphique, et ne peut pas surgir à l'écran comme le fait un
lecteur mal embarqué.

**Pourquoi ici et pas dans core/.** Le module a besoin de la libmpv livrée avec
l'application, dont l'amorçage — chemin, détournement de `find_library` — se
fait à l'import de `mpv_widget`. Le refaire ailleurs, c'est le voir diverger ;
l'importer depuis `core/` mettrait `core` sous la dépendance de `widgets`. Le
mégaphone reste donc dans le même paquet que le lecteur dont il emprunte
l'installation, bien qu'il ne soit pas lui-même un widget.
"""

from __future__ import annotations

import logging
import time

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from widgets.mpv_widget import _MPV_AVAILABLE, _mpv_module, MpvWidget

logger = logging.getLogger(__name__)

#: Le canal, tel que l'organisation le publie.
URL = "https://megaphone-api.zevent.fr/megaphone/index.m3u8?cookieCheck=1"

#: Tampon de lecture. Le mégaphone sert à ENTENDRE une annonce pendant qu'elle
#: est faite : trente secondes d'avance en feraient un décalage de trente
#: secondes sur le plateau. Deux suffisent à absorber le réseau.
_TAMPON_S = 2

#: Filtre d'analyse, ÉTIQUETÉ : la mesure ne se lit que sous la forme
#: `af-metadata/<label>`. Même construction que pour les cellules de grille.
_FILTRE_NIVEAU = "@zl:lavfi=[astats=metadata=1:reset=1:length=0.3]"

#: Cadence de relève du niveau. Trois fois par seconde : de quoi voir une
#: annonce commencer sans réveiller l'application pour rien.
_RELEVE_MS = 300

#: Au-dessus, on considère que quelqu'un parle. Le silence d'un flux ouvert
#: n'est pas un zéro absolu — il porte le bruit de fond de la régie et de
#: l'encodeur — d'où un seuil nettement au-dessus de rien, et nettement en
#: dessous d'une voix.
_SEUIL_PAROLE_DB = -45.0

#: Temps pendant lequel on continue d'annoncer « ça parle » après une baisse.
#: Une phrase est pleine de silences : sans ce maintien, l'étiquette
#: clignoterait entre chaque mot.
_MAINTIEN_S = 2.0


class Megaphone(QObject):
    """Le canal audio du plateau, allumé ou éteint.

    Le lecteur n'existe QUE pendant l'écoute : couper le mégaphone détruit
    l'instance mpv plutôt que de la mettre en pause. Une pause laisserait un
    lecteur, ses fils et sa connexion ouverts toute la soirée pour un son que
    personne n'écoute.
    """

    #: Le mégaphone joue (True) ou s'est tu (False).
    etat_change = pyqtSignal(bool)
    #: Le flux n'a pas pu être ouvert, avec la raison à montrer.
    echec = pyqtSignal(str)
    #: Quelqu'un parle (True) ou le canal est retombé au silence (False).
    parole = pyqtSignal(bool)

    def __init__(self, parent: QObject | None = None, *, url: str = URL) -> None:
        super().__init__(parent)
        self._url = url
        self._player = None
        self._volume = 100
        self._parle = False
        self._derniere_parole = 0.0
        self._sonde = QTimer(self)
        self._sonde.setInterval(_RELEVE_MS)
        self._sonde.timeout.connect(self._relever_le_niveau)

    @property
    def disponible(self) -> bool:
        """Sans libmpv, le mégaphone n'est pas proposé du tout."""
        return _MPV_AVAILABLE and _mpv_module is not None

    @property
    def actif(self) -> bool:
        return self._player is not None

    def basculer(self, actif: bool) -> bool:
        """Allume ou éteint. Rend l'état RÉELLEMENT obtenu.

        La valeur rendue n'est pas celle demandée : c'est ce qui permet au
        bouton de se relever tout seul quand le flux refuse de s'ouvrir,
        plutôt que de rester enfoncé sur un silence.
        """
        if actif:
            return self.demarrer()
        self.arreter()
        return False

    def demarrer(self) -> bool:
        """Ouvre le flux. Rend False, et dit pourquoi, en cas d'échec."""
        if self._player is not None:
            return True
        if not self.disponible:
            self.echec.emit("libmpv est absent : le mégaphone est indisponible.")
            return False
        # libmpv part en segfault sous une locale à virgule décimale, et ce
        # lecteur peut naître hors du chemin qui la règle au démarrage — c'est
        # arrivé au premier essai. La même garantie que MpvWidget, empruntée
        # plutôt que recopiée : deux versions divergeraient.
        MpvWidget._garantir_locale_c()
        try:
            self._player = _mpv_module.MPV(
                video=False, vo="null", really_quiet=True,
                # Les mêmes précautions que pour les cellules de grille : ni
                # scripts, ni raccourcis, ni curseur. Un lecteur de fond n'a
                # aucune interface à offrir.
                osc=False, load_scripts=False, input_default_bindings=False,
                input_vo_keyboard=False,
                cache_pause=False, demuxer_readahead_secs=_TAMPON_S,
                volume=self._volume,
                # Le mégaphone est muet la plupart du temps : sans mesure, on
                # ne saurait pas si le canal est ouvert et calme, ou en panne.
                af=_FILTRE_NIVEAU,
            )
            self._player.play(self._url)
        except Exception as exc:      # noqa: BLE001 — agrément, jamais fatal
            logger.exception("Mégaphone : ouverture impossible")
            self._player = None
            self.echec.emit(f"Mégaphone indisponible — {exc}")
            return False
        logger.info("Mégaphone allumé")
        self._sonde.start()
        self.etat_change.emit(True)
        return True

    def arreter(self) -> None:
        """Coupe le son et libère le lecteur. Sans effet s'il est déjà éteint."""
        self._sonde.stop()
        lecteur, self._player = self._player, None
        if lecteur is None:
            return
        self._annoncer_la_parole(False)
        try:
            lecteur.terminate()
        except Exception as exc:      # noqa: BLE001 — on l'abandonne de toute façon
            logger.debug("Mégaphone : arrêt imparfait — %s", exc)
        logger.info("Mégaphone éteint")
        self.etat_change.emit(False)

    def _relever_le_niveau(self) -> None:
        """Décide si le canal parle, d'après le niveau RMS de la piste.

        Le maintien est ce qui rend l'indication lisible : une phrase est
        pleine de silences, et sans lui l'étiquette clignoterait entre chaque
        mot. Il ne joue QUE dans le sens de l'extinction — dès que le niveau
        remonte, la parole est annoncée sans délai.
        """
        rms = self._niveau_db()
        if rms is not None and rms > _SEUIL_PAROLE_DB:
            self._derniere_parole = time.monotonic()
            self._annoncer_la_parole(True)
        elif self._parle and (time.monotonic() - self._derniere_parole
                              > _MAINTIEN_S):
            self._annoncer_la_parole(False)

    def _niveau_db(self) -> float | None:
        """Niveau RMS en dBFS, ou None tant que le filtre n'a rien à dire.

        None couvre trois cas qu'on traite pareil : le flux n'a pas encore
        démarré, le filtre n'a pas publié de mesure, et le silence PARFAIT —
        que ffmpeg rend en « -inf », qui n'est pas un flottant.
        """
        if self._player is None:
            return None
        try:
            mesures = self._player._get_property("af-metadata/zl")
            brut = (mesures or {}).get("lavfi.astats.Overall.RMS_level", "")
            if not brut or brut == "-inf":
                return None
            return float(brut)
        except Exception:      # noqa: BLE001 — indication d'agrément
            return None

    def _annoncer_la_parole(self, parle: bool) -> None:
        """Émet le changement, et lui seul : le relevé passe trois fois par
        seconde, réémettre à chaque fois repeindrait l'en-tête pour rien."""
        if parle == self._parle:
            return
        self._parle = parle
        self.parole.emit(parle)

    def set_volume(self, volume: int) -> None:
        """Volume 0-100. Retenu même à l'arrêt, pour la prochaine écoute."""
        self._volume = max(0, min(100, int(volume)))
        if self._player is None:
            return
        try:
            self._player.volume = self._volume
        except Exception as exc:      # noqa: BLE001 — réglage d'agrément
            logger.debug("Mégaphone : volume non appliqué — %s", exc)
