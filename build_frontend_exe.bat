@echo off
setlocal

set "ROOT=%~dp0"
set "FRONTEND=%ROOT%frontend"

if not exist "%FRONTEND%\package.json" (
  echo ERRO: Nao achei %FRONTEND%\package.json
  pause
  exit /b 1
)

cd /d "%FRONTEND%" || exit /b 1

echo ===============================
echo Build do EXE do FRONTEND
echo Pasta: %CD%
echo ===============================

REM Instala deps se necessario
if not exist "node_modules" (
  echo node_modules nao encontrado. Rodando npm install...
  call npm install
  if errorlevel 1 (
    echo ERRO: npm install falhou.
    pause
    exit /b 1
  )
) else (
  if not exist "node_modules\electron-builder" (
    echo electron-builder nao encontrado em node_modules. Rodando npm install...
    call npm install
    if errorlevel 1 (
      echo ERRO: npm install falhou.
      pause
      exit /b 1
    )
  )
)

REM Remove builds anteriores para nao acumular lixo nem confundir qual .exe e o novo
if exist "release" (
  echo Limpando builds anteriores...
  rmdir /s /q "release"
)

echo.
echo Gerando instalador (.exe) com electron-builder...
call npm run dist
if errorlevel 1 (
  echo ERRO: npm run dist falhou.
  pause
  exit /b 1
)

REM Localiza o instalador gerado (arquivo unico .exe dentro de release)
set "INSTALLER="
for %%F in ("release\*.exe") do set "INSTALLER=%%~nxF"

REM Copia com nome fixo (sem versao), pronto pra subir na pasta de downloads da landing page
if defined INSTALLER (
  copy /y "release\%INSTALLER%" "release\OpenDub-Setup.exe" >nul
)

echo.
echo ===============================
echo Concluido!
if defined INSTALLER (
  echo Instalador gerado: %INSTALLER%
  echo Copia com nome fixo: OpenDub-Setup.exe ^(essa e a que vai na pasta de downloads da landing page^)
) else (
  echo AVISO: nao encontrei o .exe gerado em %FRONTEND%\release
)
echo Pasta: %FRONTEND%\release
echo ===============================

start "" explorer.exe "%FRONTEND%\release"

pause
