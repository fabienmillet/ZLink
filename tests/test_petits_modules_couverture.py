# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Chemins d'échec des six petits modules de `core/`.

Les tests existants couvrent le cas nominal de ces modules ; ce qui restait
non couvert, ce sont précisément les branches qui n'arrivent que lorsque
quelque chose se passe mal — streamlink introuvable, disque en lecture seule,
config.json corrompu, git absent. Ce sont aussi celles que personne ne
déclenche à la main avant une publication.

Rien ici ne lance de programme extérieur ni n'écrit hors de `tmp_path` : un
test qui toucherait le vrai config.json abîmerait l'installation de celui qui
le lance.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types

import pytest

from core import config_store, favorites, selection_store, version
from core import stream_manager as sm
from core.sous_processus import interdire_les_consoles


# ── fixtures partagées ───────────────────────────────────────────────────────

@pytest.fixture
def config(tmp_path, monkeypatch):
    """config.json neuf, partagé par config_store et favorites."""
    cible = tmp_path / "config.json"
    monkeypatch.setattr(config_store, "CONFIG_PATH", cible)
    monkeypatch.setattr(favorites, "CONFIG_PATH", cible)
    # Les favoris gardent leur liste en mémoire : sans remise à zéro, un test
    # hériterait du cache du précédent.
    monkeypatch.setattr(favorites, "_cache", None)
    return cible


@pytest.fixture
def chemin_barre(tmp_path):
    """Un chemin dont le dossier parent est… un fichier.

    Manière portable de rendre toute écriture impossible : ni chmod, ni disque
    plein, ni permissions à simuler — `mkdir` et `write_text` échouent des deux
    côtés avec une OSError, ce qui est exactement ce que le code attrape.
    """
    obstacle = tmp_path / "ceci-est-un-fichier"
    obstacle.write_text("pas un dossier", encoding="utf-8")
    return obstacle / "config.json"


def _faux_os(**remplacements):
    """Doublure du module `os` limitée à ce que ces modules en utilisent.

    Remplacer l'attribut `os` du module testé plutôt que la fonction dans le
    vrai `os` : une panne d'écriture simulée ne doit pas s'appliquer au reste
    du processus de test pendant que le test tourne.
    """
    base = {"getpid": os.getpid, "chmod": os.chmod, "replace": os.replace}
    base.update(remplacements)
    return types.SimpleNamespace(**base)


def _leve_oserror(*_args, **_kwargs):
    raise OSError("écriture refusée")


# ═════════════════════════════════════════════════════════════════════════════
# core/config_store.py
# ═════════════════════════════════════════════════════════════════════════════

def test_des_permissions_non_restreintes_n_empechent_pas_la_sauvegarde(config,
                                                                       monkeypatch):
    """chmod échoue sur bien des systèmes de fichiers (FAT, partage réseau).

    Restreindre le fichier est un plus, pas une condition : renoncer à écrire
    la configuration parce que le mode n'a pas pu être posé perdrait les
    réglages de l'utilisateur pour une raison qui ne le concerne pas.
    """
    monkeypatch.setattr(config_store, "os", _faux_os(chmod=_leve_oserror))
    assert config_store.save_merge({"a": 1}) is True
    assert config_store.load() == {"a": 1}


def test_une_ecriture_impossible_est_signalee_et_non_subie(monkeypatch,
                                                           chemin_barre):
    """`save_merge` rend False plutôt que de lever.

    Les quatre appelants (réglages, assistant, favoris, rappels) tournent dans
    le fil de l'interface : une OSError qui remonte y ferait tomber la fenêtre
    au lieu d'afficher un message.
    """
    monkeypatch.setattr(config_store, "CONFIG_PATH", chemin_barre)
    assert config_store.save_merge({"a": 1}) is False


def test_un_echec_apres_l_ecriture_du_temporaire_ne_laisse_pas_de_trace(config,
                                                                        monkeypatch):
    """Le fichier temporaire est supprimé quand `os.replace` échoue.

    Sans ce nettoyage, chaque tentative ratée laisserait un `config.json.NNN.tmp`
    de plus à côté de la configuration, indéfiniment.
    """
    monkeypatch.setattr(config_store, "os", _faux_os(replace=_leve_oserror))
    assert config_store.save_merge({"a": 1}) is False
    assert list(config.parent.glob("config.json.*.tmp")) == []


def test_une_ecriture_ratee_laisse_l_ancienne_configuration_intacte(config,
                                                                    monkeypatch):
    """C'est tout l'intérêt du temporaire suivi d'un `os.replace`.

    Une écriture directe tronquerait le fichier à zéro avant d'échouer, et
    l'utilisateur retrouverait une configuration vide — clés comprises.
    """
    config_store.save_merge({"max_active_streams": 20})
    monkeypatch.setattr(config_store, "os", _faux_os(replace=_leve_oserror))
    assert config_store.save_merge({"autre": 1}) is False
    assert config_store.load() == {"max_active_streams": 20}


# ═════════════════════════════════════════════════════════════════════════════
# core/favorites.py
# ═════════════════════════════════════════════════════════════════════════════

def test_les_favoris_relus_sont_normalises(config):
    """Le fichier peut avoir été écrit par une version antérieure, ou à la main.

    Les comparaisons se font toutes en minuscules : un « ZeratoR » relu tel
    quel ne serait jamais reconnu comme favori dans la grille.
    """
    config.write_text(json.dumps({
        "favorite_logins": ["ZeratoR", "Aypierre", "", None, "ZeratoR"],
    }), encoding="utf-8")
    assert favorites.get() == {"zerator", "aypierre"}


def test_une_config_qui_n_est_pas_un_objet_ne_fait_pas_tomber_les_favoris(config):
    """`raw.get` n'existe pas sur une liste : sans le garde-fou, l'exception
    remonterait jusqu'au premier affichage de la grille."""
    config.write_text('["une", "liste"]', encoding="utf-8")
    assert favorites.get() == set()


def test_un_config_json_illisible_rend_un_ensemble_vide(config):
    config.write_text("{ pas du json", encoding="utf-8")
    assert favorites.get() == set()


def test_une_sauvegarde_de_favori_impossible_ne_leve_pas(config, monkeypatch):
    """`toggle` est câblé sur un clic et sur une touche de la palette.

    Une exception y ferait tomber la fenêtre ; l'état en mémoire reste juste,
    seule la persistance est perdue.
    """
    monkeypatch.setattr(favorites, "os", _faux_os(replace=_leve_oserror))
    assert favorites.toggle("zerator") is True
    assert favorites.is_favorite("zerator") is True, "l'état en mémoire tient"


# ═════════════════════════════════════════════════════════════════════════════
# core/selection_store.py
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def selection(tmp_path, monkeypatch):
    monkeypatch.setattr(selection_store, "STORE_PATH",
                        tmp_path / "grid_selection.json")


def test_une_selection_qui_n_est_pas_une_liste_est_ignoree(selection, tmp_path):
    """Le fichier attendu est un tableau de logins.

    Un objet JSON n'est pas une erreur de syntaxe : sans le contrôle de type,
    l'itération porterait sur les CLÉS et fabriquerait une sélection inventée.
    """
    (tmp_path / "grid_selection.json").write_text(
        '{"zerator": true}', encoding="utf-8")
    assert selection_store.SelectionStore().get_selected() == []


def test_les_entrees_qui_ne_sont_pas_des_logins_sont_ecartees(selection, tmp_path):
    """Un login part en argument de sous-processus streamlink : un nombre ou un
    objet relus tels quels casseraient la cellule au lieu d'être ignorés."""
    (tmp_path / "grid_selection.json").write_text(
        '["zerator", 42, null, {"x": 1}, "ponce", "zerator"]', encoding="utf-8")
    assert selection_store.SelectionStore().get_selected() == ["zerator", "ponce"]


def test_une_sauvegarde_impossible_ne_leve_pas(tmp_path, monkeypatch):
    """La sélection change à chaque clic sur une vignette.

    Perdre la persistance est acceptable ; faire tomber la grille au clic ne
    l'est pas.
    """
    obstacle = tmp_path / "ceci-est-un-fichier"
    obstacle.write_text("pas un dossier", encoding="utf-8")
    monkeypatch.setattr(selection_store, "STORE_PATH", obstacle / "sel.json")

    s = selection_store.SelectionStore()
    s.set_selected("zerator", True)
    assert s.get_selected() == ["zerator"], "l'état en mémoire tient"


def test_selectionner_deux_fois_le_meme_login_ne_le_duplique_pas(selection):
    """L'ordre de la liste EST la numérotation des slots : un doublon décalerait
    tous les suivants d'un cran."""
    s = selection_store.SelectionStore()
    s.set_selected("zerator", True)
    s.set_selected("ponce", True)
    s.set_selected("zerator", True)
    assert s.get_selected() == ["zerator", "ponce"]


def test_deselectionner_un_login_absent_ne_change_rien(selection):
    """La grille et la palette peuvent envoyer l'ordre chacune de leur côté."""
    s = selection_store.SelectionStore()
    s.set_all(["zerator", "ponce"])
    s.set_selected("inconnu", False)
    assert s.get_selected() == ["zerator", "ponce"]


def test_is_selected_reflete_la_selection(selection):
    s = selection_store.SelectionStore()
    s.set_selected("zerator", True)
    assert s.is_selected("zerator") is True
    assert s.is_selected("ponce") is False


# ═════════════════════════════════════════════════════════════════════════════
# core/version.py
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def sans_cache_de_commit(monkeypatch):
    """Le commit est mémorisé pour la durée du processus.

    Sans remise à zéro, le premier test qui appelle `commit()` fixerait la
    valeur vue par tous les suivants — et par le reste de la suite.
    """
    monkeypatch.setattr(version, "_commit_cache", None)


@pytest.fixture
def build_info(monkeypatch):
    """Simule le module que la chaîne de publication écrit avant d'empaqueter.

    Il est absent du dépôt par construction : c'est son absence qui signale un
    lancement depuis les sources.
    """
    faux = types.ModuleType("core.build_info")
    faux.COMMIT = "1a2b3c4d5e6f"      # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.build_info", faux)
    return faux


def test_le_commit_ecrit_a_la_construction_prime_sur_git(sans_cache_de_commit,
                                                         build_info, monkeypatch):
    """Un paquet publié n'a ni git ni dépôt : la valeur gelée est la seule juste.

    Elle est tronquée à sept caractères, la forme courte affichée partout
    ailleurs (journal, écran « À propos », rapports de bug).
    """
    def jamais(*_a, **_k):
        raise AssertionError("git ne doit pas être interrogé")
    monkeypatch.setattr(subprocess, "run", jamais)
    assert version.commit() == "1a2b3c4"


def test_un_paquet_gele_n_interroge_pas_git(sans_cache_de_commit, monkeypatch):
    """Sans build_info ET gelé, il n'y a rien à trouver.

    Lancer `git` depuis un exécutable installé coûterait jusqu'à deux secondes
    de démarrage pour un résultat qui ne peut pas exister.
    """
    import core.paths
    monkeypatch.setattr(core.paths, "FROZEN", True)

    def jamais(*_a, **_k):
        raise AssertionError("git ne doit pas être interrogé dans un paquet gelé")
    monkeypatch.setattr(subprocess, "run", jamais)
    assert version.commit() == ""


def test_un_git_qui_echoue_laisse_le_commit_inconnu(sans_cache_de_commit,
                                                    monkeypatch):
    """Dépôt absent, HEAD introuvable : git rend un code non nul.

    Le numéro de version seul reste parfaitement utilisable ; rien ne doit
    remonter à l'utilisateur.
    """
    import core.paths
    monkeypatch.setattr(core.paths, "FROZEN", False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        returncode=128, stdout="fatal: not a git repository\n"))
    assert version.commit() == ""


def test_git_absent_ou_trop_lent_ne_fait_pas_tomber_le_demarrage(
        sans_cache_de_commit, monkeypatch):
    """`commit()` est appelé au démarrage : une exception y bloquerait le
    lancement pour un renseignement purement informatif."""
    import core.paths
    monkeypatch.setattr(core.paths, "FROZEN", False)

    def indisponible(*_a, **_k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(subprocess, "run", indisponible)
    assert version.commit() == ""


def test_le_commit_n_est_releve_qu_une_fois(sans_cache_de_commit, monkeypatch):
    """Il est affiché à plusieurs endroits ; relancer git à chaque fois
    ajouterait un sous-processus par consultation."""
    import core.paths
    monkeypatch.setattr(core.paths, "FROZEN", False)
    appels: list[int] = []

    def compte(*_a, **_k):
        appels.append(1)
        return types.SimpleNamespace(returncode=0, stdout="abc1234\n")
    monkeypatch.setattr(subprocess, "run", compte)

    assert version.commit() == "abc1234"
    assert version.commit() == "abc1234"
    assert len(appels) == 1


def test_un_paquet_construit_n_est_pas_une_version_de_developpement(build_info):
    """La présence de build_info suffit, gelé ou non : c'est bien du code
    passé par la chaîne de publication."""
    assert version.is_dev_build() is False


def test_une_version_publiee_s_affiche_sans_suffixe(build_info):
    """« 0.1.0-dev+… » sur un paquet publié ferait douter de ce qu'on tient."""
    assert version.display_version() == version.__version__


def test_un_lancement_depuis_les_sources_est_marque_dev(sans_cache_de_commit,
                                                        monkeypatch):
    """Personne ne doit croire tenir la version publiée alors qu'il fait tourner
    un dépôt de travail."""
    import core.paths
    monkeypatch.setattr(core.paths, "FROZEN", False)
    monkeypatch.setitem(sys.modules, "core.build_info", None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout="abc1234\n"))
    assert version.is_dev_build() is True
    assert version.display_version() == f"{version.__version__}-dev+abc1234"


def test_sans_commit_connu_la_mention_dev_reste(sans_cache_de_commit, monkeypatch):
    """Le suffixe de commit est un plus ; la mention « dev », elle, est
    l'information qui compte et ne doit pas disparaître avec lui."""
    import core.paths
    monkeypatch.setattr(core.paths, "FROZEN", False)
    monkeypatch.setitem(sys.modules, "core.build_info", None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        returncode=1, stdout=""))
    assert version.display_version() == f"{version.__version__}-dev"


# ═════════════════════════════════════════════════════════════════════════════
# core/sous_processus.py — le garde-fou global
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def garde_reversible(monkeypatch):
    """Prépare la pose du garde-fou en garantissant sa dépose.

    `interdire_les_consoles` remplace `subprocess.Popen.__init__` pour tout le
    processus : sans restauration, le reste de la suite tournerait avec un
    Popen enveloppé. On enregistre donc l'original auprès de monkeypatch avant
    d'appeler, et on rend un espion qui recueille les arguments réellement
    transmis — aucun programme n'est lancé.

    La plateforme est simulée : CREATE_NO_WINDOW n'existe pas sous Linux, où
    tourne l'intégration continue, il faut donc l'apporter avec elle.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    for nom, defaut in (("CREATE_NO_WINDOW", 0x08000000),
                        ("DETACHED_PROCESS", 0x00000008),
                        ("CREATE_NEW_CONSOLE", 0x00000010)):
        monkeypatch.setattr(subprocess, nom, getattr(subprocess, nom, defaut),
                            raising=False)
    # Le drapeau de pose peut déjà être vrai — la garde survit à un test qui
    # l'aurait installée. Le remettre à faux force la pose, et monkeypatch
    # retire l'attribut au démontage s'il n'existait pas.
    monkeypatch.setattr(subprocess.Popen, "_zlink_sans_fenetre", False,
                        raising=False)

    recus: list[dict] = []

    def espion(self, *_args, **kwargs):
        recus.append(kwargs)

    # Enregistré auprès de monkeypatch : c'est CE geste qui garantit que le
    # vrai Popen.__init__ revient au démontage, même si le test échoue.
    monkeypatch.setattr(subprocess.Popen, "__init__", espion)
    return recus


def test_un_lancement_sans_drapeau_recoit_create_no_window(garde_reversible):
    """Le garde-fou de dernier recours : il rattrape les appels qui ont oublié
    `**sans_fenetre()`, et ceux des bibliothèques tierces qu'on ne relit pas."""
    assert interdire_les_consoles() is True
    subprocess.Popen.__init__(object(), ["streamlink"])
    assert garde_reversible == [{"creationflags": subprocess.CREATE_NO_WINDOW}]


@pytest.mark.parametrize("drapeau", ["DETACHED_PROCESS", "CREATE_NEW_CONSOLE"])
def test_un_appelant_qui_veut_une_console_la_garde(garde_reversible, drapeau):
    """DETACHED_PROCESS et CREATE_NEW_CONSOLE demandent explicitement une
    console : y ajouter CREATE_NO_WINDOW fabriquerait un drapeau contradictoire
    que Windows refuse."""
    voulu = getattr(subprocess, drapeau)
    interdire_les_consoles()
    subprocess.Popen.__init__(object(), ["cmd"], creationflags=voulu)
    assert garde_reversible == [{"creationflags": voulu}]


def test_poser_la_garde_deux_fois_n_empile_pas_les_enveloppes(garde_reversible):
    """Sans ce contrôle, chaque appel envelopperait l'enveloppe précédente :
    une pile de fermetures qui grandit et ralentit chaque lancement."""
    assert interdire_les_consoles() is True
    pose = subprocess.Popen.__init__
    assert interdire_les_consoles() is True
    assert subprocess.Popen.__init__ is pose


@pytest.mark.parametrize("plateforme", ["linux", "darwin"])
def test_hors_windows_la_garde_ne_touche_a_rien(monkeypatch, plateforme):
    """Aucune console n'est allouée ailleurs : envelopper Popen n'y apporterait
    qu'un risque de régression pour rien."""
    monkeypatch.setattr(sys, "platform", plateforme)
    intact = subprocess.Popen.__init__
    assert interdire_les_consoles() is False
    assert subprocess.Popen.__init__ is intact


# ═════════════════════════════════════════════════════════════════════════════
# core/stream_manager.py — résolution streamlink
# ═════════════════════════════════════════════════════════════════════════════

def test_un_palier_de_qualite_inconnu_est_transmis_tel_quel():
    """`echelle` ne sait décliner que les paliers de la forme « 720p60 ».

    Tout le reste doit passer sans être déformé, et le repli « worst » rester
    en queue : c'est lui qui empêche la cellule noire.
    """
    assert sm.echelle("banane") == "banane,worst"


def _sans_streamlink(monkeypatch, trouve_dans_le_path=None):
    """Aucun candidat sur disque ; `which` rend ce qu'on lui dit."""
    monkeypatch.setattr(sm, "os", types.SimpleNamespace(
        path=types.SimpleNamespace(isfile=lambda p: False,
                                   dirname=os.path.dirname,
                                   join=os.path.join),
        join=os.path.join))
    monkeypatch.setattr(sm, "shutil", types.SimpleNamespace(
        which=lambda nom: trouve_dans_le_path))


def test_streamlink_absent_des_venv_est_cherche_dans_le_path(monkeypatch):
    """`which` rend un chemin ABSOLU, et c'est le point.

    Retourner le nom nu « streamlink » ferait chercher à CreateProcess le
    dossier de l'application puis le répertoire courant avant le PATH — un
    exécutable déposé à côté du binaire serait lancé à sa place.
    """
    absolu = os.path.join(os.sep, "outils", "streamlink")
    _sans_streamlink(monkeypatch, trouve_dans_le_path=absolu)
    chemin = sm._streamlink_exe()
    assert chemin == absolu
    assert chemin != "streamlink", "jamais le nom nu"


def test_streamlink_introuvable_partout_rend_une_chaine_vide(monkeypatch):
    """L'application démarre quand même : le panel, les stats et le programme
    n'ont pas besoin de streamlink. Seule la vidéo manque."""
    _sans_streamlink(monkeypatch, trouve_dans_le_path=None)
    assert sm._streamlink_exe() == ""


@pytest.fixture
def streamlink(monkeypatch):
    """Doublure de streamlink : rend la ligne de commande reçue et la réponse.

    Aucun sous-processus n'est lancé — résoudre une URL Twitch pour de vrai
    ferait dépendre la suite du réseau et de l'état d'un direct.
    """
    monkeypatch.setattr(sm, "_STREAMLINK", "/faux/streamlink")
    etat: dict = {"argv": None, "reponse": types.SimpleNamespace(
        returncode=0, stdout="https://video.twitch/flux.m3u8\n", stderr="")}

    def faux_run(argv, **_kwargs):
        etat["argv"] = argv
        reponse = etat["reponse"]
        if isinstance(reponse, BaseException):
            raise reponse
        return reponse

    monkeypatch.setattr(sm.subprocess, "run", faux_run)
    return etat


def test_l_url_resolue_est_deballee_des_espaces(streamlink):
    """Elle part directement dans `mpv.play()` : un retour à la ligne collé au
    bout donnerait une URL que mpv ne sait pas ouvrir."""
    assert sm._get_stream_url("zerator") == "https://video.twitch/flux.m3u8"


def test_la_qualite_est_validee_avant_de_partir_en_argument(streamlink):
    """Une qualité vient de config.json, que l'utilisateur édite.

    Ce qui commence par un tiret serait lu par streamlink comme une OPTION —
    et `--plugin-dirs` lui fait exécuter du Python arbitraire.
    """
    sm._get_stream_url("zerator", "--plugin-dirs=/tmp")
    assert "--plugin-dirs=/tmp" not in streamlink["argv"]
    assert sm.QUALITY_FULLSCREEN in streamlink["argv"]


def test_les_publicites_twitch_sont_ecartees_a_la_resolution(streamlink):
    """Sans ce drapeau, l'URL résolue peut être celle d'un bloc publicitaire :
    la cellule joue une réclame en boucle au lieu du direct."""
    sm._get_stream_url("zerator")
    assert "--twitch-disable-ads" in streamlink["argv"]


def test_streamlink_introuvable_ne_lance_pas_de_resolution(monkeypatch):
    monkeypatch.setattr(sm, "_STREAMLINK", "")

    def jamais(*_a, **_k):
        raise AssertionError("aucun lancement possible sans exécutable")
    monkeypatch.setattr(sm.subprocess, "run", jamais)
    assert sm._get_stream_url("zerator") == ""


def test_un_code_de_retour_non_nul_rend_une_url_vide(streamlink):
    """Chaîne hors ligne, palier absent du transcodage : streamlink sort en 1.

    Rendre son stdout tel quel enverrait un message d'erreur à mpv comme s'il
    s'agissait d'une URL.
    """
    streamlink["reponse"] = types.SimpleNamespace(
        returncode=1, stdout="error: No playable streams found\n", stderr="")
    assert sm._get_stream_url("zerator") == ""


def test_une_sortie_vide_avec_un_code_nul_rend_une_url_vide(streamlink):
    streamlink["reponse"] = types.SimpleNamespace(
        returncode=0, stdout="   \n", stderr="")
    assert sm._get_stream_url("zerator") == ""


def test_un_executable_disparu_entre_temps_ne_leve_pas(streamlink):
    """Le chemin est relevé une fois à l'import ; un venv recréé ou une
    désinstallation pendant la session le rend caduc."""
    streamlink["reponse"] = FileNotFoundError("streamlink")
    assert sm._get_stream_url("zerator") == ""


def test_une_resolution_trop_lente_est_abandonnee(streamlink):
    """Vingt cellules qui attendent chacune un streamlink bloqué figeraient la
    grille : le délai est la seule garantie de reprise."""
    streamlink["reponse"] = subprocess.TimeoutExpired(cmd="streamlink", timeout=20)
    assert sm._get_stream_url("zerator") == ""


def test_toute_autre_panne_de_resolution_rend_une_url_vide(streamlink):
    """La résolution tourne dans un thread : une exception qui s'en échappe ne
    remonterait nulle part et laisserait la cellule en attente pour toujours."""
    streamlink["reponse"] = RuntimeError("panne inattendue")
    assert sm._get_stream_url("zerator") == ""


# ═════════════════════════════════════════════════════════════════════════════
# core/stream_manager.py — StreamManager : lecture et arrêt
# ═════════════════════════════════════════════════════════════════════════════

class _FilSynchrone:
    """Doublure de `threading.Thread` qui exécute la cible sur place.

    Le vrai thread rendrait ces tests dépendants d'une attente : on saurait
    qu'un signal n'a pas encore été émis sans savoir s'il le sera.
    """

    def __init__(self, target=None, args=(), daemon=False, name="") -> None:
        self._target, self._args = target, args
        self.name = name
        self.daemon = daemon

    def start(self) -> None:
        self._target(*self._args)


@pytest.fixture
def manager(qapp, monkeypatch):
    """StreamManager dont la résolution est instantanée et pilotable."""
    monkeypatch.setattr(sm, "threading", types.SimpleNamespace(Thread=_FilSynchrone))
    etat: dict = {"url": "https://video.twitch/flux.m3u8", "appels": []}

    def fausse_resolution(login, quality):
        etat["appels"].append((login, quality))
        return etat["url"]

    monkeypatch.setattr(sm, "_get_stream_url", fausse_resolution)

    m = sm.StreamManager()
    m.etat = etat                        # type: ignore[attr-defined]
    m.prets, m.erreurs, m.arrets = [], [], []   # type: ignore[attr-defined]
    m.stream_ready.connect(lambda lg, url: m.prets.append((lg, url)))
    m.stream_error.connect(lambda lg, msg: m.erreurs.append((lg, msg)))
    m.stream_stopped.connect(m.arrets.append)
    return m


def test_une_url_resolue_est_annoncee_avec_son_login(manager):
    """La fenêtre plein écran s'en sert pour ignorer une réponse tardive qui ne
    concerne plus le streamer affiché."""
    manager.play("zerator")
    assert manager.prets == [("zerator", "https://video.twitch/flux.m3u8")]
    assert manager.current_login == "zerator"
    assert manager.erreurs == []


def test_sans_qualite_demandee_c_est_celle_du_plein_ecran_qui_sert(manager):
    """Le plein écran est le SEUL flux en haute qualité : lui appliquer par
    défaut la qualité de grille le rendrait flou sur tout l'écran."""
    manager.reload_config({"fullscreen_quality": "1080p60,best"})
    manager.play("zerator")
    assert manager.etat["appels"] == [("zerator", "1080p60,best")]


def test_une_qualite_explicite_prime_sur_la_configuration(manager):
    """Le rattrapage d'un direct saturé se fait en abaissant la qualité pour un
    seul lancement, sans toucher aux réglages."""
    manager.play("zerator", "480p,480p30")
    assert manager.etat["appels"] == [("zerator", "480p,480p30")]


def test_une_resolution_ratee_libere_le_streamer_courant(manager):
    """Sans remise à zéro, un nouveau clic sur LE MÊME streamer serait vu comme
    un doublon et la fenêtre resterait noire jusqu'à ce qu'on en choisisse un
    autre."""
    manager.etat["url"] = ""
    manager.play("zerator")
    assert manager.erreurs and manager.erreurs[0][0] == "zerator"
    assert manager.current_login == ""
    assert manager.prets == []


def test_changer_de_streamer_arrete_le_precedent(manager):
    """L'arrêt est ce qui libère l'instance mpv du plein écran ; l'oublier
    laisserait deux flux décodés en parallèle."""
    manager.play("zerator")
    manager.play("ponce")
    assert manager.arrets == ["zerator"]
    assert manager.current_login == "ponce"


def test_rien_n_est_arrete_pendant_qu_une_resolution_est_en_cours(manager):
    """mpv ne joue encore rien : annoncer un arrêt ferait afficher « stream
    arrêté » pour un flux qui n'avait jamais commencé."""
    manager.play("zerator")
    manager._resolving = True            # résolution encore en vol
    manager.play("ponce")
    assert manager.arrets == []


def test_arreter_sans_stream_courant_n_annonce_rien(manager):
    """`stop` est appelé à la fermeture de la fenêtre et au changement d'écran :
    émettre à vide y déclencherait un nettoyage de plus à chaque fois."""
    manager.stop()
    assert manager.arrets == []


def test_arreter_libere_le_streamer_courant(manager):
    manager.play("zerator")
    manager.stop()
    assert manager.arrets == ["zerator"]
    assert manager.current_login == ""


def test_une_resolution_devancee_par_un_autre_clic_est_abandonnee(manager):
    """Deux clics rapprochés : la première résolution finit APRÈS la seconde.

    Sans ce contrôle, l'URL périmée écraserait celle du streamer réellement
    demandé — on cliquait sur l'un et on regardait l'autre.
    """
    manager.play("ponce")
    manager.prets.clear()
    manager._resolve_worker("zerator", "best")   # réponse tardive du 1er clic
    assert manager.prets == []
    assert manager.erreurs == []
    assert manager.current_login == "ponce"
