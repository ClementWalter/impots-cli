# impots-cli

**Visibility: public.**

**Extension: not shipped** as a Claude Desktop extension: it drives a dedicated
debug Chrome window that the user signs into by hand.

Ton **espace particulier impots.gouv.fr** depuis le terminal :

- lister et télécharger **tous les documents** (avis d'impôt, déclarations, taxes
  foncières et d'habitation, relances…) de toutes les années, en PDF ;
- lire la **messagerie sécurisée** : fils de discussion, pièces jointes, accusés de
  réception ;
- **écrire** une nouvelle demande avec pièces jointes (simulation sans `--yes`).

Il n'existe pas d'API officielle pour les particuliers : l'« API Impôt particulier »
de la DGFiP est réservée aux administrations habilitées.

## Pourquoi ça passe par Chrome

Le login (`cfspart-idp.impots.gouv.fr`) est derrière un bot manager Radware, qui
répond par un challenge JavaScript à tout client HTTP qui n'est pas un navigateur.
La messagerie est en plus une application JSF/PrimeFaces à état. La CLI pilote donc
un **Chrome dédié** via le DevTools Protocol : les PDF sont récupérés par `fetch`
dans un onglet connecté, et la messagerie est pilotée par son vrai DOM.

Le Chrome dédié (profil `~/.impots-chrome`, port 9224) démarre tout seul. Il garde
les cookies et l'appareil de confiance d'un lancement à l'autre.

## Installation

```bash
git clone https://github.com/ClementWalter/impots-cli ~/Documents/impots-cli
uv run ~/Documents/claudine/scripts/link_clis.py   # met `impots` sur le PATH
impots login                                        # connecte-toi dans la fenêtre
```

Il faut [uv](https://docs.astral.sh/uv/) et Google Chrome.

## Utilisation

```bash
impots whoami
impots documents --year 2026
impots download --all                    # ~/Downloads/impots/<année>/<Libellé>.pdf, idempotent
impots messages
impots message 1234567890 --save
impots topics
impots send --topic 14 --body-file demande.txt --attach piece.pdf         # simulation
impots send --topic 14 --body-file demande.txt --attach piece.pdf --yes   # envoi réel
```

Voir [SKILL.md](SKILL.md) pour le détail des commandes et les pièges connus.

## Tests

```bash
uv run --script tests/test_impots_cli.py
```

Tests hors ligne, sur du HTML synthétique qui reproduit la structure des vraies pages.
