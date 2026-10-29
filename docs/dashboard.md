# Local status dashboard

Run from the source checkout:

```sh
python3 -m ermik --config config.json dashboard --port 8765
```

Open `http://127.0.0.1:8765/`. JSON is available at `http://127.0.0.1:8765/api/status`. The server listens only on `127.0.0.1` and accepts a `Host` header containing `127.0.0.1` or `localhost`. It reads local SQLite state and queries GitHub for the ready queue at most once every 60 seconds. A read failure returns 503. The page refreshes every 10 seconds; the command runs until stopped.

The dashboard shows ready issues, occupied slots, job states, the last selected model, the model used for a published result, attempt counts, token totals, indicative cost estimates, retry and PR polling times, provider cooldowns, and recent events. It does not show remaining subscription quota because there is no verified common provider counter. It is read-only. Issue text, errors, branches, SHAs, logs, local paths, and secrets are omitted. JSON and HTML responses are limited to 128 KiB; visible lists are capped at 200 jobs, 100 queued issue numbers, 40 events, and 32 cooldowns. Separate flags indicate truncation of the job or queue list. Responses include `Cache-Control: no-store`.

This local page has no authentication. On a multi-user host, other local processes may reach the loopback port. Run it only on a trusted host and use a separately configured secure channel for remote viewing.
