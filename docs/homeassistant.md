# ZLink → Home Assistant

Faire clignoter un éclairage quand la cagnotte franchit un palier, qu'une grosse
donation tombe, qu'un objectif est sur le point d'être atteint, ou qu'une chaîne
s'emballe.

```
  ZLink  ──POST──▶  webhook Home Assistant  ──▶  votre automatisation
```

ZLink envoie **un message par événement**, et rien de plus. Ce que les lampes en
font vous appartient : c'est l'automatisation qui décide de la couleur, de la
durée et des pièces concernées.

Le clignotement n'est volontairement pas fait par ZLink. Vingt requêtes en dix
secondes, c'est vingt occasions de rater la dernière — une coupure au milieu et
les lampes restent éteintes. Un seul envoi, et Home Assistant tient la
séquence, y compris le retour à l'état d'avant.

---

## 1. Créer le webhook

Dans Home Assistant : **Paramètres → Automatisations et scènes → Créer une
automatisation → Déclencheur → Webhook**. Selon la version, il donne l'URL
entière ou le seul identifiant :

```
https://homeassistant.local:8123/api/webhook/-XyZ123abcDEF456
-XyZ123abcDEF456
```

Recopiez ce que vous avez dans ZLink : **Paramètres → Home Assistant**, champ
*URL ou ID du webhook*. Les deux formes sont acceptées.

Le champ *Adresse de Home Assistant* ne sert qu'au second cas, pour
reconstituer `<adresse>/api/webhook/<id>` — il se grise tout seul dès qu'une
URL entière est collée au-dessus. L'adresse retenue s'affiche sous les champs ;
*Envoyer un essai* vérifie qu'elle répond.

> L'identifiant du webhook **tient lieu de mot de passe** : qui le connaît peut
> déclencher l'automatisation. Ne le publiez pas, et laissez `local_only: true`
> si ZLink et Home Assistant sont sur le même réseau.

---

## 2. Ce que ZLink envoie

Toujours un objet JSON, dont le champ `type` dit la famille :

```json
{"type": "palier",   "montant": 1000000.0, "libelle": "1 M€"}
{"type": "don",      "login": "zerator", "streamer": "ZeratoR",
                     "montant": 500.0, "nature": "bombardement"}
{"type": "objectif", "login": "aypierre", "streamer": "Aypierre",
                     "objectif": "Explications du BattleBoost", "reste": 320.0}
{"type": "hype",     "login": "mistermv", "libelle": "Moment drôle 💀",
                     "score": 0.82, "couleur": "#a855f7", "extrait": "« lul » ×34"}
```

Le bouton *Envoyer un essai* produit `{"type": "essai", …}` : de quoi mettre au
point l'automatisation sans attendre un vrai palier.

Chaque famille se coupe séparément dans les réglages de ZLink.

---

## 3. Passer à l'éditeur YAML

L'écran d'automatisation montre trois blocs — **Quand**, **Et si**, **Alors
faire** — et le déclencheur Webhook est déjà dans le premier. Le reste
s'écrirait à la souris, mais dix clignotements et une sauvegarde d'éclairage
prennent une demi-heure en cliquant, et trente secondes en collant.

En haut à droite de la page, le menu **⋮ → « Modifier en YAML »**.

> Attention : il y a **deux** menus ⋮. Celui de la carte « Lorsqu'une charge
> utile de Webhook a été reçue » ne modifie que le déclencheur. Celui qu'il
> faut est tout en haut de la page, sur la barre de titre.

Vous obtenez un éditeur de texte contenant à peu près ceci :

```yaml
alias: Nouvelle automatisation
description: ""
triggers:
  - trigger: webhook
    allowed_methods: [POST]
    local_only: true
    webhook_id: -s8cw0QbBg5c-PEloLqHvyV-o
conditions: []
actions: []
mode: single
```

Notez votre `webhook_id` : c'est lui qui va dans ZLink.

> **Deux orthographes coexistent.** Home Assistant 2024.10 a renommé
> `trigger:` → `triggers:`, `condition:` → `conditions:`, `action:` →
> `actions:` et `service:` → `action:`. Les anciennes restent acceptées ; les
> exemples ci-dessous utilisent les nouvelles. Si votre éditeur affiche
> `trigger:` au singulier, gardez sa forme à lui et adaptez : coller les deux
> orthographes dans la même automatisation ne marche pas.

---

## 4. Une première version qui marche tout de suite

**Remplacez tout le contenu de l'éditeur** par ceci, en gardant VOTRE
`webhook_id` et en mettant VOS lampes à la place de `light.remplacez_moi` :

```yaml
alias: ZLink
description: Fait clignoter l'éclairage aux événements du ZEvent
triggers:
  - trigger: webhook
    allowed_methods: [POST]
    local_only: true
    webhook_id: COLLEZ_ICI_VOTRE_ID
conditions: []
actions:
  # L'éclairage d'avant est photographié : sans cela, la fin du clignotement
  # laisserait les lampes éteintes jusqu'au lendemain.
  - action: scene.create
    data:
      scene_id: zlink_avant
      snapshot_entities:
        - light.remplacez_moi
  - repeat:
      count: 10                       # 10 × (0,5 s + 0,5 s) = 10 secondes
      sequence:
        - action: light.turn_on
          target: {entity_id: light.remplacez_moi}
          data: {brightness: 255, rgb_color: [0, 255, 135], transition: 0}
        - delay: {milliseconds: 500}
        - action: light.turn_off
          target: {entity_id: light.remplacez_moi}
          data: {transition: 0}
        - delay: {milliseconds: 500}
  - action: scene.turn_on
    target: {entity_id: scene.zlink_avant}
mode: single
```

**Enregistrer**, puis dans ZLink : *Paramètres → Home Assistant → Envoyer un
essai*. Les lampes doivent clignoter dix secondes puis revenir comme avant.

> **« scene.zlink_avant : entité non trouvée »**, en rouge, est normal. Cette
> scène n'est pas un objet qu'on déclare : `scene.create` la fabrique au
> moment où l'automatisation s'exécute, pour photographier l'éclairage avant
> de le faire clignoter. Elle n'existe donc pas tant que rien ne s'est
> déclenché, et l'éditeur — qui valide les entités contre l'état courant — le
> signale. L'avertissement disparaît après le premier déclenchement, et il
> n'empêche ni l'enregistrement ni l'exécution.

`conditions: []` est volontaire : à ce stade **tout** déclenche le
clignotement, y compris l'essai. C'est ce qui permet de vérifier la chaîne
entière avant d'y ajouter quoi que ce soit.

> `mode: single` fait ignorer un second événement arrivé pendant les dix
> secondes. Avec `mode: queued` et `max: 5`, ils s'enchaîneraient — au risque
> de cinquante secondes de clignotement si la cagnotte s'emballe.

---

## 5. Ne réagir qu'à certains événements

Une fois que ça clignote, remplacez `conditions: []` par le filtre voulu :

```yaml
conditions:
  - condition: template
    value_template: "{{ trigger.json.type == 'palier' }}"
```

L'essai de ZLink envoie `type: "essai"` : il ne déclenchera donc plus rien.
Pour continuer à l'utiliser pendant vos réglages :

```yaml
    value_template: "{{ trigger.json.type in ['palier', 'essai'] }}"
```

### Une couleur par palier

Le montant est dans le message. De quoi passer du vert à l'or quand la
cagnotte devient sérieuse — remplacez la ligne `rgb_color` du bloc `repeat` :

```yaml
        - action: light.turn_on
          target: {entity_id: light.remplacez_moi}
          data:
            brightness: 255
            transition: 0
            rgb_color: >
              {% set m = trigger.json.montant | float(0) %}
              {% if m >= 10000000 %}[255, 215, 0]
              {% elif m >= 5000000 %}[255, 120, 0]
              {% else %}[0, 255, 135]{% endif %}
```

### Une couleur par type d'événement

Plutôt que quatre automatisations, une seule qui choisit — les `actions`
deviennent :

```yaml
actions:
  - choose:
      - conditions: "{{ trigger.json.type == 'palier' }}"
        sequence:
          - action: script.zlink_clignoter
            data: {couleur: [0, 255, 135], tours: 10}
      - conditions: "{{ trigger.json.type == 'don' }}"
        sequence:
          - action: script.zlink_clignoter
            data: {couleur: [245, 197, 24], tours: 4}
      - conditions: "{{ trigger.json.type == 'hype' }}"
        sequence:
          - action: script.zlink_clignoter
            data: {couleur: [168, 85, 247], tours: 3}
```

(`script.zlink_clignoter` est un script à créer une fois, reprenant le
`repeat` ci-dessus avec `couleur` et `tours` en variables.)

---

## Quand rien ne s'allume

* **L'essai dit « Injoignable »** — l'adresse ou le port sont faux, ou Home
  Assistant n'est pas joignable depuis cette machine. `homeassistant.local` ne
  se résout pas partout : essayez l'adresse IP.
* **« 403 — refusé AVANT Home Assistant »** — Home Assistant répond `200` à un
  webhook, **même inconnu**, délibérément, pour ne pas révéler lesquels
  existent. Un code d'erreur vient donc de ce qui est devant lui. Deux causes,
  dans l'ordre de fréquence :
  * **le déclencheur est en `local_only: true`** et vous passez par un domaine
    public : la requête arrive de l'extérieur et se fait refuser. Utilisez
    l'adresse locale de la box, ou passez à `local_only: false` — en sachant
    qu'alors l'identifiant du webhook devient un secret exposé à Internet ;
  * **un proxy ou Cloudflare filtre** la requête.
* **L'essai réussit (200) mais rien ne bouge** — c'est le cas le plus
  fréquent, et le plus déroutant : `200` ne prouve QUE l'acheminement. Home
  Assistant répond 200 à un webhook inconnu, et aussi quand il écarte la
  requête. Dans l'ordre :
  1. **`local_only`** — si vous joignez la box par un domaine public, un
     déclencheur en `local_only: true` est écarté **en répondant 200**. ZLink
     règle ce champ tout seul d'après l'adresse configurée : si vous avez
     collé le YAML avant de saisir l'adresse, recopiez-le.
  2. **L'automatisation est-elle enregistrée et activée**, et son `webhook_id`
     est-il bien celui de ZLink ?
  3. **La condition** — si vous avez déjà fait l'étape 5, elle rejette
     `type: "essai"`.
* **Vérifier que le message arrive** — dans Home Assistant, ouvrez
  l'automatisation, menu **⋮ → « Traces »**. Chaque déclenchement y figure avec
  la charge utile reçue. Aucune trace : le message n'est pas arrivé, le
  problème est entre ZLink et la box. Une trace mais pas de lumière : c'est
  une condition ou une action.
* **Ça clignote puis reste éteint** — l'étape `scene.turn_on` finale manque, ou
  `snapshot_entities` ne liste pas les mêmes lampes que les actions.
* **L'automatisation se déclenche (une trace existe) mais rien ne s'allume** —
  c'est le nom des lampes. `light.remplacez_moi` est un marqueur, pas une
  entité. Le vrai nom se trouve dans **Outils de développement → États**,
  colonne « Entité », en filtrant sur `light.` ; l'onglet « Chronologie de
  l'exécution » de la trace montre l'échec sur l'entité.
* **« Entité non trouvée » sur `scene.zlink_avant`** — normal avant le premier
  déclenchement, voir l'étape 4.
* **Rien pendant l'event** — les familles se coupent une par une dans
  *Paramètres → Home Assistant*. Un palier n'arrive par ailleurs qu'une fois franchi
  **sous les yeux de ZLink** : au lancement, ceux de la journée sont déjà
  passés et ne sont pas rejoués.

Côté ZLink, le journal porte les lignes `core.domotique` : chaque envoi, et
chaque échec avec sa raison.
