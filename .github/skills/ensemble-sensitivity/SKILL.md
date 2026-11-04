---
name: ensemble-sensitivity
description: Implement or review ensemble-sensitivity analysis, forecast-data ingestion, statistical calculations, scientific validation, or related pipeline work in this repository.
---

# Ensemble sensitivity work

## État du dépôt

- Implémenté : `fetch` (WCS Météo-France, GRIB Z500 par membre et échéance, manifeste, validation ecCodes) dans `src/ensemble_sensitivity/data_retrieval/wcs_retrieval.py`, et `plot` (cartes de contrôle PNG + provenance JSON) dans `src/ensemble_sensitivity/visualization.py`. Point d'entrée : `src/ensemble_sensitivity/main.py` (`uv run ensemble-sensitivity fetch|plot|sensitivity`).
- Observabilité optionnelle : `src/ensemble_sensitivity/pipeline_checkpoint/`.
- À faire (voir `analysis/README.md`, `analysis/ANALYSIS.md` et le README racine) : application interactive et significativité. Ne pas les présenter comme disponibles tant qu'elles ne sont pas codées et testées.
- Calcul pur implémenté et testé : `src/ensemble_sensitivity/field_io.py` (ingestion/décodage), `sensitivity/zone.py`, `sensitivity/statistics.py`, `sensitivity/compute.py`. Relié à la commande `sensitivity` (`sensitivity/command.py` : résolution du run dans `runs_index.json`; `sensitivity/render.py` : PNG + JSON sous `<uuid>/maps`).

## Avant de modifier

1. Lire les README pertinents : racine (spécification), `analysis/`, `data_retrieval/`, `src/ensemble_sensitivity/` (flux et commandes). Le README sépare ce qui est décidé de ce qui reste une question ouverte (section « Questions à résoudre ») ; ne pas transformer une question ouverte en fait.
2. Lire le code et les tests existants. Style de référence : `pipeline_checkpoint.py` et `tests/test_checkpoint.py` (API typées, aides ciblées, journalisation, tests) sans copier leur complexité.
3. Séparer ingestion, décodage, sélection spatiale, statistiques, sorties et orchestration. Pas de module spéculatif ni de grosse dépendance sans besoin confirmé.
4. Documentation en français ; mettre à jour les README et le diagramme de flux quand le comportement change.

## Exactitude scientifique et exécution

- Respecter les définitions du README : cible = moyenne du Z500 de la zone, pondérée par l'aire des mailles, par membre ; carte = corrélation de Pearson signée sur les 35 membres. Tester l'identité de la moyenne pondérée contre un calcul direct maille par maille sur données synthétiques.
- Traiter explicitement variance nulle, zone vide ou hors grille, membres manquants, grilles/unités invalides, valeurs non finies. Ne jamais produire un résultat plausible à partir d'une entrée invalide.
- Aléatoire reproductible (graine consignée) ; corriger les comparaisons multiples avec la méthode documentée plutôt que d'interpréter des seuils ponctuels comme une significativité globale.
- Mémoire bornée : une échéance à la fois pour les champs complets, sans matérialisation inutile.
- `logging` pour les diagnostics ; échec explicite en récupération, décodage ou calcul. Seules les écritures de checkpoint sont au mieux (best-effort).
- Privilégier la bibliothèque standard et les dépendances existantes.

## Tests et vérification

- Tests ciblés avec petits jeux synthétiques, sans téléchargement ; invariants numériques, cas limites, entrées invalides ; graines fixes.
- Lancer via `uv run --locked --no-sync --no-build` : `ruff format --check .`, `ruff check .`, `ty check .`, `vulture --config pyproject.toml`, `xenon --max-absolute A --max-modules A --max-average A src scripts data_retrieval`, `bandit -r src`, `pytest`. Signaler tout contrôle non exécuté.
