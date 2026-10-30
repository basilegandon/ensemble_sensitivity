# Explorer la sensibilité des prévisions PEARP

Ce projet cherche à montrer comment les différences entre les membres d’une
prévision PEARP à une échéance antérieure sont associées au Z500 moyen d’une
petite zone à une échéance future. L’outil visé est une application locale
d’exploration, pas un outil de prévision opérationnelle.

## 1. Prévisions d’ensemble et ressources

Une prévision d’ensemble rassemble plusieurs simulations d’une même situation.
Chaque simulation est un **membre**. L’ensemble PEARP de Météo-France compte
35 membres, dont un membre de contrôle. Le projet commence par le géopotentiel
à 500 hPa, ou **Z500**, sur la grille globale de 0,25°.

Pour comprendre le contexte et les données :

- Données : [jeu PE Arpege GLOB025 sur data.gouv.fr](https://www.data.gouv.fr/datasets/pe-arpege-glob025)
  (Licence Ouverte 2.0).
- Météo-France : [description technique de PEARP (PDF)](https://donneespubliques.meteofrance.fr/client/document/descriptiontechnique_pe_arpege_donneespubliques_v1_20250313_400.pdf),
  [API PE-ARPEGE](https://portail-api.meteofrance.fr/web/api/PE-ARPEGE) et
  [documentation de l’API PE Modèles](https://confluence-meteofrance.atlassian.net/wiki/spaces/OpenDataMeteoFrance/pages/853934184/API+Cibl+PE+Mod+les).
- ECMWF : [prévisions d’ensemble : rôle et construction](https://confluence.ecmwf.int/spaces/FUG/pages/673550730/Section+5+Forecast+Ensemble+ENS+-+Rationale+and+Construction)
  et [documentation du système d’ensemble IFS](https://www.ecmwf.int/en/publications/ifs-documentation).
- Articles de référence :
  [Ancell & Hakim (2007), “Comparing Adjoint- and Ensemble-Sensitivity Analysis with Applications to Observation Targeting”](https://doi.org/10.1175/2007MWR1904.1) ;
  [Torn & Hakim (2008), “Ensemble-Based Sensitivity Analysis”](https://doi.org/10.1175/2007MWR2132.1).

Les explorations de données et les notes détaillées sont rangées avec leur
sujet : [récupération des données](data_retrieval/README.md),
[exploration des sources](data_retrieval/exploration/README.md) et
[analyse de sensibilité](analysis/README.md).

## 2. Idée et méthode

L’application visée comporte deux cartes du même run. À droite, l’échéance
cible est affichée par le Z500 moyen des membres en isolignes et l’écart-type
des membres en fond de couleur. En déplaçant le curseur sur cette carte, on
place le centre d’une zone circulaire de rayon réglable en kilomètres. Le Z500
moyen de cette zone est calculé pour chacun des 35 membres, avec une pondération
par l’aire des mailles.

À gauche, une échéance antérieure sélectionnée affiche, pour chaque point de
la grille, la corrélation de Pearson entre les 35 valeurs Z500 du point et les
35 moyennes de la zone cible. Les corrélations positives et négatives sont
distinguées. Changer la position du curseur change la cible et la carte.

Pour la première phase, le terme « carte de sensibilité » désigne cette carte
de corrélations. Une corrélation décrit une association entre les membres ; ce
n’est pas une preuve de causalité.

## 3. Questions à résoudre

Les questions ci-dessous restent ouvertes. Les réponses seront ajoutées ici au
fur et à mesure qu’elles seront appuyées par des essais ou des données.

### Calcul et données

- Comment précalculer assez de cartes pour que le déplacement du curseur soit
  fluide, sans produire ou stocker un volume excessif ?
- Pour ne récupérer que les champs Z500 requis, quelle voie est la plus rapide
  et la plus économe ? Décision actuelle : l’API WCS (GRIB sélectif), voir
  [`data_retrieval`](data_retrieval/README.md) ; les fichiers GRIB ouverts n’ont
  pas été mesurés.
- Après la première phase, faut-il proposer des échéances antérieures plus
  rapprochées que le pas initial de 24 h ?
- Quelle méthode permettra plus tard de juger la significativité d’une carte
  calculée sur de nombreux points ?
- L’écart-type des membres est retenu pour la carte cible de la première phase ;
  d’autres mesures de dispersion sont-elles utiles ?

### Application

- Quelle technologie permettra une application locale rapide et simple à
  utiliser ? Dash est une piste, pas un choix arrêté.
- Les calculs lourds seront-ils préparés à l’avance ou lancés dans un processus
  séparé depuis l’application ?
- Quelles limites donner au rayon de la zone cible et quels réglages proposer ?

## 4. Première phase

La première phase va de la mise en place du dépôt à une première carte de
sensibilité sur Z500. On choisit un run — le dernier run complet par défaut —
et une échéance cible parmi celles disponibles. Pour commencer, les échéances
antérieures sont espacées de 24 h et l’on affiche une carte à la fois.

L’écart-type des 35 membres apparaît sur la carte cible. La carte de
sensibilité est une corrélation de Pearson signée. Aucun test de significativité
n’est appliqué dans cette phase.

La récupération de champs Z500 et les cartes de contrôle par membre sont déjà
disponibles. Le calcul de corrélation et l’application interactive restent à
construire. Le choix de la source de données et la stratégie de précalcul
dépendent de mesures comparables de volume et de temps.

Les choix détaillés, les hypothèses et les résultats d’essais sont conservés
dans les README et notes des répertoires concernés.

Les cartes issues des données PEARP porteront la mention « Source :
Météo-France ».

## 5. Utiliser et contribuer

Le projet utilise Python 3.14 et `uv`. Les indications de configuration et
d’utilisation des commandes actuelles se trouvent dans le
[guide de récupération](data_retrieval/README.md). La méthode statistique et
son état d’avancement sont décrits dans le [guide d’analyse](analysis/README.md).

Pour contribuer, garder chaque README centré sur son répertoire. Documenter
les choix scientifiques avec les données ou essais qui les justifient et
accompagner les changements de calcul de tests synthétiques déterministes.

Les contrôles qualité sont exécutés dans la CI et par les hooks
`pre-commit`. Pour les lancer localement avec l’environnement verrouillé :

```powershell
uv run --locked --no-sync --no-build vulture --config pyproject.toml
uv run --locked --no-sync --no-build xenon --max-absolute A --max-modules A --max-average A src scripts data_retrieval
```

Vulture signale le code probablement inutilisé avec un seuil de confiance de
80 %. Xenon impose le grade A, son seuil le plus strict, aux blocs, aux
modules et à la moyenne dans `src`, `scripts` et `data_retrieval`, y compris
les sondes exploratoires.
