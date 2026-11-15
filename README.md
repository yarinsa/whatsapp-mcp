# WhatsApp MCP Server

This is a Model Context Protocol (MCP) server for WhatsApp.

With this you can search and read your personal Whatsapp messages (including images, videos, documents, and audio messages), search your contacts and send messages to either individuals or groups. You can also send media files including images, videos, documents, and audio messages.

It connects to your **personal WhatsApp account** directly via the Whatsapp web multidevice API (using the [whatsmeow](https://github.com/tulir/whatsmeow) library). All your messages are stored locally in a SQLite database and only sent to an LLM (such as Claude) when the agent accesses them through tools (which you control).

Here's an example of what you can do when it's connected to Claude.

![WhatsApp MCP](./example-use.png)

> To get updates on this and other projects I work on [enter your email here](https://docs.google.com/forms/d/1rTF9wMBTN0vPfzWuQa2BjfGKdKIpTbyeKxhPMcEzgyI/preview)

> *Caution:* as with many MCP servers, the WhatsApp MCP is subject to [the lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/). This means that project injection could lead to private data exfiltration.

## Installation

### Prerequisites

- Go
- Python 3.6+
- Anthropic Claude Desktop app (or Cursor)
- UV (Python package manager), install with `curl -LsSf https://astral.sh/uv/install.sh | sh`
- FFmpeg (_optional_) - Only needed for audio messages. If you want to send audio files as playable WhatsApp voice messages, they must be in `.ogg` Opus format. With FFmpeg installed, the MCP server will automatically convert non-Opus audio files. Without FFmpeg, you can still send raw audio files using the `send_file` tool.

### Steps

1. **Clone this repository**

   ```bash
   git clone https://github.com/lharries/whatsapp-mcp.git
   cd whatsapp-mcp
   ```

2. **Run the WhatsApp bridge**

   Navigate to the whatsapp-bridge directory and run the Go application:

   ```bash
   cd whatsapp-bridge
   go run main.go
   ```

   The first time you run it, you will be prompted to scan a QR code. Scan the QR code with your WhatsApp mobile app to authenticate.

   After approximately 20 days, you will might need to re-authenticate.

3. **Connect to the MCP server**

   Copy the below json with the appropriate {{PATH}} values:

   ```json
   {
     "mcpServers": {
       "whatsapp": {
         "command": "{{PATH_TO_UV}}", // Run `which uv` and place the output here
         "args": [
           "--directory",
           "{{PATH_TO_SRC}}/whatsapp-mcp/whatsapp-mcp-server", // cd into the repo, run `pwd` and enter the output here + "/whatsapp-mcp-server"
           "run",
           "main.py"
         ]
       }
     }
   }
   ```

   For **Claude**, save this as `claude_desktop_config.json` in your Claude Desktop configuration directory at:

   ```
   ~/Library/Application Support/Claude/claude_desktop_config.json
   ```

   For **Cursor**, save this as `mcp.json` in your Cursor configuration directory at:

   ```
   ~/.cursor/mcp.json
   ```

4. **Restart Claude Desktop / Cursor**

   Open Claude Desktop and you should now see WhatsApp as an available integration.

   Or restart Cursor.

### Windows Compatibility

If you're running this project on Windows, be aware that `go-sqlite3` requires **CGO to be enabled** in order to compile and work properly. By default, **CGO is disabled on Windows**, so you need to explicitly enable it and have a C compiler installed.

#### Steps to get it working:

1. **Install a C compiler**  
   We recommend using [MSYS2](https://www.msys2.org/) to install a C compiler for Windows. After installing MSYS2, make sure to add the `ucrt64\bin` folder to your `PATH`.  
   → A step-by-step guide is available [here](https://code.visualstudio.com/docs/cpp/config-mingw).

2. **Enable CGO and run the app**

   ```bash
   cd whatsapp-bridge
   go env -w CGO_ENABLED=1
   go run main.go
   ```

Without this setup, you'll likely run into errors like:

> `Binary was compiled with 'CGO_ENABLED=0', go-sqlite3 requires cgo to work.`

## Running the bridge in the background (macOS)

Pair once in a terminal so the QR code can be scanned:

```bash
cd whatsapp-bridge && go run .
```

Then stop it (Ctrl+C) and install the launchd agents:

```bash
./scripts/install-launchd.sh
```

This builds `whatsapp-bridge/bridge-bin` and installs two agents:

- `com.yarin.whatsapp-bridge` starts the bridge at login and restarts it within about 10 seconds if it exits. Logs go to `~/Library/Logs/whatsapp-bridge.log`.
- `com.yarin.whatsapp-bridge-watchdog` checks `/api/health` every 5 minutes and shows a macOS notification after 30 minutes unreachable or disconnected, and again when it recovers.

The bridge itself also exits after 10 minutes without a WhatsApp connection so launchd starts a fresh process, and notifies after 30 minutes offline. A logged-out session is not restarted; pair again with `go run .`.

To remove the agents (your session in `store/` is left alone):

```bash
./scripts/uninstall-launchd.sh
```

After changing bridge code, run `./scripts/install-launchd.sh` again to rebuild and restart.

### Environment variables

| Variable | Used by | Default | Purpose |
| --- | --- | --- | --- |
| `WHATSAPP_BRIDGE_PORT` | bridge, MCP server, install script | `8765` | REST API port. Set the same value for both; pass it to the install script to bake it into the agents. |
| `WHATSAPP_API_BASE_URL` | MCP server | `http://127.0.0.1:$WHATSAPP_BRIDGE_PORT/api` | Full override of the bridge URL. |
| `WHATSAPP_QUIET_WARN_SECONDS` | MCP server | `21600` | `bridge_status` warns when connected but no message arrived for this long. |

For Claude Desktop or Claude Code, add the variable to the server's `env` in your MCP config when using a non-default port:

```json
"whatsapp": {
  "command": "uv",
  "args": ["--directory", "/path/to/whatsapp-mcp/whatsapp-mcp-server", "run", "main.py"],
  "env": { "WHATSAPP_BRIDGE_PORT": "8765" }
}
```

### Health check

```bash
curl -s http://127.0.0.1:8765/api/health
```

Returns `service`, `status` (`ok`, `disconnected`, `logged_out`), `connected`, `last_message_at`, `last_event_at`, `seconds_since_last_event` and uptime. The MCP server checks this at startup and before each tool call, so tools fail with a message such as "Bridge is down" or "Port 8765 is answered by another service" instead of returning empty results.

### Contact names

The bridge resolves WhatsApp LIDs to phone numbers and names using whatsmeow's LID map and contact store (full name, first name, business name, push name), and group subjects for groups. Names are stored in `chats.name` and a `contacts` table in `messages.db`, backfilled in the background a minute after the bridge connects, and updated on contact, push-name and group events. Force a refresh with:

```bash
curl -s -X POST http://127.0.0.1:8765/api/resync-names
```

### Read state

The bridge keeps `unread_count` and `marked_unread` on each chat. Incoming messages raise the count. It goes back to zero when you reply, read the chat on another device, or mark it read through the MCP. Counts start at zero for chats that existed before this was added, except where a history sync reports them. `POST /api/mark-read` with `{"chat_jid": "...", "read": true|false}` does the marking.

## Architecture Overview

This application consists of two main components:

1. **Go WhatsApp Bridge** (`whatsapp-bridge/`): A Go application that connects to WhatsApp's web API, handles authentication via QR code, and stores message history in SQLite. It serves as the bridge between WhatsApp and the MCP server.

2. **Python MCP Server** (`whatsapp-mcp-server/`): A Python server implementing the Model Context Protocol (MCP), which provides standardized tools for Claude to interact with WhatsApp data and send/receive messages.

### Data Storage

- All message history is stored in a SQLite database within the `whatsapp-bridge/store/` directory
- The database maintains tables for chats and messages
- Messages are indexed for efficient searching and retrieval

## Usage

Once connected, you can interact with your WhatsApp contacts through Claude, leveraging Claude's AI capabilities in your WhatsApp conversations.

### MCP Tools

Claude can access the following tools to interact with WhatsApp:

- **bridge_status**: Check whether the bridge is running, connected, and when the last message synced
- **search_contacts**: Search for people by name (Hebrew or English, saved or push name) or phone number; returns name, phone JID and LID
- **list_messages**: Retrieve messages with optional filters and context
- **list_chats**: List chats with name, JID, last message, `unread_count` and `marked_unread`; `unread_only=True` lists only unread chats
- **get_chat**: Get information about a specific chat
- **get_direct_chat_by_contact**: Find a direct chat with a specific contact
- **get_contact_chats**: List all chats involving a specific contact
- **get_last_interaction**: Get the most recent message with a contact
- **get_message_context**: Retrieve context around a specific message
- **send_message**: Send a WhatsApp message to a specified phone number or group JID
- **send_file**: Send a file (image, video, raw audio, document) to a specified recipient
- **send_audio_message**: Send an audio file as a WhatsApp voice message (requires the file to be an .ogg opus file or ffmpeg must be installed)
- **mark_chat_read**: Mark a chat as read (sends read receipts and clears the unread badge on your phone)
- **mark_chat_unread**: Mark a chat as unread on all your devices
- **download_media**: Download media from a WhatsApp message and get the local file path

### Media Handling Features

The MCP server supports both sending and receiving various media types:

#### Media Sending

You can send various media types to your WhatsApp contacts:

- **Images, Videos, Documents**: Use the `send_file` tool to share any supported media type.
- **Voice Messages**: Use the `send_audio_message` tool to send audio files as playable WhatsApp voice messages.
  - For optimal compatibility, audio files should be in `.ogg` Opus format.
  - With FFmpeg installed, the system will automatically convert other audio formats (MP3, WAV, etc.) to the required format.
  - Without FFmpeg, you can still send raw audio files using the `send_file` tool, but they won't appear as playable voice messages.

#### Media Downloading

By default, just the metadata of the media is stored in the local database. The message will indicate that media was sent. To access this media you need to use the download_media tool which takes the `message_id` and `chat_jid` (which are shown when printing messages containing the meda), this downloads the media and then returns the file path which can be then opened or passed to another tool.

## Technical Details

1. Claude sends requests to the Python MCP server
2. The MCP server queries the Go bridge for WhatsApp data or directly to the SQLite database
3. The Go accesses the WhatsApp API and keeps the SQLite database up to date
4. Data flows back through the chain to Claude
5. When sending messages, the request flows from Claude through the MCP server to the Go bridge and to WhatsApp

## Troubleshooting

- If you encounter permission issues when running uv, you may need to add it to your PATH or use the full path to the executable.
- Make sure both the Go application and the Python server are running for the integration to work properly. Ask Claude to run `bridge_status`, or `curl` the health endpoint, to see which side is failing.
- **"Route POST:/api/send not found" or "answered by another service"**: another app holds the bridge port. Pick a free port with `WHATSAPP_BRIDGE_PORT` for both the bridge (rerun the install script with it) and the MCP server config.
- **Bridge not running**: `launchctl list | grep whatsapp` and `tail -f ~/Library/Logs/whatsapp-bridge.log`.

### Authentication Issues

- **QR Code Not Displaying**: If the QR code doesn't appear, try restarting the authentication script. If issues persist, check if your terminal supports displaying QR codes.
- **WhatsApp Already Logged In**: If your session is already active, the Go bridge will automatically reconnect without showing a QR code.
- **Device Limit Reached**: WhatsApp limits the number of linked devices. If you reach this limit, you'll need to remove an existing device from WhatsApp on your phone (Settings > Linked Devices).
- **No Messages Loading**: After initial authentication, it can take several minutes for your message history to load, especially if you have many chats.
- **WhatsApp Out of Sync**: If your WhatsApp messages get out of sync with the bridge, delete both database files (`whatsapp-bridge/store/messages.db` and `whatsapp-bridge/store/whatsapp.db`) and restart the bridge to re-authenticate.

For additional Claude Desktop integration troubleshooting, see the [MCP documentation](https://modelcontextprotocol.io/quickstart/server#claude-for-desktop-integration-issues). The documentation includes helpful tips for checking logs and resolving common issues.
