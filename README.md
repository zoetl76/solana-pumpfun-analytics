# Observateur pump.fun — lecture seule

Outil pour **comprendre et observer** pump.fun (memecoins Solana) **sans risquer un centime**.
Aucune clé API, aucun wallet, aucune transaction. Se branche sur le flux temps réel
gratuit de PumpPortal et logge tout.

## Pourquoi
98,6 % des tokens pump.fun finissent sous 1 000 $ de liquidité (= sans valeur).
Avant d'envisager d'y mettre de l'argent, il faut *voir* la réalité du flux :
le débit de créations, la quasi-absence de graduations, le spam de bots, les dev
qui lancent en série. Cet outil rend tout ça visible.

## Lancer
```bash
node observer.js          # tableau de bord live (rafraîchi toutes les 15 s)
node observer.js --quiet  # collecte silencieuse vers logs/events.jsonl (pour tourner en fond)
```
Ctrl-C pour arrêter.

## Ce qu'il affiche
- **tokens créés** + débit /min (le "firehose")
- **graduations** (migrations vers PumpSwap = le ~1 % qui atteint ~69k$ de mcap)
- **ratio gradué/créé** (instantané, bruité tant que l'échantillon est petit)
- **noms dupliqués** = bots qui relancent le même token en boucle
- **dev en série** = wallets qui créent plein de tokens (usines à rug)
- **auto-achat médian du créateur** (SOL) — gros auto-achat = le dev se prépare à dump (soft rug)

## Données
- `logs/events.jsonl` : un événement brut par ligne (create / migrate) + horodatage `t`.
- Analyse a posteriori : voir les commandes d'analyse dans l'historique, ou relire le JSONL.

## Limites (honnêtes)
- Le flux GRATUIT ne donne que **créations** et **migrations**. Le détail trade-par-trade
  (qui achète, qui vend, donc la détection fine de rug en direct) est **payant** chez PumpPortal
  (~0,02 SOL) ou nécessite un RPC Solana + parsing du programme pump.fun (gratuit mais plus lourd).
- Le `mint` d'une migration concerne un token créé il y a des heures/jours → pas dans la session.
- Le "ratio gradué/créé" instantané n'est PAS un vrai taux de cohorte ; sur longue durée il
  converge vers le taux réel (~1 %).

---

# rug_watch.js — surveiller la VIE d'UN token (via RPC Solana)

Là où `observer.js` donne la vue macro (créations/graduations via PumpPortal),
`rug_watch.js` zoome sur **un seul token** et décode **chaque trade on-chain** en
temps réel via un RPC Solana gratuit — pour VOIR les patterns d'arnaque se produire.

```bash
node rug_watch.js              # attrape le prochain token créé et suit sa vie
node rug_watch.js <MINT>       # suit un token précis
# pour du 24/7 fiable (clé Helius gratuite) :
RPC_WS="wss://mainnet.helius-rpc.com/?api-key=TA_CLE" node rug_watch.js <MINT>
```

## Ce qu'il détecte (alertes 🚨)
- **DEV DUMP** : le créateur (champ `creator` lu dans chaque trade) vend
- **GROS SELL** : un wallet sort ≥1 SOL d'un coup
- **LIQUIDITÉ EN FUITE** : la réserve réelle chute de -30/-50/-75/-90 % depuis son pic (soft rug)
- **GRADUATION proche** : réserve réelle ≈85 SOL (migration PumpSwap imminente)
- suivi : trades achats/ventes, wallets uniques, snipers (<15 s), top holder

## Comment ça marche (technique, validé sur données réelles)
- `logsSubscribe {mentions:[mint]}` sur le RPC → on reçoit les transactions du token.
- Chaque `TradeEvent` Anchor est émis en `Program data: <base64>` ; on décode le **discriminator**
  8 octets `[189,219,127,211,78,230,97,238]` (= `sha256("event:TradeEvent")[:8]`) puis les champs Borsh.
- **Layout 2026 confirmé contre l'IDL officiel** (offsets FIXES) :
  `mint@8`, `sol_amount@40`, `token_amount@48`, `is_buy@56`, `user@57`, `timestamp@89`,
  `virtual_sol_reserves@97`, `virtual_token_reserves@105`, `real_sol_reserves@113`,
  `real_token_reserves@121`, `fee_recipient@129`, `fee_basis_points@161`, `fee@169`,
  **`creator@177`**, `creator_fee_basis_points@209`, `creator_fee@217`.
  Au-delà de l'octet 258 (`ix_name` string, `shareholders` vec, `quote_mint`…) le layout
  devient variable — on ne lit que la zone fixe, qui suffit pour la détection de rug.

## Choix RPC (honnête)
- Par défaut : `wss://api.mainnet-beta.solana.com` — 0 inscription, marche pour observer,
  MAIS rate-limité (100 req/10 s, 40 conns/IP), timeout d'inactivité 10 min, drops sous charge.
  Un watchdog reconnecte avant le timeout.
- Pour du durable : clé **Helius gratuite** (10 req/s, 5 conns WS) via `RPC_WS`.
- `mentions` n'accepte qu'**une** adresse par souscription → 1 token à la fois.
- Pour suivre seulement le PRIX (pas qui trade), `accountSubscribe` sur la bonding curve PDA
  (`seeds=[b"bonding-curve", mint]`) serait plus léger — mais ne dit pas QUI vend.

## Constat de terrain (1er run réel)
En 45 s sur un token actif : **516 ventes / 47 achats**, réserve **9,2 SOL → 0,2 SOL**,
un whale sortant 156 $ d'un coup. La liquidité fuit en direct. C'est la norme, pas l'exception.

---

# multi_watch.js — suivre TOUS les tokens frais + score de rug

Vue parallèle : suit en temps réel chaque token fraîchement créé et calcule un
**score de rug 0-100** pour chacun. Une seule souscription firehose RPC (`mentions:[programme]`)
route les trades vers les tokens suivis ; PumpPortal fournit les naissances (mint, créateur).

```bash
node multi_watch.js
RPC_WS="wss://mainnet.helius-rpc.com/?api-key=TA_CLE" node multi_watch.js   # 24/7
```

Tableau live trié par danger : ÉTAT (🟢 vivant / 🟡 suspect / 🔴 rug / ⚰️ mort / 🎓 gradué),
$symbole, âge, réserve(pic), achats/ventes, snipers, flag DEV DUMP, score.
Compteurs : nés / rug / morts / gradués + taux de mortalité observé.
Dataset : `logs/token_outcomes.jsonl` (un sort par token classé).

**Score** = fuite de liquidité (réserve vs pic) + dev-dump (créateur vend) +
pression vendeuse (ratio ventes) + dominance snipers + **bundle**.

## Détection de BUNDLE (rug pré-organisé)
Le firehose RPC donne le `slot` (bloc) de chaque trade. Si le créateur + **≥4 acheteurs
distincts** achètent dans le **même bloc** que la création (fenêtre = bloc de création + 1),
c'est un **lancement en bundle** : des wallets coordonnés (souvent les alts du dev) accumulent
au prix plancher pour dumper sur les acheteurs organiques. Flag `BNDn` + 🟣 compteur.

## Alerte SURVIVANTS
Un token qui tient **>10 min** en restant sain (réserve ≥1◎, encore tradé) est rare → statut
⭐ SURVIVANT, loggé dans `logs/survivors.jsonl`. Optionnel : notification sur ton téléphone via
`NTFY_TOPIC="ton-topic" node multi_watch.js` (app ntfy.sh, gratuit, abonne-toi au topic).

## Collecte longue + analyse
```bash
# daemon détaché (tourne en fond, survit à la session) :
nohup node multi_watch.js --quiet > logs/multi_watch.out 2>&1 & echo $! > logs/multi_watch.pid
node stats.js                                   # analyse le dataset à tout moment
kill $(cat logs/multi_watch.pid)                # arrêter le daemon
```
`stats.js` sort : taux de mortalité, taux de graduation, % dev-dump, % bundle (+ rug rate
bundle vs non-bundle), distribution du temps-avant-rug, liquidité atteinte, liste des survivants.
Note : sur des heures, le RPC public peut throttler/couper (watchdog reconnecte) ; une clé
Helius gratuite via `RPC_WS` fiabilise.

## Constat réel (run de 140 s, 2026-06-11)
31 tokens nés → **22 rug (71 % de mortalité), 0 graduation**. **82 % avec dev-dump.**
Âge médian au rug = **16 secondes**. Pic de réserve médian = 4,5 SOL (graduation à ~85,
soit ~5 % du chemin). Le meilleur a culminé à 34 SOL puis chuté. Noms spammés ($GTA6 ×5).
Conclusion empirique : le rug n'est pas un risque, c'est le **comportement par défaut**.

# Ce que ça ne fait PAS
Aucun trade. Aucun ordre. Aucune clé privée. Lecture seule, point.

# Moteur MT5 (dossier `mt5/`)
Un package Python autonome (bibliothèque standard, Python 3.11) pour backtester et piloter une
stratégie sur les comptes Axi MT5 via le connecteur : voir **[`mt5/README.md`](mt5/README.md)**
(installation, cycle 24/5, garde-fous, commandes) et **[`mt5/STRATEGIE.md`](mt5/STRATEGIE.md)** (fiche stratégie à remplir).
Tests : `python3 -m unittest discover -s mt5/tests -t . -v`.
