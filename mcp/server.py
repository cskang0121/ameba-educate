"""
Ameba MCP server

Tools for working with an Ameba Pro2 board: control it over serial (AT commands)
and read data from cloud services. Tool names are <transport>_<verb>_<target>
(serial_*, aws_*); add new tools in the same style. Common AT sequences get their own
tool (serial_connect_wifi, ...); serial_send_at_command stays as the generic fallback.
"""

import asyncio
import base64
import io
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Optional, Union

import boto3
import serial
import serial.tools.list_ports
from botocore.exceptions import NoCredentialsError
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ImageContent, TextContent


MAX_READ_FRAMES = 4  # hard cap on images returned per call
PROJECT_DIR = Path(__file__).resolve().parent  # the mcp/ folder, next to pyproject.toml

# Project-root .env; variables already set in the environment win.
load_dotenv(PROJECT_DIR / ".env")

DEFAULT_STREAM = os.environ.get("KVS_STREAM_NAME", "kvs_example_camera_stream")
DEFAULT_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"

MAX_LOG_CHARS = 200_000
MAX_IMAGE_PAGES = 20
MAX_LOOKBACK_MIN = 4  # longest GetImages time window AWS accepts

# The firmware console prints this, then a "#" prompt, after every command (also unknown ones).
# The board streams log lines nonstop, so waiting for quiet never works; this marks the end instead.
CMD_DONE = re.compile(r"\[MEM\] After do cmd.*?#", re.DOTALL)

# "ATW?" prints "IP  => a.b.c.d" per interface; the first one is the station (wlan0).
WIFI_IP = re.compile(r"\bIP\s+=>\s+(\d+\.\d+\.\d+\.\d+)")

# Object names the detector can report (sdk/.../src/test_model/nn_utils/class_name.c, coco_names).
COCO_NAMES = frozenset({
    "person", "bicycle", "car", "motorbike", "aeroplane", "bus", "train", "truck", "boat", "traffic light",
    "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "sofa",
    "pottedplant", "bed", "diningtable", "toilet", "tvmonitor", "laptop", "mouse", "remote", "keyboard",
    "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush",
})

mcp = FastMCP("ameba-mcp")


class Board:
    """Serial link to the board."""

    def __init__(self) -> None:
        self.port: Optional[serial.Serial] = None
        self.port_name = ""
        self.log = ""
        self.log_start = 0  # absolute offset of log[0], grows when old text is dropped
        self.lost = ""  # why the link died, if the read loop gave up; cleared on open/close
        self._reader: Optional[asyncio.Task] = None
        self._cmd_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self.port is not None and self.port.is_open

    @property
    def end(self) -> int:
        return self.log_start + len(self.log)

    def since(self, pos: int) -> str:
        return self.log[max(0, pos - self.log_start):]

    async def _read_loop(self) -> None:
        while self.connected:
            try:
                waiting = self.port.in_waiting
                if waiting:
                    self.log += self.port.read(waiting).decode("utf-8", errors="ignore")
                    if len(self.log) > MAX_LOG_CHARS:
                        drop = len(self.log) - MAX_LOG_CHARS
                        self.log = self.log[drop:]
                        self.log_start += drop
            except (serial.SerialException, OSError) as e:
                self.lost = f"Serial link to {self.port_name} lost ({e}). Call serial_connect_board(port) to reconnect."
                self.port.close()  # makes `connected` False so tools stop trusting the dead port
                break
            await asyncio.sleep(0.05)

    async def open(self, port: str, baudrate: int) -> None:
        self.port = serial.Serial(port=port, baudrate=baudrate, timeout=0, write_timeout=1)
        self.port_name = port
        self.lost = ""
        self.log, self.log_start = "", 0
        self._reader = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            self._reader = None
        if self.port:
            self.port.close()
        self.port = None
        self.port_name = ""
        self.lost = ""

    async def send(self, command: str, idle: float = 1.0, max_seconds: float = 10.0,
                   wait_for_prompt: bool = True) -> str:
        """Write a command and return its output: up to the console's end-of-command prompt, or
        (no prompt seen, or wait_for_prompt False) once the output goes quiet or max_seconds passes."""
        async with self._cmd_lock:
            pos = self.end
            self.port.write(command.rstrip("\r\n").encode() + b"\r\n")
            t0 = last_change = time.monotonic()
            last_end = self.end
            while True:
                await asyncio.sleep(0.1)
                if not self.connected:
                    raise serial.SerialException(self.lost or "Serial port closed")
                now = time.monotonic()
                if wait_for_prompt:
                    out = self.since(pos)
                    done = CMD_DONE.search(out)
                    if done:
                        return out[:done.end()].strip()
                if self.end != last_end:
                    last_end, last_change = self.end, now
                if now - last_change >= idle or now - t0 >= max_seconds:
                    break
            return self.since(pos).strip()


board = Board()


def _not_connected() -> Dict[str, Any]:
    return {"status": "error", "error": board.lost or "Not connected. Call serial_connect_board(port) first."}


@mcp.tool()
async def serial_list_pc_ports() -> Dict[str, Any]:
    """List the serial ports available on this PC."""
    ports = [{"port": p.device, "description": p.description} for p in serial.tools.list_ports.comports()]
    return {"status": "success", "ports": ports}


@mcp.tool()
async def serial_connect_board(port: str, baudrate: int = 115200) -> Dict[str, Any]:
    """Open the board's serial port (e.g. "COM5"). Pro2 uses 115200 baud."""
    if board.connected:
        if port.upper() != board.port_name.upper():
            return {"status": "error",
                    "error": f"Already connected to {board.port_name}. Call serial_disconnect_board() before opening {port}."}
        return {"status": "success", "port": board.port_name, "message": "Already connected"}
    try:
        await board.open(port, baudrate)
        reply = await board.send("", idle=0.5, max_seconds=2.0)
        return {"status": "success", "port": port, "baudrate": baudrate, "response": reply}
    except Exception as e:
        await board.close()
        return {"status": "error", "error": str(e)}


@mcp.tool()
async def serial_disconnect_board() -> Dict[str, Any]:
    """Close the serial port. The board keeps running (detection stays armed)."""
    await board.close()
    return {"status": "success", "message": "Disconnected"}


@mcp.tool()
async def serial_send_at_command(command: str, idle_timeout_s: float = 1.0,
                                 wait_for_prompt: bool = True) -> Dict[str, Any]:
    """Send any AT command to the board's console and return its output. Which commands exist
    depends on the firmware. Prefer serial_connect_wifi, serial_disconnect_wifi, serial_scan_wifi
    and serial_set_kvs_target when one fits; use this for everything else (e.g. "ATW?" for status).
    Returns as soon as the console prints its end-of-command prompt. Set wait_for_prompt=False
    for slow commands whose result arrives after that prompt; it then returns once
    the output has stayed quiet for idle_timeout_s (or after 10 s)."""
    if not board.connected:
        return _not_connected()
    try:
        return {"status": "success", "command": command, "response": await board.send(command, idle=idle_timeout_s, wait_for_prompt=wait_for_prompt)}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _bad_line(value: str) -> bool:
    """True if value would break out of a single console line."""
    return any(c in value for c in "\r\n\0")


async def _wifi_ip(tries: int = 1) -> Optional[str]:
    """The station's IPv4 address from ATW?, or None if it has none yet."""
    for i in range(tries):
        if i:
            await asyncio.sleep(1)
        m = WIFI_IP.search(await board.send("ATW?"))
        if m and m.group(1) != "0.0.0.0":
            return m.group(1)
    return None


@mcp.tool()
async def serial_connect_wifi(ssid: str, password: str = "") -> Dict[str, Any]:
    """Join a Wi-Fi network (ATW0/ATW1/ATWC) and confirm the board got an IP address. Leave
    password empty for an open network. Takes up to about 30 s. The board prints the password
    back on its console; it is masked in this tool's output."""
    if not board.connected:
        return _not_connected()
    if not ssid or len(ssid.encode()) > 32 or _bad_line(ssid) or _bad_line(password):
        return {"status": "error", "error": "ssid must be 1-32 bytes; ssid and password must be single-line text."}

    def mask(text: str) -> str:
        return text.replace(password, "***") if password else text

    try:
        steps = [f"ATW0={ssid}"] + ([f"ATW1={password}"] if password else [])
        for step in steps:
            cmd = step.split("=")[0]
            reply = await board.send(step)
            if f"[{cmd}]Usage" in reply or f"[{cmd}]Error" in reply:
                return {"status": "error", "error": f"{cmd} rejected the value.", "response": mask(reply)}
        reply = mask(await board.send("ATWC", idle=30.0, max_seconds=30.0))
        if "Got IP" not in reply:
            return {"status": "error", "error": "Could not join the network (check SSID, password and signal).", "response": reply}
        ip = await _wifi_ip(tries=5)
        if not ip:
            return {"status": "error", "error": "Joined the network but no IP address was assigned (DHCP).", "response": reply}
        return {"status": "success", "ssid": ssid, "ip": ip}
    except Exception as e:
        return {"status": "error", "error": mask(str(e))}


@mcp.tool()
async def serial_disconnect_wifi() -> Dict[str, Any]:
    """Leave the current Wi-Fi network (ATWD). Succeeds too if the board was not connected."""
    if not board.connected:
        return _not_connected()
    try:
        reply = await board.send("ATWD", idle=25.0, max_seconds=25.0)
    except Exception as e:
        return {"status": "error", "error": str(e)}
    if "ERROR" in reply:
        return {"status": "error", "error": "Disconnect failed.", "response": reply}
    return {"status": "success", "response": reply}


@mcp.tool()
async def serial_scan_wifi() -> Dict[str, Any]:
    """Scan for nearby Wi-Fi networks (ATWS) and return the raw console listing (SSID, signal,
    channel, security). Takes a few seconds."""
    if not board.connected:
        return _not_connected()
    try:
        return {"status": "success", "response": await board.send("ATWS", idle=4.0, max_seconds=15.0, wait_for_prompt=False)}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@mcp.tool()
async def serial_set_kvs_target(objects: List[str]) -> Dict[str, Any]:
    """KVS example firmware only: choose which objects trigger a recording (ATKVS=<objects>). Names
    must be COCO class names such as "person", "car", "cell phone", "dog". The board then streams
    video to KVS when one of them is detected. The reply is the firmware's own text, not parsed."""
    if not board.connected:
        return _not_connected()
    names = [o.strip().lower() for o in objects if o.strip()]
    unknown = [n for n in names if n not in COCO_NAMES]
    if not names or unknown:
        return {"status": "error",
                "error": f"Unknown object name(s): {unknown}." if unknown else "Give at least one object name.",
                "hint": "Names are COCO classes, e.g. person, car, dog, cell phone, laptop, backpack."}
    try:
        return {"status": "success", "objects": names, "response": await board.send("ATKVS=" + ",".join(names))}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _fetch_frames_sync(stream_name: str, region: str, lookback_min: float, interval_ms: int,
                       max_frames: int, save_path: str) -> Dict[str, Any]:
    kvs = boto3.client("kinesisvideo", region_name=region)
    endpoint = kvs.get_data_endpoint(StreamName=stream_name, APIName="GET_IMAGES")["DataEndpoint"]
    media = boto3.client("kinesis-video-archived-media", region_name=region, endpoint_url=endpoint)

    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=lookback_min)
    images: List[Dict[str, Any]] = []
    token = None
    for _ in range(MAX_IMAGE_PAGES):
        params = dict(
            StreamName=stream_name,
            ImageSelectorType="SERVER_TIMESTAMP",
            StartTimestamp=start,
            EndTimestamp=end,
            SamplingInterval=interval_ms,
            Format="JPEG",
            MaxResults=100,
        )
        if token:
            params["NextToken"] = token
        response = media.get_images(**params)
        images += [i for i in response.get("Images", []) if i.get("ImageContent")]
        token = response.get("NextToken")
        if not token:
            break

    out_dir = PROJECT_DIR / save_path  # relative paths land in mcp/, not the server's working dir; absolute paths are kept
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for image in images[-max_frames:]:  # newest frames in the window
        ts = image["TimeStamp"].strftime("%Y%m%d_%H%M%S_%f")
        path = out_dir / f"{stream_name}_{ts}.jpg"
        path.write_bytes(base64.b64decode(image["ImageContent"]))
        saved.append(str(path.resolve()))
    return {"saved_files": saved}


# @mcp.tool()
# async def aws_fetch_kvs_frames(stream_name: str = DEFAULT_STREAM, region: str = DEFAULT_REGION,
#                                lookback_min: float = 2, interval_ms: int = 3000, max_frames: int = 10,
#                                save_path: str = "frames") -> Dict[str, Any]:
#     """Download JPEG frames from an AWS Kinesis Video Streams (KVS) stream (GetImages) and save
#     them locally so the video content can be inspected: read the returned files to see what was recorded.
#     Returns the newest max_frames frames in the last lookback_min minutes (AWS allows at most
#     about 4 minutes per request; a longer window is rejected). Works without the
#     serial connection. Uses AWS credentials from the standard chain (env vars or ~/.aws)."""
#     if not 0 < lookback_min <= MAX_LOOKBACK_MIN:
#         return {"status": "error",
#                 "error": f"lookback_min must be between 0 and {MAX_LOOKBACK_MIN} minutes (got {lookback_min}); "
#                          "AWS rejects longer GetImages windows."}
#     try:
#         result = await asyncio.to_thread(
#             _fetch_frames_sync, stream_name, region, lookback_min, interval_ms, max_frames, save_path
#         )
#     except NoCredentialsError:
#         return {"status": "error",
#                 "error": "No AWS credentials found. Set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY or run 'aws configure'."}
#     except Exception as e:
#         return {"status": "error", "error": str(e)}

#     saved = result["saved_files"]
#     response: Dict[str, Any] = {"status": "success", "stream_name": stream_name,
#                                 "total_frames": len(saved), "saved_files": saved}
#     if not saved:
#         response["message"] = f"No frames found in the last {lookback_min} minutes for stream '{stream_name}'"
#     return response

def _to_wsl_unc(p: str) -> str:
    """Linux path in WSL -> \\\\wsl.localhost\\<distro>\\... path usable from Windows."""
    distro = os.environ.get("WSL_DISTRO_NAME")
    if not distro or not p.startswith("/"):
        return p
    return "\\\\wsl.localhost\\" + distro + p.replace("/", "\\")


@mcp.tool()
async def aws_download_kvs_frames(stream_name: str = DEFAULT_STREAM, region: str = DEFAULT_REGION,
                                  lookback_min: float = 2, interval_ms: int = 3000,
                                  max_frames: int = 10, save_path: str = "frames") -> Dict[str, Any]:
    """Download JPEG frames from an AWS KVS stream and save them locally.
    Returns only the saved file paths (Linux and Windows-style). Call aws_read_kvs_frames
    afterwards to view them."""
    if not 0 < lookback_min <= MAX_LOOKBACK_MIN:
        return {"status": "error",
                "error": f"lookback_min must be between 0 and {MAX_LOOKBACK_MIN} minutes (got {lookback_min})."}
    try:
        result = await asyncio.to_thread(
            _fetch_frames_sync, stream_name, region, lookback_min, interval_ms, max_frames, save_path
        )
    except NoCredentialsError:
        return {"status": "error", "error": "No AWS credentials found. Set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY or run 'aws configure'."}
    except Exception as e:
        return {"status": "error", "error": str(e)}

    saved = result["saved_files"]
    response: Dict[str, Any] = {"status": "success", "total_frames": len(saved), "saved_files": saved}
    if os.environ.get("WSL_DISTRO_NAME"):
        response["windows_paths"] = [_to_wsl_unc(p) for p in saved]
    if not saved:
        response["message"] = f"No frames found in the last {lookback_min} minutes for stream '{stream_name}'"
    return response


def _load_jpeg(path: Path, max_side: int = 1024) -> bytes:
    """Downscale to keep the payload small."""
    try:
        from PIL import Image as PILImage
    except ImportError:
        return path.read_bytes()
    with PILImage.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=80)
        return buf.getvalue()


@mcp.tool()
async def aws_read_kvs_frames(save_path: str = "frames", max_frames: int = 3,
                              files: Optional[List[str]] = None):
    """Return previously downloaded KVS frames as images the model can see.
    Uses the newest max_frames files in save_path, or the given file names/paths
    (Linux or \\\\wsl.localhost style; only the file name is used). At most 4 images per call."""
    base = (PROJECT_DIR / save_path).resolve()
    if not base.is_dir():
        return [f"Folder '{save_path}' not found. Run aws_download_kvs_frames first."]

    if files:
        # PureWindowsPath splits on both '/' and '\', so any path style works
        paths = [(base / PureWindowsPath(f).name).resolve() for f in files]
        paths = [p for p in paths if p.parent == base and p.is_file()]
        paths = sorted(paths, reverse=True)  # newest first (names contain timestamps)
    else:
        paths = sorted(base.glob("*.jpg"), reverse=True)[:max_frames]

    if not paths:
        return [f"No JPEG frames found in '{save_path}'."]

    out = []
    for p in paths[:MAX_READ_FRAMES]:
        data = await asyncio.to_thread(_load_jpeg, p)
        out.append(TextContent(type="text", text=p.name))
        out.append(ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType="image/jpeg"))
    return out

def main() -> None:
    # stdout carries the MCP protocol, so status text must go to stderr.
    print("Ameba MCP server starts successfully, waiting for an MCP client to connect ... (Ctrl+C to stop)", file=sys.stderr)
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
