# Site du CCQF · Cyclo-Club Quint-Fonsegrives

Site statique publié avec GitHub Pages.

## Contenu du dépôt

- `index.html` : le site (une seule page, sections Le club, Sorties, Parcours, Saison, Adhésion, Commande maillot club, Contact, Application mobile).
- `manifest.webmanifest`, `sw.js`, `icons/` : l'application installable sur Android et iPhone.
- `data/sorties.json` : les sorties à venir et les parcours du club, lus par la page à chaque ouverture.
- `tools/maj_calendrier.py` : met à jour `data/sorties.json` depuis le calendrier Google « FRF Cyclo » et OpenRunner.
- `.github/workflows/maj-calendrier.yml` : lance cette mise à jour toutes les heures.
- `source/` : le modèle de la page (`template.html`), les images (`assets/`) et le script qui assemble le site (`build.py`).

## Modifier le site

1. Modifier `source/template.html` (textes, mise en page, styles, comportements).
2. Si besoin, changer la version dans `source/build.py` (`VERSION`, `DATE`) : elle s'affiche dans la section « Application mobile ».
3. Assembler le site, puis enregistrer et pousser les changements :

       python3 source/build.py

`index.html`, `manifest.webmanifest` et `sw.js` sont produits par ce script : ne pas les modifier à la main.

## Calendrier des sorties

Les sorties affichées viennent du calendrier Google « FRF Cyclo » (frfcyclo@gmail.com).

- `tools/maj_calendrier.py` lit le calendrier, repère les 4 prochaines sorties du dimanche et leur lien Garmin,
  retrouve la trace OpenRunner de chaque parcours (distance, dénivelé, profil, tracé) et écrit `data/sorties.json`.
  Les aperçus déjà calculés sont réutilisés : OpenRunner n'est interrogé que pour les nouveaux parcours.
- La page lit `data/sorties.json` à chaque ouverture. Si le fichier manque, elle affiche le dernier état connu.
- `.github/workflows/maj-calendrier.yml` lance la mise à jour toutes les heures et enregistre le fichier s'il a changé.

Lancer une mise à jour à la main :

    pip install -r tools/requirements.txt
    python3 tools/maj_calendrier.py

Pour qu'une sortie soit bien détaillée, son événement dans le calendrier doit s'appeler « Parcours NNN H » ou « Parcours NNN T »
(H : sens horaire, T : sens inverse) et contenir le lien Garmin. Le lien OpenRunner peut être dans l'événement
ou dans une sortie précédente du même parcours. Le script signale une trace OpenRunner dont la version diffère du calendrier.
