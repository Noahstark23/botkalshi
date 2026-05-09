# Deploy en Coolify - Guía Completa

Guía paso a paso para desplegar el bot en Coolify.

## Pre-requisitos

- ✅ VPS con Coolify ya instalado (mínimo 2GB RAM, 1 vCPU disponible)
- ✅ Cuenta GitHub con repositorio privado del bot
- ✅ Cuenta Kalshi con verificación KYC completa
- ✅ Acceso al panel Coolify

---

## Paso 1: Verificar recursos del VPS

```bash
ssh tu-usuario@tu-vps-ip
free -h            # RAM disponible
df -h              # Espacio en disco
docker ps          # Servicios actuales
```

Necesitas:
- Mínimo 500MB RAM libre
- Mínimo 2GB disco libre

---

## Paso 2: Subir código a GitHub (privado)

```bash
cd ~/projects/kalshi-bot
git init
git add .

# CRÍTICO: verificar que .gitignore esté funcionando
git status --ignored
# Debe listar config/*.pem, .env, data/, etc. como ignorados

git commit -m "Initial: Semana 1 infrastructure"

# Crear repo PRIVADO en github.com (NUNCA público)
git remote add origin git@github.com:TU_USUARIO/kalshi-bot.git
git branch -M main
git push -u origin main
```

---

## Paso 3: Generar llaves RSA localmente

```bash
cd ~/projects/kalshi-bot
bash scripts/generate_keys.sh
```

Esto crea:
- `config/kalshi_private_key.pem` (NO se subirá a Git, está en .gitignore)
- `config/kalshi_public_key.pem` (se sube a Kalshi)

---

## Paso 4: Configurar API Key en Kalshi

1. https://kalshi.com → Login
2. Settings → API Keys
3. **CRÍTICO: Toggle a "Demo Environment"** (no Production)
4. "Add API Key"
5. Pegar contenido completo de `config/kalshi_public_key.pem`
6. Copiar el **Key ID** que Kalshi te genera (formato UUID)

---

## Paso 5: Crear app en Coolify

### 5.1 Nueva aplicación

1. Coolify dashboard → **New Resource** → **Application**
2. **Source:** GitHub (autorizar acceso si es primera vez)
3. **Repository:** `tu-usuario/kalshi-bot`
4. **Branch:** `main`
5. **Build Pack:** **Docker Compose**
6. **Docker Compose location:** `docker-compose.yml`
7. **Name:** `kalshi-bot`

### 5.2 Configurar Environment Variables

En la sección "Environment Variables" del servicio:

#### Variables requeridas

| Variable | Valor |
|----------|-------|
| `KALSHI_ENV` | `demo` |
| `KALSHI_API_KEY_ID` | El UUID de Kalshi |
| `ACTIVE_CAPITAL_USD` | `300.00` |
| `LOG_LEVEL` | `INFO` |
| `TRADING_ENABLED` | `false` |

#### Variables con defaults (override solo si necesario)

| Variable | Default |
|----------|---------|
| `MAX_DAILY_LOSS_PCT` | `3.0` |
| `MAX_WEEKLY_LOSS_PCT` | `8.0` |
| `MAX_MONTHLY_LOSS_PCT` | `15.0` |
| `MAX_SIMULTANEOUS_EXPOSURE_PCT` | `25.0` |
| `MAX_TRADE_SIZE_PCT` | `5.0` |
| `KELLY_FRACTION` | `0.25` |
| `MIN_EDGE_PCT` | `2.0` |

### 5.3 Montar la private key como secret

Coolify maneja secrets de archivo de varias formas según versión:

**Opción A: Si Coolify soporta "Persistent Storage" + file:**

1. Storage → Add → File Storage
2. **Mount path:** `/app/secrets/kalshi_private_key.pem`
3. Pegar contenido de `config/kalshi_private_key.pem`
4. Permisos: `0600`

**Opción B: Vía SSH al VPS (siempre funciona):**

```bash
ssh tu-vps

# Encuentra el path del volumen
docker volume inspect $(docker volume ls -q | grep kalshi.*secrets)

# Te da algo como /var/lib/docker/volumes/.../  _data
# Crea el archivo:
sudo nano /var/lib/docker/volumes/.../kalshi_private_key.pem

# Pega el contenido de tu config/kalshi_private_key.pem local
# Save & exit

sudo chmod 600 /var/lib/docker/volumes/.../kalshi_private_key.pem
sudo chown 1000:1000 /var/lib/docker/volumes/.../kalshi_private_key.pem
# UID 1000 = botuser dentro del container

# Reinicia el container desde Coolify
```

### 5.4 Configurar Health Check

En Coolify → kalshi-bot → Settings → Health Check:

- **Path:** `/health`
- **Port:** `8080`
- **Interval:** 30s
- **Timeout:** 10s
- **Healthy threshold:** 2
- **Unhealthy threshold:** 3
- **Start period:** 90s

### 5.5 Resource Limits

Coolify lee los limits del `docker-compose.yml`. Verifica en el panel:
- Memory limit: 512 MB
- CPU limit: 0.5 cores

---

## Paso 6: Deploy

1. Click **"Deploy"** en Coolify
2. Logs en tiempo real en el dashboard
3. Build tarda ~3-5 minutos primer deploy

### Logs esperados

```
🚀 Bot arrancando en DEMO
Capital activo: $300.0
Trading enabled: False
DB inicializada
Health server: http://0.0.0.0:8080
Tracking N markets
WS conectado
Subscribed: channels=['orderbook_delta', 'ticker'] markets=N
Snapshots: N/M
```

---

## Paso 7: Verificación post-deploy

### Desde Coolify dashboard

- Status: **Running** + **Healthy**
- Logs sin errores rojos
- Resources: RAM <500MB, CPU <30%

### Desde curl

Coolify expone el servicio en un subdominio. Puede ser:
- `kalshi-bot.tudominio.com`
- O un domain de Coolify auto-asignado

```bash
# Liveness check
curl https://kalshi-bot.tudominio.com/health

# Status detallado
curl https://kalshi-bot.tudominio.com/status | jq

# Stats operacionales
curl https://kalshi-bot.tudominio.com/admin/stats
```

### Desde celular

Bookmark: `https://coolify.tudominio.com/applications/kalshi-bot`

Desde ahí:
- Ver logs en vivo
- Pausar/resumir
- Ver métricas

---

## Paso 8: Backup automático

Agregar a crontab del VPS:

```bash
ssh tu-vps
crontab -e
```

Agregar:

```cron
# Backup SQLite cada 6 horas
0 */6 * * * docker exec kalshi-bot sqlite3 /app/data/trades.db ".backup /app/data/backup_$(date +\%Y\%m\%d_\%H).db" 2>&1 | logger -t kalshi-backup

# Limpiar backups > 30 días
0 3 * * * find /var/lib/docker/volumes/*/kalshi_data/_data/backup_*.db -mtime +30 -delete
```

---

## Pausar bot remotamente

Desde cualquier lugar con internet:

```bash
# Pausar (sigue corriendo container, solo no tradea)
curl -X POST "https://kalshi-bot.tudominio.com/admin/pause?reason=verificando"

# Reanudar
curl -X POST "https://kalshi-bot.tudominio.com/admin/resume"
```

---

## Troubleshooting

### Container se reinicia constantemente

```bash
docker logs kalshi-bot --tail 100
```

Causas comunes:
- Healthcheck falla → revisar puerto 8080 expuesto
- Private key no montada → verificar volume
- Settings inválidos → revisar env vars en Coolify

### "401 Unauthorized" de Kalshi

- API key ID incorrecto en env vars
- Public key no subida a Kalshi
- Subiste public key a Production por error en lugar de Demo

### "Cannot connect to Kalshi"

- Firewall del VPS bloquea outbound HTTPS (raro)
- Demo URL incorrecta (no debería pasar, está hardcoded)

### Logs muestran "Cannot read private key"

- Volume no montado correctamente
- Permisos mal (debe ser readable por UID 1000)
- Path incorrecto en `KALSHI_PRIVATE_KEY_PATH`

### Healthy pero no hay trades

**Esto es esperado** en Semana 1. El bot está en modo data capture, no tradeando. Trading se activa con `TRADING_ENABLED=true` + motor enabled, pero NO antes de Semana 2.

---

## Checklist de deploy completo

- [ ] VPS con RAM disponible verificado
- [ ] Repo en GitHub privado
- [ ] `.gitignore` excluye `*.pem` y `.env` (verificado con `git status --ignored`)
- [ ] Llaves RSA generadas localmente
- [ ] Public key subida a Kalshi en DEMO environment
- [ ] Key ID guardado
- [ ] App creada en Coolify desde el repo
- [ ] Environment variables configuradas
- [ ] Private key montada como volume secret
- [ ] Health check configurado en Coolify
- [ ] Deploy exitoso
- [ ] Container status: Running + Healthy
- [ ] `GET /health` retorna 200
- [ ] `GET /status` muestra `capture_running: true`
- [ ] Backup automático en crontab

---

## Próximo paso

Una vez verificado el deploy, déjalo corriendo 24-48h capturando datos.

Entonces empezamos **Semana 2: Motor 1 - Arbitraje intra-Kalshi**.
