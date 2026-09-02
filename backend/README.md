# AI Workspace Backend

Minimal FastAPI backend for a collaborative AI workspace.

The MVP implements:

- workspace creation
- prompt submission
- file/symbol lock acquisition
- conflict warnings
- conflict override
- model routing through TokenRouter or direct Kimi
- Bright Data web research context for current-information prompts
- model session execution for coding/general sessions
- session completion and lock release
- shared generated-file persistence
- workspace state inspection
- WebSocket updates and live participant presence

Runtime state is Redis-backed by default. Redis stores workspaces, active lock leases, agent sessions, generated files, and recent action events. The in-memory store still exists for tests and quick local experiments through `AI_WORKSPACE_STORE=memory`.

Model calls are OpenAI-compatible. TokenRouter is the preferred gateway when configured:

```bash
export MODEL_GATEWAY=tokenrouter
export AI_WORKSPACE_CODING_MODEL=kimi-k2.7-code
export AI_WORKSPACE_WEB_MODEL=kimi-k2.7-code
export AI_WORKSPACE_GENERAL_MODEL=kimi-k2.6
export TOKENROUTER_API_KEY=<your-tokenrouter-api-key>
export TOKENROUTER_BASE_URL=https://api.tokenrouter.com/v1
```

`AI_WORKSPACE_*_MODEL` controls the routed model name stored on each session. `TOKENROUTER_MODEL` is optional and acts as a gateway-level override. If your TokenRouter account uses a different model or channel name for every request, set:

```bash
export TOKENROUTER_MODEL=<your-tokenrouter-model-name>
```

If TokenRouter rejects the configured routed model with a model-availability error and direct Kimi credentials are configured, the backend automatically retries the same request against Kimi.

For direct Kimi calls without TokenRouter:

```bash
export MODEL_GATEWAY=kimi
export MOONSHOT_API_KEY=<your-api-key>
export KIMI_BASE_URL=https://api.moonshot.ai/v1
export KIMI_MODEL=kimi-k2.7-code
```

`KIMI_API_KEY` also works if you prefer that variable name.

Bright Data is a separate context provider, not a TokenRouter model route. TokenRouter chooses and calls the model. Bright Data collects live web evidence before the model call when the prompt needs current or scraped information.

Configure Bright Data SERP API for web research:

```bash
export BRIGHTDATA_API_KEY=<your-bright-data-api-key>
export BRIGHTDATA_SERP_ZONE=<your-serp-api-zone>
```

Optionally configure Unlocker API to fetch excerpts from search result pages:

```bash
export BRIGHTDATA_UNLOCKER_ZONE=<your-unlocker-zone>
```

The app automatically runs Bright Data when `task_type` is `web` or when the prompt includes current-info terms such as `latest`, `current`, `news`, `pricing`, `docs`, `scrape`, `search`, or `research`. You can override that per run with `use_web_research`.

## Run locally

Start Redis first:

```bash
docker run --rm -p 6379:6379 redis:7
```

Then start the API:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
REDIS_URL=redis://localhost:6379/0 uvicorn app.main:app --reload
```

Open the API docs at:

```txt
http://127.0.0.1:8000/docs
```

For an in-memory local run without Redis:

```bash
AI_WORKSPACE_STORE=memory uvicorn app.main:app --reload
```

## Run tests

```bash
cd backend
pytest
```

## Example flow

Create a workspace:

```bash
curl -X POST http://127.0.0.1:8000/workspaces \
  -H "Content-Type: application/json" \
  -d '{"name":"Hackathon Workspace"}'
```

Submit a coding prompt:

```bash
curl -X POST http://127.0.0.1:8000/workspaces/<workspace_id>/prompts \
  -H "Content-Type: application/json" \
  -d '{
    "member_id": "Member A",
    "prompt": "modify function_login()",
    "task_type": "coding",
    "targets": [
      {"scope_type": "symbol", "scope_key": "src/auth.py::function_login"}
    ]
  }'
```

If another member targets the same file or symbol, the backend returns `409 Conflict` unless `override_conflicts` is set to `true`.

Run the accepted session through the configured model gateway:

```bash
curl -X POST http://127.0.0.1:8000/workspaces/<workspace_id>/sessions/<session_id>/run \
  -H "Content-Type: application/json" \
  -d '{
    "instructions": "Return a concise patch plan.",
    "max_tokens": 1200,
    "temperature": 1,
    "use_web_research": null,
    "web_query": null,
    "max_web_results": 5,
    "fetch_web_pages": true
  }'
```

The `/run` endpoint optionally runs Bright Data first, then sends the member prompt, selected model, active lock map, current session, recent workspace events, and web research evidence to the configured model gateway. Structured model output is parsed into an assistant message and complete file contents, persisted in shared workspace state, and broadcast to connected clients before the session's locks are released.
