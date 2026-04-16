# mars-daemons

> Enterprise claude code — TypeScript client for the `mars-daemons` HTTP API.

Self-hosted agent runtime with persistent sandbox, per-user Unix isolation, SSE streaming, and resumable sessions. This package is the **TypeScript client** for calling a running `mars-daemons` daemon from Node.js (or any modern JS runtime with `fetch`).

The daemon itself is the Python package [`mars-daemons`](https://pypi.org/project/mars-daemons/) — run it with `python -m mars_runtime.daemon` or via the Docker image.

## Install

```bash
npm install mars-daemons
```

## Quick start

```typescript
import { MarsClient } from "mars-daemons";

const mars = new MarsClient({
  baseUrl: "http://mars-daemon.internal:8080",
  bearer: process.env.MARS_BEARER!,
});

// 1. Create a session for a specific user
const headers = { ownerSubject: "user_123", ownerRole: "user" as const };
const session = await mars.createSession(headers, { assistantId: "tariff-pro" });

// 2. Upload a file
const pdfBytes = await readFile("./invoice.pdf");
await mars.uploadFile(headers, session.sessionId, pdfBytes, "invoice.pdf");

// 3. Send a message and stream the agent's response
import { randomUUID } from "node:crypto";
const turnId = randomUUID();
for await (const event of mars.sendMessage(headers, session.sessionId, {
  turnId,
  text: "Classify invoice.pdf and give me the HS code.",
})) {
  if (event.type === "assistant_chunk") process.stdout.write(event.delta as string);
  if (event.type === "turn_completed") break;
}

// 4. Download files produced by the agent
const report = await mars.downloadFile(headers, session.sessionId, "output/classification.pdf");
```

## Reconnect after network drop

```typescript
let lastSeq = 0;
try {
  for await (const event of mars.sendMessage(headers, session.sessionId, { turnId, text })) {
    if ("sequence" in event) lastSeq = event.sequence as number;
    // ... handle event
  }
} catch (e) {
  // Reconnect and replay from lastSeq
  for await (const event of mars.getEvents(headers, session.sessionId, lastSeq)) {
    // ... resume from here
  }
}
```

## API

- `createSession(headers, { assistantId })` → `{ sessionId, createdAt, assistantId }`
- `getSession(headers, sessionId)` → metadata (no transcript)
- `sendMessage(headers, sessionId, { turnId, text })` → `AsyncIterable<WorkerEvent>`
- `getEvents(headers, sessionId, after)` → `AsyncIterable<ReplayEvent>` (replay + live-tail)
- `uploadFile(headers, sessionId, file, filename)` → `{ path }`
- `downloadFile(headers, sessionId, path)` → `Blob`
- `cancelTurn(headers, sessionId, turnId)` → `{ turn_id, state }`
- `health()` → `{ status }`
- `ready()` → `{ status }`

Headers (`SessionHeaders`):
- `ownerSubject: string` — opaque caller identity; the daemon persists and enforces it
- `ownerRole?: "user" | "admin"` — admin gets write access to `shared/`

## Backend contract

This client is designed to be called from a trusted backend (your Express/Fastify/Next API) — **not** directly from a browser. The daemon trusts the `X-Owner-Subject` header, so it must be set from verified identity on your side, never from user-controlled input.

## License

Apache-2.0 © 2026 Pedro Rios

## Links

- **Daemon source:** https://github.com/TacosyHorchata/mars-daemons
- **Python package (the daemon):** https://pypi.org/project/mars-daemons/
- **Issues:** https://github.com/TacosyHorchata/mars-daemons/issues
