@echo off
cd /d "%~dp0"
python -m pip install pyinstaller
if exist .env (set "EMBED=--add-data .env;.") else set "EMBED="
if exist agent_config.json (set "EMBED=%EMBED% --add-data agent_config.json;.")
if not exist .env if not exist agent_config.json (
  echo AVISO: nem .env nem agent_config.json encontrados — o .exe final nao tera configuracao.
)
python -m PyInstaller --onefile --noconsole --name DevAgent %EMBED% agent.py
echo.
echo Build pronto em dist\DevAgent.exe
echo Duplo clique no .exe = instala e inicia sozinho (pede admin/UAC).
echo REMOVER: DevAgent.exe --uninstall
echo ATENCAO: o .exe EMBUTE sua configuracao (inclui o token do bot). Trate-o como segredo.
pause
