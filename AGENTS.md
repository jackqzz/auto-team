# Repository Runtime Guide

## Working Directory

- Repository root: `/root/auto-team`
- Run backend commands from the repository root unless a command below says otherwise.

## Python Environment

- Use `/root/auto-team/.venv/bin/python` for this repository's WebUI and tests.
- The verified runtime is Python 3.12 with FastAPI, Uvicorn, Pydantic, Requests, and the packages from `requirements.txt` installed.
- Do not rely on bare `python` or the system Python; its installed packages can differ from the running service environment.
- Create the project environment and install or refresh backend dependencies with:

  ```bash
  cd /root/auto-team
  python3.12 -m venv .venv
  /root/auto-team/.venv/bin/python -m pip install -r requirements.txt
  /root/auto-team/.venv/bin/python -m camoufox fetch
  ```

- Camoufox browser data is stored outside the repository under `/root/.cache/camoufox`.

## Frontend Environment

- The verified frontend toolchain is Node.js 24 and npm 11.
- Frontend source is under `webui/frontend`.
- Install locked dependencies and rebuild the static WebUI with:

  ```bash
  cd /root/auto-team/webui/frontend
  npm ci
  npm run build
  ```

- `npm run build` writes production assets to `webui/static`. Rebuild after frontend source changes.

## WebUI Service

- The project WebUI listens on `0.0.0.0:8767`.
- Run it in tmux session `agt`, in a dedicated window named `webui-8767`.
- On a fresh tmux server, start it with:

  ```bash
  tmux new-session -d -s agt -n webui-8767 \
    -c /root/auto-team \
    "/root/auto-team/.venv/bin/python start_webui.py --host 0.0.0.0 --port 8767 --no-browser"
  ```

- If tmux session `agt` already exists but the `webui-8767` window does not, start it with:

  ```bash
  tmux new-window -d -t agt -n webui-8767 \
    -c /root/auto-team \
    "/root/auto-team/.venv/bin/python start_webui.py --host 0.0.0.0 --port 8767 --no-browser"
  ```

- View logs with `tmux capture-pane -p -t agt:webui-8767 -S -200` or attach with `tmux attach -t agt`.
- Stop the service with `tmux kill-window -t agt:webui-8767`.
- Before starting, check for an existing listener with `ss -ltnp | rg ':8767'` and do not create duplicate instances.
- Run Uvicorn as a single process. The public quota and 401-relogin dispatchers are process-local global queues; multiple Uvicorn workers would multiply the configured concurrency and queue limits.

## Verification

- Backend syntax check:

  ```bash
  /root/auto-team/.venv/bin/python -m py_compile webui/app.py webui/db.py webui/public_relogin.py
  ```

- Full test suite:

  ```bash
  /root/auto-team/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
  ```

- WebUI health:

  ```bash
  curl -fsS http://127.0.0.1:8767/api/health
  ```

- Public queue status is available only when the public 401-relogin page is enabled:

  ```bash
  curl -fsS http://127.0.0.1:8767/api/public-relogin/queue-status
  ```

- After edits, also run `git diff --check`.
