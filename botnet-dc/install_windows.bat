@echo off
cd /d "%~dp0"
if not exist "%~dp0DevAgent.exe" (
  echo DevAgent.exe nao encontrado nesta pasta. Rode build_agent.bat primeiro.
  pause
  exit /b 1
)
net session >nul 2>&1
if errorlevel 1 (
  echo Este instalador precisa de permissao de administrador.
  echo Clique com o botao direito neste arquivo e escolha "Executar como administrador".
  pause
  exit /b 1
)
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v DevAgent /f >nul 2>&1
reg add "HKLM\Software\Microsoft\Windows\CurrentVersion\Run" /v DevAgent /t REG_SZ /d "\"%~dp0DevAgent.exe\"" /f
if errorlevel 1 (
  echo Falha ao registrar no Run do Windows ^(HKLM^).
  pause
  exit /b 1
)
start "" "%~dp0DevAgent.exe"
echo Agente instalado com autostart de maquina ^(HKLM Run^) e iniciado.
echo Sem icone na bandeja; roda em segundo plano.
echo Para remover ^(admin^): reg delete "HKLM\Software\Microsoft\Windows\CurrentVersion\Run" /v DevAgent /f
pause
