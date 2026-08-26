# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Vérification d'intégrité de libmpv-2.dll.

C'est du code natif non signé, chargé dans le processus : la comparaison
d'empreinte est la seule garantie de provenance dont dispose l'application.
Deux exigences opposées se croisent ici, et ce sont elles que ces tests
tiennent :

- une DLL substituée doit être détectée — un False qui deviendrait True par
  distraction annulerait la protection entière ;
- la vérification ne doit JAMAIS empêcher le démarrage. Sur macOS et Linux la
  DLL n'existe pas du tout, et n'importe quelle avarie de lecture doit se
  traduire par un « non applicable », pas par une exception.

D'où le tri à trois valeurs : True vérifié, False compromis, None non
applicable. Les vrais fichiers sont détournés vers `tmp_path`, sinon le
résultat dépendrait de la présence d'une DLL sur la machine de test.
"""

from __future__ import annotations

import hashlib

import pytest

from core import libmpv_check

CONTENU = b"ceci tient lieu de bibliotheque native"
EMPREINTE = hashlib.sha256(CONTENU).hexdigest()


@pytest.fixture
def faux_fichiers(tmp_path, monkeypatch):
    """Détourne les deux chemins du module vers un dossier temporaire.

    Le module les fige à l'import : c'est donc sur ses attributs qu'il faut
    intervenir, pas sur `PROJECT_ROOT`.
    """
    dll = tmp_path / "libmpv-2.dll"
    sha = tmp_path / "libmpv-2.dll.sha256"
    monkeypatch.setattr(libmpv_check, "LIBMPV_PATH", dll)
    monkeypatch.setattr(libmpv_check, "SHA256_PATH", sha)
    return dll, sha


# ── cas nominaux ─────────────────────────────────────────────────────────────

def test_empreinte_conforme(faux_fichiers):
    dll, sha = faux_fichiers
    dll.write_bytes(CONTENU)
    sha.write_text(f"{EMPREINTE}  libmpv-2.dll\n", encoding="utf-8")
    assert libmpv_check.verify_libmpv() is True


def test_une_dll_remplacee_est_detectee(faux_fichiers):
    """Le cas que tout le module existe pour attraper : même nom, même place,
    contenu différent."""
    dll, sha = faux_fichiers
    dll.write_bytes(b"une autre bibliotheque, glissee a la place")
    sha.write_text(f"{EMPREINTE}  libmpv-2.dll\n", encoding="utf-8")
    assert libmpv_check.verify_libmpv() is False


def test_une_dll_tronquee_est_detectee(faux_fichiers):
    """Téléchargement interrompu : le fichier existe mais est incomplet."""
    dll, sha = faux_fichiers
    dll.write_bytes(CONTENU[:10])
    sha.write_text(f"{EMPREINTE}  libmpv-2.dll\n", encoding="utf-8")
    assert libmpv_check.verify_libmpv() is False


# ── vérification non applicable ──────────────────────────────────────────────

def test_dll_absente_rend_none(faux_fichiers):
    """Cas normal sur macOS et Linux : libmpv vient du gestionnaire de paquets,
    aucune DLL n'accompagne l'application. Ce n'est pas un échec."""
    _, sha = faux_fichiers
    sha.write_text(f"{EMPREINTE}  libmpv-2.dll\n", encoding="utf-8")
    assert libmpv_check.verify_libmpv() is None


def test_reference_absente_rend_none(faux_fichiers):
    """Sans empreinte de référence, il n'y a rien à comparer — refuser le
    démarrage sur cette seule base serait disproportionné."""
    dll, _ = faux_fichiers
    dll.write_bytes(CONTENU)
    assert libmpv_check.verify_libmpv() is None


def test_les_deux_fichiers_absents_rendent_none(faux_fichiers):
    assert libmpv_check.verify_libmpv() is None


@pytest.mark.parametrize("reference", [
    "",                         # fichier vide
    "\n\n",                     # que des retours à la ligne
    "   ",                      # que des espaces
])
def test_reference_vide_rend_none_sans_lever(faux_fichiers, reference):
    """Un .sha256 vide ne doit pas faire remonter d'IndexError au démarrage."""
    dll, sha = faux_fichiers
    dll.write_bytes(CONTENU)
    sha.write_text(reference, encoding="utf-8")
    assert libmpv_check.verify_libmpv() is None


def test_reference_binaire_illisible_rend_none(faux_fichiers):
    """Fichier corrompu : la lecture en UTF-8 échoue, sans conséquence."""
    dll, sha = faux_fichiers
    dll.write_bytes(CONTENU)
    sha.write_bytes(b"\xff\xfe\x00 pas de l'UTF-8")
    assert libmpv_check.verify_libmpv() is None


def test_dll_illisible_rend_none(faux_fichiers, monkeypatch):
    """DLL verrouillée par un antivirus ou droits refusés : la vérification
    abandonne, l'application démarre quand même."""
    dll, sha = faux_fichiers
    dll.write_bytes(CONTENU)
    sha.write_text(f"{EMPREINTE}  libmpv-2.dll\n", encoding="utf-8")

    def refuser(_path):
        raise PermissionError("fichier verrouillé")

    monkeypatch.setattr(libmpv_check, "_sha256", refuser)
    assert libmpv_check.verify_libmpv() is None


def test_un_dossier_a_la_place_de_la_dll_rend_none(faux_fichiers):
    """`is_file()` doit trancher, pas `exists()`."""
    dll, sha = faux_fichiers
    dll.mkdir()
    sha.write_text(f"{EMPREINTE}  libmpv-2.dll\n", encoding="utf-8")
    assert libmpv_check.verify_libmpv() is None


# ── lecture du fichier de référence ──────────────────────────────────────────

@pytest.mark.parametrize("ligne", [
    f"{EMPREINTE}  libmpv-2.dll",           # format shasum, deux espaces
    f"{EMPREINTE} *libmpv-2.dll",           # format binaire de sha256sum
    f"{EMPREINTE}\tlibmpv-2.dll",           # séparé par une tabulation
    EMPREINTE,                              # empreinte nue
    f"  {EMPREINTE}  libmpv-2.dll  \n",     # entourée d'espaces
    f"{EMPREINTE.upper()}  libmpv-2.dll",   # certains outils écrivent en capitales
    f"{EMPREINTE}  libmpv-2.dll\n{EMPREINTE}  autre.dll",  # plusieurs lignes
])
def test_les_formes_courantes_du_sha256_sont_acceptees(faux_fichiers, ligne):
    """Le .sha256 est régénéré à la main lors d'une mise à jour de mpv, avec
    l'outil du moment : un espacement ou une casse inattendus ne doivent pas
    faire passer une DLL saine pour compromise."""
    dll, sha = faux_fichiers
    dll.write_bytes(CONTENU)
    sha.write_text(ligne, encoding="utf-8")
    assert libmpv_check.verify_libmpv() is True


def test_seule_la_premiere_ligne_du_sha256_compte(faux_fichiers):
    """Convention du module : la première ligne fait foi."""
    dll, sha = faux_fichiers
    dll.write_bytes(CONTENU)
    sha.write_text(f"{'0' * 64}  libmpv-2.dll\n{EMPREINTE}  libmpv-2.dll",
                   encoding="utf-8")
    assert libmpv_check.verify_libmpv() is False


def test_sha256_calcule_la_meme_chose_que_hashlib(tmp_path):
    """La lecture par blocs d'un mégaoctet doit donner le même résultat qu'une
    lecture d'un seul tenant, y compris au-delà de la taille d'un bloc."""
    gros = tmp_path / "gros.bin"
    donnees = b"z" * (1024 * 1024 * 2 + 17)
    gros.write_bytes(donnees)
    assert libmpv_check._sha256(gros) == hashlib.sha256(donnees).hexdigest()


def test_sha256_d_un_fichier_vide(tmp_path):
    vide = tmp_path / "vide.bin"
    vide.write_bytes(b"")
    assert libmpv_check._sha256(vide) == hashlib.sha256(b"").hexdigest()


def test_expected_digest_normalise_en_minuscules(faux_fichiers):
    """La comparaison finale est sensible à la casse."""
    _, sha = faux_fichiers
    sha.write_text(f"{EMPREINTE.upper()}  libmpv-2.dll\n", encoding="utf-8")
    assert libmpv_check._expected_digest() == EMPREINTE


def test_les_chemins_par_defaut_visent_la_racine_des_ressources():
    """Empaquetée, la DLL est extraite auprès du code, pas dans le dépôt : le
    module doit partir de PROJECT_ROOT et non du répertoire courant."""
    from core.paths import PROJECT_ROOT
    assert libmpv_check.LIBMPV_PATH == PROJECT_ROOT / "libmpv-2.dll"
    assert libmpv_check.SHA256_PATH == PROJECT_ROOT / "libmpv-2.dll.sha256"
