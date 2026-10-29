# Récupération des données

Ce répertoire rassemble la documentation sur l’accès aux champs PEARP. Le code
opérationnel est dans
[`src/ensemble_sensitivity/data_retrieval`](../src/ensemble_sensitivity/data_retrieval/README.md).
Les sondes et résultats qui servent à comparer les sources sont dans
[`exploration`](exploration/README.md).

La première phase ne requiert que les champs Z500 nécessaires à l’analyse. Le
code actuel utilise l’API WCS de Météo-France ; le choix entre cette API et les
fichiers GRIB ouverts reste à confirmer par une comparaison du temps et du
volume transféré pour les mêmes membres et échéances.

## Utilisation actuelle

Installer l’environnement du projet :

```powershell
uv sync --locked
```

Le téléchargement requiert un jeton Météo-France. Le programme le lit depuis
`PEARP_METEO_FRANCE_API_TOKEN` ou depuis un fichier `.env` à la racine du dépôt.
Ne pas publier ce jeton ni l’ajouter au dépôt.

Récupérer le dernier run complet pour une cible à +24 h et un pas de 24 h :

```powershell
uv run ensemble-sensitivity fetch --target-lead-hours 24 --step-hours 24 --output-dir .\data\pearp
```

La commande enregistre les champs GRIB et un manifeste dans le dossier du run.
Elle valide le run, les membres, les échéances, la variable, le niveau, la
grille et les unités avant de le déclarer complet. Le chemin du run complet est
indiqué dans les journaux.

Produire une carte de contrôle pour un membre et une échéance :

```powershell
uv run ensemble-sensitivity plot --run-dir .\data\pearp\run_YYYYMMDDHH_t24_s24 --lead-hours 24 --members 0
```

Remplacer le chemin d’exemple par celui affiché par `fetch`. La commande
`plot` produit une carte du champ Z500 et ses métadonnées de provenance ; elle
ne calcule pas de carte de sensibilité. Voir aussi le
[flux du code actuel](../src/ensemble_sensitivity/README.md).

Le décodage GRIB utilise ecCodes ; l’installation de sa bibliothèque native
peut dépendre de la plateforme.
