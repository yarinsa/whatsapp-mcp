---
name: deploy-railway
description: Deploy or redeploy the remote WhatsApp MCP (bridge + OAuth MCP server) to Railway with the railway CLI, including first-time setup (project, service, volume, domain, variables), QR pairing and verification. Use when asked to deploy, redeploy, ship, update or check the cloud/Railway WhatsApp MCP.
---

# Deploy the WhatsApp MCP to Railway

Production: project `whatsapp-mcp`, service `whatsapp-mcp`, URL **https://whatsapp.yarinsa.me/mcp**
(fallback `https://whatsapp-mcp-production-168c8b3c.up.railway.app`, which only works if PUBLIC_URL is switched back to it).

The `Dockerfile` runs the Go bridge and the Python MCP server (OAuth over HTTP,
see `whatsapp-mcp-server/auth.py`) in one container. State lives on a Railway
volume at `/app/whatsapp-bridge/store`: the WhatsApp session, message history
and OAuth tokens. **Never delete or detach that volume.** If you do, WhatsApp
has to be paired again and every connector has to log in again.

Run everything from the repo root. Use Railway CLI v4 commands. Check flags with
`railway <cmd> --help` rather than guessing.

## 0. Preflight

```bash
railway whoami          # if "Unauthorized": ask the user to run `railway login` (browser), then continue
railway status          # shows the linked project/service, if any
```

- If `railway status` shows a linked project with a `whatsapp-mcp` service, this is a **redeploy**. Skip to step 3.
- If you have local Python changes, check that stdio mode still loads before you deploy:
  `cd whatsapp-mcp-server && uv run python -c "import main"`.

## 1. First-time setup (only when no project is linked)

```bash
railway list                                   # reuse an existing "whatsapp-mcp" project if present: railway link <id>
railway init --name whatsapp-mcp               # otherwise create it
railway add --service whatsapp-mcp
railway service whatsapp-mcp                   # link this dir to the service
railway volume add --mount-path /app/whatsapp-bridge/store
railway domain whatsapp.yarinsa.me --port 8000  # custom domain; prints the CNAME target
```

Point DNS at Railway with the AWS CLI (Route 53 zone `yarinsa.me`, `Z0485986YWI9IGQUXQAA`). Use the
CNAME target that `railway domain` printed (currently `mpd6lv42.up.railway.app`):

```bash
aws route53 change-resource-record-sets --hosted-zone-id Z0485986YWI9IGQUXQAA --change-batch \
  '{"Changes":[{"Action":"UPSERT","ResourceRecordSet":{"Name":"whatsapp.yarinsa.me","Type":"CNAME","TTL":300,"ResourceRecords":[{"Value":"<target>.up.railway.app"}]}}]}'
```

Railway also asks for a verification TXT record (the dashboard shows the value). Add it at
`_railway-verify.whatsapp.yarinsa.me` with the same command, using `"Type":"TXT"` and a quoted value
(`"\\"railway-verify=<hash>\\""`). Railway issues the TLS certificate once both resolve, which takes a few minutes.

Then set the variables. Ask the user for their phone number if it isn't known
(digits only, with country code, e.g. 9725XXXXXXXX). Don't guess it.

```bash
railway variables --set "PUBLIC_URL=https://whatsapp.yarinsa.me" \
  --set "WHATSAPP_OWNER_PHONE=<digits>" --set "PORT=8000"
```

`PUBLIC_URL` must exactly match the domain, with no trailing slash. It becomes
the OAuth issuer and the only Host header the server accepts.

## 2. Deploy (automatic from GitHub)

The service is connected to `yarinsa/whatsapp-mcp`, branch `main`
(`railway service source connect --repo yarinsa/whatsapp-mcp --branch main --service whatsapp-mcp`).
**Every push to `main` on the `fork` remote deploys automatically.** Follow it with
`railway service status --service whatsapp-mcp` or `railway deployment list`.

`railway up --ci` still works for deploying the uncommitted local tree, but the next push replaces it.

## 3. Redeploy

Push to `main`, or run `railway service redeploy`. The volume persists, so no re-pairing or re-login is needed.

## 4. Pair WhatsApp (first deploy, or after "logged out")

```bash
railway logs --deployment | tail -80
```

- If the logs show `Scan this QR code`, ask the user to scan it from WhatsApp → Settings → Linked Devices.
  The terminal QR may render badly. If it does, have them open the logs in the dashboard (`railway open`).
  QR codes rotate about every 20 seconds, so re-read the logs if one expires.
- If the logs show `Connected to WhatsApp` or recent message activity, it's already paired.
- The cloud bridge is a separate linked device. Never upload the local `whatsapp-bridge/store/`.
  It is excluded by `.dockerignore`, so keep it that way.

## 5. Verify

```bash
D=https://whatsapp.yarinsa.me
curl -s -o /dev/null -w "%{http_code}\n" -X POST $D/mcp                     # expect 401
curl -s $D/.well-known/oauth-authorization-server | head -c 300              # issuer must equal PUBLIC_URL
```

## 6. Connect Claude clients (one connector for all of them)

Add it **once** as a claude.ai custom connector. It then syncs to the Claude iPhone/Android
apps, Claude Desktop and Claude Code (Claude Code loads claude.ai connectors automatically
when it's logged in with the same account). This step needs the user's own claude.ai session:
1. claude.ai → Settings → Connectors → **Add custom connector**.
   Name it `WhatsApp` and set the URL to `https://whatsapp.yarinsa.me/mcp`. Leave the advanced OAuth fields empty, since clients register themselves.
2. Click **Connect**. The login page opens, and a 6-digit code arrives in the user's own WhatsApp chat.
   Entering the code finishes the connection.
3. In a chat, enable the connector from the tools menu.

After the connector works, **remove the old local copies** so the tools don't show up twice and two bridges don't run:
- In Claude Desktop, delete the `whatsapp` entry from `~/Library/Application Support/Claude/claude_desktop_config.json`
  (make a backup first), then restart the app.
- In Claude Code, run `claude mcp remove whatsapp` if `claude mcp get whatsapp` finds a local stdio entry.
- Stop the local bridge: `scripts/uninstall-launchd.sh`. It's a separate linked device, so unlink it in WhatsApp → Linked Devices too.

Use `claude mcp add --transport http whatsapp https://whatsapp.yarinsa.me/mcp` only for a Claude Code
setup that isn't signed in to claude.ai. It runs the same OAuth and WhatsApp-code flow.

## Pairing tip

Reading the QR code out of `railway logs` is unreliable because codes rotate every ~20s and the logs lag.
Render the newest code on a local page that polls the logs every few seconds. Use
`timeout 10 railway logs --deployment`, take the last `Scan this QR code` block, and draw each
`█▀▄` half-block as two pixel rows. Pairing succeeded once the logs show `Successfully connected`.

## Troubleshooting

- **The login page says it couldn't send the code**: the bridge isn't connected. Check the logs for a QR code or `logged out`.
- **421 or "Invalid Host"**: `PUBLIC_URL` doesn't match the domain being used.
- **Connector says invalid redirect URI**: add the client's callback prefix to `OAUTH_ALLOWED_REDIRECT_PREFIXES` (comma-separated; this replaces the defaults in `auth.py`, so include them).
- **The container keeps restarting**: `start-remote.sh` exits when either process dies. Read `railway logs --deployment`.
- Keep `numReplicas` at 1. Two bridges on one session will kick each other off.
