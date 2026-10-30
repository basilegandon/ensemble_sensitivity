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

## Questions ouvertes

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
