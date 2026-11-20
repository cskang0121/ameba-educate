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
| `aws_analyze_kvs_frames` | no (vision API only) | Send frames already saved in `frames/` to a vision model with your own `prompt` and return its text answer (the assistant does not see the images itself). Frames: the newest `max_frames` (default 1), or the files you name via `files` (only the file name is used, so Linux and `\\wsl.localhost\...` paths both work); at most 4 per call. Optional `crop_region` (`x0,y0,x1,y1` as fractions 0-1, e.g. `0,0.3,0.5,0.9`) zooms into part of the frame to read small text, and `context` adds extra info such as detector output. Frames are re-encoded to JPEG (max 1568 px on the long side). The answer is prefixed with the provider and model used, e.g. `[anthropic:claude-sonnet-5-5]` |

Tool names are `<transport>_<verb>_<target>`: `serial_*` talks to the board over the serial port, `aws_*` talks to AWS and needs only credentials. New tools should follow the same pattern.

## Requirements

- [Cline](https://cline.bot) (VS Code extension), and `uv` installed in WSL, with this repo in WSL. Cline starts the server itself through `wsl`; you don't run it by hand except to test it.
- An Ameba Pro2 connected over USB serial. Wi-Fi is needed for anything that uploads to the cloud.

Per feature:

- **`aws_*` tools:** AWS credentials on the PC with read-only permissions for the service in use (use a read-only key, not the board's upload key).
- **Video frames (`aws_download_kvs_frames`):** the board runs firmware that uploads to KVS, the stream has retention above 0, and the key can call `kinesisvideo:GetDataEndpoint` and `kinesisvideo:GetImages`.
- **Frame analysis (`aws_analyze_kvs_frames`):** an Anthropic or Gemini API key in `.env` (see Setup). Frames are sent to that provider's API, so don't use it on footage you may not share.

## Setup

In a WSL terminal, install the dependencies and check that the server starts:

```bash
cd ~/<path-to>/ameba-educate/mcp
uv sync
uv run server.py        # should show no error and wait quietly for input; Ctrl+C to stop
```

Then register it in Cline: open **MCP Servers → Configure MCP Servers** to edit `cline_mcp_settings.json`, and add the `ameba-mcp` entry from [`cline_mcp_settings.example.json`](cline_mcp_settings.example.json):

```json
{
  "mcpServers": {
    "ameba-mcp": {
      "command": "wsl",
      "args": [
        "-d", "Ubuntu",
        "--cd", "/your/wsl/path/to/repo/mcp",
        "/your/wsl/path/to/.local/bin/uv", "run", "server.py"
      ],
      "disabled": false
    }
  }
}
```

Replace the two paths: `--cd` is the WSL path to this `mcp/` folder, and the `uv` path is the output of `which uv` in WSL. Change `Ubuntu` if your WSL distribution has another name.

If you set up an earlier version, remove its entry from `cline_mcp_settings.json` first (for example `ameba-mcp-aws-kvs`, or any entry pointing at a folder that no longer exists). Otherwise the old entry can shadow the new one.

Credentials: copy `.env.example` to `.env` in this folder and fill it in. `.env` is git-ignored. AWS and `KVS_STREAM_NAME` values are read at server startup (restart the server after editing them). The vision keys are re-read from `.env` on every `aws_analyze_kvs_frames` call, so changing them needs no restart. Variables already set in the environment take priority over the file.

```
AWS_ACCESS_KEY_ID=<read-only key id>
AWS_SECRET_ACCESS_KEY=<read-only secret>
AWS_REGION=us-east-1
KVS_STREAM_NAME=kvs_example_camera_stream

# Vision analysis: set at least one key
ANTHROPIC_API_KEY=<key>
GEMINI_API_KEY=<key>
```

`AWS_REGION` is optional (the value above is the default) and `KVS_STREAM_NAME` is only used by `aws_download_kvs_frames`. Both must match where the board uploads. Instead of `.env`, you can run `aws configure` once, or add an `"env": { "NAME": "value" }` block to the server entry in `cline_mcp_settings.json` (this stores the secret in plain text in that file).

Vision provider selection for `aws_analyze_kvs_frames`: Anthropic is used if `ANTHROPIC_API_KEY` is set, otherwise Gemini (`GEMINI_API_KEY` or `GOOGLE_API_KEY`). Optional variables in `.env`:

| Variable | Default | Purpose |
|---|---|---|
| `VISION_PROVIDER` | (auto) | `anthropic` or `gemini` to force one provider |
| `ANTHROPIC_VISION_MODEL` | `claude-sonnet-5-5` | Anthropic model to use |
| `GEMINI_VISION_MODEL` | `gemini-2.5-flash` | Gemini model to use |

The server prints the selected provider to stderr at startup.

Afterwards, restart the server in Cline (toggle `ameba-mcp` off and on under **MCP Servers**) so it picks up the new settings. After changing dependencies with `uv sync`, restart it the same way.

## Usage

Ask your assistant in plain language. A typical session:

1. "List all available PC ports." — shows the serial ports you can connect to.
2. "Connect to COM5." — opens the board.
3. "Scan for Wi-Fi networks." then "Connect to Wi-Fi `<ssid>` with password `<password>`." — the assistant calls `serial_scan_wifi` and `serial_connect_wifi`, which reports the IP the board received. "Disconnect from Wi-Fi" leaves the network.
4. For anything else, "Run `ATW?`" or any AT command your firmware supports (`serial_send_at_command`).

### Example: object-triggered video (KVS firmware)

With the KVS example firmware, `serial_set_kvs_target` (`ATKVS=<objects>`, e.g. `person,car`) sets which objects the board watches for.

1. "Set the target object to cell phone." — then stand in front of the camera; the board streams about 30 seconds of video to KVS.
2. "Download frames from the KVS stream and tell me how many people are in them." — the assistant first calls `aws_download_kvs_frames` (saves recent frames into `frames/`; the default window is the last 2 minutes, at most 10 frames), then `aws_analyze_kvs_frames` with your question (at most 4 frames per call; the default prompt asks how many people are visible). To ask something else about older frames without downloading, ask it to analyze the frames in `frames/`. To read small text, ask it to zoom into a region of the frame.

Notes:

- Only one program can hold the serial port at a time; close Tera Term or other serial monitors first.
- Never put AWS keys in the firmware source, this repo or chat. If one is exposed, deactivate it in IAM. The same applies to Anthropic and Gemini keys: revoke them in the provider's console.
- The Wi-Fi password you give `serial_connect_wifi` passes through the chat and the MCP client's logs. Use a guest or throwaway network where that matters.
