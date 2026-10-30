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
output/run_<YYYY-MM-DD-HH-MM-Z>_lead_time_<échéance>_step_<pas>/
└── raw_fetch/
    ├── member_000_lead_000.grib   # un message GRIB brut par membre et échéance
    ├── …
    └── manifest.json              # provenance et validation
```

La date est celle de l’exécution (UTC). Les messages sont validés (run, échéance,
membre, Z500, niveau, unités, grille) mais non transformés.

Limites techniques :

- échéance et pas doivent être des multiples de 3 h, entre 0 et 102 h : c’est
  la cadence de publication de Z500 ;
- le nombre de requêtes vaut 35 membres × nombre d’échéances, sous un plafond
  de débit imposé par l’API ;
- si le run le plus récent est incomplet, le run précédent est essayé ;
- en cas d’échec ou d’interruption, rien n’est laissé sur disque et il n’y a
  pas de reprise : relancer retélécharge tout ;
- deux lancements identiques dans la même minute sont refusés pour ne rien
  écraser ;
- l’accès réel à l’API n’a pas été testé automatiquement (jeton requis) ; les
  tests de bout en bout utilisent de vrais messages GRIB et une API simulée.

Produire une carte de contrôle pour un membre et une échéance :

```powershell
uv run ensemble-sensitivity plot --run-dir .\output\run_<...> --lead-hours 24 --members 0
```
`plot` produit une carte du champ Z500 et ses métadonnées de provenance ; elle
ne calcule pas de carte de sensibilité. Voir aussi le
[flux du code actuel](../src/ensemble_sensitivity/README.md).

Le décodage GRIB utilise ecCodes ; l’installation de sa bibliothèque native
peut dépendre de la plateforme.
