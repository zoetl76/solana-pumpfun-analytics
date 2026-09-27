# MT5 — moteur de stratégie (Axi MT5 via connecteur)

Package Python autonome (bibliothèque standard uniquement, Python 3.11) qui prend des décisions de trading à partir de fichiers JSON et produit des actions JSON. Il ne parle jamais au réseau : c'est moi (Claude) qui lis MT5 via le connecteur Axi, qui écris les fichiers, qui lance le moteur, puis qui exécute les actions via le connecteur.

Sommaire :

1. [État de l'installation MT5](#état-de-linstallation-mt5)
2. [Comment la stratégie tourne 24/5](#comment-la-stratégie-tourne-245)
3. [Garde-fous](#garde-fous)
4. [Utilisation](#utilisation)
5. [Limites honnêtes](#limites-honnêtes)
6. [Ce qu'il me faut de toi](#ce-quil-me-faut-de-toi)

---

## État de l'installation MT5

**Le terminal MetaTrader 5 (Windows) n'est pas installé ici, et ne peut pas l'être.**

- Les domaines de téléchargement (`download.mql5.com`, `metatrader5.com`) sont bloqués par la politique réseau de l'environnement.
- Même si le téléchargement passait, le conteneur cloud est éphémère : il est recréé à chaque session. Un terminal MT5 (ou un Expert Advisor) ne pourrait donc pas tourner 24/5 dedans.

**En revanche, l'accès à MT5 est déjà opérationnel** via le connecteur **Axi MT5**, que j'appelle directement : liste des comptes, état du compte, cotations, bougies, positions ouvertes, ouverture / fermeture / modification de positions, ordres en attente, historique des exécutions.

### Comptes MT5 détectés

| Type | Login | Levier | Solde |
|---|---|---|---|
| MT5 Live | **60342696** | 1:1000 | lu à chaque cycle via `get_account` |
| MT5 Live | **60342697** | 1:1000 | lu à chaque cycle via `get_account` |
| MT5 Demo | **10064731** | lu via `get_account` | 10 000 USD |
| MT5 Demo | **10064773** | lu via `get_account` | 100 000 USD |

Le connecteur expose aussi un compte « Axi Trading Platform » (live 194618, demo 304502) et un Axi Crypto Wallet : ce ne sont pas des comptes MT5, ils sont hors périmètre.

**Recommandation : démarrer sur la démo 10 000 USD (login 10064731).** C'est le compte le plus proche d'un petit compte réel, ce qui rend le calcul des lots (0,01 minimum) et les garde-fous représentatifs. On passe en live uniquement après une période de démo concluante et une autorisation écrite de ta part (voir [Garde-fous](#garde-fous)).

---

## Comment la stratégie tourne 24/5

Il n'y a pas de processus qui tourne en continu. À la place, une **Routine planifiée** réveille une session Claude neuve à cadence fixe, typiquement **toutes les heures à hh:03 UTC** (3 minutes après l'heure pour que la bougie H1 soit bien clôturée côté Axi). Chaque session fait exactement ceci :

1. `get_accounts` → repère le compte MT5 cible (par login).
2. `get_account` → sauvegarde `account.json` (equity, marge libre, levier, devise).
3. `get_mt5_positions` → sauvegarde `positions.json` (positions ouvertes, SL/TP, heure d'ouverture).
4. `get_mt5_candles` → sauvegarde `candles.json` (dernières bougies clôturées, assez pour couvrir la période de chauffe des indicateurs).
5. `get_mt5_quote` → sauvegarde `quote.json` (bid/ask, session ouverte ou non, mode de trading).
6. `get_mt5_available_markets(symbol)` → sauvegarde `symbol.json` (digits, taille de contrat, lots min/max/pas, stopsLevel).
7. Optionnel : `get_mt5_fills` (deals du jour) → sauvegarde `fills.json`, pour calculer la perte réalisée du jour.
8. Lance `python3 -m mt5.engine.cycle ...` (voir [Utilisation](#utilisation)) → `actions.json`.
9. Exécute chaque action émise via `open_mt5_position` / `close_mt5_position` / `modify_mt5_position`.
10. Ajoute une ligne de journal (décision, actions exécutées, blocages, avertissements).

Les fichiers JSON de travail vont dans `mt5/runtime/` et les journaux dans `mt5/logs/` (tous deux ignorés par git).

### Limitation de cadence

La cadence standard minimale d'une Routine est **horaire**. Conséquences :

- Les stratégies en **H1, H4 ou D1** sont un choix naturel : la décision se prend à la clôture de la bougie, et la Routine passe juste après.
- Une stratégie **M5 / M15** n'est pas exécutable correctement dans ce modèle (on louperait la plupart des signaux). Pour ça il faudrait un vrai terminal MT5 sur un VPS avec un Expert Advisor — ce qui n'est pas possible ici.
- Entre deux cycles, personne ne surveille le marché : c'est le **SL/TP côté broker**, toujours posés à l'ouverture, qui protègent la position.

### Règle d'absence d'état (stateless)

Chaque session part d'un clone tout frais du dépôt : le moteur **n'a aucune mémoire** entre deux cycles. Tout l'état est reconstruit à partir de `positions.json` + `candles.json` (+ `fills.json`).

Protection contre la double entrée : un signal de la dernière bougie clôturée est considéré **déjà consommé** s'il existe une position ouverte sur le symbole dont `timeCreate` est supérieur ou égal à l'heure d'ouverture de cette bougie. Avec une position ouverte, le moteur n'émet de toute façon jamais une deuxième ouverture (`max_positions_per_symbol = 1` par défaut).

---

## Garde-fous

Le moteur applique ces règles **avant** d'émettre une action `open`. Chaque règle qui bloque est listée dans `blocked` (avec la règle et le détail), et le code de sortie reste 0 : « décision calculée, rien à faire » n'est pas une erreur.

| Paramètre (`RiskConfig`) | Défaut | Rôle |
|---|---|---|
| `risk_pct_per_trade` | 1,0 % | Part de l'equity risquée entre l'entrée et le SL |
| `max_positions_per_symbol` | 1 | Une seule position par symbole |
| `max_daily_loss_pct` | 3,0 % | Perte réalisée du jour (via `fills.json`) au-delà de laquelle plus aucune ouverture |
| `max_spread_points` | aucun | Spread courant max, en points |
| `trading_hours_utc` | aucun | Fenêtre `(heure_début, heure_fin)` UTC pour les nouvelles entrées |
| `no_new_trades_friday_after_hour_utc` | 20 | Pas de nouvelle entrée le vendredi après 20 h UTC (le marché FX ferme vers 20:58 UTC) |
| `margin_usage_cap` | 0,8 | La marge estimée de l'ordre ne doit pas dépasser 80 % de la marge libre |
| `profit_ccy_to_account_rate` | 1,0 | Taux devise de profit → devise du compte (1,0 pour EURUSD sur compte USD) |

Règles vérifiées à chaque cycle :

- **Perte journalière** : somme des profits réalisés du jour dans `fills.json` ; si la perte dépasse `max_daily_loss_pct` de l'equity → blocage.
- **Positions max** par symbole.
- **Spread** courant (issu de `quote.json`) au-dessus de `max_spread_points` → blocage.
- **Heures de trading** hors fenêtre → blocage.
- **Coupure du vendredi**.
- **Mode de trading du symbole** : `longonly` bloque les ventes, `shortonly` bloque les achats, `closeonly` et `disabled` bloquent toute ouverture (les fermetures restent permises en `closeonly`).
- **Session fermée** (`tradeSessionOpen: false`) → aucune ouverture ; une action `close` peut quand même être émise, elle sera exécutée à la réouverture.
- **Marge** : approximation `lots × contractSize × prix / levier` comparée à `margin_free × margin_usage_cap`.
- **Distance du SL** : si la distance entrée–SL est nulle, absente ou inférieure à `stopsLevel × point`, le nombre de lots est 0 et rien n'est ouvert.
- **Une seule ouverture maximum par cycle**, quoi qu'il arrive.

**Le moteur ne place jamais d'ordre lui-même.** Il écrit `actions.json`, c'est tout. L'exécution est faite par moi, via le connecteur, en suivant ces actions.

**Sur un compte LIVE, rien n'est exécuté sans ton autorisation explicite et écrite dans la conversation**, avec des plafonds convenus (risque par trade, perte max journalière, lots max, symboles). Sans cette phrase, je m'arrête à l'affichage de `actions.json`. La formulation attendue est dans `STRATEGIE.md`, section « Autorisation d'exécution ».

---

## Utilisation

Toutes les commandes se lancent depuis la racine du dépôt.

### Lancer les tests

```bash
cd /home/user/solana-pumpfun-analytics && python3 -m unittest discover -s mt5/tests -t . -v
```

### Backtest

```bash
python3 -m mt5.engine.backtest --strategy ema_cross --candles mt5/data/EURUSD_H1_sample.json --symbol EURUSD --balance 10000 [--risk-pct 1] [--params fast=9,slow=21] [--json out.json] [--spec symbol.json]
```

- Sans `--spec`, une spécification par défaut « FX 5 décimales type EURUSD » est utilisée (digits 5, contractSize 100 000, lots min 0,01 / pas 0,01 / max 100, stopsLevel 1).
- Un tableau récapitulatif en français est affiché : nombre de trades, gagnants/perdants, taux de réussite, profit brut, perte brute, PnL net, profit factor, drawdown max (absolu et %), gain moyen, perte moyenne, espérance, equity finale.
- `--json out.json` écrit en plus les statistiques, la courbe d'equity et la liste des trades (heure/prix d'entrée et de sortie, lots, PnL, raison).

Règles de remplissage du backtest : le signal est calculé à la clôture de la bougie `i`, l'entrée est faite à l'**ouverture de la bougie `i+1`** (achat au `open + spread`, vente au `open`). SL/TP sont vérifiés en intrabar sur le high/low. Si SL et TP sont touchés dans la même bougie, **le SL est retenu** (hypothèse prudente).

### Cycle de décision (live, sans réseau)

```bash
python3 -m mt5.engine.cycle --strategy ema_cross --symbol EURUSD --candles candles.json --positions positions.json --account account.json --quote quote.json --spec symbol.json [--fills fills.json] [--params k=v,...] [--risk-pct 1] [--max-daily-loss-pct 3] [--max-spread-points 15] [--hours 7-21] [--out actions.json]
```

Comportement :

- Le contexte est construit sur la **dernière bougie clôturée** de `candles.json`.
- **Position ouverte sur le symbole** → `should_exit()` est évalué → action `close` si sortie ; sinon `manage()` → action `modify` si le SL ou le TP bouge d'au moins 1 point.
- **Aucune position** → `on_bar()` → tous les garde-fous → action `open` avec `volumeLots` calculé par `compute_lots` et SL/TP arrondis aux digits du symbole.
- Règle stateless de ré-entrée appliquée (voir plus haut).
- Code de sortie 0 dès que la décision est calculée (même si tout est bloqué) ; non nul uniquement si une entrée est invalide (fichier illisible, bougies mal ordonnées, etc.).

### Schéma de `actions.json`

```json
{
  "schemaVersion": 1,
  "asOf": "2026-09-25T20:00:00+00:00",
  "symbol": "EURUSD",
  "strategy": "ema_cross",
  "account": {"equity": 10000.0, "currency": "USD"},
  "actions": [
    {"type": "open",   "direction": "buy", "volumeLots": 0.1, "priceSL": 1.13000, "priceTP": 1.15000, "reason": "..."},
    {"type": "close",  "positionId": 123, "reason": "..."},
    {"type": "modify", "positionId": 123, "priceSL": 1.13500, "priceTP": 1.15000, "reason": "..."}
  ],
  "blocked":  [{"rule": "max_daily_loss", "detail": "..."}],
  "warnings": ["..."],
  "lastCandle": {"time": "...", "open": 0, "high": 0, "low": 0, "close": 0, "tickVolume": 0, "volume": 0, "spread": 0}
}
```

- `asOf` : heure de clôture de la dernière bougie utilisée.
- `actions` : jamais plus d'un `open`. Les trois types ci-dessus sont montrés ensemble pour l'exemple ; en pratique un cycle produit au plus une ou deux actions.
- `blocked` : chaque garde-fou déclenché, avec sa règle (`max_daily_loss`, `max_positions`, `spread`, `trading_hours`, `friday_cutoff`, `trade_mode`, `session_closed`, `margin`, …).
- `warnings` : anomalies non bloquantes (par ex. entrées de `fills.json` illisibles).

### Syntaxe des paramètres

`--params` prend une liste `clé=valeur` séparée par des virgules, sans espaces : `--params fast=9,slow=21,atr_mult=1.5`. Les clés sont celles du dictionnaire `params` de la stratégie ; toute clé absente garde sa valeur par défaut.

### Ajouter une stratégie

1. Copie `mt5/strategies/template.py` vers `mt5/strategies/ma_strategie.py`. Le fichier est abondamment commenté et montre chaque point d'accroche :
   - `name` et `params` (valeurs par défaut) ;
   - `warmup_bars` (nombre de bougies nécessaires avant le premier signal) ;
   - `on_bar(ctx)` → renvoie un `Signal(side, sl, tp, reason)` ou `None` ;
   - `should_exit(ctx)` → renvoie un `ExitSignal(reason)` ou `None` ;
   - `manage(ctx)` (optionnel) → renvoie `{"sl": ..., "tp": ...}` pour un trailing.
2. Code les conditions à partir de `mt5/engine/indicators.py` (`sma`, `ema`, `rsi`, `atr`, `bollinger`, `macd`, `highest`, `lowest`, `crossover`, `crossunder`). Les listes renvoyées sont alignées sur les bougies, avec `None` pendant la chauffe.
3. Enregistre-la dans `mt5/strategies/registry.py`. Le nom de la stratégie est le nom du module (`ma_strategie`).
4. Backteste sur des bougies récupérées via le connecteur, puis ajoute un test dans `mt5/tests/` si tu veux la figer.

Stratégies fournies : `ema_cross` (croisement EMA 9/21, SL = 1,5 × ATR(14), TP = 2 × distance du SL, sortie sur croisement inverse) et `template` (squelette, ne trade pas).

---

## Limites honnêtes

- **Marge approximée** : `lots × contractSize × prix / levier` n'est exact que pour une paire cotée dans la devise du compte (EURUSD sur compte USD). Pour les autres, c'est une approximation, d'où le plafond de 80 % de la marge libre.
- **Pas de swap ni de commission dans le backtest.** Sur des positions tenues plusieurs jours, le swap réel pèse.
- **Règle « SL d'abord »** : quand SL et TP sont touchés dans la même bougie, on compte une perte. C'est prudent, donc le backtest sous-estime probablement un peu la performance.
- **Spread** : le champ `spread` d'une bougie MT5 est le spread **minimum** observé sur la bougie. Le coût réel à l'exécution est donc en général un peu plus élevé que dans le backtest.
- **Cadence horaire** : entre deux cycles, seuls les SL/TP posés chez le broker agissent. C'est pourquoi le moteur pose **toujours** un SL et un TP à l'ouverture. Un trailing ou une sortie discrétionnaire ne se met à jour qu'une fois par heure.
- **Pas de données tick** : tout est calculé sur des bougies clôturées. Les prix d'exécution réels (slippage, requotes) diffèrent.
- **Taux de change** : `profit_ccy_to_account_rate` est fixe (1,0 par défaut). Pour une paire dont la devise de profit n'est pas celle du compte, il faut me donner le taux ou je le lirai via `get_mt5_quote` à chaque cycle.
- **L'échantillon de 45 bougies** (`mt5/data/EURUSD_H1_sample.json`) sert à valider le code, **pas** à évaluer une stratégie. Un vrai backtest demande plusieurs mois de bougies, à récupérer via le connecteur.
- **Le journal n'est pas une source de vérité** : chaque session part d'un clone frais. Ce qui fait foi, ce sont les positions et l'historique d'exécutions du broker.
- **Une Routine peut sauter un cycle** (indisponibilité, connecteur en erreur). Le moteur étant stateless, le cycle suivant repart proprement, mais le signal d'une bougie sautée est perdu.

---

## Ce qu'il me faut de toi

Remplis `mt5/STRATEGIE.md` (un exemple complet y figure) ou donne-moi les mêmes infos en texte libre :

- [ ] **Compte** : login MT5 à utiliser (démo 10064731 recommandée pour commencer).
- [ ] **Stratégie** : indicateurs avec périodes, conditions d'entrée long et short, conditions de sortie — assez précis pour être codé sans interprétation.
- [ ] **Timeframe** : H1 minimum (H4 / D1 possibles).
- [ ] **Symbole(s)** : EURUSD ou autres, avec leur ordre de priorité si plusieurs.
- [ ] **Risque par trade** : % de l'equity (ou lots fixes) et lots max.
- [ ] **Perte max journalière** : en % de l'equity.
- [ ] **Heures de trading** : fenêtre UTC, jours, coupure du vendredi, spread max.
- [ ] **Autorisation d'exécution** :
  - démo : une phrase du type « J'autorise l'exécution automatique sur le compte démo 10064731 » ;
  - live : la phrase complète avec plafonds, telle que définie dans `STRATEGIE.md`. Sans elle, aucune action live n'est exécutée.
