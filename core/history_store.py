# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Historique temps réel des données globales ZEvent."""

from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# En test → renvoie les données même hors fenêtre event.
# Mettre à False avant l'event pour activer le filtrage.
DEBUG: bool = True

# Bornes réelles de l'édition 2026 (schedule de l'API events)
_EVENT_START: float = datetime(2026, 9, 3, 0, 0, 0, tzinfo=timezone.utc).timestamp()
_EVENT_END: float = datetime(2026, 9, 7, 2, 0, 0, tzinfo=timezone.utc).timestamp()

#: Ouverture de la CAGNOTTE, qui n'est pas l'ouverture de l'événement : les
#: directs commencent le jeudi soir, la collecte le vendredi.
#:
#: C'est l'origine des courbes, et le repère qui les rend comparables. Relevé
#: sur les quatre éditions chargées, il tombe le VENDREDI à 15-16 h UTC sans
#: exception : 2025 le 5 septembre à 16 h, 2024 le 6 à 16 h, 2022 le 9 à 16 h,
#: 2021 le 29 octobre à 15 h.
#:
#: Il sert aussi d'origine aux ABSCISSES. Les étiquettes se calculaient sur
#: l'horloge du moment : à sept jours de l'événement, le graphe annonçait
#: « jeudi 16 h » en regard d'une valeur qui, elle, appartenait au vendredi
#: soir de 2025. L'axe suit désormais le calendrier de l'édition, quel que
#: soit le jour où l'on regarde.
OUVERTURE_CAGNOTTE: float = datetime(
    2026, 9, 4, 16, 0, 0, tzinfo=timezone.utc).timestamp()

#: Cache par édition d'evenmorestats. L'identifiant de l'édition complète
#: l'adresse : c'est ce qui rend plusieurs années superposables, là où le
#: dépôt historique ne publie que la dernière.
_URL_EDITION_BASE = (
    "https://evenmorestats-cache.s3.gra.io.cloud.ovh.net/metrics/")

#: ZEvent 2025, l'édition de référence pour comparer 2026.
EDITION_PRECEDENTE = "019d3f95-bd24-7e5d-861b-1de6243e3169"

#: Éditions superposables, de la plus récente à la plus ancienne. Relevées
#: sur `api.ppr.evenmorestats.fr/events`, qui les publie toutes.
#:
#: 2021 publie ses relevés À L'ENVERS — du 1er novembre au 29 octobre. Lue
#: brute, sa courbe s'arrête à 16 135 € pour un total déclaré de 10 064 480 ;
#: triée, elle retombe sur 10 062 185, à deux millièmes près. C'est le tri de
#: `_lire_serie` qui la rend exploitable, pas une exception écrite à la main.
#:
#: Les deux contrôles ci-dessous ne visent donc personne en particulier : ils
#: sont là pour que l'édition qui se casserait un jour s'écarte d'elle-même,
#: au lieu de tracer une année sans dons qu'on lirait comme vraie.
#:
#: 2020 et 2019 n'ont pas de métriques du tout, et 2023 n'existe pas.
EDITIONS: tuple[tuple[str, str], ...] = (
    ("2025", "019d3f95-bd24-7e5d-861b-1de6243e3169"),
    ("2024", "019ebc62-7050-751b-aee8-eb7dc7ab6ceb"),
    ("2022", "019ebc61-62e7-7fdc-aa5c-a0047c921878"),
    ("2021", "019ebc50-efd9-7070-9663-404f8d79a410"),
)

#: Écart toléré entre le total déclaré d'une édition et la fin de sa courbe.
#: Deux pour cent : les relevés sont échantillonnés, la dernière mesure peut
#: précéder de peu la clôture.
_TOLERANCE_TOTAL = 0.02

# Plage de timestamps acceptée pour les données tierces (2020 → 2035)
_TS_MIN: float = 1_577_836_800.0
_TS_MAX: float = 2_051_222_400.0


def _sane_point(ts_ms: object, val: object) -> tuple[float, float] | None:
    """Valide un point (timestamp ms, valeur) issu du dépôt tiers.

    Retourne None si le point est inexploitable : une valeur aberrante
    remonterait jusqu'à datetime.fromtimestamp() dans un slot Qt sans
    try/except, ce qui ferait tomber le panel.
    """
    if not isinstance(ts_ms, (int, float)) or not isinstance(val, (int, float)):
        return None
    if isinstance(ts_ms, bool) or isinstance(val, bool):
        return None
    ts = float(ts_ms) / 1000.0
    if not (_TS_MIN <= ts <= _TS_MAX):
        logger.warning("Historique: timestamp hors plage ignoré (%r)", ts_ms)
        return None
    return ts, float(val)


#: Étendue minimale de la courbe en cours avant d'y superposer les éditions
#: passées. En deçà, elles seraient écrasées sur quelques minutes de relevés :
#: 2021 y gagnait deux cent quarante mille euros en un quart d'heure.
_ETENDUE_MINIMALE_COMPARAISON = 3600.0


def comparaison_possible(instants: list[float]) -> bool:
    """Peut-on superposer les éditions passées à celle-ci.

    La question porte sur les DONNÉES, pas sur le calendrier. Une constante
    d'ouverture était une hypothèse : relevée sur les éditions précédentes,
    elle tombait le vendredi — or la cagnotte 2026 a ouvert le jeudi à midi.
    Un garde calendaire aurait masqué douze heures de comparaison parfaitement
    valables.

    Ce qui compte est qu'il y ait de quoi comparer : les éditions sont alignées
    sur le TEMPS DE COURSE compté depuis le premier point de la courbe en
    cours, et une courbe de trois minutes n'en offre pas.
    """
    if len(instants) < 2:
        return False
    return (instants[-1] - instants[0]) >= _ETENDUE_MINIMALE_COMPARAISON


class HistoryStore:
    """Stocke les séries temporelles donation + viewers depuis le démarrage.

    4320 points × poll 30 s = 36 heures de rétention (couvre l'event 3 jours).
    Les données historiques pré-chargées depuis GitHub remplissent la base ;
    les données live s'ajoutent par-dessus en temps réel.
    """

    def __init__(self, max_points: int = 4320) -> None:
        self._donation: deque[tuple[float, float]] = deque(maxlen=max_points)
        self._viewers: deque[tuple[float, int]] = deque(maxlen=max_points)
        # Courbe de l'édition PRÉCÉDENTE, conservée à part. Elle sert de point
        # de comparaison — « à la même heure l'an dernier » — et ne doit donc
        # pas se mélanger aux points de l'édition en cours, que get_*_series
        # filtre par fenêtre.
        self._previous: list[tuple[float, float]] = []
        #: Audience de l'édition précédente. Seule la cagnotte était conservée
        #: en série ; il n'y avait qu'un pic. Superposer deux courbes demande
        #: la série entière des deux côtés.
        self._previous_viewers: list[tuple[float, float]] = []
        self._previous_peak_viewers: int = 0
        #: Éditions passées retenues : libellé → {dons, vues}. Plusieurs, parce
        #: qu'une seule référence ne dit pas si l'année dernière était un bon
        #: cru — trois courbes situent l'édition en cours dans une tendance.
        self._editions: dict[str, dict] = {}
        #: Instant du premier relevé pris EN DIRECT depuis le dernier
        #: préchargement. Sépare ce que ZLink a observé lui-même de la courbe
        #: chargée depuis GitHub, les deux vivant dans le même deque.
        self._live_depuis: float | None = None

    def add_point(self, donation: float, viewers: int,
                  instant: float | None = None) -> None:
        """Range un relevé. `instant` permet d'en antidater un.

        Antidater sert au mode mock, qui simule un événement DÉJÀ en cours :
        sans un peu de passé, la vitesse de collecte n'a rien à comparer et
        reste muette six minutes durant — le temps qu'exige son plus petit
        écart mesurable.
        """
        ts = time.time() if instant is None else float(instant)
        if self._live_depuis is None or ts < self._live_depuis:
            self._live_depuis = ts
        self._donation.append((ts, donation))
        self._viewers.append((ts, viewers))

    def _in_event_window(self) -> bool:
        return _EVENT_START <= time.time() <= _EVENT_END

    def _garder(self, ts: float) -> bool:
        """Ce point appartient-il à l'édition EN COURS.

        Un point de la fenêtre de l'édition en est toujours.

        En dehors, DEBUG tranche — et seulement pour les relevés pris EN
        DIRECT. C'est ce que le module annonce depuis toujours (« en test,
        renvoie les données même hors fenêtre event ») sans jamais le faire :
        le premier garde respectait DEBUG, ce filtre-ci l'ignorait, et la
        série revenait vide quand même. Le mode mock restait donc sans
        courbe, sans vitesse et sans projection.

        L'historique préchargé, lui, reste borné à sa fenêtre quoi qu'il
        arrive : le deque porte aussi la courbe de l'édition précédente, et
        la publier donnerait la vitesse de fin du ZEvent 2025.
        """
        if _EVENT_START <= ts <= _EVENT_END:
            return True
        return DEBUG and self._live_depuis is not None and ts >= self._live_depuis

    def get_donation_series(self) -> tuple[list[float], list[float]]:
        if not DEBUG and not self._in_event_window():
            return [], []
        if not self._donation:
            return [], []
        pairs = [(ts, v) for ts, v in self._donation if self._garder(ts)]
        if not pairs:
            return [], []
        ts, vals = zip(*pairs)
        return list(ts), list(vals)

    # -- vitesse et projection -------------------------------------------------

    def donation_rate(self, window_s: float = 900.0) -> float | None:
        """Vitesse de collecte en euros par minute sur la fenêtre demandée.

        Ne lit QUE les points de l'édition en cours : le deque contient aussi
        l'historique de l'édition précédente, préchargé depuis GitHub. Le lire
        brut donnait la vitesse de fin du ZEvent 2025, ou une pente négative au
        raccord entre les deux séries.

        Retourne None tant qu'on n'a pas deux points espacés d'au moins un
        dixième de la fenêtre : sur un intervalle trop court, le bruit de
        l'arrondi de la cagnotte donnerait une vitesse fantaisiste.
        """
        ts_all, vals_all = self.get_donation_series()
        if len(ts_all) < 2:
            return None
        now = ts_all[-1]
        cutoff = now - window_s
        window = [(t, v) for t, v in zip(ts_all, vals_all) if t >= cutoff]
        if len(window) < 2:
            window = list(zip(ts_all, vals_all))[-2:]
        (t0, v0), (t1, v1) = window[0], window[-1]
        span = t1 - t0
        if span < max(window_s * 0.1, 30.0):
            return None
        rate = (v1 - v0) / (span / 60.0)
        # Une cagnotte est cumulative : elle ne décroît pas. Une pente négative
        # ne peut venir que d'une correction de l'API ou d'un raccord de séries,
        # et l'extrapoler produirait une projection négative.
        return rate if rate >= 0.0 else None

    def _horizon(self, end_ts: float) -> float | None:
        """Jusqu'où extrapoler, ou None si extrapoler n'a pas de sens.

        Pendant l'édition, c'est sa fin. En dehors, rien : projeter jusqu'au
        7 septembre depuis une date d'août donnait des milliards, et c'est
        pour ça que le garde-fou existe.

        En test, ce garde-fou fermait la porte que DEBUG venait d'ouvrir : la
        série et la vitesse revenaient bien, mais la projection restait vide et
        l'Accueil affichait « disponible au début de l'event » alors que le
        mode mock injectait des dons à chaque seconde. On extrapole alors sur
        la DURÉE d'une édition comptée depuis le premier relevé — le mock
        simule un événement en cours, la projection porte donc sur un
        événement de même longueur, et garde un ordre de grandeur plausible
        au lieu de courir sur deux semaines.
        """
        maintenant = time.time()
        if _EVENT_START <= maintenant <= _EVENT_END:
            return end_ts
        if not DEBUG or self._live_depuis is None:
            return None
        return self._live_depuis + (_EVENT_END - _EVENT_START)

    def projected_total(self, end_ts: float, window_s: float = 3600.0) -> float | None:
        """Extrapole la cagnotte finale à la vitesse récente.

        Volontairement linéaire : la collecte n'est pas linéaire sur trois jours
        (pics de soirée, dernière ligne droite), donc c'est un ordre de grandeur
        « au rythme actuel », pas une prédiction. À afficher comme tel.

        Retourne None hors de l'édition : extrapoler quatorze jours avant le
        coup d'envoi n'a aucun sens et donnait des milliards.
        """
        horizon = self._horizon(end_ts)
        if horizon is None:
            return None
        end_ts = horizon
        ts_all, vals_all = self.get_donation_series()
        if not ts_all:
            return None
        rate = self.donation_rate(window_s)
        if rate is None:
            return None
        now, current = ts_all[-1], vals_all[-1]
        remaining_min = (end_ts - now) / 60.0
        if remaining_min <= 0:
            return current
        return current + rate * remaining_min

    # -- comparaison avec l'édition précédente ---------------------------------

    @staticmethod
    def origine_course(dons: list) -> float | None:
        """Instant où la collecte démarre : le premier relevé NON NUL.

        Pas le premier relevé tout court. Les éditions passées sont publiées à
        partir de l'ouverture des dons — leur premier point est déjà positif —
        alors que celle en cours est relevée dès l'ouverture des DIRECTS, six
        heures et demie plus tôt, à zéro.

        Caler sur le premier relevé décalait donc les comparaisons d'autant :
        au bout de sept heures de course, 2026 en était à sa première heure de
        collecte quand 2021 affichait déjà sept heures et un million d'euros.
        """
        for ts, valeur in dons:
            if valeur > 0:
                return ts
        return dons[0][0] if dons else None

    @staticmethod
    def _interpoler(points: list, elapsed_s: float,
                    origine: float | None = None) -> float | None:
        """Valeur d'une série après `elapsed_s`, entre les deux points encadrants.

        L'origine est celle de la COURSE — l'ouverture des dons — et non une
        date : deux éditions ne tombent pas les mêmes jours, et c'est le temps
        de course qui les rend comparables. Hors de la plage couverte, rien :
        extrapoler une édition terminée n'aurait pas de sens.
        """
        if len(points) < 2 or elapsed_s < 0:
            return None
        cible = (points[0][0] if origine is None else origine) + elapsed_s
        if cible > points[-1][0]:
            return None
        # Les points sont peu nombreux (quelques centaines) : la recherche
        # linéaire coûte moins qu'un index à maintenir.
        for (ta, va), (tb, vb) in zip(points, points[1:]):
            if ta <= cible <= tb:
                if tb == ta:
                    return vb
                return va + (vb - va) * (cible - ta) / (tb - ta)
        return None

    def _alignee(self, points: list, ts_courants: list[float],
                 origine_ref: float | None = None) -> list[float | None]:
        """La série `points` replacée sur l'horloge de l'édition en cours.

        Une valeur par instant courant, pour que les deux courbes partagent
        exactement le même axe : Chart.js aligne ses séries par INDICE, pas par
        abscisse — deux tableaux de longueurs différentes se décaleraient.

        Les deux temps de course sont comptés depuis l'OUVERTURE DES DONS de
        chaque édition, et non depuis leur premier relevé : celui de l'édition
        en cours précède sa collecte de plusieurs heures.

        None là où l'édition de référence ne couvre pas encore, ou plus : la
        courbe s'y interrompt au lieu de retomber à zéro.
        """
        if not points or not ts_courants:
            return [None] * len(ts_courants)
        depart = self._origine_courante(ts_courants)
        return [self._interpoler(points, t - depart, origine_ref)
                for t in ts_courants]

    def _origine_courante(self, ts_courants: list[float]) -> float:
        """Ouverture des dons de l'édition en cours, dans l'axe fourni."""
        origine = self.origine_course(
            [(t, v) for t, v in self._donation if self._garder(t)])
        if origine is None or not ts_courants:
            return ts_courants[0] if ts_courants else 0.0
        # Bornée à l'axe : une origine hors de la fenêtre affichée
        # décalerait toutes les références d'un bloc.
        return min(max(origine, ts_courants[0]), ts_courants[-1])

    def serie_precedente_alignee(
            self, ts_courants: list[float]) -> list[float | None]:
        """Cagnotte de l'édition précédente, au même temps de course."""
        return self._alignee(self._previous, ts_courants)

    def serie_viewers_precedente_alignee(
            self, ts_courants: list[float]) -> list[float | None]:
        """Audience de l'édition précédente, au même temps de course."""
        return self._alignee(self._previous_viewers, ts_courants)

    def previous_total_at(self, elapsed_s: float) -> float | None:
        """Cagnotte de l'édition précédente après `elapsed_s` de course.

        L'alignement se fait sur le TEMPS ÉCOULÉ depuis le coup d'envoi, pas
        sur la date : les deux éditions ne tombent pas les mêmes jours. Le
        premier point relevé sert d'origine — la collecte publiée commence avec
        l'événement.

        Interpolation linéaire entre les deux points encadrants ; None si l'on
        sort de la plage couverte.
        """
        return self._interpoler(self._previous, elapsed_s)

    def compare_to_previous(self, current_total: float,
                            now_ts: float | None = None) -> tuple[float, float] | None:
        """(total de l'édition précédente au même instant, écart en %).

        None hors de l'édition en cours, ou si la référence ne couvre pas ce
        moment — comparer sans référence n'aurait aucun sens.
        """
        now = now_ts if now_ts is not None else time.time()
        if not (_EVENT_START <= now <= _EVENT_END):
            return None
        # Origine = premier point RELEVÉ de l'édition en cours, pas _EVENT_START :
        # celui-ci est une frontière de minuit servant à filtrer la fenêtre,
        # alors que le direct démarre en soirée. Prendre minuit décalait la
        # comparaison de plus de quinze heures, et faisait sortir de la plage
        # couverte bien avant la fin. Les deux courbes sont ainsi alignées sur
        # « l'instant où la cagnotte a commencé à être comptée ».
        ts_live, _ = self.get_donation_series()
        origine = ts_live[0] if ts_live else _EVENT_START
        ref = self.previous_total_at(now - origine)
        if ref is None or ref <= 0 or current_total <= 0:
            return None
        return ref, (current_total - ref) / ref * 100.0

    @property
    def previous_peak_viewers(self) -> int:
        return self._previous_peak_viewers

    @property
    def event_start_ts(self) -> float:
        return _EVENT_START

    @property
    def event_end_ts(self) -> float:
        return _EVENT_END

    def get_viewers_series(self) -> tuple[list[float], list[int]]:
        if not DEBUG and not self._in_event_window():
            return [], []
        if not self._viewers:
            return [], []
        pairs = [(ts, v) for ts, v in self._viewers if self._garder(ts)]
        if not pairs:
            return [], []
        ts, vals = zip(*pairs)
        return list(ts), list(vals)

    @staticmethod
    async def _telecharger_edition(event_id: str, client=None) -> dict | None:
        """Le JSON d'une édition, ou None si la source se dérobe.

        Rendre None plutôt que lever : une comparaison manquante retire une
        courbe, elle n'empêche pas de suivre l'événement.
        """
        url = (_URL_EDITION_BASE + str(event_id).strip("/ ") + "/global.json")
        try:
            import httpx
            if client is None:
                async with httpx.AsyncClient(timeout=15) as propre:
                    reponse = await propre.get(url)
                    reponse.raise_for_status()
                    return reponse.json()
            reponse = await client.get(url)
            reponse.raise_for_status()
            return reponse.json()
        except Exception as exc:  # noqa: BLE001 — toute panne réseau vaut « pas de courbe »
            logger.warning("Édition %s : historique indisponible — %s",
                           event_id, exc)
            return None

    async def charger_edition_en_cours(self, event_id: str, client=None) -> bool:
        """Préremplit la courbe de l'édition EN COURS, depuis son début.

        Sans elle, ZLink ne trace que ce qu'il a relevé lui-même : lancer le
        panel à minuit donnait une minute de courbe, sur un graphe qui annonce
        soixante-douze heures — et l'axe des ordonnées, tassé sur cet
        intervalle, répétait « 535k€ » huit fois de suite.

        La source est celle des éditions passées, à l'identifiant de l'édition
        courante près. Elle publie déjà l'événement depuis son ouverture, au
        même format et à la même finesse : rien à inventer, ni à aller chercher
        ailleurs.

        Les points chargés sont posés AVANT ceux du direct, et `_live_depuis`
        reste vierge — ce préchargement n'est pas une observation de ZLink, et
        `_garder` doit continuer de le borner à la fenêtre de l'édition.
        """
        data = await self._telecharger_edition(event_id, client)
        if data is None:
            return False
        graphe = (data or {}).get("graph") or {}
        dons = self._lire_serie(graphe.get("donations", {}).get("all"))
        vues = self._lire_serie(graphe.get("viewers"))
        if len(dons) < 2:
            logger.warning("Édition en cours : courbe de cagnotte inexploitable")
            return False
        if not self._chronologique(dons, "en cours"):
            return False
        # Tout-ou-rien, et les listes sont construites AVANT de toucher à
        # l'état : une section viewers manquante ne doit pas laisser la
        # cagnotte préchargée face à une audience vide.
        self._donation.clear()
        self._donation.extend(dons)
        self._viewers.clear()
        self._viewers.extend((t, int(v)) for t, v in vues)
        self._live_depuis = None
        logger.info(
            "Édition en cours préchargée : %d points de cagnotte, %d de "
            "viewers, depuis %s",
            len(dons), len(vues),
            datetime.fromtimestamp(dons[0][0], tz=timezone.utc).isoformat())
        return True

    async def charger_editions(self, editions=EDITIONS, client=None) -> list[str]:
        """Charge toutes les éditions superposables. Rend celles qui tiennent.

        Une édition dont la courbe contredit son propre total déclaré est
        écartée : mieux vaut une comparaison de moins qu'une comparaison
        fausse, qu'on ne saurait pas lire comme telle.

        La plus récente qui survit devient aussi la référence de la phrase
        « à la même heure en … », qui n'en compare qu'une.
        """
        retenues: list[str] = []
        for libelle, event_id in editions:
            if await self.charger_edition(event_id, client=client,
                                          libelle=libelle):
                retenues.append(libelle)
        if retenues:
            recente = self._editions[retenues[0]]
            self._previous = recente["dons"]
            self._previous_viewers = recente["vues"]
            if recente["vues"]:
                self._previous_peak_viewers = int(
                    max(v for _t, v in recente["vues"]))
        logger.info("Éditions superposables : %s",
                    ", ".join(retenues) or "aucune")
        return retenues

    def editions_chargees(self) -> list[str]:
        """Libellés des éditions retenues, de la plus récente à la plus vieille."""
        return list(self._editions)

    def series_editions_alignees(
            self, ts_courants: list[float]) -> dict[str, list]:
        """Cagnotte de chaque édition retenue, au même temps de course."""
        return {libelle: self._alignee(donnees["dons"], ts_courants,
                                       donnees.get("origine"))
                for libelle, donnees in self._editions.items()}

    def series_viewers_editions_alignees(
            self, ts_courants: list[float]) -> dict[str, list]:
        """Audience de chaque édition retenue, au même temps de course."""
        # L'origine reste celle des DONS : le temps de course est une
        # propriété de l'édition, pas de la métrique qu'on regarde. Les
        # audiences d'avant l'ouverture existent, elles ne sont pas la course.
        return {libelle: self._alignee(donnees["vues"], ts_courants,
                                       donnees.get("origine"))
                for libelle, donnees in self._editions.items()}

    @staticmethod
    def _coherente(data: dict, dons: list, libelle: str) -> bool:
        """La courbe finit-elle bien sur le total que l'édition annonce.

        `donation_amount` est en CENTIMES, la courbe en euros. Une édition dont
        la courbe s'arrêterait loin de son total tracerait une année sans dons,
        qu'on lirait comme vraie faute de savoir qu'elle est fausse.
        """
        declare = data.get("donation_amount")
        if not isinstance(declare, (int, float)) or declare <= 0:
            return True     # rien à confronter : on fait confiance à la courbe
        declare = float(declare) / 100.0
        fin = dons[-1][1]
        if abs(declare - fin) <= declare * _TOLERANCE_TOTAL:
            return True
        logger.warning(
            "Édition %s écartée : la courbe finit à %.0f € pour un total "
            "déclaré de %.0f €", libelle, fin, declare)
        return False

    @staticmethod
    def _chronologique(points: list, libelle: str) -> bool:
        """Les relevés couvrent-ils bien une durée croissante.

        `_lire_serie` trie déjà, ce qui suffit au cas connu — 2021, publiée à
        l'envers. Ce contrôle attrape ce que le tri ne peut pas réparer : une
        série dont tous les relevés portent le même instant n'a pas de temps
        de course, et l'aligner n'aurait aucun sens.
        """
        if len(points) < 2 or points[-1][0] > points[0][0]:
            return True
        logger.warning("Édition %s écartée : relevés non chronologiques", libelle)
        return False

    async def charger_edition(self, event_id: str, client=None,
                              libelle: str = "") -> bool:
        """Charge la courbe d'une édition passée, par son identifiant.

        Source `evenmorestats-cache`, adressée PAR ÉDITION — c'est ce qui
        permettra d'en superposer plusieurs le jour où on le voudra, là où le
        dépôt historique ne publie que la dernière. Elle est aussi trois fois
        plus fine : 332 relevés de cagnotte contre 110.

        Les valeurs y sont déjà en euros, contrairement au reste des APIs
        communautaires qui comptent en centimes — vérifié sur l'édition 2025,
        dont le total ressort à 16 178 394 et non à cent fois plus.

        Rend False sans rien casser si la source se dérobe : une comparaison
        manquante retire une courbe, elle n'empêche pas de suivre l'événement.
        """
        data = await self._telecharger_edition(event_id, client)
        if data is None:
            return False

        nom = libelle or str(event_id)
        graphe = (data or {}).get("graph") or {}
        dons = self._lire_serie(graphe.get("donations", {}).get("all"))
        vues = self._lire_serie(graphe.get("viewers"))
        if len(dons) < 2:
            logger.warning("Édition %s : courbe de cagnotte inexploitable", nom)
            return False
        # Deux contrôles, parce que la source publie parfois des séries
        # inexploitables sans le signaler. Voir `EDITIONS`.
        if not self._chronologique(dons, nom) or not self._coherente(
                data, dons, nom):
            return False

        # Tout-ou-rien, comme le chargeur historique : remplacer une série et
        # pas l'autre laisserait deux éditions différentes côte à côte.
        self._editions[nom] = {"dons": dons, "vues": vues,
                               "origine": self.origine_course(dons)}
        if not libelle:
            # Appel direct, hors du chargement groupé : l'édition demandée
            # devient la référence de la phrase de comparaison.
            self._previous = dons
            self._previous_viewers = vues
            if vues:
                self._previous_peak_viewers = int(max(v for _t, v in vues))
        logger.info(
            "Édition %s : %d points de cagnotte, %d de viewers, total %.0f €",
            nom, len(dons), len(vues), dons[-1][1])
        return True

    @staticmethod
    def _lire_serie(section: object) -> list[tuple[float, float]]:
        """(labels, values) d'une section du graphe → points chronologiques.

        Les points aberrants sont écartés un par un : un timestamp absurde
        remonterait jusqu'à `datetime.fromtimestamp` dans un slot Qt sans
        try/except, et ferait tomber le panel.
        """
        if not isinstance(section, dict):
            return []
        labels = section.get("labels")
        valeurs = section.get("values")
        if not isinstance(labels, list) or not isinstance(valeurs, list):
            return []
        points = [p for p in (_sane_point(t, v)
                              for t, v in zip(labels, valeurs)) if p]
        points.sort(key=lambda p: p[0])
        return points

    async def load_historical_2026(self):
        """Charge l'historique depuis un dépôt GitHub Pages tiers.

        Les valeurs sont filtrées : un timestamp aberrant se propagerait jusqu'à
        datetime.fromtimestamp() dans un slot Qt sans try/except (crash du panel).
        """
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    "https://maniarr.github.io/cache.zevent.gdoc.fr/statistics/all.json"
                )
                r.raise_for_status()
                data = r.json()

            # CAGNOTTE — pools.large est en ordre DÉCROISSANT → inverser
            labels_ms = data["pools"]["large"]["labels"]  # ex: [1757286000000, ..., 1757089800000]
            values = data["pools"]["large"]["values"]
            # Zip puis inverser pour avoir ordre chronologique (ancien → récent)
            pairs = list(zip(labels_ms, values))
            pairs.reverse()
            # Tout-ou-rien : les listes sont construites EN LOCAL, et l'état
            # n'est remplacé qu'une fois le dépôt entièrement lu. Vider
            # self._donation avant de lire la section viewers laissait, si
            # celle-ci manquait, la courbe de l'édition PRÉCÉDENTE en place
            # avec self._previous vide : donation_rate() mesurait alors la
            # vitesse de l'édition passée en la présentant comme celle en cours.
            dons = [p for p in (_sane_point(ts, v) for ts, v in pairs) if p]

            # VIEWERS — viewers.large est en ordre CROISSANT → ne pas inverser
            v_labels_ms = data["viewers"]["large"]["labels"]  # ex: [1757088000000, ..., 1757286000000]
            v_values = data["viewers"]["large"]["values"]
            vues = [p for p in (_sane_point(ts, v)
                                for ts, v in zip(v_labels_ms, v_values)) if p]

            self._donation.clear()
            self._donation.extend(dons)
            self._viewers.clear()
            self._viewers.extend(vues)

            # Copie de la courbe précédente AVANT que les points en direct ne
            # s'y ajoutent : c'est elle qui servira de référence.
            self._previous = [(t, v) for t, v in self._donation]
            # Le préchargement remplace tout : les relevés en direct
            # recommencent après lui.
            self._live_depuis = None
            if self._viewers:
                # int() : _sane_point convertit tout en float, et la propriété
                # annonce un entier — un nombre de spectateurs n'est pas décimal.
                self._previous_peak_viewers = int(max(v for _, v in self._viewers))
            logger.info(
                "Édition précédente : %d points, total final %.0f €, pic %d viewers",
                len(self._previous),
                self._previous[-1][1] if self._previous else 0.0,
                self._previous_peak_viewers,
            )

            # VÉRIFICATION — logger les 3 premiers points pour confirmer
            if self._donation:
                from datetime import datetime
                ts0 = self._donation[0][0]
                ts1 = self._donation[-1][0]
                logger.info(f"Cagnotte: {datetime.fromtimestamp(ts0, tz=timezone.utc)} → {datetime.fromtimestamp(ts1, tz=timezone.utc)}")
            # Doit afficher: 2026-09-03 07:30:00+00:00 → 2026-09-05 18:00:00+00:00
            if self._viewers:
                ts0 = self._viewers[0][0]
                ts1 = self._viewers[-1][0]
                logger.info(f"Viewers: {datetime.fromtimestamp(ts0, tz=timezone.utc)} → {datetime.fromtimestamp(ts1, tz=timezone.utc)}")
            # Doit afficher: 2026-09-03 06:00:00+00:00 → 2026-09-05 18:00:00+00:00

        except Exception:
            logger.exception("Impossible de charger historique 2026")
