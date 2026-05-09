# Kalshi Bot

Algorithmic trading bot for [Kalshi](https://kalshi.com) prediction markets.

**Status:** Semana 1 de roadmap (infrastructure + data layer)
**Deploy target:** Coolify on DigitalOcean VPS
**Bankroll:** $300 inicial → $2,500 máximo (escalonado)

---

## Quick start

### Local development

```bash
# Clone
git clone git@github.com:USERNAME/kalshi-bot.git
cd kalshi-bot

# Setup
python3.12 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

# Generar llaves RSA
bash scripts/generate_keys.sh

# Subir public key a Kalshi (Settings → API Keys → DEMO environment)

# Configurar .env
cp .env.example .env
# Editar .env con tu KALSHI_API_KEY_ID

# IMPORTANTE para local: ajustar paths absolutos de container a relativos
# En .env modificar:
#   KALSHI_PRIVATE_KEY_PATH=./config/kalshi_private_key.pem
#   DATABASE_URL=sqlite:///./data/trades.db
mkdir -p data logs

# Smoke test
python -m scripts.smoke_test

# Run tests
pytest

# Run bot localmente (modo data capture sin trading)
python -m src.runner
```

### Production deploy (Coolify)

Ver [docs/DEPLOY_COOLIFY.md](docs/DEPLOY_COOLIFY.md) para guía completa.

Resumen:
1. Push código a GitHub privado
2. Generar llaves RSA localmente (NUNCA commits)
3. Subir public key a Kalshi DEMO
4. Crear app en Coolify apuntando al repo
5. Configurar env vars + montar private key como volume secret
6. Deploy → bot arranca en modo data capture

---

## Arquitectura

```
┌─────────────────────────────────────────────────┐
│ Coolify on VPS                                  │
│  └─ kalshi-bot container                        │
│       ├─ Health server (FastAPI :8080)          │
│       ├─ Production runner (asyncio)            │
│       │   ├─ Data capture service               │
│       │   ├─ Strategy engines (Semana 2-4)      │
│       │   └─ Risk manager (Semana 4)            │
│       └─ Volúmenes persistentes:                │
│            /app/data    (SQLite)                │
│            /app/logs    (rotated logs)          │
│            /app/secrets (private key)           │
└─────────────────────────────────────────────────┘
              ↕
        Kalshi API (REST + WS)
```

## Roadmap

- [x] **Semana 1**: Infrastructure (auth, clients, storage, health, deploy)
- [ ] **Semana 2**: Motor 1 - Arbitraje intra-Kalshi
- [ ] **Semana 3**: Motor 2 - Kalshi vs Sportsbook consensus
- [ ] **Semana 4**: Motor 3 - CLV + risk manager + Telegram alerts

## Reglas duras del sistema

Estas son hardcoded y NO se cambian sin aprobación explícita:

- Stop-loss diario: -3% capital activo → pausa 24h
- Stop-loss semanal: -8% → pausa 7 días
- Stop-loss mensual: -15% → kill-switch total
- Tope exposure simultáneo: 25% capital activo
- Sizing máximo por trade: 5% (¼ Kelly)
- Demo first: 7+ días en demo antes de production
- **NO LLMs en hot path de trading. Cero. Punto.**

## Endpoints

Una vez desplegado:

- `GET /health` - Liveness para Coolify
- `GET /ready` - Readiness probe (DB + WS)
- `GET /status` - Dashboard detallado
- `POST /admin/pause?reason=X` - Pausar trading
- `POST /admin/resume` - Reanudar
- `GET /admin/stats` - Stats operacionales

## Estructura del proyecto

```
.
├── pyproject.toml           # Project metadata + deps
├── Dockerfile               # Multi-stage build
├── docker-compose.yml       # Coolify deployment
├── .env.example             # Template de env vars
├── .gitignore               # Secrets + Python noise
├── .dockerignore            # Imagen limpia
├── src/
│   ├── auth/                # RSA-PSS signing
│   ├── clients/             # Kalshi REST + WS
│   ├── storage/             # SQLModel models
│   ├── strategies/          # Trading strategies
│   ├── risk/                # Risk manager (Semana 4)
│   ├── monitoring/          # Health + Telegram
│   ├── utils/               # Config + logging
│   └── runner.py            # Production entry point
├── scripts/
│   ├── generate_keys.sh     # Gen par RSA
│   └── smoke_test.py        # Verificación pre-deploy
├── tests/                   # pytest
├── docs/                    # Guías
└── .github/workflows/       # CI
```

## Documentación

- [DEPLOY_COOLIFY.md](docs/DEPLOY_COOLIFY.md) - Deploy paso a paso
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) - Decisiones técnicas
- [RISK_MANAGEMENT.md](docs/RISK_MANAGEMENT.md) - Reglas del sistema

## License

Proprietary. Sole owner: Noel Pineda.
