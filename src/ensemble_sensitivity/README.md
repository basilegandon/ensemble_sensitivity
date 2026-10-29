# Flux des données et commandes

Ce diagramme suit `src/ensemble_sensitivity/main.py` et les commandes
actuellement implémentées de récupération et de génération de cartes de
contrôle. Les rectangles représentent des opérations, les losanges des
conditions et les formes arrondies des entrées, des sorties ou des fichiers.

```mermaid
flowchart TB
    arguments((Arguments de la ligne de commande)) --> main["main() : analyser les arguments<br/>et configurer les journaux"]
    main --> commande{"Commande ?"}
    commande -->|aucune| accueil["Journaliser le message d'accueil"]
    commande -->|fetch| options_fetch((Options de récupération))
    commande -->|plot| options_plot((Dossier du run,<br/>échéances, membres et sortie))

    subgraph fetch["Récupération des données — wcs_retrieval.py"]
        options_fetch --> jeton["Lire le jeton d'accès"]
        jeton --> capabilities["Interroger les capacités du service WCS"]
        capabilities --> candidats((Couvertures Z500 annoncées, de la plus récente à la plus ancienne))
        options_fetch --> echeances["Déterminer les échéances requises à partir de la cible et du pas"]
        candidats --> reste{"Reste-t-il<br/>un candidat ?"}
        echeances --> reste
        reste -->|oui| champs["Pour ce candidat, traiter chaque échéance<br/>et les membres 0 à 34"]
        reste -->|non| echec_fetch((Aucun run complet : RetrievalError))
        champs --> obtenir["Réutiliser un champ en cache valide ou le demander au WCS"]
        obtenir --> valider["Valider chaque message GRIB : run, échéance, membre, Z500, niveau, unités et grille"]
        valider --> enregistrer["Enregistrer le champ et<br/>mettre à jour le manifeste"]
        enregistrer --> complet{"Tous les champs requis<br/>sont-ils valides ?"}
        complet -->|oui| run_complet((Run complet : champs et manifest.json))
        complet -->|non| reste
    end

    subgraph plot["Génération des cartes de contrôle — visualization.py"]
        options_plot --> manifeste["Lire le manifeste et<br/>exiger un run complet"]
        manifeste --> selection["Valider les échéances et membres demandés,<br/>puis retrouver les champs"]
        selection --> lire["Lire et valider le champ GRIB sélectionné"]
        lire --> decoder["Décoder Z500 et vérifier les valeurs et la grille globale"]
        decoder --> recentrer["Recentrer les longitudes<br/>pour la carte"]
        recentrer --> image["Tracer une carte globale en projection Plate Carrée"]
        image --> png((Carte PNG))
        png --> provenance["Écrire le fichier JSON de provenance<br/>et y consigner le SHA-256 du PNG"]
        provenance --> sorties((Une carte PNG et son fichier JSON par sélection))
    end

    run_complet --> manifeste

    subgraph commun["Services partagés"]
        reseau["Requêtes HTTPS avec limitation du débit<br/>et nouvelles tentatives"]
        eccodes["ecCodes : validation et décodage des messages GRIB"]
    end
    capabilities -.-> reseau
    obtenir -.-> reseau
    valider -.-> eccodes
    decoder -.-> eccodes

    echec_fetch --> erreur["main() journalise l'erreur et quitte avec le code 1"]
    sorties --> resultats((Chemins des cartes produites))
    manifeste -. échec de lecture ou de validation .-> erreur

    note["À venir : sélection de la zone cible, statistiques d'ensemble, cartes de sensibilité, test de significativité et export des résultats."]
    run_complet -. étape distincte, non implémentée .-> note
```

La commande `fetch` récupère les champs demandés et ne renvoie qu’un run pour
lequel tous les champs requis ont été validés. Elle réutilise les champs en
cache lorsqu’ils sont valides et conserve l’état du run dans son manifeste.
La commande `plot` lit ce manifeste et les champs sélectionnés pour produire
des cartes de contrôle individuelles ; elle ne calcule pas de carte de
sensibilité. En cas d’erreur pendant `fetch` ou `plot`, `main()` la journalise
et termine avec le code 1.

Les étapes statistiques décrites dans le README principal restent à
implémenter. Le diagramme ne les présente donc pas comme des fonctions déjà
disponibles.
