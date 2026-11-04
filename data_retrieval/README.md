# Récupération des données

Ce répertoire rassemble la documentation sur l’accès aux champs PEARP. Le code
opérationnel est dans
[`src/ensemble_sensitivity/data_retrieval`](../src/ensemble_sensitivity/data_retrieval/README.md).
Les sondes et résultats qui servent à comparer les sources sont dans
[`exploration`](exploration/README.md).

La première phase ne requiert que les champs Z500 nécessaires à l’analyse.

**Décision : l’API WCS de Météo-France est retenue.** Elle renvoie déjà du GRIB
(`application/wmo-grib`) et permet de ne demander que Z500, une échéance et un
membre par requête (≈ 2 Mo par champ). Les fichiers GRIB ouverts contiennent
tous les membres et variables dans des fichiers de plusieurs Go, sans index
annexe : isoler Z500 impose de parcourir les en-têtes. Cette voie n’a pas été
mesurée en conditions réelles et n’est donc pas retenue, sans être exclue.

## Utilisation

Installer l’environnement du projet :

```powershell
uv sync --locked
```

Le téléchargement requiert un jeton Météo-France. Le programme le lit depuis
`PEARP_METEO_FRANCE_API_TOKEN` ou depuis un fichier `.env` à la racine du dépôt.
Ne pas publier ce jeton ni l’ajouter au dépôt.

Récupérer le dernier run complet jusqu’à l’échéance +72 h avec un pas de 24 h :

```powershell
uv run ensemble-sensitivity fetch -l 72 -s 24
```

Options : `-l/--lead-time` (heures, défaut 24), `-s/--step` (heures, défaut 24),
`-v` (détails) ou `-q` (silencieux, sans barre de progression), `--output-dir`
(défaut `output`). Les échéances récupérées sont `72, 48, 24, 0`.

Résultat :

```text
output/
├── runs_index.json                # index global : un UUID par run, son dossier et ses paramètres
└── <uuid>/                        # un dossier par run, nommé par son UUID (v4)
    ├── raw_fetch/
    │   ├── member_000_lead_000.grib   # un message GRIB brut par membre et échéance
    │   ├── …
    │   └── manifest.json              # provenance et validation (contient « run_id »)
    └── maps/                          # cartes de contrôle de `plot` (sorties dérivées du run)
```

Chaque run reçoit un UUID à chaque `fetch`. Tout ce qui dépend du run (champs
GRIB, manifeste, cartes et leur provenance) vit sous `output/<uuid>/`. Les
paramètres propres au run (couverture, initialisation, cible, pas, échéances,
variable, unités, nombre de membres, date de création) sont dans
`runs_index.json` :

```json
{
  "schema_version": 1,
  "runs": {
    "<uuid>": {
      "directory": "<uuid>",
      "created_at_utc": "2026-10-04T08:05:30+00:00",
      "parameters": {
        "coverage_id": "…", "initialization_utc": "…", "executed_at_utc": "…",
        "target_lead_hours": 24, "time_step_hours": 24,
        "required_leads_hours": [24, 0], "variable": "Z500",
        "units": "m**2 s**-2", "source": "…", "member_count": 35
      }
    }
  }
}
```

À chaque lancement de `fetch` ou de `plot`, les entrées dont le dossier n’existe
plus sont retirées de l’index (un dossier supprimé à la main disparaît donc de
l’index). Les dossiers absents de l’index ne sont jamais touchés. Un run n’est
inscrit qu’une fois complet ; l’index est écrit de façon atomique (fichier
temporaire puis remplacement). Un index illisible ou d’un schéma inconnu fait
échouer la commande au lieu d’être réinitialisé. Il n’y a pas de verrou entre
deux lancements simultanés.

**Rupture de compatibilité.** Les anciens dossiers
`output/run_<date>_lead_time_<…>_step_<…>/` ne sont pas indexés ; `plot --run-dir`
sait encore les lire (leur manifeste n’a pas de `run_id`, noté `null` dans la
provenance des cartes). Il n’existait pas de reprise ni de cache à préserver :
un `fetch` interrompu ne laissait rien. La protection « deux lancements dans la
même minute refusés » n’a plus d’objet, deux lancements ayant deux UUID.

La date du manifeste est celle de l’exécution (UTC). Les messages sont validés (run, échéance,
membre, Z500, niveau, unités, grille) mais non transformés.

Limites techniques :

- échéance et pas doivent être des multiples de 3 h, entre 0 et 102 h : c’est
  la cadence de publication de Z500 ;
- le nombre de requêtes vaut 35 membres × nombre d’échéances, sous un plafond
  de débit imposé par l’API ;
- si le run le plus récent est incomplet, le run précédent est essayé ;
- en cas d’échec ou d’interruption, rien n’est laissé sur disque ni dans l’index et il n’y a
  pas de reprise : relancer retélécharge tout ;
- chaque lancement crée un nouveau run (UUID) ; rien n’est écrasé ;
- l’accès réel à l’API n’a pas été testé automatiquement (jeton requis) ; les
  tests de bout en bout utilisent de vrais messages GRIB et une API simulée.

Produire une carte de contrôle pour un membre et une échéance :

```powershell
uv run ensemble-sensitivity plot --run-dir .\output\<uuid> --lead-hours 24 --members 0
```
`plot` produit une carte du champ Z500 et ses métadonnées de provenance ; elle
ne calcule pas de carte de sensibilité. Voir aussi le
[flux du code actuel](../src/ensemble_sensitivity/README.md).

Le décodage GRIB utilise ecCodes ; l’installation de sa bibliothèque native
peut dépendre de la plateforme.
