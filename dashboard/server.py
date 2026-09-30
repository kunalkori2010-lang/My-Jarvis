"""
dashboard/server.py — JARVIS Local HTTP Dashboard

Plain HTTP on port 8000 (no SSL warnings, no firewall issues).
Security at the application layer: AES-256-CBC with session-key-derived key.
CryptoJS is auto-downloaded once and served locally — no CDN needed after that.

Install deps:  pip install fastapi "uvicorn[standard]" cryptography
"""

import asyncio
import base64
import hashlib
import re
import secrets
import socket
import string
import time
from pathlib import Path

_DEPS_OK = False
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
    import uvicorn
    _DEPS_OK = True
except ImportError:
    pass

# python-multipart is required for file uploads — optional dependency
_UPLOAD_OK = False
try:
    from fastapi import UploadFile, File as FastAPIFile
    _UPLOAD_OK = True
except Exception:
    pass

BASE_DIR    = Path(__file__).resolve().parent.parent
STATIC_DIR  = Path(__file__).parent / "static"
PORT        = 8000
MAX_UPLOAD_MB = 500


def _make_uploads_dir() -> Path:
    """Return (and create) the cross-platform uploads folder."""
    for candidate in [
        Path.home() / "Downloads" / "JARVIS Uploads",
        Path.home() / "Documents" / "JARVIS Uploads",
        BASE_DIR / "uploads",
    ]:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            pass
    return BASE_DIR / "uploads"


UPLOADS_DIR = _make_uploads_dir()

def _get_gemini_key() -> str | None:
    try:
        import json as _json
        with open(BASE_DIR / "config" / "api_keys.json", "r", encoding="utf-8") as f:
            return _json.load(f).get("gemini_api_key")
    except Exception:
        return None

_KEY_CHARS = [c for c in (string.ascii_uppercase + string.digits)
              if c not in ('O', 'I', 'L', '0', '1')]

# ── AES-256-CBC ───────────────────────────────────────────────────────────────
_AES_SALT = b'JARVIS-DASHBOARD-v1'


def _derive_key(session_key: str) -> bytes:
    """SHA-256(sessionKey‖salt) → 32-byte AES-256 key (microseconds, no PBKDF2 needed)."""
    return hashlib.sha256(session_key.encode('utf-8') + _AES_SALT).digest()


def _decrypt_cbc(aes_key: bytes, enc_b64: str) -> str:
    """Decrypt base64(IV[16] ‖ ciphertext) with AES-256-CBC + PKCS7."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_pad
    raw      = base64.b64decode(enc_b64)
    iv, ct   = raw[:16], raw[16:]
    dec      = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    padded   = dec.update(ct) + dec.finalize()
    unpadder = sym_pad.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode('utf-8')


# ── CryptoJS (auto-download once, served locally) ─────────────────────────────
_CRYPTOJS_CDN  = ("https://cdnjs.cloudflare.com/ajax/libs/"
                  "crypto-js/4.2.0/crypto-js.min.js")
_CRYPTOJS_FILE = STATIC_DIR / "crypto-js.min.js"


def _ensure_network_access(port: int) -> None:
    """Cross-platform, best-effort: open port in the OS firewall for LAN access.

    Runs in a background thread — never blocks uvicorn startup.

    Windows : writes a .bat file, runs it elevated via Windows ShellExecuteW
              (native UAC dialog, guaranteed to appear). One-time setup.
    macOS   : osascript admin dialog if the Application Firewall is on.
    Linux   : pkexec GUI → sudo -n → prints manual command as fallback.
    """
    import sys, subprocess, os, tempfile, threading

    # ── Windows ──────────────────────────────────────────────────────────────
    if sys.platform == "win32":
        import ctypes, time

        port_rule = f"JARVIS Dashboard Port {port}"
        prog_rule  = "JARVIS Dashboard Python"
        py_exe     = sys.executable

        def _netsh_rule_exists(name: str) -> bool:
            try:
                r = subprocess.run(
                    ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                    capture_output=True, text=True, timeout=5,
                )
                return r.returncode == 0 and "No rules match" not in r.stdout
            except Exception:
                return False

        def _network_is_public() -> bool:
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "(Get-NetConnectionProfile | "
                     "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                     "Measure-Object).Count"],
                    capture_output=True, text=True, timeout=6,
                )
                return r.stdout.strip() not in ("", "0")
            except Exception:
                return False

        need_port    = not _netsh_rule_exists(port_rule)
        need_prog    = not _netsh_rule_exists(prog_rule)
        need_private = _network_is_public()

        if not need_port and not need_prog and not need_private:
            return  # already fully configured

        # Build a .bat file — netsh + powershell, runs fast when elevated
        bat_lines = ["@echo off"]
        if need_private:
            bat_lines.append(
                'powershell -NoProfile -NonInteractive -Command "'
                'Get-NetConnectionProfile | '
                "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                'Set-NetConnectionProfile -NetworkCategory Private"'
            )
        if need_port:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{port_rule}" protocol=TCP dir=in '
                f'localport={port} action=allow'
            )
        if need_prog:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{prog_rule}" dir=in action=allow '
                f'program="{py_exe}" enable=yes'
            )

        bat_body = "\r\n".join(bat_lines) + "\r\n"
        fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="jarvis_fw_")
        try:
            os.write(fd, bat_body.encode("mbcs"))   # Windows cmd.exe expects ANSI
            os.close(fd)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            return

        # ── Try running directly (succeeds when already admin) ────────────────
        try:
            r = subprocess.run(
                [bat_path], capture_output=True, timeout=8, shell=True
            )
            if r.returncode == 0:
                print(f"[Dashboard] Firewall configured for port {port}.")
                try:
                    os.unlink(bat_path)
                except Exception:
                    pass
                return
        except Exception:
            pass

        # ── ShellExecuteW: native UAC elevation (most reliable on Windows) ────
        # ShellExecuteW with verb "runas" always shows the UAC dialog regardless
        # of UAC level settings. Non-blocking — uvicorn is already running.
        print("[Dashboard] One-time network setup required.")
        print("[Dashboard] >>> A Windows security dialog will appear — click 'Yes' <<<")
        try:
            ret = ctypes.windll.shell32.ShellExecuteW(
                None,       # hwnd  (no parent window)
                "runas",    # verb  (request elevation)
                bat_path,   # file  (our .bat)
                None,       # params
                None,       # working dir
                0,          # SW_HIDE (run without a visible cmd window)
            )
            if int(ret) > 32:
                # ShellExecuteW returns immediately; bat finishes in ~1 second.
                # Sleep briefly so the rules are in place before the first retry.
                time.sleep(2)
                print(f"[Dashboard] Network setup complete — port {port} is open.")
                print("[Dashboard] Refresh your phone browser to connect.")
            else:
                print("[Dashboard] Setup was not allowed.")
                print("[Dashboard] Phone connections may fail until JARVIS is run as Administrator.")
        except Exception as e:
            print(f"[Dashboard] Firewall setup error: {e}")
        finally:
            # Cleanup after the bat has had time to run
            def _cleanup(path: str) -> None:
                time.sleep(5)
                try:
                    os.unlink(path)
                except Exception:
                    pass
            threading.Thread(target=_cleanup, args=(bat_path,), daemon=True).start()
        return

    # ── macOS ─────────────────────────────────────────────────────────────────
    if sys.platform == "darwin":
        fw_ctl = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        try:
            r = subprocess.run(
                [fw_ctl, "--getglobalstate"], capture_output=True, text=True, timeout=5,
            )
            if "disabled" in r.stdout.lower():
                return  # firewall off — nothing to do

            py = sys.executable
            listed = subprocess.run(
                [fw_ctl, "--listapps"], capture_output=True, text=True, timeout=5,
            )
            if py in listed.stdout:
                return  # already allowed

            print("[Dashboard] One-time network setup — enter your password in the macOS dialog.")
            subprocess.run(
                ["osascript", "-e",
                 f'do shell script "{fw_ctl} --add {py} && {fw_ctl} --unblockapp {py}"'
                 f' with administrator privileges'],
                timeout=60,
            )
        except Exception:
            pass  # macOS firewall is off by default — silent failure is fine
        return

    # ── Linux ─────────────────────────────────────────────────────────────────
    def _privileged(cmd: list[str]) -> bool:
        for prefix in (["pkexec"], ["sudo", "-n"]):
            try:
                r = subprocess.run(prefix + cmd, capture_output=True, timeout=30)
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        return False

    try:  # ufw
        r = subprocess.run(["ufw", "status"], capture_output=True, text=True, timeout=5)
        if "active" in r.stdout.lower():
            if _privileged(["ufw", "allow", f"{port}/tcp"]):
                print(f"[Dashboard] ufw: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo ufw allow {port}/tcp")
            return
    except FileNotFoundError:
        pass

    try:  # firewalld
        r = subprocess.run(
            ["firewall-cmd", "--state"], capture_output=True, text=True, timeout=5,
        )
        if "running" in r.stdout.lower():
            ok = (_privileged(["firewall-cmd", "--add-port", f"{port}/tcp", "--permanent"])
                  and _privileged(["firewall-cmd", "--reload"]))
            if ok:
                print(f"[Dashboard] firewalld: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo firewall-cmd --add-port={port}/tcp --permanent && sudo firewall-cmd --reload")
            return
    except FileNotFoundError:
        pass

    try:  # iptables (not persistent but works until reboot)
        r = subprocess.run(["iptables", "-L", "INPUT", "-n"], capture_output=True, timeout=5)
        if r.returncode == 0:
            if _privileged(["iptables", "-A", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"]):
                print(f"[Dashboard] iptables: port {port} opened.")
            else:
                print(f"[Dashboard] Run manually:  sudo iptables -A INPUT -p tcp --dport {port} -j ACCEPT")
    except FileNotFoundError:
        pass  # no iptables means firewall is probably off — nothing to do


def _ensure_crypto_js() -> None:
    if _CRYPTOJS_FILE.exists():
        return
    try:
        import urllib.request
        print("[Dashboard] Downloading CryptoJS (one-time setup)…")
        urllib.request.urlretrieve(_CRYPTOJS_CDN, str(_CRYPTOJS_FILE))
        print("[Dashboard] CryptoJS cached — will serve locally from now on.")
    except Exception as e:
        print(f"[Dashboard] CryptoJS download failed: {e}")
        print(f"[Dashboard] Encryption will fall back to CDN load on client.")


_ensure_crypto_js()


# ── helpers ───────────────────────────────────────────────────────────────────

def _local_ip() -> str:
    """Return the best LAN-facing IPv4 address, no internet required."""
    # Method 1: route trick (fast, works when internet is available)
    for probe in ("8.8.8.8", "1.1.1.1", "192.168.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                return ip
        except Exception:
            pass

    # Method 2: hostname resolution (works offline on most systems)
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith("127."):
            return ip
    except Exception:
        pass

    # Method 3: enumerate all interfaces (fully offline, no external deps)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except Exception:
        pass

    return "127.0.0.1"


def _ensure_certs() -> bool:
    """
    Make sure config/certs holds a TLS key pair, generating a self-signed one the
    first time the dashboard runs.

    The pair is deliberately NOT shipped in the repository. A private key that
    every user downloads is the same as having no private key at all: anyone can
    present a certificate that matches it. Generating locally gives each install
    its own key, costs about a second, and happens exactly once.

    Returns True when a usable pair exists afterwards; False leaves the caller on
    plain HTTP, which still works — the QR code simply encodes http:// instead.
    """
    certs = BASE_DIR / "config" / "certs"
    key_p = certs / "jarvis.key"
    crt_p = certs / "jarvis.crt"
    if key_p.exists() and crt_p.exists():
        return True

    try:
        import datetime
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        print("[Dashboard] cryptography not installed — serving over plain HTTP.")
        print("[Dashboard] For HTTPS run:  pip install cryptography")
        return False

    try:
        certs.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        who = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS Dashboard"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "JARVIS"),
        ])

        # The SAN has to cover every address the phone might use: the LAN IP the
        # QR code encodes, plus localhost when testing on the machine itself.
        alt = [x509.DNSName("localhost"),
               x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]
        try:
            lan = _local_ip()
            if not lan.startswith("127."):
                alt.append(x509.IPAddress(ipaddress.IPv4Address(lan)))
        except Exception:
            pass          # no LAN address resolvable — localhost entries still work

        # Timezone-aware UTC: datetime.utcnow() is deprecated from Python 3.12 on,
        # and the builder normalises aware values to UTC itself.
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(who)
            .issuer_name(who)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )

        key_p.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        crt_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

        try:
            import os as _os
            _os.chmod(key_p, 0o600)   # best effort — largely a no-op on Windows
        except Exception:
            pass

        print(f"[Dashboard] Generated a self-signed certificate for this machine: {certs}")
        return True
    except Exception as e:
        print(f"[Dashboard] Certificate generation failed ({e}) — serving over plain HTTP.")
        return False


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


# ── DashboardServer ───────────────────────────────────────────────────────────

class DashboardServer:

    def __init__(self):
        self._ip                          = _local_ip()
        self._tokens: set[str]            = set()
        self._token_keys: dict[str, str]  = {}   # auth_token → session_key
        self._aes_cache:  dict[str, bytes]= {}   # session_key → AES bytes
        self._clients: set[WebSocket]     = set()
        self._history: list[dict]         = []
        self._command_queue               = asyncio.Queue()
        self._photo_queue                 = asyncio.Queue()   # phone photos for vision
        self._wake_callback               = None
        self._connect_callback            = None
        self._pending_keys: dict[str, float] = {}
        self._device_sessions: dict[str, dict] = {}  # device_token → {session_key}
        self._device_seen: dict[str, float] = {}    # device_token → last activity
        self._phone_audio_queue: asyncio.Queue    = asyncio.Queue(maxsize=200)
        self._uploads_dir                 = UPLOADS_DIR
        self._login_html                  = _read("login.html")
        self._app_html                    = _read("app.html")
        try:
            self._hud_html = _read("hud.html")
        except Exception:
            self._hud_html = ""
        # ── Live HUD feed (/hud + /ws/hud) ──────────────────────────────
        # Read-only, no token: it carries metrics, connection state and the
        # already-broadcast log lines — nothing that can command the
        # assistant. Anything that acts still goes through the authed APIs.
        self._hud_clients: set = set()
        self._hud_state: dict = {"state": "starting"}
        self._start_ts                    = time.time()
        self.app                          = self._build_app()

    # ── one-time key management ───────────────────────────────────────────

    def new_key(self, expiry_secs: int = 600) -> str:
        now = time.time()
        self._pending_keys = {k: v for k, v in self._pending_keys.items() if v > now}
        key = ''.join(secrets.choice(_KEY_CHARS) for _ in range(6))
        self._pending_keys[key] = now + expiry_secs
        return key

    @staticmethod
    def _ssl_enabled() -> bool:
        certs = BASE_DIR / "config" / "certs"
        return (certs / "jarvis.key").exists() and (certs / "jarvis.crt").exists()

    def get_url(self) -> str:
        proto = "https" if self._ssl_enabled() else "http"
        return f"{proto}://{self._ip}:{PORT}"

    def get_manual_url(self) -> str:
        """URL for manual browser entry. When HTTPS active, points to alias port (also HTTPS)."""
        if self._ssl_enabled():
            return f"{self._ip}:{PORT + 1}"
        return f"{self._ip}:{PORT}"

    def _aes_key(self, session_key: str) -> bytes:
        if session_key not in self._aes_cache:
            self._aes_cache[session_key] = _derive_key(session_key)
        return self._aes_cache[session_key]

    def _decrypt(self, token: str, enc_b64: str) -> str | None:
        sk = self._token_keys.get(token)
        if not sk:
            return None
        try:
            return _decrypt_cbc(self._aes_key(sk), enc_b64)
        except Exception:
            return None

    def _touch_device(self, token: str) -> None:
        """Record LAN presence for a persistent device token, if we know it."""
        try:
            for dev_tok, sess in self._device_sessions.items():
                if sess.get("session_key") and self._token_keys.get(token) == sess["session_key"]:
                    self._device_seen[dev_tok] = time.time()
                    break
        except Exception:
            pass

    def device_presence(self) -> dict:
        """{'paired': n, 'stale_sec': seconds since newest sighting or None}."""
        try:
            now = time.time()
            if not self._device_seen:
                return {"paired": len(self._device_sessions), "stale_sec": None}
            return {"paired": len(self._device_sessions),
                    "stale_sec": int(now - max(self._device_seen.values()))}
        except Exception:
            return {"paired": 0, "stale_sec": None}

    # ── callbacks ────────────────────────────────────────────────────────

    def set_wake_callback(self, fn) -> None:
        self._wake_callback = fn

    def set_connect_callback(self, fn) -> None:
        self._connect_callback = fn

    # ── broadcast ────────────────────────────────────────────────────────

    def notify_beep(self, seconds: int = 10) -> bool:
        """Thread-safe: ring connected phones from any thread (e.g. an action
        running in the executor). Returns False when nobody is listening."""
        try:
            if not self._clients:
                return False
            loop = getattr(self, "_loop", None)
            if loop is None:
                return False
            loop.call_soon_threadsafe(
                lambda: loop.create_task(
                    self.broadcast({"type": "beep",
                                    "for": max(1, min(int(seconds), 30))})))
            return True
        except Exception:
            return False

    async def broadcast(self, msg: dict) -> None:
        self._history.append(msg)
        if len(self._history) > 300:
            self._history = self._history[-300:]
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead
        # Mirror conversation lines + status to the read-only HUD feed so the
        # /hud page speaks and pulses with the real session, not a simulation.
        try:
            if isinstance(msg, dict) and msg.get("type") in ("log", "status", "sys"):
                await self._push_hud(msg)
        except Exception:
            pass

    # ── Live HUD feed ────────────────────────────────────────────────────

    def update_hud(self, **kw) -> None:
        """Merge keys into the HUD snapshot and push it. Sync so main.py can
        call it from anywhere; the socket writes happen on the event loop."""
        try:
            self._hud_state.update(kw)
            self._hud_state["ts"] = time.time()
        except Exception:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._push_hud({"type": "hud",
                                             **self._hud_snapshot()}))
        except Exception:
            pass

    def _hud_snapshot(self) -> dict:
        snap = dict(self._hud_state)
        try:
            snap["uptime"] = int(time.time() - self._start_ts)
            snap["sessions"] = len(self._clients)
        except Exception:
            pass
        return snap

    async def _push_hud(self, msg: dict) -> None:
        dead: set = set()
        for ws in list(self._hud_clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._hud_clients -= dead
        # Phone clients are authenticated, so they also get the live snapshot:
        # the phone becomes a second HUD, not just a remote keyboard.
        if isinstance(msg, dict) and msg.get("type") == "hud":
            dead_p: set[WebSocket] = set()
            for ws in list(self._clients):
                try:
                    await ws.send_json(msg)
                except Exception:
                    dead_p.add(ws)
            self._clients -= dead_p

    async def _hud_ticker(self) -> None:
        """Every 3 s: refresh system metrics into the HUD snapshot and push."""
        while True:
            try:
                cpu = ram = 0.0
                try:
                    import psutil as _ps
                    cpu = float(_ps.cpu_percent(interval=None))
                    ram = float(_ps.virtual_memory().percent)
                except Exception:
                    pass
                self._hud_state.update({"cpu": round(cpu, 1),
                                        "ram": round(ram, 1)})
                await self._push_hud({"type": "hud", **self._hud_snapshot()})
            except Exception:
                pass
            await asyncio.sleep(3)

    # ── FastAPI app ───────────────────────────────────────────────────────

    def _build_app(self) -> "FastAPI":
        app = FastAPI(docs_url=None, redoc_url=None)

        def _auth(req: Request) -> bool:
            tok = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            return bool(tok) and tok in self._tokens

        # serve CryptoJS from local cache, fallback to CDN redirect
        @app.get("/static/crypto.js")
        async def serve_crypto():
            if _CRYPTOJS_FILE.exists():
                return FileResponse(str(_CRYPTOJS_FILE),
                                    media_type="application/javascript")
            from fastapi.responses import RedirectResponse
            return RedirectResponse(_CRYPTOJS_CDN)

        @app.get("/login", response_class=HTMLResponse)
        async def login_page():
            return HTMLResponse(self._login_html)

        @app.get("/", response_class=HTMLResponse)
        async def index():
            # Auth is handled client-side via sessionStorage bearer token.
            # Server-side header auth can't work here because browser navigations
            # don't send custom headers (location.href doesn't carry Authorization).
            html = (self._app_html
                    .replace("__IP__", self._ip)
                    .replace("__PORT__", str(PORT)))
            return HTMLResponse(html)

        @app.get("/hud", response_class=HTMLResponse)
        async def hud_page():
            # Enhanced local HUD — no auth, same machine only UI.
            # Remote / still serves the phone controller.
            if self._hud_html:
                return HTMLResponse(self._hud_html)
            return HTMLResponse("<h3>hud.html missing</h3>", status_code=404)

        @app.websocket("/ws/hud")
        async def hud_ws(websocket: WebSocket):
            """Read-only live feed for the /hud page: snapshot + log/status
            lines as they happen. No token — it can only watch, never act."""
            await websocket.accept()
            self._hud_clients.add(websocket)
            try:
                await websocket.send_json({"type": "hud",
                                           **self._hud_snapshot()})
                try:
                    from memory import history as _hist
                    await websocket.send_json({"type": "history",
                                               "turns": _hist.recent(20)})
                except Exception:
                    pass
                try:
                    from core import routines as _rt
                    await websocket.send_json({"type": "routines",
                                               "routines": _rt.list_all()})
                except Exception:
                    pass
                for entry in self._history[-20:]:
                    try:
                        if isinstance(entry, dict) and entry.get("type") in (
                                "log", "status", "sys"):
                            await websocket.send_json(entry)
                    except Exception:
                        break
                while True:
                    try:
                        await websocket.receive_text()
                    except Exception:
                        break
            finally:
                self._hud_clients.discard(websocket)

        @app.get("/api/history")
        async def history_ep(req: Request, n: int = 20, q: str = ""):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from memory import history as _hist
                turns = _hist.search(q, n) if q else _hist.recent(n)
                return JSONResponse({"ok": True, "turns": turns})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        # ── Routines (user automations) ──────────────────────────────────

        @app.get("/api/routines")
        async def routines_list(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import routines as _rt
                return JSONResponse({"ok": True, "routines": _rt.list_all()})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.post("/api/routines")
        async def routines_add(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import routines as _rt
                body = await req.json()
                r = _rt.add(str(body.get("name", "")),
                            body.get("schedule") or {},
                            str(body.get("command", "")))
                if "error" in r:
                    return JSONResponse({"error": r["error"]}, status_code=400)
                await self.broadcast({"type": "sys",
                                      "text": f"Routine added: {r['name']}"})
                return JSONResponse({"ok": True, "routine": r})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.delete("/api/routines/{rid}")
        async def routines_del(req: Request, rid: str):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import routines as _rt
                if not _rt.remove(rid.strip()):
                    return JSONResponse({"error": "Not found"}, status_code=404)
                return JSONResponse({"ok": True})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.post("/api/routines/{rid}/toggle")
        async def routines_toggle(req: Request, rid: str):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import routines as _rt
                body = await req.json()
                if not _rt.set_enabled(rid.strip(),
                                       bool(body.get("enabled", True))):
                    return JSONResponse({"error": "Not found"}, status_code=404)
                return JSONResponse({"ok": True})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        # ── Usage ledger ───────────────────────────────────────────────
        @app.get("/api/usage")
        async def usage_ep(req: Request, days: int = 7):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import usage as _usage
                return JSONResponse(_usage.summary(days))
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        # ── Staged facts (memory consolidation inbox) ────────────────

        @app.get("/api/staged")
        async def staged_list(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import consolidate as _cs
                return JSONResponse({"ok": True, "facts": _cs.list_staged()})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.post("/api/staged/{fid}/approve")
        async def staged_approve(req: Request, fid: str):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import consolidate as _cs
                ok, msg = _cs.approve(fid.strip())
                if not ok:
                    return JSONResponse({"error": msg}, status_code=404)
                return JSONResponse({"ok": True, "message": msg})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.post("/api/staged/{fid}/reject")
        async def staged_reject(req: Request, fid: str):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import consolidate as _cs
                ok, msg = _cs.reject(fid.strip())
                if not ok:
                    return JSONResponse({"error": msg}, status_code=404)
                return JSONResponse({"ok": True, "message": msg})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.post("/api/consolidate")
        async def consolidate_now(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import consolidate as _cs
                return JSONResponse({"ok": True, **_cs.run_once()})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        # ── Presence lock (walk-away security) ─────────────────────────
        # Off (0 minutes) unless the user opts in: locking someone's PC
        # unprompted is not a default any feature gets to have.

        @app.get("/api/presence")
        async def presence_get(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                import json as _json
                cfg_path = BASE_DIR / "config" / "api_keys.json"
                mins = int((_json.loads(cfg_path.read_text(encoding="utf-8"))
                            .get("presence_lock_minutes", 0) or 0))
            except Exception:
                mins = 0
            return JSONResponse({"ok": True,
                                 **self.device_presence(),
                                 "lock_after_minutes": mins})

        @app.post("/api/presence")
        async def presence_set(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                import json as _json
                body = await req.json()
                mins = max(0, min(int(body.get("minutes", 0)), 240))
                cfg_path = BASE_DIR / "config" / "api_keys.json"
                cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
                cfg["presence_lock_minutes"] = mins
                tmp = cfg_path.with_suffix(".tmp")
                tmp.write_text(_json.dumps(cfg, indent=1), encoding="utf-8")
                tmp.replace(cfg_path)
                return JSONResponse({"ok": True,
                                     "lock_after_minutes": mins})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        # ── Backup / restore ─────────────────────────────────────────

        @app.get("/api/backups")
        async def backups_list(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import backup as _bk
                return JSONResponse({"ok": True, "backups": _bk.list_backups()})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.post("/api/backup")
        async def backup_now(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import backup as _bk
                ok, msg = _bk.export()
                if not ok:
                    return JSONResponse({"error": msg}, status_code=500)
                await self.broadcast({"type": "sys",
                                      "text": f"💾 Backup written: {Path(msg).name}"})
                return JSONResponse({"ok": True, "message": msg})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.get("/api/backup/download")
        async def backup_download(token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import backup as _bk
                items = _bk.list_backups()
                if not items:
                    return JSONResponse({"error": "No backups yet"}, status_code=404)
                path = _bk._BACKUP_DIR / items[0]["name"]
                return FileResponse(str(path), filename=items[0]["name"],
                                    media_type="application/zip")
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        if _UPLOAD_OK:
            @app.post("/api/restore")
            async def restore_ep(req: Request, file: UploadFile = FastAPIFile(...)):
                if not _auth(req):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)
                try:
                    from core import backup as _bk
                    data = await file.read(51 * 1024 * 1024)
                    ok, msg = _bk.restore(data)
                    if not ok:
                        return JSONResponse({"error": msg}, status_code=400)
                    await self.broadcast({"type": "sys", "text": f"💾 {msg}"})
                    return JSONResponse({"ok": True, "message": msg})
                except Exception as e:
                    return JSONResponse({"error": str(e)}, status_code=500)

        # ── Checklists ─────────────────────────────────────────────

        @app.get("/api/lists")
        async def lists_ep(req: Request, name: str = ""):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import checklists as _cl
                if name:
                    return JSONResponse({"ok": True, "items": _cl.show(name)})
                return JSONResponse({"ok": True, "lists": _cl.lists()})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.post("/api/lists")
        async def lists_add(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import checklists as _cl
                body = await req.json()
                mode = str(body.get("mode", "add"))
                if mode == "check":
                    msg = _cl.check(str(body.get("list", "")),
                                    str(body.get("text", "")))
                elif mode == "clear":
                    msg = _cl.clear_done(str(body.get("list", "")))
                else:
                    msg = _cl.add(str(body.get("list", "")),
                                  str(body.get("text", "")))
                return JSONResponse({"ok": True, "message": msg})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        # ── Plugin installer ─────────────────────────────────────────
        @app.post("/api/plugins/install")
        async def plugins_install(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import plugin_loader as _pl
                body = await req.json()
                ok, msg = _pl.install_from_url(str(body.get("url", "")))
                if ok:
                    await self.broadcast({"type": "sys", "text": msg})
                    return JSONResponse({"ok": True, "message": msg})
                return JSONResponse({"error": msg}, status_code=400)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        # ── Audio self-test (first-run wizard helper) ────────────────────
        # Never opens a stream — the live session owns the devices — so it is
        # always safe to call, even mid-conversation. It answers the three
        # questions every "can't hear me / can't hear it" report starts with:
        # what is configured, what actually exists, and do they match.

        @app.get("/api/audio-test")
        async def audio_test(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import audio_devices as _ad
                from memory import config_manager as _cm
                want_in = _cm.get_input_device()
                want_out = _cm.get_output_device()
                ins = _ad.list_devices("input")
                outs = _ad.list_devices("output")
                in_ok = _ad.resolve(want_in, "input") is not None or not ins
                out_ok = _ad.resolve(want_out, "output") is not None or not outs
                return JSONResponse({
                    "ok": True,
                    "configured": {"input": want_in, "output": want_out},
                    "available": {"inputs": ins, "outputs": outs},
                    "match": {"input": bool(in_ok), "output": bool(out_ok)},
                    "hint": ("All good." if (in_ok and out_ok)
                             else "Open JARVIS ⚙ → Audio Devices and re-pick "
                                  "the missing device."),
                })
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.post("/login")
        async def login(req: Request):
            body    = await req.json()
            entered = str(body.get("pin", "")).strip().upper()
            now     = time.time()
            if entered in self._pending_keys and self._pending_keys[entered] > now:
                del self._pending_keys[entered]          # one-time use
                tok = secrets.token_urlsafe(32)
                self._tokens.add(tok)
                self._token_keys[tok] = entered
                self._aes_key(entered)                   # pre-derive & cache
                if self._connect_callback:
                    self._connect_callback()
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Remote connection established."}
                ))
                # Bearer token in response body — no cookies needed (works on any browser/HTTP)
                return JSONResponse({"ok": True, "token": tok})
            return JSONResponse({"ok": False, "error": "Invalid or expired key"},
                                status_code=401)

        @app.get("/auto-login")
        async def auto_login(key: str = ""):
            """QR code target — validates one-time key, creates session, redirects phone."""
            now = time.time()
            if not key or key not in self._pending_keys or self._pending_keys[key] <= now:
                return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}
  h2{color:#f87171;margin-bottom:12px}p{color:#5e6a7e;font-size:14px}
</style></head>
<body><div><h2>Link Expired</h2>
<p>Press <strong style="color:#dde3ed">Remote Control</strong> in JARVIS to get a new QR code.</p>
</div></body></html>""")

            del self._pending_keys[key]
            tok     = secrets.token_urlsafe(32)
            dev_tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._token_keys[tok] = key
            self._aes_key(key)
            self._device_sessions[dev_tok] = {"session_key": key}
            self._device_seen[dev_tok] = time.time()

            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Remote connection established via QR code."}
            ))

            return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}}
  p{{color:#5e6a7e;font-size:14px}}
</style></head>
<body>
<script>
  sessionStorage.setItem('jarvis_token','{tok}');
  sessionStorage.setItem('jarvis_key','{key}');
  localStorage.setItem('jarvis_device_token','{dev_tok}');
  setTimeout(function(){{location.replace('/')}},400);
</script>
<p>Connecting to JARVIS…</p>
</body></html>""")

        @app.post("/api/device-login")
        async def device_login_ep(req: Request):
            """Return a fresh auth token for a previously paired device token."""
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"ok": False}, status_code=400)
            dev_tok = (body.get("device_token") or "").strip()
            if not dev_tok or dev_tok not in self._device_sessions:
                return JSONResponse({"ok": False}, status_code=401)
            session_key = self._device_sessions[dev_tok]["session_key"]
            self._device_seen[dev_tok] = time.time()
            tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._token_keys[tok] = session_key
            self._aes_key(session_key)
            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Known device reconnected automatically."}
            ))
            return JSONResponse({"ok": True, "token": tok, "key": session_key})

        @app.post("/api/revoke-devices")
        async def revoke_devices(req: Request):
            """Invalidate all persistent device tokens (admin action)."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            count = len(self._device_sessions)
            self._device_sessions.clear()
            return JSONResponse({"ok": True, "revoked": count})

        @app.post("/api/command")
        async def command(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body  = await req.json()
            token = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            enc   = body.get("enc", "")
            if enc:
                text = self._decrypt(token, enc)
                if text is None:
                    return JSONResponse({"error": "Decryption failed"}, status_code=400)
            else:
                text = (body.get("text") or "").strip()
            if text:
                await self._command_queue.put(text)
                self._touch_device(token)
                if self._wake_callback:
                    self._wake_callback()
            return JSONResponse({"ok": True})

        # ── Phone photo as vision ──────────────────────────────────────
        # The phone camera becomes another eye: the picture is queued and the
        # main loop injects it into the Live session like a screen capture,
        # labelled with its source. Authed — a photo is a command.

        @app.post("/api/photo")
        async def photo_ep(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if not _UPLOAD_OK:
                return JSONResponse(
                    {"error": "Photo upload requires: pip install python-multipart"},
                    status_code=503)
            try:
                form = await req.form()
                up = form.get("photo")
                question = str(form.get("question", "") or "").strip() or \
                    "What do you see in this photo from my phone?"
                if up is None or not hasattr(up, "read"):
                    return JSONResponse({"error": "No photo attached"},
                                        status_code=400)
                data = await up.read()
                if not data or len(data) > 10 * 1024 * 1024:
                    return JSONResponse({"error": "Photo empty or over 10 MB"},
                                        status_code=400)
                fname = _safe_filename(getattr(up, "filename", "") or "phone.jpg")
                if "." not in fname:
                    fname += ".jpg"
                dest = self._uploads_dir / fname
                stem, suffix = Path(fname).stem, Path(fname).suffix
                counter = 1
                while dest.exists():
                    dest = self._uploads_dir / f"{stem}_{counter}{suffix}"
                    counter += 1
                dest.write_bytes(data)
                await self._photo_queue.put({"path": str(dest),
                                             "question": question})
                self._touch_device(req.headers.get("authorization", "")
                                   .removeprefix("Bearer ").strip())
                if self._wake_callback:
                    self._wake_callback()
                return JSONResponse({"ok": True, "name": dest.name})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.post("/api/wake")
        async def wake_ep(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if self._wake_callback:
                self._wake_callback()
            return JSONResponse({"ok": True})

        # ── Phone mic real-time audio → Gemini Live ──────────────────────────

        @app.websocket("/ws/phone-audio")
        async def phone_audio_ws(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._touch_device(tok)
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Phone microphone live."}
            ))
            try:
                while True:
                    data = await websocket.receive_bytes()
                    try:
                        self._phone_audio_queue.put_nowait(
                            {"data": data, "mime_type": "audio/pcm"}
                        )
                    except asyncio.QueueFull:
                        pass  # drop frame rather than block
            except WebSocketDisconnect:
                pass
            finally:
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Phone microphone stopped."}
                ))

        # ── File sharing ──────────────────────────────────────────────────────

        def _safe_filename(raw: str) -> str:
            name = Path(raw).name                          # strip path components
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(". ")
            return name or "upload"

        if _UPLOAD_OK:
            @app.post("/api/upload")
            async def upload_file(req: Request, file: UploadFile = FastAPIFile(...)):
                if not _auth(req):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)

                safe = _safe_filename(file.filename or "upload")
                dest = self._uploads_dir / safe
                stem, suffix = Path(safe).stem, Path(safe).suffix
                counter = 1
                while dest.exists():
                    dest = self._uploads_dir / f"{stem}_{counter}{suffix}"
                    counter += 1

                size = 0
                max_bytes = MAX_UPLOAD_MB * 1024 * 1024
                try:
                    with open(dest, "wb") as fout:
                        while True:
                            chunk = await file.read(65536)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > max_bytes:
                                fout.close()
                                dest.unlink(missing_ok=True)
                                return JSONResponse(
                                    {"error": f"File too large (max {MAX_UPLOAD_MB} MB)"},
                                    status_code=413,
                                )
                            fout.write(chunk)
                except Exception as exc:
                    try:
                        dest.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return JSONResponse({"error": str(exc)}, status_code=500)

                asyncio.create_task(self.broadcast({
                    "type": "file_received",
                    "name": dest.name,
                    "size": size,
                    "saved_to": str(self._uploads_dir),
                }))
                return JSONResponse({"ok": True, "name": dest.name, "size": size})
        else:
            @app.post("/api/upload")
            async def upload_unavailable(req: Request):
                return JSONResponse(
                    {"error": "File uploads require: pip install python-multipart"},
                    status_code=503,
                )

        @app.get("/api/files")
        async def list_files(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            files = []
            try:
                for f in sorted(
                    (p for p in self._uploads_dir.iterdir() if p.is_file()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                ):
                    files.append({"name": f.name, "size": f.stat().st_size})
            except Exception:
                pass
            return JSONResponse({"files": files})

        @app.get("/uploads/{filename}")
        async def download_file(filename: str, token: str = ""):
            # Auth via query param — browser <a download> can't send custom headers
            tok = token.strip()
            if not tok or tok not in self._tokens:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            safe = re.sub(r'[/\\]', '', filename)
            path = self._uploads_dir / safe
            if not path.exists() or not path.is_file():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return FileResponse(str(path), filename=safe)

        @app.websocket("/ws")
        async def ws_ep(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._touch_device(tok)
            self._clients.add(websocket)
            for entry in self._history[-50:]:
                try:
                    await websocket.send_json(entry)
                except Exception:
                    break
            try:
                while True:
                    data = await websocket.receive_json()
                    if data.get("type") == "command":
                        enc = data.get("enc", "")
                        t   = self._decrypt(tok, enc) if enc else (data.get("text") or "").strip()
                        if t:
                            await self._command_queue.put(t)
                            if self._wake_callback:
                                self._wake_callback()
            except WebSocketDisconnect:
                pass
            finally:
                self._clients.discard(websocket)

        return app

    # ── serve ─────────────────────────────────────────────────────────────

    async def _serve_alias(self) -> None:
        """Second HTTPS server on PORT+1 sharing the same app and in-memory state.
        Chrome HTTPS-upgrades any bare IP:PORT the user types, so this port also needs TLS.
        User types IP:8001 → Chrome tries https → self-signed cert warning → accept once → done."""
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"
        asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT + 1)
        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT + 1, log_level="warning",
            ssl_keyfile=str(ssl_key), ssl_certfile=str(ssl_cert),
        )
        print(f"[Dashboard] Manual entry:  {self._ip}:{PORT + 1}  (type in browser, accept cert once)")
        await uvicorn.Server(cfg).serve()

    async def serve(self) -> None:
        if not _DEPS_OK:
            print("[Dashboard] fastapi/uvicorn not installed — dashboard disabled.")
            print("[Dashboard] Run:  pip install fastapi 'uvicorn[standard]' cryptography")
            return

        # Firewall setup runs in a thread — uvicorn starts immediately,
        # no waiting for UAC dialogs or subprocess timeouts.
        asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT)

        # Generate the TLS pair on first run so no private key ships in the repo.
        _ensure_certs()

        try:
            self._loop = asyncio.get_running_loop()
        except Exception:
            pass

        # Live HUD metrics ticker (read-only feed for /hud).
        try:
            asyncio.get_event_loop().create_task(self._hud_ticker())
        except Exception:
            pass

        use_ssl  = self._ssl_enabled()
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"

        if use_ssl:
            asyncio.create_task(self._serve_alias())

        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT, log_level="warning",
            **({"ssl_keyfile": str(ssl_key), "ssl_certfile": str(ssl_cert)} if use_ssl else {}),
        )

        proto = "https" if use_ssl else "http"
        print(f"[Dashboard] {proto}://{self._ip}:{PORT}")
        print("[Dashboard] Press 'Remote Control' in JARVIS UI to get the QR code.")
        await uvicorn.Server(cfg).serve()
