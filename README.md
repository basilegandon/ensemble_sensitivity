# Guide de projet : analyse de sensibilité d'ensemble PEARP (Z500)

> **Nom de travail :** `ensemble_sensitivity` (_Ensemble Sensitivity Analysis_)
> **Version du guide :** 0.1, 2 octobre 2026
> **Statut :** cadrage, document vivant à affiner section par section
> **Langage :** Python

Légende des statuts utilisée dans tout le document :

- ✅ **Acté** : décidé pendant le cadrage
- 🔶 **Hypothèse** : proposition de travail, à confirmer
- ❓ **À vérifier** : dépend d'une vérification sur les données réelles

---

## 1. Contexte et objectif

### 1.1 Problème

Un système de prévision d'ensemble produit, à chaque run, 35 scénarios plausibles de l'atmosphère. La dispersion entre membres à une échéance donnée (par exemple l'incertitude sur la position d'un creux à t+72 h) a des **précurseurs** : des différences entre membres, plus tôt dans la prévision et ailleurs sur le globe, statistiquement liées à cette dispersion.

### 1.2 Objectif

Pour **un run PEARP donné**, une **échéance cible t₁** et une **zone d'intérêt** choisies par l'utilisateur, produire **une carte par échéance antérieure τ < t₁** qui montre où le géopotentiel à 500 hPa (Z500) est **corrélé ou anti-corrélé**, sur les 35 membres, avec le Z500 de la zone à t₁.

Autrement dit : « si je veux savoir ce que fera la zone Z à t₁, quelles régions du globe, et à quelles échéances antérieures, dois-je surveiller ? »

### 1.3 Principe

C'est une _ensemble sensitivity analysis_ au sens de Ancell & Hakim (2007) et Torn & Hakim (2008) : une statistique (corrélation, régression) entre une quantité de réponse et le champ d'état à une échéance antérieure, calculée sur les membres de l'ensemble.

### 1.4 Usage

- Outil **local d'exploration** ✅
- **Déclenchement manuel** ✅
- Pas de diffusion automatique, pas d'usage opérationnel

### 1.5 Non-objectifs

- Établir une **causalité** : la précédence temporelle et la corrélation sur 35 membres ne prouvent rien sur les mécanismes.
- Produire une prévision ou un produit officiel.
- Faire du temps réel strict : « à la volée » signifie ici « pour le dernier run disponible, au moment où l'utilisateur lance l'outil ».

---

## 2. Décisions de cadrage

| #   | Décision                   | Valeur                                                                                 | Statut |
| --- | -------------------------- | -------------------------------------------------------------------------------------- | ------ |
| D1  | Variable de départ         | Z500, à la fois pour la cible et pour les prédicteurs                                  | ✅     |
| D2  | Agrégation de la cible     | Aucune au départ : chaque cellule de la zone est une cible                             | ✅     |
| D3  | Synthèse multi-cellules    | Une carte de corrélation par cellule cible, puis **moyenne pondérée** sur les cellules | ✅     |
| D4  | Choix de t₁ et de la zone  | Fixés par l'utilisateur à chaque lancement                                             | ✅     |
| D5  | Domaine des prédicteurs    | Tout le domaine disponible (globe, grille 0,25°)                                       | ✅     |
| D6  | Pas temporel initial       | 24 h, à réduire selon le temps de calcul                                               | ✅     |
| D7  | Échéance la plus lointaine | Le maximum disponible dans les données                                                 | ✅     |
| D8  | Exécution                  | En local                                                                               | ✅     |
| D9  | Validation                 | Pas de cas historique ; validation prospective sur événements à venir                  | ✅     |
| D10 | Langage                    | Python                                                                                 | ✅     |
| D11 | Mode de calcul             | En flux, une échéance à la fois                                                        | 🔶     |
| D12 | Significativité            | Test de permutation des membres, statistique du maximum                                | 🔶     |

---

## 3. Données

### 3.1 Source

Jeu de données **« PE Arpege GLOB025 »** de Météo-France sur data.gouv.fr (licence ouverte Etalab 2.0).

**Établi :**

- Ensemble de **35 membres** (1 contrôle + 34 simulations perturbées)
- Réseaux (heures d'initialisation) : **0, 6, 12 et 18 UTC**
- Grille globale **0,25°** (90°S–90°N, 0°–359,75°E), soit environ 1440 × 721 ≈ 1,04 million de points
- Fichiers au format **GRIB2**, environ **103 ressources** de 2 à 4 Go
- Les ressources sont accessibles par des liens de la forme `https://www.data.gouv.fr/api/1/datasets/r/<uuid>`
- Obligation d'attribution : « Source : Météo-France »

**Documentation manquante.** La fiche signale que la documentation des fichiers n'est pas renseignée : la structure interne des 103 fichiers est à découvrir.

**Piste technique.** Un projet tiers ([PEARP-25-km](https://github.com/alertesmeteo-hub/PEARP-25-km)) lit ces mêmes fichiers par requêtes HTTP _Range_, indexe les messages GRIB, ne télécharge que les champs utiles, puis vérifie run, échéance, grille, unités et présence des 35 membres. Le principe est directement réutilisable.

### 3.2 À vérifier (premier jalon)

| #   | Question                                                                                                     | Statut |
| --- | ------------------------------------------------------------------------------------------------------------ | ------ |
| V1  | Z500 est-il présent ? Quel identifiant GRIB (paramètre, niveau) ?                                            | ❓     |
| V2  | Quelle échéance maximale pour le jeu ouvert ? (une page tierce évoque 102 h, non confirmé)                   | ❓     |
| V3  | Quel pas d'échéance (3 h, 6 h, 12 h, 24 h) selon la plage ?                                                  | ❓     |
| V4  | Comment les échéances et les variables se répartissent-elles dans les 103 fichiers ?                         | ❓     |
| V5  | Des index (`.idx`) sont-ils fournis, ou faut-il parcourir les en-têtes de messages ?                         | ❓     |
| V6  | Unité de Z500 : hauteur géopotentielle (m) ou géopotentiel (m²/s²) ?                                         | ❓     |
| V7  | Les membres sont-ils identifiés de façon stable (numéro d'ensemble, perturbation) ?                          | ❓     |
| V8  | Délai entre l'heure nominale d'un run et la disponibilité complète de ses fichiers ?                         | ❓     |
| V9  | Un jeu PEARP à 0,1° existe-t-il en accès ouvert ? (les paquets 0,1° trouvés sont ceux d'ARPEGE déterministe) | ❓     |

### 3.3 Volumétrie estimée

- Un champ Z500 pour 35 membres, une échéance : 35 × 721 × 1440 × 4 octets ≈ **145 Mo** en float32.
- Avec requêtes Range, le téléchargement se limite aux messages Z500 : de l'ordre de **100 à 150 Mo par échéance** (estimation à confirmer).
- À 24 h de pas sur 4 à 5 jours : environ 5 échéances, soit quelques centaines de Mo au total.

---

## 4. Définitions mathématiques

### 4.1 Notation

- `N = 35` membres, indice `m`
- Cellules de grille `s` (prédicteurs), `P ≈ 1,04 × 10⁶`
- Cellules de la zone cible `c ∈ C`, `K = |C|` (K ≥ 1)
- Échéance cible `t₁`, échéances antérieures `τ_k = t₁ − k·Δ` (k = 1, 2, … tant que τ_k ≥ 0), `Δ` = pas temporel
- Optionnel : `τ = t₁` (carte « contemporaine » de la structure de variabilité de la cible, utile comme référence)

### 4.2 Anomalies

Pour chaque échéance et chaque cellule, on retire la moyenne d'ensemble :

```
Z'_m(s, τ) = Z_m(s, τ) − (1/N) Σ_m Z_m(s, τ)
```

On travaille donc toujours en anomalies **par rapport à l'ensemble du même run**. Il n'y a pas de climatologie à gérer.

### 4.3 Cible : cellules standardisées puis moyenne pondérée

Pour chaque cellule cible `c` à t₁ :

```
z_{m,c} = Z'_m(c, t₁) / σ_c         avec σ_c l'écart-type d'ensemble (ddof = 1)
```

Poids de chaque cellule (aire de la maille sur la sphère) :

```
w_c = cos(lat_c),     w̃_c = w_c / Σ_c w_c
```

Cible agrégée, **calculée uniquement comme outil de calcul** (la sortie reste la moyenne de corrélations, voir 4.4) :

```
z̄_m = Σ_c w̃_c · z_{m,c}
σ_z̄ = écart-type d'ensemble de z̄      (0 ≤ σ_z̄ ≤ 1)
```

Les cellules de σ_c quasi nul (< ε) sont exclues de la zone.

### 4.4 Sortie principale : moyenne des corrélations par cellule

Pour chaque échéance τ et chaque point `s` :

```
r(c, s, τ) = corr_m( Z'_m(c, t₁),  Z'_m(s, τ) )

M(s, τ) = Σ_c w̃_c · r(c, s, τ)              ← carte demandée (D3)
```

### 4.5 Identité qui simplifie le calcul

Comme chaque `z_c` est standardisé, `r(c, s, τ) = cov(z_c, x) / σ_x` avec `x = Z'(s, τ)`. La covariance est linéaire, donc :

```
M(s, τ) = cov(z̄, x) / σ_x  =  σ_z̄ · r(z̄, x)
```

**Conséquences :**

1. Il n'est **pas nécessaire de calculer K cartes** : un seul vecteur `z̄` de 35 valeurs suffit, et une seule passe par échéance.
2. La carte `M` a **la même forme** que la corrélation avec la cible moyenne `z̄`. Elle en diffère par le facteur scalaire `σ_z̄`.
3. `σ_z̄` mesure la **cohérence de la zone** : proche de 1 si les cellules varient ensemble, faible si elles se compensent (dipôle, zone trop étendue). **Il doit toujours être affiché avec la carte.**
4. Moyenner sur les cellules **ne réduit pas le bruit d'échantillonnage** de la corrélation. Les cellules d'un champ lisse partagent les mêmes 35 membres et donc le même bruit. En revanche, cela retire la variabilité propre à chaque cellule, ce qui améliore le signal.
5. Signal et bruit sont mis à l'échelle de la même façon par `σ_z̄` : le seuil de significativité de `M` est `σ_z̄` fois celui de `r(z̄, x)`.

### 4.6 Sorties

| Sortie                                                                                     | Définition                                                                 | Statut |
| ------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------- | ------ |
| `M(s, τ)`                                                                                  | moyenne pondérée des corrélations par cellule (carte principale)           | ✅     |
| `σ_z̄`                                                                                      | cohérence de la zone cible                                                 | ✅     |
| `r(z̄, x) = M / σ_z̄`                                                                        | corrélation avec la cible moyenne (version normalisée, option d'affichage) | 🔶     |
| masque de significativité                                                                  | voir 5.4                                                                   | 🔶     |
| `β(s, τ) = cov(J, x) / var(x)` avec `J` moyenne pondérée des anomalies en unités physiques | sensibilité en mètres par mètre, **secondaire, v2**                        | 🔶     |

### 4.7 Évolutions possibles (hors périmètre v1)

- Remplacer `z̄` par la première composante principale (PC1) du champ cible à t₁ : plus fidèle quand la zone contient un dipôle. La structure du calcul reste identique.
- Carte de « fraction d'accord » : part des cellules cibles dont |r| dépasse un seuil, avec séparation positif/négatif. Utile comme diagnostic de cohérence.

---

## 5. Choix méthodologiques détaillés

### 5.1 Normalisation

- **Anomalies par rapport à la moyenne d'ensemble**, par cellule et par échéance (4.2).
- **Standardisation des cellules cibles** (poids égal avant pondération d'aire) ✅. Alternative : anomalies physiques, où les cellules les plus variables dominent. Non retenue en v1 ; à comparer au jalon 4.
- **Plancher sur l'écart-type des prédicteurs** : si `σ_x < ε`, le point est masqué (NaN). Aux premières échéances, les membres sont presque identiques et la corrélation devient instable. Valeur de ε à calibrer sur les données (❓).
- Corrélation de Pearson en `float32`, avec accumulation en `float64` pour les sommes si nécessaire.

### 5.2 Granularité spatiale

- **Prédicteurs :** grille native 0,25° 🔶. Le coût est dominé par l'I/O et non par le calcul. Option de sous-échantillonnage ×2 (0,5°) pour accélérer l'exploration, car Z500 est très lisse.
- **Cible :** cellules de la grille native, sélectionnées par une boîte lat/lon en v1 (liste de cellules ou polygone en option plus tard).
- **Longitudes :** grille de 0° à 359,75° ; gérer le passage du méridien d'origine et de l'antiméridien dans la sélection de zone.

### 5.3 Granularité temporelle

- **Pas Δ = 24 h au départ** ✅. Échéances considérées : `t₁ − 24 h, t₁ − 48 h, …` jusqu'à 0.
- Réduction progressive vers 12 h, 6 h, 3 h tant que le temps de calcul reste acceptable et que les données existent (V3).
- **Échéance maximale :** celle du jeu de données (V2).
- Une analyse de sensibilité au pas temporel fait partie du jalon 4.

### 5.4 Significativité

Avec 35 membres :

- Pour **un test isolé**, |r| > 0,33 correspond à environ 95 % (t de Student à 33 degrés de liberté).
- Sur un million de points, de nombreuses « corrélations fortes » sont **dues au hasard**, surtout loin de la zone, où la dynamique n'offre pas de raison physique de lien.

Méthode proposée 🔶 : **test de permutation avec statistique du maximum**.

1. Mélanger aléatoirement les 35 étiquettes de membres du vecteur `z̄`. Cela conserve la structure spatiale des prédicteurs et détruit le lien avec la cible.
2. Calculer la carte `M_perm` et en retenir `max_s |M_perm(s, τ)|`.
3. Répéter B fois (B = 500 à 1000).
4. Seuil de la carte = quantile 95 % des maxima. Les points qui le dépassent sont significatifs **à l'échelle de la carte entière**.

Coût : un produit matriciel `(P × N) · (N × B)` par échéance, découpé en blocs de points avec maximum courant pour éviter de stocker `P × B`.

Choix à trancher (❓) : seuil **par échéance** (plus permissif) ou **commun à toutes les échéances** (plus strict).

### 5.5 Variables

- **V1 :** Z500 pour la cible et les prédicteurs ✅.
- **Extensions envisagées :**
  - autres niveaux de géopotentiel (300, 700, 850 hPa)
  - température à 850 hPa, pression au niveau de la mer
  - cible différente de Z500 (précipitations, température) avec prédicteurs Z500
- Comme la corrélation est adimensionnelle, les cartes de variables différentes restent comparables.

---

## 6. Architecture

### 6.1 Flux de données

```
[Catalogue data.gouv.fr] → [Index GRIB (Range)] → [Extraction Z500 par Range]
        → [Cache disque (Zarr/NetCDF)] → [Tenseur (N, lat, lon) par échéance]
        → [Statistiques : z̄, M, seuil] → [Cartes + exports + manifeste]
```

Le calcul se fait **en flux** : pour chaque échéance, on charge `(35, P)` en mémoire, on calcule `M`, on met à jour le maximum de permutation, puis on libère. La mémoire de travail reste de l'ordre de quelques centaines de Mo.

### 6.2 Modules Python (proposition)

```
pearp_esa/
├── catalog.py     # liste des ressources, détection du dernier run complet
├── gribindex.py   # index des messages GRIB (offset, longueur, paramètre, niveau, membre, échéance)
├── fetch.py       # téléchargement par Range, cache, reprise sur erreur
├── decode.py      # décodage GRIB2 → numpy, vérification grille et unités
├── zone.py        # sélection de zone, poids cos(lat), gestion des longitudes
├── stats.py       # anomalies, z̄, σ_z̄, carte M, permutations
├── plot.py        # cartes (colormap divergente centrée sur 0, zone cible, significativité)
├── manifest.py    # traçabilité d'un run d'analyse
└── cli.py         # point d'entrée
```

### 6.3 Bibliothèques envisagées 🔶

| Besoin              | Candidats                                                         |
| ------------------- | ----------------------------------------------------------------- |
| Décodage GRIB2      | `eccodes` (via `cfgrib`/`xarray`), ou `pygrib`                    |
| Calcul              | `numpy` (produits matriciels BLAS), `xarray` pour les métadonnées |
| Cache               | `zarr` (découpage par échéance) ou NetCDF                         |
| Requêtes HTTP Range | `httpx` ou `requests`                                             |
| Cartes              | `matplotlib` + `cartopy`                                          |
| Tests               | `pytest`, `hypothesis` pour les propriétés statistiques           |

Le décodage GRIB dépend d'une bibliothèque binaire (ECMWF eccodes) : prévoir une installation via conda-forge pour éviter les surprises.

Le principe de la bibliothèque de checkpoints déjà développée (manifeste JSON, sauvegardes best-effort) peut être réutilisé pour la traçabilité ; à évaluer si elle convient à des tableaux N-D ou si Zarr + manifeste suffit.

### 6.4 Interface en ligne de commande (esquisse)

```bash
 run ensemble_sensitivity \
  --run latest \
  --t1 +72h \
  --bbox 42 51 -5 8 \
  --step 24h \
  --nperm 500 \
  --out ./sorties/
```

### 6.5 Cœur du calcul (esquisse)

```python
import numpy as np


def sensitivity_map(X: np.ndarray, zbar: np.ndarray, eps: float) -> np.ndarray:
    """
    X    : (N, P) champ Z500 à l'échéance τ, N membres, P points de grille
    zbar : (N,)   cible agrégée standardisée (moyenne pondérée des cellules)
    Retourne M(s) = cov(zbar, x) / σ_x
    """
    N = X.shape[0]
    Xc = X - X.mean(axis=0, keepdims=True)
    zc = zbar - zbar.mean()
    sx = Xc.std(axis=0, ddof=1)
    cov = (zc @ Xc) / (N - 1)
    return np.where(sx > eps, cov / sx, np.nan)
```

---

## 7. Plan par jalons

| Jalon  | Contenu                   | Livrable                                                              | Critère de sortie                                   |
| ------ | ------------------------- | --------------------------------------------------------------------- | --------------------------------------------------- |
| **M0** | Exploration des données   | Notebook ou script d'inventaire + fiche « format des fichiers »       | V1 à V8 résolus                                     |
| **M1** | Ingestion                 | Chargement fiable de Z500 (35, 721, 1440) pour un run et une échéance | Complétude vérifiée, cache fonctionnel              |
| **M2** | Cœur statistique          | Calcul de `M`, `σ_z̄`, seuil de permutation, tests unitaires           | Tests passants sur données synthétiques             |
| **M3** | Sorties et visualisation  | CLI de bout en bout, cartes par échéance                              | Une exécution manuelle complète en temps acceptable |
| **M4** | Exploration et robustesse | Études de sensibilité aux choix, extensions                           | Rapport de résultats et de limites                  |

Ordre : M0 → M1 → M2 → M3, puis M4. M2 peut démarrer en parallèle de M1 sur données synthétiques.

---

## 8. Tickets

Estimations indicatives : **S** ≈ une demi-journée, **M** ≈ une à deux journées, **L** ≈ trois jours ou plus.

### Jalon M0 : Exploration des données

#### T0.1 Inventaire des ressources du jeu PE Arpege GLOB025 (S)

- **Objectif :** produire une table (nom de ressource, taille, URL, date, run) des 103 ressources.
- **Critères d'acceptation :**
  - [ ] table exportée en CSV ou Parquet
  - [ ] regroupement visible par run (0/6/12/18) et par plage d'échéances si déductible des noms
- **Dépendances :** aucune

#### T0.2 Indexer un fichier GRIB2 sans le télécharger en entier (M)

- **Objectif :** lister les messages d'un fichier (paramètre, niveau, membre, échéance, offset, longueur) par requêtes Range.
- **Critères d'acceptation :**
  - [ ] vérifier d'abord la présence d'un fichier d'index associé ; sinon parcours des en-têtes de messages
  - [ ] index sauvegardé sur disque, réutilisable
  - [ ] volume téléchargé pendant l'indexation mesuré et documenté
- **Dépendances :** T0.1

#### T0.3 Localiser Z500 et caractériser la couverture (S)

- **Objectif :** répondre à V1, V2, V3, V4, V7.
- **Critères d'acceptation :**
  - [ ] identifiant GRIB de Z500 documenté
  - [ ] liste des échéances disponibles et du pas
  - [ ] confirmation de 35 membres et de la façon de les identifier
- **Dépendances :** T0.2

#### T0.4 Décoder un champ Z500 et valider la grille et les unités (S)

- **Objectif :** décoder un champ, vérifier la grille (1440 × 721), l'orientation des latitudes et les unités (V6).
- **Critères d'acceptation :**
  - [ ] image de contrôle : le champ est plausible (creux, dorsales)
  - [ ] unité documentée (m ou m²/s²) et conversion éventuelle codée
- **Dépendances :** T0.3

#### T0.5 Mesurer les délais de disponibilité des runs (S)

- **Objectif :** répondre à V8 en observant plusieurs runs.
- **Critères d'acceptation :**
  - [ ] délai moyen et maximal entre l'heure nominale et la disponibilité complète
  - [ ] règle de détection d'un run complet définie
- **Dépendances :** T0.1

### Jalon M1 : Ingestion

#### T1.1 Détection du dernier run complet (S)

- **Critères d'acceptation :**
  - [ ] fonction `latest_complete_run()` fiable selon la règle de T0.5
  - [ ] possibilité de forcer un run précis (date + réseau)
- **Dépendances :** T0.5

#### T1.2 Téléchargeur Range avec cache et reprise (M)

- **Critères d'acceptation :**
  - [ ] clé de cache : (run, paramètre, niveau, échéance, membre)
  - [ ] reprise après interruption sans re-télécharger les messages déjà reçus
  - [ ] nouvelles tentatives avec attente croissante en cas d'erreur réseau
  - [ ] parallélisme borné et paramétrable
- **Dépendances :** T0.2

#### T1.3 Assemblage du tenseur d'ensemble (S)

- **Critères d'acceptation :**
  - [ ] retourne `(35, 721, 1440)` float32 pour une échéance
  - [ ] erreur explicite si un membre manque ou si des NaN inattendus apparaissent
  - [ ] ordre des membres stable et documenté
- **Dépendances :** T0.4, T1.2

### Jalon M2 : Cœur statistique

#### T2.1 Sélection de zone et poids (S)

- **Critères d'acceptation :**
  - [ ] boîte lat/lon → liste de cellules, avec gestion des longitudes 0–360
  - [ ] poids `cos(lat)` normalisés
  - [ ] erreur claire si la zone est vide ou hors grille
- **Dépendances :** aucune

#### T2.2 Construction de la cible agrégée `z̄` et de `σ_z̄` (S)

- **Critères d'acceptation :**
  - [ ] standardisation par cellule, exclusion des cellules à σ_c < ε
  - [ ] retourne `z̄` (35,) et `σ_z̄`
  - [ ] avertissement si `σ_z̄` est faible (seuil à définir)
- **Dépendances :** T2.1

#### T2.3 Carte de sensibilité `M(s, τ)` (S)

- **Critères d'acceptation :**
  - [ ] implémentation vectorisée en flux (esquisse en 6.5)
  - [ ] masque des points à σ_x < ε
- **Dépendances :** T2.2

#### T2.4 Tests sur données synthétiques (M)

- **Critères d'acceptation :**
  - [ ] **identité 4.5 vérifiée numériquement** : `M` calculée via `z̄` égale à la moyenne pondérée de `r(c, ·)` calculée cellule par cellule (tolérance 1e-5)
  - [ ] ensemble synthétique avec lien connu (un point prédicteur construit pour corréler à 0,8 avec la cible) : la carte le retrouve
  - [ ] ensemble de bruit pur : taux de points dépassant le seuil cohérent avec le niveau annoncé
  - [ ] cas du dipôle : `σ_z̄` bas détecté
- **Dépendances :** T2.3

#### T2.5 Test de permutation (statistique du maximum) (M)

- **Critères d'acceptation :**
  - [ ] seuil par échéance pour B configurable
  - [ ] calcul par blocs de points, mémoire bornée
  - [ ] graine aléatoire fixée et enregistrée pour la reproductibilité
  - [ ] sur bruit pur, environ 5 % des cartes contiennent au moins un point significatif
- **Dépendances :** T2.3

### Jalon M3 : Sorties et visualisation

#### T3.1 Carte d'une échéance (M)

- **Critères d'acceptation :**
  - [ ] colormap divergente centrée sur 0, bornes symétriques
  - [ ] zone cible tracée, côtes et frontières visibles
  - [ ] hachures ou contour des points significatifs (T2.5)
  - [ ] titre avec run, t₁, τ, `σ_z̄`
  - [ ] mention « Source : Météo-France »
- **Dépendances :** T2.5

#### T3.2 Planche multi-échéances et exports (S)

- **Critères d'acceptation :**
  - [ ] une figure regroupant toutes les échéances
  - [ ] export des cartes en NetCDF ou Zarr, avec métadonnées complètes
- **Dépendances :** T3.1

#### T3.3 Interface en ligne de commande (S)

- **Critères d'acceptation :**
  - [ ] la commande de 6.4 fonctionne de bout en bout
  - [ ] messages d'erreur explicites (run indisponible, zone invalide)
- **Dépendances :** T1.1, T1.3, T3.2

#### T3.4 Manifeste d'exécution (S)

- **Critères d'acceptation :**
  - [ ] fichier JSON : run, t₁, zone, pas, B, graine, version du code, durées par étape, volumes téléchargés
- **Dépendances :** T3.3

### Jalon M4 : Exploration et robustesse

#### T4.1 Sensibilité au pas temporel et à la résolution (M)

- **Objectif :** comparer 24 h, 12 h, 6 h, 3 h, puis 0,25° et 0,5°, en lisant les différences de carte et le temps de calcul.
- **Critères d'acceptation :**
  - [ ] tableau temps/qualité documenté
  - [ ] pas par défaut recommandé

#### T4.2 Comparaison des synthèses de zone (M)

- **Objectif :** comparer `M`, la PC1 et la fraction d'accord sur plusieurs zones et événements.
- **Critères d'acceptation :**
  - [ ] cas où les méthodes divergent identifiés et expliqués

#### T4.3 Stabilité entre runs consécutifs (M)

- **Objectif :** pour une même échéance valide, comparer les cartes issues de deux runs successifs.
- **Critères d'acceptation :**
  - [ ] indicateur de similarité des cartes (corrélation spatiale) calculé
  - [ ] structures robustes distinguées des structures fluctuantes

#### T4.4 Extensions de variables (L)

- **Critères d'acceptation :**
  - [ ] au moins un autre niveau ou une autre variable ajoutée proprement
  - [ ] cible non-Z500 testée sur un cas

#### T4.5 Protocole de validation prospective (M)

- Voir section 9.

#### T4.6 Documentation et note de présentation (S)

- **Critères d'acceptation :**
  - [ ] README d'utilisation
  - [ ] note de présentation de la méthode avec ses limites et questions ouvertes

---

## 9. Validation

La démarche est exploratoire et **sans cas historique de référence** (D9). La validation se fera sur les événements à venir.

### 9.1 Validation technique (obligatoire)

- Tests synthétiques du jalon M2 (identité de la moyenne, retrouvailles d'un lien connu, taux de faux positifs).
- Cohérence des unités et de la grille (T0.4).

### 9.2 Validation prospective 🔶

Proposition à affiner : **utiliser les runs successifs** comme vérité provisoire, ce qui permet d'accumuler des cas chaque jour (4 runs par jour) sans attendre un événement rare.

1. À partir du run R, calculer `M(s, τ)` et une région prédicteur `A` jugée sensible à une échéance τ.
2. Au run suivant R+1, observer le changement de la moyenne d'ensemble dans `A` à τ : `Δx_A`.
3. Prédire le changement de la cible : `ΔJ_prédit = β_A · Δx_A` (régression sur la région moyennée).
4. Comparer avec le changement réel `ΔJ_observé` de la zone entre R et R+1.

**Métriques proposées :** accord de signe, corrélation entre `ΔJ_prédit` et `ΔJ_observé` sur un grand nombre de cas, comparaison à une référence « région choisie au hasard ».

Limite : R+1 n'est pas la réalité. Une validation contre les analyses (la vérité observée) pourra être ajoutée une fois la chaîne stabilisée.

### 9.3 Critère de succès global (provisoire)

L'outil est utile si, sur de nombreux cas, les régions significatives de `M` prédisent le changement de la cible mieux que des régions tirées au hasard.

---

## 10. Risques et limites

| Risque                             | Effet                                                                            | Parade                                                                       |
| ---------------------------------- | -------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| Taille d'ensemble réduite (35)     | bruit d'échantillonnage élevé (~±0,17 sur r), corrélations lointaines spurieuses | test de permutation, lecture par structures cohérentes plutôt que par pixels |
| Corrélation ≠ causalité            | sur-interprétation                                                               | formulation prudente dans toutes les sorties                                 |
| Linéarité et gaussianité supposées | peu adaptées aux événements extrêmes ou bimodaux                                 | signaler ; pistes en 4.7                                                     |
| Sous-dispersion de l'ensemble      | l'ensemble ne représente pas toute l'incertitude réelle                          | mentionner dans la note de limites                                           |
| Zone incohérente (`σ_z̄` faible)    | `M` écrasée par annulation de signes                                             | afficher `σ_z̄`, option PC1                                                   |
| Format des fichiers non documenté  | travail d'exploration plus long                                                  | jalon M0                                                                     |
| Fichiers volumineux (2–4 Go)       | téléchargements lourds                                                           | lecture par Range, cache                                                     |
| Évolution du jeu de données        | rupture de l'outil                                                               | tests de contrôle de grille, d'unités et de complétude                       |
| Délai de publication des runs      | outil lancé trop tôt                                                             | règle de détection d'un run complet (T1.1)                                   |
| Attribution                        | non-conformité de licence                                                        | mention « Source : Météo-France » sur toutes les sorties                     |

---

## 11. Questions ouvertes

1. **Pas de temps et échéances :** quelles échéances et quel pas sont réellement disponibles ? (V2, V3)
2. **Temps de calcul acceptable :** budget visé pour une exécution complète ? Proposition de départ : quelques minutes pour une zone, un t₁, ~5 échéances et B = 500 (🔶).
3. **Seuil de permutation :** par échéance ou commun à toutes ?
4. **Affichage par défaut :** `M` (comme demandé) ou `r(z̄, x)` normalisé ?
5. **Forme de la zone :** boîte lat/lon seulement, ou liste de cellules et polygones ?
6. **Cible à t₁ et échéance zéro :** inclure la carte contemporaine (τ = t₁) et l'analyse (τ = 0) ?
7. **Réseaux traités :** tous (0, 6, 12, 18) ou une partie ?
8. **Résolution 0,1° :** existe-t-il un accès ouvert pour l'ensemble ? (V9)
9. **Réutilisation du code de checkpoints existant :** adapté ou remplacé par Zarr + manifeste ?
10. **Validation prospective :** protocole de 9.2 à confirmer ou remplacer.

---

## 12. Références

- Ancell, B. & Hakim, G. J. (2007). _Comparing adjoint- and ensemble-sensitivity analysis with applications to observation targeting._ Monthly Weather Review.
- Torn, R. D. & Hakim, G. J. (2008). _Ensemble-based sensitivity analysis._ Monthly Weather Review.
- Jeu de données « PE Arpege GLOB025 », Météo-France, data.gouv.fr (Licence Ouverte 2.0).
- Projet tiers de lecture par Range des fichiers PEARP : github.com/alertesmeteo-hub/PEARP-25-km.

---

## Annexe A. Preuve de l'identité M = σ_z̄ · r(z̄, x)

Soit `x = Z'(s, τ)` et `z_c` la cible standardisée de la cellule `c`, de variance 1 (ddof = 1). Alors :

```
r(c, s, τ) = cov(z_c, x) / (σ_{z_c} · σ_x) = cov(z_c, x) / σ_x
```

Par linéarité de la covariance, avec `z̄ = Σ_c w̃_c z_c` :

```
M = Σ_c w̃_c · r(c, s, τ) = cov(Σ_c w̃_c z_c, x) / σ_x = cov(z̄, x) / σ_x = σ_z̄ · r(z̄, x)
```

avec `σ_z̄² = Σ_{c,c'} w̃_c w̃_{c'} ρ_{cc'}` où `ρ_{cc'}` est la corrélation d'ensemble entre les cellules `c` et `c'`. Si toutes les cellules sont parfaitement corrélées, `σ_z̄ = 1` et `M = r(z̄, x)`.

## Annexe B. Seuil analytique pour un test isolé

Pour une corrélation de Pearson sur N membres, avec `t` le quantile de Student à N − 2 degrés de liberté :

```
r_crit = t / √(N − 2 + t²)
```

Pour N = 35 et un niveau bilatéral de 95 % (t ≈ 2,035), `r_crit ≈ 0,33`. Ce seuil **ne corrige pas** les tests multiples : d'où la méthode de permutation de 5.4.
