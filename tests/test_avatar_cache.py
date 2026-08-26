# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Cache disque des avatars.

Ce module existe pour deux raisons, et ce sont elles qu'on vérifie ici :

- ne pas retélécharger ce qui est déjà là, y compris quand deux threads
  réclament la même image en même temps — c'était 70 % de trafic inutile ;
- ne rien écrire hors du cache et ne rien chercher ailleurs qu'en https, parce
  que la clé et l'URL viennent d'APIs tierces.

Aucun test ne sort sur le réseau : `urlopen` est toujours remplacé, et le cache
est détourné vers `tmp_path`.
"""

from __future__ import annotations

import threading
import urllib.request

import pytest

from core import avatar_cache


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """Cache neuf dans `tmp_path`, sans verrou hérité du test précédent.

    `_LOCKS` est un état de module : le conserver ferait qu'un test de
    concurrence attende un verrou posé ailleurs, et ne prouve plus rien.
    """
    dossier = tmp_path / "avatars"
    monkeypatch.setattr(avatar_cache, "CACHE_DIR", dossier)
    monkeypatch.setattr(avatar_cache, "_LOCKS", {})
    return dossier


class _FausseReponse:
    """Le strict minimum de ce que `download` attend d'`urlopen`."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self, taille: int = -1) -> bytes:
        return self._payload if taille < 0 else self._payload[:taille]

    def __enter__(self) -> "_FausseReponse":
        return self

    def __exit__(self, *_) -> bool:
        return False


def _intercepte_urlopen(monkeypatch, payload: bytes = b"\x89PNG-faux",
                        leve: BaseException | None = None) -> list:
    """Remplace `urlopen` et renvoie la liste des requêtes qu'il a vues.

    La liste est le point d'observation de tous les tests « aucune requête ne
    doit partir » : un refus qui laisserait quand même passer l'appel réseau
    serait une régression silencieuse.
    """
    vues: list = []

    def _ouvrir(requete, timeout=None):
        vues.append(requete)
        if leve is not None:
            raise leve
        return _FausseReponse(payload)

    monkeypatch.setattr(urllib.request, "urlopen", _ouvrir)
    return vues


# ── emplacement des fichiers ─────────────────────────────────────────────────

@pytest.mark.parametrize("cle", ["zerator", "Ponce", "avatar-123"])
def test_path_for_place_le_fichier_dans_le_cache(cache, cle):
    chemin = avatar_cache.path_for(cle)
    assert chemin.parent == cache
    assert chemin.name == f"{cle}.png"


# ── refus sans requête réseau ────────────────────────────────────────────────

@pytest.mark.parametrize("cle,url", [
    ("", "https://exemple.test/a.png"),
    (None, "https://exemple.test/a.png"),
    ("zerator", ""),
    ("zerator", None),
    ("", ""),
])
def test_cle_ou_url_vide_refusee(cache, monkeypatch, cle, url):
    vues = _intercepte_urlopen(monkeypatch)
    assert avatar_cache.download(cle, url) is False
    assert vues == []


@pytest.mark.parametrize("url", [
    "http://exemple.test/a.png",
    "file:///etc/passwd",
    "ftp://exemple.test/a.png",
    "//exemple.test/a.png",
    "exemple.test/a.png",
])
def test_url_non_https_refusee(cache, monkeypatch, url):
    """L'URL vient d'une API tierce ; `urlopen` accepterait file:// et ftp://."""
    vues = _intercepte_urlopen(monkeypatch)
    assert avatar_cache.download("zerator", url) is False
    assert vues == []
    assert not cache.exists() or list(cache.iterdir()) == []


def test_url_https_majuscule_acceptee(cache, monkeypatch):
    """Le filtre compare en minuscules : « HTTPS:// » reste une URL valide."""
    _intercepte_urlopen(monkeypatch)
    assert avatar_cache.download("zerator", "HTTPS://exemple.test/a.png") is True


@pytest.mark.parametrize("cle", ["../evil", "sub/evil", "../../etc/passwd"])
def test_cle_qui_sort_du_cache_refusee(cache, monkeypatch, cle):
    """`pathlib` ne normalise pas « .. » : la clé sert de nom de fichier."""
    vues = _intercepte_urlopen(monkeypatch)
    assert avatar_cache.download(cle, "https://exemple.test/a.png") is False
    assert vues == []
    assert not avatar_cache.path_for(cle).exists()


# ── téléchargement ───────────────────────────────────────────────────────────

def test_telechargement_ecrit_le_fichier(cache, monkeypatch):
    vues = _intercepte_urlopen(monkeypatch, b"contenu-png")
    assert avatar_cache.download("zerator", "https://exemple.test/a.png") is True
    assert avatar_cache.path_for("zerator").read_bytes() == b"contenu-png"
    assert len(vues) == 1
    assert vues[0].full_url == "https://exemple.test/a.png"
    assert vues[0].get_header("User-agent") == "ZLink/1.0"


def test_fichier_deja_present_evite_la_requete(cache, monkeypatch):
    """Le cœur du sujet : trois appelants réclament les mêmes images."""
    cache.mkdir(parents=True)
    avatar_cache.path_for("zerator").write_bytes(b"deja-la")
    vues = _intercepte_urlopen(monkeypatch)
    assert avatar_cache.download("zerator", "https://exemple.test/a.png") is True
    assert vues == []
    assert avatar_cache.path_for("zerator").read_bytes() == b"deja-la"


def test_reponse_trop_grosse_ignoree(cache, monkeypatch):
    """Un avatar est une vignette ; au-delà du plafond, on ne met rien en cache."""
    monkeypatch.setattr(avatar_cache, "MAX_BYTES", 8)
    _intercepte_urlopen(monkeypatch, b"0123456789")
    assert avatar_cache.download("zerator", "https://exemple.test/a.png") is False
    assert not avatar_cache.path_for("zerator").exists()


def test_reponse_exactement_au_plafond_acceptee(cache, monkeypatch):
    """La comparaison est stricte : la taille limite passe encore."""
    monkeypatch.setattr(avatar_cache, "MAX_BYTES", 8)
    _intercepte_urlopen(monkeypatch, b"01234567")
    assert avatar_cache.download("zerator", "https://exemple.test/a.png") is True
    assert avatar_cache.path_for("zerator").read_bytes() == b"01234567"


@pytest.mark.parametrize("erreur", [
    OSError("réseau coupé"),
    TimeoutError("trop lent"),
    ValueError("URL bancale"),
])
def test_echec_de_telechargement_ne_leve_pas(cache, monkeypatch, erreur):
    """Un avatar manquant se dégrade en initiales : ce n'est pas une erreur."""
    _intercepte_urlopen(monkeypatch, leve=erreur)
    assert avatar_cache.download("zerator", "https://exemple.test/a.png") is False
    assert not avatar_cache.path_for("zerator").exists()


def test_aucun_fichier_temporaire_ne_survit_a_un_echec(cache, monkeypatch):
    """Qt décode un PNG tronqué sans le signaler : un reliquat serait mis en
    cache définitivement sous forme de vignette abîmée."""
    monkeypatch.setattr(avatar_cache, "MAX_BYTES", 4)
    _intercepte_urlopen(monkeypatch, b"beaucoup trop long")
    avatar_cache.download("zerator", "https://exemple.test/a.png")
    assert list(cache.glob("*.tmp")) == []
    assert list(cache.iterdir()) == []


def test_le_fichier_final_ne_porte_pas_de_suffixe_temporaire(cache, monkeypatch):
    """L'écriture passe par un fichier intermédiaire, qui doit être renommé."""
    _intercepte_urlopen(monkeypatch, b"png")
    avatar_cache.download("zerator", "https://exemple.test/a.png")
    assert [c.name for c in cache.iterdir()] == ["zerator.png"]


# ── verrou par clé ───────────────────────────────────────────────────────────

def test_une_cle_a_toujours_le_meme_verrou(cache):
    assert avatar_cache._lock_for("a") is avatar_cache._lock_for("a")


def test_deux_cles_ont_des_verrous_distincts(cache):
    """Sinon deux avatars différents se sérialiseraient sans raison."""
    assert avatar_cache._lock_for("a") is not avatar_cache._lock_for("b")


def test_deux_threads_sur_la_meme_cle_ne_telechargent_qu_une_fois(cache, monkeypatch):
    """Le second demandeur attend, puis trouve le fichier et n'émet rien.

    C'est la raison d'être du module : 510 requêtes pour 300 images distinctes.
    """
    barriere = threading.Barrier(2)
    vues: list = []
    garde = threading.Lock()

    def _ouvrir(requete, timeout=None):
        with garde:
            vues.append(requete)
        return _FausseReponse(b"png")

    monkeypatch.setattr(urllib.request, "urlopen", _ouvrir)

    resultats: list = []

    def _demander() -> None:
        # La barrière garantit que les deux threads franchissent le test
        # « le fichier existe ? » d'avant-verrou avant toute écriture.
        barriere.wait()
        resultats.append(
            avatar_cache.download("zerator", "https://exemple.test/a.png")
        )

    threads = [threading.Thread(target=_demander) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive(), "verrou non relâché"

    assert resultats == [True, True]
    assert len(vues) == 1, "la seconde demande a retéléchargé"
