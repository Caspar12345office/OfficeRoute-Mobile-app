# Projectcontext voor Claude

Dit bestand is de projectkennis voor elke Claude-sessie, ook in de cloud of vanaf de
iPhone. Werk de sectie "Stand van zaken" bij aan het eind van een sessie waarin iets
veranderde. **Zet hier nooit wachtwoorden, sleutels, tokens of database-adressen in:
die staan alleen als environment variable in Render.**

## Wie en wat

Office-Interior (kantoormeubilair, Breda). Caspar is de beheerder en opdrachtgever.
Vier Flask-apps, elk met een eigen GitHub-repo (organisatie `Caspar12345office`) en een
eigen Render-service in Frankfurt, op **één gedeelde PostgreSQL-database**:

| App | Repo | Hoofdbestand | Live |
|---|---|---|---|
| Kantoor (OfficeRoute) | `Planning-O-I` | `planning_oi.py` | https://planning-o-i-fra.onrender.com |
| Monteur-app | `OfficeRoute-Mobile-app` | `monteur.py` | https://officeroute-monteur-fra.onrender.com |
| Magazijn-app | `OfficeRoute-Magazijn-app` | `magazijn.py` | https://officeroute-magazijn-fra.onrender.com |
| OfficeHub + Bedrijfsbank + Argus | `OfficeHub` | `hub.py`, `kennisbank.py`, `argus.py` | https://www.office-interiorhub.nl |

- "OR" in de chat betekent OfficeRoute. In UI, code en commits altijd voluit "OfficeRoute".
- Gedeelde `users`-tabel, 2FA via een authenticator-app (TOTP). Zonder gekoppelde 2FA
  koppelt wie als eerste inlogt de authenticator.
- Single sign-on: de hub-tegels lopen via `/go`, dat een kort geldig HMAC-token meegeeft;
  elke app verifieert dat met dezelfde env `SSO_SECRET`.
- De kantoor-app vult de database (seed); de andere apps seeden alleen lokaal op SQLite.
- **De monteur-app die monteurs echt gebruiken is `OfficeRoute-Mobile-app`.** De
  `/monteur`-view in `Planning-O-I` is iets anders; daar is eerder per ongeluk in gebouwd.

## Werkafspraken met Caspar

- Antwoorden en UI-teksten in het Nederlands, je-vorm, kort.
- **Nooit een em dash of en dash** (de lange en halflange streepjes), ook niet in code,
  commits, mails of UI. Gebruik een komma, dubbele punt, haakjes of "tot".
- Geen emoji in de UI of navigatie.
- Lichte huisstijl: teal `#0f3d3e`, goud `#b88a44`, lichte achtergrond, witte panelen.
  Geen donkere of fotorijke ontwerpen. Zie de skill `officeroute-huisstijl` in
  `.claude/skills/`.
- **Bij een nieuw of ingrijpend scherm eerst een mockup laten zien** en akkoord vragen.
  Bij creatieve opdrachten (animatie, "maak iets moois") eerst om een concrete
  referentie vragen in plaats van concepten te gokken.
- Caspar houdt van rustige, continue beweging (zoals het deeltjesweb op de hub), niet
  van drukte.
- Commitberichten in het Nederlands: korte titel, daaronder waarom.
- Eerlijk melden wat wel en niet getest is.

## Deploy

Commit en push naar `main`, Render bouwt dan automatisch (1 tot 8 minuten). Controleer
met `curl <app-url>/version`: dat geeft de korte commit-hash die live draait.
- **`main` is direct live voor alle collega's.** Werk je in een cloudsessie op een eigen
  branch, vraag Caspar dan of hij wil samenvoegen naar `main`.
- Secrets en instellingen staan alleen als environment variable in Render. Een env-var
  via de Render-API zetten herstart de service niet: daarna handmatig deployen.

## Lokaal testen

```
python -m venv .venv && .venv/bin/pip install -r requirements.txt
PLANNING_OI_DB_PATH=/tmp/test.db python -c "..."   # SQLite met demo-seed
```
- Test met de Flask test-client. Inloggen in een test: `session["p_user_id"] = <id>`
  via `client.session_transaction()`.
- Kantoor: `VELOCITY_DISABLE=1` zet de Radius-achtergrondtaak uit tijdens tests.
- Templates worden niet automatisch herladen: herstart de server na een wijziging.

## Valkuilen die eerder echt tijd kostten

- SQLite naar PostgreSQL loopt via de `_xlate`-shim: `?` wordt `%s` (buiten
  string-literals), `GROUP_CONCAT` wordt `string_agg`, `BLOB` wordt `BYTEA`. Tabellen
  zonder `id`-kolom horen in `_NO_ID_TABLES`. Houd de shim in alle apps gelijk.
- `ALTER TABLE ... ADD COLUMN x INTEGER DEFAULT 0` vult bestaande rijen met 0, niet
  NULL. Eenmalige backfill dus via een vlag in `settings`.
- Een nieuwe standaardtekst in `MAIL_TEXT_DEFAULTS` doet niets als de oude tekst al in
  `settings` staat.
- Resend (mail): elke aanroep met een eigen `User-Agent`, anders weigert Cloudflare met
  403 / error 1010. "E-mails écht versturen" staat bewust uit tot Caspar het aanzet.
- Mobiel: `100svh`, niet `dvh` (met dvh beweegt de hele app mee met de browserbalk).
- Inline `<svg>` altijd een expliciete breedte en hoogte geven, anders onzichtbaar.
- `loading="lazy"` in een blok met `display:none` laadt nooit, zonder foutmelding.
- HTML krijgt `Cache-Control: no-store`; iOS en de PWA hielden anders oude versies vast.
- Jinja: nooit een dict-key `items` (botst met `.items()`).
- Een class `app` botst met de globale `.app{min-height:100vh}`.
- Verse Render-build: de psycopg-pool valideren met snelle fallback; geen `--preload`.
- Web-apps krijgen op de achtergrond geen GPS (Waze, scherm op slot). Daarom komen de
  busposities uit Radius Velocity (in de kantoor-app). Caspar wil geen App Store-app.

## Stand van zaken (bijwerken)

Laatst bijgewerkt: 2026-10-12.

Uitrol naar ongeveer 50 collega's in vier stappen:
1. Klaarmaken: **af** (Sentry-code in alle apps, versiecode van de inlogpagina's, onaffe
   hub-tegels alleen voor beheerders, achtergrondtabbladen pollen niet + korte cache,
   controle op startwachtwoorden en 2FA op de Gebruikers-pagina).
2. Echte gegevens invullen (bussen, monteurs, montagetijden, accounts): Caspar.
3. Proefgroep 1 tot 2 weken, monteurfuncties naar de echte monteur-app overzetten
   (stepper, GPS-startplicht, meten), "mijn account"-pagina.
4. Uitrol naar iedereen, oude Oregon-database opruimen.

Gedaan op 2026-10-12: echte routes en reistijden (Google, schatting zonder sleutel);
verlof goedkeuren als recht `approve_leave` + "Mijn aanvragen" in monteur- en magazijn-app;
Radius Velocity werkt tegen de echte API (refresh token inwisselen, Telematics-account,
bussen op de dashboardkaart, kilometers per voertuig als schatting); donkere modus kantoor
(keuze onderin het menu); monteurs toevoegen + automatische monteurskaart bij rol Monteur.

Open:
- Google Maps-sleutel nog niet ingevuld (Koppelingen > Google Maps): tot dan is reistijd een
  schatting met "≈". Niet getest met een echte sleutel.
- Radius: Caspar moet het refresh token zelf bij Koppelingen zetten en de 9 bussen met
  kenteken onder Bussen zetten (km tellen ook zonder, maar CO2 alleen voor bekende bussen).
- Na een deploy geeft de kantoor-app soms één keer een 500 op /login (oorzaak onbekend;
  traceback staat in Sentry of Koppelingen > Live status > Backend Python).
- Mogelijk: kantoor rekent met servertijd (Render = UTC); live aankomsttijden kunnen dan
  2 uur afwijken. Nog onderzoeken.
- Bedrijfsbank: documenten bij register-items alleen als download, nog geen "bekijken".
- `SENTRY_DSN` nog zetten op monteur-, magazijn- en hub-service.
- 12 accounts staan nog op het gedeelde startwachtwoord.

Verbeterlijst (nog niets gekozen): testomgeving, eigen subdomeinen, "mijn account"-pagina,
één kleurpalet in alle apps, eigen 404/500-pagina's, "wat is er nieuw"-melding, lege
staten, handleiding bijwerken, UptimeRobot, backup-terugzettest.

## Deze repo: monteur-app

- `monteur.py`: blueprint `planning` op de root. Hoofdscherm `/monteur`
  (`templates/planning/monteur_app.html`), basis `m_base.html` in de Cream Editorial-stijl
  (crème, warme kaarten, gouden haarlijnen), donkere modus via `[data-theme="dark"]`.
- De busvloot staat in `FLEET` (kentekens). De monteur kiest 's ochtends via `/kies-bus`;
  dat komt in de tabel `bus_choices`, die de kantoor-app gebruikt voor de Radius-busposities.
- GPS: `watchPosition` in `monteur_app.html` stuurt naar `/api/location`; stopt zodra de
  app op de achtergrond staat.
- Navigeren via Google Maps en Waze (bewust geen Apple Maps). Begroeting "Hoi <naam>" in Jost.
- Klantmail "monteur is er bijna" is een kopie van het kantoorsjabloon: houd beide gelijk.
  De verwachte aankomst daarin komt van `_drive_minutes` (Google met verkeer, vanaf de
  positie die de telefoon meestuurt of de laatste bekende), afgerond op 5 minuten.
- Service worker: HTML altijd van het netwerk; de app checkt elke minuut `/version` en
  herlaadt zichzelf na een deploy.
