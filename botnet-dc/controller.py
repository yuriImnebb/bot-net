import asyncio
import json
import time
import uuid
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from protocol_common import make_message, parse_message, verify_payload

BASE_DIR = Path(__file__).parent
DEVICES_PATH = BASE_DIR / "devices.json"


def load_env(path: Path) -> dict:
    env = {}
    if not path.exists():
        raise SystemExit(f".env não encontrado em {path} — copie .env.example para .env e preencha")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


ENV = load_env(BASE_DIR / ".env")
TOKEN = ENV["CONTROLLER_TOKEN"]
AGENT_BOT_ID = int(ENV["AGENT_BOT_ID"])
CMD_CHANNEL_ID = int(ENV["CMD_CHANNEL_ID"])
RESULTS_CHANNEL_ID = int(ENV["RESULTS_CHANNEL_ID"])
SECRET = ENV["SHARED_SECRET"]
ALLOWED_USERS = {int(x) for x in ENV.get("AUTHORIZED_USER_IDS", "").replace(";", ",").split(",") if x.strip()}
GUILD_ID = ENV.get("GUILD_ID")
if ENV.get("ALLOW_ALL_USERS", "").lower() not in ("1", "true", "yes") and not ALLOWED_USERS:
    raise SystemExit("AUTHORIZED_USER_IDS vazio. Preencha no .env ou defina ALLOW_ALL_USERS=true (não recomendado)")

BROADCAST_TIMEOUT = 35
SINGLE_TIMEOUT = 45


def load_devices() -> dict:
    try:
        return json.loads(DEVICES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


DEVICES = load_devices()


def save_devices():
    try:
        DEVICES_PATH.write_text(json.dumps(DEVICES, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"aviso: não consegui salvar devices.json: {exc}")


def register_device(data: dict):
    dev_id = str(data.get("device", ""))
    if not dev_id:
        return
    entry = DEVICES.get(dev_id, {})
    entry.update({
        "name": data.get("name") or entry.get("name") or dev_id,
        "os": data.get("os") or entry.get("os") or "?",
        "last_seen": int(time.time()),
    })
    entry.setdefault("first_seen", int(time.time()))
    DEVICES[dev_id] = entry
    save_devices()


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
GUILD = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None

PENDING: dict[str, asyncio.Queue] = {}


async def device_autocomplete(interaction: discord.Interaction, current: str):
    choices = [app_commands.Choice(name="todos (broadcast)", value="todos")]
    for dev_id, info in sorted(DEVICES.items(), key=lambda kv: kv[1].get("name", "")):
        label = f"{info.get('name', dev_id)} ({dev_id})"
        choices.append(app_commands.Choice(name=label[:100], value=dev_id))
    current = current.lower()
    return [c for c in choices if current in c.name.lower() or current in c.value.lower()][:25]


def is_allowed(interaction: discord.Interaction) -> bool:
    return not ALLOWED_USERS or interaction.user.id in ALLOWED_USERS


def fmt_disks(disks) -> str:
    return ", ".join(f"{d['mount']} {d['used_percent']}% de {d['total_gb']}GB" for d in disks) or "?"


def fmt_result(data: dict) -> str:
    name = data.get("name") or data.get("device", "?")
    ok = data.get("ok")
    action = data.get("action", "?")
    head = f"**{name}** `[{action}]` {'OK' if ok else 'FALHOU'}"
    if action == "status" and ok:
        d = data.get("data", {})
        return (
            f"{head}\n"
            f"  SO: {d.get('os')}\n"
            f"  CPU: {d.get('cpu_percent')}% | RAM: {d.get('memory_percent')}% "
            f"({d.get('memory_used_gb')}/{d.get('memory_total_gb')} GB)\n"
            f"  Disco: {fmt_disks(d.get('disks', []))}\n"
            f"  Uptime: {d.get('uptime')}"
        )
    if ok:
        detail = data.get("output") or json.dumps(data.get("data", {}), ensure_ascii=False)
        detail = str(detail)
        if len(detail) > 900:
            detail = detail[:900] + "…"
        if data.get("exit_code") is not None:
            detail += f"\n(exit {data['exit_code']})"
        return f"{head}\n```\n{detail}\n```"
    return f"{head}\n```\n{data.get('error', 'erro')}\n```"


async def send_command(target: str, action: str, args: dict | None = None) -> str:
    command_id = uuid.uuid4().hex
    payload = {
        "type": "cmd",
        "id": command_id,
        "target": "all" if target == "todos" else target,
        "action": action,
        "args": args or {},
        "ts": int(time.time()),
    }
    channel = bot.get_channel(CMD_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(CMD_CHANNEL_ID)
    PENDING[command_id] = asyncio.Queue()
    try:
        await channel.send(make_message(payload, SECRET))
    except Exception:
        PENDING.pop(command_id, None)
        raise
    return command_id


async def run_action(interaction: discord.Interaction, action: str, dispositivo: str, args: dict | None = None):
    if not is_allowed(interaction):
        await interaction.response.send_message("Você não tem permissão para usar este bot.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)

    command_id = await send_command(dispositivo, action, args)
    queue = PENDING[command_id]

    if dispositivo != "todos":
        try:
            data, attachments = await asyncio.wait_for(queue.get(), timeout=SINGLE_TIMEOUT)
        except asyncio.TimeoutError:
            await interaction.followup.send(f"Sem resposta de `{dispositivo}` em {SINGLE_TIMEOUT}s. O agente está online?")
            return
        finally:
            PENDING.pop(command_id, None)
        files = [await a.to_file() for a in attachments] if attachments else []
        await interaction.followup.send(fmt_result(data), files=files)
        return

    known = set(DEVICES.keys())
    collected: list[dict] = []
    all_attachments = []
    deadline = time.monotonic() + BROADCAST_TIMEOUT
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            data, attachments = await asyncio.wait_for(queue.get(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        collected.append(data)
        all_attachments.extend(attachments)
        responded_ids = {str(d.get("device")) for d in collected}
        if known and known <= responded_ids:
            break
    PENDING.pop(command_id, None)

    responded_ids = {str(d.get("device")) for d in collected}
    lines = [f"Broadcast `[{action}]` — {len(collected)}/{len(known) or '?'} dispositivos responderam"]
    for data in sorted(collected, key=lambda d: str(d.get("name", ""))):
        block = fmt_result(data)
        if len(block) > 550:
            block = block[:550] + "…\n```"
        lines.append(block)
    missing = known - responded_ids
    if missing:
        missing_names = ", ".join(DEVICES[m].get("name", m) for m in sorted(missing))
        lines.append(f"Sem resposta: {missing_names}")
    content = "\n".join(lines)
    if len(content) > 1950:
        content = content[:1900] + "\n… (truncado)"
    files = [await a.to_file() for a in all_attachments[:10]] if all_attachments else []
    await interaction.followup.send(content, files=files)


@bot.tree.command(name="dispositivos", description="Lista os dispositivos conhecidos e quando foram vistos por último", guild=GUILD)
async def dispositivos(interaction: discord.Interaction):
    if not is_allowed(interaction):
        await interaction.response.send_message("Você não tem permissão para usar este bot.", ephemeral=True)
        return
    if not DEVICES:
        await interaction.response.send_message("Nenhum dispositivo registrado ainda. Inicie o agente em uma máquina.", ephemeral=True)
        return
    now = time.time()
    lines = ["**Dispositivos registrados**"]
    for dev_id, info in sorted(DEVICES.items(), key=lambda kv: kv[1].get("name", "")):
        seen = int(now - info.get("last_seen", 0))
        lines.append(f"• `{info.get('name', '?')}` (`{dev_id}`) — {info.get('os', '?')} — visto há {seen}s")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="status", description="CPU, RAM, disco e uptime do dispositivo", guild=GUILD)
@app_commands.describe(dispositivo="ID do dispositivo ou 'todos'")
@app_commands.autocomplete(dispositivo=device_autocomplete)
async def status_cmd(interaction: discord.Interaction, dispositivo: str):
    await run_action(interaction, "status", dispositivo)


@bot.tree.command(name="desligar", description="Desliga o dispositivo (5s de aviso no Windows)", guild=GUILD)
@app_commands.describe(dispositivo="ID do dispositivo ou 'todos'")
@app_commands.autocomplete(dispositivo=device_autocomplete)
async def desligar_cmd(interaction: discord.Interaction, dispositivo: str):
    await run_action(interaction, "shutdown", dispositivo)


@bot.tree.command(name="reiniciar", description="Reinicia o dispositivo (5s de aviso no Windows)", guild=GUILD)
@app_commands.describe(dispositivo="ID do dispositivo ou 'todos'")
@app_commands.autocomplete(dispositivo=device_autocomplete)
async def reiniciar_cmd(interaction: discord.Interaction, dispositivo: str):
    await run_action(interaction, "restart", dispositivo)


@bot.tree.command(name="dormir", description="Coloca o dispositivo em suspensão", guild=GUILD)
@app_commands.describe(dispositivo="ID do dispositivo ou 'todos'")
@app_commands.autocomplete(dispositivo=device_autocomplete)
async def dormir_cmd(interaction: discord.Interaction, dispositivo: str):
    await run_action(interaction, "sleep", dispositivo)


@bot.tree.command(name="screenshot", description="Captura a tela do dispositivo", guild=GUILD)
@app_commands.describe(dispositivo="ID do dispositivo ou 'todos'")
@app_commands.autocomplete(dispositivo=device_autocomplete)
async def screenshot_cmd(interaction: discord.Interaction, dispositivo: str):
    await run_action(interaction, "screenshot", dispositivo)


@bot.tree.command(name="comando", description="Executa um comando de terminal no dispositivo", guild=GUILD)
@app_commands.describe(dispositivo="ID do dispositivo ou 'todos'", cmd="Comando shell a executar", timeout="Timeout em segundos (máx 120)")
@app_commands.autocomplete(dispositivo=device_autocomplete)
async def comando_cmd(interaction: discord.Interaction, dispositivo: str, cmd: str, timeout: app_commands.Range[int, 1, 120] = 30):
    await run_action(interaction, "shell", dispositivo, {"command": cmd, "timeout": timeout})


@bot.tree.command(name="ping", description="Testa se o agente responde", guild=GUILD)
@app_commands.describe(dispositivo="ID do dispositivo ou 'todos'")
@app_commands.autocomplete(dispositivo=device_autocomplete)
async def ping_cmd(interaction: discord.Interaction, dispositivo: str):
    await run_action(interaction, "ping", dispositivo)


@bot.event
async def on_message(message: discord.Message):
    if message.channel.id != RESULTS_CHANNEL_ID:
        return
    if message.author.id != AGENT_BOT_ID:
        return
    data = parse_message(message.content or "")
    if not data or not verify_payload(data, SECRET):
        return
    dtype = data.get("type")
    if dtype == "hello":
        register_device(data)
        return
    if dtype == "result":
        register_device(data)
        command_id = str(data.get("id", ""))
        queue = PENDING.get(command_id)
        if queue is not None:
            await queue.put((data, message.attachments))


@bot.event
async def on_ready():
    print(f"Conectado como {bot.user} (id {bot.user.id})")
    try:
        if GUILD is not None:
            synced = await bot.tree.sync(guild=GUILD)
            print(f"{len(synced)} slash commands sincronizados no servidor {GUILD_ID}")
        else:
            synced = await bot.tree.sync()
            print(f"{len(synced)} slash commands sincronizados globalmente (podem demorar até 1h)")
    except discord.HTTPException as exc:
        print(f"erro ao sincronizar comandos: {exc}")


if __name__ == "__main__":
    bot.run(TOKEN)
