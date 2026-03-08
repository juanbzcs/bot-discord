# Discord Security Bot (Anti-Raid / Anti-Nuke / AutoMod)

Bot de moderación avanzada para Discord, inspirado en herramientas tipo Carl-bot: **moderation**, **anti-raid**, **anti-nuke**, **automod**, **antispam**, **verification/captcha** y utilidades.

## Funciones incluidas

- ✅ Anti-spam configurable con sanción automática: `timeout`, `kick`, `ban`, `tempban`.
- ✅ Anti-mentions (menciones masivas) configurable con acción automática.
- ✅ AutoMod de contenido:
  - bloqueo de links
  - bloqueo de invitaciones (`discord.gg` / `discord.com/invite`)
  - control de exceso de mayúsculas
- ✅ Anti-raid por oleada de entradas + **modo emergencia (lockdown)** automático.
- ✅ Anti-nuke por auditoría (borrado de canales/roles) con baneo/expulsión automática.
- ✅ Verificación por captcha simple con comando `!verificar <codigo>`.
- ✅ Comandos de moderación manual: `!ban`, `!kick`, `!mute`, `!unmute`, `!warn`, `!purge`.
- ✅ Sistema de warns con escalado automático configurable.
- ✅ Logs de moderación en canal configurable.
- ✅ Persistencia por servidor en `data/settings.json`.

## Instalación rápida (de una)

```bash
chmod +x install.sh
./install.sh
```

Luego abre `.env`, pega tu token y arranca:

```bash
source .venv/bin/activate
python bot.py
```

## Requisitos

- Python 3.10+
- Dependencias del proyecto (`requirements.txt`)
- Intents habilitados en el Developer Portal:
  - `SERVER MEMBERS INTENT`
  - `MESSAGE CONTENT INTENT`

## Instalación

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edita `.env`:

```env
DISCORD_TOKEN=tu_token
BOT_PREFIX=!
```

## Ejecución

```bash
python bot.py
```

## Comandos principales

### Setup / Config

- `!setup_proteccion`
- `!estado_proteccion`
- `!set_log #canal`
- `!config_spam <mensajes> <ventana_segundos> <accion>`
- `!config_spam_tiempos <timeout_minutos> <tempban_minutos>`
- `!config_mentions <max_menciones> <accion> <timeout_minutos>`
- `!config_automod <bloquear_links:true|false> <bloquear_invites:true|false> <max_caps_percent> <accion>`
- `!config_raid <joins> <ventana_segundos> <activar_lockdown:true|false>`
- `!config_nuke <acciones> <ventana_segundos> <accion>`
- `!config_warns <umbral> <accion> <timeout_minutos>`
- `!setup_verificacion <@rol_verificado> <#canal_verificacion> [expira_minutos]`
- `!emergencia on|off`

### Moderación manual

- `!ban @usuario [razon]`
- `!kick @usuario [razon]`
- `!mute @usuario <minutos> [razon]`
- `!unmute @usuario [razon]`
- `!warn @usuario [razon]`
- `!warnings @usuario`
- `!clearwarns @usuario`
- `!purge <cantidad>`

### Verificación

- `!verificar <codigo>`

### Utilidad

- `!helpmod`
- `!ping`
- `!userinfo [@usuario]`
- `!serverinfo`

## Permisos recomendados del bot

- View Audit Log
- Manage Channels
- Manage Roles
- Ban Members
- Kick Members
- Moderate Members
- Manage Messages
- Read Message History

## Notas

- El bot excluye owner/admin de sanciones automáticas.
- `tempban` se gestiona con un worker interno que revisa expiraciones y desbanea automáticamente.
- Para producción, prueba primero en servidor de staging.
