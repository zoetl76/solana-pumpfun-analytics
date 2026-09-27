# Backtest « NY Open 9:31 » — XAUUSD, avril → septembre 2026

Données : bougies M1 Axi (compte démo), 13:25→14:40 UTC de chaque séance, 127 séances (Vendredi saint absent). Simulateur : `mt5/engine/backtest_nyopen.py`, paramètres par défaut de la fiche (achat H + 0,10 + spread, vente L − 0,10, RR 2, 0,5 % de 10 000 USD, fenêtre 30 min, détention 60 min, latence OCO 60 s). Détail jour par jour : `nyopen_backtest_2026-04_09.json`.

## Résultat de base

| Mesure | Valeur |
|---|---|
| Séances / trades | 127 / 127 (déclenchement tous les jours) |
| Sorties | TP 39 · SL 86 · temps 1 · fin de données 1 |
| Taux de réussite | 31.5 % (40 gagnants / 87 perdants) |
| Total R | -9.43 R (moyenne -0.074 R par trade) |
| PnL net (hors slippage) | -475.87 USD, soit -4.76 % du compte |
| Facteur de profit | 0.89 |
| Drawdown max | 1,134.10 USD (11.19 %) |
| Pertes consécutives max | 10 |
| Amplitude bougie 9h31 | min 1.05 / médiane 4.27 / max 24.91 USD |
| Second ordre déclenché avant annulation | 23 jours, coût 53.92 USD |
| Jours ambigus (les 2 niveaux dans la même minute) | 7 |
| Délai médian jusqu'au TP | 8 min |

## Par mois

| Mois | Trades | Gagnants | R | PnL USD |
|---|---|---|---|---|
| 2026-04 | 21 | 5 | -6.2 | -305 |
| 2026-05 | 21 | 10 | +6.9 | +323 |
| 2026-06 | 22 | 5 | -7.4 | -369 |
| 2026-07 | 23 | 7 | -2.1 | -99 |
| 2026-08 | 21 | 4 | -8.5 | -408 |
| 2026-09 | 19 | 9 | +7.9 | +382 |

## Par sens

| Sens | Trades | Gagnants | R |
|---|---|---|---|
| buy | 65 | 18 | -12.9 |
| sell | 62 | 22 | +3.5 |

## Sensibilité (mêmes 127 séances, une variante à la fois)

| Variante | Total R | PnL USD | Facteur de profit | Drawdown max |
|---|---|---|---|---|
| Base (fiche) | -9.43 | -476 | 0.89 | 1 134 USD (11,2 %) |
| Décalages 0 / 0 | +2.43 | +80 | 1.02 | 589 USD (5,8 %) |
| Achat sans ajout du spread | -0.62 | -78 | 0.98 | 765 USD (7,5 %) |
| RR 1,5 | +1.07 | +10 | 1.00 | 811 USD (7,8 %) |
| RR 3 | +8.85 | +365 | 1.08 | 602 USD (5,9 %) |
| Détention max 30 min | -7.69 | -400 | 0.90 | 1 058 USD (10,4 %) |
| Fenêtre d'entrée 15 min | -9.47 | -477 | 0.89 | 1 135 USD (11,2 %) |
| Amplitude max 6 USD (25 jours sautés) | -10.09 | -497 | 0.86 | 988 USD (9,8 %) |
| Amplitude min 3 USD (26 jours sautés) | -9.86 | -501 | 0.85 | 1 065 USD (10,5 %) |
| Base + slippage 0,10 + spread +0,10 | -16.26 | -818 | 0.82 | — |

## Lecture honnête

- Sur 6 mois la stratégie telle que spécifiée est **légèrement perdante avant coûts** (−9,4 R, facteur 0,89) et **nettement perdante avec un slippage réaliste** (−16 R). Le taux de réussite de 31,5 % est juste sous le seuil d'équilibre d'un RR 2 (33,3 %).
- L'écart-type attendu de la somme de 127 trades à ±1 R / +2 R est d'environ 15 R : aucune variante (ni la base, ni RR 3 à +8,9 R) ne sort du bruit statistique. Ne pas choisir un paramètre parce qu'il « gagne » sur cet échantillon.
- Les mois alternent fortement (+6,9 R en mai, −8,5 R en août) et les achats sont bien plus faibles que les ventes (−12,9 R contre +3,5 R) ; là aussi l'échantillon est trop petit pour conclure à un biais structurel.
- Le second ordre part avant l'annulation manuelle 23 jours sur 127 (18 %) : le coût direct est faible (54 USD au total) parce que la seconde position est fermée immédiatement, mais cela reste la principale fragilité d'exécution du modèle sans OCO natif.
- 6 déclenchement(s) après 9h34 seulement : l'entrée se fait presque toujours dans les deux premières minutes, d'où la faible sensibilité à la fenêtre d'entrée.
- Non modélisé : slippage réel des stops (souvent supérieur à 0,10 USD sur l'or à l'ouverture), requotes, variation du spread à la seconde, jours de publications macro.
