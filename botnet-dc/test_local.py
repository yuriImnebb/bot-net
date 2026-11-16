import json
import sys
from pathlib import Path

BASE = Path(__file__).parent
DUMMY_CFG = BASE / "agent_config.json"
ARTIFACTS = [DUMMY_CFG, BASE / "device_id.json", BASE / "agent_state.json", BASE / "agent.log"]
for path in ARTIFACTS:
    try:
        path.unlink()
    except OSError:
        pass

DUMMY_CFG.write_text(json.dumps({
    "token": "dummy-token",
    "config_channel_id": "900",
    "authorized_user_ids": ["111"],
    "device_name": "pc-de-teste",
    "poll_interval": 3,
}, ensure_ascii=False), encoding="utf-8")

try:
    import agent

    status = agent.cmd_status()
    assert "STATUS" in status and "pc-de-teste" in status and "Uptime" in status
    print("[ok] cmd_status")

    out = agent.cmd_shell("echo devtest-ok", 10)
    assert "devtest-ok" in out
    out = agent.cmd_shell("exit 3" if sys.platform == "win32" else "exit 3", 10)
    assert "exit 3" in out
    print("[ok] cmd_shell (stdout e exit code)")

    my_id = agent.DEVICE_ID
    sent = []
    agent.reply = lambda channel_id, text, file=None: sent.append((channel_id, text, file))
    agent.latest_message_id = lambda cid: "5000"

    def msg(content, author="111", bot=False):
        return {"id": "6000", "author": {"id": author, "bot": bot}, "content": content}

    assert agent.is_authorized(msg("!status", author="111"))
    assert not agent.is_authorized(msg("!status", author="999"))
    assert not agent.is_authorized(msg("!status", author="111", bot=True))
    print("[ok] is_authorized")

    assert agent.parse_mention("<#123>") == "123"
    assert agent.parse_mention("123") == "123"
    assert agent.parse_mention("abc") is None
    print("[ok] parse_mention")

    agent.process_config_message(msg("!bind <#1001> pc-de-teste"), "900", silent=False)
    assert agent.STATE["binds"]["1001"] == "pc-de-teste"
    assert "1001" in agent.STATE["positions"]["channels"]
    agent.process_config_message(msg("!broadcast <#1002>"), "900", silent=False)
    assert agent.STATE["broadcast"] == "1002"
    assert agent.command_channels() == {"1001", "1002"}
    print("[ok] !bind e !broadcast gravam estado")

    agent.process_channel_message(msg("!status"), "1001")
    assert sent[-1][0] == "1001" and "CPU" in sent[-1][1]
    agent.process_channel_message(msg("!ping"), "1002")
    assert sent[-1][0] == "1002" and sent[-1][1].endswith("pong")
    agent.process_channel_message(msg("!comando timeout=10 echo cmd-ok"), "1001")
    assert "cmd-ok" in sent[-1][1]
    agent.process_channel_message(msg("!comando echo arg1 arg2"), "1001")
    assert "arg1 arg2" in sent[-1][1]
    agent.process_channel_message(msg("!comando"), "1001")
    assert "uso" in sent[-1][1]
    agent.process_channel_message(msg("!auto"), "1001")
    assert "desativado" in sent[-1][1]
    agent.process_channel_message(msg("!auto 30"), "1001")
    assert agent.STATE["auto"] == {"enabled": True, "interval": 30}
    agent.process_channel_message(msg("!auto 2"), "1001")
    assert agent.STATE["auto"]["interval"] == 5 and "minimo" in sent[-1][1]
    agent.process_channel_message(msg("!auto abc"), "1001")
    assert "uso" in sent[-1][1]
    agent.process_channel_message(msg("!auto off"), "1001")
    assert agent.STATE["auto"]["enabled"] is False
    agent.process_channel_message(msg("!auto 10"), "1001")
    agent.process_channel_message(msg("!stop"), "1001")
    assert agent.STATE["auto"]["enabled"] is False
    agent.cmd_screenshot = lambda: ("screenshot:", ("s.jpg", b"\xff\xd8\xfffake", "image/jpeg"))
    agent.AUTO_LAST[0] = 0.0
    agent.STATE["auto"] = {"enabled": True, "interval": 5}
    before = len(sent)
    agent.maybe_auto_screenshot()
    assert len(sent) == before + 1 and sent[-1][0] == "1001" and "auto-screenshot" in sent[-1][1]
    agent.maybe_auto_screenshot()
    assert len(sent) == before + 1
    agent.STATE["auto"]["enabled"] = False

    called = []
    real_popen = agent.subprocess.Popen
    agent.subprocess.Popen = lambda *a, **k: called.append((a, k)) or None
    assert "uso" in agent.cmd_message("", system_name="Windows")
    out = agent.cmd_message("opa, teste", system_name="Windows")
    assert "enviado" in out and called
    import base64
    script = base64.b64decode(called[-1][0][0][-1]).decode("utf-16-le")
    assert called[-1][0][0][0] == "powershell.exe" and "-EncodedCommand" in called[-1][0][0]
    assert "opa, teste" in script and "DevAgent" in script
    agent.cmd_message("'; Remove-Item C:\\; '", system_name="Windows")
    script = base64.b64decode(called[-1][0][0][-1]).decode("utf-16-le")
    assert "''" in script
    agent.cmd_message("oi", system_name="Linux")
    assert called[-1][0][0][0] in ("zenity", "xmessage", "notify-send")
    agent.process_channel_message(msg("!msg opa, tudo bem"), "1001")
    assert called and called[-1][0][0][0] == "powershell.exe"
    agent.subprocess.Popen = real_popen
    print("[ok] cmd_message: uso, windows (encoded command + escape), linux, branch !msg")

    agent.process_channel_message(msg("!dispositivos"), "1001")
    assert my_id in sent[-1][1]
    agent.process_channel_message(msg("!comando !bind #x y"), "1001")  # não executa config como comando
    assert sent[-1][0] == "1001"
    print("[ok] auto-screenshot: enable/clamp/stop/persistência")

    agent.process_channel_message(msg("!status", author="999"), "1001")
    assert len(sent) == 6 or not sent[-1][1].startswith("[pc-de-teste] **STATUS")
    agent.process_config_message(msg("!unbind <#1001>"), "900", silent=False)
    assert "1001" not in agent.STATE["binds"]
    assert agent.command_channels() == {"1002"}
    print("[ok] !unbind remove vínculo")

    assert agent.STATE["positions"]["channels"].get("1002") == "5000"
    agent.save_state()
    reloaded = agent.load_state()
    assert reloaded["binds"] == {} and reloaded["broadcast"] == "1002"
    print("[ok] estado persistido e recarregado")

    print("\nTODOS OS TESTES PASSARAM")
finally:
    for path in ARTIFACTS:
        try:
            path.unlink()
        except OSError:
            pass
