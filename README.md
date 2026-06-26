# OfficeRoute — Monteur-app

Zelfstandige mobiele app voor de monteurs van Office-Interior. Aparte repo en aparte
Render-service, maar **deelt dezelfde PostgreSQL-database** met de kantoorsoftware
(planning) zodat alles live synchroon loopt.

## Functionaliteit
- Mobiele, installeerbare app (PWA): "zet op beginscherm", werkt door bij slecht bereik
- Inloggen met 2FA (dezelfde accounts als de planning)
- Route van vandaag: stops, navigatie (Google/Apple/Waze), "onderweg" en afronden
- Levering afronden met naam + handtekening + resultaat
- Live GPS delen, verlof/afspraak aanvragen, route afsluiten (privacy)

## Lokaal draaien (SQLite + mini dev-seed)
```bash
pip install -r requirements.txt
python app.py            # http://127.0.0.1:5060  (login: tom@office-interior.nl / PlanningOI2025!)
```

## Deploy op Render (gedeelde database)
1. Push deze repo naar GitHub.
2. Render → New → Web Service → koppel de repo. Start command: `gunicorn --workers 2 --threads 4 app:app`.
3. Env vars: `SECRET_KEY` (Generate) en **`DATABASE_URL` = dezelfde Internal Database URL
   als de kantoorsoftware** (zelfde PostgreSQL in dezelfde regio, Oregon).
4. Deploy. De app gebruikt automatisch PostgreSQL en deelt de planning/leveringen live.

> De database wordt beheerd door de kantoorsoftware; deze app maakt geen demodata aan in
> productie (alleen lokaal een mini dev-seed).
