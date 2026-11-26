# Weekmenu

Stelt elke week een menu van 7 avondmalen voor uit de recepten van Dagelijkse kost.
De pagina in de zijbalk toont het voorstel; daar keur je het goed, vervang je een dag of
pas je de regels aan.

Persoonlijk gebruik. De recepten blijven op dagelijksekost.vrt.be; de app bewaart alleen
naam, tijd, type en ingrediënten om te kunnen kiezen, en linkt naar het recept zelf.

## Eerste keer

1. Start de app en zet "Weergeven in zijbalk" aan.
2. Open **Weekmenu** en klik **Zelftest starten**. Die haalt de index en 5 recepten op en
   toont wat gelezen kon worden (naam, tijd, type gerecht, ingrediënten).
3. Klik **Catalogus opbouwen**. Dat haalt alle recepten één keer op, met een pauze van
   1 seconde ertussen (ongeveer een uur). Hier komt geen AI aan te pas.
4. Klik **Voorstel voor volgende periode**.

Daarna loopt alles vanzelf: elke maandag om 06:00 haalt de app de nieuwe recepten op en
maakt een voorstel voor dinsdag tot en met maandag. Dag, uur en startdag stel je in.

Onder elk gerecht staan de ingrediënten, omgerekend naar jouw aantal personen. Met **Verberg
ingrediënten** klap je ze dicht.

## Weergave voor het gezin

Naast het beheer is er een rustige pagina om te tonen wat er op het menu staat: het gerecht van
vandaag groot in beeld (foto, naam, bereidingstijd, één zin uitleg) en daaronder de volgende dagen.
Tik op een andere dag om die te bekijken; na een minuut springt de pagina terug naar vandaag.
De pagina is alleen-lezen: er kan niets mee gewijzigd worden en ze toont geen instellingen.

- In de app: de link **Weergave voor het gezin** bovenaan.
- Op een dashboard: voeg een kaart **Webpagina** toe met het adres van Home Assistant en poort 8100:

```yaml
type: iframe
url: http://192.168.x.x:8100/
aspect_ratio: 16:10
```

  Voor een tablet aan de muur werkt een aparte dashboardweergave van het type "Paneel" met alleen
  deze kaart het best. Voeg `?theme=dark` of `?theme=light` toe aan het adres om het thema vast te
  zetten; anders volgt de pagina het toestel.
- Poort 8100 staat bij de app onder Configuratie → Netwerk. Ze is alleen binnen je thuisnetwerk
  bereikbaar en je kunt ze daar uitschakelen. Open je Home Assistant via https, dan blokkeert de
  browser een http-kaart; gebruik dan het lokale http-adres van Home Assistant.

## Regels

Deze regels worden in code afgedwongen, ook als AI de keuze maakt:

- alleen hoofdgerechten;
- maximumtijd op weekdagen (standaard 45 minuten), ruimer op de gekozen "ruime dagen";
- de weekmix: minimum en maximum per type (vis, vegetarisch, gevogelte, vlees, gemengd);
- woorden onder "Nooit voorstellen" en gerechten die je met "Nooit meer" blokkeerde;
- het seizoensfilter: van oktober tot maart geen zomergerechten (barbecue, gazpacho…), van mei tot
  september geen wintergerechten (stoofpot, stamppot…);
- alleen alledaagse ingrediënten: geen wild, orgaanvlees, kreeft, oesters, truffel en dergelijke;
- geen gerecht dat in de voorbije weken al voorgesteld werd.

Beide filters kun je uitzetten, en de woordenlijsten pas je aan onder Instellingen → Woordenlijsten
aanpassen. Een woord raakt ook het meervoud (`oester` raakt oesters, niet oesterzwam); `kreeft*` raakt ook
samenstellingen en `*truffel*` raakt het woord overal.

Kan een regel niet gehaald worden (te weinig recepten), dan staat dat bovenaan het voorstel.

## Boodschappen

Het tabblad **Boodschappen** toont de lijst voor het gekozen weekmenu:

- hoeveelheden zijn omgerekend van het aantal personen van het recept naar jouw aantal personen;
- hetzelfde product uit verschillende recepten wordt samengeteld (ui, uien en ajuin zijn één product);
- de lijst is gegroepeerd per rayon en je kunt afvinken wat je hebt;
- producten die je normaal in huis hebt (zout, peper, olijfolie, boter…) staan apart onderaan;
- "Altijd toevoegen" zet vaste producten elke week op de lijst;
- "Toon details" laat per product zien uit welke recepten en regels het komt;
- "Kopieer als tekst" zet de open producten op het klembord.

De lijst volgt het menu: vervang je een dag, dan verandert de lijst mee. Aantal personen en de
lijsten stel je in bij Instellingen → Boodschappen.

## Colruyt / Collect&Go

Het tabblad **Colruyt** verbindt de app met je Collect&Go-account. Colruyt biedt geen publieke
toegang; de app gebruikt dezelfde adressen als de website, met de sessie van een browser waarin
je zelf inlogde. Dat is niet officieel ondersteund en kan zonder waarschuwing stoppen met werken.

Verbinden (op een computer):

1. Open collectandgo.be, log in met je Xtra-account (Colruyt vraagt zelf zijn sms-code) en kies je winkel.
2. Druk op F12 (Mac: ⌥⌘I) → tabblad Netwerk → klik op een verzoek naar collectandgo.be → rechtsklik →
   Kopiëren → Kopiëren als cURL.
3. Plak het in het tabblad Colruyt en klik **Verbind**.

De app bewaart alleen de sessiecookies (nooit je wachtwoord of sms-code) en toont ze nergens.
Een sessie blijft dagen tot weken geldig; de app controleert ze één keer per dag en meldt het
wanneer je opnieuw moet verbinden.

Met de testknoppen zie je of de koppeling vanaf jouw toestel werkt: winkelmand lezen, het
assortiment doorzoeken, en één testproduct toevoegen en weer verwijderen. Bestellen en betalen
doet de app nooit; dat doe je zelf in Collect&Go.

## AI (optioneel)

Zonder API-sleutel werkt alles: het type wordt met woordregels bepaald en de keuze is
willekeurig binnen de regels. Met een Anthropic API-sleutel (tabblad Configuratie van de app):

- **Slimme keuze**: één kleine oproep per voorstel, voor variatie en seizoen.
- **Onduidelijke recepten**: alleen recepten waar de regels het type niet konden bepalen,
  één keer per recept.

- **Boodschappenlijst opschonen**: één oproep wanneer je een menu goedkeurt. AI bepaalt alleen welke
  regels hetzelfde product zijn en in welk rayon ze horen; optellen doet de app zelf, en geen enkele
  regel kan daarbij verloren gaan.

Het verbruik (aantal oproepen en tokens) staat op het tabblad Status. Het ophalen van
recepten gebruikt nooit AI.

## Sensoren in Home Assistant

| Sensor | Waarde | Attributen |
| --- | --- | --- |
| `sensor.weekmenu` | `voorstel`, `goedgekeurd` of `geen` | `dagen` (lijst), `te_beoordelen`, `start`, `einde`, `opmerkingen` |
| `sensor.weekmenu_vandaag` | gerecht van vandaag | `url`, `minuten`, `type` |
| `sensor.weekmenu_morgen` | gerecht van morgen | `url`, `minuten`, `type` |
| `sensor.weekmenu_boodschappen` | aantal nog te kopen producten | `items` (naam, hoeveelheid, rayon, afgevinkt) |
| `sensor.weekmenu_status` | `ok`, `bezig` of `fout` | aantallen, `laatste_fout` |

Kaart voor het dashboard:

```yaml
type: markdown
title: Weekmenu
content: >-
  {% if state_attr('sensor.weekmenu', 'te_beoordelen') %}**Nieuw voorstel te beoordelen**

  {% endif %}{% for d in state_attr('sensor.weekmenu', 'dagen') or [] %}**{{ d.dag[:2] }}**
  {% if d.url %}[{{ d.gerecht }}]({{ d.url }}){% else %}–{% endif %}{% if d.minuten %} · {{ d.minuten }} min{% endif %}

  {% endfor %}
```

## Gegevens

Alles staat in `/data/weekmenu.db` van de app en gaat mee in de Home Assistant-back-up.
