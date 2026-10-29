# Exploration des sources PEARP

Ce répertoire contient les sondes d’API, le pilote WCS et leurs résultats.
Ces outils servent à vérifier le comportement des sources et à guider
l’ingestion ; ils ne font pas partie de la commande de récupération
opérationnelle.

- [`ANALYSIS.md`](ANALYSIS.md) décrit les observations, les mesures et les
  limites connues.
- `probe_pe_arpege_api.py` explore le catalogue et les requêtes de l’API.
- `pilot_pearp_wcs.py` mesure une récupération WCS de champs Z500 et vérifie
  les métadonnées GRIB.
- Le fichier Swagger local documente les opérations de l’API.

Les deux scripts peuvent être lancés depuis la racine du dépôt :

```powershell
python data_retrieval\exploration\probe_pe_arpege_api.py
uv run --no-project --with eccodes python -m data_retrieval.exploration.pilot_pearp_wcs --target-lead-hours 24 --step-hours 24
```

Les scripts utilisent le jeton `PEARP_METEO_FRANCE_API_TOKEN` ou le fichier
`.env` à la racine. Ne jamais inclure le jeton dans un rapport ou un commit.
