# Analyse de sensibilité

Ce répertoire décrit le calcul statistique visé et son état. La première
approche met en relation, sur les membres d’un même run, le Z500 moyen d’une
petite zone à une échéance cible avec le Z500 de chaque point de la grille à
une échéance antérieure.

La première méthode retenue est une corrélation de Pearson, sans test de
significativité. Le module de calcul pur est implémenté et testé (paquet
`src/ensemble_sensitivity/sensitivity/`) ; l’application interactive, la
commande CLI associée et la significativité ne le sont pas. Les choix déjà
discutés et les questions qui restent ouvertes sont consignés dans
[`ANALYSIS.md`](ANALYSIS.md).

## Module de calcul

| Module | Rôle |
| --- | --- |
| `field_io.py` | Ingestion et décodage des champs Z500 d’un run (manifeste, GRIB). |
| `sensitivity/zone.py` | Sélection spatiale : poids cos(latitude) dans un cercle (distance orthodromique en km). |
| `sensitivity/statistics.py` | Moyenne pondérée et corrélation de Pearson signée, accumulée un champ à la fois. |
| `sensitivity/compute.py` | Cible par membre, puis carte de corrélation à une échéance antérieure. |

Comportements explicites : variance nulle de la cible → erreur ; point de grille
constant sur les membres → NaN (compté et journalisé) ; zone invalide ou ne
contenant aucune maille, membres manquants ou dupliqués (moins de 3), valeurs non
finies dans la zone ou dans un champ prédicteur, échéance antérieure non
strictement inférieure à la cible → erreur. La zone suit l’antimeridien et les
pôles.
