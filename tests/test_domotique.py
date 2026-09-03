# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Sortie vers Home Assistant : ce qui part, et ce qui ne doit rien casser.

Un webhook mal configuré, une box éteinte ou un réseau coupé arrivent en plein
event. Aucun de ces cas ne doit se voir autrement que dans le journal : ces
tests portent donc surtout sur les échecs.

Aucun test n'ouvre de connexion — `urlopen` est remplacé.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from core import domotique


@pytest.fixture
def poste(monkeypatch):
    """Capture les envois au lieu de les faire, et rend la liste."""
    envois: list[tuple[str, dict]] = []

    class _Reponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def faux_urlopen(requete, timeout=None):
        envois.append((requete.full_url,
                       json.loads(requete.data.decode("utf-8"))))
        return _Reponse()

    monkeypatch.setattr(domotique.urllib.request, "urlopen", faux_urlopen)
    return envois


CONF = {"domotique": {"url": "http://ha.local:8123/api/webhook/zlink",
                      "evenements": ["palier", "don", "objectif", "hype"]}}


# ── ce qui part ──────────────────────────────────────────────────────────────

def test_un_palier_part_avec_son_montant(poste):
    assert domotique.annonce("palier", {"montant": 1_000_000.0,
                                        "libelle": "1 M€"}, config=CONF)
    _fil_attendu()
    url, charge = poste[0]
    assert url == CONF["domotique"]["url"]
    assert charge == {"type": "palier", "montant": 1_000_000.0,
                      "libelle": "1 M€"}


def test_le_type_est_toujours_present(poste):
    """C'est sur lui que se branchent les automatisations."""
    domotique.annonce("hype", {"login": "mistermv"}, config=CONF)
    _fil_attendu()
    assert poste[0][1]["type"] == "hype"


def test_les_accents_ne_sont_pas_echappes(poste):
    """« 1 M€ » doit arriver lisible dans un template Home Assistant."""
    domotique.annonce("palier", {"libelle": "1 M€"}, config=CONF)
    _fil_attendu()
    assert poste[0][1]["libelle"] == "1 M€"


# ── ce qui ne part pas ───────────────────────────────────────────────────────

def test_sans_url_rien_n_est_envoye(poste):
    assert not domotique.annonce("palier", {"montant": 1.0}, config={})
    assert poste == []


@pytest.mark.parametrize("url", [
    "", "   ", "ha.local/webhook",        # sans schéma
    "file:///etc/passwd",                  # lecture de fichier
    "ftp://ha.local/x",
    "http://",                             # sans hôte
])
def test_une_url_inexploitable_est_refusee(url):
    assert not domotique.url_valable(url)


def test_une_famille_decochee_ne_part_pas(poste):
    conf = {"domotique": {"url": CONF["domotique"]["url"],
                          "evenements": ["palier"]}}
    assert not domotique.annonce("hype", {"login": "x"}, config=conf)
    assert poste == []


def test_une_famille_inconnue_est_ignoree(poste):
    assert not domotique.annonce("inventee", {}, config=CONF)
    assert poste == []


# ── les échecs ───────────────────────────────────────────────────────────────

def test_une_box_injoignable_ne_leve_pas(monkeypatch):
    """En plein event, une box éteinte ne doit pas faire remonter d'exception."""
    def refuser(_requete, timeout=None):
        raise urllib.error.URLError("connexion refusée")

    monkeypatch.setattr(domotique.urllib.request, "urlopen", refuser)
    # C'est `_poster` qu'on éprouve : dans un fil, une exception serait perdue
    # sans que rien ne le dise.
    domotique._poster(CONF["domotique"]["url"], {"type": "palier"})


def test_une_erreur_http_ne_leve_pas(monkeypatch):
    def refuser(_requete, timeout=None):
        raise urllib.error.HTTPError(
            "http://ha.local", 404, "Not Found", {}, None)

    monkeypatch.setattr(domotique.urllib.request, "urlopen", refuser)
    domotique._poster(CONF["domotique"]["url"], {"type": "palier"})


# ── le bouton d'essai ────────────────────────────────────────────────────────

def test_l_essai_dit_ce_qu_il_ne_peut_pas_promettre(poste):
    """Un webhook sans automatisation derrière répond 200 et ne fait rien."""
    reussi, message = domotique.essayer(CONF["domotique"]["url"])
    assert reussi
    assert "automatisation" in message.lower()
    assert poste[0][1]["type"] == "essai"


def test_l_essai_rejette_une_url_invalide(poste):
    reussi, message = domotique.essayer("pas une url")
    assert not reussi
    assert "http" in message
    assert poste == []


def test_l_essai_rapporte_le_code_de_home_assistant(monkeypatch):
    def refuser(_requete, timeout=None):
        raise urllib.error.HTTPError(
            "http://ha.local", 405, "Method Not Allowed", {}, None)

    monkeypatch.setattr(domotique.urllib.request, "urlopen", refuser)
    reussi, message = domotique.essayer(CONF["domotique"]["url"])
    assert not reussi
    assert "405" in message


def test_l_essai_rapporte_une_box_injoignable(monkeypatch):
    def refuser(_requete, timeout=None):
        raise urllib.error.URLError("nom introuvable")

    monkeypatch.setattr(domotique.urllib.request, "urlopen", refuser)
    reussi, message = domotique.essayer(CONF["domotique"]["url"])
    assert not reussi
    assert "injoignable" in message.lower()


# ── réglages ─────────────────────────────────────────────────────────────────

def test_sans_bloc_toutes_les_familles_sont_actives():
    """Configurer une URL doit suffire : rien d'autre à cocher pour démarrer."""
    assert domotique.reglages({})["evenements"] == list(domotique.EVENEMENTS)


@pytest.mark.parametrize("brut", [None, [], "palier", 42, {"a": 1}])
def test_un_bloc_abime_ne_fait_pas_tomber_les_reglages(brut):
    """config.json s'édite à la main."""
    conf = domotique.reglages({"domotique": brut})
    assert isinstance(conf["evenements"], list)
    assert isinstance(conf["url"], str)


def test_les_familles_inventees_sont_ecartees():
    conf = domotique.reglages(
        {"domotique": {"url": "http://x/y", "evenements": ["palier", "licorne"]}})
    assert conf["evenements"] == ["palier"]


def _fil_attendu() -> None:
    """`annonce` poste dans un fil : on le laisse finir."""
    import threading
    import time

    limite = time.monotonic() + 2.0
    while threading.active_count() > 1 and time.monotonic() < limite:
        time.sleep(0.01)


# ── Ce que Home Assistant montre vraiment ────────────────────────────────────
#
# Son éditeur d'automatisation n'affiche pas d'URL, mais un « ID du webhook ».
# Demander une URL revenait à faire deviner que l'adresse complète vaut
# `<base>/api/webhook/<id>`.

def test_un_identifiant_seul_suffit():
    assert (domotique.composer("http://ha.local:8123", "-XyZ123")
            == "http://ha.local:8123/api/webhook/-XyZ123")


def test_l_adresse_tolere_une_barre_finale():
    assert domotique.composer("http://ha.local:8123/", "abc").endswith(
        ":8123/api/webhook/abc")


def test_une_url_entiere_collee_dans_l_identifiant_est_gardee():
    """C'est ce que fera quiconque l'a trouvée ailleurs."""
    url = "https://ailleurs.example/api/webhook/zz"
    assert domotique.composer("http://ha.local:8123", url) == url


def test_un_identifiant_ne_peut_pas_greffer_un_autre_chemin():
    """Il part dans un chemin d'URL : encodé, il n'y ajoute pas de segment."""
    compose = domotique.composer("http://ha.local:8123", "../../api/onstate")
    assert compose == "http://ha.local:8123/api/webhook/onstate"
    compose = domotique.composer("http://ha.local:8123", "abc?x=1")
    assert compose == "http://ha.local:8123/api/webhook/abc%3Fx%3D1"


@pytest.mark.parametrize("base,ident", [
    ("", "abc"),                 # sans adresse
    ("http://ha.local", ""),     # sans identifiant
    ("", ""),
])
def test_un_morceau_manquant_ne_compose_rien(base, ident):
    assert domotique.composer(base, ident) == ""


def test_les_reglages_reconstituent_l_url():
    conf = domotique.reglages({"domotique": {"base": "http://ha.local:8123",
                                             "webhook_id": "abc"}})
    assert conf["url"] == "http://ha.local:8123/api/webhook/abc"
    assert conf["base"] == "http://ha.local:8123"
    assert conf["webhook_id"] == "abc"


def test_sans_adresse_celle_du_reseau_local_est_proposee():
    conf = domotique.reglages({"domotique": {"webhook_id": "abc"}})
    assert conf["base"] == domotique.BASE_DEFAUT


def test_une_url_ecrite_a_la_main_reste_prioritaire():
    """config.json s'édite : une adresse entière ne doit pas être rejetée."""
    conf = domotique.reglages({"domotique": {
        "url": "http://autre:8123/api/webhook/zz", "webhook_id": "abc"}})
    assert conf["url"] == "http://autre:8123/api/webhook/zz"


def test_l_url_entiere_rend_l_adresse_sans_effet():
    """Selon les versions, Home Assistant donne l'URL et non l'identifiant.

    Collée dans le champ, elle doit primer : corriger l'adresse en dessous ne
    changerait rien, et l'écran la grise pour le dire.
    """
    conf = domotique.reglages({"domotique": {
        "base": "http://une-autre-adresse:8123",
        "webhook_id": "https://homeassist.exemple.fr/api/webhook/-s8cw0QbBg5c",
    }})
    assert conf["url"] == "https://homeassist.exemple.fr/api/webhook/-s8cw0QbBg5c"


def test_une_adresse_avec_barre_finale_compose_juste():
    """C'est ce qu'on obtient en copiant depuis une barre d'adresse."""
    conf = domotique.reglages({"domotique": {
        "base": "https://homeassist.exemple.fr/", "webhook_id": "-s8cw0QbBg5c"}})
    assert conf["url"] == "https://homeassist.exemple.fr/api/webhook/-s8cw0QbBg5c"


# ── L'automatisation prête à coller ──────────────────────────────────────────
#
# Elle est affichée DANS l'application, avec l'identifiant déjà en place :
# aller chercher un fichier markdown, puis y remplacer deux valeurs à la main,
# c'est deux occasions de se tromper avant même d'avoir essayé.

def test_le_yaml_produit_est_analysable():
    """Une indentation fausse ne se verrait qu'au collage, chez l'utilisateur."""
    yaml = pytest.importorskip("yaml")
    charge = yaml.safe_load(domotique.automatisation("-abc"))
    assert list(charge) == ["alias", "description", "triggers", "conditions",
                            "actions", "mode"]


def test_l_identifiant_est_deja_en_place():
    yaml = pytest.importorskip("yaml")
    charge = yaml.safe_load(domotique.automatisation(
        "https://ha.exemple.fr/api/webhook/-s8cw0QbBg5c"))
    assert charge["triggers"][0]["webhook_id"] == "-s8cw0QbBg5c"


def test_les_lampes_choisies_remplacent_l_exemple():
    yaml = pytest.importorskip("yaml")
    charge = yaml.safe_load(domotique.automatisation("-abc", "light.bureau"))
    assert charge["actions"][0]["data"]["snapshot_entities"] == ["light.bureau"]
    assert "light.salon" not in domotique.automatisation("-abc", "light.bureau")


def test_sans_webhook_le_yaml_le_dit():
    """Un identifiant vide donnerait une automatisation qui ne part jamais."""
    assert domotique.SANS_WEBHOOK in domotique.automatisation("")


def test_la_premiere_version_n_a_pas_de_condition():
    """C'est ce qui permet au bouton d'essai de vérifier la chaîne entière."""
    yaml = pytest.importorskip("yaml")
    assert yaml.safe_load(domotique.automatisation("-abc"))["conditions"] == []


def test_l_eclairage_est_remis_comme_avant():
    """Sans cette dernière étape, les lampes restent éteintes après coup."""
    yaml = pytest.importorskip("yaml")
    actions = yaml.safe_load(domotique.automatisation("-abc"))["actions"]
    assert actions[0]["action"] == "scene.create"
    assert actions[-1]["action"] == "scene.turn_on"


def test_le_clignotement_dure_dix_secondes():
    yaml = pytest.importorskip("yaml")
    actions = yaml.safe_load(domotique.automatisation("-abc"))["actions"]
    boucle = next(a for a in actions if "repeat" in a)["repeat"]
    attentes = sum(e["delay"]["milliseconds"] for e in boucle["sequence"]
                   if "delay" in e)
    assert boucle["count"] * attentes == 10_000


@pytest.mark.parametrize("colle,attendu", [
    ("-abc", "-abc"),
    ("https://ha.exemple.fr/api/webhook/-abc", "-abc"),
    ("https://ha.exemple.fr/api/webhook/-abc/", "-abc"),
    ("  -abc  ", "-abc"),
    ("", ""),
])
def test_l_identifiant_est_extrait_de_ce_qui_est_colle(colle, attendu):
    assert domotique.identifiant(colle) == attendu


# ── Ce que ZLink annonce de lui-même ─────────────────────────────────────────
#
# Sans User-Agent, Python annonce « Python-urllib/3.x », que les protections
# anti-robots refusent. Un Home Assistant publié derrière Cloudflare répondait
# 403 tout en acceptant la même requête d'un navigateur — et le code d'erreur
# ne venait même pas de Home Assistant, qui n'avait jamais vu la requête.

def test_les_envois_annoncent_zlink(poste):
    domotique.annonce("palier", {"montant": 1.0}, config=CONF)
    _fil_attendu()
    assert poste, "rien n'est parti"


def test_l_entete_porte_un_agent_qui_n_est_pas_celui_de_python():
    entetes = domotique._entetes()
    assert entetes["User-Agent"].startswith("ZLink/")
    assert "urllib" not in entetes["User-Agent"].lower()


def test_l_envoi_pose_bien_l_agent(monkeypatch):
    vus = {}

    class _R:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def capter(requete, timeout=None):
        vus["ua"] = requete.get_header("User-agent")
        return _R()

    monkeypatch.setattr(domotique.urllib.request, "urlopen", capter)
    domotique._poster("http://ha.local/api/webhook/x", {"type": "palier"})
    assert vus["ua"].startswith("ZLink/")


def test_l_essai_pose_aussi_l_agent(monkeypatch):
    vus = {}

    class _R:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def capter(requete, timeout=None):
        vus["ua"] = requete.get_header("User-agent")
        return _R()

    monkeypatch.setattr(domotique.urllib.request, "urlopen", capter)
    domotique.essayer("http://ha.local/api/webhook/x")
    assert vus["ua"].startswith("ZLink/")


# ── Les codes d'erreur doivent dire quoi faire ───────────────────────────────

@pytest.mark.parametrize("code,attendu", [
    (403, "local_only"),      # proxy, Cloudflare, ou déclencheur local
    (404, "/api/webhook/"),   # mauvaise adresse
    (405, "POST"),            # méthode refusée
    (502, "allumée"),         # proxy sans box derrière
])
def test_un_code_d_erreur_explique_ce_qui_a_refuse(code, attendu):
    """Home Assistant répond 200 même à un webhook inconnu : un code d'erreur
    vient presque toujours de ce qui est DEVANT lui."""
    assert attendu in domotique._expliquer(code)


def test_un_code_inconnu_est_rapporte_tel_quel():
    assert "418" in domotique._expliquer(418)


# ── local_only : la panne qui répond 200 ─────────────────────────────────────
#
# Un déclencheur `local_only: true` atteint par un domaine public est écarté
# par Home Assistant, QUI RÉPOND QUAND MÊME 200 — pour ne pas révéler quels
# webhooks existent. Symptôme : « l'essai réussit et rien ne s'allume », sans
# la moindre trace côté box. ZLink connaît l'adresse : il règle donc lui-même.

@pytest.mark.parametrize("url,local", [
    ("http://homeassistant.local:8123/api/webhook/x", True),
    ("http://localhost:8123/api/webhook/x", True),
    ("http://127.0.0.1:8123/x", True),
    ("http://192.168.1.42:8123/x", True),
    ("http://10.0.0.7:8123/x", True),
    ("http://172.16.0.5:8123/x", True),      # début de 172.16.0.0/12
    ("http://172.31.255.1:8123/x", True),    # fin de la plage
    ("http://ha.lan:8123/x", True),
    ("https://homeassist.exemple.fr/api/webhook/x", False),
    ("http://172.32.0.5:8123/x", False),     # juste au-delà de la plage privée
    ("http://8.8.8.8/x", False),
    ("", False),
])
def test_une_adresse_est_locale_ou_ne_l_est_pas(url, local):
    assert domotique.est_local(url) is local


def test_une_adresse_distante_desactive_local_only():
    """Sinon on livre une automatisation qui ne se déclenchera jamais."""
    yaml = pytest.importorskip("yaml")
    charge = yaml.safe_load(domotique.automatisation(
        "https://homeassist.exemple.fr/api/webhook/-abc"))
    assert charge["triggers"][0]["local_only"] is False


def test_une_adresse_locale_garde_local_only():
    """C'est le réglage sûr : le webhook n'est alors pas exposé à Internet."""
    yaml = pytest.importorskip("yaml")
    charge = yaml.safe_load(domotique.automatisation(
        "-abc", url="http://192.168.1.42:8123/api/webhook/-abc"))
    assert charge["triggers"][0]["local_only"] is True


def test_le_message_d_essai_ne_promet_pas_l_execution(poste):
    """200 ne prouve que l'acheminement : le dire, plutôt que laisser croire."""
    _reussi, message = domotique.essayer("http://ha.local/api/webhook/x")
    assert "ne dit PAS" in message
    assert "Traces" in message


# ── Le nom des lampes ────────────────────────────────────────────────────────
#
# « light.salon » avait l'air d'une valeur plausible. Collé tel quel, il donne
# une automatisation qui se déclenche, s'exécute, et n'allume rien : la panne
# la plus pénible à diagnostiquer, puisque le trace de Home Assistant montre
# un déclenchement réussi.

def test_sans_lampes_le_nom_est_manifestement_faux():
    yaml = pytest.importorskip("yaml")
    charge = yaml.safe_load(domotique.automatisation("-abc"))
    cible = charge["actions"][0]["data"]["snapshot_entities"][0]
    assert cible == domotique.LAMPES_DEFAUT
    assert "remplacez" in cible, "le nom doit se remarquer dans l'éditeur"


def test_les_lampes_sont_conservees_entre_deux_ouvertures():
    """Les ressaisir à chaque fois inviterait à recoller l'exemple."""
    conf = domotique.reglages({"domotique": {"lampes": "light.bureau"}})
    assert conf["lampes"] == "light.bureau"


# ── Le clair : risque réel, et seulement là où il l'est ─────────────────────
#
# Home Assistant sert sur 8123 sans TLS et n'a pas de certificat pour
# « homeassistant.local » : exiger https ferait échouer l'installation par
# défaut de tout le monde, pour un trafic qui ne quitte pas la maison. Dès que
# l'adresse est publique, en revanche, l'identifiant du webhook — qui tient
# lieu de mot de passe — voyagerait en clair.

@pytest.mark.parametrize("url", [
    "http://homeassistant.local:8123/api/webhook/x",
    "http://192.168.1.42:8123/api/webhook/x",
    "http://10.0.0.7:8123/x",
    "https://homeassist.exemple.fr/api/webhook/x",
    "",
])
def test_rien_a_dire_quand_le_clair_ne_sort_pas_du_reseau(url):
    assert domotique.avertissement_clair(url) == ""


def test_le_clair_sur_internet_est_signale():
    message = domotique.avertissement_clair("http://homeassist.exemple.fr/api/webhook/x")
    assert "en clair" in message
    assert "https" in message


def test_le_defaut_reste_en_clair():
    """Sinon la connexion échouerait chez tous ceux qui n'ont pas de certificat."""
    assert domotique.BASE_DEFAUT.startswith("http://")
    assert domotique.est_local(domotique.BASE_DEFAUT), (
        "un défaut en clair n'est acceptable que parce qu'il est local")


# ── n'annoncer que ses favoris ───────────────────────────────────────────────

CONF_FAVORIS = {"domotique": {**CONF["domotique"], "favoris_seulement": True}}


@pytest.fixture
def favoris(monkeypatch):
    """Pose la liste des favoris sans toucher à config.json.

    `_dans_la_portee` importe `core.favorites` au moment de l'appel : c'est
    donc le module lui-même qu'on remplace, pas une référence capturée.
    """
    from core import favorites

    def poser(*logins):
        monkeypatch.setattr(favorites, "get", lambda: set(logins))
    return poser


def test_sans_filtre_tout_part_comme_avant():
    """Le réglage est faux par défaut : personne ne perd d'annonce."""
    assert domotique.reglages(CONF)["favoris_seulement"] is False


def test_le_filtre_laisse_passer_un_favori(favoris):
    favoris("zerator")
    conf = domotique.reglages(CONF_FAVORIS)
    assert domotique._dans_la_portee(conf, {"login": "zerator"})


def test_le_filtre_arrete_une_chaine_non_suivie(favoris):
    """Trois cents participants : « don » et « hype » partaient sans arrêt."""
    favoris("zerator")
    conf = domotique.reglages(CONF_FAVORIS)
    assert not domotique._dans_la_portee(conf, {"login": "quelquun_dautre"})


def test_le_filtre_ignore_la_casse(favoris):
    """Les favoris sont rangés en minuscules ; l'API rend parfois autre chose."""
    favoris("mistermv")
    conf = domotique.reglages(CONF_FAVORIS)
    assert domotique._dans_la_portee(conf, {"login": "MisterMV"})


def test_un_palier_part_toujours(favoris):
    """Il n'appartient à personne, et n'est pas ce qui noie la maison."""
    favoris("zerator")
    conf = domotique.reglages(CONF_FAVORIS)
    assert domotique._dans_la_portee(conf, {"montant": 1_000_000.0,
                                            "libelle": "1 M€"})


def test_le_filtre_agit_sur_l_envoi_reel(poste, favoris):
    """Posé dans `annonce`, il couvre les quatre familles d'un coup."""
    favoris("zerator")
    assert domotique.annonce("don", {"login": "zerator"}, config=CONF_FAVORIS)
    assert not domotique.annonce("hype", {"login": "inconnu"},
                                 config=CONF_FAVORIS)
    assert domotique.annonce("palier", {"libelle": "1 M€"},
                             config=CONF_FAVORIS)
    assert [e[1]["type"] for e in poste] == ["don", "palier"]


def test_sans_favori_le_filtre_ne_laisse_que_les_paliers(poste, favoris):
    """Cocher la case sans avoir posé d'étoile éteint presque tout : c'est
    cohérent, et l'infobulle du réglage le dit."""
    favoris()
    assert not domotique.annonce("don", {"login": "zerator"},
                                 config=CONF_FAVORIS)
    assert domotique.annonce("palier", {"libelle": "1 M€"},
                             config=CONF_FAVORIS)
