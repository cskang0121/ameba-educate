# MCP

An MCP server for working with an Ameba Pro2 from an AI assistant. It controls the board over serial and reads data from cloud services. The tool set is meant to grow over time.

| Tool | Needs board (serial) | Purpose |
|---|---|---|
| `serial_list_pc_ports` | no | List the PC's serial ports |
| `serial_connect_board` | no | Open the Pro2's serial port |
| `serial_disconnect_board` | no | Close the Pro2's serial port |
| `serial_send_at_command` | yes | Send an AT command to the board's console and return its output, e.g. `ATW?`, `ATWS`, `ATW0=`, `ATW1=`, `ATWC`. Which commands exist depends on the firmware running on the board |
| `aws_fetch_kvs_frames` | no (AWS only) | Download recent video frames from an AWS Kinesis Video Streams (KVS) stream, saved to `frames/` inside this folder (a relative `save_path` is resolved against it, an absolute one is used as is) |

Tool names are `<transport>_<verb>_<target>`: `serial_*` talks to the board over the serial port, `aws_*` talks to AWS and needs only credentials. New tools should follow the same pattern.

## Requirements

- Windows with `uv` and `claude`. Run it from a Windows path, not `\\wsl.localhost\...`.
- An Ameba Pro2 connected over USB serial. Wi-Fi is needed for anything that uploads to the cloud.

Per feature:

- **`aws_*` tools:** AWS credentials on the PC with read-only permissions for the service in use (use a read-only key, not the board's upload key).
- **Video frames (`aws_fetch_kvs_frames`):** the board runs firmware that uploads to KVS, the stream has retention above 0, and the key can call `kinesisvideo:GetDataEndpoint` and `kinesisvideo:GetImages`.

## Setup

```powershell
cd C:\path\to\ameba-edu-pro2\mcp
uv sync
uv run server.py        # should show no error and wait quietly for input; Ctrl+C to stop
```

Register it (name right after `add`; answer `n` to any update prompt):

```powershell
claude mcp add ameba-mcp --scope user -- uv --directory C:\path\to\ameba-edu-pro2\mcp run ameba-mcp
```

AWS credentials: copy `.env.example` to `.env` in this folder and fill it in. `.env` is git-ignored and is read at server startup (restart the server after editing it). Variables already set in the environment take priority over the file.

```
AWS_ACCESS_KEY_ID=<read-only key id>
AWS_SECRET_ACCESS_KEY=<read-only secret>
AWS_REGION=us-east-1
KVS_STREAM_NAME=kvs_example_camera_stream
```

`AWS_REGION` is optional (the value above is the default) and `KVS_STREAM_NAME` is only used by the video-frame tool. Both must match where the board uploads. Instead of `.env`, you can run `aws configure` once, or pass the variables with `claude mcp add -e NAME=value` (this stores the secret in plain text in `~/.claude.json`).

Start a new Claude Code session afterwards. To change dependencies later, stop the MCP server first or `uv sync` cannot replace its `.exe`.

## Usage

Ask your assistant in plain language. A typical session:

1. "List all available PC ports." — shows the serial ports you can connect to.
2. "Connect to COM5." then "Run `ATW?`" — opens the board and checks that Wi-Fi has an IP. If not, join with `ATW0=<ssid>`, `ATW1=<password>`, `ATWC`.
3. Send whatever AT commands your firmware supports.

### Example: object-triggered video (KVS firmware)

With the KVS example firmware, `ATKVS=<objects>` (e.g. `ATKVS=person,car`) sets which objects the board watches for.

1. "Set the target object to cell phone." — then stand in front of the camera; the board streams about 30 seconds of video to KVS.
2. "Fetch frames from the KVS stream and show me what's in them." — downloads recent frames into `frames/` (the default window is the last 2 minutes, at most 4) and shows them.

Notes:

- Only one program can hold the serial port at a time; close Tera Term or other serial monitors first.
- Never put AWS keys in the firmware source, this repo or chat. If one is exposed, deactivate it in IAM.
