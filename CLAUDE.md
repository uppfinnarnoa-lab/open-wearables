# CLAUDE.md

Please follow the guidelines and project structure defined in ./AGENTS.md

For Cursor and other agents: Refer to .cursor/rules/ for detailed configuration.

## Var det här körs

Forken är driftsatt på **https://vadsomhelst.helgars.se** —
`noa@192.168.242.2`, nginx + systemd, **inte** Docker. `docker-compose.yml` i
repot är upstreams utvecklingsuppsättning och används inte i drift.

| Sak | Var |
|---|---|
| Checkout | `/home/noa/open-wearables/app` (backend i `app/backend`) |
| Konfiguration | `backend/config/.env` — ingen `EnvironmentFile=` i units |
| Units | `openwearables-api`, `openwearables-worker`, `openwearables-beat` |
| Postgres / Redis | lokala, `postgresql@16-main` respektive `redis-server` |
| Git-remote på servern | heter `fork`, inte `origin` |

`journalctl -u openwearables-worker` fungerar utan sudo. Det är snabbaste vägen
att se vad en synk faktiskt gjorde — API:t svarar så fort Celery-tasken är
köad, så en stoppad worker ser exakt ut som en lyckad synk utan data.

**Fråga Noa om lov före varje kommando som körs på servern**, varje gång.
VPN:et kopplar Noa upp manuellt, och `sudo` kräver lösenord.
