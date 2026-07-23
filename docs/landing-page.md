# Landing page

A landing page (`landing/`) e um site estatico separado do aplicativo — ela nao vai
dentro do `.exe`. Apresenta o projeto (print real da interface, demo animado, texto
"gratis e codigo aberto") e serve o instalador pra download.

## Estrutura

```text
landing/
  Dockerfile
  docker-compose.yml   # sobe nginx na porta 5500
  nginx.conf
  site/                # html/css/js estaticos, sem build step
  downloads/           # pasta com bind mount; e aqui que entra o .exe
```

## Rodar local

```powershell
cd landing
docker compose up -d --build
```

Abre em `http://localhost:5500`.

## Botao de download

O botao aponta pra `downloads/OpenDub-Setup.exe` (nome fixo, sem numero de versao).
`build_frontend_exe.bat` ja gera essa copia com nome fixo automaticamente em
`frontend/release/OpenDub-Setup.exe` junto do instalador versionado — e esse arquivo
que deve ser copiado pra `landing/downloads/` a cada nova versao.

Como `downloads/` e um volume (bind mount pro host), basta substituir o arquivo la e o
nginx serve a versao nova na hora — nao precisa rebuildar o container.

## Deploy na VPS

1. Suba a pasta `landing/` inteira pra VPS (mesmo processo de sempre, arrastando via
   Bitvise).
2. Copie `OpenDub-Setup.exe` pra dentro de `landing/downloads/` na VPS.
3. `docker compose up -d --build` dentro de `landing/`.
4. Aponte `opendub.larchertech.com` (o wildcard cert `*.larchertech.com` ja cobre)
   pro proxy Nginx da VPS na porta 5500, do mesmo jeito que os outros projetos.
