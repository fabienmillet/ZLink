# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Poser l'extension Stream Deck depuis ZLink, sans toucher à la vraie.

Tout se passe dans des dossiers jetables : un test qui écrirait dans
`%APPDATA%\\Elgato` remplacerait l'extension réellement installée par celle du
banc d'essai, et le boîtier de la personne qui lance la suite cesserait de
répondre.

Ce qui est éprouvé ici, ce sont les cas où l'installation NE DOIT PAS avoir
lieu : logiciel absent, exécutable manquant, fichier verrouillé. Le chemin
heureux est le plus facile à écrire et le moins susceptible de surprendre.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from core import streamdeck_install as sdi


def _extension(racine, version="1.0.0.0", avec_exe=True):
    """Une extension livrée, telle que ZLink l'embarque."""
    dossier = racine / "streamdeck" / sdi.NOM_PLUGIN
    (dossier / "pi").mkdir(parents=True)
    (dossier / "icones").mkdir()
    (dossier / "manifest.json").write_text(
        json.dumps({"Name": "ZLink", "Version": version}), encoding="utf-8")
    (dossier / "pi" / "flux.html").write_text("<html>", encoding="utf-8")
    (dossier / "icones" / "plugin.png").write_bytes(b"\x89PNG")
    if avec_exe:
        (dossier / sdi.NOM_EXE).write_bytes(b"MZ")
    return dossier


@pytest.fixture
def livree(tmp_path, monkeypatch):
    """L'extension côté ZLink, et un dossier Elgato qui existe."""
    monkeypatch.setattr(sdi, "RESOURCE_ROOT", tmp_path / "zlink")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    (tmp_path / "appdata" / "Elgato" / "StreamDeck" / "Plugins").mkdir(
        parents=True)
    return _extension(tmp_path / "zlink")


# ── ce qu'on sait avant d'agir ───────────────────────────────────────────────

def test_sans_logiciel_elgato_rien_n_est_propose(tmp_path, monkeypatch):
    """Proposer d'installer une extension pour un logiciel absent n'a aucun sens."""
    monkeypatch.setattr(sdi, "RESOURCE_ROOT", tmp_path / "zlink")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    _extension(tmp_path / "zlink")

    assert sdi.dossier_elgato() is None
    situation = sdi.etat()
    assert not situation["logiciel"]
    assert not situation["possible"]
    assert "Stream Deck" in situation["raison"]


def test_une_extension_sans_executable_est_annoncee_incomplete(
        tmp_path, monkeypatch):
    """Depuis un dépôt cloné, l'exécutable n'existe pas encore.

    Le bouton doit le dire plutôt que de lancer une copie qui produirait une
    extension inerte, qu'Elgato refuserait sans un mot.
    """
    monkeypatch.setattr(sdi, "RESOURCE_ROOT", tmp_path / "zlink")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    (tmp_path / "appdata" / "Elgato" / "StreamDeck" / "Plugins").mkdir(parents=True)
    _extension(tmp_path / "zlink", avec_exe=False)

    situation = sdi.etat()
    assert not situation["possible"]
    assert sdi.NOM_EXE in situation["raison"]


def test_avant_installation_l_etat_dit_qu_il_n_y_a_rien(livree):
    situation = sdi.etat()
    assert situation["possible"]
    assert situation["disponible"] == "1.0.0.0"
    assert situation["installee"] == ""
    assert not situation["a_jour"]


# ── l'installation ───────────────────────────────────────────────────────────

def test_l_installation_depose_tous_les_fichiers(livree):
    reussi, message = sdi.installer()
    assert reussi, message

    pose = sdi.dossier_elgato() / sdi.NOM_PLUGIN
    assert (pose / "manifest.json").exists()
    assert (pose / "pi" / "flux.html").exists()
    assert (pose / "icones" / "plugin.png").exists()
    assert (pose / sdi.NOM_EXE).exists()
    assert "relancer" in message.lower()


def test_l_installation_n_emporte_pas_le_journal_ni_les_caches(livree):
    """Le journal du plugin et les .pyc n'ont rien à faire dans une livraison."""
    (livree / "zlink-deck.log").write_text("bruit", encoding="utf-8")
    (livree / "__pycache__").mkdir()
    (livree / "__pycache__" / "zlink_deck.pyc").write_bytes(b"\x00")

    assert sdi.installer()[0]
    pose = sdi.dossier_elgato() / sdi.NOM_PLUGIN
    assert not (pose / "zlink-deck.log").exists()
    assert not (pose / "__pycache__").exists()


def test_reinstaller_remplace_l_ancienne_version(livree):
    assert sdi.installer()[0]
    pose = sdi.dossier_elgato() / sdi.NOM_PLUGIN
    (pose / "vestige.txt").write_text("d'une version d'avant", encoding="utf-8")

    assert sdi.installer()[0]
    assert not (pose / "vestige.txt").exists(), (
        "un fichier d'une version précédente survit à la réinstallation")


def test_apres_installation_l_etat_dit_a_jour(livree):
    sdi.installer()
    situation = sdi.etat()
    assert situation["installee"] == "1.0.0.0"
    assert situation["a_jour"]


def test_une_version_plus_recente_n_est_pas_dite_a_jour(livree, tmp_path):
    sdi.installer()
    manifeste = livree / "manifest.json"
    manifeste.write_text(json.dumps({"Version": "1.1.0.0"}), encoding="utf-8")

    situation = sdi.etat()
    assert situation["installee"] == "1.0.0.0"
    assert situation["disponible"] == "1.1.0.0"
    assert not situation["a_jour"]
    assert situation["possible"], "une mise à jour doit rester possible"


def test_un_reste_d_installation_interrompue_ne_bloque_pas(livree):
    """Le dossier provisoire d'une tentative précédente est balayé."""
    reste = sdi.dossier_elgato() / (sdi.NOM_PLUGIN + ".neuf")
    reste.mkdir(parents=True)
    (reste / "moitie.txt").write_text("interrompu", encoding="utf-8")

    reussi, message = sdi.installer()
    assert reussi, message
    assert not reste.exists()


# ── les refus ────────────────────────────────────────────────────────────────

def test_un_fichier_verrouille_donne_une_consigne_claire(livree, monkeypatch):
    """Le logiciel Elgato tient l'exécutable ouvert tant qu'il tourne."""
    def refuser(*_args, **_kwargs):
        raise PermissionError("fichier utilisé par un autre processus")

    monkeypatch.setattr(sdi.shutil, "copy2", refuser)
    reussi, message = sdi.installer()
    assert not reussi
    assert "quitter" in message.lower()


def test_une_erreur_disque_ne_fait_pas_tomber_l_application(livree, monkeypatch):
    def casser(*_args, **_kwargs):
        raise OSError("disque plein")

    monkeypatch.setattr(sdi.shutil, "copy2", casser)
    reussi, message = sdi.installer()
    assert not reussi
    assert "disque plein" in message


def test_on_n_installe_pas_ce_qu_on_a_dit_impossible(tmp_path, monkeypatch):
    """Le refus annoncé par `etat()` est celui qu'applique `installer()`."""
    monkeypatch.setattr(sdi, "RESOURCE_ROOT", tmp_path / "zlink")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    _extension(tmp_path / "zlink")          # pas de dossier Elgato

    reussi, message = sdi.installer()
    assert not reussi
    assert message == sdi.etat()["raison"]


# ── La livraison : l'extension part-elle vraiment avec l'application ─────────
#
# Le bouton d'installation ne pose que ce qu'il a sous la main. Si la recette
# d'empaquetage cesse d'embarquer l'extension, ou si la CI la construit APRÈS
# l'application, la version publiée s'installe sans elle — et le bouton se
# contente de dire « extension incomplète » sans que rien n'ait prévenu.

RACINE = pathlib.Path(__file__).resolve().parent.parent


def test_la_recette_embarque_l_extension():
    recette = (RACINE / "ZLink.spec").read_text(encoding="utf-8")
    assert "donnees.append((_EXTENSION, _EXTENSION))" in recette
    assert sdi.NOM_PLUGIN in recette


def test_l_extension_atterrit_la_ou_l_application_la_cherche():
    """La destination du paquet doit être le chemin que `source()` reconstruit."""
    recette = (RACINE / "ZLink.spec").read_text(encoding="utf-8")
    assert f'os.path.join("streamdeck", "{sdi.NOM_PLUGIN}")' in recette
    attendu = pathlib.PurePath("streamdeck") / sdi.NOM_PLUGIN
    obtenu = sdi.source().relative_to(sdi.RESOURCE_ROOT)
    assert obtenu == attendu


def test_la_ci_construit_l_extension_avant_l_application():
    """Empaquetée avant d'exister, elle serait absente de la version publiée."""
    flux = (RACINE / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8")
    extension = flux.index("streamdeck/construire.py")
    application = flux.index("pyinstaller --noconfirm --clean ZLink.spec")
    assert extension < application,         "l'extension est construite après l'empaquetage : elle en sera absente"


def test_la_ci_refuse_de_publier_une_extension_sans_executable():
    """Sans exécutable, l'extension livrée serait inerte."""
    flux = (RACINE / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8")
    assert f"test -f streamdeck/{sdi.NOM_PLUGIN}/{sdi.NOM_EXE}" in flux
