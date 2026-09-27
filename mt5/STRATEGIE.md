# Fiche stratégie — à remplir

Cette fiche sert à coder ta stratégie **de façon déterministe** dans `mt5/strategies/`. Plus elle est précise, moins j'aurai à interpréter. Si un champ ne s'applique pas, écris « aucun ». Un exemple entièrement rempli se trouve [en bas de page](#exemple-rempli--ema_cross) : c'est le niveau de précision attendu.

Rappels :

- La décision est prise **à la clôture d'une bougie** et exécutée au marché juste après (cycle horaire à hh:03 UTC). Formule donc tes conditions sur des bougies clôturées (« la clôture passe au-dessus de… », pas « le prix touche… »).
- Un SL et un TP sont **toujours** posés chez le broker à l'ouverture.
- Indicateurs disponibles sans développement supplémentaire : SMA, EMA, RSI (Wilder), ATR (Wilder), Bollinger, MACD, plus haut / plus bas sur N bougies, croisements. Autre chose : dis-le, je l'ajoute.

---

## 1. Compte

| Champ | Ta réponse |
|---|---|
| Login MT5 | `……………` (démo 10064731 recommandée) |
| Type | démo / live |
| Devise du compte | `USD` / … |
| Levier | `1:…` (lu via `get_account` si tu ne sais pas) |

## 2. Symbole(s)

| Symbole | Priorité si plusieurs | Remarques (devise de profit, digits) |
|---|---|---|
| `……………` | 1 | |
| `……………` | 2 | |

## 3. Timeframe

| Champ | Ta réponse |
|---|---|
| Timeframe des signaux | H1 / H4 / D1 (H1 minimum, cadence horaire) |
| Timeframe de filtre (optionnel) | par ex. tendance D1 pour filtrer des entrées H1, ou « aucun » |

## 4. Indicateurs

| Indicateur | Période(s) / paramètres | Source | Sert à |
|---|---|---|---|
| ex. EMA | 9 | close | signal rapide |
| ex. ATR | 14 | high/low/close | distance du SL |
| `……………` | | | |

## 5. Conditions d'entrée LONG

Toutes les conditions doivent être vraies **à la clôture de la même bougie** (sauf si tu précises « OU »).

1. `……………`
2. `……………`
3. `……………`

Exécution : ouverture au marché à la bougie suivante, au prix ask.

## 6. Conditions d'entrée SHORT

- Symétrique du long ? oui / non
- Sinon, liste :

1. `……………`
2. `……………`

## 7. Sortie / SL / TP

| Élément | Choix | Valeur |
|---|---|---|
| Stop loss | pips fixes / multiple d'ATR / plus bas-haut des N dernières bougies | `……………` |
| Take profit | pips fixes / ratio risque-rendement (RR) / multiple d'ATR / aucun (sortie discrétionnaire seulement) | `……………` |
| Trailing | aucun / suivi ATR / passage à breakeven après X R / plus bas-haut des N bougies | `……………` |
| Sortie discrétionnaire | signal inverse / RSI extrême / fin de tendance / aucune | `……………` |
| Sortie temporelle | fermer avant le week-end (vendredi HH UTC) / après N bougies / aucune | `……………` |

## 8. Taille de position

| Champ | Ta réponse |
|---|---|
| Méthode | risque en % de l'equity / lots fixes |
| Valeur | `…… %` ou `…… lots` |
| Lots max par position | `……` |
| Arrondi | au pas du symbole, vers le bas (par défaut) |

## 9. Filtres

| Filtre | Ta réponse |
|---|---|
| Heures UTC autorisées pour les entrées | `HH–HH` ou « toutes » |
| Jours autorisés | lun–ven / autre |
| Pas de nouvelle entrée le vendredi après | `HH` UTC (défaut 20) |
| Spread max (points) | `……` |
| News | pas de filtre automatique ; si tu veux exclure des plages (ex. NFP), donne-les en UTC et je les applique à la main dans la Routine |

## 10. Limites

| Limite | Ta réponse |
|---|---|
| Positions max par symbole | `1` (défaut) |
| Positions max au total | `……` |
| Perte max journalière (% equity, réalisée) | `……` (défaut 3) |
| Perte max hebdomadaire (% equity) | `……` — non implémentée dans le moteur v1 ; je peux l'ajouter ou l'appliquer via l'historique d'ordres à chaque cycle |
| Marge utilisée max (part de la marge libre) | `0,8` (défaut) |
| Que faire si une limite est atteinte | ne plus ouvrir jusqu'au lendemain / fermer aussi les positions ouvertes |

## 11. Autorisation d'exécution

Coche une seule option et recopie la phrase correspondante **dans la conversation** (la fiche seule ne suffit pas).

- [ ] **Démo — exécution automatique** :
  « J'autorise l'exécution automatique de la stratégie `<nom>` sur le compte démo `<login>`. »

- [ ] **Live — exécution automatique avec plafonds** :
  « J'autorise l'exécution automatique de la stratégie `<nom>` sur le compte live `<login>`, sur `<symboles>`, avec un risque max de `<x> %` par trade, `<n>` position(s) max, `<z>` lots max par position et une perte max journalière de `<y> %`. Je comprends que les pertes sont réelles. »

- [ ] **Signal seulement** : le moteur calcule `actions.json`, rien n'est exécuté, je te montre les actions et tu décides.

Sans phrase live, aucune action n'est exécutée sur un compte live, même si `actions.json` en propose.

---

## Exemple rempli — `ema_cross`

### 1. Compte

| Champ | Réponse |
|---|---|
| Login MT5 | 10064731 |
| Type | démo |
| Devise du compte | USD |
| Levier | 1:1000 (lu via `get_account`) |

### 2. Symbole(s)

| Symbole | Priorité | Remarques |
|---|---|---|
| EURUSD | 1 | 5 décimales, point 0,00001, devise de profit USD → taux 1,0 |

### 3. Timeframe

| Champ | Réponse |
|---|---|
| Timeframe des signaux | H1 |
| Timeframe de filtre | aucun |

### 4. Indicateurs

| Indicateur | Paramètres | Source | Sert à |
|---|---|---|---|
| EMA rapide | 9 | close | signal |
| EMA lente | 21 | close | signal |
| ATR | 14 (Wilder) | high/low/close | distance du SL |

### 5. Conditions d'entrée LONG

À la clôture de la bougie H1 `i` :

1. EMA(9) était **inférieure ou égale** à EMA(21) à la clôture de la bougie `i−1`.
2. EMA(9) est **strictement supérieure** à EMA(21) à la clôture de la bougie `i` (croisement haussier sur cette bougie exactement).
3. Aucune position ouverte sur EURUSD.

Exécution : achat au marché à l'ouverture de la bougie `i+1`, au prix ask.

### 6. Conditions d'entrée SHORT

Symétrique : EMA(9) passe **strictement en dessous** de EMA(21) sur la bougie `i` (elle était supérieure ou égale sur `i−1`), aucune position ouverte. Vente au marché à l'ouverture de `i+1`, au prix bid.

### 7. Sortie / SL / TP

| Élément | Choix | Valeur |
|---|---|---|
| Stop loss | multiple d'ATR | 1,5 × ATR(14) calculé à la clôture de `i`, placé sous (long) ou au-dessus (short) du prix d'entrée, arrondi à 5 décimales |
| Take profit | RR | 2,0 × la distance du SL, dans le sens du trade |
| Trailing | aucun | — |
| Sortie discrétionnaire | signal inverse | croisement opposé à la clôture d'une bougie → fermeture au marché à l'ouverture de la suivante |
| Sortie temporelle | aucune | la position peut passer le week-end, protégée par SL/TP |

### 8. Taille de position

| Champ | Réponse |
|---|---|
| Méthode | risque en % de l'equity |
| Valeur | 1 % |
| Lots max par position | 1,00 |
| Arrondi | vers le bas au pas 0,01 ; si le résultat est < 0,01 ou si la distance du SL est < stopsLevel × point, pas de trade |

Formule : `lots = (equity × 1 %) / (|entrée − SL| × 100 000 × 1,0)`. Exemple : equity 10 000 USD, ATR 0,00120 → SL à 0,00180 → risque 100 USD → 100 / (0,00180 × 100 000) = 0,55 lot.

### 9. Filtres

| Filtre | Réponse |
|---|---|
| Heures UTC autorisées pour les entrées | 7–21 |
| Jours autorisés | lun–ven |
| Pas de nouvelle entrée le vendredi après | 20 h UTC |
| Spread max | 15 points |
| News | aucun filtre |

### 10. Limites

| Limite | Réponse |
|---|---|
| Positions max par symbole | 1 |
| Positions max au total | 1 |
| Perte max journalière | 3 % de l'equity, sur les profits réalisés du jour (`fills.json`) |
| Perte max hebdomadaire | aucune |
| Marge utilisée max | 80 % de la marge libre |
| Si limite atteinte | plus aucune ouverture jusqu'au jour suivant ; les positions ouvertes gardent leur SL/TP |

### 11. Autorisation d'exécution

- [x] **Démo — exécution automatique** :
  « J'autorise l'exécution automatique de la stratégie `ema_cross` sur le compte démo 10064731. »

### Commandes correspondantes

```bash
# Backtest sur l'échantillon (validation du code, pas de la stratégie)
python3 -m mt5.engine.backtest --strategy ema_cross --candles mt5/data/EURUSD_H1_sample.json --symbol EURUSD --balance 10000 --risk-pct 1 --params fast=9,slow=21,atr_period=14,atr_mult=1.5,rr=2.0

# Cycle horaire (fichiers écrits par Claude depuis le connecteur Axi MT5)
python3 -m mt5.engine.cycle --strategy ema_cross --symbol EURUSD \
  --candles mt5/runtime/candles.json --positions mt5/runtime/positions.json \
  --account mt5/runtime/account.json --quote mt5/runtime/quote.json \
  --spec mt5/runtime/symbol.json --fills mt5/runtime/fills.json \
  --params fast=9,slow=21,atr_period=14,atr_mult=1.5,rr=2.0 \
  --risk-pct 1 --max-daily-loss-pct 3 --max-spread-points 15 --hours 7-21 \
  --out mt5/runtime/actions.json
```
