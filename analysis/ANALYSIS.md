# Cadrage de l’analyse

Cette note rassemble les choix discutés pour la première carte de sensibilité.
Elle sera mise à jour quand les essais permettront de répondre aux questions
ouvertes.

## Première méthode

- Variable : géopotentiel à 500 hPa (Z500).
- Run : un run à la fois, le dernier run complet par défaut.
- Échéance cible : choisie parmi celles disponibles.
- Échéances antérieures : une seule carte affichée à la fois ; pas initial de
  24 h.
- Zone cible : cercle de rayon réglable en kilomètres, centré sous le curseur
  sur la carte cible.
- Cible par membre : moyenne spatiale du Z500 dans le cercle, pondérée par
  l’aire des mailles.
- Carte cible : moyenne d’ensemble en isolignes et écart-type des 35 membres
  en fond de couleur.
- Carte de sensibilité : coefficient de Pearson, calculé sur les 35 membres
  entre la cible par membre et le Z500 à chaque point de l’échéance antérieure.
  Les signes positif et négatif sont conservés.
- Significativité : aucun test dans la première phase.
- Usage : exploration locale, sans usage opérationnel.

## État d’implémentation

Implémenté et testé (données synthétiques) : zone circulaire, moyenne pondérée
cos(latitude), cible par membre, corrélation de Pearson signée sur les membres,
avec mémoire bornée (un champ à la fois). La commande `sensitivity` génère
une carte PNG (colormap divergente, zone tracée) et son JSON sous `<uuid>/maps`.
Sans `--predictor-lead`, `sensitivity` calcule le triangle des couples
(cible, prédicteur) : pour la cible T, une carte par échéance disponible du
manifeste strictement antérieure à T, puis la cible devient l’échéance
disponible précédente, jusqu’à épuisement (la plus ancienne n’est jamais
cible). Le couple prédicteur = cible est exclu (corrélation triviale de 1).
La cible par membre est calculée une fois par cible ; un seul champ est en
mémoire à la fois ; une barre de progression `tqdm` suit les couples.
Application locale (`ensemble-sensitivity app`, Panel + Bokeh, option choisie
par l’utilisateur) : `app/engine.py` (calcul à la volée, cache LRU de 2
échéances), `app/model.py` (sélection validée, données des cartes, sans
interface) et `app/view.py` (deux cartes liées, clic = centre de zone).
Non implémenté : précalcul, survol (clic seul), significativité.

Choix de conception : rayon limité à ]0, π·R] km (R = 6371,0088 km) ; mailles
du pôle pondérées par cos(±90°) ≈ 0 ; une maille hors de la zone n’est jamais
lue pour la cible, donc une valeur non finie y est ignorée.

## Coût du précalcul (mesures)

**Données synthétiques** : pas de jeton ni de données réelles. Champs lisses
(ondes + bruit) 1440×721, 35 membres, vrai GRIB2 simple 16 bits encodé avec
ecCodes. Machine : Intel i5-10310U, 7,7 Go de RAM, un seul processus.
Reproductible : `uv run python scripts/benchmark_precompute.py`.
Les tailles compressées sont pessimistes (bruit blanc sur la carte ; une vraie
carte de corrélation est plus lisse).

| Mesure | Valeur |
|---|---|
| Décodage d’un champ, avant (`decode_arrays`, valeurs + lat/lon) | 0,18 à 0,30 s, pic alloué 37 Mo |
| Décodage d’un champ, après (`decode_values`, valeurs seules) | 0,010 à 0,024 s, pic alloué 12 Mo |
| Taille d’un GRIB2 (16 bits) | 2,08 Mo |
| Carte, Pearson en flux (35 champs en RAM) | 0,82 s, pic alloué 84 Mo |
| Carte, produit matriciel, anomalies recalculées | 0,37 s |
| Carte, produit matriciel, anomalies/normes en cache (float32) | 0,016 s |
| Cible par membre (35 champs), 250 à 2000 km | 0,042 à 0,051 s |
| Poids de zone (par centre/rayon) | 0,032 s |
| Rendu PNG cartopy d’une carte pleine grille | 0,69 s (599 Ko) |

Optimisation faite : les axes lat/lon (≈ 90 % du décodage) sont validés une seule
fois par `run_field_loader` (premier champ, `decode_arrays`) ; les champs suivants
utilisent `decode_values` (gain ≈ 12× en temps, pic mémoire 37 → 12 Mo, aucun
stockage disque supplémentaire, valeurs identiques, mêmes contrôles de dimensions et de
finitude ; `plot` garde la validation complète à chaque champ). Une pile de 35 champs
float32 pèse 145 Mo ; les 34 échéances (3 h à 102 h) en RAM : ≈ 4,9 Go, trop
pour cette machine.

Taille d’une carte stockée (1440×721) :

| Encodage | Taille |
|---|---|
| float32 brut | 4056 Ko |
| float32 + zlib | 3692 Ko |
| float16 + zlib | 1716 Ko |
| int8 (pas 1/127) + zlib | 842 Ko |
| int8 + zlib, grille 0,5° | 215 Ko |
| int8 + zlib, grille 1° | 55 Ko |

Nombre de couples pour 34 échéances (3 h à 102 h) : 34·33/2 = 561 cartes par
couple (centre, rayon) ; 33 cibles. Coût par (centre, rayon), champs en RAM :
33·0,045 s (cibles) + 561·0,016 s (cartes) ≈ 10,5 s, hors décodage.
Sans cache, la commande `sensitivity` actuelle coûte par carte ≈ 6,2 s de
décodage (35 champs, ancien chemin ; ≈ 0,5 s depuis l’optimisation) + 0,82 s + 0,69 s de rendu ≈ 7,7 s, soit ≈ 72 min pour un
triangle complet.

Extrapolation, 4 rayons, 561 couples d’échéances, un processus :

| Pas des centres | Centres | (centre, rayon) | Calcul ≈ 10,5 s chacun | Stockage int8+zlib 1° (55 Ko/carte) |
|---|---|---|---|---|
| 5° | 2 664 | 10 656 | ≈ 31 h | ≈ 329 Go |
| 2° | 16 380 | 65 520 | ≈ 191 h | ≈ 2,0 To |
| 1° | 65 160 | 260 640 | ≈ 760 h | ≈ 8,0 To |

Stratégies comparées :

| Stratégie | Calcul | Stockage | Latence d’interaction | Avis |
|---|---|---|---|---|
| Précalcul complet | 31 h à 5° ; ≥ 190 h à 2° | 0,3 à 8 To | lecture disque (≈ ms) | Rejeté : volume et temps disproportionnés, résolution des centres imposée. |
| Précalcul grossier (5°, 1 rayon) + calcul à la volée | ≈ 8 h | ≈ 80 Go à 1° de grille | lecture, puis raffinement à la volée | Complexe ; le gain est faible face à la volée. |
| Tout à la volée avec cache | 0 à l’avance | ≈ 290 Mo RAM (2 échéances × 35 champs float32) | zone 0,032 + cible 0,045 + carte 0,016 ≈ 0,09 s | Retenu : centre, rayon et échéances libres. |

Conditions de la volée : garder en RAM les piles de la cible et du prédicteur
sélectionnés et les anomalies/normes du prédicteur en cache (0,016 s par carte
au lieu de 0,82 s) ; changer d’échéance coûte un chargement de 35 champs
(≈ 0,35 à 0,85 s avec le chemin rapide ; ≈ 6 à 10 s avec l’ancien décodage). Le calcul tient donc sous
100 à 200 ms, mais le rendu cartopy (0,69 s) ne convient pas : l’application
doit mettre à jour une image (`imshow`/glyphe image) sans redessiner le fond.
La cible peut encore être accélérée en limitant le masque à la bande de
latitudes du cercle (non mesuré).

## Options d’application locale (comparaison ; Panel + Bokeh retenu et implémenté)

Critères : deux cartes liées (zoom/pan synchronisés), curseur → recalcul
< 100–200 ms, Python 3.14/uv, taille des dépendances. La disponibilité des
roues Python 3.14 de Panel 1.10 et Bokeh 3.10 a été vérifiée
(`uv pip install --dry-run`).

| Option | Avantages | Inconvénients |
|---|---|---|
| Panel + Bokeh | Serveur Python local, calcul dans le même processus ; images float32 envoyées en binaire ; axes liés natifs entre figures ; curseurs côté Python | Fond de carte à tracer en lignes Bokeh (géométries cartopy converties) ; dépendances moyennes (bokeh, panel, param) |
| Dash (Plotly) | Écosystème mature, callbacks simples, bon rendu web | Heatmap 1M points trop lourde en JSON : sous-échantillonnage nécessaire ; aller-retour HTTP par curseur ; liaison des deux cartes via callbacks ; dépendances plotly + flask volumineuses |
| Streamlit | Très rapide à écrire | Réexécute le script à chaque interaction ; pas de liaison native de deux cartes ; latence incompatible avec 100–200 ms |
| Qt + matplotlib (PySide6) | Aucune sérialisation, cartopy conservé, axes partagés natifs, mise à jour d’image par blit | Dépendance lourde (PySide6 > 150 Mo) ; application de bureau, pas de partage ; roues 3.14 à vérifier |

Recommandation : **Panel + Bokeh** (équilibre latence, cartes liées,
poids de dépendances, un seul processus Python), avec Qt + matplotlib en
solution de repli si le fond de carte cartopy est prioritaire. **Décision de
l’utilisateur : Panel + Bokeh.**

## Application Panel + Bokeh : mesures du moteur à la volée

Données synthétiques (35 membres, grille 1440 × 721, i5-10310U, 7,7 Go ; aucun
jeton ni donnée réelle). `SensitivityEngine` garde par échéance les anomalies
centrées float32 (≈ 145 Mo), moyenne, écart-type et inverse des normes
(≈ 160 Mo par échéance) ; cache de 2 échéances ≈ 300 Mo (cible ≈ 290 Mo).

| Action | Mesure |
|---|---|
| Déplacement du centre ou du rayon (échéances en cache) | 0,087 à 0,101 s |
| Moyenne et écart-type de la cible (en cache) | ≈ 10 µs |
| Changement de prédicteur ou de cible (charge 35 champs, centrage) | ≈ 1,8 à 1,9 s, dont la génération aléatoire synthétique des champs |
| Premier calcul (2 échéances à froid) | ≈ 3,1 s |
| Pic d’allocations numpy (tracemalloc) | ≈ 368 Mo (transitoires de centrage inclus) |

L’objectif d’interaction ≈ 0,1 s est atteint ; la RAM visée (≈ 290 Mo) est
dépassée d’≈ 80 Mo en pic transitoire. L’affichage envoie des images
sous-échantillonnées (pas de 0,5°) et des isolignes tracées avec contourpy,
sans redessiner de fond cartopy.

## Questions ouvertes

- Le survol (hover) ne définit pas encore le centre : seul le clic le fait.
- Les côtes viennent de cartopy (110 m) ; sans données Natural Earth en cache
  ni réseau, elles sont omises (avertissement).
- Comment précalculer les cartes pour garder une interaction fluide, et quel
  volume de calcul et de stockage cela représente-t-il pour les centres, les
  rayons et les couples d’échéances possibles ?
- Quelle stratégie de précalcul réduit suffisamment le calcul pendant
  l’interaction sans rendre les résultats trop grossiers ?
- Quelle méthode de significativité utiliser dans une phase ultérieure, compte
  tenu du nombre de points cartographiés ?
- L’écart-type suffit-il pour montrer la dispersion, ou d’autres indicateurs
  seront-ils utiles ?
- Quel support d’application locale et quelle organisation des processus
  conviennent le mieux ? Dash n’est qu’une piste.
- Quelles limites et quels réglages donner au rayon de la zone ?
