# mars-daemons

> Claude Code for your whole company.

Your team. Your cloud. Your agents. Deploy in 5 minutes.

---

**Every employee gets a Claude Code-style workspace — in YOUR cloud.** Agents read files, accumulate memory, and produce artifacts there. Files survive sessions, restarts, everything.

- 📁 **Per-employee workspaces** — each user has their own directory of files, memory, and outputs. Same mental model as Claude Code, but hosted on your infra.
- 🏢 **Shared org-level directory** — common data, rules, agent configs, and knowledge base accessible to all employees read-only. Admins write once, everyone gets it.
- 🔒 **Kernel-level isolation** — each employee's workspace is locked to their Unix uid. OS-enforced privacy between users — no app-level bypass.
- 🛠️ **Skills & tools** — agents ship with `bash`, `read`, `edit`, `list`, `grep`, `glob`, `websearch`. Plug any MCP server for domain-specific tools.
- 🌊 **Streaming sessions** — SSE-native, resumable, restart-safe.
- 📦 **Your cloud** — ships as a Docker image. Deploy to AWS (Fargate + EFS), k8s, Fly, or bare metal. Your data never leaves your infrastructure.

---

## Quick start (local)

```bash
git clone https://github.com/TacosyHorchata/mars-daemons.git
cd mars-daemons
docker build -t mars-daemons:0.3.0 .

# Create a bearer token
openssl rand -hex 32 > /tmp/bearer.token
chmod 600 /tmp/bearer.token

# Run
docker run -p 8080:8080 \
  -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
  -e MARS_AUTH_TOKEN_FILE=/secrets/bearer.token \
  -v /tmp/bearer.token:/secrets/bearer.token:ro \
  -v $(pwd)/data:/data \
  mars-daemons:0.3.0
```

Smoke test:

```bash
BEARER=$(cat /tmp/bearer.token)

# Create a session
curl -X POST http://localhost:8080/v1/sessions \
  -H "Authorization: Bearer $BEARER" \
  -H "X-Owner-Subject: employee_123" \
  -H "Content-Type: application/json" \
  -d '{"assistant_id":"default"}'

# Send a message (SSE stream)
curl -N -X POST http://localhost:8080/v1/sessions/$SID/messages \
  -H "Authorization: Bearer $BEARER" \
  -H "X-Owner-Subject: employee_123" \
  -H "Content-Type: application/json" \
  -d '{"turn_id":"<uuid-v4>","text":"Hello"}'
```

---

## Deploy to AWS

> 🚧 **Coming soon.** One-command Python provisioner for Fargate + EFS per org. Meanwhile, the Dockerfile works with any ECS / k8s / Fly deployment — mount an EFS volume at `/data` and set `MARS_AUTH_TOKEN_FILE` + `ANTHROPIC_API_KEY`.

---

## How it works

```
  Browser → Your backend (Express / FastAPI / etc.)
              ↓
         Sets X-Owner-Subject (employee ID) + X-Owner-Role
              ↓
         mars daemon (per-org Fargate task)
              ↓
         Worker subprocess — drops privileges to employee's Unix uid
              ↓
         Per-employee workspace on EFS + shared/ org data
```

- Cross-org isolation = container + volume boundary
- Intra-org isolation = per-user Unix uid (kernel-enforced)
- Agents live in YAML files on the volume — edit them live, no redeploy

---

## Clients

- **TypeScript SDK:** [`clients/typescript/`](./clients/typescript/) — `npm install mars-daemons`
- **Raw HTTP:** `fetch` + SSE from any language

---

## Status

**v0.3.0 — enterprise beta.** Production runtime. Contracts stable. See [`CHANGELOG.md`](./CHANGELOG.md).

## License

[Apache 2.0](./LICENSE)
