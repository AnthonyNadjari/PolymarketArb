# Maker papier : Polymarket "Bitcoin Up or Down - 5 min"

Simulation de market making en direct, sans argent réel. Le bot écoute le carnet et les trades Polymarket
(WebSocket) et le prix BTC Coinbase, affiche des cotes simulées des deux côtés du jeton Up, et compte un
fill seulement quand un vrai trade passe à notre prix pour la part qui dépasse la quantité déjà affichée
(on se suppose dernier dans la file). Positions gardées jusqu'à l'échéance et réglées avec le résultat
officiel (Gamma API). Une stratégie témoin identique mais qui ne retire jamais ses cotes tourne en parallèle.

## Suivi en direct

Le bot tourne sur GitHub Actions (workflow `paper-bot.yml`, tranches de 6 h, relancé par cron) et publie son état
toutes les 5 minutes sur la branche `gh-pages`, servie par GitHub Pages :

**https://anthonynadjari.github.io/PolymarketArb/**

La page se rafraîchit seule. L'heure de la dernière donnée est affichée en haut à droite. Pour relancer le bot à la
main : onglet Actions du repo, workflow paper-bot, "Run workflow".

## Lancer chez toi

```bash
pip install requests websockets
python mm_paper.py            # dashboard sur http://localhost:8787/
```

Options utiles :

```
--size 50        shares par cote (défaut 50)
--max-inv 200    position nette max par marché
--pull-move 0.0001   retire les cotes si |mouvement BTC sur 2 s| > 1 bp
--pull-secs 3    durée du retrait
--pull-ticks 2   retire aussi si le mid Polymarket bouge de 2 ticks en 2 s
--no-quote-last 10   pas de cotes dans les 10 dernières secondes (cotes limitées à 5c-95c)
--bankroll 1000  capital fictif pour le % de rendement
--state state.json   fichier d'état (lu par le dashboard toutes les 3 s)
```

Laisse tourner au moins deux semaines pour avoir un chiffre fiable : la plupart des marchés ne donnent aucun
fill avec l'hypothèse prudente, et le PnL par marché est très bruité.

## Lire les chiffres

- **Argent net** = gains à l'échéance − coût des positions + rebate maker (20 % des frais taker, estimation).
- **Fills toxiques** = part des fills où le mid a bougé de 2c ou plus contre nous dans les 10 s suivantes.
  C'est la sélection adverse des bots rapides, la raison principale pour laquelle le maker moyen gagne ~0.
- **Témoin sans esquive** : même cotes, jamais retirées. L'écart avec la stratégie esquive mesure la valeur
  du retrait des cotes sur mouvement rapide du BTC.

## Limites de la simulation

- On ne connaît pas notre vraie place dans la file ; l'hypothèse "dernier" est prudente, parfois trop.
- Latence réelle ignorée : un vrai bot a 150 ms de délai taker imposé et quelques dizaines de ms de réseau.
- Rebate : la répartition exacte entre makers n'est pas publique ; 20 % des frais sur nos fills est une approximation.
- Résultat officiel : Chainlink TWAP 60 s. Le bot utilise Gamma pour le résultat, Coinbase seulement comme repli provisoire.
- Polymarket est restreint en France : la simulation n'en dépend pas, un compte réel oui.

## Fichiers de l'étude historique

`collect.py` (collecte), `analyze.py` / `calib_boot.py` (calibration), `model.py` / `lag.py` / `incremental.py`
(modèle et test de look-ahead), `endgame.py` (fin de fenêtre), `maker.py` (économie du maker), `RESULTATS.md` (compte rendu).
