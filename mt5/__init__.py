"""Moteur de décision de trading MT5 (pur Python, sans dépendance).

Ce paquet ne parle JAMAIS au réseau : il lit des fichiers JSON produits par le
connecteur « Axi MT5 » (bougies, positions, compte, cotation, spécification du
symbole, exécutions) et émet des actions JSON (open / close / modify) qui sont
ensuite exécutées par Claude via le connecteur.

Sous-paquets :
- ``mt5.engine``     : bougies, indicateurs, modèle de stratégie, risque,
                        backtester et cycle de décision live.
- ``mt5.strategies`` : stratégies concrètes (``ema_cross``, ``template``…).
"""

__version__ = "1.0.0"
