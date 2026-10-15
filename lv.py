# -*- coding: utf-8 -*-
import sys
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")
os.environ['PYTHONUNBUFFERED'] = '1'
os.environ.setdefault('PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION', 'python')
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import asyncio
import httpx
import random
import json
import socket
import struct
import time
import uuid
import itertools
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

from google_play_scraper import app as play_scraper
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from protobuf_decoder.protobuf_decoder import Parser
from message_ids import MESSAGE_ID_TO_NAME
import thunderFF_pb2

# Default to a modest per-worker queue cadence rather than a synchronized
# one-request-per-second burst across the whole fleet. Server acceptance varies.
START_MATCH_INTERVAL = max(1.0, min(30.0, float(os.getenv("START_MATCH_INTERVAL_SECONDS", "3"))))
# Across ALL accounts: two StartMatch requests never leave this process closer than
# this. Four accounts searching on their own 3s cadence used to arrive together at the
# same gateway, which the game server answers by not settling matches (0 EXP).
MATCHMAKING_MIN_GAP_SECONDS = max(0.2, min(10.0, float(os.getenv("MATCHMAKING_MIN_GAP_SECONDS", "1.2"))))
_matchmaking_last_sent = 0.0


async def matchmaking_slot() -> float:
    """Wait for this account's turn to send a match request; returns the wait time.

    Serialising the sends keeps the aggregate rate of a multi-account fleet identical to
    a single account's rate instead of multiplying it. The check-and-claim below contains
    no await, so two accounts can never claim the same slot (single-threaded event loop),
    and nothing is bound to a specific loop.
    """
    global _matchmaking_last_sent
    waited = 0.0
    while True:
        now = time.monotonic()
        gap = MATCHMAKING_MIN_GAP_SECONDS - (now - _matchmaking_last_sent)
        if gap <= 0:
            _matchmaking_last_sent = now
            return waited
        await asyncio.sleep(gap)
        waited += gap
MAX_MATCH_DURATION = 1500
# Reconnect rather than display a stale match for 25 minutes after UDP stops.
UDP_IDLE_RECONNECT_SECONDS = max(60, int(os.getenv("UDP_IDLE_RECONNECT_SECONDS", "180")))
UDP_JOIN_TIMEOUT_SECONDS = max(20, min(300, int(os.getenv("UDP_JOIN_TIMEOUT_SECONDS", "75"))))
# How often the worker re-reads the real server profile. Lower = faster proof that
# the server did or did not credit EXP; 15s is the floor so the API is not hammered.
PROFILE_POLL_SECONDS = max(15, min(600, int(os.getenv("PROFILE_POLL_SECONDS", "45"))))
# Hard safety ceilings for the profile API. They exist so neither the hosting box nor
# the game servers ever see a burst: the poll has a floor, a ceiling, a per-account
# debounce for forced checks and a small global concurrency cap. Failures back off.
PROFILE_POLL_MAX_SECONDS = max(60, min(3600, int(os.getenv("PROFILE_POLL_MAX_SECONDS", "300"))))
PROFILE_FORCED_MIN_INTERVAL_SECONDS = max(5, min(120, int(os.getenv("PROFILE_FORCED_MIN_INTERVAL_SECONDS", "10"))))
PROFILE_CHECK_CONCURRENCY = max(1, min(20, int(os.getenv("PROFILE_CHECK_CONCURRENCY", "3"))))
GAME_API_BACKOFF_THRESHOLD = max(2, min(50, int(os.getenv("GAME_API_BACKOFF_THRESHOLD", "5"))))
# Never hold this during a sleep: it only spans the HTTP round trip.
_PROFILE_SEMAPHORE = asyncio.Semaphore(PROFILE_CHECK_CONCURRENCY)


def profile_poll_delay(fail_streak: int = 0, base: Optional[float] = None) -> float:
    """Next profile poll delay for one account, backed off after repeated failures.

    The floor (15s) and ceiling (PROFILE_POLL_MAX_SECONDS) are the safety rails: a
    game API that refuses requests is retried more slowly and can never be hammered.
    """
    base_seconds = float(base if base is not None else PROFILE_POLL_SECONDS)
    streak = max(0, int(fail_streak))
    if streak >= GAME_API_BACKOFF_THRESHOLD:
        steps = 1 + (streak - GAME_API_BACKOFF_THRESHOLD) // GAME_API_BACKOFF_THRESHOLD
        base_seconds = base_seconds * (2 ** min(steps, 4))
    return max(15.0, min(float(PROFILE_POLL_MAX_SECONDS), base_seconds))


def forced_refresh_wait(last_check_at: Optional[float], now: float,
                        min_interval: Optional[float] = None) -> float:
    """Wait time before a forced check so bursts never turn into extra game API calls."""
    interval = float(min_interval if min_interval is not None else PROFILE_FORCED_MIN_INTERVAL_SECONDS)
    if not last_check_at:
        return 0.0
    return max(0.0, interval - max(0.0, now - float(last_check_at)))
MATCH_IDLE_TIMEOUT = 2
NON_MATCH_RECONNECT_DELAY = 0.5

MODE_ID = 1
MAP_ID = 1

ACCOUNT_REGION = os.getenv("DEFAULT_ACCOUNT_REGION", "ME")

xK = os.getenv("FF_AES_KEY", "Yg&tc%DEuh6%Zc^8").encode("utf-8")[:16].ljust(16, b"\x00")
xV = os.getenv("FF_AES_IV", "6oyZDr22E3ychjM%").encode("utf-8")[:16].ljust(16, b"\x00")
AES_KEY = xK
AES_IV = xV
GARENA_CLIENT_ID = os.getenv("GARENA_CLIENT_ID", "100067")
GARENA_CLIENT_SECRET = os.getenv(
    "GARENA_CLIENT_SECRET",
    "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3",
)
ACCESS_JWT_API_URL = os.getenv(
    "ACCESS_JWT_API_URL", "https://sabbir-paid-api2.vercel.app/access-jwt"
).strip()


class AccessJWTExchangeError(RuntimeError):
    """Safe, user-facing summary for an Access Token → JWT API failure."""


class GetLoginDataError(RuntimeError):
    """Sanitized GetLoginData failure detail; never carries response bodies or tokens."""


DEVICES_FILE_PATH = os.getenv("DEVICES_FILE_PATH", "devices.json")

REGION_SERVERS = {
    "IND": "https://clientbp.ppmainecoonghj.com/",
    "ID": "https://clientbp.ppmainecoonghj.com/",
    "BR": "https://clientbp.ppmainecoonghj.com/",
    "ME": "https://clientbp.ppmainecoonghj.com/",
    "VN": "https://clientbp.ppmainecoonghj.com/",
    "TH": "https://clientbp.ppmainecoonghj.com/",
    "CIS": "https://clientbp.ppmainecoonghj.com/",
    "BD": "https://clientbp.ppmainecoonghj.com/",
    "PK": "https://clientbp.ppmainecoonghj.com/",
    "SG": "https://clientbp.ppmainecoonghj.com/",
    "NA": "https://clientbp.ppmainecoonghj.com/",
    "SAC": "https://clientbp.ppmainecoonghj.com/",
    "EUROPE": "https://clientbp.ppmainecoonghj.com/",
    "TW": "https://clientbp.ppmainecoonghj.com/",
}

REGION_LANG = {
    "BD": "bn", "IND": "hi", "PK": "ur", "SG": "en", "ID": "id",
    "ME": "ar", "VN": "vi", "TW": "zh", "CIS": "ru", "TH": "th",
    "EU": "en", "US": "en", "SAC": "es", "LK": "en", "BR": "pt",
}

REGION_PREFIX = {
    "ME": "031900",
    "IND": "031400",
    "BD": "031400",
    "SG": "031500",
    "TH": "031500",
    "PH": "031500",
    "VN": "031500",
    "MY": "031500",
    "ID": "031500",
    "HK": "031500",
    "TW": "031500",
    "PK": "031500",
    "CIS": "031500",
    "BR": "031500",
    "NA": "031500",
    "SAC": "031500",
    "EUROPE": "031500",
}

CRC7_TABLE = bytes([
    0, 9, 18, 27, 36, 45, 54, 63, 72, 65, 90, 83, 108, 101, 126, 119,
    25, 16, 11, 2, 61, 52, 47, 38, 81, 88, 67, 74, 117, 124, 103, 110,
    50, 59, 32, 41, 22, 31, 4, 13, 122, 115, 104, 97, 94, 87, 76, 69,
    43, 34, 57, 48, 15, 6, 29, 20, 99, 106, 113, 120, 71, 78, 85, 92,
    100, 109, 118, 127, 64, 73, 82, 91, 44, 37, 62, 55, 8, 1, 26, 19,
    125, 116, 111, 102, 89, 80, 75, 66, 53, 60, 39, 46, 17, 24, 3, 10,
    86, 95, 68, 77, 114, 123, 96, 105, 30, 23, 12, 5, 58, 51, 40, 33,
    79, 70, 93, 84, 107, 98, 121, 112, 7, 14, 21, 28, 35, 42, 49, 56,
    65, 72, 83, 90, 101, 108, 119, 126, 9, 0, 27, 18, 45, 36, 63, 54,
    88, 81, 74, 67, 124, 117, 110, 103, 16, 25, 2, 11, 52, 61, 38, 47,
    115, 122, 97, 104, 87, 94, 69, 76, 59, 50, 41, 32, 31, 22, 13, 4,
    106, 99, 120, 113, 78, 71, 92, 85, 34, 43, 48, 57, 6, 15, 20, 29,
    37, 44, 55, 62, 1, 8, 19, 26, 109, 100, 127, 118, 73, 64, 91, 82,
    60, 53, 46, 39, 24, 17, 10, 3, 116, 125, 102, 111, 80, 89, 66, 75,
    23, 30, 5, 12, 51, 58, 33, 40, 95, 86, 77, 68, 123, 114, 105, 96,
    14, 7, 28, 21, 42, 35, 56, 49, 70, 79, 84, 93, 98, 107, 112, 121,
])

_DELTA = 0x9E3779B9
_ROUNDS = 16
_FIELD_SIZES = {0: 1, 1: 2, 2: 2, 3: 1, 4: 2}
_FIELD_NAMES = {0: "sendOption", 1: "cmd", 2: "orderId", 3: "flags", 4: "length"}

CLOUDFLARE_PRIMARY_DNS = "1.1.1.1"
CLOUDFLARE_SECONDARY_DNS = "1.0.0.1"
_DNS_CACHE: Dict[str, Tuple[str, float]] = {}
_DNS_CACHE_TTL = 300.0

client = httpx.AsyncClient(
    verify=False,
    timeout=15.0,
    limits=httpx.Limits(max_connections=300, max_keepalive_connections=150)
)
# Access Tokens are high-value secrets: use a separate, certificate-verifying HTTPS client.
ACCESS_API_CLIENT = httpx.AsyncClient(
    verify=True,
    timeout=15.0,
    limits=httpx.Limits(max_connections=50, max_keepalive_connections=25),
)
# Shared by CLI and dashboard. Do not hold this during retry/backoff sleeps.
_LOGIN_SEMAPHORE = asyncio.Semaphore(max(1, min(100, int(os.getenv("MAX_CONCURRENT_LOGINS", "8")))))

headers = {
    'User-Agent': 'UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)',
    'Connection': 'Keep-Alive',
    'Accept-Encoding': 'gzip',
    'Content-Type': 'application/x-www-form-urlencoded',
    'Expect': '100-continue',
    'X-Unity-Version': '2018.4.12f1',
    'X-GA-SV': '1789535859',
    'X-GA': 'v1 1',
    'ReleaseVersion': 'OB55'
}


_LOG_CALLBACKS = []


def register_log_callback(cb):
    if cb not in _LOG_CALLBACKS:
        _LOG_CALLBACKS.append(cb)


def unregister_log_callback(cb):
    if cb in _LOG_CALLBACKS:
        _LOG_CALLBACKS.remove(cb)


def log(msg):
    print(msg, flush=True)
    for cb in list(_LOG_CALLBACKS):
        try:
            cb(str(msg))
        except Exception:
            pass


def print_error(msg): log(f"[-] {msg}")
def print_success(msg): log(f"[+] {msg}")
def print_info(msg): log(f"[i] {msg}")
def print_warning(msg): log(f"[!] {msg}")


def get_proto_field(d, key, default=None):
    if not d or not isinstance(d, dict):
        return default
    if key in d:
        val = d[key].get('data')
        return val if val is not None else default
    if str(key) in d:
        val = d[str(key)].get('data')
        return val if val is not None else default
    return default


def _pb_varint(n):
    if n < 0:
        n = (1 << 64) + n
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            break
    return bytes(out)


def _pb_tag(f, w):
    return _pb_varint((f << 3) | w)


def _pb_field(f, v):
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, int):
        return _pb_tag(f, 0) + _pb_varint(v)
    if isinstance(v, str):
        d = v.encode('utf-8')
        return _pb_tag(f, 2) + _pb_varint(len(d)) + d
    if isinstance(v, (bytes, bytearray)):
        d = bytes(v)
        return _pb_tag(f, 2) + _pb_varint(len(d)) + d
    return b""


def crc7(data):
    c = 0
    for b in data:
        c = CRC7_TABLE[((2 * (c & 0xFF)) ^ (b & 0xFF)) & 0xFF] & 0x7F
    return c & 0x7F


def zigzag_encode(n):
    z = n << 1
    out = bytearray()
    while z >= 0x80:
        out.append((z & 0x7F) | 0x80)
        z >>= 7
    out.append(z)
    return bytes(out)


def uleb_encode(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            break
    return bytes(out)


def _generate_new_device() -> dict:
    device_list = [
        ("Samsung", "SM-G998B", "Adreno (TM) 660", "Android OS 12 / API-31"),
        ("Xiaomi", "2201122G", "Adreno (TM) 730", "Android OS 13 / API-33"),
        ("Realme", "RMX3700", "Mali-G710", "Android OS 14 / API-34"),
        ("OnePlus", "CPH2451", "Adreno (TM) 740", "Android OS 13 / API-33"),
        ("OPPO", "CPH2611", "Adreno (TM) 720", "Android OS 14 / API-34"),
        ("Vivo", "V2203", "Mali-G710", "Android OS 12 / API-31"),
        ("Poco", "M2102J20SG", "Adreno (TM) 660", "Android OS 13 / API-33"),
    ]
    brand, model, gpu, os_ver = random.choice(device_list)
    return {
        "unique_device_id": f"Google|{str(uuid.uuid4())}",
        "brand": brand, "model": model, "gpu_renderer": gpu, "system_software": os_ver,
        "screen_width": random.choice([1080, 1440, 720, 1280]),
        "screen_height": random.choice([2400, 3200, 1600, 2400]),
        "screen_dpi": str(random.randint(300, 420)),
        "memory": random.randint(2800, 6500),
        "processor_details": f"ARM64 FP ASIMD AES VMH | {random.randint(2200, 3200)} | {random.randint(6, 12)}",
        "client_ip": f"{random.randint(103, 223)}.{random.randint(10, 250)}.{random.randint(10, 250)}.{random.randint(10, 250)}"
    }


_DEVICES_MEM_CACHE: Dict[str, dict] = {}


def get_device_for_account(account_identifier: str) -> dict:
    global _DEVICES_MEM_CACHE
    key = str(account_identifier).strip() or "default"
    if key in _DEVICES_MEM_CACHE and isinstance(_DEVICES_MEM_CACHE[key], dict):
        return _DEVICES_MEM_CACHE[key]

    devices_map = _DEVICES_MEM_CACHE
    if not devices_map:
        try:
            if os.path.exists(DEVICES_FILE_PATH):
                with open(DEVICES_FILE_PATH, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        devices_map = loaded
                        _DEVICES_MEM_CACHE = loaded
        except Exception:
            devices_map = {}

    if key in devices_map and isinstance(devices_map[key], dict):
        return devices_map[key]

    new_dev = _generate_new_device()
    devices_map[key] = new_dev
    _DEVICES_MEM_CACHE[key] = new_dev
    try:
        parent = os.path.dirname(DEVICES_FILE_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(DEVICES_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(devices_map, f, indent=2)
    except Exception:
        pass
    return new_dev


async def resolve_host_cloudflare(hostname: str) -> str:
    if not hostname:
        return hostname
    parts = hostname.split('.')
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return hostname
    now = time.time()
    if hostname in _DNS_CACHE:
        ip, exp = _DNS_CACHE[hostname]
        if now < exp:
            return ip

    def _query_cloudflare(server_ip: str) -> Optional[str]:
        s = None
        try:
            tx_id = random.randint(1000, 65535)
            header = struct.pack(">HHHHHH", tx_id, 0x0100, 1, 0, 0, 0)
            qname = b"".join(bytes([len(part)]) + part.encode('ascii') for part in hostname.split('.')) + b"\x00"
            query_pkt = header + qname + struct.pack(">HH", 1, 1)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.2)
            s.sendto(query_pkt, (server_ip, 53))
            resp, _ = s.recvfrom(1024)
            if len(resp) >= 12:
                ancount = struct.unpack(">H", resp[6:8])[0]
                if ancount > 0:
                    offset = 12 + len(qname) + 4
                    for _ in range(ancount):
                        if offset >= len(resp):
                            break
                        if (resp[offset] & 0xC0) == 0xC0:
                            offset += 2
                        else:
                            while offset < len(resp) and resp[offset] != 0:
                                offset += 1 + resp[offset]
                            offset += 1
                        if offset + 10 > len(resp):
                            break
                        rtype, rclass, ttl, rdlen = struct.unpack(">HHIH", resp[offset:offset+10])
                        offset += 10
                        if rtype == 1 and rdlen == 4 and offset + 4 <= len(resp):
                            return socket.inet_ntoa(resp[offset:offset+4])
                        offset += rdlen
        except Exception:
            pass
        finally:
            if s:
                try: s.close()
                except Exception: pass
        return None

    loop = asyncio.get_running_loop()
    ip = await loop.run_in_executor(None, _query_cloudflare, CLOUDFLARE_PRIMARY_DNS)
    if not ip:
        ip = await loop.run_in_executor(None, _query_cloudflare, CLOUDFLARE_SECONDARY_DNS)
    if not ip:
        try:
            ip_info = await loop.getaddrinfo(hostname, None, family=socket.AF_INET)
            if ip_info:
                ip = ip_info[0][4][0]
        except Exception:
            ip = hostname
    if ip:
        _DNS_CACHE[hostname] = (ip, now + _DNS_CACHE_TTL)
    return ip or hostname


def optimize_tcp_socket(sock: socket.socket):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "SIO_KEEPALIVE_VALS") and os.name == 'nt':
            try: sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 10000, 2000))
            except Exception: pass
        elif hasattr(socket, "TCP_KEEPIDLE"):
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
                if hasattr(socket, "TCP_KEEPINTVL"):
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 2)
                if hasattr(socket, "TCP_KEEPCNT"):
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)
            except Exception: pass
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 131072)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 131072)
    except Exception:
        pass


def optimize_udp_socket(sock: socket.socket):
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 131072)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 131072)
        if hasattr(socket, 'SIO_UDP_CONNRESET') and os.name == 'nt':
            try: sock.ioctl(socket.SIO_UDP_CONNRESET, False)
            except Exception: pass
    except Exception:
        pass


async def safe_close_writer(writer):
    if not writer:
        return
    try:
        if not writer.is_closing():
            writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout=1.5)
    except Exception:
        pass


async def aes_encrypt(payload, key, iv):
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return cipher.encrypt(pad(payload, AES.block_size))


async def get_playstore_version():
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None, lambda: play_scraper('com.dts.freefireth', lang='hi', country='id')
        )
        return result.get("version")
    except Exception:
        return "1.132.8"


async def version_config(region: Optional[str] = None):
    reg = (region or ACCOUNT_REGION or "ME").upper()
    app_version = await get_playstore_version()
    lang = REGION_LANG.get(reg, "en")
    api_url = (
        "https://version.ggwhitehawk.com/live/ver.php"
        f"?version={app_version}"
        f"&lang={lang}&device=android&channel=android"
        f"&appstore=googleplay&region={reg}"
        "&whitelist_version=1.3.0&whitelist_sp_version=1.0.0"
    )
    try:
        response = await client.get(api_url)
        response.raise_for_status()
        data = response.json()
        server_url = data.get("server_url")
        remote_version = data.get("remote_version")
        latest_release_version = data.get("latest_release_version")
        if not server_url or not remote_version or not latest_release_version:
            return None
        return latest_release_version, remote_version, server_url
    except Exception:
        return None


async def get_access_token(uid, password):
    url = "https://100067.connect.garena.com/oauth/guest/token/grant"
    hdrs = {
        "Host": "100067.connect.garena.com",
        "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 12; SM-G998B Build/SP1A.210812.016)",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "close"
    }
    data = {
        "uid": uid, "password": password,
        "response_type": "token", "client_type": "2",
        "client_secret": GARENA_CLIENT_SECRET,
        "client_id": GARENA_CLIENT_ID
    }
    for attempt in range(5):
        try:
            response = await client.post(url, headers=hdrs, data=data)
            if response.status_code == 200:
                rd = response.json()
                open_id = rd.get("open_id")
                access_token = rd.get("access_token")
                platform = rd.get("platform", 4)
                if open_id and access_token:
                    return open_id, access_token, platform
            if response.status_code == 429:
                await asyncio.sleep(1)
                continue
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return None


async def exchange_access_token_for_jwt(access_token: str) -> Dict[str, Any]:
    """POST a manually supplied Access Token to the configured HTTPS exchange API.

    The API supports two successful shapes:
    * raw Access Token: it resolves an Open ID and returns a newly issued game JWT;
    * already-issued JWT: it returns that JWT directly and may leave Open ID/platform null.

    The original value is sent only in the POST body and is never logged or returned.
    """
    token = (access_token or "").strip()
    if not token:
        raise AccessJWTExchangeError("No Access Token was supplied.")
    if not ACCESS_JWT_API_URL.lower().startswith("https://"):
        raise AccessJWTExchangeError("Access-token API URL must use HTTPS; request was not sent.")

    try:
        response = await ACCESS_API_CLIENT.post(
            ACCESS_JWT_API_URL,
            json={"access_token": token},
            headers={"Accept": "application/json"},
        )
    except Exception as exc:
        # Do not expose exception text: HTTP libraries may include request details.
        raise AccessJWTExchangeError(
            f"Access-token API request failed ({type(exc).__name__})."
        ) from None

    if response.status_code != 200:
        if response.status_code == 422:
            detail = "API could not extract an Open ID; the token may be invalid, expired, or unsupported."
        elif response.status_code == 502:
            detail = "API could not complete its JWT/MajorLogin step."
        else:
            detail = "API rejected the request."
        raise AccessJWTExchangeError(f"{detail} (HTTP {response.status_code}).")

    try:
        body = response.json()
    except Exception:
        raise AccessJWTExchangeError("Access-token API returned invalid JSON (HTTP 200).") from None
    if not isinstance(body, dict) or str(body.get("status", "")).lower() not in {"success", "ok"}:
        raise AccessJWTExchangeError("Access-token API did not return a successful response (HTTP 200).")

    jwt_value = body.get("jwt")
    if not isinstance(jwt_value, str) or not jwt_value.strip() or len(jwt_value) > 16384:
        raise AccessJWTExchangeError("Access-token API response did not include a usable JWT.")
    jwt_value = jwt_value.strip()

    open_id_raw = body.get("open_id")
    open_id = str(open_id_raw).strip() if isinstance(open_id_raw, (str, int)) else ""
    direct_jwt = not bool(open_id)
    if direct_jwt and not (jwt_value.startswith("ey") and jwt_value.count(".") >= 2):
        raise AccessJWTExchangeError(
            "API response omitted Open ID and did not contain a recognizable direct JWT."
        )

    player_info = body.get("player_info")
    if not isinstance(player_info, dict):
        player_info = {}

    platform_raw = body.get("platform_type_used", body.get("platform_type"))
    if platform_raw in (None, ""):
        platform_raw = player_info.get("platform_id", 4)
    try:
        platform = int(platform_raw)
    except (TypeError, ValueError):
        platform = 4

    api_account_id = (
        player_info.get("account_id")
        or player_info.get("game_id")
        or player_info.get("uid")
        or player_info.get("id")
        or body.get("account_id")
        or ""
    )
    region = str(player_info.get("region") or body.get("region") or "").strip().upper()
    nickname = str(
        player_info.get("nickname") or player_info.get("name") or player_info.get("nick_name") or ""
    ).strip()[:80]
    if nickname.lower() in {"unknown", "none", "null"}:
        nickname = ""
    if region.lower() in {"unknown", "none", "null"}:
        region = ""

    return {
        "jwt": jwt_value,
        "open_id": open_id,
        "auth_mode": "direct_jwt" if direct_jwt else "access_token",
        "open_id_source": str(body.get("open_id_source") or "")[:64],
        "platform_type_used": platform,
        "account_id": str(api_account_id).strip(),
        "region": region,
        "nickname": nickname,
        "player_info": player_info,
    }


async def parse_results(parsed_results):
    result_dict = {}
    for result in parsed_results:
        field_data = {"wire_type": result.wire_type}
        if result.wire_type in ("varint", "string", "bytes"):
            field_data["data"] = result.data
        elif result.wire_type == "length_delimited":
            if hasattr(result.data, "results"):
                field_data["data"] = await parse_results(result.data.results)
            elif isinstance(result.data, list):
                field_data["data"] = await parse_results(result.data)
            else:
                field_data["data"] = str(result.data)
        result_dict[str(result.field)] = field_data
    return result_dict


async def decode_protobuf(data):
    parsed_results = Parser().parse(data)
    parsed_results_dict = await parse_results(parsed_results)
    return json.dumps(parsed_results_dict)


async def build_majorlogin_payload(open_id, access_token, platform, client_version, device_info, verr=None, honor_platform=False):
    try:
        if verr is None:
            verr = client_version
        proto = thunderFF_pb2.MajorLoginReq()
        proto.event_time = str(datetime.now())[:-7]
        proto.game_name = "free fire"
        proto.platform_id = 1
        proto.client_version = str(verr)
        proto.client_version_code = "2019121229"
        proto.system_software = "Android OS 15 / API-35 (AP3A.240905.015.A2/185014)"
        proto.system_hardware = "Handheld"
        proto.device_type = "Handheld"
        proto.screen_width = 1600
        proto.screen_height = 719
        proto.screen_dpi = "234"
        proto.processor_details = "ARM64 FP ASIMD AES | 1820 | 8"
        proto.memory = 2798
        proto.gpu_renderer = "Mali-G57"
        proto.gpu_version = "OpenGL ES 3.2 v1.r49p1-04eac0.2848c17a2fd4e9340e06555168eaa3c9"
        proto.unique_device_id = "Google|f744e396-5694-4e65-995d-97a958f2bd1f"
        proto.client_ip = "197.0.137.129"
        proto.language = "pt-br"
        platform_type = 4
        if honor_platform:
            try:
                platform_type = int(platform)
            except (TypeError, ValueError):
                platform_type = 4
        proto.open_id = str(open_id)
        proto.open_id_type = str(platform_type)
        proto.login_open_id_type = platform_type
        proto.access_token = str(access_token)
        proto.login_by = 2
        proto.platform_sdk_id = 1
        proto.origin_platform_type = str(platform_type)
        proto.primary_platform_type = str(platform_type)
        proto.reg_avatar = 1
        proto.channel_type = 3
        proto.telecom_operator = "TUNTEL"
        proto.network_operator_a = "TUNTEL"
        proto.network_type = "WIFI"
        proto.network_type_a = "WIFI"
        proto.cpu_type = 2
        proto.cpu_architecture = "64"
        proto.graphics_api = "OpenGLES2"
        proto.supported_astc_bitset = 8191
        proto.client_using_version = "7428b253defc164018c604a1ebbfebdf"
        proto.loading_time = 15078
        proto.release_channel = "android"
        proto.extra_info = "KqsHTx3+QOmBRR1WKvaWewlcpqJBfjki+PPHQoQG8+0yV+Uos7gUFFjHMQ/e7u6han6Fl77r7c3vMN3p8UbKgN+nfycQCgwBmWgBzomx2gj84c+p"
        proto.android_engine_init_flag = 111207
        proto.if_push = 1
        proto.is_vpn = 0

        memory_available = proto.memory_available
        memory_available.version = 55
        memory_available.hidden_value = 81

        proto.external_storage_total = 49973
        proto.external_storage_available = 11338
        proto.internal_storage_total = 854
        proto.internal_storage_available = 11466
        proto.game_disk_storage_total = 49973
        proto.game_disk_storage_available = 11466
        proto.external_sdcard_total_storage = 49973
        proto.external_sdcard_avail_storage = 11466

        proto.library_path = "/data/app/~~lHFxTCCbupG2QVJmsURtZw==/com.dts.freefireth-N3aCHpHNXpdxjD80uIIbww==/lib/arm64"
        proto.library_token = "b8e0cd5e295eee42f5860d3c86e483dd|/data/app/~~lHFxTCCbupG2QVJmsURtZw==/com.dts.freefireth-N3aCHpHNXpdxjD80uIIbww==/base.apk"

        base_payload = proto.SerializeToString()
        extra = b""
        extra += _pb_field(96, '{"cur_rate":[90,60,120],"support_etc2":false}')
        extra += _pb_field(97, 1)
        extra += _pb_field(99, str(platform_type))
        extra += _pb_field(100, str(platform_type))
        extra += _pb_field(102, b"\x17]ENWU\x0eR5")
        extra += _pb_field(104, 52882)
        extra += _pb_field(105, 1)
        extra += _pb_field(106, "https://dl.ak.freefiremobile.com/live/ABHotUpdates/|https://core-ak.freefiremobile.com/live/ABHotUpdates/|6b2078db9d22dd98f8e9386a39af8462")
        extra += _pb_field(107, "c8e41b7a93f02d56e1a94c7b8203f5d1")

        full_payload = base_payload + extra
        return await aes_encrypt(full_payload, AES_KEY, AES_IV)
    except Exception:
        return None


async def send_majorlogin(data, release_version, server_url):
    try:
        url = f"{server_url}MajorLogin" if server_url.endswith('/') else f"{server_url}/MajorLogin"
        req_headers = headers.copy()
        req_headers["ReleaseVersion"] = str(release_version)
        response = await client.post(url, headers=req_headers, data=data)
        if response.status_code != 200:
            return None
        response_content = response.content
        if len(response_content) < 40:
            return None

        res_proto = thunderFF_pb2.MajorLoginRes()
        try:
            res_proto.ParseFromString(response_content)
        except Exception:
            pass

        dict_res = {}
        try:
            parsed = Parser().parse(response_content.hex())
            dict_res = await parse_results(parsed)
        except Exception:
            pass

        key_val = get_proto_field(dict_res, 22)
        iv_val = get_proto_field(dict_res, 23)
        if not key_val:
            key_val = res_proto.aes_ak
        if not iv_val:
            iv_val = res_proto.iv_i

        if isinstance(key_val, str):
            try: key_val = bytes.fromhex(key_val)
            except Exception: pass
        if isinstance(iv_val, str):
            try: iv_val = bytes.fromhex(iv_val)
            except Exception: pass

        if not key_val or not iv_val:
            for offset in range(min(128, len(response_content))):
                try:
                    candidate = thunderFF_pb2.MajorLoginRes()
                    candidate.ParseFromString(response_content[offset:])
                    if candidate.region and candidate.token:
                        res_proto = candidate
                        break
                except Exception:
                    pass
            try:
                parsed = Parser().parse(response_content.hex())
                dict_res = await parse_results(parsed)
                kv = get_proto_field(dict_res, 22)
                ivv = get_proto_field(dict_res, 23)
                if isinstance(kv, str):
                    try: kv = bytes.fromhex(kv)
                    except Exception: pass
                if isinstance(ivv, str):
                    try: ivv = bytes.fromhex(ivv)
                    except Exception: pass
                if kv: key_val = kv
                if ivv: iv_val = ivv
            except Exception:
                pass

        if key_val:
            try: res_proto.aes_ak = key_val
            except Exception: pass
        if iv_val:
            try: res_proto.iv_i = iv_val
            except Exception: pass

        return res_proto
    except Exception as e:
        log(f"[-] send_majorlogin error: {e}")
        return None


async def send_getlogin(data, base_url, token, release_version):
    try:
        url = f"{base_url.rstrip('/')}/GetLoginData"
        req_headers = headers.copy()
        req_headers["ReleaseVersion"] = release_version
        req_headers['Authorization'] = f"Bearer {token}"
        req_headers['Host'] = "clientbp.ppmainecoonghj.com"
        response = await client.post(url, headers=req_headers, data=data)
        if response.status_code != 200:
            code = response.status_code
            if code in (401, 403):
                detail = "game server rejected the JWT/session"
            elif code == 429:
                detail = "game server rate-limited the login request"
            elif code >= 500:
                detail = "game server returned an upstream error"
            else:
                detail = "game server rejected the login request"
            raise GetLoginDataError(f"{detail} (HTTP {code}).")
        response_content = response.content

        res_proto = thunderFF_pb2.GetLoginDataRes()
        try:
            res_proto.ParseFromString(response_content)
        except Exception:
            pass

        def _extract_addr(raw_bytes, field_no):
            try:
                pos = 0
                n = len(raw_bytes)
                def read_varint(buf, p):
                    res = 0
                    sh = 0
                    while p < len(buf):
                        b = buf[p]
                        p += 1
                        res |= (b & 0x7F) << sh
                        if not (b & 0x80):
                            break
                        sh += 7
                    return res, p
                fields = {}
                while pos < n:
                    try:
                        key, pos = read_varint(raw_bytes, pos)
                    except Exception:
                        break
                    fn = key >> 3
                    wt = key & 0x07
                    try:
                        if wt == 0:
                            val, pos = read_varint(raw_bytes, pos)
                        elif wt == 2:
                            ln, pos = read_varint(raw_bytes, pos)
                            val = raw_bytes[pos:pos + ln]
                            pos += ln
                        elif wt == 5:
                            val = raw_bytes[pos:pos + 4]
                            pos += 4
                        elif wt == 1:
                            val = raw_bytes[pos:pos + 8]
                            pos += 8
                        else:
                            break
                    except Exception:
                        break
                    fields.setdefault(fn, []).append(val)
                if field_no in fields:
                    v = fields[field_no][0]
                    if isinstance(v, bytes):
                        try:
                            return v.decode('utf-8', 'ignore')
                        except Exception:
                            return None
                    return str(v)
            except Exception:
                return None

        try:
            for offset in range(0, min(80, len(response_content))):
                fa = _extract_addr(response_content[offset:], 14)
                ia = _extract_addr(response_content[offset:], 32)
                if fa and ":" in fa and ia and ":" in ia:
                    res_proto.functional_addrs = fa
                    res_proto.informational_addrs = ia
                    break
        except Exception:
            pass

        dict_res = {}
        try:
            parsed = Parser().parse(response_content.hex())
            dict_res = await parse_results(parsed)
        except Exception:
            pass

        return res_proto, dict_res
    except GetLoginDataError:
        raise
    except Exception as exc:
        # Exception strings can contain request details; surface only the exception class.
        raise GetLoginDataError(
            f"request failed ({type(exc).__name__})."
        ) from None


async def build_tcp_startup_packet(account_id, token, server_time, key, iv, region=None, typ='OnLine'):
    if region is None:
        region = ACCOUNT_REGION
    uid_hex = f"{int(account_id):016x}"
    timestamp_hex = f"{int(server_time):08x}"
    encode_token = token.encode()
    encrypted_packet = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(encode_token, 16)).hex()
    encrypted_packet_length = f"{len(encrypted_packet) // 2:08x}"
    if typ == 'OnLine':
        return f"9015{uid_hex}{timestamp_hex}00000000{encrypted_packet_length}{encrypted_packet}"
    else:
        return f"9015{uid_hex}{timestamp_hex}{encrypted_packet_length}{encrypted_packet}"


async def send_keep_alive(region=None):
    try:
        if region is None:
            region = ACCOUNT_REGION
        reg = str(region).upper() if region else ACCOUNT_REGION
        # Keepalive must follow the same region family as StartMatch/TCP prefix.
        # BD and IND both use 0314xx; the previous BD fallback sent 0215.
        prefix = REGION_PREFIX.get(reg, REGION_PREFIX["SG"])
        return bytes.fromhex("02" + prefix[2:4])
    except Exception:
        return bytes.fromhex("0219")


async def start_game_battle_royale(region, client_version, writer, key, iv):
    packet = bytes.fromhex("080112800a0a010110013a110a044944433110aa011a064555524f50453a100a044944433210311a064555524f504540014a0801090a0b1219202758016291090a8001303838463832424630324139363736373032303130313030303030303030303030303136303030313030313530303032323246393745454530463030303030303436373632353134303030303030303030303030303030303030303030303030303030303030303030303030303066663030303030303030636163666131366410241afb02735d5e571400024a775d45414d1a041b1c001f11010449715f4243481a001e1d071c1703004b1a4066785c524570735c51486775421b5c5a4c07504042685a63610816054e19025e75196001477c015165406370195f5547404e4550640103020f1304064863754268676c755f65576e40467e5f0a417a4701026d675d6e73670b1108495a4c6a0b78470b740065645e525a057258425f584a447d4e6759440c11044e7c596d7f4b625f7d04055a47505c4e1d6b5b4107447d7201057d7f0f14084e430457674f7e517d72015172415d027473577c4d615f79535256780911030f4d5e027a797f614165067806505d53777750475e75064257076500460817014e741e7e5078487e7a7c465e7669767153497064605a7376677773550d160148037e18675966787f4c42607a645f577e7b441b460776026b18685d0b110205490060020f70676175654674706671797f41067346677c4e06585e780f15074c57047b40517075415f6364027259674b5b0166407f7340600407770a22047a5d5c52300b3a0a167305067162727516134208312e3133302e3232480350015ae90403626253513635686e556f4e36416456324b796f566c636f477776484f624e56526c4d727073504b4f43654177616848494176795556497273743752737149734a7a786b3247525268377a2f637664626d504f6a73552f79626d38547a4c69586d2f474351696d494b53486833447955726f39515152756c34545350626d6d624b7949565937545671577059455372323646572f59624578507338514f706d317372785455736c30796a434144444d4f34616a654b615753366361496c554b4963797a494e396d52516f715277687939797257476d337a644345337a6a61436f492f5a585233656f65365a42647a64677654636b6b665733356e4d4c6a6a565072564b6433523172756174394e50514150724a5546627859696c4c5a3859707336654d5447666b6649793574666a526c314d4648706b51774c6373374439656378566c41636f374e664f6d2b30654756466c4434744478706771385533595973587645384842502f70666c767a737138316a32524f4d7857437556445442492f684735625462773166456e4249725162762b636144775147696f74554e316d4c4b77734379456f4766706746614251457645672b736a764c4c78704743334c304a5344532f74526169504354553344374e6249306547516651622f5a466f4c36455630775a324d6f583932414c572f5049752f56634663584e70596b356f7966326151416a536971486a2f363276354843644f525551303578754e6171795251625653704654303137655237675255636b4966366c6f447476342b514e4a4670766d74757077707774396a5a5974437a4b56743657726d6e36785837706658456251555434684f3758a201050803108703a201050804108103a20105080510c001a20105081d10cc01a2010408161078a20105080e10af01a201020815")
    proto = thunderFF_pb2.StartMatch()
    proto.ParseFromString(packet)
    if hasattr(proto.main, 'region_list') and len(proto.main.region_list) > 0:
        proto.main.region_list[0].region = region
        if len(proto.main.region_list) > 1:
            proto.main.region_list[1].region = region
    if hasattr(proto.main, 'client_version'):
        proto.main.client_version.remote_version = client_version
    packet = proto.SerializeToString()
    encrypted_packet = (await aes_encrypt(packet, key, iv)).hex()
    packet_length = len(encrypted_packet) // 2
    hex_length = hex(packet_length)[2:]
    hex_length = hex_length if len(hex_length) > 1 else "0" + hex_length
    reg = str(region).upper() if region else ACCOUNT_REGION
    reg_prefix = REGION_PREFIX.get(reg, "031500")
    final_packet = reg_prefix + "0" * (6 - len(hex_length)) + hex_length + encrypted_packet
    writer.write(bytes.fromhex(final_packet))
    await writer.drain()
    log(f"[BR] Match search sent ({packet_length} bytes) | Region: {reg}")


def has_ssan_zig(n):
    z = (n << 1) & 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while z >= 0x80:
        out.append((z & 0x7F) | 0x80)
        z >>= 7
    out.append(z)
    return bytes(out)


async def tea_enc(v0, v1, k0, k1, k2, k3):
    s = 0
    for _ in range(_ROUNDS):
        s = (s + _DELTA) & 0xFFFFFFFF
        v0 = (v0 + (((((v1 << 4) & 0xFFFFFFFF) + k0) & 0xFFFFFFFF ^
                      ((v1 + s) & 0xFFFFFFFF) ^
                      (((v1 >> 5) + k1) & 0xFFFFFFFF)))) & 0xFFFFFFFF
        v1 = (v1 + (((((v0 << 4) & 0xFFFFFFFF) + k2) & 0xFFFFFFFF ^
                      ((v0 + s) & 0xFFFFFFFF) ^
                      (((v0 >> 5) + k3) & 0xFFFFFFFF)))) & 0xFFFFFFFF
    return v0, v1


async def tea_dec(v0, v1, k0, k1, k2, k3):
    s = (_DELTA * _ROUNDS) & 0xFFFFFFFF
    for _ in range(_ROUNDS):
        v1 = (v1 - (((((v0 << 4) & 0xFFFFFFFF) + k2) & 0xFFFFFFFF ^
                      ((v0 + s) & 0xFFFFFFFF) ^
                      (((v0 >> 5) + k3) & 0xFFFFFFFF)))) & 0xFFFFFFFF
        v0 = (v0 - (((((v1 << 4) & 0xFFFFFFFF) + k0) & 0xFFFFFFFF ^
                      ((v1 + s) & 0xFFFFFFFF) ^
                      (((v1 >> 5) + k1) & 0xFFFFFFFF)))) & 0xFFFFFFFF
        s = (s - _DELTA) & 0xFFFFFFFF
    return v0, v1


async def tea_cbc_encrypt(padded, key_bytes):
    k0, k1, k2, k3 = (struct.unpack_from("<I", key_bytes, o)[0] for o in (0, 4, 8, 12))
    out = bytearray(len(padded))
    prev_cipher = bytearray(8)
    prev_intermediate = bytearray(8)
    for i in range(0, len(padded), 8):
        xored = bytearray(8)
        for j in range(8):
            xored[j] = padded[i + j] ^ prev_cipher[j]
        e0, e1 = await tea_enc(
            struct.unpack_from("<I", xored, 0)[0],
            struct.unpack_from("<I", xored, 4)[0],
            k0, k1, k2, k3,
        )
        enc = bytearray(8)
        struct.pack_into("<I", enc, 0, e0)
        struct.pack_into("<I", enc, 4, e1)
        for j in range(8):
            out[i + j] = enc[j] ^ prev_intermediate[j]
        prev_cipher[:] = out[i:i + 8]
        prev_intermediate[:] = xored
    return bytes(out)


async def build_padded(content):
    pad_len = (8 - (len(content) + 10) % 8) % 8
    return bytes([pad_len, 0, 0]) + b"\x00" * pad_len + content + b"\x00" * 7


async def encode_header(layout, send_option, cmd, order_id, flags, length, k, v80):
    out = bytearray()
    for code in layout:
        value = {0: send_option, 1: cmd, 2: order_id, 3: flags, 4: length}[code]
        if _FIELD_SIZES[code] == 1:
            out.append((value & 0xFF) ^ k)
        else:
            v = ((value & 0xFFFF) ^ v80) & 0xFFFF
            out.append(v & 0xFF)
            out.append((v >> 8) & 0xFF)
    return bytes(out)


async def crc7_buff(crc, buf):
    c = crc & 0x7F
    for b in buf:
        c = CRC7_TABLE[((2 * (c & 0xFF)) ^ (b & 0xFF)) & 0xFF] & 0x7F
    return c & 0x7F


async def sv_frame(msg_key, layout, send_option, cmd, order_id, flags, content, key, encrypted=True):
    k = key[0]
    v80 = ((k << 8) | k) & 0xFFFF
    body = await tea_cbc_encrypt(await build_padded(content), key) if encrypted else content
    hdr = bytearray([msg_key, 0]) + await encode_header(layout, send_option, cmd, order_id, flags, len(body), k, v80)
    packet = bytearray(hdr + body)
    packet[1] = await crc7_buff(0, bytes(packet[2:])) & 0x7F
    return bytes(packet)


async def build_match_startup_packets(token, udp_key, match_code, account_id, block_val,
                                      server_ip="", region=None, client_version="1.132.8",
                                      client_version_code="2019121229", access_token="",
                                      mode_id=MODE_ID, map_id=MAP_ID):
    if region is None:
        region = ACCOUNT_REGION
    token = token.strip()
    udp_key = bytes.fromhex(udp_key)
    match_code = [int(ch) for ch in str(match_code).strip()]

    thunder_jwt = token[:660] if len(token) > 660 else token
    sharma_jwt = token[660:] if len(token) > 660 else ""
    encoded_thunder_jwt = thunder_jwt.encode() if isinstance(thunder_jwt, str) else thunder_jwt
    encoded_sharma_jwt = sharma_jwt.encode() if isinstance(sharma_jwt, str) else sharma_jwt

    garena420 = has_ssan_zig(len(encoded_thunder_jwt)) + encoded_thunder_jwt
    reg = str(region).upper() if region else ACCOUNT_REGION

    csoversea_block = bytes.fromhex(
        "ca0163736f7665727365612e7374726f6e67686f6c642e66726565666972656d6f62696c652e636f6d"
        "3b302e302e302e303b33342e3132362e37362e34353b33342e38372e3137372e31343b33342e38372e"
        "3137302e3233303b33352e3138352e3138332e35370000000000000100000000000000000000000001"
        "00000800000100000000000100a8a2d7bebd8d8bdf110200"
    )

    mid = bytes.fromhex('0000000001000102030101') + has_ssan_zig(len(reg)) + reg.encode()
    mid += bytes.fromhex('0001030003000004')
    mid += has_ssan_zig(len(client_version)) + client_version.encode()
    mid += has_ssan_zig(len(client_version_code)) + client_version_code.encode()
    mid += csoversea_block

    clean_ip = server_ip.split(':')[0] if server_ip else "0.0.0.0"
    mid += has_ssan_zig(len(clean_ip)) + clean_ip.encode()

    clean_acc_tok = access_token.strip() if access_token else ""
    if clean_acc_tok:
        mid += has_ssan_zig(len(clean_acc_tok)) + clean_acc_tok.encode()

    mid += has_ssan_zig(len(encoded_sharma_jwt)) + encoded_sharma_jwt

    tg_garena420 = (
        uleb_encode(int(account_id)) +
        uleb_encode(int(block_val)) +
        uleb_encode(1) +
        uleb_encode(int(mode_id)) +
        uleb_encode(int(block_val)) +
        uleb_encode(int(map_id)) +
        mid
    )

    process = await sv_frame(0x5E, match_code, 2, 447, 0, 1, garena420, udp_key)
    loading = await sv_frame(0x5A, match_code, 2, 448, 1, 1, tg_garena420, udp_key)
    return process.hex(), loading.hex()


async def produce_xor_key(secret_key):
    k = secret_key[0] if secret_key and len(secret_key) > 0 else 10
    return k, ((k << 8) | k) & 0xFFFF


async def parse_layout(layout):
    if isinstance(layout, str):
        return [int(ch) for ch in layout.strip()]
    return list(layout)


async def tea_cbc_decrypt(body, key_bytes):
    k0, k1, k2, k3 = (struct.unpack_from("<I", key_bytes, o)[0] for o in (0, 4, 8, 12))
    out = bytearray(len(body))
    prev_intermediate = bytearray(8)
    prev_cipher = bytearray(8)
    xored = bytearray(8)
    dec = bytearray(8)
    for i in range(0, len(body), 8):
        for j in range(8):
            xored[j] = body[i + j] ^ prev_intermediate[j]
        d0, d1 = await tea_dec(
            struct.unpack_from("<I", xored, 0)[0],
            struct.unpack_from("<I", xored, 4)[0],
            k0, k1, k2, k3
        )
        struct.pack_into("<I", dec, 0, d0)
        struct.pack_into("<I", dec, 4, d1)
        for j in range(8):
            out[i + j] = dec[j] ^ prev_cipher[j]
        prev_cipher[:] = body[i:i + 8]
        prev_intermediate[:] = dec
    return bytes(out)


async def build_hello_packet(text, key, layout):
    data = text.encode("utf-8")
    if len(data) > 25:
        raise ValueError(f"Text is too long ({len(data)} bytes)")
    content = b"\x10\x00\x00\x00" + data + b"\x00" * (29 - 4 - len(data))
    k, v80 = await produce_xor_key(key)
    layout = await parse_layout(layout)
    padded = await build_padded(content)
    enc_body = await tea_cbc_encrypt(padded, key)
    header_bytes = await encode_header(layout, 1, 1, 0, 1, len(enc_body), k, v80)
    packet = bytearray([0x63, 0x00]) + header_bytes + enc_body
    packet[1] = await crc7_buff(0, packet[2:]) & 0x7F
    return bytes(packet).hex()


async def classify(frame):
    cmd = frame["cmd"]
    msg_name = MESSAGE_ID_TO_NAME.get(cmd, f"UNKNOWN_{cmd}")
    if msg_name == "UDP_HELLO":
        return "HELLO"
    if msg_name == "UDP_ACK":
        return "ACK"
    if msg_name == "UDP_PING":
        return "PING"
    if msg_name == "RUDP_JOIN_MATCH":
        return "JOIN_MATCH"
    if msg_name.startswith("RUDP_"):
        return msg_name
    if msg_name.startswith("UDP_"):
        return msg_name
    return "DATA"


async def build_packet(msg_key, layout, send_option, cmd, order_id, flags, content, key, encrypted=True):
    k = key[0]
    v80 = ((k << 8) | k) & 0xFFFF
    body = await tea_cbc_encrypt(await build_padded(content), key) if encrypted else content
    hdr = bytearray([msg_key, 0])
    for code in layout:
        value = {0: send_option, 1: cmd, 2: order_id, 3: flags, 4: len(body)}[code]
        if _FIELD_SIZES[code] == 1:
            hdr.append((value & 0xFF) ^ k)
        else:
            v = ((value & 0xFFFF) ^ v80) & 0xFFFF
            hdr.append(v & 0xFF)
            hdr.append((v >> 8) & 0xFF)
    packet = bytearray(hdr + body)
    packet[1] = await crc7_buff(0, bytes(packet[2:])) & 0x7F
    return bytes(packet)


async def layouts_from_mask(mask):
    ru = [int(c) for c in str(mask).strip()]
    nr = [c for c in ru if c != 2]
    return ru, nr


async def reply_for(frame, key, mask, ack_key=0x68, ping_key=0x6D, hello_key=0x5B, ack_style="short"):
    ru, nr = await layouts_from_mask(mask)
    typ = await classify(frame)
    if typ == "HELLO":
        if ack_style == "echo":
            content = frame["content"] if frame["content"] else b"\x10\x00\x00\x00"
            return typ, await build_packet(hello_key, nr, 1, 1, None, 1, content, key)
        return typ, await build_packet(ack_key, nr, 0, 2, None, 1, b"\x01\x00", key)
    if typ == "ACK":
        content = frame["content"] if frame["content"] else b"\x01\x00"
        return typ, await build_packet(ack_key, nr, 0, 2, None, 1, content, key)
    if typ == "PING":
        c = frame["content"]
        counter = c[:4] if len(c) >= 4 else c
        return typ, await build_packet(ping_key, nr, 0, 3, None, 0, counter + b"\x00\x00\x00", key, encrypted=False)
    if typ == "JOIN_MATCH":
        return typ, await build_packet(ack_key, nr, 0, 2, None, 1, b"\x02\x00", key)
    return typ, None


async def _udp_sendto(loop, sock, data: bytes, addr):
    try:
        return await loop.sock_sendto(sock, data, addr)
    except NotImplementedError:
        return sock.sendto(data, addr)


async def _udp_recvfrom(loop, sock, bufsize: int):
    try:
        return await loop.sock_recvfrom(sock, bufsize)
    except NotImplementedError:
        try:
            return sock.recvfrom(bufsize)
        except BlockingIOError:
            pass
        fut = loop.create_future()
        fd = sock.fileno()

        def _on_readable():
            if fut.done():
                return
            try:
                res = sock.recvfrom(bufsize)
            except BlockingIOError:
                return
            except Exception as exc:
                try:
                    loop.remove_reader(fd)
                except Exception:
                    pass
                fut.set_exception(exc)
            else:
                try:
                    loop.remove_reader(fd)
                except Exception:
                    pass
                fut.set_result(res)

        loop.add_reader(fd, _on_readable)
        try:
            return await fut
        finally:
            try:
                loop.remove_reader(fd)
            except Exception:
                pass


async def keepalive_ping(sock, ip, port, key_bytes, mask, stop_event):
    nr = (await layouts_from_mask(mask))[1]
    ping_keys = [0x66, 0x6D, 0x69, 0x6C, 0x6B, 0x6E, 0x6F, 0x70]
    loop = asyncio.get_event_loop()
    i = 0
    while not stop_event.is_set():
        pk = ping_keys[i % len(ping_keys)]
        counter = int(time.time() * 1000) & 0xFFFFFFFF
        pkt = await build_packet(pk, nr, 0, 3, None, 0, struct.pack("<I", counter) + b"\x00\x00\x00", key_bytes, encrypted=False)
        try:
            await _udp_sendto(loop, sock, pkt, (ip, port))
        except Exception:
            pass
        i += 1
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            pass


async def try_header(buf, layout, k, v80):
    off = 2
    out = {}
    for code in layout:
        size = _FIELD_SIZES[code]
        if off + size > len(buf):
            return None
        out[_FIELD_NAMES[code]] = (buf[off] ^ k) if size == 1 else ((buf[off] | (buf[off + 1] << 8)) ^ v80) & 0xFFFF
        off += size
    out["headerLen"] = off
    return out


async def oicq_unpad(padded):
    if not padded or len(padded) < 8:
        return None
    if not all(padded[-1 - i] == 0 for i in range(7)):
        return None
    pad_len = padded[0] & 0x07
    s = 3 + pad_len
    e = len(padded) - 7
    return padded[s:e] if s < e else b""


async def decode_packet(packet, key, mask=None):
    data = bytes(packet) if isinstance(packet, bytes) else bytes.fromhex(packet)
    if len(data) < 8:
        return None
    k = key[0]
    v80 = ((k << 8) | k) & 0xFFFF
    crc_ok = (data[1] & 0x7F) == await crc7_buff(0, data[2:])
    candidates = []
    if mask:
        ru, nr = await layouts_from_mask(mask)
        layouts = [("RUDP", ru), ("nonRUDP", nr)]
    else:
        layouts = [("RUDP", list(p)) for p in itertools.permutations([0, 1, 2, 3, 4])]
        layouts += [("nonRUDP", list(p)) for p in itertools.permutations([0, 1, 3, 4])]
    for kind, layout in layouts:
        f = await try_header(data, layout, k, v80)
        if not f:
            continue
        if f["flags"] > 7 or f["sendOption"] > 7:
            continue
        if f["length"] != len(data) - f["headerLen"]:
            continue
        body = data[f["headerLen"]:f["headerLen"] + f["length"]]
        content = None
        padded = None
        if f["flags"] & 1:
            if len(body) < 8 or len(body) % 8 != 0:
                continue
            padded = await tea_cbc_decrypt(body, key)
            content = await oicq_unpad(padded)
            if content is None:
                continue
        else:
            content = body
        score = (1 if crc_ok else 0) + (1 if content is not None else 0)
        candidates.append({
            "kind": kind, "layout": layout, "headerLen": f["headerLen"],
            "msgKey": data[0], "cmd": f["cmd"], "flags": f["flags"],
            "sendOption": f["sendOption"], "orderId": f.get("orderId"),
            "length": f["length"], "content": content, "crcOk": crc_ok,
            "padded": padded, "score": score, "total": len(data),
        })
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c["kind"] == "RUDP" or c["kind"] == "nonRUDP", c["score"]), reverse=True)
    return candidates[0]


def udp_timeout_reason(elapsed: float, startup_age: Optional[float],
                       joined: bool, valid_packet_age: float) -> Optional[str]:
    """Pure timeout decision; startup send is NOT a confirmed server join."""
    if elapsed >= MAX_MATCH_DURATION:
        return "session_limit"
    if startup_age is not None and not joined and startup_age >= UDP_JOIN_TIMEOUT_SECONDS:
        return "join_timeout"
    if joined and valid_packet_age >= UDP_IDLE_RECONNECT_SECONDS:
        return "idle_timeout"
    return None


async def play_game(server_ip_port, thunder, sharma, udp_key, match_code,
                    account_id, player_region, client_version, key, iv,
                    match_index: int, log_uid: Optional[str] = None,
                    account_data: Optional[Dict] = None):
    match_start_time = time.time()
    ping_task = None
    sock = None
    ping_stop = asyncio.Event()
    uid_str = str(log_uid or account_id)
    completed_cleanly = False

    try:
        ip, port = server_ip_port.split(":")
        port = int(port)
        resolved_ip = await resolve_host_cloudflare(ip)

        loop = asyncio.get_event_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(('0.0.0.0', 0))
        except Exception:
            pass
        optimize_udp_socket(sock)
        sock.setblocking(False)

        udp_key_bytes = bytes.fromhex(udp_key)
        hello_id_str = str(account_id) if len(f"{account_id}_2585") <= 25 else str(uid_str)[:20]
        hello_packet = await build_hello_packet(f"{hello_id_str}_2585", udp_key_bytes, match_code)
        try:
            await _udp_sendto(loop, sock, bytes.fromhex(hello_packet), (resolved_ip, port))
            log(f"[MATCH #{match_index}] [SEND-OK] HELLO size={len(bytes.fromhex(hello_packet))}B -> {(resolved_ip, port)} | UID: {uid_str}")
        except Exception as e:
            log(f"[MATCH #{match_index}] [SEND-FAIL] HELLO: {e!r} | UID: {uid_str}")

        ack_state = "waiting_for_hello_reply"
        thunder_sent = False
        sharma_sent = False
        join_match_received = False
        startup_sent_at = None
        observed_udp_phases: set[str] = set()
        local_closed = False
        send_lock = asyncio.Lock()

        ping_task = asyncio.create_task(
            keepalive_ping(sock, resolved_ip, port, udp_key_bytes, match_code, ping_stop)
        )

        last_activity = time.time()
        last_server_packet_at = time.monotonic()
        last_hello_sent = time.time()
        MAX_IDLE_BEFORE_HELLO_RESEND = 7.0

        async def send_thunder_sharma_inline():
            nonlocal ack_state, thunder_sent, sharma_sent, startup_sent_at
            if thunder_sent:
                return
            async with send_lock:
                if thunder_sent:
                    return
                try:
                    thunder_b = bytes.fromhex(thunder)
                    await _udp_sendto(loop, sock, thunder_b, (resolved_ip, port))
                    log(f"[MATCH #{match_index}] [SEND-OK] THUNDER size={len(thunder_b)}B -> {(resolved_ip, port)} | UID: {uid_str}")
                    await asyncio.sleep(0.3)

                    prepare_ack = await build_packet(
                        0x68, (await layouts_from_mask(match_code))[1],
                        0, 2, None, 1, b"\x01\x00", udp_key_bytes
                    )
                    await _udp_sendto(loop, sock, prepare_ack, (resolved_ip, port))
                    log(f"[MATCH #{match_index}] [SEND-OK] PREPARE_ACK size={len(prepare_ack)}B -> {(resolved_ip, port)} | UID: {uid_str}")

                    await asyncio.sleep(0.4)

                    sharma_b = bytes.fromhex(sharma)
                    await _udp_sendto(loop, sock, sharma_b, (resolved_ip, port))
                    log(f"[MATCH #{match_index}] [SEND-OK] SHARMA size={len(sharma_b)}B -> {(resolved_ip, port)} | UID: {uid_str}")

                    thunder_sent = True
                    sharma_sent = True
                    ack_state = "thunder_sharma_sent"
                    startup_sent_at = time.monotonic()
                    log(f"[MATCH #{match_index}] UDP startup packets sent; awaiting JOIN_MATCH (timeout {UDP_JOIN_TIMEOUT_SECONDS}s) | UID: {uid_str}")
                except Exception as e:
                    # Do not mark THUNDER as complete if PREPARE_ACK or SHARMA failed.
                    # The next iteration may retry; a 25s handshake cap prevents loops.
                    thunder_sent = False
                    sharma_sent = False
                    log(f"[MATCH #{match_index}] [SEND-FAIL] Startup: {e!r} | UID: {uid_str}")
                    await asyncio.sleep(0.5)

        while not local_closed:
            now_ts = time.time()
            elapsed = now_ts - match_start_time
            mono_now = time.monotonic()
            reason = udp_timeout_reason(
                elapsed, mono_now - startup_sent_at if startup_sent_at is not None else None,
                join_match_received, mono_now - last_server_packet_at,
            )
            if startup_sent_at is None and not join_match_received and elapsed >= 25.0:
                log(f"[!] Match #{match_index} UDP handshake timeout (25s, no complete startup) | UID: {uid_str}")
                break
            if reason == "session_limit":
                log(f"[!] Match #{match_index} session limit reached without completion packet | UID: {uid_str}")
                break
            if reason == "join_timeout":
                log(f"[!] Match #{match_index} UDP JOIN_MATCH timeout ({UDP_JOIN_TIMEOUT_SECONDS}s after startup; no confirmation) | UID: {uid_str}")
                break
            if reason == "idle_timeout":
                log(f"[!] Match #{match_index} UDP idle timeout ({UDP_IDLE_RECONNECT_SECONDS}s without valid server packet) | UID: {uid_str}")
                break

            try:
                response, server_addr = await asyncio.wait_for(
                    _udp_recvfrom(loop, sock, 65535), timeout=2.0
                )
                if response:
                    last_activity = time.time()
                    frame = await decode_packet(response, udp_key_bytes, match_code)
                    if frame:
                        last_server_packet_at = time.monotonic()
                        ptype = await classify(frame)
                        cmd = frame['cmd']
                        if ptype in ("HELLO", "ACK") and ptype not in observed_udp_phases:
                            observed_udp_phases.add(ptype)
                            log(f"[MATCH #{match_index}] UDP {ptype} received from server | UID: {uid_str}")

                        if cmd in [103, 107]:
                            log(f"[★] Match #{match_index} finished (cmd {cmd}) | UID: {uid_str}")
                            completed_cleanly = True
                            local_closed = True
                            continue

                        if cmd == 101:
                            try:
                                ack_pkt = await build_packet(
                                    0x68, (await layouts_from_mask(match_code))[1],
                                    0, 2, None, 1, b"\x01\x00", udp_key_bytes
                                )
                                await _udp_sendto(loop, sock, ack_pkt, server_addr)
                                log(f"[MATCH #{match_index}] [SEND-OK] ACK-101 size={len(ack_pkt)}B -> {server_addr} | UID: {uid_str}")
                            except Exception as e:
                                log(f"[MATCH #{match_index}] [SEND-FAIL] ACK-101: {e!r} | UID: {uid_str}")

                        if ptype in ["ACK", "PING", "HELLO", "JOIN_MATCH"]:
                            if ptype == "HELLO" and ack_state == "waiting_for_hello_reply":
                                typ, reply = await reply_for(
                                    frame, udp_key_bytes, match_code, ack_style="short"
                                )
                                if reply:
                                    try:
                                        await _udp_sendto(loop, sock, reply, server_addr)
                                        log(f"[MATCH #{match_index}] [SEND-OK] HELLO-REPLY size={len(reply)}B -> {server_addr} | UID: {uid_str}")
                                    except Exception as e:
                                        log(f"[MATCH #{match_index}] [SEND-FAIL] HELLO-REPLY: {e!r} | UID: {uid_str}")
                                ack_state = "ack_sent_waiting"
                            elif ptype == "ACK":
                                if ack_state == "waiting_for_hello_reply":
                                    typ, reply = await reply_for(frame, udp_key_bytes, match_code)
                                    if reply:
                                        try:
                                            await _udp_sendto(loop, sock, reply, server_addr)
                                            log(f"[MATCH #{match_index}] [SEND-OK] ACK-REPLY size={len(reply)}B -> {server_addr} | UID: {uid_str}")
                                        except Exception as e:
                                            log(f"[MATCH #{match_index}] [SEND-FAIL] ACK-REPLY: {e!r} | UID: {uid_str}")
                                    ack_state = "ready_to_send_thunder"
                                elif ack_state == "ack_sent_waiting":
                                    ack_state = "ready_to_send_thunder"
                                else:
                                    typ, reply = await reply_for(frame, udp_key_bytes, match_code)
                                    if reply:
                                        try:
                                            await _udp_sendto(loop, sock, reply, server_addr)
                                            log(f"[MATCH #{match_index}] [SEND-OK] ACK-REPLY size={len(reply)}B -> {server_addr} | UID: {uid_str}")
                                        except Exception as e:
                                            log(f"[MATCH #{match_index}] [SEND-FAIL] ACK-REPLY: {e!r} | UID: {uid_str}")
                            elif ptype == "PING":
                                typ, reply = await reply_for(frame, udp_key_bytes, match_code)
                                if reply:
                                    try:
                                        await _udp_sendto(loop, sock, reply, server_addr)
                                        log(f"[MATCH #{match_index}] [SEND-OK] PING-REPLY size={len(reply)}B -> {server_addr} | UID: {uid_str}")
                                    except Exception as e:
                                        log(f"[MATCH #{match_index}] [SEND-FAIL] PING-REPLY: {e!r} | UID: {uid_str}")
                            elif ptype == "JOIN_MATCH" and not join_match_received:
                                log(f"[MATCH #{match_index}] UDP JOIN_MATCH received from server | UID: {uid_str}")
                                signal_profile_refresh(account_data)
                                join_match_received = True
                                typ, reply = await reply_for(frame, udp_key_bytes, match_code)
                                if reply:
                                    try:
                                        await _udp_sendto(loop, sock, reply, server_addr)
                                        log(f"[MATCH #{match_index}] [SEND-OK] JOIN-MATCH-REPLY size={len(reply)}B -> {server_addr} | UID: {uid_str}")
                                    except Exception as e:
                                        log(f"[MATCH #{match_index}] [SEND-FAIL] JOIN-MATCH-REPLY: {e!r} | UID: {uid_str}")

            except asyncio.TimeoutError:
                if ack_state == "ready_to_send_thunder" and not thunder_sent:
                    await send_thunder_sharma_inline()
                elif ack_state == "waiting_for_hello_reply":
                    if (time.time() - last_activity) > MAX_IDLE_BEFORE_HELLO_RESEND:
                        try:
                            pkt = await build_hello_packet(
                                f"{hello_id_str}_2585", udp_key_bytes, match_code
                            )
                            await _udp_sendto(loop, sock, bytes.fromhex(pkt), (resolved_ip, port))
                            log(f"[MATCH #{match_index}] [SEND-OK] HELLO-RESEND size={len(bytes.fromhex(pkt))}B -> {(resolved_ip, port)} | UID: {uid_str}")
                        except Exception as e:
                            log(f"[MATCH #{match_index}] [SEND-FAIL] HELLO-RESEND: {e!r} | UID: {uid_str}")
                        last_activity = time.time()
            except BlockingIOError:
                await asyncio.sleep(0.05)
            except OSError:
                await asyncio.sleep(0.5)
                continue
            except Exception:
                await asyncio.sleep(0.5)
                continue

            if ack_state == "ready_to_send_thunder" and not thunder_sent:
                await send_thunder_sharma_inline()

        return f"match #{match_index} finished"

    except Exception as e:
        log(f"[MATCH #{match_index}] Session error: {e} | UID: {uid_str}")
        return f"match #{match_index} error"

    finally:
        signal_profile_refresh(account_data)
        ping_stop.set()
        if ping_task:
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass
        if sock:
            try:
                sock.close()
            except Exception:
                pass


async def functional_br(addrs, starter_packet, account_region, client_version,
                        key, iv, account_id="", account_data=None,
                        max_reconnects=10):
    reconnects = 0
    ip, port = addrs.split(":")
    play_matches: List[asyncio.Task] = []
    no_response_count = 0
    search_attempts = 0
    match_index_counter = 0
    last_start_time = 0.0
    next_search_delay = START_MATCH_INTERVAL
    uid_str = str(account_id)

    current_token = starter_packet
    current_key = key
    current_iv = iv
    current_account_data = account_data

    try:
        while True:
            writer = None
            gateway_ping_task = None
            try:
                resolved_ip = await resolve_host_cloudflare(ip)
                reader, writer = await asyncio.open_connection(resolved_ip, int(port))

                raw_sock = writer.get_extra_info('socket')
                if raw_sock:
                    optimize_tcp_socket(raw_sock)

                writer.write(bytes.fromhex(current_token))
                await writer.drain()

                try:
                    init_ka = await send_keep_alive(account_region)
                    if init_ka and writer and not writer.is_closing():
                        writer.write(init_ka)
                        await asyncio.wait_for(writer.drain(), timeout=3)
                except Exception:
                    pass

                async def func_gateway_keepalive():
                    ka_bytes = await send_keep_alive(account_region)
                    while True:
                        await asyncio.sleep(5)
                        try:
                            if writer and not writer.is_closing():
                                writer.write(ka_bytes)
                                await writer.drain()
                        except Exception:
                            break

                gateway_ping_task = asyncio.create_task(func_gateway_keepalive())

                log(f"[+] TCP gateway connected | UID: {uid_str}")
                reconnects = 0
                no_response_count = 0
                last_start_time = 0.0

                async def send_start_match():
                    nonlocal search_attempts, last_start_time, next_search_delay
                    search_attempts += 1
                    current_region = account_region or ACCOUNT_REGION
                    try:
                        await asyncio.sleep(random.uniform(0.2, 0.4))
                        await matchmaking_slot()
                        await start_game_battle_royale(
                            current_region, client_version, writer,
                            current_key, current_iv
                        )
                        # Count only a successfully drained TCP StartMatch send,
                        # not a failed local attempt. This is NOT a server match.
                        log(f"[BR] UID {uid_str} -> BR match #{search_attempts} (request sent)")
                    except Exception as e:
                        log(f"[!] StartMatch notice: {e} | UID: {uid_str}")
                    last_start_time = asyncio.get_running_loop().time()
                    next_search_delay = random.uniform(0.85, 1.15) * START_MATCH_INTERVAL

                if not (current_account_data and current_account_data.get('_queue_cooldown_pending')):
                    await send_start_match()

                while True:
                    play_matches[:] = [m for m in play_matches if not m.done()]

                    now = asyncio.get_running_loop().time()
                    if (len(play_matches) == 0 and (now - last_start_time >= next_search_delay)
                            and not (current_account_data and current_account_data.get('_queue_cooldown_pending'))):
                        await send_start_match()

                    try:
                        data = await asyncio.wait_for(reader.read(65535), timeout=0.5)
                    except asyncio.TimeoutError:
                        no_response_count += 1
                        if no_response_count > 60:
                            no_response_count = 0
                        continue

                    if not data:
                        raise ConnectionError("Connection closed by server")

                    packet_length = len(data)
                    no_response_count = 0

                    if packet_length < 10 or packet_length > 2500:
                        continue

                    hex_data = data.hex()

                    is_match_packet = False
                    payload_hex = None

                    if hex_data.startswith("0300"):
                        if 10 < packet_length < 30:
                            log(f"[*] Queue confirmed (BR) | UID: {uid_str}")
                            continue
                        elif 300 <= packet_length <= 2500:
                            is_match_packet = True
                            payload_hex = hex_data[10:]
                        else:
                            continue
                    elif 200 <= packet_length <= 1400:
                        is_match_packet = True
                        payload_hex = hex_data

                    if not is_match_packet:
                        continue

                    try:
                        res = json.loads(await decode_protobuf(payload_hex))
                        token = None
                        udp_key = None
                        match_code = None
                        server_ip_port = None
                        match_account_id = None
                        block_val = None

                        if '42' in res and 'data' in res['42']:
                            match_code = res['42']['data']
                        if '5' in res and 'data' in res['5']:
                            res_field5 = res['5']['data']
                            server_ip_port = res_field5.get('2', {}).get('data')
                            udp_key = res_field5.get('3', {}).get('data')
                            token = res_field5.get('4', {}).get('data')
                            if '42' in res_field5:
                                match_code = res_field5['42']['data']
                        if '1' in res and 'data' in res['1']:
                            match_account_id = res['1']['data']
                        if '5' in res and 'data' in res['5']:
                            block_val = res['5']['data'].get('1', {}).get('data')

                        if match_account_id is not None and str(match_account_id) != str(account_id):
                            # Field 1's precise semantics can change across protocol versions;
                            # surface the difference but do not discard a possibly valid match.
                            log(f"[!] Match packet account field differs from login game ID | UID: {uid_str}")
                        effective_acc_id = match_account_id or account_id or "ME_BOT"

                        log(f"[DEBUG] UID {uid_str} parsed: token={'YES' if token else 'NO'} udp_key={'YES' if udp_key else 'NO'} match_code={'YES' if match_code else 'NO'} server={server_ip_port}")

                        if token and udp_key and match_code and server_ip_port:
                            log(f"[+] Match found [BR] | UID: {uid_str} | size={packet_length}")
                            acc_tok = current_account_data.get('access_token', '') if current_account_data else ""

                            thunder, sharma = await build_match_startup_packets(
                                token, udp_key, match_code, effective_acc_id, block_val or 0,
                                server_ip=server_ip_port,
                                region=account_region,
                                client_version=client_version,
                                access_token=acc_tok,
                                mode_id=int(current_account_data.get('mode_id', MODE_ID)) if current_account_data else MODE_ID,
                                map_id=int(current_account_data.get('map_id', MAP_ID)) if current_account_data else MAP_ID
                            )

                            match_index_counter += 1
                            match_index = match_index_counter
                            log(f"[+] Match #{match_index} [BR] injected -> {server_ip_port} | UID: {uid_str}")

                            new_match = asyncio.create_task(
                                play_game(
                                    server_ip_port,
                                    thunder,
                                    sharma,
                                    udp_key,
                                    match_code,
                                    effective_acc_id,
                                    account_region,
                                    client_version,
                                    current_key,
                                    current_iv,
                                    match_index=match_index,
                                    log_uid=uid_str,
                                    account_data=current_account_data,
                                )
                            )
                            play_matches.append(new_match)

                            async def drain_gateway_reader():
                                while not new_match.done():
                                    try:
                                        data_gw = await asyncio.wait_for(reader.read(4096), timeout=1.0)
                                        if not data_gw:
                                            break
                                    except asyncio.TimeoutError:
                                        continue
                                    except Exception:
                                        break

                            drain_task = asyncio.create_task(drain_gateway_reader())

                            try:
                                await new_match
                            except Exception as e:
                                log(f"[MATCH #{match_index}] error: {e} | UID: {uid_str}")
                            finally:
                                drain_task.cancel()
                                try:
                                    await drain_task
                                except asyncio.CancelledError:
                                    pass

                            play_matches[:] = [m for m in play_matches if not m.done()]

                            try:
                                lock = current_account_data.get('_profile_refresh_lock') if current_account_data else None
                                if lock:
                                    async with lock:
                                        result = await refresh_account_profile(current_account_data)
                                else:
                                    result = await refresh_account_profile(current_account_data)
                                report_profile_result(current_account_data, result)
                            except Exception:
                                pass

                            new_level = 1
                            new_exp = 0
                            if current_account_data:
                                new_level = max(1, int(current_account_data.get("level", 1) or 1))
                                new_exp = max(0, int(current_account_data.get("exp", 0) or 0))
                            log(f"[*] UID {uid_str} | Lvl {new_level} | EXP {new_exp} | Next: BR")

                            if gateway_ping_task:
                                gateway_ping_task.cancel()
                            await safe_close_writer(writer)
                            writer = None
                            break

                        else:
                            log(f"[DEBUG] UID {uid_str} not a valid match packet — ignoring")
                            continue

                    except Exception as e:
                        log(f"[!] Match packet notice: {e} | UID: {uid_str}")
                        continue

            except asyncio.CancelledError:
                if gateway_ping_task:
                    gateway_ping_task.cancel()
                raise
            except Exception as e:
                if gateway_ping_task:
                    gateway_ping_task.cancel()
                play_matches[:] = [m for m in play_matches if not m.done()]
                await safe_close_writer(writer)

                if "Cache expired" in str(e):
                    break

                reconnects += 1
                if reconnects > max_reconnects:
                    reconnects = 0
                    break

                await asyncio.sleep(min(reconnects * 0.5, 2.0))
            finally:
                if gateway_ping_task:
                    gateway_ping_task.cancel()
                if writer:
                    await safe_close_writer(writer)

    except asyncio.CancelledError:
        raise
    finally:
        for m in play_matches:
            if not m.done():
                m.cancel()
        if play_matches:
            await asyncio.gather(*play_matches, return_exceptions=True)


async def informational(addrs, starter_packet, key, iv, region=None, account_id="", max_reconnects=3):
    if region is None:
        region = ACCOUNT_REGION
    uid_str = str(account_id)
    reconnects = 0
    ip, port = addrs.split(":")
    while True:
        writer = None
        ping_task = None
        try:
            resolved_ip = await resolve_host_cloudflare(ip)
            reader, writer = await asyncio.open_connection(resolved_ip, int(port))

            raw_sock = writer.get_extra_info('socket')
            if raw_sock:
                optimize_tcp_socket(raw_sock)

            writer.write(bytes.fromhex(starter_packet))
            await writer.drain()
            reconnects = 0

            try:
                init_ka = await send_keep_alive(region)
                if init_ka and writer and not writer.is_closing():
                    writer.write(init_ka)
                    await asyncio.wait_for(writer.drain(), timeout=3)
            except Exception:
                pass

            async def info_keepalive():
                ka_bytes = await send_keep_alive(region)
                while True:
                    await asyncio.sleep(5)
                    try:
                        if writer and not writer.is_closing():
                            writer.write(ka_bytes)
                            await writer.drain()
                    except Exception:
                        break

            ping_task = asyncio.create_task(info_keepalive())

            while True:
                try:
                    data = await asyncio.wait_for(reader.read(8192), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                if not data:
                    raise ConnectionError("Connection closed")
        except asyncio.CancelledError:
            if ping_task:
                ping_task.cancel()
            await safe_close_writer(writer)
            raise
        except Exception:
            if ping_task:
                ping_task.cancel()
            await safe_close_writer(writer)
            reconnects += 1
            if reconnects > max_reconnects:
                await asyncio.sleep(3)
                reconnects = 0
            else:
                await asyncio.sleep(1)


async def refresh_account_profile(account_data: Dict) -> Dict[str, Any]:
    """Check *actual* profile data; never treat parse defaults as a real EXP value."""
    if not account_data:
        return {"ok": False, "reason": "No active account profile", "fail_streak": 0}
    url = account_data.get('server_url')
    token = account_data.get('token')
    release_version = account_data.get('release_version')
    payload = account_data.get('login_payload_data')
    if not all((url, token, release_version, payload)):
        return {"ok": False, "reason": "Profile refresh credentials are unavailable", "fail_streak": 0}
    account_data['_last_profile_check_at'] = time.monotonic()
    try:
        async with _PROFILE_SEMAPHORE:
            res = await send_getlogin(payload, url, token, release_version)
        if not res:
            return {"ok": False, "reason": "GetLoginData returned no valid response (network, token or server error)",
                    "fail_streak": int(account_data.get('_profile_fail_streak', 0) or 0) + 1}
        res_proto, dict_res = res
        # Both fields MUST be present. Old default=1/0 could silently overwrite
        # an authenticated Level 2 / 48 EXP with fabricated Level 1 / 0 EXP.
        raw_level = get_proto_field(dict_res, 6, None)
        raw_exp = get_proto_field(dict_res, 7, None)
        if raw_level is None or raw_exp is None:
            return {"ok": False, "reason": "GetLoginData did not include level and EXP fields", "fail_streak": int(account_data.get('_profile_fail_streak', 0) or 0) + 1}
        level = int(raw_level)
        exp = int(raw_exp)
        if level <= 0 or exp < 0:
            return {"ok": False, "reason": "GetLoginData returned invalid level or EXP", "fail_streak": int(account_data.get('_profile_fail_streak', 0) or 0) + 1}
        reported_id = get_proto_field(dict_res, 1, None)
        if reported_id is not None and str(reported_id) != str(account_data.get('account_id')):
            return {"ok": False, "reason": "GetLoginData returned a different account ID", "fail_streak": int(account_data.get('_profile_fail_streak', 0) or 0) + 1}
        old_exp = int(account_data.get('exp', 0) or 0)
        old_lvl = int(account_data.get('level', 1) or 1)
        nickname = res_proto.nickname or get_proto_field(dict_res, 4, "")
        changed = exp != old_exp or level != old_lvl
        account_data['level'] = level
        account_data['exp'] = exp
        if nickname:
            account_data['nickname'] = nickname
        if changed:
            log(f"[+] Profile sync | UID {account_data['account_id']} | Lvl {level} | EXP {exp}")
        account_data['_profile_fail_streak'] = 0
        return {"ok": True, "changed": changed, "level": level, "exp": exp,
                "exp_delta": exp - old_exp, "fail_streak": 0,
                "reason": "Profile changed" if changed else "Server profile unchanged"}
    except Exception as exc:
        return {"ok": False, "reason": f"Profile refresh failed ({type(exc).__name__})",
                "fail_streak": int(account_data.get('_profile_fail_streak', 0) or 0) + 1}


def signal_profile_refresh(account_data: Optional[Dict]) -> None:
    """Ask the worker's profile refresher to re-read the server profile right now.

    Used after JOIN_MATCH and after a match session ends: those are the two moments
    where the game server may have just credited (or refused to credit) EXP.
    """
    if not account_data:
        return
    event = account_data.get('_profile_refresh_event')
    if event is not None:
        try:
            event.set()
        except Exception:
            pass


def report_profile_result(account_data: Dict, result: Dict[str, Any]) -> None:
    """Notify dashboard manager; CLI mode has no callback."""
    cb = account_data.get('_profile_result_callback') if account_data else None
    if cb:
        try:
            cb(result)
        except Exception:
            pass


async def process_account_uid_pass(uid: str, password: str, region_override: Optional[str] = None) -> Optional[Dict]:
    log(f"[*] Logging in UID: {uid}")
    target_region = (region_override or ACCOUNT_REGION or "ME").upper()
    try:
        async with _LOGIN_SEMAPHORE:
            verconfig_res = await version_config(region=target_region)
            if verconfig_res is None:
                return None
            release_version, client_version, server_url = verconfig_res

            tokengrant_response = await get_access_token(uid, password)
            if tokengrant_response is None:
                log(f"[-] OAuth failed for UID {uid}")
                return None
            open_id, access_token, platform = tokengrant_response

            device_info = get_device_for_account(uid)
            login_payload_data = await build_majorlogin_payload(open_id, access_token, platform, client_version, device_info)
            if login_payload_data is None:
                return None

            majorlogin_response = await send_majorlogin(login_payload_data, release_version, server_url)
            if majorlogin_response is None:
                log(f"[-] MajorLogin failed for {uid}")
                return None

            getlogin_result = await send_getlogin(
                login_payload_data,
                majorlogin_response.url,
                majorlogin_response.token,
                release_version
            )
            if getlogin_result is None:
                log(f"[-] GetLoginData failed for {uid}")
                return None

            res_proto, dict_res = getlogin_result

        acc_id = str(majorlogin_response.account_id or getattr(res_proto, "account_id", 0) or get_proto_field(dict_res, 1, 0) or "0")
        if acc_id == "0" or not acc_id or acc_id == "None":
            log(f"[-] Invalid account_id (0) for UID {uid} — skipping this account")
            return None
        majorlogin_response.account_id = int(acc_id)

        level = int(get_proto_field(dict_res, 6, 1))
        exp = int(get_proto_field(dict_res, 7, 0))
        nickname = res_proto.nickname or get_proto_field(dict_res, 4, f"Player_{acc_id}")
        region = majorlogin_response.region or get_proto_field(dict_res, 3, target_region)

        if level <= 0:
            level = 1
        if exp < 0:
            exp = 0

        func_addr = res_proto.functional_addrs
        info_addr = res_proto.informational_addrs
        if not isinstance(func_addr, str) or not func_addr or ":" not in func_addr:
            func_addr = None
        if not isinstance(info_addr, str) or not info_addr or ":" not in info_addr:
            info_addr = None

        key_val = majorlogin_response.aes_ak
        iv_val = majorlogin_response.iv_i
        if not isinstance(key_val, (bytes, bytearray)) or len(key_val) < 16:
            key_val = AES_KEY
        if not isinstance(iv_val, (bytes, bytearray)) or len(iv_val) < 16:
            iv_val = AES_IV

        log(f"[+] Login OK | UID {acc_id} | {nickname} | Lvl {level} | Mode: BR")

        account_data = {
            'account_id': majorlogin_response.account_id,
            'nickname': nickname,
            'region': region,
            'level': level,
            'exp': exp,
            'open_id': open_id,
            'access_token': access_token,
            'platform': str(platform),
            'token': majorlogin_response.token,
            'server_time': majorlogin_response.server_time,
            'aes_ak': key_val,
            'iv_i': iv_val,
            'functional_addrs': func_addr,
            'informational_addrs': info_addr,
            'release_version': release_version,
            'client_version': client_version,
            'server_url': majorlogin_response.url,
            'login_payload_data': login_payload_data,
            'auth_type': 'guest',
            'auth_uid': uid,
            'auth_password': password
        }
        return account_data
    except Exception as e:
        log(f"[-] process_account_uid_pass error: {e}")
        return None


async def run_account_worker(account_data: Dict, label: str):
    acc_id = str(account_data['account_id'])
    informational_task = None
    exp_task = None
    functional_task = None
    try:
        reg = account_data.get('region', ACCOUNT_REGION)
        func_addr = account_data.get('functional_addrs')
        info_addr = account_data.get('informational_addrs')

        if not func_addr or not isinstance(func_addr, str) or ":" not in func_addr:
            print_warning(f"[!] No functional_addrs for {acc_id} — worker idle")
            while True:
                await asyncio.sleep(60)
            return

        tcp_packet_online = await build_tcp_startup_packet(
            account_data['account_id'],
            account_data['token'],
            account_data['server_time'],
            account_data['aes_ak'],
            account_data['iv_i'],
            region=reg,
            typ='OnLine'
        )

        tcp_packet_chat = await build_tcp_startup_packet(
            account_data['account_id'],
            account_data['token'],
            account_data['server_time'],
            account_data['aes_ak'],
            account_data['iv_i'],
            region=reg,
            typ='ChaT'
        )

        if info_addr and isinstance(info_addr, str) and ":" in info_addr:
            informational_task = asyncio.create_task(
                informational(
                    info_addr,
                    tcp_packet_chat,
                    account_data['aes_ak'],
                    account_data['iv_i'],
                    region=reg,
                    account_id=acc_id
                )
            )

        account_data['_profile_refresh_event'] = asyncio.Event()

        async def exp_refresher():
            while True:
                streak = int(account_data.get('_profile_fail_streak', 0) or 0)
                delay = profile_poll_delay(streak)
                delay = max(15.0, delay + random.uniform(-5.0, 5.0))
                event = account_data.get('_profile_refresh_event')
                woken = False
                if event is not None:
                    try:
                        await asyncio.wait_for(event.wait(), timeout=delay)
                        event.clear()
                        woken = True
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(delay)
                if woken:
                    # Match events ask for an early check; the debounce keeps a burst of
                    # them from becoming a burst of game API calls.
                    wait = forced_refresh_wait(account_data.get('_last_profile_check_at'),
                                               time.monotonic())
                    if wait > 0:
                        await asyncio.sleep(wait)
                    # Give the game server a moment to settle the finished match.
                    await asyncio.sleep(2.0)
                try:
                    lock = account_data.get('_profile_refresh_lock')
                    if lock:
                        async with lock:
                            result = await refresh_account_profile(account_data)
                    else:
                        result = await refresh_account_profile(account_data)
                    try:
                        account_data['_profile_fail_streak'] = int(result.get('fail_streak') or 0)
                    except Exception:
                        pass
                    result['forced'] = bool(woken)
                    report_profile_result(account_data, result)
                except Exception:
                    pass

        exp_task = asyncio.create_task(exp_refresher())

        functional_task = asyncio.create_task(
            functional_br(
                func_addr,
                tcp_packet_online,
                account_data['region'],
                account_data['client_version'],
                account_data['aes_ak'],
                account_data['iv_i'],
                account_id=acc_id,
                account_data=account_data
            )
        )

        await functional_task

    except asyncio.CancelledError:
        raise
    except Exception as e:
        print_error(f"run_account_worker error for {label}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        for t in (informational_task, exp_task, functional_task):
            if t and not t.done():
                t.cancel()
        for t in (informational_task, exp_task, functional_task):
            if t:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass


async def account_loop_guest(uid: str, password: str):
    uid_str = str(uid)
    while True:
        try:
            log(f"[*] Starting login for UID: {uid_str}")
            account_data = await process_account_uid_pass(uid_str, password)
            if not account_data:
                log(f"[-] Login failed for {uid_str}. Retry in 15s...")
                await asyncio.sleep(15)
                continue
            await run_account_worker(account_data, uid_str)
            log(f"[!] Session ended for {uid_str}. Reconnecting in 3s...")
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            log(f"[!] Worker stopped for {uid_str}")
            break
        except Exception as e:
            log(f"[-] Error for UID {uid_str}: {e}. Retry in 10s...")
            await asyncio.sleep(10)


def _prompt_credentials():
    print("[i] Enter account UID and Password")
    uid = input("UID: ").strip()
    password = input("Password: ").strip()
    accounts = [{"uid": uid, "password": password}]
    return accounts


def _prompt_region():
    print("[i] Available Regions: ME, IND, SG, TH, PH, VN, MY, ID, HK, TW, BD, PK, CIS, BR, NA, SAC, EUROPE")
    region = input("Region (default ME): ").strip().upper()
    if not region:
        region = "ME"
    if region not in REGION_PREFIX:
        print(f"[!] Unknown region '{region}', falling back to ME")
        region = "ME"
    return region


async def main():
    global ACCOUNT_REGION

    print("=" * 60)
    print("    ＧＡＭＥＲ-ＣＯＲＰ - FreeFire Level Up Bot (Web Dashboard Mode)")
    print("   Persistent Device ID + TRUE Parallel + Smart DNS")
    print("=" * 60)
    print(f"[i] Start Match Interval: {START_MATCH_INTERVAL}s")
    print(f"[i] Offline Wait: 3.0s (after match found)")
    print(f"[i] Non-match Reconnect: {NON_MATCH_RECONNECT_DELAY}s")
    print(f"[i] Cache Invalidation Threshold: 5.0x")
    print(f"[i] Parallel Matches: UNLIMITED (background)")
    print(f"[i] Cache TTL: 1200s (20 min)")
    print(f"[i] Priority Regions: ['BD', 'IND', 'SG', 'TH', 'PH', 'VN', 'MY', 'ID', 'HK', 'TW']")
    print("[i] Device System: 1 ID = 1 Persistent Device ID (devices.json)")
    print("=" * 60)

    accounts = _prompt_credentials()
    region = _prompt_region()

    ACCOUNT_REGION = region
    print(f"[i] Using Region: {ACCOUNT_REGION}")

    log(f"[*] Starting BR bot for {len(accounts)} accounts...")

    tasks = []
    for i, acc in enumerate(accounts, 1):
        uid = acc["uid"]
        password = acc["password"]
        log(f"[*] Launching worker {i} | UID: {uid}")
        tasks.append(asyncio.create_task(account_loop_guest(uid, password)))

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log("[!] Shutting down all workers...")
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        log("[+] All sessions cleanly closed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")