# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Big Screen : cache d'avatars, défilement de la mosaïque, mise en forme.

Ce module est presque entièrement du dessin, mais trois endroits portent une
vraie logique et ont déjà régressé :

- le cache de pixmaps, dont l'IDENTITÉ des placeholders conditionne la mise en
  cache du gris (20 des 23 ms par image de la mosaïque) ;
- le défilement, exprimé en pixels PAR SECONDE et non par image, sans quoi la
  mosaïque ralentit quand la machine est chargée ;
- les repêchages du chargement d'avatar, qui créaient jadis un thread par clé.

Rien ici n'ouvre de fenêtre plein écran, ne télécharge quoi que ce soit, ni
n'écrit hors de `tmp_path` : le dossier de cache disque est détourné et le pool
de threads est remplacé par un enregistreur.
"""

from __future__ import annotations

import logging
import types

import pytest
from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QColor, QPixmap, QResizeEvent
from PyQt6.QtWidgets import QLabel

from core.api_client import GlobalStats, GoalWithStreamer, StreamerInfo
from widgets import bigscreen_widget as bs


# ── fabriques ────────────────────────────────────────────────────────────────

def streamer(login: str, *, online: bool = True, display: str = "",
             url: str = "") -> StreamerInfo:
    """StreamerInfo minimal — seuls quatre champs sont lus par ce module."""
    return StreamerInfo(
        twitch_login=login, display=display or login.capitalize(),
        online=online, game="", location="", viewers=0, donation=0.0,
        donation_formatted="", profile_url=url,
    )


def redimensionner(widget, largeur: int, hauteur: int) -> None:
    """Redimensionne ET délivre l'événement.

    Qt diffère les QResizeEvent des widgets cachés jusqu'à leur affichage :
    sans cet appel explicite, `resizeEvent` ne serait jamais exécuté ici.
    """
    avant = widget.size()
    widget.resize(largeur, hauteur)
    widget.resizeEvent(QResizeEvent(QSize(largeur, hauteur), avant))


def png(chemin, taille: int = 64, couleur: str = "#804020") -> None:
    """Écrit une vraie image PNG là où le cache disque l'attend."""
    px = QPixmap(taille, taille)
    px.fill(QColor(couleur))
    chemin.parent.mkdir(parents=True, exist_ok=True)
    assert px.save(str(chemin), "PNG")


# ── isolation ────────────────────────────────────────────────────────────────

@pytest.fixture
def cache_disque(tmp_path, monkeypatch, qapp):
    """Détourne le cache disque des avatars hors de ~/.zlink."""
    dossier = tmp_path / "avatars"
    dossier.mkdir()
    monkeypatch.setattr(bs, "_AVATAR_CACHE_DIR", dossier)
    return dossier


@pytest.fixture
def envois(monkeypatch):
    """Remplace le pool de threads : on enregistre au lieu d'exécuter.

    Le pool réel est un singleton de module partagé par toute la session :
    y soumettre depuis un test lancerait de vrais chargements disque, et le
    fermer casserait les tests suivants.
    """
    faits: list[tuple] = []
    monkeypatch.setattr(bs, "_submit_avatar",
                        lambda fn, *args: (faits.append((fn, args)), True)[1])
    return faits


@pytest.fixture
def rappels(monkeypatch):
    """Capture ce qui serait rejoué sur le thread GUI."""
    postes: list = []
    monkeypatch.setattr(bs, "_post_to_gui", postes.append)
    return postes


@pytest.fixture
def cache(cache_disque, envois, rappels, monkeypatch):
    """Cache de pixmaps NEUF, substitué au singleton du module.

    Le singleton vit toute la session : un test qui le remplirait fausserait
    les suivants.
    """
    frais = bs._AvatarPixmapCache()
    monkeypatch.setattr(bs, "_avatar_cache", frais)
    return frais


@pytest.fixture
def horloge(monkeypatch):
    """Temps piloté à la main — le défilement se vérifie à la seconde près."""
    t = {"monotonic": 1000.0, "perf": 500.0}
    monkeypatch.setattr("widgets.bigscreen_widget.time.monotonic",
                        lambda: t["monotonic"])
    monkeypatch.setattr("widgets.bigscreen_widget.time.perf_counter",
                        lambda: t["perf"])
    return t


# ─────────────────────────────────────────────────────────────────────────────
# Transformations de pixmaps
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("taille", [0, -1, -64])
def test_un_gris_de_taille_nulle_rend_un_pixmap_vide(qapp, taille):
    """QPixmap(0, 0) serait mis en cache comme un succès et jamais réparé."""
    source = QPixmap(32, 32)
    source.fill(QColor("#804020"))
    assert bs._grayscale_pixmap(source, taille).isNull()


def test_le_gris_desature_sans_detruire_la_transparence(qapp):
    """Format_Grayscale8 DÉTRUIT l'alpha : les logos détourés viraient au noir."""
    source = QPixmap(32, 32)
    source.fill(QColor(0, 0, 0, 0))
    gris = bs._grayscale_pixmap(source, 32)
    assert gris.toImage().pixelColor(16, 16).alpha() == 0


def test_le_gris_rend_les_trois_canaux_egaux(qapp):
    source = QPixmap(32, 32)
    source.fill(QColor("#804020"))
    couleur = bs._grayscale_pixmap(source, 32).toImage().pixelColor(16, 16)
    assert couleur.red() == couleur.green() == couleur.blue()
    assert 0 < couleur.alpha() < 255, "l'opacité de 0,55 est appliquée ici"


@pytest.mark.parametrize("source_px,cible", [(16, 64), (128, 64), (64, 64)])
def test_le_gris_est_toujours_a_la_taille_demandee(qapp, source_px, cible):
    """Sans mise à l'échelle, une source plus petite laisserait le reste vide."""
    source = QPixmap(source_px, source_px)
    source.fill(QColor("#804020"))
    gris = bs._grayscale_pixmap(source, cible)
    assert (gris.width(), gris.height()) == (cible, cible)
    assert gris.toImage().pixelColor(cible // 2, cible // 2).alpha() > 0


def test_le_rognage_rond_evide_les_coins(qapp):
    source = QPixmap(64, 64)
    source.fill(QColor("#804020"))
    image = bs._circle_pixmap(source, 64).toImage()
    assert image.pixelColor(1, 1).alpha() == 0
    assert image.pixelColor(32, 32).alpha() == 255


def test_le_rognage_carre_garde_le_centre_de_l_image(qapp):
    """KeepAspectRatioByExpanding puis crop centré : pas de déformation."""
    source = QPixmap(100, 50)
    source.fill(QColor("#ff0000"))
    peintre = __import__("PyQt6.QtGui", fromlist=["QPainter"]).QPainter(source)
    peintre.fillRect(50, 0, 50, 50, QColor("#0000ff"))
    peintre.end()

    carre = bs._square_pixmap(source, 50).toImage()
    assert (carre.width(), carre.height()) == (50, 50)
    # La bande retenue va de x=25 à x=75 dans la source : rouge puis bleu.
    assert carre.pixelColor(10, 25).name() == "#ff0000"
    assert carre.pixelColor(40, 25).name() == "#0000ff"


@pytest.mark.parametrize("fabrique,coin_opaque", [
    (bs._initials_pixmap, False),          # rond : coins évidés
    (bs._initials_square_pixmap, True),    # carré : coins pleins
])
def test_les_initiales_ont_la_forme_attendue(qapp, fabrique, coin_opaque):
    px = fabrique("zerator", "ZeratoR", 64)
    assert (px.width(), px.height()) == (64, 64)
    coin = px.toImage().pixelColor(1, 1)
    assert (coin.alpha() == 255) is coin_opaque


@pytest.mark.parametrize("login,display", [
    ("zerator", "ZeratoR"), ("z", ""), ("", "A"), ("", ""),
])
def test_les_initiales_acceptent_n_importe_quel_nom(qapp, login, display):
    """Un login vide vient de l'API, pas de nous : il ne doit pas lever."""
    assert not bs._initials_pixmap(login, display, 32).isNull()
    assert not bs._initials_square_pixmap(login, display, 32).isNull()


# ── lecture du cache disque ──────────────────────────────────────────────────

@pytest.mark.parametrize("charge", [bs._load_avatar_pixmap,
                                    bs._load_square_avatar_pixmap])
def test_un_avatar_absent_du_disque_rend_none(cache_disque, charge):
    assert charge("inconnu", 32) is None


@pytest.mark.parametrize("charge", [bs._load_avatar_pixmap,
                                    bs._load_square_avatar_pixmap])
def test_un_fichier_de_cache_corrompu_rend_none(cache_disque, charge):
    """Un téléchargement interrompu laisse un fichier illisible sur le disque."""
    (cache_disque / "zerator.png").write_bytes(b"ceci n'est pas une image")
    assert charge("zerator", 32) is None


@pytest.mark.parametrize("charge,coin_opaque", [
    (bs._load_avatar_pixmap, False),
    (bs._load_square_avatar_pixmap, True),
])
def test_un_avatar_present_est_charge_a_la_bonne_taille(cache_disque, charge,
                                                        coin_opaque):
    png(cache_disque / "zerator.png")
    px = charge("zerator", 48)
    assert (px.width(), px.height()) == (48, 48)
    assert (px.toImage().pixelColor(1, 1).alpha() == 255) is coin_opaque


# ─────────────────────────────────────────────────────────────────────────────
# Cache de pixmaps
# ─────────────────────────────────────────────────────────────────────────────

def test_le_placeholder_garde_son_identite(cache):
    """C'est l'IDENTITÉ qui compte : elle conditionne la mise en cache du gris.

    Recréé à chaque appel, le placeholder faisait repartir `_grayscale_pixmap`
    de zéro à chaque image — 20 des 23 ms par frame de la mosaïque.
    """
    a = cache._placeholder("zerator@32", "zerator", "ZeratoR", 32, False)
    b = cache._placeholder("zerator@32", "zerator", "ZeratoR", 32, False)
    assert a is b


def test_le_placeholder_carre_et_le_rond_sont_distincts(cache):
    rond = cache._placeholder("zerator@32", "zerator", "Z", 32, False)
    carre = cache._placeholder("zerator@32sq", "zerator", "Z", 32, True)
    assert rond.toImage().pixelColor(1, 1).alpha() == 0
    assert carre.toImage().pixelColor(1, 1).alpha() == 255


@pytest.mark.parametrize("methode,suffixe", [("get", ""), ("get_sq", "sq")])
def test_un_avatar_inconnu_rend_les_initiales_et_declenche_un_chargement(
        cache, envois, methode, suffixe):
    px = getattr(cache, methode)("zerator", "ZeratoR", 32)
    assert px is cache._placeholders[f"zerator@32{suffixe}"]
    assert len(envois) == 1
    assert envois[0][1][:1] == ("zerator",)


@pytest.mark.parametrize("methode", ["get", "get_sq"])
def test_un_chargement_en_cours_n_est_pas_relance(cache, envois, horloge,
                                                  methode):
    """paintEvent redemande le même avatar 20 fois par seconde, par cellule."""
    for _ in range(5):
        getattr(cache, methode)("zerator", "ZeratoR", 32)
    assert len(envois) == 1


@pytest.mark.parametrize("methode,suffixe", [("get", ""), ("get_sq", "sq")])
def test_un_avatar_deja_charge_est_rendu_sans_nouveau_travail(
        cache, envois, methode, suffixe):
    attendu = QPixmap(32, 32)
    cache._cache[f"zerator@32{suffixe}"] = attendu
    assert getattr(cache, methode)("zerator", "ZeratoR", 32) is attendu
    assert envois == []


# ── repêchages ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("charge,suffixe", [("_load", ""), ("_load_sq", "sq")])
@pytest.mark.parametrize("url,delai", [
    ("", 2.0),                       # pas d'URL : le fichier peut arriver via bg
    ("https://x/a.png", 5.0),        # l'URL a échoué : on patiente plus
])
def test_un_echec_programme_un_repechage_date(cache, cache_disque, horloge,
                                              monkeypatch, charge, suffixe,
                                              url, delai):
    """Une date d'expiration, pas un threading.Timer : 300 avatars introuvables
    faisaient jadis 300 threads vivants en permanence."""
    monkeypatch.setattr(bs, "_download_avatar", lambda *a: None)
    cle = f"zerator@32{suffixe}"
    cache._loading[cle] = (url, float("inf"))
    getattr(cache, charge)("zerator", "ZeratoR", 32, cle, url)
    assert cache._loading[cle] == (url, horloge["monotonic"] + delai)


@pytest.mark.parametrize("methode", ["get", "get_sq"])
def test_avant_l_expiration_aucune_nouvelle_tentative(cache, envois, horloge,
                                                      methode):
    getattr(cache, methode)("zerator", "ZeratoR", 32)
    cle = next(iter(cache._loading))
    cache._loading[cle] = ("", horloge["monotonic"] + 2.0)
    horloge["monotonic"] += 1.9
    getattr(cache, methode)("zerator", "ZeratoR", 32)
    assert len(envois) == 1


@pytest.mark.parametrize("methode", ["get", "get_sq"])
def test_apres_l_expiration_on_retente(cache, envois, horloge, methode):
    getattr(cache, methode)("zerator", "ZeratoR", 32)
    cle = next(iter(cache._loading))
    cache._loading[cle] = ("", horloge["monotonic"] + 2.0)
    horloge["monotonic"] += 2.0
    getattr(cache, methode)("zerator", "ZeratoR", 32)
    assert len(envois) == 2


@pytest.mark.parametrize("methode", ["get", "get_sq"])
def test_l_arrivee_d_une_url_court_circuite_l_attente(cache, envois, horloge,
                                                      methode):
    """Le premier essai n'avait pas d'URL : dès qu'elle arrive, on retente."""
    getattr(cache, methode)("zerator", "ZeratoR", 32)
    cle = next(iter(cache._loading))
    cache._loading[cle] = ("", horloge["monotonic"] + 999.0)
    getattr(cache, methode)("zerator", "ZeratoR", 32,
                            profile_url="https://x/a.png")
    assert len(envois) == 2


# ── callbacks ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("methode", ["get", "get_sq"])
def test_le_meme_callback_n_est_enregistre_qu_une_fois(cache, envois, methode):
    """paintEvent réenregistre self.update à chaque image tant que l'avatar
    n'est pas résolu : sans dédoublonnage, _pending enfle par milliers."""
    def rappel() -> None:
        pass

    for _ in range(50):
        getattr(cache, methode)("zerator", "ZeratoR", 32, rappel)
    cle = next(iter(cache._pending))
    assert cache._pending[cle] == [rappel]


@pytest.mark.parametrize("methode", ["get", "get_sq"])
def test_plusieurs_callbacks_distincts_sont_tous_gardes(cache, methode):
    """Cas rebuild du RemoteMenu : plusieurs vues attendent le même avatar."""
    rappels_ = [lambda: None, lambda: None]
    for r in rappels_:
        getattr(cache, methode)("zerator", "ZeratoR", 32, r)
    cle = next(iter(cache._pending))
    assert cache._pending[cle] == rappels_


class _PendingQuiCourse(dict):
    """Simule la fin du chargement PENDANT l'enregistrement du callback.

    C'est la course que `get()` traite juste après avoir empilé le callback :
    sans ce rattrapage, le rappel atterrit dans une liste déjà vidée et n'est
    jamais rejoué.
    """

    def __init__(self, cache, cle, px) -> None:
        super().__init__()
        self._cache = cache
        self._cle = cle
        self._px = px

    def setdefault(self, cle, defaut):
        self._cache._cache[self._cle] = self._px
        return super().setdefault(cle, defaut)


@pytest.mark.parametrize("methode,suffixe", [("get", ""), ("get_sq", "sq")])
def test_un_chargement_termine_pendant_l_enregistrement_est_rattrape(
        cache, rappels, methode, suffixe):
    cle = f"zerator@32{suffixe}"
    attendu = QPixmap(32, 32)
    cache._pending = _PendingQuiCourse(cache, cle, attendu)

    def rappel() -> None:
        pass

    px = getattr(cache, methode)("zerator", "ZeratoR", 32, rappel)
    assert px is attendu
    assert rappels == [rappel], "le callback est rejoué malgré la course"


# ── chargement effectif ──────────────────────────────────────────────────────

@pytest.mark.parametrize("charge,suffixe", [("_load", ""), ("_load_sq", "sq")])
def test_un_chargement_reussi_remplit_le_cache_et_reveille_les_attentes(
        cache, cache_disque, rappels, charge, suffixe):
    png(cache_disque / "zerator.png")
    cle = f"zerator@32{suffixe}"

    def rappel() -> None:
        pass

    cache._pending[cle] = [rappel]
    cache._placeholders[cle] = QPixmap(32, 32)
    cache._gray[f"zerator@32{suffixe}:gray"] = QPixmap(32, 32)
    cache._gray[f"zerator@32{suffixe}:gray:ph"] = QPixmap(32, 32)

    getattr(cache, charge)("zerator", "ZeratoR", 32, cle, "")

    assert cle in cache._cache
    assert cle not in cache._placeholders, "les initiales n'ont plus lieu d'être"
    assert cache._gray == {}, "le gris dérivait de l'ancien pixmap"
    assert rappels == [rappel]


@pytest.mark.parametrize("charge", ["_load", "_load_sq"])
def test_le_telechargement_n_a_lieu_que_si_le_disque_est_vide(
        cache, cache_disque, monkeypatch, charge):
    """Le disque d'abord : c'est ce qui évitait 70 % du trafic HTTP."""
    demandes: list[tuple[str, str]] = []
    monkeypatch.setattr(bs, "_download_avatar",
                        lambda login, url: demandes.append((login, url)))
    png(cache_disque / "zerator.png")
    getattr(cache, charge)("zerator", "ZeratoR", 32, "cle",
                           "https://x/a.png")
    assert demandes == []


@pytest.mark.parametrize("charge", ["_load", "_load_sq"])
def test_un_avatar_absent_est_telecharge_puis_relu(cache, cache_disque,
                                                   monkeypatch, charge):
    monkeypatch.setattr(
        bs, "_download_avatar",
        lambda login, url: png(cache_disque / f"{login}.png"),
    )
    getattr(cache, charge)("zerator", "ZeratoR", 32, "cle", "https://x/a.png")
    assert "cle" in cache._cache


def test_un_repechage_concurrent_ne_reecrase_pas_la_date(cache, cache_disque,
                                                         horloge, monkeypatch):
    """Une tentative AVEC url ne doit pas dater le repêchage d'une tentative
    SANS url : l'entrée ne serait plus celle qu'on croit relire."""
    monkeypatch.setattr(bs, "_download_avatar", lambda *a: None)
    cache._loading["cle"] = ("https://x/a.png", float("inf"))
    cache._load("zerator", "ZeratoR", 32, "cle", "")
    assert cache._loading["cle"] == ("https://x/a.png", float("inf"))


# ── déclinaison en gris ──────────────────────────────────────────────────────

@pytest.mark.parametrize("methode,suffixe", [
    ("get_gray", ""), ("get_gray_sq", "sq"),
])
def test_le_gris_du_placeholder_est_memorise_a_part(cache, methode, suffixe):
    """Sous une clé distincte, purgée quand le vrai avatar arrive."""
    a = getattr(cache, methode)("zerator", "ZeratoR", 32)
    b = getattr(cache, methode)("zerator", "ZeratoR", 32)
    assert a is b
    assert f"zerator@32{suffixe}:gray:ph" in cache._gray
    assert f"zerator@32{suffixe}:gray" not in cache._gray


@pytest.mark.parametrize("methode,suffixe", [
    ("get_gray", ""), ("get_gray_sq", "sq"),
])
def test_le_gris_d_un_vrai_avatar_est_mis_en_cache(cache, methode, suffixe):
    source = QPixmap(32, 32)
    source.fill(QColor("#804020"))
    cache._cache[f"zerator@32{suffixe}"] = source

    a = getattr(cache, methode)("zerator", "ZeratoR", 32)
    b = getattr(cache, methode)("zerator", "ZeratoR", 32)
    assert a is b
    assert cache._gray[f"zerator@32{suffixe}:gray"] is a


@pytest.mark.parametrize("methode,suffixe", [
    ("get_gray", ""), ("get_gray_sq", "sq"),
])
def test_le_gris_du_placeholder_n_est_jamais_pris_pour_celui_de_l_avatar(
        cache, methode, suffixe):
    """On compare l'IDENTITÉ, pas la présence de la clé : tester la présence
    figerait les initiales pour toujours si le chargement finit entre-temps."""
    getattr(cache, methode)("zerator", "ZeratoR", 32)
    # Le chargement se termine : le vrai pixmap arrive dans le cache couleur.
    vrai = QPixmap(32, 32)
    vrai.fill(QColor("#804020"))
    cache._cache[f"zerator@32{suffixe}"] = vrai
    cache._placeholders.pop(f"zerator@32{suffixe}", None)

    gris = getattr(cache, methode)("zerator", "ZeratoR", 32)
    assert cache._gray[f"zerator@32{suffixe}:gray"] is gris


# ── pool de threads ──────────────────────────────────────────────────────────

def test_aucun_chargement_n_est_programme_apres_la_fermeture(monkeypatch):
    """Un rafraîchissement Qt en file peut encore demander un avatar pendant
    qu'on quitte : submit() lèverait et la trace remonterait à l'utilisateur."""
    monkeypatch.setattr(bs, "_POOL_CLOSED", True)
    monkeypatch.setattr(bs, "_AVATAR_POOL",
                        types.SimpleNamespace(submit=lambda *a: pytest.fail(
                            "le pool fermé ne doit pas être sollicité")))
    assert bs._submit_avatar(lambda: None) is False


def test_une_fermeture_concurrente_est_absorbee(monkeypatch):
    """Course : la fermeture a eu lieu entre le test et l'envoi."""
    def tombe(*a):
        raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(bs, "_POOL_CLOSED", False)
    monkeypatch.setattr(bs, "_AVATAR_POOL",
                        types.SimpleNamespace(submit=tombe))
    assert bs._submit_avatar(lambda: None) is False


def test_un_chargement_est_bien_transmis_au_pool(monkeypatch):
    recu: list[tuple] = []
    monkeypatch.setattr(bs, "_POOL_CLOSED", False)
    monkeypatch.setattr(bs, "_AVATAR_POOL", types.SimpleNamespace(
        submit=lambda fn, *a: recu.append((fn, a))))
    marqueur = object()
    assert bs._submit_avatar(lambda: None, marqueur) is True
    assert recu[0][1] == (marqueur,)


# ── dispatcher GUI ───────────────────────────────────────────────────────────

def test_le_dispatcher_rejoue_sur_le_thread_gui(qtbot):
    """QTimer.singleShot depuis un thread sans boucle d'événements ne part
    jamais : c'est le bug que ce dispatcher corrige."""
    d = bs._GuiDispatcher()
    faits: list[int] = []
    d.post(lambda: faits.append(1))
    qtbot.waitUntil(lambda: faits == [1], timeout=1000)


def test_un_callback_fautif_n_abat_pas_l_application(qtbot, caplog):
    """PyQt appelle qFatal() sur une exception non rattrapée dans un slot."""
    def tombe() -> None:
        raise ValueError("widget mal en point")

    with caplog.at_level(logging.ERROR, logger=bs.logger.name):
        bs._GuiDispatcher._run(tombe)
    assert "Callback avatar en échec" in caplog.text


def test_un_widget_detruit_entre_temps_est_ignore_silencieusement(caplog):
    def tombe() -> None:
        raise RuntimeError("wrapped C/C++ object has been deleted")

    with caplog.at_level(logging.ERROR, logger=bs.logger.name):
        bs._GuiDispatcher._run(tombe)
    assert caplog.text == ""


def test_sans_dispatcher_l_abandon_est_signale(monkeypatch, caplog):
    """Surtout pas de QTimer.singleShot en repli : mieux vaut le dire."""
    monkeypatch.setattr(bs, "_gui_dispatcher", None)
    with caplog.at_level(logging.ERROR, logger=bs.logger.name):
        bs._post_to_gui(lambda: None)
    assert "Dispatcher GUI absent" in caplog.text


def test_le_dispatcher_n_est_construit_qu_une_fois(qapp, monkeypatch):
    monkeypatch.setattr(bs, "_gui_dispatcher", None)
    bs._ensure_dispatcher()
    premier = bs._gui_dispatcher
    bs._ensure_dispatcher()
    assert bs._gui_dispatcher is premier is not None


# ── application dans un QLabel ───────────────────────────────────────────────

def test_un_avatar_deja_en_memoire_est_applique_tout_de_suite(cache, qtbot):
    px = QPixmap(32, 32)
    px.fill(QColor("#804020"))
    cache._cache["zerator@32"] = px
    label = QLabel()
    qtbot.addWidget(label)
    bs.load_avatar_into_label(label, "zerator", "ZeratoR", 32, "")
    assert not label.pixmap().isNull()


def test_un_avatar_absent_est_applique_a_l_arrivee(cache, rappels, qtbot):
    label = QLabel()
    qtbot.addWidget(label)
    bs.load_avatar_into_label(label, "zerator", "ZeratoR", 32, "https://x/a.png")
    assert label.pixmap().isNull(), "rien à afficher tant que rien n'est chargé"

    px = QPixmap(32, 32)
    px.fill(QColor("#804020"))
    cache._cache["zerator@32"] = px
    for cb in cache._pending.pop("zerator@32"):
        cb()
    assert not label.pixmap().isNull()


def test_un_label_detruit_avant_l_arrivee_ne_fait_pas_lever(cache, qtbot):
    """Le callback survit au widget : le Big Screen se reconstruit souvent."""
    label = QLabel()
    bs.load_avatar_into_label(label, "zerator", "ZeratoR", 32, "")
    rappel = cache._pending["zerator@32"][0]
    label.deleteLater()
    del label
    cache._cache["zerator@32"] = QPixmap(32, 32)
    rappel()          # ne doit pas lever


# ─────────────────────────────────────────────────────────────────────────────
# Instrumentation de cadence
# ─────────────────────────────────────────────────────────────────────────────

def test_le_premier_reveil_ne_compte_pas_de_retard(horloge):
    """Il n'y a pas encore d'échéance à comparer."""
    cad = bs._Cadence("test", 50.0)
    cad.reveil()
    assert cad._retards == []
    assert cad._attendu == pytest.approx(horloge["perf"] + 0.05)


def test_le_retard_du_reveil_est_mesure_par_rapport_au_budget(horloge):
    """Distinguer « le widget dépasse son budget » de « la boucle est engorgée »."""
    cad = bs._Cadence("test", 50.0)
    cad.reveil()
    horloge["perf"] += 0.080          # 30 ms après l'échéance de 50 ms
    cad.reveil()
    assert cad._retards[0] == pytest.approx(30.0)


def test_le_resume_arrive_au_bout_de_cinq_secondes(horloge, caplog):
    cad = bs._Cadence("mosaique", 50.0)
    with caplog.at_level(logging.WARNING, logger=bs.logger.name):
        cad.peinture(12.0)
        assert caplog.text == "", "rien avant l'échéance"
        horloge["perf"] += 5.0
        cad.peinture(80.0)
    assert "PERF" in caplog.text
    assert "mosaique" in caplog.text
    assert cad._peintures == [], "les relevés repartent à zéro"
    assert cad._t_resume == horloge["perf"]


def test_le_resume_compte_les_images_hors_budget(horloge, caplog):
    cad = bs._Cadence("mosaique", 50.0)
    for ms in (10.0, 60.0, 70.0):
        cad._peintures.append(ms)
    with caplog.at_level(logging.WARNING, logger=bs.logger.name):
        cad._resumer()
    assert "2 image(s) au-dessus du budget" in caplog.text


def test_un_resume_sans_releve_ne_leve_pas(horloge, caplog):
    """Un widget masqué n'a rien peint : les listes sont vides."""
    cad = bs._Cadence("ticker", 16.0)
    with caplog.at_level(logging.WARNING, logger=bs.logger.name):
        cad._resumer()
    assert "n/a" in caplog.text


def test_l_instrumentation_est_inerte_par_defaut():
    """Un test de booléen par image, et rien d'autre, sans ZLINK_PERF=1."""
    assert bs._PERF is False


# ─────────────────────────────────────────────────────────────────────────────
# Ticker
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def ticker(qtbot, horloge):
    w = bs._TickerWidget()
    qtbot.addWidget(w)
    return w


def test_le_ticker_ne_montre_que_les_streamers_en_live(ticker):
    ticker.set_streamers([streamer("b"), streamer("a", online=False),
                          streamer("c")])
    assert [s.twitch_login for s in ticker._streamers] == ["b", "c"]


def test_le_ticker_trie_sans_tenir_compte_de_la_casse(ticker):
    ticker.set_streamers([streamer("Zerator"), streamer("antoine"),
                          streamer("Mister")])
    assert [s.twitch_login for s in ticker._streamers] == [
        "antoine", "Mister", "Zerator"]


def test_le_ticker_avance_a_cinquante_pixels_par_seconde(ticker, horloge):
    ticker.set_streamers([streamer(f"s{i}") for i in range(10)])
    ticker._tick()                    # amorce l'horloge interne
    horloge["monotonic"] += 1.0
    ticker._tick()
    assert ticker._offset == pytest.approx(50.0)


def test_le_defilement_du_ticker_boucle(ticker, horloge):
    """Deux cartes, donc 480 px de cycle : au-delà, on repart à zéro."""
    ticker.set_streamers([streamer("a"), streamer("b")])
    ticker._tick()
    horloge["monotonic"] += 10.0      # 500 px parcourus
    ticker._tick()
    assert ticker._offset == pytest.approx(20.0)


def test_une_liste_vide_ne_fige_pas_l_horloge_du_ticker(ticker, horloge):
    """Sinon la première image après l'arrivée des données ferait un saut."""
    ticker._tick()
    horloge["monotonic"] += 10.0
    ticker._tick()                    # aucun streamer : rien ne bouge
    ticker.set_streamers([streamer("a"), streamer("b")])
    horloge["monotonic"] += 0.1
    ticker._tick()
    assert ticker._offset == pytest.approx(5.0), "0,1 s de défilement, pas 10 s"


def test_le_ticker_ne_tourne_que_lorsqu_il_est_visible(ticker, qtbot):
    """62 réveils par seconde pour un widget invisible."""
    assert not ticker._timer.isActive()
    ticker.show()
    assert ticker._timer.isActive()
    ticker.hide()
    assert not ticker._timer.isActive()


def test_le_ticker_se_peint_sans_streamer(ticker, qtbot):
    ticker.resize(800, 56)
    ticker.grab()                     # force un paintEvent hors écran


def test_le_ticker_peint_ses_cartes(ticker, cache, envois, qtbot):
    ticker.set_streamers([streamer(f"s{i}") for i in range(4)])
    ticker.resize(800, 56)
    ticker.grab()
    assert envois, "chaque carte réclame son avatar"


# ─────────────────────────────────────────────────────────────────────────────
# Mosaïque de fond
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mosaique(qtbot, horloge):
    w = bs._BgAvatarsWidget()
    qtbot.addWidget(w)
    w.resize(1000, 500)               # 10 colonnes → cellules de 100 px
    return w


def test_la_mosaique_trie_sans_tenir_compte_de_la_casse(mosaique):
    """Pas de regroupement par état live : l'ordre doit rester stable."""
    mosaique.set_streamers([streamer("Zerator"), streamer("antoine",
                                                          online=False)])
    assert [s.twitch_login for s in mosaique._streamers] == ["antoine",
                                                             "Zerator"]


def test_la_vitesse_de_la_mosaique_ne_depend_pas_de_la_cadence(mosaique,
                                                               horloge):
    """En px PAR SECONDE : avec un pas par image, la mosaïque ralentissait à
    37 px/s au lieu de 45 dès que la machine était chargée."""
    mosaique.set_streamers([streamer(f"s{i}") for i in range(100)])

    nominal = bs._BgAvatarsWidget._FPS_INTERVAL / 1000.0
    vitesse = bs._BgAvatarsWidget._SCROLL_PX_S

    # Meme duree ecoulee des deux cotes : seule la CADENCE differe.
    ecoule = 5 * nominal
    assert ecoule <= 0.25, "au-dela, le plafond anti-saut fausserait la comparaison"

    # Cadence nominale : 5 images d'un intervalle chacune.
    mosaique._tick()                  # amorce
    for _ in range(5):
        horloge["monotonic"] += nominal
        mosaique._tick()
    rapide = mosaique._offset

    # Machine chargee : une seule image pour la meme duree.
    mosaique._offset = 0.0
    mosaique._last_tick = 0.0
    mosaique._tick()                  # amorce
    horloge["monotonic"] += ecoule
    mosaique._tick()

    assert mosaique._offset == pytest.approx(rapide), \
        "cinq petites images ou une grosse : meme distance parcourue"
    assert rapide == pytest.approx(vitesse * (ecoule + nominal)), \
        "l'amorce compte une image nominale"


def test_le_premier_tick_prend_une_image_nominale(mosaique, horloge):
    """Reprise après masquage : un delta de plusieurs secondes ferait sauter."""
    mosaique.set_streamers([streamer(f"s{i}") for i in range(100)])
    horloge["monotonic"] += 3600.0
    mosaique._tick()
    assert mosaique._offset == pytest.approx(
        bs._BgAvatarsWidget._SCROLL_PX_S * bs._BgAvatarsWidget._FPS_INTERVAL / 1000.0)


def test_un_ecart_trop_grand_est_plafonne(mosaique, horloge):
    """Le widget était masqué : sans plafond, la mosaïque bondirait."""
    mosaique.set_streamers([streamer(f"s{i}") for i in range(100)])
    nominal = bs._BgAvatarsWidget._FPS_INTERVAL / 1000.0
    vitesse = bs._BgAvatarsWidget._SCROLL_PX_S
    mosaique._tick()
    horloge["monotonic"] += 30.0
    mosaique._tick()
    assert mosaique._offset == pytest.approx(vitesse * nominal + vitesse * 0.25)


def test_le_defilement_de_la_mosaique_boucle(mosaique, horloge):
    """10 streamers, 1 rangée de 102 px de haut : le cycle fait 102 px."""
    mosaique.set_streamers([streamer(f"s{i}") for i in range(10)])
    mosaique._last_tick = horloge["monotonic"]
    for _ in range(10):               # 10 images de 0,25 s → 112,5 px
        horloge["monotonic"] += 0.25
        mosaique._tick()
    assert mosaique._offset == pytest.approx(112.5 - 102.0)


@pytest.mark.parametrize("largeur,streamers", [
    (0, 10),      # pas encore dimensionné
    (1000, 0),    # données pas encore arrivées
])
def test_la_mosaique_ne_defile_pas_sans_matiere(mosaique, horloge, largeur,
                                                streamers):
    mosaique.set_streamers([streamer(f"s{i}") for i in range(streamers)])
    mosaique.resize(largeur, 500)
    mosaique._tick()
    assert mosaique._offset == 0.0


# ── préchauffage ─────────────────────────────────────────────────────────────

class FauxCache:
    """Enregistre les demandes de la mosaïque, sans rien charger."""

    def __init__(self) -> None:
        self.couleur: list[tuple] = []
        self.gris: list[tuple] = []

    def get_sq(self, login, display, size, cb=None, url=""):
        self.couleur.append((login, size, url))
        return QPixmap(max(size, 1), max(size, 1))

    def get_gray_sq(self, login, display, size, cb=None, url=""):
        self.gris.append((login, size, url))
        return QPixmap(max(size, 1), max(size, 1))


@pytest.fixture
def prechauffage(mosaique, monkeypatch):
    """Mosaïque « visible » avec un cache et un minuteur factices.

    On ne l'affiche PAS : show() sur un widget de fond n'apporte rien et
    laisserait un vrai QTimer relancer _prewarm sur un widget détruit.
    """
    faux = FauxCache()
    reprises: list[int] = []
    monkeypatch.setattr(bs, "_avatar_cache", faux)
    monkeypatch.setattr(bs, "QTimer", types.SimpleNamespace(
        singleShot=lambda ms, fn: reprises.append(ms)))
    mosaique.isVisible = lambda: True
    return mosaique, faux, reprises


def test_le_prechauffage_procede_par_salves(prechauffage):
    """Sans salves, un thread par streamer d'un seul coup."""
    mosaique, faux, reprises = prechauffage
    mosaique.set_streamers([streamer(f"s{i:03d}") for i in range(100)])
    assert len(faux.couleur) == bs._BgAvatarsWidget._PREWARM_BATCH
    assert mosaique._prewarm_idx == bs._BgAvatarsWidget._PREWARM_BATCH
    assert reprises == [200], "la salve suivante est programmée"


def test_le_prechauffage_distingue_en_ligne_et_hors_ligne(prechauffage):
    mosaique, faux, _ = prechauffage
    mosaique.set_streamers([streamer("enligne"),
                            streamer("horsligne", online=False)])
    assert [c[0] for c in faux.couleur] == ["enligne"]
    assert [g[0] for g in faux.gris] == ["horsligne"]


def test_le_prechauffage_vise_la_taille_de_cellule_affichee(prechauffage):
    """Le cache indexe par taille : préchauffer à la mauvaise taille ne sert
    à rien et la mosaïque repart en rangées de lettres."""
    mosaique, faux, _ = prechauffage
    mosaique.set_streamers([streamer("zerator")])
    assert faux.couleur[0][1] == 100          # 1000 px / 10 colonnes


def test_le_prechauffage_transmet_l_url_de_profil(prechauffage):
    mosaique, faux, _ = prechauffage
    mosaique.set_streamers([streamer("zerator", url="https://x/a.png")])
    assert faux.couleur[0][2] == "https://x/a.png"


def test_une_chaine_de_prechauffage_perimee_s_arrete(prechauffage):
    """Sans jeton de génération, chaque set_streamers empilait une chaîne de
    plus sans arrêter les précédentes."""
    mosaique, faux, _ = prechauffage
    mosaique.set_streamers([streamer(f"s{i}") for i in range(5)])
    faux.couleur.clear()
    mosaique._prewarm(mosaique._prewarm_gen - 1)
    assert faux.couleur == []


def test_chaque_relance_invalide_la_precedente(prechauffage):
    mosaique, _, _ = prechauffage
    avant = mosaique._prewarm_gen
    mosaique.set_streamers([streamer("a")])
    assert mosaique._prewarm_gen == avant + 1


def test_un_widget_cache_ne_prechauffe_pas(mosaique, monkeypatch):
    """Sa taille est celle par défaut : on remplirait le cache de pixmaps à
    une taille jamais affichée."""
    faux = FauxCache()
    monkeypatch.setattr(bs, "_avatar_cache", faux)
    mosaique.set_streamers([streamer("zerator")])
    assert faux.couleur == [] and faux.gris == []


def test_un_widget_pas_encore_dimensionne_retente(prechauffage):
    mosaique, faux, reprises = prechauffage
    mosaique._streamers = [streamer("zerator")]
    mosaique.resize(0, 500)
    reprises.clear()
    mosaique._prewarm(mosaique._prewarm_gen)
    assert faux.couleur == []
    assert reprises == [200]


def test_sans_streamer_il_n_y_a_rien_a_prechauffer(prechauffage):
    mosaique, faux, reprises = prechauffage
    mosaique._streamers = []
    reprises.clear()
    mosaique._prewarm(mosaique._prewarm_gen)
    assert faux.couleur == [] and faux.gris == []
    assert reprises == [], "inutile de reprogrammer une salve vide"


def test_une_taille_inconnue_est_retentee_meme_sans_streamer(prechauffage):
    """La taille est calculée AVANT le test de la liste : sinon un widget pas
    encore dimensionné et sans données n'aurait plus jamais de seconde chance."""
    mosaique, faux, reprises = prechauffage
    mosaique._streamers = []
    mosaique.resize(0, 500)
    reprises.clear()
    mosaique._prewarm(mosaique._prewarm_gen)
    assert reprises == [200]


def test_la_liste_change_mais_pas_la_taille_de_cellule(prechauffage):
    """Réinitialiser _prewarm_cell forcerait un balayage complet à chaque
    rafraîchissement périodique."""
    mosaique, faux, _ = prechauffage
    mosaique.set_streamers([streamer("a")])
    assert mosaique._prewarm_cell == 100
    mosaique.set_streamers([streamer("a"), streamer("b")])
    assert mosaique._prewarm_cell == 100
    assert mosaique._prewarm_idx == 2, "on repart bien du début de la liste"


def test_un_changement_de_taille_relance_le_prechauffage(prechauffage):
    """Les données arrivent avant que la fenêtre ait sa taille finale."""
    mosaique, faux, _ = prechauffage
    mosaique.set_streamers([streamer(f"s{i}") for i in range(5)])
    faux.couleur.clear()
    redimensionner(mosaique, 500, 500)   # cellules de 50 px désormais
    assert mosaique._prewarm_cell == 50
    assert {c[1] for c in faux.couleur} == {50}


def test_un_redimensionnement_sans_effet_sur_les_cellules_ne_relance_rien(
        prechauffage):
    mosaique, faux, _ = prechauffage
    mosaique.set_streamers([streamer(f"s{i}") for i in range(5)])
    faux.couleur.clear()
    redimensionner(mosaique, 1005, 400)  # 1005 // 10 == 100, taille inchangée
    assert faux.couleur == []


def test_la_mosaique_ne_tourne_que_lorsqu_elle_est_visible(mosaique):
    """Le Big Screen est construit puis masqué : le timer tournait à vide."""
    assert not mosaique._timer.isActive()
    mosaique.show()
    assert mosaique._timer.isActive()
    mosaique.hide()
    assert not mosaique._timer.isActive()


# ── peinture de la mosaïque ──────────────────────────────────────────────────

def test_une_mosaique_vide_ne_peint_rien(mosaique):
    mosaique.grab()                   # ne doit pas lever


def test_la_mosaique_peint_ses_deux_passes(mosaique, monkeypatch):
    """Une passe en ligne, une hors ligne : l'opacité ne change que deux fois
    par image au lieu d'une fois par cellule."""
    faux = FauxCache()
    monkeypatch.setattr(bs, "_avatar_cache", faux)
    mosaique._streamers = [streamer(f"s{i}", online=(i % 2 == 0))
                           for i in range(30)]
    mosaique.grab()
    assert faux.couleur and faux.gris


def test_une_largeur_inferieure_au_nombre_de_colonnes_ne_peint_pas(
        mosaique, monkeypatch):
    """0 < w < 10 : les transformations rendraient un pixmap nul, mis en cache
    comme un succès et jamais réparé."""
    faux = FauxCache()
    monkeypatch.setattr(bs, "_avatar_cache", faux)
    mosaique._streamers = [streamer("zerator")]
    mosaique.resize(5, 100)
    mosaique.grab()
    assert faux.couleur == []


# ─────────────────────────────────────────────────────────────────────────────
# Barre de progression et cartes
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("demande,attendu", [
    (-10.0, 0.0), (0.0, 0.0), (42.5, 42.5), (100.0, 100.0), (150.0, 100.0),
])
def test_le_pourcentage_est_borne(qtbot, demande, attendu):
    barre = bs._ProgressBar()
    qtbot.addWidget(barre)
    barre.set_pct(demande)
    assert barre._pct == attendu


def test_la_barre_se_peint_a_toutes_les_valeurs(qtbot):
    barre = bs._ProgressBar()
    qtbot.addWidget(barre)
    barre.resize(200, 10)
    for pct in (0.0, 50.0, 100.0):
        barre.set_pct(pct)
        barre.grab()


@pytest.mark.parametrize("brut,attendu", [
    ("1 154 212", "1 154 212 €"),     # l'unité manquait
    ("1 154 212 €", "1 154 212 €"),   # déjà présente : pas de doublon
    ("", "—"),
])
def test_la_cagnotte_porte_toujours_son_unite(qtbot, cache, brut, attendu):
    carte = bs._CagnotteCard()
    qtbot.addWidget(carte)
    carte.update_stats(GlobalStats(donation_total=0.0, donation_formatted=brut,
                                   viewers_total=0, website_mode="live"))
    assert carte._amount_odo._current_text == attendu


def test_sans_objectif_global_la_barre_disparait(qtbot, cache):
    carte = bs._CagnotteCard()
    qtbot.addWidget(carte)
    carte.update_stats(GlobalStats(0.0, "12 €", 0, "live"))
    assert carte._progress_bar.isHidden()
    assert carte._pct_lbl.isHidden()


def test_le_nombre_de_viewers_est_lisible(qtbot, cache):
    """Espace fine insécable : 1234567 est illisible tel quel."""
    carte = bs._CagnotteCard()
    qtbot.addWidget(carte)
    carte.update_viewers(1234567)
    assert carte._viewers_lbl.text() == "● 1 234 567 viewers en live"


@pytest.mark.parametrize("nombre,attendu", [
    (0, "● 0 streamers en live"),
    (1, "● 1 streamer en live"),      # singulier
    (2, "● 2 streamers en live"),
])
def test_le_compteur_de_lives_s_accorde(qtbot, cache, nombre, attendu):
    carte = bs._CagnotteCard()
    qtbot.addWidget(carte)
    carte.update_live_count(nombre)
    assert carte._viewers_lbl.text() == attendu


# ── objectifs ────────────────────────────────────────────────────────────────

def goal(login: str, pct: float) -> GoalWithStreamer:
    return GoalWithStreamer(streamer_login=login, streamer_display=login,
                            goal_name=f"objectif de {login}",
                            amount_target=100.0, accomplished=pct >= 100.0,
                            pct=pct)


@pytest.mark.parametrize("pourcentages,retenus", [
    ([50.0, 89.9], 0),                       # trop loin du but
    ([90.0, 95.0, 100.0], 3),                # bornes incluses
    ([100.1, 120.0], 0),                     # déjà dépassés
    ([91.0, 92.0, 93.0, 94.0, 95.0], 4),     # plafonné à _MAX_GOALS
])
def test_seuls_les_objectifs_proches_sont_montres(qtbot, cache, envois,
                                                  pourcentages, retenus):
    carte = bs._GoalsCard()
    qtbot.addWidget(carte)
    carte.update_goals([goal(f"s{i}", p) for i, p in enumerate(pourcentages)])
    assert carte._content_vl.count() == retenus
    assert carte.isHidden() is (retenus == 0)


def test_la_carte_objectifs_est_reconstruite_a_chaque_fois(qtbot, cache,
                                                           envois):
    """Sans nettoyage, les anciennes lignes s'empileraient sous les nouvelles."""
    carte = bs._GoalsCard()
    qtbot.addWidget(carte)
    carte.update_goals([goal("a", 95.0), goal("b", 96.0)])
    carte.update_goals([goal("c", 97.0)])
    assert carte._content_vl.count() == 1


def test_une_ligne_d_objectif_affiche_son_avancement(qtbot, cache, envois):
    ligne = bs._GoalRow(goal("zerator", 93.0))
    qtbot.addWidget(ligne)
    textes = [w.text() for w in ligne.findChildren(QLabel)]
    assert "zerator" in textes
    assert "objectif de zerator" in textes
    assert "93%" in textes


def test_le_nom_d_objectif_est_du_texte_brut(qtbot, cache, envois):
    """Il vient de l'API : interprété en HTML, il injecterait du balisage."""
    ligne = bs._GoalRow(goal("zerator", 93.0))
    qtbot.addWidget(ligne)
    for w in ligne.findChildren(QLabel):
        if w.text():
            assert w.textFormat() != Qt.TextFormat.RichText


# ─────────────────────────────────────────────────────────────────────────────
# Compteur à chiffres
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def compteur(qtbot):
    from PyQt6.QtGui import QFont
    w = bs._OdometerWidget(QFont("Consolas", 12))
    qtbot.addWidget(w)
    return w


def test_le_compteur_cree_un_widget_par_caractere(compteur):
    compteur.set_text("1 234 €")
    assert len(compteur._digits) == len("1 234 €")


def test_le_compteur_est_aligne_a_droite(compteur):
    """Les unités doivent rester sous les unités quand le montant grandit."""
    compteur.set_text("123")
    compteur.set_text("45")
    assert [d._current_char for d in compteur._digits[:3]] == [" ", "4", "5"]


def test_le_compteur_recycle_ses_chiffres(compteur):
    """Un montant qui grandit ne doit pas fuir en widgets."""
    compteur.set_text("1 234 567 €")
    avant = len(compteur._digits)
    compteur.set_text("42 €")
    assert len(compteur._digits) == avant


def test_le_meme_montant_ne_relance_pas_l_animation(compteur):
    compteur.set_text("1 234 €")
    compteur.set_text("1 234 €")
    assert all(d._anim is None for d in compteur._digits)


@pytest.mark.parametrize("depart,arrivee,anime", [
    (" ", "5", False),      # apparition : pas d'animation
    ("5", " ", False),      # disparition non plus
    ("4", "5", True),
])
def test_un_chiffre_n_anime_que_les_vraies_bascules(qtbot, depart, arrivee,
                                                    anime):
    from PyQt6.QtGui import QFont
    d = bs._Digit(QFont("Consolas", 12))
    qtbot.addWidget(d)
    d.set_char(depart, animate=False)
    d.set_char(arrivee)
    assert (d._anim is not None) is anime
    if d._anim is not None:
        d._anim.stop()


def test_un_chiffre_identique_est_ignore(qtbot):
    from PyQt6.QtGui import QFont
    d = bs._Digit(QFont("Consolas", 12))
    qtbot.addWidget(d)
    d.set_char("7", animate=False)
    d.set_char("7")
    assert d._anim is None and d._from_char == " "


def test_le_decalage_anime_est_lisible_et_ecrivable(qtbot):
    """C'est la propriété que pilote QPropertyAnimation."""
    from PyQt6.QtGui import QFont
    d = bs._Digit(QFont("Consolas", 12))
    qtbot.addWidget(d)
    d.anim_offset = -0.5
    assert d.anim_offset == pytest.approx(-0.5)
    assert d._offset == pytest.approx(-0.5)


@pytest.mark.parametrize("decalage", [0.0, -0.5, -1.0])
def test_un_chiffre_se_peint_a_tous_les_stades(qtbot, decalage):
    from PyQt6.QtGui import QFont
    d = bs._Digit(QFont("Consolas", 12))
    qtbot.addWidget(d)
    d.set_char("5", animate=False)
    d.anim_offset = decalage
    d.grab()


# ─────────────────────────────────────────────────────────────────────────────
# Assemblage
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def big_screen(qtbot, cache, envois):
    w = bs.BigScreenWidget()
    qtbot.addWidget(w)
    redimensionner(w, 1280, 720)
    return w


def test_le_big_screen_repartit_ses_sous_widgets(big_screen):
    """Le fond commence sous le ticker de 56 px."""
    assert big_screen._ticker.geometry().height() == 56
    assert big_screen._bg.geometry().y() == 56
    assert big_screen._bg.geometry().height() == 720 - 56


def test_les_cartes_restent_dans_la_fenetre(big_screen):
    for carte in (big_screen._cagnotte_card, big_screen._goals_card):
        assert carte.x() >= 0 and carte.y() >= 0
        assert carte.x() + carte.width() <= 1280


def test_la_mise_a_jour_des_streamers_irrigue_les_trois_composants(big_screen):
    big_screen.update_streamers([streamer("a"), streamer("b", online=False),
                                 streamer("c")])
    assert len(big_screen._ticker._streamers) == 2, "le ticker filtre les live"
    assert len(big_screen._bg._streamers) == 3, "la mosaïque montre tout le monde"
    assert big_screen._cagnotte_card._viewers_lbl.text() == \
        "● 2 streamers en live"


def test_la_cagnotte_traverse_le_big_screen(big_screen):
    big_screen.update_stats(GlobalStats(0.0, "1 154 212", 0, "live"))
    assert big_screen._cagnotte_card._amount_odo._current_text == "1 154 212 €"


def test_les_objectifs_sont_repositionnes_apres_mise_a_jour(big_screen):
    big_screen.update_goals([goal("a", 95.0)])
    carte = big_screen._goals_card
    assert carte.x() + carte.width() == 1280 - 40
    assert carte.y() + carte.height() == 720 - 40


def test_le_bouton_de_fermeture_demande_la_sortie(big_screen, qtbot):
    with qtbot.waitSignal(big_screen.close_requested, timeout=1000):
        big_screen._close_btn.click()


def test_le_bouton_de_fermeture_reste_en_haut_a_droite(big_screen):
    assert big_screen._close_btn.x() + big_screen._close_btn.width() == 1280 - 12
    assert big_screen._close_btn.y() == 12


# ─────────────────────────────────────────────────────────────────────────────
# Téléchargement et fermeture
# ─────────────────────────────────────────────────────────────────────────────

def test_le_telechargement_passe_par_le_cache_partage(monkeypatch):
    """Ce module avait sa propre copie du téléchargement, concurrente de celle
    de data_manager : les deux tiraient la même image en parallèle."""
    demandes: list[tuple[str, str]] = []
    monkeypatch.setattr(bs._avatar_disk, "download",
                        lambda cle, url: demandes.append((cle, url)))
    bs._download_avatar("zerator", "https://x/a.png")
    assert demandes == [("zerator", "https://x/a.png")]


def test_la_fermeture_du_pool_n_attend_pas_les_telechargements(monkeypatch):
    """Ses threads sont NON-DAEMON : un téléchargement lent retarderait la sortie."""
    arrets: list[dict] = []
    monkeypatch.setattr(bs, "_POOL_CLOSED", False)
    monkeypatch.setattr(bs, "_AVATAR_POOL", types.SimpleNamespace(
        shutdown=lambda **kw: arrets.append(kw)))
    bs.shutdown_avatar_pool()
    assert bs._POOL_CLOSED is True
    assert arrets == [{"wait": False, "cancel_futures": True}]


def test_le_dispatcher_est_bien_celui_qu_on_sollicite(monkeypatch):
    recus: list = []
    monkeypatch.setattr(bs, "_gui_dispatcher",
                        types.SimpleNamespace(post=recus.append))

    def rappel() -> None:
        pass

    bs._post_to_gui(rappel)
    assert recus == [rappel]


# ── labels détruits ──────────────────────────────────────────────────────────

class LabelMort:
    """Double d'un QLabel dont l'objet C++ a été détruit."""

    def setPixmap(self, px) -> None:      # noqa: N802 - API Qt
        raise RuntimeError("wrapped C/C++ object has been deleted")


def test_un_label_mort_n_empeche_pas_l_application_immediate(cache):
    px = QPixmap(32, 32)
    cache._cache["zerator@32"] = px
    bs.load_avatar_into_label(LabelMort(), "zerator", "ZeratoR", 32, "")


def test_un_label_mort_n_empeche_pas_l_application_differee(cache):
    bs.load_avatar_into_label(LabelMort(), "zerator", "ZeratoR", 32, "")
    rappel = cache._pending["zerator@32"][0]
    cache._cache["zerator@32"] = QPixmap(32, 32)
    rappel()                              # ne doit pas lever


def test_un_avatar_disparu_du_cache_ne_fait_rien(cache):
    """Le callback peut se réveiller après une purge : rien à appliquer."""
    label = QLabel()
    bs.load_avatar_into_label(label, "zerator", "ZeratoR", 32, "")
    cache._pending["zerator@32"][0]()      # cache toujours vide
    assert label.pixmap().isNull()


# ─────────────────────────────────────────────────────────────────────────────
# Instrumentation active (ZLINK_PERF=1)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def perf(monkeypatch):
    """Active l'instrumentation sur des compteurs neufs.

    Les compteurs du module vivent toute la session : les réutiliser mêlerait
    les relevés d'un test à ceux du suivant.
    """
    monkeypatch.setattr(bs, "_PERF", True)
    mosaique = bs._Cadence("mosaique", 50.0)
    ticker = bs._Cadence("ticker", 16.0)
    monkeypatch.setattr(bs, "_CAD_MOSAIQUE", mosaique)
    monkeypatch.setattr(bs, "_CAD_TICKER", ticker)
    return mosaique, ticker


def test_le_ticker_instrumente_ses_reveils_et_ses_peintures(ticker, perf,
                                                            horloge):
    _, cadence = perf
    ticker.set_streamers([streamer("a")])
    ticker.resize(400, 56)
    ticker._tick()
    ticker.grab()
    assert cadence._attendu, "le réveil est daté"
    assert len(cadence._peintures) == 1


def test_le_ticker_vide_est_quand_meme_mesure(ticker, perf):
    """Un fond opaque et un séparateur coûtent déjà le vidage du backing store."""
    cadence = perf[1]
    ticker.resize(400, 56)
    ticker.grab()
    assert len(cadence._peintures) == 1


def test_la_mosaique_instrumente_ses_reveils_et_ses_peintures(
        mosaique, perf, horloge, monkeypatch):
    cadence = perf[0]
    monkeypatch.setattr(bs, "_avatar_cache", FauxCache())
    mosaique.set_streamers([streamer(f"s{i}") for i in range(20)])
    mosaique._tick()
    mosaique.grab()
    assert cadence._attendu
    assert len(cadence._peintures) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Compléments
# ─────────────────────────────────────────────────────────────────────────────

def test_une_bascule_rapide_interrompt_l_animation_precedente(qtbot):
    """La cagnotte peut changer deux fois en moins de 250 ms."""
    from PyQt6.QtGui import QFont
    d = bs._Digit(QFont("Consolas", 12))
    qtbot.addWidget(d)
    d.set_char("1", animate=False)
    d.set_char("2")
    premiere = d._anim
    d.set_char("3")
    assert d._anim is not premiere
    assert premiere.state() != premiere.State.Running
    d._anim.stop()


def test_une_ligne_d_objectif_se_rafraichit_a_l_arrivee_de_l_avatar(cache,
                                                                    qtbot):
    ligne = bs._GoalRow(goal("zerator", 93.0))
    qtbot.addWidget(ligne)
    avatar = ligne.findChildren(QLabel)[0]
    assert avatar.pixmap().cacheKey() == \
        cache._placeholders["zerator@32"].cacheKey(), "initiales en attendant"

    vrai = QPixmap(32, 32)
    vrai.fill(QColor("#804020"))
    cache._cache["zerator@32"] = vrai
    cache._pending["zerator@32"][0]()
    assert avatar.pixmap().cacheKey() == vrai.cacheKey()


def test_le_big_screen_survit_a_des_objectifs_vides(big_screen):
    big_screen.update_goals([])
    assert big_screen._goals_card.isHidden()


def test_le_prechauffage_s_arrete_en_bout_de_liste(prechauffage):
    """Sans cette sortie, on reprogrammerait une salve toutes les 200 ms à vide."""
    mosaique, faux, reprises = prechauffage
    mosaique._streamers = [streamer("zerator")]
    mosaique._prewarm_cell = 100
    mosaique._prewarm_idx = 1          # tout est déjà demandé
    reprises.clear()
    mosaique._prewarm(mosaique._prewarm_gen)
    assert faux.couleur == [] and reprises == []


def test_une_ligne_d_objectif_detruite_ne_fait_pas_lever(cache, qtbot):
    """Le Big Screen reconstruit ses lignes à chaque rafraîchissement."""
    ligne = bs._GoalRow(goal("zerator", 93.0))
    rappel = cache._pending["zerator@32"][0]
    ligne.deleteLater()
    del ligne
    qtbot.wait(10)
    cache._cache["zerator@32"] = QPixmap(32, 32)
    rappel()                            # ne doit pas lever


# ── carte cagnotte : horloge et vitesse de collecte ──────────────────────────

@pytest.fixture
def carte(qtbot):
    c = bs._CagnotteCard()
    qtbot.addWidget(c)
    return c


def test_l_heure_s_affiche_au_format_vingt_quatre_heures(carte):
    """Le plein écran rétracte la barre des tâches : plus aucune horloge."""
    from PyQt6.QtCore import QTime

    carte.rafraichir_heure(QTime(21, 47))
    assert carte._heure_lbl.text() == "21:47"


def test_l_horloge_bat_a_la_seconde(carte):
    """À la minute, l'affichage aurait jusqu'à soixante secondes de retard
    au tour de minute, et l'horloge paraîtrait arrêtée."""
    assert carte._horloge.interval() == 1000
    assert carte._horloge.isActive()


def test_l_heure_n_est_reecrite_que_si_elle_change(carte):
    """Un setText par seconde sur un libellé inchangé repeint pour rien."""
    from PyQt6.QtCore import QTime

    carte.rafraichir_heure(QTime(9, 5))
    ecritures = []
    carte._heure_lbl.setText = ecritures.append
    carte.rafraichir_heure(QTime(9, 5))
    assert ecritures == []
    carte.rafraichir_heure(QTime(9, 6))
    assert ecritures == ["09:06"]


def test_la_vitesse_de_collecte_s_affiche_par_heure(carte):
    carte.update_rate(12_420.0)
    assert carte._rythme_lbl.text() == "+ 12\u00a0420 € / h"
    assert carte._rythme_lbl.isHidden() is False


@pytest.mark.parametrize("valeur", [None, 0.0, -50.0])
def test_sans_vitesse_mesurable_la_ligne_disparait(carte, valeur):
    """« 0 €/h » affirmerait que rien ne rentre, alors qu'avant l'événement
    il n'y a simplement pas encore de série à comparer."""
    carte.update_rate(12_420.0)
    carte.update_rate(valeur)
    assert carte._rythme_lbl.isHidden() is True


def test_le_grand_ecran_convertit_les_euros_par_minute_en_euros_par_heure(qtbot):
    """`donation_rate` rend des euros par MINUTE : les afficher tels quels
    diviserait la vitesse annoncée par soixante."""
    ecran = bs.BigScreenWidget()
    qtbot.addWidget(ecran)

    class _Hist:
        @staticmethod
        def donation_rate():
            return 100.0

    ecran.update_history(_Hist())
    assert ecran._cagnotte_card._rythme_lbl.text() == "+ 6\u00a0000 € / h"


def test_un_historique_absent_ne_fait_rien_tomber(qtbot):
    """Le grand écran s'ouvre avant le premier sondage."""
    ecran = bs.BigScreenWidget()
    qtbot.addWidget(ecran)
    ecran.update_history(None)
    assert ecran._cagnotte_card._rythme_lbl.isHidden() is True
