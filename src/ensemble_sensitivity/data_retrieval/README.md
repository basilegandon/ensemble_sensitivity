# Code de récupération

Ce paquet contient la récupération opérationnelle des champs PEARP et leur
validation avant persistance. Il ne contient pas le calcul statistique de
sensibilité.

- `wcs_retrieval.py` découvre les runs candidats, récupère les champs Z500,
  vérifie leurs métadonnées et enregistre les champs ainsi que le manifeste.
- Le point d’entrée de la commande est dans `../main.py`.
- Les cartes de contrôle et leurs métadonnées sont produites par
  `../visualization.py`.

Le [flux d’exécution du paquet](../README.md) suit les entrées, les contrôles
et les fichiers produits par les commandes actuelles. Les sondes de recherche
de sources sont documentées dans
[`data_retrieval/exploration`](../../../data_retrieval/exploration/README.md).
