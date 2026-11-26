# Changelog

## 0.5.0
- Boodschappen: koppel de lijst aan Colruyt-producten (uit het assortiment van je winkel) met een voorstel voor product en aantal verpakkingen.
- Je kunt elk product en aantal aanpassen of een lijn overslaan; keuzes worden onthouden voor volgende weken.
- "Zet in winkelmand" plaatst de gekoppelde producten in je Collect&Go-winkelmand. Bestellen en betalen blijft bij jou.
- Instellingen: voorkeur (goedkoopste, Boni of bio) en kiezen met AI.

## 0.4.5
- Collect&Go: een lege winkelmand wordt herkend. De winkel antwoordt dan met een fout (HTTP 500) in plaats van een lege lijst.

## 0.4.4
- Collect&Go: een lijn uit de winkelmand verwijderen werkt nu met de aanroep die de winkel verwacht, met "aantal 0" als tweede weg.
- Winkelmand toont productnamen in plaats van artikelnummers; beschikbaarheid en prijs worden correct gelezen.

## 0.4.3
- Collect&Go: zoekt eerst in het assortiment van je eigen winkel.
- Collect&Go-test: probeert een ander testproduct wanneer Collect&Go er een weigert, en toont de reden die Collect&Go zelf geeft.

## 0.4.2
- De app heet in de zijbalk nu "Weekmenu beheer" (eigen icoon), zodat ze niet verward wordt met een dashboard "Weekmenu" voor het gezin.
- Handleiding: dashboard met de gezinsweergave op volledige hoogte.

## 0.4.1
- Collect&Go-test: het testproduct wordt nu teruggevonden en weer verwijderd, ook als het al in de winkelmand stond (dan wordt het aantal teruggezet).
- Technische details van de test zijn te kopiëren.

## 0.4.0
- Nieuwe weergave voor het gezin: het gerecht van vandaag groot in beeld, met de volgende dagen eronder. Alleen lezen, bedoeld voor een dashboard of tablet.
- Bereikbaar via de knop "Weergave voor het gezin" en rechtstreeks op poort 8100 (voor een webpagina-kaart in Home Assistant).
- Bij gerechten op het menu wordt de korte beschrijving van het recept opgehaald.

## 0.3.0
- Nieuw tabblad Colruyt: verbind de app met je Collect&Go-account (sessie plakken, geen wachtwoord), met status en tests voor lezen, zoeken en toevoegen/verwijderen.
- Dagelijkse controle van de sessie; `sensor.weekmenu_status` toont of de koppeling verbonden of verlopen is.
- Nog geen automatisch vullen van de winkelmand: dat volgt zodra de tests op jouw toestel slagen.

## 0.2.1
- Menu: onder elk gerecht staan de ingrediënten, omgerekend naar jouw aantal personen (knop om ze te verbergen).

## 0.2.0
- Nieuw tabblad Boodschappen: lijst per weekmenu, omgerekend naar het aantal personen, zelfde producten samengevoegd, per rayon gegroepeerd en afvinkbaar.
- Instellingen: aantal personen, producten die je in huis hebt, vaste producten voor elke week.
- Optioneel: lijst opschonen met AI bij het goedkeuren van een menu (één oproep); de hoeveelheden telt de app zelf op.
- Sensor `sensor.weekmenu_boodschappen`.

## 0.1.1
- Seizoensfilter: geen zomergerechten (barbecue…) in de winter en omgekeerd.
- Filter "alleen alledaagse ingrediënten": geen wild, orgaanvlees, kreeft, oesters, truffel…
- Beide woordenlijsten zijn aanpasbaar bij Instellingen.
- AI-keuze krijgt een ruimere shortlist en mijdt gerechten buiten het seizoen en feestgerechten.

## 0.1.0
- First version: catalogue crawl, rule-based typing, weekly menu proposal, settings page, Home Assistant sensors.
