# Analyse de sensibilité

Ce répertoire décrit le calcul statistique visé et son état. La première
approche met en relation, sur les membres d’un même run, le Z500 moyen d’une
petite zone à une échéance cible avec le Z500 de chaque point de la grille à
une échéance antérieure.

La première méthode retenue est une corrélation de Pearson, sans test de
significativité. Le module de calcul pur est implémenté et testé (paquet
`src/ensemble_sensitivity/sensitivity/`) ; la commande `sensitivity` produit les cartes PNG (triangle cible/prédicteur) et leur provenance ; le coût du précalcul est mesuré dans [`ANALYSIS.md`](ANALYSIS.md) ; l’application
locale Panel + Bokeh (`ensemble-sensitivity app`) est implémentée ; la
significativité et le précalcul ne le sont pas. Les choix déjà
discutés et les questions qui restent ouvertes sont consignés dans
[`ANALYSIS.md`](ANALYSIS.md).

## Application locale

Deux cartes liées (zoom et déplacement synchronisés) : à droite, moyenne
d’ensemble en isolignes et écart-type en fond à l’échéance cible ; à gauche,
corrélation de Pearson signée (RdBu_r, ±1) avec l’échéance prédicteur choisie.
Un clic définit le centre de la zone ; le rayon (50 à 3000 km), le run
(`runs_index.json`), la cible et le prédicteur se règlent par des sélecteurs.
Le calcul se fait à la volée (`app/engine.py`, cache de 2 échéances ≈ 300 Mo) ;
mention « Source : Météo-France ». Lancement :
`uv run ensemble-sensitivity app [--run-id UUID] [--port 5006] [--no-browser]`
(serveur lié à 127.0.0.1).

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
