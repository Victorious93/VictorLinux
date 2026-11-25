# Switch models without a reboot

## Arch edition (Control Center → Models → "Switch model")
1. The Control Center writes `/run/victorlinux/swap.json`. The API gateway sees it and **holds** new API requests instead of failing them.
2. Only `llama-server` is stopped. The gateway keeps running with the same keys, TLS certificate and port, so phones and other clients do not need to reconnect.
3. The new model starts on the same private port and must answer one test generation.
4. On success the choice is saved. If it fails to start or answer, the previous model is started again and the error is shown.
5. The marker is removed and held requests continue.

A request that waits more than 3 minutes gets `503` with `Retry-After: 10` and `"type": "model_switching"`. `/victor/health` reports `switching_model` while the switch runs. If no model is running, the button only selects the model.

API: `GET /api/llm/switch` (models, current model, switch state) and `POST /api/llm/switch {"model": "<path>"}`. Only reachable on 127.0.0.1 with the Control Center session token.

## Debian edition
`victor-ctl switch <file.gguf>` or the **Switch model** tab. Debian serves the API straight from `llama-server` (no gateway in front), so **the API is unavailable while the new model loads**. Clients see connection errors for that time. Rollback to the previous model works the same way.

Model weights are never downloaded. Only files already in your model folders can be chosen.
