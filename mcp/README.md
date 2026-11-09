# MCP

An MCP server for working with an Ameba Pro2 from an AI assistant. It controls the board over serial and reads data from cloud services. The tool set is meant to grow over time.

| Tool | Needs board (serial) | Purpose |
|---|---|---|
| `serial_list_pc_ports` | no | List the PC's serial ports |
| `serial_connect_board` | no | Open the Pro2's serial port |
| `serial_disconnect_board` | no | Close the Pro2's serial port |
| `serial_connect_wifi` | yes | Join a Wi-Fi network (`ssid`, optional `password`; runs `ATW0`/`ATW1`/`ATWC`) and confirm the board got an IP; returns `{ssid, ip}`. The password is masked in the output |
| `serial_disconnect_wifi` | yes | Leave the current Wi-Fi network (`ATWD`); also succeeds if the board was not connected |
| `serial_scan_wifi` | yes | Scan for nearby networks (`ATWS`) and return the raw listing |
| `serial_set_kvs_target` | yes | KVS firmware only: set which objects trigger a recording (`ATKVS=<objects>`). Names are checked against the 80 COCO classes, e.g. `person`, `car`, `cell phone` |
| `serial_send_at_command` | yes | Send any other AT command to the board's console and return its output, e.g. `ATW?`. Which commands exist depends on the firmware running on the board |
| `aws_download_kvs_frames` | no (AWS only) | Download recent video frames from an AWS Kinesis Video Streams (KVS) stream, saved to `frames/` inside this folder (a relative `save_path` is resolved against it, an absolute one is used as is). Returns only the saved file paths, not the images; when the server runs inside WSL it also returns them as `\\wsl.localhost\...` paths (`windows_paths`) |
| `aws_read_kvs_frames` | no | Return frames already saved in `frames/` as images the assistant can see: the newest `max_frames` (default 3), or the files you name (only the file name is used, so Linux and `\\wsl.localhost\...` paths both work). At most 4 images per call, each downscaled to 1024 px on the long side if Pillow is installed (otherwise sent as saved) |

Tool names are `<transport>_<verb>_<target>`: `serial_*` talks to the board over the serial port, `aws_*` talks to AWS and needs only credentials. New tools should follow the same pattern.

## Requirements

- Windows with `uv` and `claude` (the serial port is a Windows COM port), and this repo in WSL. All commands below run in **PowerShell on Windows** and reach the repo through its WSL path, `\\wsl.localhost\Ubuntu\home\<user>\ameba-edu-pro2`.
- An Ameba Pro2 connected over USB serial. Wi-Fi is needed for anything that uploads to the cloud.

Per feature:

- **`aws_*` tools:** AWS credentials on the PC with read-only permissions for the service in use (use a read-only key, not the board's upload key).
- **Video frames (`aws_download_kvs_frames`, then `aws_read_kvs_frames`):** the board runs firmware that uploads to KVS, the stream has retention above 0, and the key can call `kinesisvideo:GetDataEndpoint` and `kinesisvideo:GetImages`.

## Setup

PowerShell (Windows), repo on the WSL path. If a Linux `.venv` already exists in `mcp\` (from running `uv sync` inside WSL), delete it first; Windows `uv` cannot reuse it.

```powershell
cd \\wsl.localhost\Ubuntu\home\<user>\ameba-edu-pro2\mcp
uv sync
uv run server.py        # should show no error and wait quietly for input; Ctrl+C to stop
```

If you set up an earlier version, remove it first, including any copy deployed under another path. Otherwise the old registration can shadow the new one or point at a folder that no longer exists (PowerShell, Windows):

```powershell
claude mcp list                                  # look for ameba-mcp, ameba-mcp-aws-kvs, or any entry whose path is \\wsl.localhost\...
claude mcp remove ameba-mcp --scope user         # repeat for each old name; "No MCP server found" means it is already gone
```

If an entry was added at another scope, rerun with `--scope local` or `--scope project`. Then delete the old server folder under the WSL path, if there is one, and stop any running copy first.

Register it (PowerShell, Windows; name right after `add`; answer `n` to any update prompt):

```powershell
claude mcp add ameba-mcp --scope user -- uv --directory \\wsl.localhost\Ubuntu\home\<user>\ameba-edu-pro2\mcp run ameba-mcp
```

AWS credentials: copy `.env.example` to `.env` in this folder and fill it in. `.env` is git-ignored and is read at server startup (restart the server after editing it). Variables already set in the environment take priority over the file.

```
AWS_ACCESS_KEY_ID=<read-only key id>
AWS_SECRET_ACCESS_KEY=<read-only secret>
AWS_REGION=us-east-1
KVS_STREAM_NAME=kvs_example_camera_stream
```

`AWS_REGION` is optional (the value above is the default) and `KVS_STREAM_NAME` is only used by `aws_download_kvs_frames`. Both must match where the board uploads. Instead of `.env`, you can run `aws configure` once, or pass the variables with `claude mcp add -e NAME=value` (this stores the secret in plain text in `~/.claude.json`).

Start a new Claude Code session afterwards. To change dependencies later, stop the MCP server first or `uv sync` cannot replace its `.exe`.

## Usage

Ask your assistant in plain language. A typical session:

1. "List all available PC ports." — shows the serial ports you can connect to.
2. "Connect to COM5." — opens the board.
3. "Scan for Wi-Fi networks." then "Connect to Wi-Fi `<ssid>` with password `<password>`." — the assistant calls `serial_scan_wifi` and `serial_connect_wifi`, which reports the IP the board received. "Disconnect from Wi-Fi" leaves the network.
4. For anything else, "Run `ATW?`" or any AT command your firmware supports (`serial_send_at_command`).

### Example: object-triggered video (KVS firmware)

With the KVS example firmware, `serial_set_kvs_target` (`ATKVS=<objects>`, e.g. `person,car`) sets which objects the board watches for.

1. "Set the target object to cell phone." — then stand in front of the camera; the board streams about 30 seconds of video to KVS.
2. "Download frames from the KVS stream and show me what's in them." — the assistant first calls `aws_download_kvs_frames` (saves recent frames into `frames/`; the default window is the last 2 minutes, at most 10 frames), then `aws_read_kvs_frames` to view them (at most 4 per call). To look at older frames again without downloading, ask it to read the frames in `frames/`.

Notes:

- Only one program can hold the serial port at a time; close Tera Term or other serial monitors first.
- Never put AWS keys in the firmware source, this repo or chat. If one is exposed, deactivate it in IAM.
- The Wi-Fi password you give `serial_connect_wifi` passes through the chat and the MCP client's logs. Use a guest or throwaway network where that matters.
