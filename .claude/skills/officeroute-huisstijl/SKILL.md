---
name: officeroute-huisstijl
description: >
  Projectspecifieke huisstijl + codebase-conventies voor de OfficeRoute-apps
  (kantoorsoftware planning_oi + monteur-app). Gebruik samen met ui-ux-pro-max:
  ui-ux-pro-max levert algemene design-intelligentie, deze skill borgt de
  ECHTE tokens, componenten en valkuilen van dit project. Inzetten bij elke
  visuele wijziging: schermen/tabs, knoppen, formulieren, tabellen, e-mails,
  print-layouts of "maak dit mooier".
---

# OfficeRoute huisstijl & codebase-conventies

Aanvulling op `ui-ux-pro-max`: gebruik die voor algemene design-keuzes, en
deze skill voor de concrete waarden/patronen van dit project. Liever rustig,
licht en strak dan druk, donker of fotorijk. Bij twijfel: de rustigere variant.

## Werkwijze (altijd)
1. **Mockup eerst** bij een nieuw/ingrijpend scherm — laat beeld zien en vraag
   goedkeuring vóór je in de echte templates bouwt.
2. **Hergebruik bestaande componenten** uit `plan_base.html`/`m_base.html`.
3. **CSS-variabelen, geen losse hex.** 
4. **Verifieer**: Jinja-parse + render via Flask test-client; toon resultaat.
5. **Nederlands**, je-vorm, korte microcopy. Geen Engelse UI-teksten.

## Tokens kantoorsoftware (`plan_base.html`)
```
--accent:#0f3d3e  --accent2:#15595a   /* teal: koppen, actieve nav, primair  */
--gold:#b88a44   --gold2:#cda35a       /* goud: accenten/aantallen/gold-knop  */
--bg:#f5f7f3  --panel:#fff  --ink:#16302d  --muted:#7c8a85
--soft:#eef3ec  --line:#e6ebe4        /* zachte vulling / hairlines           */
--green/red/blue/amber/violet         /* status/pills                         */
--shadow / --shadow-sm                /* zachte teal-getinte schaduw          */
```
Font **Jost** (geladen). Koppen 700, `letter-spacing:-.02em`. Body 14px.

### Vaste componenten (klassen)
- Knoppen: `.btn` + `.primary`/`.gold`/`.ghost` (+ `.sm`). Eén primaire per context.
- `.card` (radius 18, --shadow-sm) · `.pill` (+kleur) · `.tag` (goud-label).
- Tabellen al gestyled (uppercase th, hairline-rijen, hover) — zet data in tabellen.
- `input/select/textarea` al gestyled; `.field` voor marges, `<label>` erboven.
- Lege staat: centraal kaartje met emoji + 1 zin (zie `.empty`).
- Template: `extends 'planning/plan_base.html'`; vul `pagehead`/`page_css`/`content`.
- Radius: kaarten 16–18, knoppen/inputs 11, pills 999. Genoeg witruimte.
- **Bovenbalk `.topbar` is sticky én dekkend.** Iets wat eronder moet blijven plakken
  (of een anker) offset je met `calc(var(--topbar-h, 80px) + …)`; die variabele wordt
  in `plan_base.html` gemeten (hoogte verschilt per pagina door de ondertitel).
  Nooit een eigen getal als 78px hardcoderen.
- **Lange vinkjes-/keuzelijsten in een `<details>`** met een samenvatting die het
  aantal toont; een rij van dertien chips vult anders het halve scherm. Voor
  proefmail-ontvangers bestaat dat al: macro `kiezer()` in `planning/_proefmail.html`
  (levert ook `pmChecked()`, `pmMsg()`, `pmOpen()`), hergebruiken i.p.v. namaken.
- **Checkbox in een gestylede context: expliciet `width:auto`** (+`flex:0 0 auto`).
  Formuliervelden zijn hier `width:100%`, dus zonder dat wordt het vinkje zo breed
  als de kolom. Dit is al twee keer misgegaan.

## Tokens monteur-app (`m_base.html`)
- Teal gradiënt-bovenbalk, witte content, onderbalk **icoon+label** (actief teal+streep).
- Vult mobiel `100svh` (niet `dvh`: dan beweegt de app mee met de browserbalk); telefoon-frame alleen desktop (@media min-width:430 & min-height:760).
- **Donkere modus** (`[data-theme="dark"]`): test elke kleur ook daar (geen harde hex).
- Herbruikbare realistische NL-kentekenplaat in `kies_bus.html`.

## E-mail & print
- **Klantmail altijd via `_mail_html()` / `_mail_text()`** (sjabloon `MAIL_TEMPLATE` +
  `MAIL_COLORS`): grijs buitenvlak `#efefef`, witte kaart zonder rand (radius 22),
  wordmark bovenaan, teal koppen `#053f44`, **groene** knoppen en meldingsblok
  `#2f9631`. `_brand_email(...)` is de OUDE crème/gouden stijl en mag alleen nog
  voor de handmatige klantmail en de testmail — niet voor iets wat een klant
  automatisch krijgt. Publieke klantpagina's erven van `public_base.html` met
  dezelfde kleuren.
- Nieuwe klantmail = ook een blok in `MAIL_TEXT_DEFAULTS` **én** `MAIL_PREVIEW_BLOCKS`
  (die twee moeten gelijk blijven) plus een regel in `_preview_mails()`, zodat kop en
  tekst te wijzigen zijn en er een proefmail van te sturen is.
- Print (picklijst): standalone template (niet plan_base), `@media print` verbergt
  chrome, `page-break-after:always` per document, `window.print()` opent vanzelf.

## Iconen
Kantoor: **inline SVG-lijniconen uit de sprite** in `plan_base.html` (`<use href="#ic-…"/>`,
via de macro `nicon()`) — nieuw icoon = nieuw `<symbol id="ic-…">` in die sprite.
Emoji alleen nog in tekst en lege staten, niet in de navigatie. Monteur-app: inline
SVG-lijniconen (currentColor) in de tabbalk. Geef inline `<svg>` altijd een expliciete
breedte/hoogte (of de klasse `.i`/`.ico`), anders is hij onzichtbaar.

## Jinja-/codebase-valkuilen
- **Nooit dict-key `items`** (botst met `.items()` in Jinja) → `lines`/`rows`.
- Nieuwe permissie → `PERMISSIONS` + relevante `ROLE_DEFAULTS`; `login_required("<perm>")`.
- Nieuwe tabel → `CREATE TABLE IF NOT EXISTS` in `init_db`; binaire data ` BLOB`
  (de `_xlate`-shim vertaalt naar ` BYTEA` op PostgreSQL).

## Anti-patronen
Donkere/fotorijke achtergronden, zware schaduwen, neon, gradiënt-overload, losse
hex i.p.v. tokens, meerdere primaire knoppen, Engelse microcopy, Title Case in NL,
data in losse divs i.p.v. tabel, direct in productie bouwen zonder mockup+akkoord.
