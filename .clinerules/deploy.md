# Deployment — building and shipping `inverter-monitor` with GitHub Actions

**Status: repo changes applied 2026-11-27, NOT committed, NOT yet deployable.**
The live stack on `acer` was deliberately left untouched (§7 closes the ordering
dependency before the first deploy).

Reference implementation: `tailucas/simple-app` (`.clinerules/github-actions.md`).
Runner facts: home-lab `.clinerules/github-runner.md`, `docs/ci-deploy.md`.
This file covers only what is different for **this** app.

## 1. What this application is

A **hardware-facing collector**. It speaks to an inverter/BMS over a **USB serial
device**, persists readings to SQLite, and exposes a Telegram bot (with Gemini
image analysis and Sonoff switch control) plus a Prometheus metrics endpoint.

Three consequences that shape deployment:

* it needs a **device node**, not just a port;
* it is a **long-running poller** — no HTTP request path to exercise;
* it runs a **Telegram polling bot**, which is a **singleton per bot token**.

## 2. Runtime contract

| Requirement | Detail |
|---|---|
| **Serial device** | `devices: ["/dev/ttyUSB0:/dev/ttyUSB0"]`. The host node is `crw-rw---- root:dialout`; the image adds `app` to `dialout` (`usermod -aG dialout app`). If the USB adapter re-enumerates as `/dev/ttyUSB1`, the container fails to start — that is a host problem, not an image problem. |
| **1Password Connect** | Runtime dependency: `Creds()` is used in `app/__main__.py`, `app/sonoff.py`, `app/telegram_bot.py`, `app/gemini_image.py`. Live it arrives via compose `secrets:` → `/run/secrets/*`; `.env` has **no `OP_*`**. |
| **Telegram token** | One poller only. Two containers with the same bot token produce `Conflict: terminated by other getUpdates request` and **both** stop responding. Never run a second copy for a canary, and never deploy this stack to `giada` as a spare. |
| **`BMS_SERIAL_PORT`** | Must match the container-side device path (`/dev/ttyUSB0`). |
| **`./data`** | See §3. |
| **Locale** | `LANG`/`LANGUAGE`/`LC_ALL` in `.env`. |

The build needs **no** secret (`uv sync --no-dev` + PyPI only), and this project
has **no Java**, so the Maven-`PATH` workaround is irrelevant.

## 3. `./data/` — what it is, and the rules

`/home/tai/inverter-monitor/data` (bind-mounted at `/data`, **284 KB** on
2026-11-27):

| Path | What it is |
|---|---|
| `gauge_all_20.db`, `gauge_all_21.db` | **One SQLite database per inverter** (`TABLESPACE_PATH`-style naming). The long-term history lives here. |
| `supervisord.log` | supervisord's own log (pid/log file target). |
| `cron-std{out,err}---supervisor-*.log` | cron program output captured by supervisord. |

Rules:

* The databases **grow continuously** (one row per sample). Treat them as state to
  preserve, not cache to clear — a deploy must never recreate `data/`.
* When you need a consistent copy, stop the stack first. Do not `cp` a live
  SQLite file that is being written.
* `data/` is gitignored and excluded by `.dockerignore`; it must never be sent to
  a CI runner.
* Ownership must stay uid 999 (`app`).

## 4. Current deployment (verified 2026-11-27, read-only)

| Fact | Value |
|---|---|
| Host / dir | `acer`, `/home/tai/inverter-monitor` |
| Container | `inverter-monitor-app-1`, image `tailucas/inverter-monitor`, up, `restarts=0` |
| Ports | 9401→9401 (metrics) |
| Devices | `/dev/ttyUSB0` (present on `acer`, `root:dialout`) |
| Image ref | `tailucas/inverter-monitor:latest` (`lab_image_inverter_monitor`) |
| Catalog | `ansible/vars/container_catalog.yml` → `inverter-monitor` |
| Secrets live | `/run/secrets/op_connect_token`, `/run/secrets/op_vault`; no `OP_*` in `.env` |
| Repo | `tailucas/inverter-monitor`, branch **`master`** |

## 5. What is specific to this project

1. **Branch is `master`, not `main`.**
2. **The deploy preflight must check the serial device**, or the failure is
   confusing (`error gathering device information while adding custom device`):
   ```bash
   test -c /dev/ttyUSB0 || { echo "::error::/dev/ttyUSB0 is missing"; exit 1; }
   ```
   `test -c` proves it is a character device; `-e` would accept a stale symlink.
3. **The device is briefly released during recreate.** `compose up -d` stops the
   old container before starting the new one, so the BMS serial handle closes and
   reopens. That is normal, but it means a deploy coincides with a short gap in
   readings — do not deploy while you are watching a live gauge.
4. **`build: network: host` is present** in the compose file. It is harmless on
   the Linux CI runner, but it is why a local build on a non-Linux Docker host
   can behave differently. Leave it unless you can prove the build works without
   it.
5. **There is no usable readiness endpoint.** Port 9401 is published and
   `docker-proxy` accepts connections, but `curl http://127.0.0.1:9401/metrics`
   returned **no response** on 2026-11-27, and the app's own log lines do not
   reach `docker logs` at all. So verification is supervisord-based (§6), exactly
   like `net-tool`.
6. **`ruff` and `mypy` are both clean** on `app/` (12 source files), so CI gates
   on both.
7. **`.python-version` is already `3.14`.**

## 6. Verifying a deploy

```bash
cid=$(docker compose ps -q app)
docker logs "$cid" 2>&1 | grep "success: app entered RUNNING state"
sleep 30
! docker logs "$cid" 2>&1 | grep -q "exited:"
docker inspect "$cid" --format '{{.State.Status}} restarts={{.RestartCount}}'
```

Why not something better:

* `/metrics` on 9401 accepts a TCP connection via `docker-proxy` but does not
  answer — it is a pre-existing condition of this stack, not a deploy signal.
* `log.info("Startup complete.")` exists (`app/__main__.py:2482`) but does not
  appear in `docker logs`, so it cannot be asserted here.
* `ss -ltn` is worthless: every published port is accepted whether or not the app
  is behind it.

`restarts=0` after the settle window is the strongest available local evidence;
the real confirmation is a reading appearing in a `gauge_all_*.db`, or a Telegram
reply.

## 7. Before the first deploy — ordering matters

The repo compose no longer declares `secrets:`, but the **host was not touched**:

* `/home/tai/inverter-monitor/docker-compose.yml` still has `secrets:` and still
  relies on the operator's shell;
* `/home/tai/inverter-monitor/.env` still has **no `OP_*`**.

Before deploying, in this order:

1. Add `OP_CONNECT_HOST=http://host.docker.internal:8082`, `OP_CONNECT_TOKEN`,
   `OP_VAULT` to the host `.env` (`chmod 600`). The token is already inside the
   running container: `docker exec <cid> cat /run/secrets/op_connect_token` copies
   it without printing it.
2. Mirror the `secrets:` removal on the host compose.
3. Prove it: `env -u OP_CONNECT_TOKEN -u OP_VAULT docker compose up -d --force-recreate`,
   then confirm the container stays up with `restarts=0` and a new row lands in
   `data/gauge_all_*.db`.
4. Only then grant the repo to `acer-deploy`.

## 8. Related documents

| Document | Why |
|---|---|
| `tailucas/simple-app` → `.clinerules/github-actions.md` | reference implementation and environment/1Password rules |
| `tailucas/net-tool` → `.clinerules/deploy.md` | the supervisord-based verification pattern this file reuses |
| home-lab → `docs/ci-deploy.md` | adoption checklist and ownership boundary |
| home-lab → `.clinerules/github-runner.md` | runner names, groups, labels, silent-failure pitfalls |
| home-lab → `.clinerules/hosts.md` | `/dev/ttyUSB0` is listed there as a stack prerequisite |

