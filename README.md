# TG Clone Pro — GitHub Action

Professionally structured runner that:

1. Uses a **delivery bot** (token from GitHub secrets) **only** to receive files  
2. Accepts **`tg-cl.tar.gz`**, then **`config.json`**  
3. Unpacks the archive → `tg-cl/`  
4. Places `config.json` inside that folder  
5. Runs **`./tg-cl bot`** (control bot comes from `config.json` — delivery token is discarded)  
6. Stays alive until you cancel the workflow, the process exits, or **15 minutes of inactivity**

```
┌─────────────────┐     tg-cl.tar.gz      ┌──────────────────────┐
│  You (Telegram) │ ───────────────────►  │  Delivery bot        │
│                 │     config.json       │  (secret token only) │
└─────────────────┘                       └──────────┬───────────┘
                                                     │ unpack + install config
                                                     ▼
                                          ┌──────────────────────┐
                                          │  ./tg-cl bot         │
                                          │  (from package +     │
                                          │   config.json)       │
                                          └──────────────────────┘
```

## Layout

```
action/
├── action.yml                 # Composite action definition
├── README.md
└── scripts/
    ├── entrypoint.sh          # Ordered orchestrator
    ├── receive_files.py       # Telegram file intake (delivery only)
    └── idle_watchdog.py       # Idle / exit supervisor
```

Workflow entrypoint (repository root):

```
.github/workflows/tg-cloner-pro.yml
```

## Setup

### 1. Create a delivery bot

1. Open [@BotFather](https://t.me/BotFather) → `/newbot`  
2. Copy the token  
3. In the GitHub repo: **Settings → Secrets and variables → Actions**  
4. Add secret:

| Secret | Required | Purpose |
|--------|----------|---------|
| `TELEGRAM_BOT_TOKEN` | **Yes** | Delivery bot only (receive the two files) |
| `TELEGRAM_ALLOWED_USER_IDS` | Recommended | Your Telegram user ID(s), comma-separated |

> This delivery token is **never** passed to `./tg-cl`.  
> The control bot token lives inside the `config.json` you send.

### 2. Prepare your package

- **`tg-cl.tar.gz`** — protected/dist build that extracts to a folder containing the `tg-cl` binary (e.g. `tg-cl/tg-cl`)  
- **`config.json`** — normal TG Clone Pro config (`api_id`, `api_hash`, `control_bot_token`, `owner_user_ids`, `workers`, …)

### 3. Run the workflow

1. **Actions → TG Clone Pro → Run workflow**  
2. Optionally set idle / delivery timeouts  
3. Open the delivery bot in Telegram  
4. Send **`tg-cl.tar.gz`** as a document  
5. Send **`config.json`** as a document  
6. Operate TG Clone Pro via the **control bot** defined in your config  

## Stop conditions

| Condition | Result |
|-----------|--------|
| Cancel workflow in GitHub UI | Job stops immediately |
| `tg-cl` process exits | Job ends with that exit code |
| No CPU + no disk I/O + no meaningful network for N minutes (default **15**) | Watchdog SIGTERM → clean stop (job success) |
| Job wall clock (`timeout-minutes: 360`) | GitHub cancels the job |

### Activity signals

The idle watchdog resets its timer when any of these move:

- process CPU time  
- process disk I/O bytes  
- host network RX/TX above a small noise floor (filters pure long-poll chatter)

## Security notes

- Delivery token is unset after file intake; child env is started without it.  
- `config.json` is installed mode `600` and wiped on job cleanup when possible.  
- Prefer setting `TELEGRAM_ALLOWED_USER_IDS` so strangers cannot feed the runner.  
- Do not commit real tokens or `config.json` to git.

## Local dry-run

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_ALLOWED_USER_IDS="your_telegram_user_id"
export IDLE_TIMEOUT_SECONDS=900
export DELIVERY_TIMEOUT_SECONDS=1800

./action/scripts/entrypoint.sh
```

## Composite action inputs

| Input | Default | Description |
|-------|---------|-------------|
| `telegram_bot_token` | — | Delivery bot token |
| `telegram_allowed_user_ids` | `""` | Allowed uploader IDs |
| `idle_timeout_seconds` | `900` | Inactivity auto-stop |
| `delivery_timeout_seconds` | `1800` | Wait for both files |
| `idle_poll_seconds` | `30` | Watchdog sample interval |
| `idle_net_noise_bytes` | `4096` | Network noise floor |

## Order of operations (entrypoint)

```
Preflight
  → receive_files.py   (bot token in use)
  → unset TELEGRAM_BOT_TOKEN
  → tar -xzf tg-cl.tar.gz
  → install config.json → tg-cl/
  → idle_watchdog.py -- ./tg-cl bot
  → cleanup
```
