# Code Repository Manager

An AI-powered code quality platform that automatically analyzes Python repositories, detects issues, and proposes fixes through a human-in-the-loop review workflow.

Managers register their repositories, receive AI-generated findings grouped by file, and approve or reject each proposed change — all through a clean web dashboard.

---

## Features

- **Dead Code Detection** — finds unused functions and unused imports
- **Security Scanning** — detects hardcoded credentials, replaces them with `os.getenv()`
- **Documentation Generation** — generates Google-style docstrings via local LLM (Ollama)
- **Code Refactoring** — reduces cyclomatic complexity via local LLM
- **File Restructuring** — suggests how to split large files into smaller modules
- **Human-in-the-Loop Reviews** — every risky change requires manager approval before execution
- **Priority Ordering** — actions execute in safe order per file (Security → Delete → Refactor → Docstring → Restructure)
- **Duplicate Prevention** — re-analysis never floods the queue with copies of pending reviews
- **Multi-user Isolation** — each manager sees only their own projects and reviews
- **File Watcher** — monitors registered repos for changes, triggers analysis automatically
- **AST-based Targeting** — all actions use live symbol table lookups, immune to line number shifts
- **Graceful LLM Degradation** — core features work without Ollama

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        Manager Browser                           │
│                    Streamlit UI  :8501                           │
└────────────────────────────┬─────────────────────────────────────┘
                             │ HTTP REST
┌────────────────────────────▼─────────────────────────────────────┐
│                    FastAPI Backend  :8000                         │
│                                                                   │
│  ┌──────────────┐  ┌───────────────┐  ┌────────────────────────┐ │
│  │  JWT Auth    │  │ Project       │  │  Review Queue API      │ │
│  │  /register   │  │ Watcher       │  │  /api/reviews          │ │
│  │  /login      │  │ (watchdog)    │  │  /api/reviews/grouped  │ │
│  └──────────────┘  └──────┬────────┘  └────────────────────────┘ │
│                           │ file change detected                  │
│                           ▼                                       │
│  ┌────────────────────────────────────────────────────────────┐   │
│  │               LangGraph Agent Workflow                     │   │
│  │                                                            │   │
│  │  ┌─────────────┐ ┌──────────────┐ ┌──────────────────┐    │   │
│  │  │  Dead Code  │ │  Security    │ │  Documentation   │    │   │
│  │  │  Agent      │ │  Agent       │ │  Agent           │    │   │
│  │  └──────┬──────┘ └──────┬───────┘ └────────┬─────────┘    │   │
│  │         └──────────────┬┘                  │              │   │
│  │                        ▼                   │              │   │
│  │             ┌───────────────────┐          │              │   │
│  │             │  Structure Agent  │◄─────────┘              │   │
│  │             └─────────┬─────────┘                         │   │
│  └───────────────────────┼────────────────────────────────── ┘   │
│                          ▼                                        │
│  ┌────────────────────────────────────────────────────────────┐   │
│  │               Action Coordinator                           │   │
│  │  Finding → Symbol-based Action  (@variable:NAME)          │   │
│  │  Merges compatible findings (docstring + refactor)        │   │
│  └───────────────────────┬────────────────────────────────────┘   │
│                          ▼                                        │
│  ┌────────────────────────────────────────────────────────────┐   │
│  │                   HITL Router                              │   │
│  │                                                            │   │
│  │  LOW risk + high confidence  →  Auto Execute              │   │
│  │  HIGH / CRITICAL risk        →  Review Queue              │   │
│  │  Security / Refactor         →  Always Review Queue       │   │
│  └──────────┬─────────────────────────────┬───────────────── ┘   │
│             │                             │                       │
│             ▼                             ▼                       │
│  ┌─────────────────────┐     ┌────────────────────────┐          │
│  │   Auto Execute      │     │     Review Queue        │          │
│  │   immediately       │     │   (pending approval)   │          │
│  └─────────────────────┘     └────────────┬───────────┘          │
│                                           │ manager approves      │
│                                           ▼                       │
│                              ┌────────────────────────┐          │
│                              │   Background Job        │          │
│                              │  RegistryActionExecutor │          │
│                              │  (AST + FileEditor)     │          │
│                              └────────────────────────┘          │
└──────────────────────────────────────────────────────────────────┘
                             │
               ┌─────────────┴──────────────┐
               ▼                            ▼
┌──────────────────────┐      ┌──────────────────────────┐
│    PostgreSQL DB      │      │     File Registry         │
│                      │      │   (in-memory AST index)   │
│  users               │      │                           │
│  projects            │      │  SymbolTable per file:    │
│  analysis_sessions   │      │    functions              │
│  background_jobs     │      │    classes                │
│  reviews             │      │    imports                │
│  notifications       │      │    assignments            │
│  learning_data       │      │                           │
└──────────────────────┘      └──────────────────────────┘
```

---

## Action Execution Order

Actions on the same file always execute in this safe sequence:

| Order | Action | Notes |
|-------|--------|-------|
| 1 | `FIX_SECURITY` | Credentials fixed first — no structural dependency |
| 2 | `DELETE_IMPORT` | Auto-approved, low-risk cleanup |
| 3 | `DELETE_FUNCTION` | Dead code removal |
| 4 | `REFACTOR_CODE` | Requires manager approval |
| 5 | `ADD_DOCSTRING` | Describes the already-refactored code |
| 6 | `RESTRUCTURE` | Last — only meaningful after deletions |
| 7 | `MOVE_FILE` | File-level, always last |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | Streamlit |
| Backend | FastAPI + Uvicorn |
| Agent Workflow | LangGraph |
| Code Analysis | AST, Radon, Bandit, Pylint |
| Local LLM | Ollama (deepseek-coder:6.7b) |
| Database | PostgreSQL (psycopg2) |
| Authentication | JWT |
| File Watching | Watchdog |
| MCP Server | MCP protocol |
| Containerization | Docker + Docker Compose |
| CI/CD | GitHub Actions |

---

## Project Structure

```
code_repo_manager/
├── api/
│   └── main.py               # FastAPI routes, JWT auth, background jobs
├── agents/
│   └── workflow.py           # LangGraph agent workflow definition
├── core/
│   ├── models.py             # Pydantic models (Finding, Action, ReviewRequest)
│   ├── action_coordinator.py # Finding → symbol-based Action conversion
│   ├── executor.py           # RegistryActionExecutor, BackupManager, LocalLLM
│   ├── file_registry.py      # In-memory AST index, FileEditor, SymbolTable
│   ├── hitl.py               # HITLRouter + ReviewQueue with priority ordering
│   ├── watcher.py            # ProjectWatcher (watchdog-based file monitoring)
│   ├── auth.py               # JWT token creation and validation
│   └── logger.py             # Centralized logging (stdout + optional file)
├── storage/
│   └── checkpoint.py         # PostgreSQL storage (all persistence)
├── mcp_server/
│   ├── server.py             # MCP server entry point
│   └── tools/
│       └── code_analysis.py  # MCP tool definitions
├── config/
│   └── settings.py           # Pydantic settings loaded from .env
├── ui/
│   └── app.py                # Streamlit manager dashboard
├── tests/
│   └── sample_repo/          # Sample Python repo for testing
├── .github/
│   └── workflows/
│       └── deploy.yml        # GitHub Actions CI/CD pipeline
├── Dockerfile.api
├── Dockerfile.ui
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── .gitignore
```

---

## Prerequisites

- Python 3.11+
- PostgreSQL 14+
- Docker + Docker Compose (for containerized setup)
- Ollama (optional — for docstring and refactor features)

---

## Local Development Setup

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/code-repo-manager.git
cd code-repo-manager
```

### 2. Create virtual environment

```bash
python -m venv venv
source venv/bin/activate        # Linux / Mac
venv\Scripts\activate           # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your values:

```env
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=code_repo_manager
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_password_here

JWT_SECRET_KEY=your_generated_secret
JWT_ALGORITHM=HS256
JWT_EXPIRE_MINUTES=1440

LOG_TO_FILE=true
ENABLE_BACKUP=true
BACKUP_DIR=/your/local/backup/path
```

Generate a strong JWT secret:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### 5. Create PostgreSQL database

```bash
psql -U postgres -c "CREATE DATABASE code_repo_manager;"
```

Tables are created automatically on first startup.

### 6. (Optional) Install Ollama for LLM features

```bash
# Install from https://ollama.ai
ollama pull deepseek-coder:6.7b
ollama serve
```

Without Ollama, security fixes and dead code removal still work fully. Docstring generation and refactoring return graceful fallback messages.

### 7. Start the API

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
```

### 8. Start the UI

```bash
streamlit run ui/app.py --server.port 8501
```

Open `http://localhost:8501` in your browser.

---

## Docker Setup

Run everything with one command:

```bash
cp .env.example .env
# fill in your values in .env

docker-compose up --build
```

| Service | URL |
|---------|-----|
| Streamlit UI | `http://localhost:8501` |
| FastAPI | `http://localhost:8000` |
| FastAPI docs | `http://localhost:8000/docs` |

**Useful commands:**

```bash
docker-compose up --build -d     # run in background
docker-compose logs -f api       # follow API logs
docker-compose logs -f ui        # follow UI logs
docker-compose ps                # show running containers
docker-compose down              # stop all containers
docker-compose down -v           # stop and delete database volume
```

---

## Usage

### 1. Register an account
Open `http://localhost:8501` → Register tab → create a manager account.

### 2. Add a project
Projects tab → Register New Project → enter the name and absolute path to your Python repo.

Analysis starts automatically. Switch to the **Review Queue** tab — reviews appear within seconds.

### 3. Review findings

Each review card shows:
- Action type and description
- Risk level and confidence score
- Target file and symbol name
- Impact analysis details

Click **Approve** to execute the change or **Reject** to skip it.

### 4. Monitor execution
Approved actions run as background jobs. The UI polls automatically and displays execution results inline.

---

## Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_HOST` | `localhost` | PostgreSQL host |
| `POSTGRES_PORT` | `5432` | PostgreSQL port |
| `POSTGRES_DB` | `code_repo_manager` | Database name |
| `POSTGRES_USER` | `postgres` | Database user |
| `POSTGRES_PASSWORD` | — | Database password (**required**) |
| `JWT_SECRET_KEY` | — | JWT signing secret (**required**, min 32 chars) |
| `JWT_ALGORITHM` | `HS256` | JWT algorithm |
| `JWT_EXPIRE_MINUTES` | `1440` | Token expiry in minutes (24 hours) |
| `API_BASE_URL` | `http://localhost:8000` | FastAPI URL used by Streamlit |
| `LOG_TO_FILE` | `false` | Write logs to file — set `true` for local dev only |
| `ENABLE_BACKUP` | `false` | Enable file backups — set `true` for local dev only |
| `BACKUP_DIR` | — | Backup directory path (required when `ENABLE_BACKUP=true`) |

---

## Deployment on AWS EC2

### GitHub Secrets Required

Go to your repo → **Settings** → **Secrets and variables** → **Actions**

| Secret Name | Value |
|-------------|-------|
| `EC2_HOST` | EC2 public IP address |
| `EC2_USER` | `ubuntu` |
| `EC2_SSH_KEY` | Full contents of your `.pem` key file |
| `POSTGRES_PASSWORD` | Your database password |
| `SECRET_KEY` | Your JWT secret |

### EC2 Instance Setup (one time only)

```bash
# SSH into EC2
ssh -i your-key.pem ubuntu@YOUR_EC2_IP

# Install Docker
sudo apt-get update
sudo apt-get install -y docker.io docker-compose
sudo systemctl start docker
sudo systemctl enable docker
sudo usermod -aG docker ubuntu
newgrp docker

# Create app directory
mkdir -p ~/code-repo-manager
```

### Security Group Inbound Rules

| Port | Purpose | Source |
|------|---------|--------|
| 22 | SSH | Your IP only |
| 8000 | FastAPI | 0.0.0.0/0 |
| 8501 | Streamlit | 0.0.0.0/0 |

### Deploy

Push to `main` — GitHub Actions handles everything automatically:

```bash
git push origin main
```

Watch live: GitHub repo → **Actions** tab

After deploy:
- Streamlit UI → `http://YOUR_EC2_IP:8501`
- FastAPI docs → `http://YOUR_EC2_IP:8000/docs`

### Read logs on EC2

```bash
docker logs crm_api              # all API logs
docker logs crm_api --tail 100  # last 100 lines
docker-compose logs -f           # follow all containers live
```

---

## How the Review Queue Works

```
1. Analysis finds issues in your repo files

2. ActionCoordinator converts findings to symbol-based Actions
      e.g.  /repo/config.py@variable:API_KEY
            /repo/utils.py@function:unused_helper

3. HITLRouter decides: auto-execute or human review?
      CRITICAL risk    → always queue for review
      FIX_SECURITY     → always queue for review
      REFACTOR_CODE    → always queue for review
      Low confidence   → queue for review
      Low risk + high confidence → auto-execute immediately

4. ReviewQueue stores pending reviews
      Deduplicated by (action_type, target)
      Ordered by FILE_ACTION_ORDER within each file
      Higher-priority actions block lower-priority ones

5. Manager approves → Background job executes
      FileRegistry resolves current symbol from live SymbolTable
      FileEditor applies mutation and rebuilds AST index
      Stale reviews auto-invalidated after each execution
```

---

## Key Design Decisions

**AST-based targeting over raw line numbers**
All actions target symbols (`@function:name`, `@variable:name`) not line numbers. The SymbolTable rebuilds after every edit so targets stay valid even when prior actions shift line positions.

**Deduplication by (action_type, target)**
Re-running analysis on a repo with pending reviews never creates duplicate entries. The queue returns the existing review ID instead of creating a new one.

**Graceful LLM degradation**
Ollama is completely optional. The system detects at runtime whether it is running. Security fixes, dead code removal, and import cleanup work entirely without a local LLM.

**Per-user data isolation**
Every review, job, analysis session, and notification is scoped to the manager who triggered it. Users cannot see or act on each other's data.

**Backups disabled in production**
`ENABLE_BACKUP` defaults to `false`. On production servers, file backups should be handled by dedicated object storage (S3 or MinIO). Enabling backups on the application server would fill disk space at scale.

---

## License

MIT
