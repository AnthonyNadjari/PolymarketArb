# Polymarket BTC 5-min Up/Down : test de l'idée de Matt

Date : 11 octobre 2026. Données : tous les marchés `btc-updown-5m-*` résolus du 14 août (passage au TWAP 60 s) au 11 octobre 2026.

| | |
|---|---|
| Marchés résolus | 16 932 |
| Trades | 25.5 M |
| Taux de "Up" | 51 % |
| Frais taker (V2) | 0.07 × p(1−p) par share, soit 1.75c à 50c, 0.6c à 90c |
| Source BTC | Binance BTCUSDT klines 1 s (data.binance.vision) |

## Question posée

Un modèle de proba maison (distance au prix de départ ÷ vol × √temps restant) diverge-t-il assez du prix Polymarket pour qu'on gagne en pariant la différence, frais inclus ?

## Réponse courte

Non. Le marché est calibré à moins d'un point partout, l'écart est inférieur aux frais, et le modèle n'apporte aucune information que le prix ne contient déjà. Le seul "edge" qu'un backtest naïf fait apparaître est un artefact d'horodatage.

## Test 1 : le marché se trompe-t-il ? (calibration)

Pour chaque trade, prix payé du token Up contre résultat réel. Erreurs-types calculées par bootstrap sur les marchés (les trades d'un même marché partagent le résultat).

| Prix payé | Fréquence réelle Up | Écart | Frais | Edge net meilleur côté |
|---|---|---|---|---|
| 4c | 4.1 % | 0.0 | 0.3c | −0.3c |
| 15c | 14.8 % | 0.0 | 0.9c | −0.8c |
| 25c | 25.0 % | +0.1 | 1.3c | −1.2c |
| 35c | 34.3 % | −0.6 | 1.6c | −1.0c |
| 45c | 43.9 % | −0.9 | 1.7c | −0.8c |
| 55c | 54.5 % | −0.1 | 1.7c | −1.7c |
| 65c | 64.6 % | −0.3 | 1.6c | −1.3c |
| 75c | 74.6 % | −0.3 | 1.3c | −1.1c |
| 85c | 85.9 % | +0.9 | 0.9c | 0.0c |
| 96c | 96.2 % | +0.3 | 0.3c | 0.0c |

Aucune tranche n'est exploitable en taker. Par temps restant (6 tranches × 10 prix = 60 cellules), 3 cellules sortent à |z| > 2.5, ce qui est à peine plus que le hasard, et leur edge net est de 0.7 à 1.2c, soit moins qu'un tick de spread.

## Test 2 : le modèle fait-il mieux que le marché ?

Modèle : P(Up) = Φ(d / σ√τ) adapté au règlement TWAP (la dernière minute est une moyenne, donc une partie du prix final est déjà connue). Vol réalisée sur 1 h de rendements 1 s (la meilleure fenêtre testée parmi 1, 5, 15, 30, 60 min). Prix de départ K = moyenne Binance des 60 s avant l'ouverture. Le proxy Binance reproduit le résultat Chainlink dans 97.5 % des marchés.

Régression logistique du résultat sur logit(prix marché) + logit(modèle), entraînée sur la première moitié des marchés, évaluée sur la seconde :

| Décalage Binance | Coef. marché | Coef. modèle | Log-loss marché seul | Log-loss marché + modèle |
|---|---|---|---|---|
| 0 s (triché) | 0.89 | 0.13 | 0.3958 | 0.3947 |
| 5 s (honnête) | 1.00 | 0.04 | 0.3958 | 0.3957 |
| 10 s | 1.02 | 0.03 | 0.3958 | 0.3957 |

Avec un décalage honnête, le modèle ne change rien. Le value betting simulé (parier quand modèle et marché divergent de plus de 3 ou 5 points) donne 0 ± 2.7c par share, avant même le spread.

## Le piège : les timestamps

Les timestamps du Data API Polymarket sont ceux de l'inscription on-chain, pas du match. Corrélation entre variation du prix Polymarket à t et variation Binance à t−lag : pic net à **lag = +4 s** (corr 0.245 sur 16 397 marchés, nulle au-delà de 6 s).

Conséquence : si on utilise le prix Binance "au timestamp du trade", le modèle voit 4 secondes dans le futur. Résultat du value betting selon le décalage appliqué (un pari par marché, prix du trade, frais inclus, sans spread) :

| Décalage | PnL/share seuil 5 pts | PnL/share seuil 20 pts |
|---|---|---|
| 0 s | +5.1c | +21.4c |
| 3 s | +4.1c | +14.7c |
| 5 s | +2.9c | +2.1c |
| 10 s | +2.0c | −0.8c |
| 30 s | +0.3c | −4.3c |

Le résidu de 2c à 5-10 s au seuil de 5 points est inférieur au spread et provient des mêmes cellules de fin de fenêtre que le test 3 : l'analyse incrémentale montre qu'il ne survit pas à une évaluation hors échantillon.

Un backtest naïf affiche donc un edge énorme et imaginaire. Toute étude de ces marchés doit décaler le prix spot de 5 s au moins, ou utiliser le flux WebSocket en direct pour horodater les matchs.

## Test 3 : biais de fin de fenêtre

Hypothèse : dans les dernières secondes, les favoris sont sous-cotés (biais favori-outsider). Sur un échantillon partiel de 5 000 marchés, les favoris à 80-90c dans les 30 dernières secondes gagnaient 90.5 % du temps au lieu de 85.6 %. Sur le jeu complet l'écart tombe à 88.0 % contre 85.6 % (z = 1.7, non significatif), soit +1.5c net de frais et avant spread. Régression vers la moyenne classique d'une cellule sélectionnée après coup.

Version "un pari par marché" (premier trade à moins de 30 s avec un favori entre 75 et 92c) :

| | |
|---|---|
| Marchés concernés | 1 873 (11 % du total) |
| Taux de gain du favori | 86.8 % |
| Prix moyen payé | 84.8c |
| PnL net de frais, par share | +1.2c ± 0.8c |
| Avec 1 tick de slippage | +0.2c |

Le signe change une semaine sur deux. Rien d'exploitable.

## Test 4 : économie du maker (sur l'historique complet)

Le maker est la contrepartie de chaque trade. Son PnL brut à l'échéance est l'opposé de celui du taker. Pondéré par le volume, erreurs par marché. Rebate maker : 20 % des frais taker (catégorie Crypto, docs Polymarket).

| | Par share |
|---|---|
| PnL brut maker | −0.05c ± 0.03c |
| Frais taker moyen | 0.98c |
| Rebate maker (20 %) | +0.20c |
| **PnL net maker** | **+0.15c** |
| PnL net taker | −0.93c |

Le maker moyen gagne donc 0.15c par share, soit 0.3 % du notionnel à 50c. Sur 740 M de shares en deux mois, ça fait ~$1.1 M répartis entre tous les makers. Polymarket garde ~$5.8 M.

Mais le détail par taille de trade adverse est éclairant :

| Taille du trade taker | Part du volume | PnL brut maker | PnL net maker (rebate 20 %) |
|---|---|---|---|
| ≤ 20 shares | 18 % | −0.51c | −0.27c |
| 20-100 | 34 % | −0.63c | −0.41c |
| 100-500 | 29 % | +0.20c | +0.39c |
| 500-2000 | 14 % | +1.16c | +1.30c |
| > 2000 | 5 % | +0.61c | +0.65c |

Les petits ordres rapides (74 % des trades, typiques des bots de latence) sont informés : le maker perd contre eux. Les gros ordres (gamblers, ordres "all-in") sont non informés : le maker gagne. Face aux takers qui vendent (sorties, paniques), le maker gagne +0.79c brut.

Carnet en direct (1 h d'enregistrement, 11 oct. 20:00 UTC) : spread à 1 tick dans 97 % des snapshots, profondeur médiane 150-200 shares au meilleur prix. Dans 4 % des snapshots, acheter Up et Down aux asks coûte moins de $1 (minimum 0.93) : arbitrage gratuit, capté par les bots les plus rapides.

**Lecture.** Le maker moyen gagne presque rien car il subit la sélection adverse des bots. Un maker qui saurait éviter ces fills (retirer ses cotes dès que le spot bouge, les reposer après) capterait l'essentiel du flux non informé, soit l'ordre de 1c par share. C'est faisable mais c'est une course de vitesse, et le prix d'entrée est un bot avec flux Binance et WebSocket Polymarket, pas une formule.

## Ce que ça veut dire

- **La stratégie telle que formulée ne gagne pas en taker.** Le marché est efficace au point près, ce qui est cohérent avec l'analyse Bitquery (la foule est break-even avant frais).
- **Le vrai coût est frais + spread**, environ 2.5 à 3c par share à 50c. Aucun écart de calibration n'atteint ce seuil.
- **Si on continue, c'est côté maker** : zéro frais, rebates, et c'est là que va le spread payé par la foule. Le modèle sert alors à poser des quotes et à se retirer quand la vol saute, pas à "battre" le prix. Ça demande une collecte du carnet en direct (WebSocket), non disponible en historique.
- **Prérequis pratique** : accès à Polymarket depuis la France à vérifier.

## Fichiers

Tout est dans le scratchpad `pm/` :

- `collect.py` : collecte Gamma + Data API (résumable).
- `analyze.py` : chargement et calibration brute. `calib_boot.py` : calibration avec erreurs par marché.
- `model.py` : modèle TWAP-aware (option `--lag`). `lag.py` : test de look-ahead et lead-lag. `incremental.py` : apport du modèle au marché. `endgame.py` : favoris en fin de fenêtre.
- `data/twap_era/`, `data/last3d/` : marchés et trades (CSV). `btc_raw/` : klines Binance 1 s.
- `out_*_full.txt` : sorties complètes.
