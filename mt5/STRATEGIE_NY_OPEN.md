# Fiche stratégie — « NY Open 9:31 » sur l'or (XAUUSD)

Fiche remplie à partir de ta description, complétée par les réglages validés (« Oui lance », 27/09/2026).
Code : `mt5/strategies/ny_open_931.py` · backtest : `mt5/engine/backtest_nyopen.py` · live : `mt5/engine/nyopen_cycle.py`.

| Champ | Valeur |
|---|---|
| **Compte** | Axi MT5 **démo**, login **10064731** (10 000 USD, levier 1:1000). Aucune exécution live sans nouvelle autorisation écrite. |
| **Symbole** | `XAUUSD` (or, 2 décimales, contrat 100 oz, lot mini 0,01, maxi 20, stopsLevel 1 point) |
| **Timeframe** | M1, une seule bougie de référence par jour |
| **Bougie de référence** | celle qui ouvre à **9h31 New York** (`America/New_York`, heure d'été/hiver gérée) : 13:31 UTC l'été, 14:31 UTC l'hiver |
| **Ordres à 9h32** | `buy_stop` à **H + 0,10 $ + spread** (un buy stop se déclenche sur le ask, les bougies sont en bid) ; `sell_stop` à **L − 0,10 $** |
| **Stop de protection** | achat : SL = L ; vente : SL = H |
| **Objectif** | 2 × la distance de risque (RR 2) |
| **Expiration des ordres** | 30 min après 9h32 (10h02 NY), posée côté broker (`timeType specified`) |
| **Limite de détention** | position fermée au marché 60 min après son ouverture |
| **Un seul trade par jour** | dès qu'un ordre est déclenché, l'autre est annulé (OCO manuel, ~45 s de latence) ; si le second part quand même, la seconde position est **fermée immédiatement** |
| **Taille** | 0,5 % de l'équité entre l'entrée et le SL, arrondi au 0,01 lot inférieur (≈ 0,08 à 0,23 lot sur 10 000 USD) |
| **Filtres** | jour sauté si : bougie 9h31 absente, amplitude nulle, session fermée ou `tradeMode ≠ full`, prix déjà au-delà d'un niveau au moment de placer (retard), date dans `skip_dates` |
| **Jours à sauter** | aucun par défaut (`skip_dates` vide). À toi de me donner les dates de publications (emploi, inflation, FOMC) ou de jours fériés US à exclure. |
| **Perte max journalière** | sans objet (un seul trade, risque fixé à 0,5 %) |
| **Autorisation d'exécution** | démo 10064731 : **accordée** (« Oui lance », 27/09/2026). Live : **non**. |

## Paramètres du code (`--params`)

| Clé | Défaut | Rôle |
|---|---|---|
| `buffer_buy` | 0.10 | décalage au-dessus de H, en USD (le spread s'ajoute si `add_spread_to_buy`) |
| `buffer_sell` | 0.10 | décalage sous L, en USD |
| `add_spread_to_buy` | true | ajouter le spread courant au niveau d'achat |
| `rr` | 2.0 | objectif en multiples du risque |
| `risk_pct` | 0.5 | % de l'équité risqué |
| `entry_window_min` | 30 | durée de vie des ordres après 9h32 |
| `max_hold_min` | 60 | durée max d'une position |
| `close_second_position` | true | fermer une seconde position si l'ordre opposé part avant l'annulation |
| `skip_if_late` | true | sauter le jour si le prix a déjà franchi un niveau au moment de planifier |
| `min_range` / `max_range` | 0 / 0 | bornes d'amplitude de la bougie de référence (0 = désactivé) |
| `poll_seconds` | 45 | cadence de supervision |
| `skip_dates` | vide | dates locales à exclure, séparées par des virgules |

## Ce que le simulateur ne modélise pas

Slippage réel des ordres stop (paramètre `--slippage` pour le forcer), requotes, commission (aucune sur ce type de compte), variation du spread à la seconde (le spread de la bougie est son minimum), et le fait qu'un déclenchement puis un stop puissent survenir dans la même minute sans qu'on sache lequel est venu en premier (cas « ambigu », tranché par le corps de la bougie).
