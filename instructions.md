# ArbitrageBot — Linux VPS Setup & Deployment Guide

> **Instructions for AI Agents & Administrators:**  
> This file contains the complete, step-by-step instructions to set up, configure, verify, and run the **Cross-Venue Delta-Neutral Funding Rate Arbitrage Bot** on a Linux VPS (Ubuntu 22.04 / 24.04 LTS or Debian 12).  
> If you are an AI assistant executing this setup, follow each section in order. All bash commands are copy-pasteable and deterministic.

---

## Table of Contents

1. [Architecture & Overview](#1-architecture--overview)
2. [Prerequisites & System Packages](#2-prerequisites--system-packages)
3. [Repository & Python Environment](#3-repository--python-environment)
4. [Environment Configuration (`.env`)](#4-environment-configuration-env)
5. [Smoke Testing & Pre-Flight Checks](#5-smoke-testing--pre-flight-checks)
6. [Deployment Method A: Systemd Service (Recommended)](#6-deployment-method-a-systemd-service-recommended)
7. [Deployment Method B: Docker & Docker Compose](#7-deployment-method-b-docker--docker-compose)
8. [Dashboard Access & Security](#8-dashboard-access--security)
9. [Operational CLI Toolkit](#9-operational-cli-toolkit)
10. [Troubleshooting & FAQ](#10-troubleshooting--faq)

---

## 1. Architecture & Overview

- **Application:** FastAPI web server + background trading engine (scanner, order execution, delta risk manager, position monitor).
- **Default Port:** `8000` (serves JSON API and embedded monitoring dashboard at `/dashboard`).
- **Default Run Mode:** `paper` mode (real exchange market data, simulated orders & equity).
  - *Note:* Live trading is disabled by default (`LIVE_TRADING_ENABLED=false`) and requires explicit gate confirmations and API keys.
- **Storage:** Local SQLite database at `data/arbitrage.db` (auto-created) or PostgreSQL via `DATABASE_URL`.
- **Market Data Sources:**
  - **Direct Exchange Adapters:** Binance, Bybit, OKX, Gate, MEXC, BloFin, Hyperliquid (public market data, order books, candles, funding rates).
  - **ZeroDelta Discovery:** Identifies cross-venue funding rate spreads either via direct PostgreSQL database connection (`ZERO_DELTA_SOURCE=postgres`) or Dash web API (`ZERO_DELTA_SOURCE=dash`).

---

## 2. Prerequisites & System Packages

Ensure system packages are updated and required dependencies (Python 3.12+, git, curl, build essentials, libpq) are installed:

```bash
sudo apt-get update && sudo apt-get install -y \
    python3 \
    python3-venv \
    python3-pip \
    git \
    curl \
    build-essential \
    libpq-dev \
    htop \
    ufw
```

Verify Python version (requires Python >= 3.12):
```bash
python3 --version
```

---

## 3. Repository & Python Environment

### 3.1. Navigate to the Bot Directory
Ensure you are in the project root directory:
```bash
cd /path/to/ArbitrageBot
# or clone if not already present:
# git clone <YOUR_GITHUB_REPO_URL> /opt/arbitragebot && cd /opt/arbitragebot
```

### 3.2. Create & Activate Virtual Environment
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
```

### 3.3. Install Dependencies
Install production dependencies and PostgreSQL driver:
```bash
pip install -r requirements.txt
```

If you plan to run automated tests on the VPS, also install test dependencies:
```bash
pip install -r requirements-dev.txt
```

*(Alternatively, install editable package with extras: `pip install -e ".[postgres,dev]" `)*

---

## 4. Environment Configuration (`.env`)

The bot requires a `.env` file for runtime configuration. **Never commit `.env` to Git.**

### 4.1. Initialize `.env` from template:
```bash
cp .env.example .env
chmod 600 .env
```

### 4.2. Essential Configuration Parameters

Open `.env` in an editor (`nano .env`) and configure the key fields:

```dotenv
# ==============================================================================
# RUNTIME MODE
# ==============================================================================
RUN_MODE=paper
LIVE_TRADING_ENABLED=false

# ==============================================================================
# DATABASE
# Default: SQLite stored in data/ (directory is created automatically)
# ==============================================================================
DATABASE_URL=sqlite:///data/arbitrage.db

# ==============================================================================
# ADMIN API SECURITY
# Used to authenticate administrative commands and kill-switch endpoints
# Generate with: openssl rand -hex 32
# ==============================================================================
ADMIN_API_TOKEN=your-random-secret-token-here

# ==============================================================================
# SCANNING & VENUE RESTRICTIONS
# ==============================================================================
SCAN_ASSETS=ALL
# If your VPS IP is blocked by certain exchanges (e.g. BloFin in US/SG), exclude them:
EXCLUDE_VENUES=BloFin

# ==============================================================================
# ZERODELTA DISCOVERY PROVIDER
# If using direct PostgreSQL upstream database:
# ==============================================================================
ZERO_DELTA_ENABLED=true
ZERO_DELTA_SOURCE=postgres
ZERO_DELTA_DATABASE_URL=postgresql://user:password@host:5432/dbname
ZERO_DELTA_DB_VOLUME_UNIT=millions
ZERO_DELTA_DB_FUNDING_UNIT=fraction
ZERO_DELTA_DB_INTERVAL_UNIT=hours
ZERO_DELTA_DB_TIMESTAMP_UNIT=milliseconds
ZERO_DELTA_DB_FUNDING_KIND=predicted
ZERO_DELTA_DB_DATETIME_UTC=true

# ==============================================================================
# LOGGING
# Full details go to logs/bot.log; console shows warnings and milestones.
# ==============================================================================
LOG_LEVEL=INFO
CONSOLE_LOG_LEVEL=WARNING
```

---

## 5. Smoke Testing & Pre-Flight Checks

Before starting the bot as a persistent daemon, run the built-in diagnostic and test suite.

### 5.1. Run Full Automated Test Suite
Verify that all unit and integration tests pass on the Linux environment:
```bash
pytest tests/
```
*Expected result:* All 900+ tests should pass with 0 failures.

### 5.2. Test Public Exchange Connectors
Verify that the VPS IP can connect to exchange APIs without geo-blocking or rate limiting:
```bash
python -m app.tools.public_check Bitget OKX Gate "MEXC USDT"
```
*Expected result:* JSON output with `"symbol": "BTC...", "closed_1m_candles": ...` for each venue. If an exchange returns 403 or geo-block, add that exchange name to `EXCLUDE_VENUES` in `.env`.

### 5.3. Test ZeroDelta Database Connection (If using PostgreSQL source)
```bash
python -m app.tools.zerodelta_db_check
```
*Expected result:* Reports successful connection, row counts, and sample venues.

### 5.4. Single Run Check
Test starting the API server manually for 10 seconds:
```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```
In another terminal (or using `curl`):
```bash
curl http://127.0.0.1:8000/health
```
*Expected output:* `{"status":"ok",...}`. Press `Ctrl+C` to stop the manual process.

---

## 6. Deployment Method A: Systemd Service (Recommended)

Deploying as a `systemd` service ensures the bot runs continuously 24/7, auto-starts on VPS reboot, and recovers automatically if interrupted.

### 6.1. Create Systemd Service File

Determine your absolute project path and user:
```bash
pwd        # e.g., /opt/arbitragebot or /home/ubuntu/ArbitrageBot
whoami     # e.g., ubuntu or root
```

Create `/etc/systemd/system/arbitrage-bot.service` (replace `/opt/arbitragebot` and `ubuntu` with your actual path and user):

```ini
[Unit]
Description=Cross-Venue Delta-Neutral Funding Rate Arbitrage Bot
After=network.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/arbitragebot
Environment="PATH=/opt/arbitragebot/.venv/bin:/usr/local/bin:/usr/bin"
EnvironmentFile=/opt/arbitragebot/.env
ExecStart=/opt/arbitragebot/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5s
LimitNOFILE=65535

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=arbitrage-bot

[Install]
WantedBy=multi-user.target
```

### 6.2. Enable & Start the Service
```bash
sudo systemctl daemon-reload
sudo systemctl enable arbitrage-bot
sudo systemctl start arbitrage-bot
```

### 6.3. Inspect Service Status & Logs
Check service status:
```bash
sudo systemctl status arbitrage-bot
```

Stream live application logs via journalctl:
```bash
journalctl -u arbitrage-bot -f
```

Stream file logs:
```bash
tail -f /opt/arbitragebot/logs/bot.log
```

---

## 7. Deployment Method B: Docker & Docker Compose

If containerization is preferred over native systemd:

### 7.1. Install Docker & Docker Compose Plugin
```bash
sudo apt-get install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
```

### 7.2. Start with Docker Compose
```bash
# Ensure .env is populated with POSTGRES_PASSWORD
docker compose up -d --build
```

### 7.3. Monitor Containers
```bash
docker compose ps
docker compose logs -f bot
```

---

## 8. Dashboard Access & Security

The bot serves a real-time web console at `http://127.0.0.1:8000/dashboard`.  
Because it contains financial data and controls, **never expose port 8000 directly to the public internet without authentication.**

### Option 1: Secure SSH Tunneling (Recommended & Zero Configuration)
From your local desktop or laptop terminal, establish an encrypted SSH tunnel to the VPS:
```bash
ssh -N -L 8000:127.0.0.1:8000 user@your-vps-ip
```
Then open your desktop browser at:
```
http://localhost:8000/dashboard
```

### Option 2: Nginx Reverse Proxy with Basic Authentication & SSL
If you require direct web access from anywhere:

1. Install Nginx and Apache utilities:
   ```bash
   sudo apt-get install -y nginx apache2-utils
   ```
2. Create password for basic auth:
   ```bash
   sudo htpasswd -c /etc/nginx/.htpasswd admin
   ```
3. Configure `/etc/nginx/sites-available/arbitrage-bot`:
   ```nginx
   server {
       listen 80;
       server_name bot.yourdomain.com;

       location / {
           auth_basic "Restricted Area";
           auth_basic_user_file /etc/nginx/.htpasswd;

           proxy_pass http://127.0.0.1:8000;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
       }
   }
   ```
4. Enable site and install Let's Encrypt SSL:
   ```bash
   sudo ln -s /etc/nginx/sites-available/arbitrage-bot /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   sudo apt-get install -y certbot python3-certbot-nginx
   sudo certbot --nginx -d bot.yourdomain.com
   ```

---

## 9. Operational CLI Toolkit

The bot includes administrative and observation CLI utilities located in `app.tools`:

| Task | Command |
|---|---|
| **View System Status Summary** | `python -m app.tools.observe --summary` |
| **Real-time Live Observer** | `python -m app.tools.observe --loop 5` |
| **Test Public Market Data Connectors** | `python -m app.tools.public_check Bitget OKX Gate` |
| **Check ZeroDelta DB Connection** | `python -m app.tools.zerodelta_db_check` |
| **Scan Arbitrage Opportunities** | `python -m app.tools.zerodelta_opportunities` |
| **Settle a Paper Trade Manually** | `python -m app.tools.settle_paper_trade <trade_id> --reason "manual"` |
| **Pause New Trade Entries** | `python -m app.tools.admin --pause` |
| **Resume Trade Entries** | `python -m app.tools.admin --resume` |
| **Emergency Flatten All Positions** | `python -m app.tools.admin --flatten` |

*(Always run these commands with `.venv` activated and from the repo root directory.)*

---

## 10. Troubleshooting & FAQ

### Issue: `Another bot instance is running or holds data/bot.lock`
**Cause:** The bot uses single-instance locking (`app.core.instance_lock`). If a previous process crashed or was forcefully killed (`kill -9`), the lockfile might remain.  
**Resolution:**  
1. Ensure no rogue uvicorn process is running: `pgrep -af uvicorn`
2. If no process is running, remove the stale lockfile: `rm data/bot.lock`
3. Restart the service: `sudo systemctl restart arbitrage-bot`

### Issue: Exchange returns `HTTP 403 Forbidden` or `Geo-restricted`
**Cause:** Some exchanges (e.g. BloFin, OKX) restrict certain IP blocks or datacenter hosting providers.  
**Resolution:**  
Edit `.env` and add the exchange to `EXCLUDE_VENUES`:
```dotenv
EXCLUDE_VENUES=BloFin,OKX
```

### Issue: `Database is locked` (SQLite)
**Cause:** Concurrent writes or external reader blocking SQLite.  
**Resolution:**  
The repository is pre-configured with SQLite WAL (Write-Ahead Logging) and a 30-second busy timeout. If needed, restart the bot to clear open file handles, or configure a local PostgreSQL instance:
```dotenv
DATABASE_URL=postgresql+psycopg://bot:password@localhost:5432/arbitrage
```

### Issue: ZeroDelta discovery reports `stale` data
**Cause:** Upstream funding data hasn't refreshed within the expected window.  
**Resolution:**  
The bot will pause opening *new* trades during stale data while continuing to manage existing open positions safely. Once upstream data refreshes, scanning resumes automatically.

---

## Quick Reference Summary for AI Agent Deployment

```bash
# 1. Update and install packages
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git curl build-essential libpq-dev

# 2. Virtual environment and dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt

# 3. Environment configuration
cp .env.example .env
# Edit .env with your settings (nano .env)

# 4. Verify test suite & public connectors
pytest tests/
python -m app.tools.public_check Bitget OKX Gate

# 5. Setup & launch systemd service
sudo cp <SERVICE_FILE> /etc/systemd/system/arbitrage-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now arbitrage-bot

# 6. Verify health
curl http://127.0.0.1:8000/health
```
