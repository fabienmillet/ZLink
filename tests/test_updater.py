# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Mises à jour : vérification de signature, appel à GitHub, décision d'alerter.

Le cœur du fichier est `verify_signature`. C'est le seul rempart entre une
release publiée par l'auteur et une release publiée par quelqu'un d'autre :
une régression qui la ferait renvoyer True à tort transformerait le canal de
mise à jour en canal d'exécution de code arbitraire. Les tests fabriquent donc
une VRAIE paire de clés Ed25519 et signent un contenu de référence, plutôt que
de simuler la bibliothèque de crypto — un test qui détourne l'appel qu'il
prétend vérifier ne prouve rien.

Le reste (`fetch_latest`, `UpdateChecker`) est du réseau : entièrement
détourné, aucun test ne doit sortir sur Internet.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

from core import updater
from core.version import __version__

#: Contenu d'artefact de référence : ce qui est signé dans les tests.
CONTENU = b"ZLink-0.1.0-windows.exe (artefact de reference pour les tests)"


@pytest.fixture(scope="module")
def paire():
    """Une paire Ed25519 fabriquée pour la session de test.

    Portée module : générer une clé coûte, et rien ici ne la modifie.
    Retourne (clé publique en hexadécimal brut, signataire prêt à l'emploi).
    """
    cle = ECC.generate(curve="ed25519")
    return (cle.public_key().export_key(format="raw").hex(),
            eddsa.new(cle, "rfc8032"))


@pytest.fixture
def cle_du_projet(paire, monkeypatch):
    """Fait passer la clé de test pour la clé embarquée dans l'application."""
    monkeypatch.setattr(updater, "RELEASE_PUBKEY_HEX", paire[0])
    return paire[1]


# ── verify_signature — le cas nominal ────────────────────────────────────────

def test_une_signature_authentique_est_acceptee(cle_du_projet):
    """Sans ce test, un durcissement excessif pourrait rejeter toute release."""
    signature = cle_du_projet.sign(CONTENU)
    assert len(signature) == 64, "Ed25519 produit 64 octets"
    assert updater.verify_signature(CONTENU, signature) is True


def test_un_artefact_vide_correctement_signe_est_accepte(cle_du_projet):
    """Ed25519 signe le message vide : « vide » n'est pas « invalide ».

    La distinction compte, car c'est la SIGNATURE absente qui doit refuser,
    pas la taille du contenu.
    """
    assert updater.verify_signature(b"", cle_du_projet.sign(b"")) is True


# ── verify_signature — tout le reste doit refuser ────────────────────────────

def test_la_signature_d_un_autre_contenu_est_refusee(cle_du_projet):
    """Le scénario réel de substitution : bonne signature, mauvais binaire."""
    signature = cle_du_projet.sign(CONTENU)
    assert updater.verify_signature(CONTENU + b"!", signature) is False


def test_la_signature_d_une_autre_cle_est_refusee(cle_du_projet):
    """Une clé valide mais étrangère au projet ne doit rien autoriser."""
    intruse = eddsa.new(ECC.generate(curve="ed25519"), "rfc8032")
    assert updater.verify_signature(CONTENU, intruse.sign(CONTENU)) is False


def _tronquee(sig):
    return sig[:32]


def _allongee(sig):
    return sig + b"\x00"


def _premier_octet_modifie(sig):
    return bytes([sig[0] ^ 0x01]) + sig[1:]


def _dernier_octet_modifie(sig):
    return sig[:-1] + bytes([sig[-1] ^ 0x01])


def _remise_a_zero(sig):
    return b"\x00" * 64


@pytest.mark.parametrize("abimer", [
    _tronquee,
    _allongee,
    _premier_octet_modifie,
    _dernier_octet_modifie,
    _remise_a_zero,
], ids=["tronquée", "allongée", "premier octet", "dernier octet", "nulle"])
def test_une_signature_abimee_est_refusee(cle_du_projet, abimer):
    """Aucune altération, si petite soit-elle, ne doit passer."""
    signature = cle_du_projet.sign(CONTENU)
    assert updater.verify_signature(CONTENU, abimer(signature)) is False


@pytest.mark.parametrize("signature", [b"", None], ids=["vide", "absente"])
def test_sans_signature_c_est_non(cle_du_projet, signature):
    """« Pas de signature » doit se comporter comme « mauvaise signature »."""
    assert updater.verify_signature(CONTENU, signature) is False


@pytest.mark.parametrize("cle_hex,pourquoi", [
    ("", "aucune clé configurée : on ne peut rien garantir"),
    ("00" * 31, "trop courte d'un octet"),
    ("00" * 33, "trop longue d'un octet"),
    ("abc", "longueur hexadécimale impaire"),
    ("zz" * 32, "hors de l'alphabet hexadécimal"),
    ("pas une clé du tout", "chaîne quelconque"),
])
def test_une_cle_publique_inutilisable_refuse_tout(paire, monkeypatch,
                                                   cle_hex, pourquoi):
    """Une clé absente ou corrompue ne doit jamais valoir « accepté ».

    C'est le comportement le plus important du module : « je ne sais pas »
    doit se comporter comme « non », y compris quand la clé embarquée a été
    abîmée par une mauvaise fusion.
    """
    signature = paire[1].sign(CONTENU)
    monkeypatch.setattr(updater, "RELEASE_PUBKEY_HEX", cle_hex)
    assert updater.verify_signature(CONTENU, signature) is False, pourquoi


def test_la_cle_embarquee_est_exploitable():
    """La clé livrée avec l'application doit être importable telle quelle.

    Une coquille d'un caractère dans `RELEASE_PUBKEY_HEX` ne se voit nulle
    part et ferait échouer TOUTES les vérifications, silencieusement.
    """
    brut = bytes.fromhex(updater.RELEASE_PUBKEY_HEX)
    assert len(brut) == 32, "Ed25519 : clé publique brute de 32 octets"
    assert eddsa.import_public_key(brut) is not None


def test_un_echec_de_verification_est_journalise(cle_du_projet, caplog):
    """Une signature refusée doit laisser une trace : sinon, personne ne saura
    pourquoi une mise à jour légitime n'est jamais proposée."""
    with caplog.at_level("WARNING", logger=updater.logger.name):
        updater.verify_signature(CONTENU, b"\x00" * 64)
    assert any("ignature" in enr.message for enr in caplog.records)


# ── fetch_latest — le réseau, entièrement simulé ─────────────────────────────

class _Reponse:
    """Le strict minimum de ce que `fetch_latest` attend d'urlopen."""

    def __init__(self, corps: bytes):
        self._corps = corps

    def read(self, taille=-1):
        return self._corps if taille < 0 else self._corps[:taille]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def reseau(monkeypatch):
    """Remplace urlopen. Le poser systématiquement empêche toute sortie réelle."""
    def poser(reponse):
        def faux_urlopen(req, timeout=None):
            if isinstance(reponse, Exception):
                raise reponse
            faux_urlopen.vu = req
            return _Reponse(reponse)
        faux_urlopen.vu = None
        monkeypatch.setattr(urllib.request, "urlopen", faux_urlopen)
        return faux_urlopen
    return poser


def test_une_release_lisible_est_rendue_telle_quelle(reseau):
    corps = json.dumps({"tag_name": "v1.0.0"}).encode("utf-8")
    reseau(corps)
    assert updater.fetch_latest() == {"tag_name": "v1.0.0"}


def test_la_requete_s_annonce(reseau):
    """GitHub limite sévèrement les appels sans User-Agent identifiable."""
    poseur = reseau(b"{}")
    updater.fetch_latest()
    entetes = {k.lower(): v for k, v in poseur.vu.header_items()}
    assert entetes["user-agent"] == f"ZLink/{__version__}"
    assert "github" in entetes["accept"]


def _http(code):
    return urllib.error.HTTPError(updater._API, code, "erreur", {}, None)


@pytest.mark.parametrize("panne", [
    _http(404),                       # aucune release publiée
    _http(403),                       # quota dépassé
    _http(500),
    urllib.error.URLError("hors ligne"),
    TimeoutError("trop lent"),
    b"{ ceci n'est pas du json",
    b"",
], ids=["404", "403", "500", "hors ligne", "délai", "json cassé", "corps vide"])
def test_une_panne_se_solde_par_un_silence(reseau, panne):
    """Une mise à jour indisponible ne doit jamais déranger l'utilisateur."""
    reseau(panne)
    assert updater.fetch_latest() is None


def test_un_json_qui_n_est_pas_un_objet_est_rejete(reseau):
    """La réponse doit être un objet JSON — le reste n'est pas une release.

    `_worker` fait confiance à `fetch_latest` et appelle directement
    `data.get()` : tout ce qui n'est pas un dict devrait être filtré ici.
    """
    reseau(b'["une liste, pas un objet de release"]')
    assert updater.fetch_latest() is None


def test_une_reponse_demesuree_est_ignoree(reseau):
    """Garde-fou mémoire : une release GitHub reste petite."""
    reseau(b"x" * (updater._MAX_BYTES + 10))
    assert updater.fetch_latest() is None


def test_une_reponse_juste_a_la_limite_passe(reseau):
    """La borne est un « au-delà », pas un « à partir de » : à vérifier, une
    inégalité inversée couperait les réponses parfaitement valides."""
    bourrage = " " * (updater._MAX_BYTES - len(b'{"tag_name":"v1.0.0"}'))
    reseau(('{"tag_name":"v1.0.0"' + bourrage + "}").encode("utf-8"))
    assert updater.fetch_latest() == {"tag_name": "v1.0.0"}


# ── UpdateChecker — la décision d'alerter ────────────────────────────────────

@pytest.fixture
def alertes(monkeypatch):
    """Un UpdateChecker dont on collecte les signaux, sans réseau."""
    def poser(data):
        monkeypatch.setattr(updater, "fetch_latest", lambda: data)
        checker = updater.UpdateChecker()
        recues = []
        checker.update_available.connect(
            lambda version, url: recues.append((version, url)))
        checker._worker()
        return recues
    return poser


def test_une_version_plus_recente_est_signalee(alertes):
    url = "https://github.com/fabienmillet/ZLink/releases/tag/v9.9.9"
    assert alertes({"tag_name": "v9.9.9", "html_url": url}) == [("v9.9.9", url)]


@pytest.mark.parametrize("data,pourquoi", [
    (None, "GitHub injoignable"),
    ({}, "réponse vide"),
    ({"tag_name": "v9.9.9", "html_url": "https://github.com/x/y", "draft": True},
     "un brouillon n'est pas publié"),
    ({"tag_name": "v9.9.9", "html_url": "https://github.com/x/y",
      "prerelease": True},
     "une pré-release ne se propose pas d'office"),
    ({"tag_name": "", "html_url": "https://github.com/x/y"},
     "sans tag, rien à annoncer"),
    ({"tag_name": "v9.9.9", "html_url": ""},
     "sans url, nulle part où envoyer l'utilisateur"),
    ({"tag_name": "v9.9.9", "html_url": "https://exemple.invalide/piege"},
     "une url hors github.com enverrait l'utilisateur n'importe où"),
    ({"tag_name": "v9.9.9",
      "html_url": "https://github.com.exemple.invalide/piege"},
     "un domaine qui ressemble à github.com n'est pas github.com"),
    ({"tag_name": f"v{__version__}",
      "html_url": "https://github.com/x/y"},
     "déjà à jour"),
    ({"tag_name": "v0.0.1", "html_url": "https://github.com/x/y"},
     "plus ancien que l'installé"),
    ({"tag_name": "pas une version", "html_url": "https://github.com/x/y"},
     "numéro illisible"),
], ids=["injoignable", "vide", "brouillon", "pré-release", "sans tag",
        "sans url", "url étrangère", "domaine sosie", "à jour", "plus ancien",
        "illisible"])
def test_aucune_alerte_dans_ces_cas(alertes, data, pourquoi):
    """Une notification de mise à jour interrompt : elle doit être méritée."""
    assert alertes(data) == [], pourquoi


def test_check_ne_bloque_pas_l_appelant(monkeypatch):
    """`check()` est appelé depuis l'interface : il doit rendre la main aussitôt.

    On intercepte la création du fil plutôt que de l'attendre — un test qui
    dépend de l'ordonnancement finit par échouer au hasard.
    """
    lancés = []

    class _FauxFil:
        def __init__(self, target=None, daemon=None, name=None, **_):
            self.target, self.daemon, self.name = target, daemon, name

        def start(self):
            lancés.append(self)

    monkeypatch.setattr(updater.threading, "Thread", _FauxFil)
    monkeypatch.setattr(updater, "fetch_latest", lambda: None)

    updater.UpdateChecker().check()

    assert len(lancés) == 1
    fil = lancés[0]
    assert fil.daemon is True, "l'application ne doit pas attendre ce fil pour quitter"
    fil.target()      # la cible doit être exécutable sans exception
