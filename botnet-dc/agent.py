import getpass
import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import psutil
import requests

API = "https://discord.com/api/v10"
MENTION_RE = re.compile(r"<#(\d+)>")


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def bundled_dir() -> Path | None:
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else None


def config_file(name: str) -> Path | None:
    candidates = [APP_DIR / name]
    bundled = bundled_dir()
    if bundled:
        candidates.append(bundled / name)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def data_dir() -> Path:
    if getattr(sys, "frozen", False):
        if platform.system() == "Windows":
            base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        else:
            base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        target = Path(base) / "DevAgent"
        try:
            target.mkdir(parents=True, exist_ok=True)
            return target
        except OSError:
            return APP_DIR
    return Path(__file__).parent


APP_DIR = app_dir()
DATA_DIR = data_dir()
LOG_PATH = DATA_DIR / "agent.log"
STATE_PATH = DATA_DIR / "agent_state.json"
logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("devagent")


def parse_dotenv(path: Path) -> dict:
    data = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return data
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            data[key] = value
    return data


def config_from_dotenv(env: dict) -> dict:
    cfg = {}
    token = env.get("DISCORD_TOKEN") or env.get("BOT_TOKEN")
    if token:
        cfg["token"] = token
    if env.get("CONFIG_CHANNEL_ID"):
        cfg["config_channel_id"] = env["CONFIG_CHANNEL_ID"]
    ids = env.get("ADMIN_USER_ID") or env.get("AUTHORIZED_USER_IDS")
    if ids:
        cfg["authorized_user_ids"] = ids
    if env.get("GENERAL_CHANNEL_ID") or env.get("BROADCAST_CHANNEL_ID"):
        cfg["general_channel_id"] = env.get("GENERAL_CHANNEL_ID") or env.get("BROADCAST_CHANNEL_ID")
    if env.get("DEVICE_NAME"):
        cfg["device_name"] = env["DEVICE_NAME"]
    if env.get("POLL_INTERVAL"):
        cfg["poll_interval"] = env["POLL_INTERVAL"]
    if env.get("ANNOUNCE_ON_START"):
        cfg["announce_on_start"] = env["ANNOUNCE_ON_START"].lower() in ("1", "true", "yes", "on")
    return cfg


def load_config() -> dict:
    env_path = config_file(".env")
    cfg = config_from_dotenv(parse_dotenv(env_path) if env_path else {})
    cfg_path = config_file("agent_config.json")
    if cfg_path:
        try:
            cfg_json = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(cfg_json, dict):
                cfg.update(cfg_json)
        except json.JSONDecodeError as exc:
            print(f"agent_config.json invalido: {exc}")
            sys.exit(1)
    if not cfg:
        print(f"Nem .env nem agent_config.json encontrados (app/bundled: {APP_DIR})")
        sys.exit(1)
    for key in ("token", "config_channel_id"):
        if not str(cfg.get(key, "")).strip():
            print(f"Configuracao: campo '{key}' faltando ou vazio (.env ou agent_config.json)")
            sys.exit(1)
    ids = cfg.get("authorized_user_ids", [])
    if isinstance(ids, str):
        ids = [x.strip() for x in ids.replace(";", ",").split(",") if x.strip()]
    cfg["authorized_user_ids"] = {str(x) for x in ids}
    if not cfg["authorized_user_ids"]:
        print("Configuracao: 'authorized_user_ids' vazio")
        sys.exit(1)
    return cfg


CFG = load_config()
HEADERS = {"Authorization": f"Bot {CFG['token']}"}
CONFIG_CHANNEL = str(CFG["config_channel_id"])


def get_device_id() -> str:
    id_path = DATA_DIR / "device_id.json"
    try:
        data = json.loads(id_path.read_text(encoding="utf-8"))
        if isinstance(data.get("id"), str) and data["id"]:
            return data["id"]
    except Exception:
        pass
    new_id = uuid.uuid4().hex[:12]
    try:
        id_path.write_text(json.dumps({"id": new_id}), encoding="utf-8")
    except OSError as exc:
        log.warning("nÃ£o consegui salvar device_id.json: %s", exc)
    return new_id


DEVICE_ID = get_device_id()
DEVICE_NAME = str(CFG.get("device_name") or socket.gethostname())
POLL_INTERVAL = float(CFG.get("poll_interval", 3))
MIN_AUTO_INTERVAL = 5


def default_state() -> dict:
    return {"binds": {}, "broadcast": None, "positions": {}, "auto": {"enabled": False, "interval": 30}, "shell_cwd": str(Path.home())}


def load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        state = default_state()
        state["binds"] = {str(k): str(v) for k, v in data.get("binds", {}).items()}
        state["broadcast"] = str(data["broadcast"]) if data.get("broadcast") else None
        pos = data.get("positions", {})
        state["positions"]["channels"] = {str(k): str(v) for k, v in pos.get("channels", {}).items()}
        if pos.get("config"):
            state["positions"]["config"] = str(pos["config"])
        auto = data.get("auto", {})
        state["auto"] = {
            "enabled": bool(auto.get("enabled", False)),
            "interval": max(MIN_AUTO_INTERVAL, int(auto.get("interval", 30))),
        }
        cwd = str(data.get("shell_cwd") or "")
        state["shell_cwd"] = cwd if cwd and Path(cwd).is_dir() else str(Path.home())
        return state
    except Exception:
        return default_state()


STATE = load_state()


def save_state():
    payload = {
        "binds": STATE["binds"],
        "broadcast": STATE["broadcast"],
        "positions": {
            "config": STATE["positions"].get("config"),
            "channels": STATE["positions"].get("channels", {}),
        },
        "auto": STATE.get("auto", {"enabled": False, "interval": 30}),
        "shell_cwd": STATE.get("shell_cwd") or str(Path.home()),
    }
    try:
        STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("nÃ£o consegui salvar agent_state.json: %s", exc)


def my_keys() -> set:
    return {DEVICE_NAME.lower(), DEVICE_ID.lower()}


def my_bound_channels() -> list:
    return [cid for cid, target in STATE["binds"].items() if target.lower() in my_keys()]


AUTO_LAST = [0.0]


def auto_target_channel():
    bound = my_bound_channels()
    if bound:
        return bound[0]
    return STATE.get("broadcast")


def maybe_auto_screenshot():
    auto = STATE.get("auto") or {}
    if not auto.get("enabled"):
        return
    now = time.time()
    if now - AUTO_LAST[0] < auto.get("interval", 30):
        return
    channel = auto_target_channel()
    if not channel:
        return
    try:
        _, file = cmd_screenshot()
    except Exception as exc:
        log.warning("auto-screenshot falhou: %s", exc)
        AUTO_LAST[0] = now
        return
    AUTO_LAST[0] = now
    reply(channel, f"auto-screenshot (a cada {auto['interval']}s):", file=file)


def command_channels() -> set:
    channels = set(my_bound_channels())
    if STATE["broadcast"]:
        channels.add(STATE["broadcast"])
    return channels


def api_get(path: str, **params):
    for _ in range(3):
        r = requests.get(f"{API}{path}", headers=HEADERS, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 1)) + 0.2)
            continue
        r.raise_for_status()
        return r.json()
    return []


def api_post(path: str, content: str):
    for _ in range(3):
        r = requests.post(f"{API}{path}", headers=HEADERS, json={"content": content[:2000]}, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 1)) + 0.2)
            continue
        r.raise_for_status()
        return r.json()
    return None


def api_post_file(path: str, content: str, filename: str, file_bytes: bytes, content_type: str):
    files = {"files[0]": (filename, file_bytes, content_type)}
    data = {"payload_json": json.dumps({"content": content[:2000]}, ensure_ascii=False)}
    for _ in range(3):
        r = requests.post(f"{API}{path}", headers=HEADERS, data=data, files=files, timeout=60)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 1)) + 0.2)
            continue
        r.raise_for_status()
        return r.json()
    return None


def reply(channel_id: str, content: str, file: tuple | None = None):
    text = f"[{DEVICE_NAME}] {content}"
    try:
        if file:
            filename, file_bytes, content_type = file
            api_post_file(f"/channels/{channel_id}/messages", text, filename, file_bytes, content_type)
        else:
            api_post(f"/channels/{channel_id}/messages", text)
    except requests.RequestException as exc:
        log.error("falha ao responder em %s: %s", channel_id, exc)


def latest_message_id(channel_id: str):
    try:
        msgs = api_get(f"/channels/{channel_id}/messages", limit=1)
        return msgs[0]["id"] if msgs else "0"
    except requests.RequestException as exc:
        log.error("falha ao buscar Ãºltimo id de %s: %s", channel_id, exc)
        return None


def fetch_messages(channel_id: str, after_id: str | None):
    params = {"limit": 100}
    if after_id:
        params["after"] = after_id
    try:
        return api_get(f"/channels/{channel_id}/messages", **params)
    except requests.RequestException as exc:
        log.error("falha ao ler canal %s: %s", channel_id, exc)
        return []


def human_uptime() -> str:
    secs = int(time.time() - psutil.boot_time())
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{rem // 60}min")
    return " ".join(parts)


def disk_info() -> str:
    lines = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        lines.append(f"  {part.mountpoint}: {usage.percent}% de {round(usage.total / 1e9, 1)} GB")
    return "\n".join(lines) or "  (sem discos legÃ­veis)"


def net_info() -> str:
    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    lines = []
    for name, entries in sorted(addrs.items()):
        st = stats.get(name)
        up = "up" if (st and st.isup) else "down"
        ips = []
        for entry in entries:
            fam = getattr(entry.family, "name", str(entry.family))
            if "AF_INET6" in fam:
                ips.append(f"ipv6 {entry.address}")
            elif "AF_INET" in fam:
                ips.append(f"ipv4 {entry.address}")
        if ips:
            lines.append(f"  {name} [{up}]: {', '.join(ips)}")
    return "\n".join(lines) or "  (sem interfaces de rede legíveis)"


def hardware_info() -> str:
    vm = psutil.virtual_memory()
    disk_total = 0
    disk_used = 0
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        disk_total += usage.total
        disk_used += usage.used
    disk_pct = round(disk_used / disk_total * 100, 1) if disk_total else 0.0
    return (
        f"CPU: {psutil.cpu_count(logical=True)} threads, {psutil.cpu_percent(interval=0.1)}% agora | "
        f"RAM: {vm.percent}% ({round(vm.used / 1e9, 1)}/{round(vm.total / 1e9, 1)} GB)\n"
        f"Disco: {disk_pct}% usado ({round(disk_used / 1e9, 1)}/{round(disk_total / 1e9, 1)} GB)"
    )


def startup_announce() -> str | None:
    if not CFG.get("announce_on_start", True):
        return None
    target = STATE.get("broadcast")
    if not target:
        log.info("sem canal geral (broadcast) configurado; aviso de início não enviado")
        return None
    bound = my_bound_channels()
    if bound:
        destino = f"respondendo em <#{bound[0]}>"
        pergunta = ""
    else:
        destino = "ainda sem canal"
        pergunta = (
            f"\nOnde quer conversar com este PC? Crie um canal no Discord e depois envie no #config:\n"
            f"`!bind #canal {DEVICE_NAME}` (ou `!bind #canal {DEVICE_ID}`)"
        )
    text = (
        f"**online** — `{DEVICE_NAME}` (`{DEVICE_ID}`) — {destino}\n"
        f"User: `{getpass.getuser()}` | SO: {platform.system()} {platform.release()}\n"
        f"Uptime: {human_uptime()}\n"
        f"Rede:\n{net_info()}\n"
        f"Hardware:\n{hardware_info()}"
        f"{pergunta}"
    )
    reply(target, text)
    return target


def cmd_status() -> str:
    vm = psutil.virtual_memory()
    return (
        f"**STATUS** `{DEVICE_NAME}` (`{DEVICE_ID}`)\n"
        f"SO: {platform.system()} {platform.release()}\n"
        f"CPU: {psutil.cpu_percent(interval=0.5)}% | RAM: {vm.percent}% "
        f"({round(vm.used / 1e9, 1)}/{round(vm.total / 1e9, 1)} GB)\n"
        f"Disco:\n{disk_info()}\n"
        f"Uptime: {human_uptime()}"
    )


def run_power(kind: str) -> str:
    is_windows = platform.system() == "Windows"
    if kind == "desligar":
        cmd = ["shutdown", "/s", "/t", "5"] if is_windows else ["systemctl", "poweroff"]
        fallback = ["shutdown", "-h", "now"]
    elif kind == "reiniciar":
        cmd = ["shutdown", "/r", "/t", "5"] if is_windows else ["systemctl", "reboot"]
        fallback = ["shutdown", "-r", "now"]
    else:
        cmd = ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"] if is_windows else ["systemctl", "suspend"]
        fallback = ["loginctl", "suspend"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=15,
                              creationflags=no_window_flags())
        if proc.returncode != 0 and not is_windows:
            proc = subprocess.run(fallback, capture_output=True, text=True, errors="replace", timeout=15,
                                  creationflags=no_window_flags())
        if proc.returncode == 0:
            return f"'{kind}' enviado"
        return f"FALHOU: {(proc.stderr or proc.stdout).strip() or 'exit ' + str(proc.returncode)}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"FALHOU: {exc}"


def cmd_message(text: str, system_name: str | None = None) -> str:
    text = " ".join(text.split())[:500]
    if not text:
        return "uso: `!msg <texto>`"
    system = system_name or platform.system()
    try:
        if system == "Windows":
            import base64
            safe = text.replace("'", "''")
            script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "[System.Windows.Forms.MessageBox]::Show('" + safe + "', 'DevAgent — aviso', 'OK', 'Information') | Out-Null"
            )
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=no_window_flags(),
            )
            return "popup enviado ao PC"

        for candidate in (
            ["zenity", "--info", "--title=DevAgent — aviso", f"--text={text}"],
            ["xmessage", "-center", text],
            ["notify-send", "DevAgent — aviso", text],
        ):
            try:
                subprocess.Popen(candidate, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=no_window_flags())
                return f"popup enviado ({candidate[0]})"
            except FileNotFoundError:
                continue
        return "FALHOU: nenhum zenity, xmessage ou notify-send encontrado"
    except OSError as exc:
        return f"FALHOU: {exc}"


def cmd_screenshot():
    import io
    import mss
    from PIL import Image
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        shot = sct.grab(monitor)
        image = Image.frombytes("RGB", shot.size, shot.rgb)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=70)
    return "screenshot:", ("screenshot.jpg", buffer.getvalue(), "image/jpeg")


RAW_RISKY_RE = re.compile(r"\b(format|diskpart|shutdown|restart-computer|stop-computer|rm|rmdir|rd|del|erase|remove-item|ri)\b", re.IGNORECASE)


def is_risky_raw_command(command: str) -> bool:
    return bool(RAW_RISKY_RE.search(command))


def change_directory_command(command: str):
    match = re.match(r"^\s*(?:cd|chdir)(?:\s+/d)?(?:\s+(.*))?\s*$", command, re.IGNORECASE)
    if not match:
        return None
    target = (match.group(1) or "").strip()
    if target.startswith('"') and target.endswith('"') and len(target) >= 2:
        target = target[1:-1]
    elif target.startswith("'") and target.endswith("'") and len(target) >= 2:
        target = target[1:-1]
    current = STATE.get("shell_cwd") or str(Path.home())
    if not target:
        return current, f"cwd: `{current}`"
    try:
        path = Path(target).expanduser()
        if not path.is_absolute():
            path = Path(current) / path
        path = path.resolve()
    except OSError as exc:
        return None, f"FALHOU: caminho invalido: {exc}"
    if not path.is_dir():
        return None, f"FALHOU: pasta nao existe: `{path}`"
    STATE["shell_cwd"] = str(path)
    save_state()
    return str(path), f"cwd: `{path}`"


def run_shell_with_cwd(command: str, timeout: int = 30) -> str:
    changed = change_directory_command(command)
    if changed is not None:
        return changed[1]
    return cmd_shell(command, timeout, STATE.get("shell_cwd"))


def cmd_shell(command: str, timeout: int = 30, cwd: str | None = None) -> str:
    timeout = min(max(timeout, 1), 120)
    if platform.system() == "Windows":
        argv = ["powershell.exe", "-NoProfile", "-Command", command]
    else:
        argv = ["/bin/sh", "-c", command]
    try:
        with tempfile.TemporaryDirectory(prefix="devagent_") as td:
            out_path = Path(td) / "out.txt"
            err_path = Path(td) / "err.txt"
            with out_path.open("wb") as out_f, err_path.open("wb") as err_f:
                try:
                    proc = subprocess.Popen(argv, stdout=out_f, stderr=err_f,
                                            stdin=subprocess.DEVNULL, cwd=cwd or None,
                                            creationflags=no_window_flags())
                except OSError as exc:
                    return f"FALHOU: {exc}"
                try:
                    returncode = proc.wait(timeout=timeout)
                    timed_out = False
                except subprocess.TimeoutExpired:
                    timed_out = True
                    try:
                        proc.kill()
                        proc.wait(timeout=5)
                    except Exception:
                        pass
                    returncode = None
            out_bytes = out_path.read_bytes()
            err_bytes = err_path.read_bytes()
    except OSError as exc:
        return f"FALHOU: {exc}"
    output = out_bytes.decode("utf-8", errors="replace")
    if err_bytes:
        output += ("\n[stderr]\n" if output else "[stderr]\n") + err_bytes.decode("utf-8", errors="replace")
    output = output.strip() or "(sem saída)"
    text = f"`{command}`\n```\n{output[-1700:]}\n```"
    if timed_out:
        text += f"\n(timeout apos {timeout}s — processo pode ter ficado em segundo plano)"
    elif returncode not in (0, None):
        text += f"\n(exit {returncode})"
    return text


HELP_CONFIG = (
    "**Config**\n"
    "`!bind <#canal> <nome-ou-id>` â€” vincula um canal a um PC\n"
    "`!unbind <#canal>` â€” remove o vÃ­nculo\n"
    "`!broadcast <#canal>` â€” define o canal que controla todos os PCs\n"
    "`!broadcast` â€” mostra o canal geral atual\n"
    "`!config` â€” mostra a config salva\n"
    "`!help` â€” esta ajuda"
)

HELP_PC = (
    "**Comandos**\n"
    "`!status` — CPU, RAM, disco, uptime\n"
    "`!ping` — testa resposta\n"
    "`!desligar` / `!reiniciar` / `!dormir` — energia (5s de aviso no Windows)\n"
    "`!screenshot` — captura a tela\n"
    "`!auto <segundos>` — auto-screenshot a cada N segundos (mín 5s)\n"
    "`!auto` — mostra o estado; `!auto off` ou `!stop` — para\n"
    "`!comando [timeout=60] <comando>` — executa shell (padrão 30s, máx 120)\n"
    "texto puro em canal vinculado (`ipconfig`) — vira comando shell (sem broadcast)\n"
    "`cd <pasta>` — muda a pasta do terminal virtual e memoriza\n"
    "`!cwd` / `!cwd reset` — mostra ou reseta a pasta atual\n"
    "`!msg <texto>` — mostra um popup na tela do PC\n"
    "`!dispositivos` — responde com nome e id\n"
    "`!menu` — lista os comandos internos do bot\n"
    "`!help` — esta ajuda"
)


def no_window_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if platform.system() == "Windows" else 0


def clean_content(raw) -> str:
    text = raw or ""
    for ch in ("\u2060", "\u200b", "\u200c", "\u200d", "\ufeff", "\u00ad"):
        text = text.replace(ch, "")
    return text.strip()


def is_authorized(msg: dict) -> bool:
    author = msg.get("author", {})
    if str(author.get("bot", "")).lower() == "true":
        return False
    return str(author.get("id", "")) in CFG["authorized_user_ids"]


def parse_mention(arg: str):
    m = MENTION_RE.fullmatch(arg.strip())
    return m.group(1) if m else (arg.strip() if arg.strip().isdigit() else None)


def process_config_message(msg: dict, channel_id: str, silent: bool):
    if not is_authorized(msg):
        return
    content = clean_content(msg.get("content"))
    parts = content.split()
    if not parts or not parts[0].startswith("!"):
        return
    cmd = parts[0].lower()
    args = parts[1:]

    def confirm(text):
        if not silent:
            reply(channel_id, text)

    if cmd == "!bind" and len(args) >= 2:
        target_cid = parse_mention(args[0])
        name = " ".join(args[1:]).strip().lower()
        if not target_cid or not name:
            confirm("uso: `!bind <#canal> <nome-ou-id>`")
            return
        STATE["binds"][target_cid] = name
        positions = STATE["positions"].setdefault("channels", {})
        if target_cid not in positions:
            latest = latest_message_id(target_cid)
            if latest is not None:
                positions[target_cid] = latest
        save_state()
        if name in my_keys():
            confirm(f"vÃ­nculo ok: este PC escuta <#{target_cid}> como `{name}`")
        else:
            confirm(f"<#{target_cid}> â†’ `{name}` registrado")
        return

    if cmd == "!unbind" and len(args) >= 1:
        target_cid = parse_mention(args[0])
        if not target_cid:
            confirm("uso: `!unbind <#canal>`")
            return
        removed = STATE["binds"].pop(target_cid, None)
        STATE["positions"].get("channels", {}).pop(target_cid, None)
        save_state()
        confirm(f"removido <#{target_cid}> (era `{removed}`)" if removed else f"<#{target_cid}> nÃ£o estava vinculado")
        return

    if cmd == "!broadcast":
        if not args:
            current = STATE["broadcast"]
            confirm(f"canal geral atual: <#{current}>" if current else "nenhum canal geral configurado")
            return
        target_cid = parse_mention(args[0])
        if not target_cid:
            confirm("uso: `!broadcast <#canal>`")
            return
        STATE["broadcast"] = target_cid
        positions = STATE["positions"].setdefault("channels", {})
        if target_cid not in positions:
            latest = latest_message_id(target_cid)
            if latest is not None:
                positions[target_cid] = latest
        save_state()
        confirm(f"canal geral â†’ <#{target_cid}>")
        return

    if cmd == "!config":
        binds = ", ".join(f"<#{cid}>â†’`{name}`" for cid, name in sorted(STATE["binds"].items())) or "vazio"
        broadcast = f"<#{STATE['broadcast']}>" if STATE["broadcast"] else "nÃ£o definido"
        confirm(f"broadcast: {broadcast} | vÃ­nculos ({len(STATE['binds'])}): {binds}")
        return

    if cmd == "!help":
        confirm(HELP_CONFIG)
        return

    confirm("comando desconhecido. `!help`")


def process_channel_message(msg: dict, channel_id: str):
    if not is_authorized(msg):
        return
    content = clean_content(msg.get("content"))
    parts = content.split()
    if not parts:
        return
    if not parts[0].startswith("!"):
        if channel_id in my_bound_channels():
            if is_risky_raw_command(content):
                reply(channel_id, "por seguranca, esse comando precisa ser explicito: use `!comando " + content + "`")
            else:
                reply(channel_id, run_shell_with_cwd(content, 30))
        return
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd == "!status":
        reply(channel_id, cmd_status())
    elif cmd == "!ping":
        reply(channel_id, "pong")
    elif cmd == "!desligar":
        reply(channel_id, run_power("desligar"))
    elif cmd == "!reiniciar":
        reply(channel_id, run_power("reiniciar"))
    elif cmd == "!dormir":
        reply(channel_id, run_power("dormir"))
    elif cmd == "!screenshot":
        text, file = cmd_screenshot()
        reply(channel_id, text, file=file)
    elif cmd == "!comando":
        timeout = 30
        if args and args[0].lower().startswith("timeout="):
            try:
                timeout = int(args[0].split("=", 1)[1])
                args = args[1:]
            except ValueError:
                pass
        if not args:
            reply(channel_id, "uso: `!comando [timeout=60] <comando>`")
            return
        reply(channel_id, run_shell_with_cwd(" ".join(args), timeout))
    elif cmd == "!auto":
        auto = STATE.setdefault("auto", {"enabled": False, "interval": 30})
        if not args:
            estado = f"ativo, a cada {auto['interval']}s" if auto.get("enabled") else "desativado"
            destino = auto_target_channel()
            msg_out = f"auto-screenshot: {estado}"
            if destino:
                msg_out += f" -> <#{destino}>"
            else:
                msg_out += " (nenhum canal vinculado)"
            reply(channel_id, msg_out)
            return
        if args[0].lower() == "off":
            STATE["auto"]["enabled"] = False
            save_state()
            reply(channel_id, "auto-screenshot parado")
            return
        try:
            interval = int(args[0])
        except ValueError:
            reply(channel_id, "uso: `!auto 10`, `!auto off` ou `!auto`")
            return
        avisado = interval < MIN_AUTO_INTERVAL
        if avisado:
            interval = MIN_AUTO_INTERVAL
        STATE["auto"] = {"enabled": True, "interval": interval}
        AUTO_LAST[0] = 0.0
        save_state()
        msg_out = f"auto-screenshot ativo: a cada {interval}s"
        if avisado:
            msg_out += f" (minimo de {MIN_AUTO_INTERVAL}s — abaixo disso o Discord bane o bot)"
        reply(channel_id, msg_out)
        return
    elif cmd == "!stop":
        STATE.setdefault("auto", {"enabled": False, "interval": 30})["enabled"] = False
        save_state()
        reply(channel_id, "auto-screenshot parado")
        return
    elif cmd == "!msg":
        raw = content.split(None, 1)
        reply(channel_id, cmd_message(raw[1] if len(raw) > 1 else ""))
    elif cmd == "!dispositivos":
        reply(channel_id, f"online â€” id `{DEVICE_ID}`")
    elif cmd == "!cwd":
        if args and args[0].lower() == "reset":
            STATE["shell_cwd"] = str(Path.home())
            save_state()
            reply(channel_id, f"cwd: `{STATE['shell_cwd']}`")
            return
        reply(channel_id, f"cwd: `{STATE.get('shell_cwd') or str(Path.home())}`")
    elif cmd == "!menu":
        reply(channel_id, HELP_PC)
    elif cmd == "!help":
        reply(channel_id, HELP_PC)
    else:
        reply(channel_id, f"comando desconhecido: `{cmd}`. `!help`")


def poll_channel(channel_id: str, after_id: str, handler):
    messages = fetch_messages(channel_id, after_id)
    messages.sort(key=lambda m: int(m["id"]))
    for msg in messages:
        handler(msg)
    if messages:
        return str(max(int(m["id"]) for m in messages))
    return after_id


def replay_config_silent():
    messages = fetch_messages(CONFIG_CHANNEL, None)
    messages.sort(key=lambda m: int(m["id"]))
    for msg in messages:
        process_config_message(msg, CONFIG_CHANNEL, silent=True)
    latest = max((int(m["id"]) for m in messages), default=None)
    if latest is not None:
        STATE["positions"]["config"] = str(latest)
        save_state()


def ensure_positions():
    positions = STATE["positions"].setdefault("channels", {})
    for cid in command_channels():
        if cid not in positions:
            latest = latest_message_id(cid)
            if latest is not None:
                positions[cid] = latest
    save_state()


def start_gateway_keepalive():
    try:
        import discord
    except ImportError:
        log.warning("discord.py nao instalado; bot aparecera offline na lista de membros")
        return
    intents = discord.Intents.none()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        log.info("gateway conectado como %s", client.user)

    def runner():
        try:
            client.run(CFG["token"], log_handler=None)
        except Exception:
            log.exception("gateway caiu")

    threading.Thread(target=runner, daemon=True, name="gateway").start()


def message_box(text: str, error: bool = False) -> None:
    if platform.system() != "Windows":
        print(text)
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "DevAgent", 0x10 if error else 0x40)
    except Exception:
        print(text)


def is_admin_windows() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_elevated_windows(flag: str) -> int:
    import ctypes
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, flag, None, 1)
    return 0 if int(rc) > 32 else 1


def copy_self(target: Path) -> None:
    source = Path(sys.executable)
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    elif not target.exists():
        source.replace(target)


def install_self() -> int:
    if platform.system() == "Windows":
        if not is_admin_windows():
            return relaunch_elevated_windows("--install")
        dest_dir = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "DevAgent"
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            message_box(f"Falha ao criar {dest_dir}: {exc}", error=True)
            return 1
        target = dest_dir / "DevAgent.exe"
        try:
            copy_self(target)
        except OSError as exc:
            message_box(f"Falha ao copiar o agente: {exc}", error=True)
            return 1
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"Software\Microsoft\Windows\CurrentVersion\Run",
                                 0, winreg.KEY_SET_VALUE)
            with key:
                winreg.SetValueEx(key, "DevAgent", 0, winreg.REG_SZ, f'"{target}"')
        except OSError as exc:
            message_box(f"Falha ao registrar autostart: {exc}", error=True)
            return 1
        subprocess.Popen([str(target)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=no_window_flags())
        message_box(f"DevAgent instalado e iniciado.\nAutostart de maquina ativo (HKLM Run).\nSomente administrador remove.")
        return 0
    import getpass as gp
    user = os.environ.get("SUDO_USER") or gp.getuser()
    home = os.path.expanduser(f"~{user}")
    unit = Path("/etc/systemd/system/devagent.service")
    if os.geteuid() != 0:
        print("Instalacao de sistema requer root. Use: sudo ./DevAgent --install")
        return 1
    try:
        Path("/opt/devagent").mkdir(parents=True, exist_ok=True)
        target = Path("/opt/devagent/DevAgent")
        copy_self(target)
        target.chmod(0o755)
        unit.write_text(
            "[Unit]\n"
            "Description=DevAgent - Gerenciamento remoto de dispositivos\n"
            "After=network-online.target graphical.target\n"
            "Wants=network-online.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"User={user}\n"
            f"ExecStart={target}\n"
            "Environment=DISPLAY=:0\n"
            f"Environment=XAUTHORITY={home}/.Xauthority\n"
            "Restart=always\n"
            "RestartSec=5\n\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n",
            encoding="utf-8",
        )
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "--now", "devagent.service"], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Falha na instalacao: {exc}")
        return 1
    print("DevAgent instalado e iniciado como servico de sistema (somente root desabilita).")
    return 0


def uninstall_self() -> int:
    if platform.system() == "Windows":
        if not is_admin_windows():
            return relaunch_elevated_windows("--uninstall")
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"Software\Microsoft\Windows\CurrentVersion\Run",
                                 0, winreg.KEY_SET_VALUE)
            with key:
                try:
                    winreg.DeleteValue(key, "DevAgent")
                except OSError:
                    pass
        except OSError as exc:
            message_box(f"Falha ao remover autostart: {exc}", error=True)
            return 1
        subprocess.run(["taskkill", "/IM", "DevAgent.exe", "/F"], capture_output=True,
                       creationflags=no_window_flags())
        shutil.rmtree(Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "DevAgent",
                      ignore_errors=True)
        message_box("DevAgent removido (autostart e arquivos de instalacao).")
        return 0
    if os.geteuid() != 0:
        print("Remocao requer root. Use: sudo ./DevAgent --uninstall")
        return 1
    subprocess.run(["systemctl", "disable", "--now", "devagent.service"], capture_output=True)
    Path("/etc/systemd/system/devagent.service").unlink(missing_ok=True)
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    shutil.rmtree(Path("/opt/devagent"), ignore_errors=True)
    print("DevAgent removido.")
    return 0


def is_installed() -> bool:
    if platform.system() == "Windows":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"Software\Microsoft\Windows\CurrentVersion\Run")
            with key:
                value, _ = winreg.QueryValueEx(key, "DevAgent")
            return Path(value.strip('"')).exists()
        except OSError:
            return False
    return (Path("/etc/systemd/system/devagent.service").exists()
            and Path("/opt/devagent/DevAgent").exists())


def main():
    log.info("agente iniciado: device_id=%s name=%s", DEVICE_ID, DEVICE_NAME)
    general = CFG.get("general_channel_id")
    if general and not STATE.get("broadcast"):
        STATE["broadcast"] = str(general)
        save_state()
    positions = STATE["positions"]
    if not positions.get("config"):
        log.info("primeira execuÃ§Ã£o: replicando histÃ³rico de config silenciosamente")
        replay_config_silent()
        if not positions.get("config"):
            positions["config"] = "0"
            save_state()
    ensure_positions()
    start_gateway_keepalive()
    reply(CONFIG_CHANNEL, f"online — id `{DEVICE_ID}`")
    startup_announce()

    last_signature = None
    while True:
        try:
            positions = STATE["positions"]
            positions["config"] = poll_channel(
                CONFIG_CHANNEL, positions.get("config") or "0",
                lambda m: process_config_message(m, CONFIG_CHANNEL, silent=False),
            )
            positions.setdefault("channels", {})
            for cid in command_channels():
                before = positions["channels"].get(cid)
                if before is None:
                    before = latest_message_id(cid)
                    if before is None:
                        continue
                    positions["channels"][cid] = before
                new_pos = poll_channel(cid, before, lambda m, c=cid: process_channel_message(m, c))
                if new_pos is not None:
                    positions["channels"][cid] = new_pos
            positions["channels"] = {
                cid: pos for cid, pos in positions["channels"].items() if cid in command_channels()
            }
            signature = json.dumps([positions.get("config"), positions.get("channels"),
                                    STATE["binds"], STATE["broadcast"],
                                    STATE.get("auto")], sort_keys=True)
            if signature != last_signature:
                save_state()
                last_signature = signature
            maybe_auto_screenshot()
        except Exception:
            log.exception("erro no loop principal")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    if "--install" in sys.argv:
        sys.exit(install_self())
    if "--uninstall" in sys.argv:
        sys.exit(uninstall_self())
    if getattr(sys, "frozen", False) and not is_installed():
        sys.exit(install_self())
    main()
