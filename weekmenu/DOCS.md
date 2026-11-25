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

## Regels

Deze regels worden in code afgedwongen, ook als AI de keuze maakt:

- alleen hoofdgerechten;
- maximumtijd op weekdagen (standaard 45 minuten), ruimer op de gekozen "ruime dagen";
- de weekmix: minimum en maximum per type (vis, vegetarisch, gevogelte, vlees, gemengd);
- woorden onder "Nooit voorstellen" en gerechten die je met "Nooit meer" blokkeerde;
- geen gerecht dat in de voorbije weken al voorgesteld werd.

Kan een regel niet gehaald worden (te weinig recepten), dan staat dat bovenaan het voorstel.

## AI (optioneel)

Zonder API-sleutel werkt alles: het type wordt met woordregels bepaald en de keuze is
willekeurig binnen de regels. Met een Anthropic API-sleutel (tabblad Configuratie van de app):

- **Slimme keuze**: één kleine oproep per voorstel, voor variatie en seizoen.
- **Onduidelijke recepten**: alleen recepten waar de regels het type niet konden bepalen,
  één keer per recept.

Het verbruik (aantal oproepen en tokens) staat op het tabblad Status. Het ophalen van
recepten gebruikt nooit AI.

## Sensoren in Home Assistant

| Sensor | Waarde | Attributen |
| --- | --- | --- |
| `sensor.weekmenu` | `voorstel`, `goedgekeurd` of `geen` | `dagen` (lijst), `te_beoordelen`, `start`, `einde`, `opmerkingen` |
| `sensor.weekmenu_vandaag` | gerecht van vandaag | `url`, `minuten`, `type` |
| `sensor.weekmenu_morgen` | gerecht van morgen | `url`, `minuten`, `type` |
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
