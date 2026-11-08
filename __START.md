# start

Starte Remiqora im Terminal auf Port 9010. Dort laufen Oberfläche und API zusammen.
```bash
cd /Users/rupertgermann/AI/remiqora/backend
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 9010
```

Öffne Remiqora im Browser. Mit Ctrl+C stoppst du den Server.

Der Befehl nutzt den vorhandenen Frontend-Build. Falls du das Frontend geändert hast, führe vorher diesen Befehl aus:
```bash
cd /Users/rupertgermann/AI/remiqora/frontend
npm run build
```
    