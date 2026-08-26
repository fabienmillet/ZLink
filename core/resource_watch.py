# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Surveillance des ressources — prévenir quand la machine sature.

ZLink décode vingt-cinq flux en matériel : c'est de loin ce qui coûte le plus
cher sur le poste, et rien dans l'interface ne le disait. Quand le processeur
ou le décodeur vidéo arrive à saturation, l'image saccade, les flux décrochent,
et l'utilisateur n'a aucune raison de faire le lien avec le nombre de flux
qu'il a ouverts.

Le principe est celui de toutes les alertes du projet : ne rien dire tant qu'il
reste de la marge, et ne parler qu'une fois, au bon moment.

DEUX CONDITIONS, et les deux comptent :
  - la ressource est réellement saturée à l'échelle de la MACHINE ;
  - ZLink en est le principal responsable.

Sans la seconde, un export vidéo lancé à côté déclencherait un conseil de
baisser le nombre de flux qui n'y changerait rien.

La mesure est séparée de la décision : `_Detecteur` est de la logique pure,
testable sans plateforme, et les sondes ctypes vivent dans leurs propres
fonctions, inertes là où le système ne les fournit pas.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass

from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)

#: Au-dessus, la ressource est considérée saturée à l'échelle de la machine.
SEUIL_SATURATION = 90.0
#: Part minimale de ZLink dans cette saturation pour que le conseil ait un sens.
SEUIL_PART_ZLINK = 50.0
#: Relevés consécutifs saturés avant d'alerter. Un pic isolé — un changement de
#: flux, une pub qui démarre — ne doit pas déclencher.
RELEVES_CONSECUTIFS = 3
#: Entre deux alertes sur la même ressource. La situation ne se corrige pas en
#: dix secondes, et répéter le conseil ne le rend pas plus utile.
COOLDOWN_S = 600.0
#: Période d'échantillonnage.
PERIODE_S = 5.0


@dataclass
class Releve:
    """Un instantané. `None` quand la mesure n'est pas disponible ici."""
    cpu_systeme: float | None = None
    cpu_zlink: float | None = None
    gpu_systeme: float | None = None
    gpu_zlink: float | None = None


#: Libellés affichés, par ressource.
LIBELLES = {"cpu": "processeur", "gpu": "décodeur vidéo"}


class _Detecteur:
    """Décide s'il faut alerter, et sur quelle ressource.

    Logique pure : aucun appel système, aucune horloge implicite. L'instant est
    fourni par l'appelant, ce qui rend la fenêtre de silence testable.
    """

    def __init__(self, seuil: float = SEUIL_SATURATION,
                 part_min: float = SEUIL_PART_ZLINK,
                 consecutifs: int = RELEVES_CONSECUTIFS,
                 cooldown: float = COOLDOWN_S) -> None:
        self._seuil = seuil
        self._part_min = part_min
        self._consecutifs = max(1, consecutifs)
        self._cooldown = cooldown
        self._series: dict[str, int] = {"cpu": 0, "gpu": 0}
        self._derniere: dict[str, float] = {}

    def observer(self, releve: Releve, maintenant: float) -> str | None:
        """Renvoie la ressource à signaler, ou None.

        Une seule ressource par relevé : si le processeur ET le décodeur
        saturent, le conseil est le même, et le dire deux fois n'aide pas.
        """
        for ressource, total, part in (
            ("gpu", releve.gpu_systeme, releve.gpu_zlink),
            ("cpu", releve.cpu_systeme, releve.cpu_zlink),
        ):
            if not self._sature(total, part):
                self._series[ressource] = 0
                continue
            self._series[ressource] += 1
            if self._series[ressource] < self._consecutifs:
                continue
            if maintenant - self._derniere.get(ressource, float("-inf")) < self._cooldown:
                continue
            self._derniere[ressource] = maintenant
            self._series[ressource] = 0
            return ressource
        return None

    def _sature(self, total: float | None, part: float | None) -> bool:
        """Mesure indisponible = pas de saturation : on ne devine pas."""
        if total is None or part is None:
            return False
        return total >= self._seuil and part >= self._part_min


# ── Sondes système ───────────────────────────────────────────────────────────
# Chacune renvoie None là où elle ne s'applique pas, plutôt que de lever : une
# surveillance est un agrément, elle ne doit jamais gêner le démarrage.

_WINDOWS = sys.platform == "win32"


class _EtatCpu:
    """Compteurs cumulés du relevé précédent — l'usage se lit sur un écart."""

    def __init__(self) -> None:
        self.systeme_total = 0.0
        self.systeme_actif = 0.0
        self.processus = 0.0
        #: Faux tant qu'aucun relevé n'a encore été pris. Sans ce drapeau, le
        #: premier appel calculait un écart depuis ZÉRO et rendait la charge
        #: CUMULÉE DEPUIS LE DÉMARRAGE de la machine — un chiffre qui n'a rien
        #: à voir avec l'usage courant, et qui pouvait déclencher une alerte.
        self.amorce = False


def _temps_cpu_windows() -> tuple[float, float, float] | None:
    """(total systeme, actif systeme, temps processus), en unités arbitraires."""
    try:
        k32 = ctypes.windll.kernel32
        # Les types sont declares : GetCurrentProcess rend un pseudo-handle -1
        # que ctypes tronquerait en 32 bits, et GetProcessTimes echouerait
        # silencieusement en rendant zero partout.
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.c_void_p] * 4
        # FILETIME est lu comme un entier 64 bits : meme taille, meme encodage.
        repos, noyau, utilisateur = (ctypes.c_ulonglong() for _ in range(3))
        if not k32.GetSystemTimes(ctypes.byref(repos), ctypes.byref(noyau),
                                  ctypes.byref(utilisateur)):
            return None
        total = float(noyau.value + utilisateur.value)
        # Le temps « noyau » INCLUT le repos : l'actif est ce qui reste.
        actif = total - float(repos.value)

        creation, sortie, proc_noyau, proc_user = (ctypes.c_ulonglong()
                                                   for _ in range(4))
        if not k32.GetProcessTimes(k32.GetCurrentProcess(),
                                   ctypes.byref(creation), ctypes.byref(sortie),
                                   ctypes.byref(proc_noyau),
                                   ctypes.byref(proc_user)):
            return None
        return total, actif, float(proc_noyau.value + proc_user.value)
    except (AttributeError, OSError) as exc:
        logger.debug("Temps CPU indisponibles : %s", exc)
        return None


def _temps_cpu_linux() -> tuple[float, float, float] | None:
    try:
        with open("/proc/stat", encoding="ascii") as f:
            champs = [float(x) for x in f.readline().split()[1:]]
        total = sum(champs)
        # La quatrieme colonne de /proc/stat est le temps oisif, la cinquieme
        # l'attente d'entrees-sorties : le processeur n'y travaille pas non
        # plus. Les noyaux anciens s'arretent avant la cinquieme.
        oisif = champs[3]
        attente_es = champs[4] if len(champs) > 4 else 0.0
        actif = total - oisif - attente_es
        with open(f"/proc/{os.getpid()}/stat", encoding="ascii") as f:
            parts = f.read().rsplit(") ", 1)[1].split()
        # Champs 14 et 15 de /proc/<pid>/stat : temps utilisateur et temps
        # noyau du processus. Ils sont ici aux indices 11 et 12, la coupure sur
        # « ) » ayant deja consomme les treize premiers champs.
        temps_utilisateur = float(parts[11])
        temps_noyau = float(parts[12])
        processus = temps_utilisateur + temps_noyau
        return total, actif, processus
    except (OSError, ValueError, IndexError) as exc:
        logger.debug("Temps CPU indisponibles : %s", exc)
        return None


def lire_cpu(etat: _EtatCpu) -> tuple[float | None, float | None]:
    """(% machine, % de cette charge imputable à ZLink) depuis le dernier appel.

    Le premier appel ne renvoie rien : un pourcentage d'usage se lit sur un
    ÉCART entre deux relevés, pas sur un compteur cumulé.
    """
    brut = _temps_cpu_windows() if _WINDOWS else _temps_cpu_linux()
    if brut is None:
        return None, None
    total, actif, processus = brut
    d_total = total - etat.systeme_total
    d_actif = actif - etat.systeme_actif
    d_proc = processus - etat.processus
    etat.systeme_total, etat.systeme_actif, etat.processus = total, actif, processus
    if not etat.amorce:
        etat.amorce = True
        return None, None
    if d_total <= 0:
        return None, None
    charge = max(0.0, min(100.0, d_actif / d_total * 100.0))
    if d_actif <= 0:
        return charge, 0.0
    part = max(0.0, min(100.0, d_proc / d_actif * 100.0))
    return charge, part


# ── GPU : compteurs de performance Windows ───────────────────────────────────
# `\GPU Engine(*)\Utilization Percentage` nomme ses instances avec le PID du
# processus propriétaire : un seul compteur donne donc le total machine ET la
# part de ZLink, sans avoir à croiser deux sources.

_PDH_FMT_DOUBLE = 0x00000200
_PDH_MORE_DATA = 0x800007D2


class _SondeGpu:
    """Requête PDH ouverte une fois, relue à chaque relevé."""

    def __init__(self) -> None:
        self._pdh = None
        self._requete = ctypes.c_void_p()
        self._compteur = ctypes.c_void_p()
        self._pret = False
        self._amorce = False
        #: Type du moteur le plus charge au dernier releve (« Video Codec 0 »…).
        self.moteur = ""

    def ouvrir(self) -> bool:
        if self._pret or not _WINDOWS:
            return self._pret
        try:
            self._pdh = ctypes.windll.pdh
            if self._pdh.PdhOpenQueryW(
                    None, 0, ctypes.byref(self._requete)) & 0xFFFFFFFF:
                return False
            chemin = r"\GPU Engine(*)\Utilization Percentage"
            if self._pdh.PdhAddEnglishCounterW(
                    self._requete, chemin, 0,
                    ctypes.byref(self._compteur)) & 0xFFFFFFFF:
                logger.debug("Compteur GPU indisponible sur ce poste")
                return False
            self._pdh.PdhCollectQueryData(self._requete)
            self._pret = True
        except (AttributeError, OSError) as exc:
            logger.debug("PDH inaccessible : %s", exc)
            return False
        return self._pret

    def lire(self) -> tuple[float | None, float | None]:
        """(% machine, % de cette charge imputable à ZLink)."""
        if not self.ouvrir():
            return None, None
        try:
            if self._pdh.PdhCollectQueryData(self._requete) & 0xFFFFFFFF:
                return None, None
            if not self._amorce:
                # Le premier relevé n'a pas d'écart : PDH rend zéro partout.
                self._amorce = True
                return None, None
            valeurs = self._valeurs()
            if valeurs is None:
                return None, None
            total, mien, self.moteur = _charge_par_moteur(valeurs, os.getpid())
            if total is None:
                return None, None
            if total <= 0.0:
                return 0.0, 0.0
            return total, max(0.0, min(100.0, mien / total * 100.0))
        except (AttributeError, OSError) as exc:
            logger.debug("Lecture GPU impossible : %s", exc)
            return None, None

    def _valeurs(self) -> list[tuple[str, float]] | None:
        """Toutes les instances du compteur, avec leur nom."""
        taille = ctypes.c_ulong(0)
        nombre = ctypes.c_ulong(0)
        # Les codes PDH sont des HRESULT : ctypes les rend en entier SIGNE, donc
        # 0x800007D2 arrive negatif. Sans le masque, la comparaison echouait et
        # la sonde se declarait indisponible alors qu'elle marchait.
        rc = self._pdh.PdhGetFormattedCounterArrayW(
            self._compteur, _PDH_FMT_DOUBLE, ctypes.byref(taille),
            ctypes.byref(nombre), None) & 0xFFFFFFFF
        if rc != _PDH_MORE_DATA or taille.value == 0:
            return None
        tampon = ctypes.create_string_buffer(taille.value)
        rc = self._pdh.PdhGetFormattedCounterArrayW(
            self._compteur, _PDH_FMT_DOUBLE, ctypes.byref(taille),
            ctypes.byref(nombre), tampon) & 0xFFFFFFFF
        if rc != 0:
            return None
        return _decoder_instances(tampon, nombre.value)


class _ItemPdh(ctypes.Structure):
    """PDH_FMT_COUNTERVALUE_ITEM_W : nom d'instance + valeur formatée."""
    _fields_ = [
        ("szName", ctypes.c_wchar_p),
        ("CStatus", ctypes.c_ulong),
        ("_bourrage", ctypes.c_ulong),
        ("doubleValue", ctypes.c_double),
    ]


def _charge_par_moteur(valeurs: list[tuple[str, float]],
                       pid: int) -> tuple[float | None, float, str]:
    """(charge du moteur le plus occupé, part de ce pid, nom du moteur).

    Le compteur expose une instance par (processus, moteur) : sommer le tout
    donnerait 97 % là où Windows en affiche 63, puisqu'un GPU fait tourner le
    rendu 3D, le décodage vidéo et les copies EN PARALLÈLE. On agrège donc par
    TYPE DE MOTEUR et on retient le plus chargé — c'est ce que montre le
    gestionnaire de tâches, et c'est celui qui sature en premier.

    Pour ZLink, le moteur qui compte est « Video Codec » : vingt-cinq flux
    décodés en matériel y passent tous.
    """
    marque = f"pid_{pid}_"
    total_par_moteur: dict[str, float] = {}
    mien_par_moteur: dict[str, float] = {}
    for nom, valeur in valeurs:
        if "_engtype_" not in nom:
            continue
        moteur = nom.split("_engtype_", 1)[1]
        total_par_moteur[moteur] = total_par_moteur.get(moteur, 0.0) + valeur
        if marque in nom:
            mien_par_moteur[moteur] = mien_par_moteur.get(moteur, 0.0) + valeur
    if not total_par_moteur:
        return None, 0.0, ""
    moteur = max(total_par_moteur, key=lambda m: total_par_moteur[m])
    return (max(0.0, min(100.0, total_par_moteur[moteur])),
            mien_par_moteur.get(moteur, 0.0), moteur)


def _decoder_instances(tampon, nombre: int) -> list[tuple[str, float]]:
    items = ctypes.cast(tampon, ctypes.POINTER(_ItemPdh))
    sortie: list[tuple[str, float]] = []
    for i in range(nombre):
        item = items[i]
        if item.szName:
            sortie.append((item.szName, float(item.doubleValue)))
    return sortie


# ── Surveillance ─────────────────────────────────────────────────────────────

class ResourceWatch(QObject):
    """Échantillonne les ressources et signale une saturation durable.

    Signal : saturation(ressource, pourcentage machine, part de ZLink).
    `ressource` vaut « cpu » ou « gpu » ; voir LIBELLES pour l'affichage.
    """

    saturation = pyqtSignal(str, float, float)

    def __init__(self, parent: QObject | None = None,
                 periode: float = PERIODE_S) -> None:
        super().__init__(parent)
        self._periode = max(1.0, periode)
        self._detecteur = _Detecteur()
        self._etat_cpu = _EtatCpu()
        self._gpu = _SondeGpu()
        self._arret = threading.Event()
        self._fil: threading.Thread | None = None

    def start(self) -> None:
        if self._fil is not None:
            return
        self._fil = threading.Thread(target=self._boucle, daemon=True,
                                     name="resource-watch")
        self._fil.start()

    def stop(self) -> None:
        self._arret.set()

    def releve(self) -> Releve:
        """Un instantané, sans décision. Utile aux réglages et aux tests."""
        cpu_sys, cpu_moi = lire_cpu(self._etat_cpu)
        gpu_sys, gpu_moi = self._gpu.lire()
        return Releve(cpu_systeme=cpu_sys, cpu_zlink=cpu_moi,
                      gpu_systeme=gpu_sys, gpu_zlink=gpu_moi)

    def _boucle(self) -> None:
        # Premier relevé jeté : les compteurs cumulés n'ont pas encore d'écart.
        self.releve()
        while not self._arret.wait(self._periode):
            try:
                r = self.releve()
            except Exception:      # noqa: BLE001 — une sonde ne doit rien casser
                logger.exception("Relevé de ressources impossible")
                continue
            ressource = self._detecteur.observer(r, time.monotonic())
            if ressource is None:
                continue
            total = r.gpu_systeme if ressource == "gpu" else r.cpu_systeme
            part = r.gpu_zlink if ressource == "gpu" else r.cpu_zlink
            logger.warning(
                "Saturation %s : %.0f %% de la machine, dont %.0f %% pour ZLink",
                LIBELLES[ressource], total or 0.0, part or 0.0)
            self.saturation.emit(ressource, total or 0.0, part or 0.0)


def message_conseil(ressource: str, total: float, part: float) -> str:
    """Phrase affichée à l'utilisateur. Dit le constat, puis quoi faire."""
    return (f"{LIBELLES.get(ressource, ressource).capitalize()} à "
            f"{total:.0f} %, dont {part:.0f} % pour ZLink — "
            "réduisez le nombre de flux de la grille ou leur qualité.")
