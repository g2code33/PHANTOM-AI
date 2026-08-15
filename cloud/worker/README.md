# Phantom — Portable Cloud Worker

The always-on, lightweight Phantom for when your PC is off. Runs on Cloudflare
Workers (free tier). It has **no PC access by design** — cloud-only tools:
memory, reminders, web search, chat (NVIDIA), voice STT (Deepgram).

## Deploy (one time, ~5 minutes)

1. Install wrangler:  `npm i -g wrangler`  (or use `npx wrangler`)
2. Log in:           `npx wrangler login`   (opens browser → free Cloudflare account)
3. Create the KV namespaces (run 3×, copy each returned id):
   ```
   npx wrangler kv namespace create MEMORY
   npx wrangler kv namespace create PROFILE
   npx wrangler kv namespace create REMINDERS
   ```
4. Paste the 3 ids into `wrangler.toml` (replace `REPLACE_WITH_KV_*_ID`).
5. Set secrets:
   ```
   npx wrangler secret put NVIDIA_API_KEY
   npx wrangler secret put DEEPGRAM_API_KEY
   npx wrangler secret put PHANTOM_CLOUD_TOKEN    # optional; strong token recommended
   ```
6. Deploy:
   ```
   npx wrangler deploy
   ```
   It prints your worker URL: `https://phantom-portable.<your-subdomain>.workers.dev`

## Verify

```
curl https://phantom-portable.<sub>.workers.dev/api/status
# → {"ok":true,"mode":"portable","cloud":true,...}
```

## Use from the companion app

Set the app's **Portable URL** to `https://phantom-portable.<sub>.workers.dev`
and (if you set a token) the **Cloud token**. The app auto-switches to it when
your PC tunnel is unreachable, and the manual toggle forces either mode.

## API (same shape as the PC companion, cloud subset)

- `GET  /api/status`
- `POST /api/chat`           `{text}` → `{reply, tools, mode:"portable"}`
- `POST /api/voice/stt`      raw WAV → `{transcript}`
- `GET/POST /api/memory`     cloud memory (this is the "save to cloud" store)
- `GET/PUT /api/profile`     shared profile (JOOJO)
- `GET/POST /api/reminders`
- `GET  /api/briefing`       spoken-style cloud briefing
