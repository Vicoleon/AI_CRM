# Cubie: Deployment Roles & Canary Flags

This document explains the environment variables available in `rubik_core/config.py` that control the deployment architecture, scaling strategy, and feature rollouts for Cubie.

## `DEPLOYMENT_ROLE`

**What it does:**
Controls which API endpoints (FastAPI routers) are mounted when the application starts.

**Why it matters:**
In a production Cloud Run environment, we don't want heavy, long-running batch jobs (like the nightly briefing pipeline) to steal CPU or memory from user-facing chat requests. By deploying the *same container image* as two distinct Cloud Run services with different `DEPLOYMENT_ROLE` variables, we can scale them independently.

**Available Options:**
- `all` (Default): Mounts all API routes and the worker routes. Best for local development and single-container deployments.
- `api`: Mounts **only** the interactive endpoints (`/api/chat`, `/api/home`, `/api/directory`, etc.). Used for the user-facing web backend.
- `worker`: Mounts **only** the background job endpoints (`/api/jobs`). Used for the Cloud Scheduler target.

**How to test locally:**
1. Start the API role: `DEPLOYMENT_ROLE=api uvicorn backend.main:app --port 8000`
2. Start the Worker role: `DEPLOYMENT_ROLE=worker uvicorn backend.main:app --port 8001`
*(See the Walkthrough for exact cURL commands to verify the isolation)*.

---

## Canary & Rollout Flags

These flags allow us to safely merge code into `main` without activating experimental or unverified features in production.

### `AGENT_MANAGEMENT_MODE`
- **Options:** `self_managed` (default), `platform_managed`
- **Explanation:** Determines if the application orchestrates the `google-adk` agents manually in memory (`self_managed`) or delegates orchestration to Vertex AI Agent Engine (`platform_managed`). 
- **Effect:** Keeps our current stable architecture intact while we prepare to test Google's managed Agent APIs.

### `ENABLE_MULTI_AGENT_RUNTIME`
- **Type:** `bool` (Default: `True`)
- **Explanation:** Toggles the use of the complex multi-agent graph (Planner -> Parallel Retrievers -> Synthesizer -> Critique) vs a fallback single-agent.

### `ENABLE_A2A_SPECIALISTS`
- **Type:** `bool` (Default: `False`)
- **Explanation:** Enables Agent-to-Agent (A2A) remote calling. When `True`, the Coordinator will attempt to reach out to independently deployed Cloud Run specialist agents via HTTP. Since we verified in GCP that these remote specialists do not exist yet, this is safely defaulted to `False`.

### `ENABLE_MCP_INTEGRATIONS`
- **Type:** `bool` (Default: `True`)
- **Explanation:** Toggles the Model Context Protocol (MCP) integrations (like the new HubSpot server).

### `ENABLE_MANAGED_SESSIONS`
- **Type:** `bool` (Default: `False`)
- **Explanation:** Toggles whether session history is stored locally/in-memory vs in Google's managed Firestore Session infrastructure.
