"""
Ameba MCP server

Tools for working with an Ameba Pro2 board: control it over serial (AT commands)
and read data from cloud services. Tool names are <transport>_<verb>_<target>
(serial_*, aws_*); add new tools in the same style.
"""

import asyncio
import base64
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import boto3
import serial
import serial.tools.list_ports
from botocore.exceptions import NoCredentialsError
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

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
    """Send an AT command to the board's console and return its output. Which commands exist
    depends on the firmware. Examples: "ATW?" (Wi-Fi status), "ATWS" (Wi-Fi scan), and for the KVS
    example firmware "ATKVS=person,car" (record when one of those COCO objects is detected).
    Returns as soon as the console prints its end-of-command prompt. Set wait_for_prompt=False
    for slow commands whose result arrives after that prompt (e.g. ATWS); it then returns once
    the output has stayed quiet for idle_timeout_s (or after 10 s)."""
    if not board.connected:
        return _not_connected()
    try:
        return {"status": "success", "command": command, "response": await board.send(command, idle=idle_timeout_s, wait_for_prompt=wait_for_prompt)}
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


@mcp.tool()
async def aws_fetch_kvs_frames(stream_name: str = DEFAULT_STREAM, region: str = DEFAULT_REGION,
                               lookback_min: float = 2, interval_ms: int = 3000, max_frames: int = 10,
                               save_path: str = "frames") -> Dict[str, Any]:
    """Download JPEG frames from an AWS Kinesis Video Streams (KVS) stream (GetImages) and save
    them locally so the video content can be inspected: read the returned files to see what was recorded.
    Returns the newest max_frames frames in the last lookback_min minutes (AWS allows at most
    about 4 minutes per request; a longer window is rejected). Works without the
    serial connection. Uses AWS credentials from the standard chain (env vars or ~/.aws)."""
    if not 0 < lookback_min <= MAX_LOOKBACK_MIN:
        return {"status": "error",
                "error": f"lookback_min must be between 0 and {MAX_LOOKBACK_MIN} minutes (got {lookback_min}); "
                         "AWS rejects longer GetImages windows."}
    try:
        result = await asyncio.to_thread(
            _fetch_frames_sync, stream_name, region, lookback_min, interval_ms, max_frames, save_path
        )
    except NoCredentialsError:
        return {"status": "error",
                "error": "No AWS credentials found. Set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY or run 'aws configure'."}
    except Exception as e:
        return {"status": "error", "error": str(e)}

    saved = result["saved_files"]
    response: Dict[str, Any] = {"status": "success", "stream_name": stream_name,
                                "total_frames": len(saved), "saved_files": saved}
    if not saved:
        response["message"] = f"No frames found in the last {lookback_min} minutes for stream '{stream_name}'"
    return response


def main() -> None:
    # stdout carries the MCP protocol, so status text must go to stderr.
    print("Ameba MCP server starts successfully, waiting for an MCP client to connect ... (Ctrl+C to stop)", file=sys.stderr)
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
