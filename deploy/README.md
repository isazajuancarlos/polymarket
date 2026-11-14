# Despliegue en VPS (paper trading)

Supervisión del bot con **systemd**: arranque al boot y reinicio automático si
el proceso muere. El loop del scanner ya sobrevive a fallos de ciclo individuales
(`core/scanner.py`); systemd cubre la muerte del proceso entero (OOM, excepción
en el arranque, reboot del VPS).

## Instalación

```bash
# 1. Clona/copia el proyecto y crea el venv con dependencias.
cd /mnt/data/polymarket-bot
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # y rellena lo necesario (Telegram opcional)

# 2. Revisa el unit: User/Group, WorkingDirectory y la ruta de ExecStart
#    deben coincidir con TU VPS (por defecto apuntan a /mnt/data/polymarket-bot
#    y al usuario `juan`).
$EDITOR deploy/polymarket-bot.service

# 3. Instala y arranca.
sudo cp deploy/polymarket-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-bot
```

## Operación

```bash
systemctl status polymarket-bot        # estado
journalctl -u polymarket-bot -f        # logs en vivo
sudo systemctl restart polymarket-bot  # reiniciar
sudo systemctl stop polymarket-bot     # parar (SIGTERM -> apagado limpio)
```

## Notas

- **Modo:** el unit corre `--mode paper`. Para solo detección usa `--mode scan`.
  **No** pongas `--mode live` aquí hasta validar en paper y leer el runbook LIVE
  de `CLAUDE.md`.
- **Crecimiento de la DB:** controlado por `DB_RETENTION_DAYS` en `.env` (7 por
  defecto). El bot poda los snapshots de `mercados`/`logs` viejos al arrancar y
  cada hora; `operaciones` y `portafolio` (historial de trading) nunca se tocan.
- **Reinicios en bucle:** si el bot crashea >5 veces en 5 min (p. ej. `.env` mal
  configurado), systemd se rinde. Arréglalo y `systemctl reset-failed
  polymarket-bot` + `start`.
