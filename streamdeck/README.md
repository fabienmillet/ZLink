# ZLink sur Stream Deck

Piloter la régie depuis le boîtier : choisir le flux affiché en grand, déclencher
les gestes du plein écran, régler le son aux molettes.

```
  Windows, macOS   Stream Deck  ⇄  zlink-deck.exe  ⇄  ZLink (127.0.0.1:8730)
  Linux            Stream Deck  ⇄  ZLink
```

Deux chemins, parce que le boîtier n'appartient pas au même monde des deux
côtés. Sous Windows et macOS, le logiciel d'Elgato l'ouvre en exclusif : pour y
écrire, il faut être une extension qu'il lance lui-même, d'où l'exécutable et
le WebSocket. Ce logiciel n'existe pas sous Linux — le boîtier y est un simple
périphérique HID que personne ne réclame, et ZLink l'ouvre directement.

Ni l'un ni l'autre ne décide de rien. Une touche pressée devient une commande,
un état reçu devient une image ; ce qui se passe à l'écran reste écrit dans
ZLink, où c'est déjà testé.

---

## Linux : rien à installer

Il n'y a ni extension à poser, ni exécutable à construire, ni jeton, ni profil
à importer. On branche le boîtier, on lance ZLink, les touches s'allument.

`core/streamdeck_direct.py` énumère les Stream Deck branchés au démarrage et
les pilote en direct. Il porte les mêmes signaux que la télécommande
WebSocket : `main.py` branche l'un ou l'autre sur les mêmes fenêtres, sans
savoir par où l'ordre est arrivé.

### Ce qu'il faut sur la machine

**hidapi**, qui est un paquet système et non Python :

```bash
sudo pacman -S hidapi          # Arch
sudo apt install libhidapi-hidraw0   # Debian, Ubuntu
sudo dnf install hidapi        # Fedora
```

**Et la règle udev**, sans laquelle `/dev/hidraw*` appartient à root seul :

```bash
sudo cp packaging/70-zlink-streamdeck.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

puis **rebrancher le boîtier** — une règle ne s'applique qu'aux périphériques
branchés après son chargement. C'est le seul obstacle sérieux de
l'installation, et le journal le dit en toutes lettres quand il se présente :

```
Stream Deck : Stream Deck + inaccessible (…). Sous Linux, poser la règle
udev 70-zlink-streamdeck.rules puis rebrancher le boîtier
```

Rien de tout cela n'est fatal. Sans hidapi, sans la règle, ou sans boîtier,
ZLink le note dans son journal et tourne exactement comme avant.

### La disposition suit le modèle

Sans logiciel Elgato, il n'y a pas de profil à importer : les touches sont
posées par le code, et c'est le matériel qui décide de quoi. Ce sont les deux
profils décrits plus bas, rendus à leur destination — un test les compare à
ceux que `gen_profils.py` écrit, pour qu'un utilisateur qui passe de Windows à
Linux retrouve ses touches au même endroit.

| Boîtier | Ce qu'il reçoit |
|---|---|
| **avec molettes** (Stream Deck +) | La régie : Chat, Don, Clip, Revoir, Muet, Favori, Précédent, Suivant — et le mixage aux molettes |
| **sans molette** (Original, MK.2, XL) | La grille : les chaînes, et les deux dernières touches pour paginer |
| **six touches ou moins** (Mini) | La grille entière, sans pagination : deux flèches y coûteraient plus de chaînes qu'elles n'en donneraient |

L'écran du Stream Deck + est composé par ZLink lui-même — le logiciel Elgato le
découpait en quatre cases à partir d'un `setFeedback`, ici c'est une seule
image de 800 × 100 à peindre. On y touche une case pour couper sa piste.

### Si un autre logiciel pilote déjà le boîtier

ZLink n'est pas seul à savoir le faire sous Linux, et deux programmes qui
écrivent sur le même Stream Deck se le disputent image par image. Poser dans
`config.json` :

```json
{"streamdeck": {"direct": false}}
```

---

## Installation (Windows, macOS)

Tout ce qui suit ne concerne que les systèmes où le logiciel Elgato tourne.
Sous Linux, voir la section précédente : il n'y a rien à installer.

**Depuis ZLink** — c'est la voie normale, et elle ne demande rien d'autre :

> Paramètres → **Stream Deck** → *Installer l'extension*, puis quitter et
> relancer le logiciel Stream Deck.

L'extension est livrée avec ZLink. Le bouton la copie dans le dossier des
plugins d'Elgato et dit ce qu'il en est : logiciel absent, extension déjà
installée, version plus récente disponible. Il n'y a ni jeton à recopier, ni
dossier à trouver.

**Depuis les sources**, l'exécutable n'existe pas encore — il est produit à la
publication, pas versionné. Pour l'obtenir :

```bash
py -3.12 -m venv streamdeck/.venv
streamdeck/.venv/Scripts/pip install -r streamdeck/requirements.txt pyinstaller
streamdeck/.venv/Scripts/python streamdeck/construire.py --installer
```

`construire.py` produit `zlink-deck.exe`, le dépose dans le dossier de
l'extension — d'où ZLink le prendra — et écrit
`streamdeck/dist/zlink-deck.streamDeckPlugin`. `--installer` copie en plus
directement chez Elgato.

Rien à saisir nulle part : le plugin lit le jeton dans
`%APPDATA%\ZLink\remote.json`, que ZLink écrit à chaque démarrage. Il faut donc
avoir lancé ZLink **au moins une fois** avant que les touches s'allument.

---

## Les quatre actions

| Action | Contrôleur | Réglage | Ce qu'elle fait |
|---|---|---|---|
| **Flux** | touche | rang de la cellule | Porte l'avatar et l'audience d'une chaîne de la grille ; l'appui la passe en plein écran |
| **Action** | touche | geste | Clip, revoir 30 s, chat, don, favori, muet — sur la chaîne au plein écran |
| **Navigation** | touche | sens | Flux précédent / suivant, ou page précédente / suivante des touches Flux |
| **Mixage** | molette | piste | Tourner règle le volume (5 points par cran), appuyer coupe le son |

Chaque geste porte son propre dessin : le point rouge de l'enregistrement, la
flèche de retour, la bulle du chat, le cœur du don, l'étoile du favori, le
haut-parleur. Six touches frappées du même éclair ne se distingueraient plus
une fois posées côte à côte.

**Muet**, **Chat** et **Favori** ont DEUX dessins, et montrent où l'on en est :
haut-parleur avec ou sans ondes, bulle pleine ou en contour, étoile pleine ou
creuse. Une touche qui n'affiche que le geste ne dit pas s'il est déjà fait —
on appuie pour voir, et on découvre en défaisant.

Une molette porte l'avatar de la chaîne qu'elle règle, et se ternit quand la
piste est coupée : sa valeur affiche « Muet » et sa barre tombe à zéro, parce
qu'elle montre ce qu'on ENTEND, pas le réglage gardé pour le retour du son.

### Deux profils tout faits

Plutôt que poser vingt touches une à une, `streamdeck/com.zlink.deck.sdPlugin/profils/`
contient les deux dispositions prêtes. Un double-clic sur le fichier l'importe ;
depuis ZLink, *Paramètres → Stream Deck → Ouvrir les profils* montre le dossier.

| Profil | Appareil | Contenu |
|---|---|---|
| **ZLink — Grille** | Stream Deck 5×3 | 13 touches *Flux* + « Page précédente » et « Page suivante » |
| **ZLink — Régie** | Stream Deck + | Chat, Don, Clip, Revoir / Muet, Favori, Précédent, Suivant + 4 molettes de mixage |

Les touches Flux posées forment une page : le plugin compte celles qui
existent, et les flèches décalent l'ensemble. Trente chaînes tiennent ainsi sur
treize touches.

Un profil ne s'installe pas comme l'extension. Le logiciel Stream Deck réécrit
ses profils en se fermant : un fichier déposé dans son dossier pendant qu'il
tourne disparaîtrait à sa sortie. D'où l'import, qui est son geste à lui.

Ils se régénèrent par `python streamdeck/gen_profils.py`. Le format n'est pas
deviné : il reproduit celui que le logiciel écrit dans `ProfilesV3`, relevé sur
une installation réelle. Aucun numéro de série n'y figure — celui d'un profil
exporté lierait le fichier à une seule machine, et le publierait.

---

## Ce que ZLink expose

`core/remote_api.py` ouvre un WebSocket **sur 127.0.0.1 uniquement**, jamais sur
une adresse joignable depuis le réseau : une télécommande qui coupe le son et
change de flux n'a rien à faire sur un réseau de LAN party.

La première trame d'un client doit porter le jeton, sinon la connexion est
fermée. Le local n'est pas une frontière — n'importe quel programme de la
machine peut ouvrir une connexion sur la boucle locale.

ZLink pousse son état à chaque changement visible sur les touches (cellules,
chaîne au plein écran, volumes) :

```json
{"type": "etat", "actif": "zerator", "volume": 42, "muet": false,
 "cellules": [{"login": "zerator", "viewers": 45500, "online": true,
               "epingle": false, "avatar": "https://…", "volume": 100,
               "muet": false}]}
```

Les commandes acceptées : `slot`, `voisin`, `chaine`, `action`, `volume`,
`muet`, `volume_chaine`, `muet_chaine`.

L'avatar circule comme URL, jamais comme image : le plugin la télécharge une
fois et garde le résultat. Réémettre quelques dizaines de kilo-octets par
chaîne à chaque changement d'audience n'apporterait rien.

---

## Quand rien ne s'allume

Le plugin écrit à côté de son exécutable, dans
`%APPDATA%\Elgato\StreamDeck\Plugins\com.zlink.deck.sdPlugin\zlink-deck.log` :

```
enregistre aupres du Stream Deck
jeton lu dans C:\Users\…\AppData\Roaming\ZLink\remote.json (port 8730)
connecte a ZLink
```

Ces trois lignes réunies, la chaîne est complète. Sinon :

* **« aucun jeton trouvé »** — ZLink n'a jamais tourné sur cette machine.
* **« ZLink injoignable »** — ZLink est fermé. Le plugin réessaie toutes les
  trois secondes, indéfiniment : le Stream Deck démarre avec la machine, pas
  ZLink, et abandonner à la première tentative laisserait des touches mortes
  jusqu'au prochain redémarrage du logiciel Elgato.
* **rien du tout** — le logiciel Elgato n'a pas lancé le plugin : vérifier que
  `zlink-deck.exe` est bien dans le dossier installé, et relancer le logiciel.

Côté ZLink, `journal` porte les lignes `core.remote_api` : l'écoute, chaque
client authentifié, et tout jeton refusé.
