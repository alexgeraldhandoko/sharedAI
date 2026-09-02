# SharedAI

SharedAI is a real-time, multi-user AI workspace for software development. A team creates or joins a six-digit workspace, sends requests to a shared coding agent, and sees generated files, active work, conflicts, and teammate presence update live.

This branch combines the React interface from `main` with the concurrency-aware FastAPI backend developed on `Alex`.

## What it does

- Creates and joins shared workspaces using six-digit codes.
- Synchronizes workspace state and participant presence over WebSockets.
- Routes coding and general requests through TokenRouter or Kimi.
- Adds Bright Data search and page context when a request needs current web information.
- Stores generated files in shared workspace state so every teammate sees the same result.
- Prevents overlapping edits with file- and symbol-level lease locks.
- Returns explicit conflict details and supports intentional conflict overrides.
- Falls back from TokenRouter to direct Kimi for eligible gateway failures.
- Uses Redis for shared state in normal deployments or an in-memory store for local development and tests.

## Architecture

```text
Browser
  React + Vite UI
        |
        | REST: workspaces, prompts, sessions, shared state
        | WebSocket: presence and live workspace events
        v
  FastAPI application
        |
        +-- WorkspaceStore
        |     +-- Redis store (default)
        |     `-- in-memory store (local/test option)
        |
        +-- lock leases and conflict detection
        +-- shared sessions, events, and generated files
        |
        +-- TokenRouter ---------+
        |                        +--> Kimi model response
        +-- direct Kimi fallback +
        |
        `-- Bright Data web research (when requested)
```

The browser never calls model or research providers directly. It sends a prompt and inferred file or symbol targets to FastAPI. The backend atomically checks the requested targets, acquires time-bounded leases, builds model context from shared workspace state, optionally gathers web context, runs the selected model gateway, validates the structured response, persists generated files, releases the leases, and broadcasts the completed session.

## Repository layout

```text
.
├── backend/
│   ├── app/
│   │   ├── main.py            # REST API, WebSocket hub, and model execution
│   │   ├── store.py           # Redis/memory state, locks, conflicts, and files
│   │   ├── session_runner.py  # Model context and structured-output parsing
│   │   ├── model_router.py    # Task-aware model routing
│   │   ├── tokenrouter.py     # TokenRouter client
│   │   ├── kimi.py            # Direct Kimi client
│   │   └── brightdata.py      # Optional live web research
│   ├── tests/
│   ├── pyproject.toml
│   └── .env.example
├── frontend/
│   ├── src/
│   │   ├── App.jsx            # Workspace interface
│   │   ├── auth.js            # Google and local-development sign-in
│   │   ├── ws.js              # Alex backend REST/WebSocket adapter
│   │   └── styles.css
│   ├── package.json
│   └── .env.example
├── requirements.txt
└── README.md
```

## Requirements

- Python 3.12+
- Node.js 20+
- npm
- Redis for persistent/shared backend state; optional for local development
- A TokenRouter API key, a Kimi API key, or both to execute agent requests
- Bright Data credentials only for web-research requests
- A Google OAuth web client ID only if Google sign-in is required; local sign-in remains available without it

## Install

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
npm --prefix frontend install
```

Create local environment files:

```bash
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env
```

Set at least one model credential in `backend/.env`:

```dotenv
# TokenRouter is used when this key is set.
TOKENROUTER_API_KEY=

# Direct Kimi is used when MODEL_GATEWAY=kimi, and as the configured fallback.
MOONSHOT_API_KEY=
```

For Google sign-in, set `VITE_GOOGLE_CLIENT_ID` in `frontend/.env`. Without it, the UI provides local-development sign-in. The frontend uses `VITE_API_BASE=http://127.0.0.1:8000` by default.

## Run locally

Start the backend with the in-memory store:

```bash
AI_WORKSPACE_STORE=memory .venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 --reload --env-file backend/.env
```

In a second terminal, start the frontend:

```bash
npm --prefix frontend run dev -- --port 3000
```

Open [http://127.0.0.1:3000](http://127.0.0.1:3000). The API health endpoint is [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health).

For Redis-backed state, start Redis, leave `AI_WORKSPACE_STORE=redis` in `backend/.env`, set `REDIS_URL`, and run the same backend command without the leading `AI_WORKSPACE_STORE=memory` override.

## Use the workspace

1. Sign in with Google or enter a local display name.
2. Select **Create workspace** and share the generated six-digit code.
3. A teammate signs in, enters the code, and selects **Join**.
4. Submit a coding or research request.
5. Generated files and recent work appear for every connected participant.
6. If another active session owns the same target, SharedAI reports the owner and lease expiry instead of silently overwriting the work.

Example requests:

```text
Create src/calculator.py with typed add and subtract functions and unit tests.
```

```text
Research the latest FastAPI release notes and summarize any breaking changes.
```

## Configuration

The full configuration template is in `backend/.env.example`. The main settings are:

| Variable | Purpose |
| --- | --- |
| `AI_WORKSPACE_STORE` | `redis` (default) or `memory` |
| `REDIS_URL` | Redis connection URL |
| `CORS_ORIGINS` | Comma-separated allowed frontend origins |
| `MODEL_GATEWAY` | `tokenrouter` or `kimi` |
| `TOKENROUTER_API_KEY` | TokenRouter credential |
| `MOONSHOT_API_KEY` / `KIMI_API_KEY` | Direct Kimi credential |
| `BRIGHTDATA_API_KEY` | Bright Data credential |
| `BRIGHTDATA_SERP_ZONE` | Bright Data SERP zone used for search |
| `BRIGHTDATA_UNLOCKER_ZONE` | Optional zone for fetching result pages |

Do not commit populated `.env` files or credentials.

## Tests and build

Run the backend test suite:

```bash
source .venv/bin/activate
python -m pip install -e './backend[dev]'
python -m pytest backend/tests
```

Build the production frontend:

```bash
npm --prefix frontend run build
```

The backend tests cover routing, model clients, Bright Data, Redis-backed synchronization, lock conflicts and overrides, lease expiry, WebSocket presence, CORS, structured model output, and generated-file persistence.

## Operational behavior and limitations

- The in-memory store is process-local and resets on restart. Use Redis when state must survive process restarts or be shared by multiple backend instances.
- Presence is live connection state and is not persisted in Redis.
- A model key is required to run an accepted session; workspace creation, joining, presence, and conflict checks work without one.
- Web research requires Bright Data credentials. Requests that explicitly need web data fail clearly when it is not configured.
- File targets are inferred by the frontend and enforced by backend leases. Prompts that do not identify a path use a shared workspace-level target.
- Local sign-in is intended for development and does not provide production authentication or authorization.
- This branch does not execute generated code in an isolated sandbox. Model output is validated and stored, but should still be reviewed before execution.
