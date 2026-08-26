// SPDX-License-Identifier: GPL-3.0-or-later
// ZLink — panel ZEvent. Copyright (C) 2026 Fabien MILLET.
//
// Le peu de code que demandent les quatre panneaux de réglages.
//
// Le logiciel Elgato ouvre la page dans une vue web et appelle la fonction
// globale `connectElgatoStreamDeckSocket` — c'est lui qui décide du nom, on
// ne peut pas le changer. Le reste tient en deux gestes : lire les réglages
// de la touche à l'ouverture, les renvoyer à chaque modification.
//
// Aucune bibliothèque n'est chargée. Le panneau doit s'afficher sans réseau :
// les composants officiels viennent d'un CDN, et un Stream Deck hors ligne
// afficherait alors une page vide.

"use strict";

let socket = null;
let contexte = "";
let reglages = {};
const auDemarrer = [];

// eslint-disable-next-line no-unused-vars
function connectElgatoStreamDeckSocket(port, uuid, evenement, info, actionInfo) {
  contexte = uuid;
  try {
    reglages = (JSON.parse(actionInfo).payload || {}).settings || {};
  } catch (erreur) {
    reglages = {};
  }
  socket = new WebSocket("ws://127.0.0.1:" + port);
  socket.onopen = function () {
    socket.send(JSON.stringify({ event: evenement, uuid: uuid }));
    auDemarrer.forEach(function (f) { f(reglages); });
  };
}

/** Retient un réglage et le transmet. Sans envoi, rien n'est gardé. */
function enregistrer(cle, valeur) {
  reglages[cle] = valeur;
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({
      event: "setSettings", context: contexte, payload: reglages,
    }));
  }
}

/** À exécuter quand les réglages de la touche sont connus. */
function auChargement(f) {
  auDemarrer.push(f);
}

/**
 * Relie un <select> ou un <input> à un réglage.
 *
 * `defaut` est celui qu'applique le plugin quand la touche n'a rien : le
 * panneau doit montrer la même chose, sinon on croit avoir choisi autre chose
 * que ce qui se passe.
 */
function relier(id, cle, defaut, transforme) {
  const champ = document.getElementById(id);
  auChargement(function (r) {
    const valeur = r[cle] === undefined ? defaut : r[cle];
    if (champ.type === "checkbox") {
      champ.checked = Boolean(valeur);
    } else {
      champ.value = String(valeur);
    }
  });
  champ.addEventListener("change", function () {
    const brut = champ.type === "checkbox" ? champ.checked : champ.value;
    enregistrer(cle, transforme ? transforme(brut) : brut);
  });
}
