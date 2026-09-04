# SPDX-License-Identifier: GPL-3.0-or-later
# ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
# Distribué sans AUCUNE GARANTIE, selon les termes de la GNU General Public
# License version 3 ou ultérieure. Voir le fichier LICENSE.
"""Cagnotte en TEMPS RÉEL, par le flux de marentdev.eu.

Le relais HTTP voisin (`cagnotte_marentdev`) relève un rapport toutes les
trente secondes. C'est peu coûteux, mais ce n'est pas du direct : une grosse
donation met une demi-minute à apparaître. Le site publie à côté un flux
WebSocket qui pousse chaque don à la seconde, et la cagnotte avec.

**Pourquoi un navigateur pour lire un socket.** `wss://.../api/flux/socket` est
derrière un challenge Cloudflare : la poignée de main revient en 403 pour tout
client qui n'est pas un navigateur. Rejouer un cookie de clearance récolté
ailleurs serait un contournement, et un contournement fragile.

On fait donc l'inverse : on EST un navigateur. QtWebEngine — déjà embarqué pour
le chat Twitch — charge la page publique du site, franchit le challenge comme
n'importe quel visiteur, puis c'est du JavaScript servi par cette même origine
qui ouvre le socket. Rien n'est contourné : le challenge est résolu, pas évité.

**Ce qui circule.** Trois formes, relevées sur le flux réel :

    {"type":"snapshot","donations":[…]}          à la connexion
    {"type":"donation","donation":{…}}           à chaque don
    {"type":"amount","amount":880074.03}         la cagnotte

Le `snapshot` est de l'HISTORIQUE — les derniers dons déjà passés. Il est lu
pour rien d'autre que sa présence : le republier ferait sonner toutes les
alertes de ZLink d'un coup à chaque reconnexion.

**Le coût.** Un moteur web vivant en permanence, pour un compteur. C'est le
prix du challenge, et c'est pour cela que ce flux est OPTIONNEL : sans lui, le
relais HTTP prend le relais, et sans lui non plus, la cagnotte officielle.
"""

from __future__ import annotations

import json
import logging

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal

logger = logging.getLogger(__name__)

try:
    from PyQt6.QtWebEngineCore import QWebEnginePage as _QWebEnginePage
    WEBENGINE_OK: bool = True
except ImportError:
    _QWebEnginePage = None  # type: ignore[assignment]
    WEBENGINE_OK = False


#: La page publique du site. On la charge pour son ORIGINE, pas pour son
#: contenu : c'est elle qui porte le challenge résolu et le cookie qui va avec.
PAGE_URL = "https://zevent.marentdev.eu/"

#: Le flux, tel que le site l'expose.
SOCKET_URL = "wss://zevent.marentdev.eu/api/flux/socket"

#: Cadence de vidage de la file JavaScript. Ce n'est PAS la latence du flux :
#: les messages arrivent quand ils arrivent, ce timer ne fait que les
#: transporter du navigateur vers Qt. Une demi-seconde est indiscernable du
#: direct pour un compteur, et ne coûte qu'un `runJavaScript` par tour.
_VIDAGE_MS = 500

#: Plafond de la file côté JavaScript. Si Qt cessait de vider — fenêtre gelée,
#: page rechargée — la file grossirait sans fin dans le navigateur.
_FILE_MAX = 200

#: Dons rapatriés par tour. Au-delà, le reste attend le tour suivant : un pic
#: de donations ne doit pas faire traverser un tableau géant à chaque vidage.
_LOT_MAX = 100


def _js_ouverture(socket_url: str) -> str:
    """Le script qui ouvre le flux et le met en file, DANS la page.

    Réentrant : injecté deux fois, il ne rouvre rien. C'est nécessaire, la
    page peut être rechargée sous nous.
    """
    return """
(function () {
  if (window.__zlinkFlux) return "deja";
  var etat = {file: [], total: null, ouvert: false, snapshots: 0};
  window.__zlinkFlux = etat;
  var sock = null, tentatives = 0;

  function empiler(don) {
    etat.file.push(don);
    if (etat.file.length > %(file_max)d) {
      etat.file.splice(0, etat.file.length - %(file_max)d);
    }
  }

  function reprogrammer() {
    tentatives += 1;
    // Recul exponentiel plafonné : un serveur qui tombe ne doit pas se faire
    // marteler par un onglet qui reconnecte en boucle.
    var attente = Math.min(30000, 1000 * Math.pow(2, tentatives));
    setTimeout(ouvrir, attente);
  }

  function ouvrir() {
    try { sock = new WebSocket(%(url)s); }
    catch (err) { reprogrammer(); return; }
    sock.onopen = function () { etat.ouvert = true; tentatives = 0; };
    sock.onclose = function () { etat.ouvert = false; reprogrammer(); };
    sock.onerror = function () { try { sock.close(); } catch (e) {} };
    sock.onmessage = function (ev) {
      var m;
      try { m = JSON.parse(ev.data); } catch (err) { return; }
      if (!m || !m.type) return;
      if (m.type === "amount") {
        if (typeof m.amount === "number") etat.total = m.amount;
      } else if (m.type === "donation") {
        if (m.donation) empiler(m.donation);
      } else if (m.type === "snapshot") {
        // Historique : compté, jamais republié. Voir le module.
        etat.snapshots += 1;
      }
    };
  }

  ouvrir();
  return "lance";
})();
""" % {"url": json.dumps(socket_url), "file_max": _FILE_MAX}


#: Vide la file et rend l'état, en une seule traversée.
_JS_VIDAGE = """
(function () {
  var e = window.__zlinkFlux;
  if (!e) return "{}";
  return JSON.stringify({
    ouvert: e.ouvert, total: e.total, dons: e.file.splice(0, %(lot)d)
  });
})();
""" % {"lot": _LOT_MAX}


class FluxCagnotte(QObject):
    """Cagnotte et dons poussés en direct, via une page QtWebEngine invisible.

    La page n'est jamais montrée et n'a pas de vue : `QWebEnginePage` seule
    charge, exécute le JavaScript et tient le socket. Un `QWebEngineView`
    ajouterait un widget et son rendu, pour rien.

    Sans PyQt6-WebEngine, l'objet se construit quand même et ne fait rien —
    `disponible` vaut False et aucun signal n'est jamais émis. C'est une source
    d'appoint, son absence ne doit rien casser.
    """

    #: Nouvelle cagnotte totale, en euros.
    cagnotte_changee = pyqtSignal(float)
    #: Un don, tel que le flux l'annonce (dict brut). Jamais ceux du snapshot.
    don_recu = pyqtSignal(object)
    #: Le socket vient de s'ouvrir (True) ou de tomber (False).
    etat_change = pyqtSignal(bool)

    def __init__(self, parent: QObject | None = None, *,
                 page_url: str = PAGE_URL,
                 socket_url: str = SOCKET_URL) -> None:
        super().__init__(parent)
        self._page_url = page_url
        self._socket_url = socket_url
        self._page = None
        self._total: float | None = None
        self._ouvert: bool = False
        self._timer = QTimer(self)
        self._timer.setInterval(_VIDAGE_MS)
        self._timer.timeout.connect(self._vider)

    @property
    def disponible(self) -> bool:
        """QtWebEngine est-il là ? Sans lui, cet objet est inerte."""
        return WEBENGINE_OK

    @property
    def total(self) -> float | None:
        """Dernière cagnotte reçue, ou None si le flux n'a rien dit."""
        return self._total

    @property
    def ouvert(self) -> bool:
        return self._ouvert

    def demarrer(self) -> bool:
        """Charge la page et ouvre le flux. Rend False si c'est impossible."""
        if not WEBENGINE_OK or _QWebEnginePage is None:
            logger.info("Flux cagnotte : PyQt6-WebEngine absent — relais HTTP seul")
            return False
        if self._page is not None:
            return True
        self._page = _SilencieusePage(self)
        # L'injection est refaite à CHAQUE chargement, pas seulement au
        # premier : le challenge Cloudflare provoque lui-même une navigation,
        # et la page qui suit ne connaît plus notre script.
        self._page.loadFinished.connect(self._sur_chargement)
        self._page.load(QUrl(self._page_url))
        self._timer.start()
        logger.info("Flux cagnotte : page %s en chargement", self._page_url)
        return True

    def arreter(self) -> None:
        """Ferme le flux et libère le moteur web."""
        self._timer.stop()
        if self._page is not None:
            self._page.deleteLater()
            self._page = None
        if self._ouvert:
            self._ouvert = False
            self.etat_change.emit(False)

    def _sur_chargement(self, ok: bool) -> None:
        if not ok or self._page is None:
            logger.warning("Flux cagnotte : chargement de %s échoué", self._page_url)
            return
        self._page.runJavaScript(_js_ouverture(self._socket_url))

    def _vider(self) -> None:
        """Rapatrie ce que le navigateur a mis en file depuis le tour d'avant."""
        if self._page is None:
            return
        self._page.runJavaScript(_JS_VIDAGE, self._appliquer)

    def _appliquer(self, brut: object) -> None:
        """Traite le résultat du vidage. Ne lève jamais : c'est un rappel Qt."""
        try:
            etat = json.loads(brut) if isinstance(brut, str) else {}
        except ValueError:
            return
        if not isinstance(etat, dict) or not etat:
            return

        ouvert = bool(etat.get("ouvert"))
        if ouvert != self._ouvert:
            self._ouvert = ouvert
            logger.info("Flux cagnotte : socket %s",
                        "ouvert" if ouvert else "fermé")
            self.etat_change.emit(ouvert)

        total = etat.get("total")
        if isinstance(total, (int, float)) and total > 0:
            valeur = float(total)
            # Émis seulement quand il CHANGE : le flux répète le total à
            # chaque don, et vingt émissions par seconde repeindraient le
            # panel pour rien.
            if valeur != self._total:
                self._total = valeur
                self.cagnotte_changee.emit(valeur)

        for don in etat.get("dons") or []:
            if isinstance(don, dict):
                self.don_recu.emit(don)


if WEBENGINE_OK and _QWebEnginePage is not None:
    class _SilencieusePage(_QWebEnginePage):  # type: ignore[misc]
        """Page qui n'écrit pas dans le journal de ZLink.

        Le site charge Chart.js et ses plugins depuis un CDN ; leurs avis de
        console n'ont rien à faire dans les traces d'un panneau de régie.
        """

        def javaScriptConsoleMessage(self, level, message, line, source) -> None:  # type: ignore[override]
            pass
else:                                   # pragma: no cover - dépend de l'install
    _SilencieusePage = None             # type: ignore[assignment]
