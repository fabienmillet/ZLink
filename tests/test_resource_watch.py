# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Surveillance des ressources : décision d'alerte, sondes et boucle.

L'enjeu tient en une phrase : ne conseiller de baisser le nombre de flux que
lorsque c'est vraiment ZLink qui sature la machine. Une alerte injustifiée
apprend à l'utilisateur à ignorer les suivantes.

Rien ici ne mesure la machine et rien ne démarre de fil : kernel32, PDH, /proc
et l'attente de la boucle sont tous simulés. Un test qui lirait les vrais
compteurs dépendrait de la charge du poste qui l'exécute — et ne tournerait
pas du tout sur l'intégration continue, qui est sous Linux.
"""

from __future__ import annotations

import builtins
import ctypes
import io
import logging
import os
import threading

import pytest

from core import resource_watch as rw
from core.resource_watch import (
    LIBELLES,
    Releve,
    ResourceWatch,
    _charge_par_moteur,
    _decoder_instances,
    _Detecteur,
    _EtatCpu,
    _ItemPdh,
    _SondeGpu,
    _temps_cpu_linux,
    _temps_cpu_windows,
    lire_cpu,
    message_conseil,
)


def _sature(**kw) -> Releve:
    """Relevé saturé sur les deux ressources, sauf indication contraire."""
    base = dict(cpu_systeme=95.0, cpu_zlink=80.0,
                gpu_systeme=95.0, gpu_zlink=80.0)
    base.update(kw)
    return Releve(**base)


# ── il faut que ça dure ──────────────────────────────────────────────────────

def test_un_pic_isole_ne_declenche_rien():
    """Un changement de flux ou une pub qui démarre fait un pic d'une seconde.

    Alerter dessus rendrait le conseil inutilisable.
    """
    d = _Detecteur(consecutifs=3)
    assert d.observer(_sature(), 0.0) is None
    assert d.observer(Releve(cpu_systeme=10.0, cpu_zlink=5.0,
                             gpu_systeme=10.0, gpu_zlink=5.0), 1.0) is None
    assert d.observer(_sature(), 2.0) is None


def test_une_saturation_qui_dure_finit_par_alerter():
    d = _Detecteur(consecutifs=3)
    assert d.observer(_sature(), 0.0) is None
    assert d.observer(_sature(), 5.0) is None
    assert d.observer(_sature(), 10.0) == "gpu"


def test_la_serie_repart_de_zero_apres_une_accalmie():
    d = _Detecteur(consecutifs=3)
    d.observer(_sature(), 0.0)
    d.observer(_sature(), 5.0)
    d.observer(Releve(gpu_systeme=10.0, gpu_zlink=5.0), 10.0)   # accalmie
    assert d.observer(_sature(), 15.0) is None, "la série doit repartir de zéro"


# ── les deux conditions ──────────────────────────────────────────────────────

def test_une_machine_saturee_par_AUTRE_CHOSE_ne_declenche_pas():
    """Un export vidéo lancé à côté ne se corrige pas en fermant des cellules."""
    d = _Detecteur(consecutifs=1)
    assert d.observer(_sature(gpu_zlink=5.0, cpu_zlink=5.0), 0.0) is None


def test_zlink_gourmand_sur_une_machine_qui_respire_ne_declenche_pas():
    """80 % d'une machine à 40 % de charge : il reste de la marge."""
    d = _Detecteur(consecutifs=1)
    assert d.observer(Releve(gpu_systeme=40.0, gpu_zlink=80.0,
                             cpu_systeme=40.0, cpu_zlink=80.0), 0.0) is None


@pytest.mark.parametrize("total,part,attendu", [
    (90.0, 50.0, "gpu"),      # bornes incluses des deux côtés
    (89.9, 50.0, None),
    (90.0, 49.9, None),
    (100.0, 100.0, "gpu"),
])
def test_bornes_des_seuils(total, part, attendu):
    d = _Detecteur(consecutifs=1)
    assert d.observer(Releve(gpu_systeme=total, gpu_zlink=part), 0.0) == attendu


def test_une_mesure_indisponible_n_est_pas_une_saturation():
    """On ne devine pas : sans mesure, pas d'alerte."""
    d = _Detecteur(consecutifs=1)
    assert d.observer(Releve(), 0.0) is None
    assert d.observer(Releve(gpu_systeme=99.0, gpu_zlink=None), 0.0) is None
    assert d.observer(Releve(gpu_systeme=None, gpu_zlink=99.0), 0.0) is None


# ── ne pas répéter ───────────────────────────────────────────────────────────

def test_le_conseil_n_est_pas_repete_en_boucle():
    """La situation ne se corrige pas en dix secondes.

    Redire la même chose toutes les cinq secondes ne la rendrait pas plus utile.
    """
    d = _Detecteur(consecutifs=1, cooldown=600.0)
    # Uniquement le GPU : avec les deux saturés, le détecteur bascule sur le
    # processeur pendant le silence du GPU, ce qui est un autre comportement.
    gpu_seul = Releve(gpu_systeme=95.0, gpu_zlink=80.0)
    assert d.observer(gpu_seul, 0.0) == "gpu"
    for t in (5.0, 60.0, 599.0):
        assert d.observer(gpu_seul, t) is None
    assert d.observer(gpu_seul, 601.0) == "gpu"


def test_pendant_le_silence_du_gpu_le_processeur_reste_signalable():
    """Deux ressources, deux silences : ce n'est pas la même information.

    Le conseil est le même, mais savoir QUOI sature oriente le réglage — la
    qualité des flux pour le décodeur, leur nombre pour le processeur.
    """
    d = _Detecteur(consecutifs=1, cooldown=600.0)
    assert d.observer(_sature(), 0.0) == "gpu"
    assert d.observer(_sature(), 5.0) == "cpu"
    assert d.observer(_sature(), 10.0) is None, "les deux sont maintenant en silence"


def test_les_deux_ressources_ont_leur_propre_silence():
    d = _Detecteur(consecutifs=1, cooldown=600.0)
    assert d.observer(_sature(), 0.0) == "gpu"
    # Le GPU se calme, le processeur sature : le conseil reste pertinent.
    assert d.observer(Releve(cpu_systeme=95.0, cpu_zlink=80.0), 5.0) == "cpu"


def test_une_seule_ressource_signalee_a_la_fois():
    """Si les deux saturent, le conseil est le même — le dire deux fois n'aide pas."""
    d = _Detecteur(consecutifs=1)
    assert d.observer(_sature(), 0.0) == "gpu"


# ── agrégation GPU ───────────────────────────────────────────────────────────

def _inst(pid: int, moteur: str) -> str:
    return f"pid_{pid}_luid_0x0_0x1_phys_0_eng_0_engtype_{moteur}"


def test_le_moteur_le_plus_charge_l_emporte():
    """Un GPU fait tourner rendu, décodage et copies EN PARALLÈLE.

    Sommer le tout donnait 97 % là où Windows en affiche 63.
    """
    valeurs = [
        (_inst(42, "3D"), 29.4),
        (_inst(42, "Video Codec 0"), 40.0),
        (_inst(99, "Video Codec 0"), 19.1),
        (_inst(7, "Copy"), 0.4),
    ]
    total, mien, moteur = _charge_par_moteur(valeurs, pid=42)
    assert moteur == "Video Codec 0"
    assert total == pytest.approx(59.1)
    assert mien == pytest.approx(40.0), "la part de ce pid sur CE moteur"


def test_la_charge_est_plafonnee_a_cent():
    valeurs = [(_inst(1, "3D"), 80.0), (_inst(2, "3D"), 80.0)]
    total, _mien, _m = _charge_par_moteur(valeurs, pid=1)
    assert total == 100.0


def test_instances_sans_type_de_moteur_ignorees():
    total, _mien, moteur = _charge_par_moteur([("compteur_bizarre", 50.0)], pid=1)
    assert total is None and moteur == ""


def test_aucune_instance():
    assert _charge_par_moteur([], pid=1) == (None, 0.0, "")


def test_un_pid_absent_a_une_part_nulle():
    total, mien, _m = _charge_par_moteur([(_inst(99, "3D"), 50.0)], pid=42)
    assert total == pytest.approx(50.0) and mien == 0.0


# ── lecture CPU ──────────────────────────────────────────────────────────────

def test_le_premier_releve_ne_rend_rien(monkeypatch):
    """Un pourcentage d'usage se lit sur un ÉCART, pas sur un compteur cumulé."""
    monkeypatch.setattr("core.resource_watch._temps_cpu_windows",
                        lambda: (1000.0, 500.0, 100.0))
    monkeypatch.setattr("core.resource_watch._temps_cpu_linux",
                        lambda: (1000.0, 500.0, 100.0))
    assert lire_cpu(_EtatCpu()) == (None, None)


def test_l_usage_se_lit_sur_l_ecart(monkeypatch):
    compteurs = {"v": (1000.0, 500.0, 100.0)}
    monkeypatch.setattr("core.resource_watch._temps_cpu_windows",
                        lambda: compteurs["v"])
    monkeypatch.setattr("core.resource_watch._temps_cpu_linux",
                        lambda: compteurs["v"])
    etat = _EtatCpu()
    lire_cpu(etat)
    # +100 de temps total, dont +75 actif, dont +30 pour nous.
    compteurs["v"] = (1100.0, 575.0, 130.0)
    charge, part = lire_cpu(etat)
    assert charge == pytest.approx(75.0)
    assert part == pytest.approx(40.0), "30 des 75 unités actives"


def test_une_machine_au_repos_donne_une_part_nulle(monkeypatch):
    compteurs = {"v": (1000.0, 500.0, 100.0)}
    monkeypatch.setattr("core.resource_watch._temps_cpu_windows",
                        lambda: compteurs["v"])
    monkeypatch.setattr("core.resource_watch._temps_cpu_linux",
                        lambda: compteurs["v"])
    etat = _EtatCpu()
    lire_cpu(etat)
    compteurs["v"] = (1100.0, 500.0, 100.0)     # rien d'actif
    charge, part = lire_cpu(etat)
    assert charge == 0.0 and part == 0.0


def test_sonde_indisponible(monkeypatch):
    monkeypatch.setattr("core.resource_watch._temps_cpu_windows", lambda: None)
    monkeypatch.setattr("core.resource_watch._temps_cpu_linux", lambda: None)
    assert lire_cpu(_EtatCpu()) == (None, None)


# ── message ──────────────────────────────────────────────────────────────────

def test_le_message_dit_le_constat_puis_quoi_faire():
    m = message_conseil("gpu", 97.0, 82.0)
    assert LIBELLES["gpu"] in m.lower()
    assert "97" in m and "82" in m
    assert "flux" in m, "un constat sans conseil ne sert à rien"


def test_le_message_supporte_une_ressource_inconnue():
    assert "memoire" in message_conseil("memoire", 99.0, 90.0).lower()


# ═══ sondes système ══════════════════════════════════════════════════════════
#
# Rien de ce qui suit n'interroge la machine. Ce module lit des compteurs
# matériels : un test qui les lirait vraiment dépendrait de la charge du poste
# qui l'exécute, et ne prouverait rien. Les DLL Windows et les fichiers /proc
# sont donc simulés, et la plateforme avec eux — l'intégration continue tourne
# sous Linux, où `ctypes.windll` n'existe même pas.


class _FonctionWin32:
    """Fonction ctypes simulée : mémorise ses appels, porte restype/argtypes.

    Les sondes DÉCLARENT les types de ce qu'elles appellent ; sans attributs
    assignables, la déclaration lèverait avant même le premier appel.
    """

    def __init__(self, effet=None, retour: int = 1,
                 leve: BaseException | None = None) -> None:
        self.appels: list[tuple] = []
        self.restype = None
        self.argtypes: list | None = None
        self._effet = effet
        self._retour = retour
        self._leve = leve

    def __call__(self, *args) -> int:
        self.appels.append(args)
        if self._leve is not None:
            raise self._leve
        if self._effet is not None:
            self._effet(*args)
        return self._retour


class _FauxEspace:
    """Porteur d'attributs, façon `windll`, `kernel32` ou `pdh`."""

    def __init__(self, **membres) -> None:
        self.__dict__.update(membres)


class _WindllAbsent:
    """`ctypes.windll` d'un poste où la DLL demandée ne se charge pas."""

    def __init__(self, exception: BaseException) -> None:
        self._exception = exception

    def __getattr__(self, nom: str):
        raise self._exception


class _FauxCtypes:
    """Le vrai ctypes, sauf `windll`.

    Les sondes se servent des types ctypes réels — `c_ulonglong`, `byref`,
    `cast`, `create_string_buffer`. Seul l'aiguillage vers les DLL du système
    est détourné : remplacer ctypes en entier ferait perdre au test toute
    valeur sur la façon dont les tampons sont écrits puis relus.
    """

    def __init__(self, windll) -> None:
        self.windll = windll

    def __getattr__(self, nom: str):
        return getattr(ctypes, nom)


def _ecrire(reference, valeur: float) -> None:
    """Écrit dans une variable passée par référence, comme le ferait Win32."""
    reference._obj.value = int(valeur)


# ── sonde CPU Windows ────────────────────────────────────────────────────────

def _kernel32(repos: float = 100.0, noyau: float = 500.0,
              utilisateur: float = 200.0, proc_noyau: float = 30.0,
              proc_user: float = 70.0, systeme_ok: bool = True,
              processus_ok: bool = True) -> _FauxEspace:
    """kernel32 réduit aux trois fonctions que la sonde CPU appelle."""
    def _systeme(r, n, u) -> None:
        _ecrire(r, repos)
        _ecrire(n, noyau)
        _ecrire(u, utilisateur)

    def _processus(_handle, _creation, _sortie, pn, pu) -> None:
        _ecrire(pn, proc_noyau)
        _ecrire(pu, proc_user)

    return _FauxEspace(
        GetSystemTimes=_FonctionWin32(_systeme, retour=int(systeme_ok)),
        GetProcessTimes=_FonctionWin32(_processus, retour=int(processus_ok)),
        # Le pseudo-handle du processus courant : -1 sur 64 bits.
        GetCurrentProcess=_FonctionWin32(retour=0xFFFFFFFFFFFFFFFF),
    )


@pytest.fixture
def kernel32(monkeypatch):
    """Installe un kernel32 simulé et le rend, pour inspection."""
    def _poser(**kw) -> _FauxEspace:
        k32 = _kernel32(**kw)
        monkeypatch.setattr(rw, "ctypes", _FauxCtypes(_FauxEspace(kernel32=k32)))
        return k32
    return _poser


def test_le_temps_de_repos_est_defalque_du_temps_noyau(kernel32):
    """GetSystemTimes compte l'inactivité DANS le temps noyau.

    La prendre pour du travail donnerait une machine à 100 % en permanence, et
    donc une alerte permanente dès le troisième relevé.
    """
    kernel32(repos=100.0, noyau=500.0, utilisateur=200.0,
             proc_noyau=30.0, proc_user=70.0)
    assert _temps_cpu_windows() == (700.0, 600.0, 100.0)


def test_le_pseudo_handle_du_processus_est_declare_sur_64_bits(kernel32):
    """GetCurrentProcess rend -1, que ctypes tronquerait en 32 bits.

    GetProcessTimes échouerait alors sans le dire, la part de ZLink tomberait à
    zéro, et plus aucune alerte ne se déclencherait — l'app se croirait
    innocente d'une saturation qu'elle cause.
    """
    k32 = kernel32()
    _temps_cpu_windows()
    assert k32.GetCurrentProcess.restype is ctypes.c_void_p
    assert k32.GetProcessTimes.argtypes == [ctypes.c_void_p] * 5
    assert k32.GetProcessTimes.appels[0][0] == 0xFFFFFFFFFFFFFFFF


@pytest.mark.parametrize("panne", ["systeme_ok", "processus_ok"])
def test_un_appel_win32_qui_echoue_ne_rend_aucune_mesure(kernel32, panne):
    """Un zéro passerait pour une machine au repos ; l'absence de mesure, elle,
    fait taire le détecteur — c'est la seule réponse honnête."""
    kernel32(**{panne: False})
    assert _temps_cpu_windows() is None


@pytest.mark.parametrize("panne", [
    AttributeError("windll absent"),
    OSError("kernel32 introuvable"),
])
def test_une_kernel32_inaccessible_ne_leve_pas(monkeypatch, panne):
    """La surveillance est un agrément : son indisponibilité ne doit jamais
    remonter dans la boucle, qui tourne dans un fil sans filet."""
    monkeypatch.setattr(rw, "ctypes", _FauxCtypes(_WindllAbsent(panne)))
    assert _temps_cpu_windows() is None


# ── sonde CPU Linux ──────────────────────────────────────────────────────────

def _stat_processus(utime: str = "1500", stime: str = "500",
                    nom: str = "ZLink") -> str:
    """Une ligne de /proc/<pid>/stat : pid, (nom), état, puis les champs.

    utime et stime sont les 14e et 15e champs du fichier.
    """
    champs = ["S"] + ["0"] * 19
    champs[11] = utime
    champs[12] = stime
    return f"4242 ({nom}) " + " ".join(champs) + "\n"


#: /proc/stat d'une machine à 200 unités actives sur 950.
_PROC_STAT = "cpu  100 20 80 700 50\n"


@pytest.fixture
def proc(monkeypatch):
    """Remplace les deux fichiers /proc lus par la sonde, ou leur panne."""
    def _poser(stat: str | Exception = _PROC_STAT,
               processus: str | Exception | None = None) -> None:
        contenus = {"/proc/stat": stat,
                    f"/proc/{os.getpid()}/stat":
                        _stat_processus() if processus is None else processus}
        vrai_open = builtins.open

        def _open(chemin, *a, **kw):
            contenu = contenus.get(str(chemin))
            if contenu is None:
                return vrai_open(chemin, *a, **kw)
            if isinstance(contenu, Exception):
                raise contenu
            return io.StringIO(contenu)

        monkeypatch.setattr(builtins, "open", _open)
    return _poser


def test_le_repos_et_l_attente_d_entrees_sorties_ne_sont_pas_du_travail(proc):
    """Un processeur qui attend un disque ne décode aucun flux.

    Le compter comme actif gonflerait la charge machine et déclencherait des
    alertes pendant les gros chargements de vignettes.
    """
    proc()
    assert _temps_cpu_linux() == (950.0, 200.0, 2000.0)


def test_un_noyau_sans_colonne_d_attente_reste_lisible(proc):
    """Les noyaux anciens s'arrêtent avant la cinquième colonne.

    Lire un indice absent lèverait, et la sonde deviendrait muette là où elle
    peut parfaitement fonctionner.
    """
    proc(stat="cpu  100 20 80 700\n")
    assert _temps_cpu_linux() == (900.0, 200.0, 2000.0)


def test_un_nom_de_processus_a_parentheses_ne_decale_pas_les_colonnes(proc):
    """Le nom du processus est libre, parenthèses comprises.

    Couper sur la PREMIÈRE parenthèse fermante décalerait toutes les colonnes,
    et le temps CPU du processus serait lu dans un champ sans rapport.
    """
    proc(processus=_stat_processus(nom="Le (vrai) ZLink"))
    assert _temps_cpu_linux() == (950.0, 200.0, 2000.0)


@pytest.mark.parametrize("stat,processus", [
    (FileNotFoundError("/proc/stat"), None),
    (_PROC_STAT, PermissionError("/proc/self/stat")),
])
def test_un_proc_inaccessible_ne_rend_aucune_mesure(proc, stat, processus):
    """Conteneur sans /proc, /proc masqué : la sonde s'abstient au lieu de
    faire tomber la boucle de surveillance."""
    proc(stat=stat, processus=processus)
    assert _temps_cpu_linux() is None


@pytest.mark.parametrize("stat,processus", [
    ("cpu  100 20 pas_un_nombre 700 50\n", None),     # ValueError
    ("cpu  100 20\n", None),                          # IndexError
    (_PROC_STAT, "ligne sans parenthese"),            # IndexError
])
def test_un_proc_illisible_ne_rend_aucune_mesure(proc, stat, processus):
    """Le format de /proc n'est garanti par personne, et un jour il changera.

    Ce jour-là ZLink doit perdre sa surveillance, pas son fil de surveillance.
    """
    proc(stat=stat, processus=processus)
    assert _temps_cpu_linux() is None


# ── aiguillage et écarts ─────────────────────────────────────────────────────

@pytest.mark.parametrize("windows,attendue", [(True, "windows"), (False, "linux")])
def test_la_sonde_cpu_interrogee_suit_la_plateforme(monkeypatch, windows, attendue):
    """Lire /proc sous Windows, ou charger kernel32 sous Linux, ne rend rien de
    bon : la sonde inadaptée ne doit même pas être appelée."""
    appelees: list[str] = []

    def _sonde_windows() -> tuple[float, float, float]:
        appelees.append("windows")
        return 1000.0, 500.0, 100.0

    def _sonde_linux() -> tuple[float, float, float]:
        appelees.append("linux")
        return 1000.0, 500.0, 100.0

    monkeypatch.setattr(rw, "_WINDOWS", windows)
    monkeypatch.setattr(rw, "_temps_cpu_windows", _sonde_windows)
    monkeypatch.setattr(rw, "_temps_cpu_linux", _sonde_linux)
    lire_cpu(_EtatCpu())
    assert appelees == [attendue]


@pytest.mark.parametrize("suivant", [
    (1000.0, 500.0, 100.0),     # compteurs figés : deux relevés au même instant
    (900.0, 400.0, 90.0),       # compteurs qui reculent : horloge réajustée
])
def test_des_compteurs_qui_n_avancent_pas_ne_rendent_aucun_pourcentage(
        monkeypatch, suivant):
    """Sans écart de temps il n'y a rien à diviser : un pourcentage calculé
    là-dessus serait au mieux absurde, au pire une division par zéro."""
    compteurs = {"v": (1000.0, 500.0, 100.0)}
    monkeypatch.setattr(rw, "_temps_cpu_windows", lambda: compteurs["v"])
    monkeypatch.setattr(rw, "_temps_cpu_linux", lambda: compteurs["v"])
    etat = _EtatCpu()
    lire_cpu(etat)
    compteurs["v"] = suivant
    assert lire_cpu(etat) == (None, None)


# ── sonde GPU : compteurs PDH ────────────────────────────────────────────────

#: PDH répond en HRESULT, que ctypes rend en entier SIGNÉ : c'est sous cette
#: forme que le code doit reconnaître PDH_MORE_DATA.
_MORE_DATA_SIGNE = rw._PDH_MORE_DATA - 0x100000000
#: Une erreur PDH quelconque, telle que ctypes la rend : négative elle aussi.
_ECHEC_PDH = 0xC0000BB8 - 0x100000000


class _TableauPdh:
    """PdhGetFormattedCounterArrayW, avec son protocole en deux temps.

    Premier appel sans tampon : PDH répond MORE_DATA et annonce la taille
    nécessaire. Second appel avec le tampon : PDH y écrit ses structures.
    Simuler les deux, tampon compris, est la seule façon d'éprouver le décodage
    réel — une doublure qui rendrait directement une liste Python ne dirait
    rien de l'endroit où les bugs de ce module se logent.
    """

    def __init__(self, instances: list[tuple[str, float]],
                 rc_taille: int = _MORE_DATA_SIGNE, rc_remplissage: int = 0,
                 taille: int | None = None) -> None:
        self._instances = instances
        self._rc_taille = rc_taille
        self._rc_remplissage = rc_remplissage
        self._taille = (ctypes.sizeof(_ItemPdh) * len(instances)
                        if taille is None else taille)
        #: Les chaînes pointées doivent survivre à l'appel : la structure ne
        #: garde qu'un pointeur, pas la chaîne.
        self._vivants: list = []
        self.appels = 0

    def __call__(self, _compteur, _format, taille, nombre, tampon) -> int:
        self.appels += 1
        if tampon is None:
            _ecrire(taille, self._taille)
            _ecrire(nombre, len(self._instances))
            return self._rc_taille
        if self._rc_remplissage == 0:
            items = ctypes.cast(tampon, ctypes.POINTER(_ItemPdh))
            for i, (nom, valeur) in enumerate(self._instances):
                pointeur = ctypes.c_wchar_p(nom)
                self._vivants.append(pointeur)
                items[i].szName = pointeur
                items[i].doubleValue = valeur
        return self._rc_remplissage


def _pdh(instances: list[tuple[str, float]] | None = None,
         rc_ouverture: int = 0, rc_compteur: int = 0, rc_collecte: int = 0,
         **kw) -> _FauxEspace:
    """PDH simulé : ouverture de requête, ajout du compteur, collecte, tableau."""
    return _FauxEspace(
        PdhOpenQueryW=_FonctionWin32(retour=rc_ouverture),
        PdhAddEnglishCounterW=_FonctionWin32(retour=rc_compteur),
        PdhCollectQueryData=_FonctionWin32(retour=rc_collecte),
        PdhGetFormattedCounterArrayW=_TableauPdh(list(instances or []), **kw),
    )


@pytest.fixture
def sonde_gpu(monkeypatch):
    """Une _SondeGpu branchée sur un PDH simulé, sous une plateforme Windows."""
    def _poser(**kw) -> tuple[_SondeGpu, _FauxEspace]:
        pdh = _pdh(**kw)
        monkeypatch.setattr(rw, "_WINDOWS", True)
        monkeypatch.setattr(rw, "ctypes", _FauxCtypes(_FauxEspace(pdh=pdh)))
        return _SondeGpu(), pdh
    return _poser


def _instances_gpu(pid: int) -> list[tuple[str, float]]:
    """Un jeu d'instances réaliste : deux moteurs, deux processus."""
    return [
        (_inst(pid, "3D"), 12.0),
        (_inst(pid, "Video Codec 0"), 55.0),
        (_inst(pid + 1, "Video Codec 0"), 25.0),
    ]


def test_la_sonde_gpu_est_inerte_hors_windows(monkeypatch):
    """PDH n'existe que sous Windows, et ZLink tourne aussi ailleurs.

    Y toucher lèverait à chaque relevé, douze fois par minute.
    """
    monkeypatch.setattr(rw, "_WINDOWS", False)
    monkeypatch.setattr(rw, "ctypes", _FauxCtypes(
        _WindllAbsent(AssertionError("PDH ne doit pas être sollicité"))))
    sonde = _SondeGpu()
    assert sonde.ouvrir() is False
    assert sonde.lire() == (None, None)


def test_la_requete_pdh_n_est_ouverte_qu_une_fois(sonde_gpu):
    """Une requête PDH est une ressource système : en rouvrir une à chaque
    relevé les accumulerait jusqu'à épuisement des handles."""
    sonde, pdh = sonde_gpu(instances=_instances_gpu(os.getpid()))
    assert sonde.ouvrir() is True
    assert sonde.ouvrir() is True
    assert len(pdh.PdhOpenQueryW.appels) == 1


def test_une_ouverture_de_requete_refusee_laisse_la_sonde_muette(sonde_gpu):
    """Rien ne sert d'ajouter un compteur à une requête qui n'existe pas."""
    sonde, pdh = sonde_gpu(rc_ouverture=_ECHEC_PDH)
    assert sonde.ouvrir() is False
    assert sonde.lire() == (None, None)
    assert pdh.PdhAddEnglishCounterW.appels == []


def test_un_poste_sans_compteur_gpu_laisse_la_sonde_muette(sonde_gpu):
    """Le compteur « GPU Engine » n'existe pas partout — machine virtuelle,
    pilote ancien. C'est un cas normal, pas une panne : la surveillance perd le
    décodeur et garde le processeur.
    """
    sonde, _ignore = sonde_gpu(rc_compteur=_ECHEC_PDH)
    assert sonde.ouvrir() is False
    assert sonde.lire() == (None, None)


@pytest.mark.parametrize("panne", [
    AttributeError("windll absent"),
    OSError("pdh.dll introuvable"),
])
def test_pdh_inaccessible_ne_leve_pas(monkeypatch, panne):
    """Même raison que pour kernel32 : la boucle n'a pas de filet."""
    monkeypatch.setattr(rw, "_WINDOWS", True)
    monkeypatch.setattr(rw, "ctypes", _FauxCtypes(_WindllAbsent(panne)))
    sonde = _SondeGpu()
    assert sonde.ouvrir() is False
    assert sonde.lire() == (None, None)


def test_le_premier_releve_gpu_est_jete(sonde_gpu):
    """PDH rend zéro partout tant qu'il n'a qu'une collecte au compteur.

    Publier ce zéro ferait croire à un GPU au repos au démarrage, et fausserait
    la toute première décision.
    """
    sonde, _ignore = sonde_gpu(instances=_instances_gpu(os.getpid()))
    assert sonde.lire() == (None, None)
    assert sonde.lire() != (None, None)


@pytest.mark.parametrize("rc_taille", [_MORE_DATA_SIGNE, rw._PDH_MORE_DATA])
def test_la_sonde_gpu_rend_le_moteur_le_plus_charge_et_la_part_de_zlink(
        sonde_gpu, rc_taille):
    """Bout en bout, tampon PDH compris — et quel que soit le SIGNE du code.

    Les codes PDH sont des HRESULT que ctypes rend négatifs : sans le masque,
    la comparaison à PDH_MORE_DATA échouait et la sonde se déclarait
    indisponible alors qu'elle marchait parfaitement.
    """
    sonde, _ignore = sonde_gpu(instances=_instances_gpu(os.getpid()),
                               rc_taille=rc_taille)
    sonde.lire()                                    # relevé d'amorçage
    total, part = sonde.lire()
    assert total == pytest.approx(80.0), "les deux processus sur Video Codec 0"
    assert part == pytest.approx(55.0 / 80.0 * 100.0)
    assert sonde.moteur == "Video Codec 0"


def test_un_gpu_au_repos_ne_divise_pas_par_zero(sonde_gpu):
    """Un GPU parfaitement inactif est un cas courant hors event."""
    sonde, _ignore = sonde_gpu(instances=[(_inst(os.getpid(), "3D"), 0.0)])
    sonde.lire()
    assert sonde.lire() == (0.0, 0.0)


def test_un_echec_de_collecte_ne_rend_aucune_mesure(sonde_gpu):
    """Le GPU peut disparaître en cours de route : pilote qui redémarre, écran
    débranché. La sonde le dit en se taisant."""
    sonde, _ignore = sonde_gpu(instances=_instances_gpu(os.getpid()),
                               rc_collecte=_ECHEC_PDH)
    assert sonde.lire() == (None, None)


def test_une_erreur_pdh_pendant_la_lecture_ne_leve_pas(sonde_gpu):
    """La sonde s'ouvre, puis PDH lâche. Le relevé suivant ne doit pas faire
    remonter l'exception dans la boucle."""
    sonde, pdh = sonde_gpu(instances=_instances_gpu(os.getpid()))
    assert sonde.ouvrir() is True
    pdh.PdhCollectQueryData = _FonctionWin32(leve=OSError("PDH parti"))
    assert sonde.lire() == (None, None)


@pytest.mark.parametrize("kw", [
    {"rc_taille": 0},                       # PDH ne réclame pas de tampon
    {"taille": 0},                          # il en réclame un de taille nulle
    {"rc_remplissage": _ECHEC_PDH},         # il refuse de le remplir
])
def test_un_tableau_de_compteurs_indisponible_ne_rend_aucune_mesure(
        sonde_gpu, kw):
    """Trois façons pour PDH de ne pas livrer ses instances. Aucune ne doit
    produire de chiffre : un tampon non rempli se lirait comme un GPU à zéro."""
    sonde, _ignore = sonde_gpu(instances=_instances_gpu(os.getpid()), **kw)
    sonde.lire()
    assert sonde.lire() == (None, None)


def test_des_instances_sans_type_de_moteur_ne_rendent_aucune_mesure(sonde_gpu):
    """Le compteur existe, ses instances ne se nomment plus pareil.

    Windows a déjà changé ce format ; ce jour-là, il vaut mieux perdre la
    mesure GPU que rendre un zéro pris pour un décodeur au repos.
    """
    sonde, _ignore = sonde_gpu(instances=[("compteur_sans_type", 50.0)])
    sonde.lire()
    assert sonde.lire() == (None, None)


def test_une_instance_sans_nom_est_ignoree():
    """PDH annonce parfois plus d'entrées qu'il n'en nomme.

    Les décoder quand même mettrait un None dans la liste, et le regroupement
    par moteur lèverait sur le premier test d'appartenance.
    """
    tampon = ctypes.create_string_buffer(ctypes.sizeof(_ItemPdh) * 3)
    items = ctypes.cast(tampon, ctypes.POINTER(_ItemPdh))
    vivants = [ctypes.c_wchar_p(_inst(1, "3D")),
               ctypes.c_wchar_p(_inst(2, "Copy"))]
    items[0].szName, items[0].doubleValue = vivants[0], 12.5
    items[2].szName, items[2].doubleValue = vivants[1], 3.0
    assert _decoder_instances(tampon, 3) == [(_inst(1, "3D"), 12.5),
                                             (_inst(2, "Copy"), 3.0)]


# ── la surveillance elle-même ────────────────────────────────────────────────

class _FilFactice:
    """Fil jamais lancé : la boucle est appelée à la main.

    Un vrai fil rendrait le test tributaire de l'ordonnanceur, et surtout il
    échantillonnerait la machine qui exécute la suite.
    """

    def __init__(self, target, daemon: bool = False, name: str = "") -> None:
        self.cible = target
        self.daemon = daemon
        self.name = name
        self.demarre = False

    def start(self) -> None:
        self.demarre = True


class _FauxThreading:
    """`threading`, privé de sa capacité à démarrer un fil réel."""

    Event = threading.Event

    def __init__(self) -> None:
        self.fils: list[_FilFactice] = []

    def Thread(self, **kw) -> _FilFactice:   # noqa: N802 — nom de l'API imitée
        fil = _FilFactice(**kw)
        self.fils.append(fil)
        return fil


class _EvenementScript:
    """`threading.Event` scénarisé : la boucle sort au tour voulu.

    Aucune seconde ne s'écoule réellement — les attentes sont enregistrées, pas
    subies, ce qui rend la période observable sans ralentir la suite.
    """

    def __init__(self, tours: int) -> None:
        self.attentes: list[float] = []
        self._restants = tours
        self._pose = False

    def wait(self, delai: float) -> bool:
        self.attentes.append(delai)
        if self._pose or self._restants <= 0:
            return True
        self._restants -= 1
        return False

    def set(self) -> None:
        self._pose = True

    def is_set(self) -> bool:
        return self._pose


@pytest.fixture
def surveillance(qapp, monkeypatch):
    """Une ResourceWatch dont le fil est une doublure.

    `qapp` est requis : `saturation` est un pyqtSignal, qui n'existe qu'avec
    une application Qt.
    """
    def _poser(periode: float = 5.0) -> tuple[ResourceWatch, _FauxThreading]:
        faux = _FauxThreading()
        monkeypatch.setattr(rw, "threading", faux)
        return ResourceWatch(periode=periode), faux
    return _poser


def test_la_surveillance_ne_lance_qu_un_seul_fil(surveillance):
    """Un second fil doublerait les relevés ET les alertes : chaque détecteur
    aurait sa propre série et son propre silence.
    """
    w, faux = surveillance()
    w.start()
    w.start()
    assert len(faux.fils) == 1
    fil = faux.fils[0]
    assert fil.demarre and fil.daemon, "un fil non démon retiendrait la fermeture"
    assert fil.name == "resource-watch"


def test_l_arret_fait_sortir_la_boucle_sans_attendre(surveillance, monkeypatch):
    """stop() est appelé à la fermeture de l'application. Si la boucle n'en
    sortait qu'au terme de sa période, la fenêtre resterait cinq secondes de
    plus à l'écran."""
    w, _ignore = surveillance()
    releves: list[int] = []

    def _releve() -> Releve:
        releves.append(1)
        return Releve()

    monkeypatch.setattr(w, "releve", _releve)
    w.stop()
    w._boucle()
    assert releves == [1], "seul le relevé d'amorçage a été pris"


def test_la_periode_ne_descend_jamais_sous_la_seconde(surveillance, monkeypatch):
    """Échantillonner plus vite coûterait plus cher que ce qu'on surveille : la
    sonde GPU parcourt toutes les instances du compteur à chaque relevé."""
    w, _ignore = surveillance(periode=0.05)
    monkeypatch.setattr(w, "releve", Releve)
    w._arret = _EvenementScript(tours=2)
    w._boucle()
    assert w._arret.attentes == [1.0, 1.0, 1.0]


def test_le_releve_d_amorcage_ne_va_pas_au_detecteur(surveillance, monkeypatch):
    """Les compteurs cumulés n'ont pas encore d'écart au premier relevé.

    Le soumettre au détecteur reviendrait à juger la charge accumulée depuis le
    démarrage de la machine, sans rapport avec l'instant présent.
    """
    vus: list[Releve] = []
    w, _ignore = surveillance()
    w._detecteur = _FauxEspace(observer=lambda r, t: vus.append(r))
    monkeypatch.setattr(w, "releve", _sature)
    w._arret = _EvenementScript(tours=2)
    w._boucle()
    assert len(vus) == 2, "trois relevés pris, deux soumis à la décision"


@pytest.mark.parametrize("ressource,mesure,attendu", [
    ("gpu", {"gpu_systeme": 95.0, "gpu_zlink": 80.0}, (95.0, 80.0)),
    ("cpu", {"cpu_systeme": 93.0, "cpu_zlink": 71.0}, (93.0, 71.0)),
])
def test_le_signal_porte_les_chiffres_de_la_ressource_saturee(
        surveillance, monkeypatch, ressource, mesure, attendu):
    """Ces deux nombres sont repris tels quels dans la phrase affichée.

    Envoyer ceux de l'AUTRE ressource ferait afficher « décodeur vidéo à 0 % »
    sous un conseil de réduire la grille.
    """
    w, _ignore = surveillance()
    w._detecteur = _Detecteur(consecutifs=1)
    monkeypatch.setattr(w, "releve", lambda: Releve(**mesure))
    w._arret = _EvenementScript(tours=1)
    recus: list[tuple] = []
    w.saturation.connect(lambda *a: recus.append(a))
    w._boucle()
    assert recus == [(ressource, *attendu)]


def test_une_saturation_sans_mesure_chiffree_n_emet_pas_de_None(
        surveillance, monkeypatch):
    """Le signal est typé (str, float, float) : un None y ferait lever Qt au
    moment le moins choisi, à savoir pendant une saturation."""
    w, _ignore = surveillance()
    w._detecteur = _FauxEspace(observer=lambda r, t: "gpu")
    monkeypatch.setattr(w, "releve", Releve)
    w._arret = _EvenementScript(tours=1)
    recus: list[tuple] = []
    w.saturation.connect(lambda *a: recus.append(a))
    w._boucle()
    assert recus == [("gpu", 0.0, 0.0)]


def test_une_sonde_qui_leve_n_interrompt_pas_la_surveillance(
        surveillance, monkeypatch, caplog):
    """Une sonde matérielle peut disparaître en cours de route — pilote qui
    redémarre, GPU débranché. La surveillance doit continuer, et l'incident
    figurer au journal plutôt que d'être avalé.
    """
    w, _ignore = surveillance()
    w._detecteur = _Detecteur(consecutifs=1)
    sequence = iter([Releve(), OSError("pilote parti"), _sature()])

    def _releve() -> Releve:
        valeur = next(sequence)
        if isinstance(valeur, Exception):
            raise valeur
        return valeur

    monkeypatch.setattr(w, "releve", _releve)
    w._arret = _EvenementScript(tours=2)
    recus: list[tuple] = []
    w.saturation.connect(lambda *a: recus.append(a))
    with caplog.at_level(logging.ERROR, logger="core.resource_watch"):
        w._boucle()
    assert recus == [("gpu", 95.0, 80.0)]
    assert "pilote parti" in caplog.text


def test_le_releve_assemble_les_mesures_des_deux_sondes(surveillance, monkeypatch):
    """Quatre nombres, deux sources : les croiser attribuerait au décodeur la
    charge du processeur, et le conseil porterait sur le mauvais réglage."""
    w, _ignore = surveillance()
    monkeypatch.setattr(rw, "lire_cpu", lambda etat: (40.0, 25.0))
    monkeypatch.setattr(w._gpu, "lire", lambda: (60.0, 90.0))
    assert w.releve() == Releve(cpu_systeme=40.0, cpu_zlink=25.0,
                                gpu_systeme=60.0, gpu_zlink=90.0)


def test_le_releve_survit_a_des_sondes_muettes(surveillance, monkeypatch):
    """Sur un poste sans compteur GPU, un relevé doit rester un relevé — un
    Releve vide, que le détecteur sait ignorer."""
    w, _ignore = surveillance()
    monkeypatch.setattr(rw, "lire_cpu", lambda etat: (None, None))
    monkeypatch.setattr(w._gpu, "lire", lambda: (None, None))
    assert w.releve() == Releve()
