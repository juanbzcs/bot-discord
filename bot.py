import asyncio
import json
import os
import random
import string
import time
from collections import defaultdict, deque
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("BOT_PREFIX", "!")

if not TOKEN:
    raise RuntimeError("Falta DISCORD_TOKEN en variables de entorno (.env).")

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
SETTINGS_FILE = DATA_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "spam": {
        "max_messages": 6,
        "window_seconds": 8,
        "action": "timeout",  # timeout | kick | ban | tempban
        "timeout_minutes": 15,
        "tempban_minutes": 60,
    },
    "mentions": {
        "max_mentions": 5,
        "action": "timeout",  # timeout | kick | ban
        "timeout_minutes": 30,
    },
    "automod": {
        "block_links": False,
        "block_invites": True,
        "max_caps_percent": 80,
        "min_caps_length": 12,
        "action": "timeout",  # timeout | kick | ban
        "timeout_minutes": 10,
    },
    "raid": {
        "max_joins": 10,
        "window_seconds": 20,
        "enable_lockdown": True,
    },
    "nuke": {
        "max_actions": 2,
        "window_seconds": 20,
        "action": "ban",  # ban | kick
    },
    "verification": {
        "enabled": False,
        "role_id": 0,
        "channel_id": 0,
        "captcha_expire_minutes": 15,
    },
    "moderation": {
        "log_channel_id": 0,
        "warn_threshold": 3,
        "warn_action": "timeout",  # timeout | kick | ban
        "warn_timeout_minutes": 60,
    },
    "emergency_mode": False,
    "tempbans": [],
}

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

message_events: dict[tuple[int, int], deque[float]] = defaultdict(deque)
join_events: dict[int, deque[float]] = defaultdict(deque)
nuke_events: dict[tuple[int, int], deque[float]] = defaultdict(deque)

verification_pending: dict[tuple[int, int], dict[str, Any]] = {}
user_warnings: dict[tuple[int, int], int] = defaultdict(int)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_settings() -> dict[str, Any]:
    if not SETTINGS_FILE.exists():
        SETTINGS_FILE.write_text("{}", encoding="utf-8")
    raw = SETTINGS_FILE.read_text(encoding="utf-8").strip() or "{}"
    data = json.loads(raw)
    if not isinstance(data, dict):
        return {}
    return data


def save_settings(settings: dict[str, Any]) -> None:
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def get_guild_settings(guild_id: int) -> dict[str, Any]:
    settings = load_settings()
    gid = str(guild_id)
    if gid not in settings:
        settings[gid] = deepcopy(DEFAULT_SETTINGS)
        save_settings(settings)
    merged = deepcopy(DEFAULT_SETTINGS)
    merged.update(settings[gid])
    for key in ["spam", "mentions", "automod", "raid", "nuke", "verification", "moderation"]:
        if isinstance(settings[gid].get(key), dict):
            merged[key].update(settings[gid][key])
    return merged


def update_guild_settings(guild_id: int, patch: dict[str, Any]) -> dict[str, Any]:
    settings = load_settings()
    gid = str(guild_id)
    current = deepcopy(settings.get(gid, DEFAULT_SETTINGS))
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(current.get(key), dict):
            current[key].update(value)
        else:
            current[key] = value
    settings[gid] = current
    save_settings(settings)
    return current


def is_privileged(member: discord.Member) -> bool:
    return member.guild.owner_id == member.id or member.guild_permissions.administrator


async def log_mod(guild: discord.Guild, message: str) -> None:
    cfg = get_guild_settings(guild.id)
    channel_id = _safe_int(cfg["moderation"].get("log_channel_id"), 0)
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.send(message)
        except discord.Forbidden:
            return


async def apply_action(member: discord.Member, cfg: dict[str, Any], action: str, reason: str) -> str:
    action = action.lower()
    if action == "ban":
        await member.ban(reason=reason, delete_message_days=0)
        return "ban"
    if action == "kick":
        await member.kick(reason=reason)
        return "kick"
    if action == "tempban":
        minutes = max(1, _safe_int(cfg.get("tempban_minutes"), 60))
        await member.ban(reason=reason, delete_message_days=0)
        guild_cfg = get_guild_settings(member.guild.id)
        tempbans = guild_cfg.get("tempbans", [])
        tempbans.append(
            {
                "user_id": member.id,
                "unban_at": int(time.time() + minutes * 60),
                "reason": reason,
            }
        )
        update_guild_settings(member.guild.id, {"tempbans": tempbans})
        return f"tempban({minutes}m)"

    timeout_minutes = max(1, _safe_int(cfg.get("timeout_minutes"), 15))
    until = discord.utils.utcnow() + timedelta(minutes=timeout_minutes)
    await member.timeout(until, reason=reason)
    return f"timeout({timeout_minutes}m)"


async def set_emergency_mode(guild: discord.Guild, enabled: bool, reason: str) -> None:
    update_guild_settings(guild.id, {"emergency_mode": enabled})
    everyone = guild.default_role
    for channel in guild.text_channels:
        overwrite = channel.overwrites_for(everyone)
        overwrite.send_messages = not enabled
        try:
            await channel.set_permissions(everyone, overwrite=overwrite, reason=reason)
        except discord.Forbidden:
            continue


def generate_captcha_code(length: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


@tasks.loop(seconds=30)
async def tempban_worker() -> None:
    now = int(time.time())
    all_settings = load_settings()
    dirty = False
    for gid, cfg in all_settings.items():
        guild = bot.get_guild(int(gid))
        if not guild:
            continue

        tempbans = cfg.get("tempbans", [])
        if not isinstance(tempbans, list) or not tempbans:
            continue

        remaining = []
        for entry in tempbans:
            user_id = _safe_int(entry.get("user_id"), 0)
            unban_at = _safe_int(entry.get("unban_at"), 0)
            if not user_id or not unban_at:
                dirty = True
                continue

            if now >= unban_at:
                try:
                    await guild.unban(discord.Object(id=user_id), reason="Tempban expirado")
                    await log_mod(guild, f"⏱️ Usuario `{user_id}` desbaneado automáticamente (tempban expirado).")
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    remaining.append(entry)
                dirty = True
            else:
                remaining.append(entry)

        cfg["tempbans"] = remaining

    if dirty:
        save_settings(all_settings)


@bot.event
async def on_ready() -> None:
    if not tempban_worker.is_running():
        tempban_worker.start()
    print(f"Bot conectado como {bot.user} (ID: {bot.user.id})")


@bot.command(name="helpmod")
async def helpmod(ctx: commands.Context) -> None:
    embed = discord.Embed(title="Comandos de Moderación y Seguridad", color=discord.Color.blurple())
    embed.description = (
        "`!ban @user [razon]`\n"
        "`!kick @user [razon]`\n"
        "`!mute @user <minutos> [razon]`\n"
        "`!unmute @user [razon]`\n"
        "`!warn @user [razon]`\n"
        "`!warnings @user`\n"
        "`!clearwarns @user`\n"
        "`!purge <cantidad>`\n"
        "`!setup_proteccion` / `!estado_proteccion`\n"
        "`!config_spam <msg> <seg> <accion>`\n"
        "`!config_spam_tiempos <timeout_min> <tempban_min>`\n"
        "`!config_mentions <max> <accion> <timeout_min>`\n"
        "`!config_automod <links:true|false> <invites:true|false> <caps%> <accion>`\n"
        "`!config_raid <joins> <seg> <lockdown>`\n"
        "`!config_nuke <acciones> <seg> <accion>`\n"
        "`!config_warns <umbral> <accion> <timeout_min>`\n"
        "`!set_log <#canal>`\n"
        "`!setup_verificacion <@rol> <#canal> [expira_min]`\n"
        "`!verificar <codigo>`\n"
        "`!emergencia on|off`\n"
        "`!ping`, `!userinfo [@user]`, `!serverinfo`"
    )
    await ctx.send(embed=embed)


@bot.command(name="setup_proteccion")
@commands.has_guild_permissions(manage_guild=True)
async def setup_proteccion(ctx: commands.Context) -> None:
    update_guild_settings(ctx.guild.id, deepcopy(DEFAULT_SETTINGS))
    await ctx.send("✅ Protección completa inicial configurada para este servidor.")


@bot.command(name="estado_proteccion")
@commands.has_guild_permissions(manage_guild=True)
async def estado_proteccion(ctx: commands.Context) -> None:
    cfg = get_guild_settings(ctx.guild.id)
    embed = discord.Embed(title="Estado de Protección", color=discord.Color.green())
    embed.add_field(name="Spam", value=str(cfg["spam"]), inline=False)
    embed.add_field(name="Mentions", value=str(cfg["mentions"]), inline=False)
    embed.add_field(name="AutoMod", value=str(cfg["automod"]), inline=False)
    embed.add_field(name="Raid", value=str(cfg["raid"]), inline=False)
    embed.add_field(name="Nuke", value=str(cfg["nuke"]), inline=False)
    embed.add_field(name="Verification", value=str(cfg["verification"]), inline=False)
    embed.add_field(name="Moderation", value=str(cfg["moderation"]), inline=False)
    embed.add_field(name="Emergencia", value=str(cfg["emergency_mode"]), inline=False)
    await ctx.send(embed=embed)


@bot.command(name="set_log")
@commands.has_guild_permissions(manage_guild=True)
async def set_log(ctx: commands.Context, canal: discord.TextChannel) -> None:
    update_guild_settings(ctx.guild.id, {"moderation": {"log_channel_id": canal.id}})
    await ctx.send(f"✅ Canal de logs configurado: {canal.mention}")


@bot.command(name="config_spam")
@commands.has_guild_permissions(manage_guild=True)
async def config_spam(ctx: commands.Context, mensajes: int, ventana: int, accion: str) -> None:
    accion = accion.lower()
    if accion not in {"ban", "kick", "timeout", "tempban"}:
        await ctx.send("❌ Acción inválida. Usa: ban, kick, timeout o tempban.")
        return
    update_guild_settings(
        ctx.guild.id,
        {
            "spam": {
                "max_messages": max(2, mensajes),
                "window_seconds": max(2, ventana),
                "action": accion,
            }
        },
    )
    await ctx.send("✅ Configuración anti-spam actualizada.")


@bot.command(name="config_spam_tiempos")
@commands.has_guild_permissions(manage_guild=True)
async def config_spam_tiempos(ctx: commands.Context, timeout_minutos: int, tempban_minutos: int) -> None:
    update_guild_settings(
        ctx.guild.id,
        {
            "spam": {
                "timeout_minutes": max(1, timeout_minutos),
                "tempban_minutes": max(1, tempban_minutos),
            }
        },
    )
    await ctx.send("✅ Tiempos de anti-spam actualizados.")


@bot.command(name="config_mentions")
@commands.has_guild_permissions(manage_guild=True)
async def config_mentions(ctx: commands.Context, max_menciones: int, accion: str, timeout_minutos: int = 30) -> None:
    accion = accion.lower()
    if accion not in {"timeout", "kick", "ban"}:
        await ctx.send("❌ Acción inválida. Usa: timeout, kick o ban.")
        return
    update_guild_settings(
        ctx.guild.id,
        {
            "mentions": {
                "max_mentions": max(2, max_menciones),
                "action": accion,
                "timeout_minutes": max(1, timeout_minutos),
            }
        },
    )
    await ctx.send("✅ Configuración de menciones actualizada.")


@bot.command(name="config_automod")
@commands.has_guild_permissions(manage_guild=True)
async def config_automod(
    ctx: commands.Context,
    bloquear_links: bool,
    bloquear_invites: bool,
    max_caps_percent: int,
    accion: str,
) -> None:
    accion = accion.lower()
    if accion not in {"timeout", "kick", "ban"}:
        await ctx.send("❌ Acción inválida. Usa: timeout, kick o ban.")
        return
    update_guild_settings(
        ctx.guild.id,
        {
            "automod": {
                "block_links": bloquear_links,
                "block_invites": bloquear_invites,
                "max_caps_percent": min(100, max(30, max_caps_percent)),
                "action": accion,
            }
        },
    )
    await ctx.send("✅ Configuración de automod actualizada.")


@bot.command(name="config_raid")
@commands.has_guild_permissions(manage_guild=True)
async def config_raid(ctx: commands.Context, joins: int, ventana: int, activar_lockdown: bool) -> None:
    update_guild_settings(
        ctx.guild.id,
        {
            "raid": {
                "max_joins": max(2, joins),
                "window_seconds": max(3, ventana),
                "enable_lockdown": activar_lockdown,
            }
        },
    )
    await ctx.send("✅ Configuración anti-raid actualizada.")


@bot.command(name="config_nuke")
@commands.has_guild_permissions(manage_guild=True)
async def config_nuke(ctx: commands.Context, acciones: int, ventana: int, accion: str) -> None:
    accion = accion.lower()
    if accion not in {"ban", "kick"}:
        await ctx.send("❌ Acción inválida para anti-nuke. Usa: ban o kick.")
        return
    update_guild_settings(
        ctx.guild.id,
        {
            "nuke": {
                "max_actions": max(1, acciones),
                "window_seconds": max(3, ventana),
                "action": accion,
            }
        },
    )
    await ctx.send("✅ Configuración anti-nuke actualizada.")


@bot.command(name="config_warns")
@commands.has_guild_permissions(manage_guild=True)
async def config_warns(ctx: commands.Context, umbral: int, accion: str, timeout_minutos: int = 60) -> None:
    accion = accion.lower()
    if accion not in {"timeout", "kick", "ban"}:
        await ctx.send("❌ Acción inválida. Usa: timeout, kick o ban.")
        return
    update_guild_settings(
        ctx.guild.id,
        {
            "moderation": {
                "warn_threshold": max(1, umbral),
                "warn_action": accion,
                "warn_timeout_minutes": max(1, timeout_minutos),
            }
        },
    )
    await ctx.send("✅ Configuración de warns actualizada.")


@bot.command(name="setup_verificacion")
@commands.has_guild_permissions(manage_guild=True)
async def setup_verificacion(
    ctx: commands.Context,
    rol_verificado: discord.Role,
    canal_verificacion: discord.TextChannel,
    expira_minutos: int = 15,
) -> None:
    update_guild_settings(
        ctx.guild.id,
        {
            "verification": {
                "enabled": True,
                "role_id": rol_verificado.id,
                "channel_id": canal_verificacion.id,
                "captcha_expire_minutes": max(2, expira_minutos),
            }
        },
    )
    await ctx.send("✅ Verificación activada. Nuevos usuarios deberán usar captcha con `!verificar <codigo>`.")


@bot.command(name="verificar")
async def verificar(ctx: commands.Context, codigo: str) -> None:
    if not ctx.guild or not isinstance(ctx.author, discord.Member):
        return

    cfg = get_guild_settings(ctx.guild.id)
    if not cfg["verification"]["enabled"]:
        await ctx.send("ℹ️ La verificación no está activada en este servidor.")
        return

    key = (ctx.guild.id, ctx.author.id)
    pending = verification_pending.get(key)
    if not pending:
        await ctx.send("❌ No tienes captcha pendiente o ya expiró.")
        return

    if time.time() > pending["expires_at"]:
        verification_pending.pop(key, None)
        await ctx.send("❌ Tu captcha expiró. Pide uno nuevo esperando el próximo aviso.")
        return

    if codigo.upper() != pending["code"]:
        await ctx.send("❌ Código incorrecto.")
        return

    role_id = _safe_int(cfg["verification"]["role_id"], 0)
    role = ctx.guild.get_role(role_id)
    if not role:
        await ctx.send("❌ No encuentro el rol de verificados. Reconfigura con `!setup_verificacion`.")
        return

    try:
        await ctx.author.add_roles(role, reason="Verificación captcha completada")
        verification_pending.pop(key, None)
        await ctx.send("✅ Verificación completada. ¡Bienvenido!")
        await log_mod(ctx.guild, f"✅ {ctx.author} verificado correctamente.")
    except discord.Forbidden:
        await ctx.send("⚠️ No tengo permisos para asignarte el rol.")


@bot.command(name="emergencia")
@commands.has_guild_permissions(manage_guild=True)
async def emergencia(ctx: commands.Context, estado: str) -> None:
    estado = estado.lower()
    if estado not in {"on", "off"}:
        await ctx.send("Uso: !emergencia on|off")
        return
    enabled = estado == "on"
    await set_emergency_mode(ctx.guild, enabled, reason=f"Modo emergencia por {ctx.author}")
    await ctx.send(f"✅ Modo emergencia {'activado' if enabled else 'desactivado'}.")


@bot.command(name="ban")
@commands.has_guild_permissions(ban_members=True)
async def ban_member(ctx: commands.Context, member: discord.Member, *, reason: str = "Sin razón") -> None:
    if is_privileged(member):
        await ctx.send("❌ No se puede banear a un administrador/owner.")
        return
    await member.ban(reason=f"{reason} | por {ctx.author}", delete_message_days=0)
    await ctx.send(f"🔨 {member} baneado. Razón: {reason}")
    await log_mod(ctx.guild, f"🔨 {member} baneado por {ctx.author}. Razón: {reason}")


@bot.command(name="kick")
@commands.has_guild_permissions(kick_members=True)
async def kick_member(ctx: commands.Context, member: discord.Member, *, reason: str = "Sin razón") -> None:
    if is_privileged(member):
        await ctx.send("❌ No se puede expulsar a un administrador/owner.")
        return
    await member.kick(reason=f"{reason} | por {ctx.author}")
    await ctx.send(f"👢 {member} expulsado. Razón: {reason}")
    await log_mod(ctx.guild, f"👢 {member} expulsado por {ctx.author}. Razón: {reason}")


@bot.command(name="mute")
@commands.has_guild_permissions(moderate_members=True)
async def mute_member(ctx: commands.Context, member: discord.Member, minutos: int, *, reason: str = "Sin razón") -> None:
    if is_privileged(member):
        await ctx.send("❌ No se puede mutear a un administrador/owner.")
        return
    until = discord.utils.utcnow() + timedelta(minutes=max(1, minutos))
    await member.timeout(until, reason=f"{reason} | por {ctx.author}")
    await ctx.send(f"🔇 {member} muteado por {max(1, minutos)} minutos.")
    await log_mod(ctx.guild, f"🔇 {member} muteado por {ctx.author} ({max(1, minutos)} min). Razón: {reason}")


@bot.command(name="unmute")
@commands.has_guild_permissions(moderate_members=True)
async def unmute_member(ctx: commands.Context, member: discord.Member, *, reason: str = "Sin razón") -> None:
    await member.timeout(None, reason=f"{reason} | por {ctx.author}")
    await ctx.send(f"🔈 {member} desmuteado.")
    await log_mod(ctx.guild, f"🔈 {member} desmuteado por {ctx.author}. Razón: {reason}")


@bot.command(name="warn")
@commands.has_guild_permissions(manage_messages=True)
async def warn_member(ctx: commands.Context, member: discord.Member, *, reason: str = "Sin razón") -> None:
    if is_privileged(member):
        await ctx.send("❌ No se puede warn a un administrador/owner.")
        return

    key = (ctx.guild.id, member.id)
    user_warnings[key] += 1
    count = user_warnings[key]
    await ctx.send(f"⚠️ {member.mention} advertido. Total warns: {count}.")
    await log_mod(ctx.guild, f"⚠️ Warn a {member} por {ctx.author}. Total={count}. Razón: {reason}")

    cfg = get_guild_settings(ctx.guild.id)
    threshold = _safe_int(cfg["moderation"].get("warn_threshold"), 3)
    if count >= threshold:
        action = cfg["moderation"].get("warn_action", "timeout")
        action_cfg = {
            "timeout_minutes": _safe_int(cfg["moderation"].get("warn_timeout_minutes"), 60),
            "tempban_minutes": _safe_int(cfg["spam"].get("tempban_minutes"), 60),
        }
        try:
            result = await apply_action(member, action_cfg, action, "AutoMod: límite de warns alcanzado")
            await ctx.send(f"🛡️ Acción automática por warns aplicada a {member.mention}: {result}")
            await log_mod(ctx.guild, f"🛡️ Acción por warns aplicada a {member}: {result}")
            user_warnings[key] = 0
        except discord.Forbidden:
            await ctx.send("⚠️ No pude aplicar acción por warns (permisos insuficientes).")


@bot.command(name="warnings")
@commands.has_guild_permissions(manage_messages=True)
async def warnings_count(ctx: commands.Context, member: discord.Member) -> None:
    count = user_warnings[(ctx.guild.id, member.id)]
    await ctx.send(f"ℹ️ {member.mention} tiene {count} warns.")


@bot.command(name="clearwarns")
@commands.has_guild_permissions(manage_messages=True)
async def clear_warnings(ctx: commands.Context, member: discord.Member) -> None:
    user_warnings[(ctx.guild.id, member.id)] = 0
    await ctx.send(f"✅ Warns reiniciados para {member.mention}.")


@bot.command(name="purge")
@commands.has_guild_permissions(manage_messages=True)
async def purge_messages(ctx: commands.Context, cantidad: int) -> None:
    deleted = await ctx.channel.purge(limit=max(1, min(100, cantidad + 1)))
    msg = await ctx.send(f"🧹 Eliminados {len(deleted) - 1} mensajes.")
    await asyncio.sleep(4)
    await msg.delete()


@bot.command(name="ping")
async def ping(ctx: commands.Context) -> None:
    await ctx.send(f"🏓 Pong: {round(bot.latency * 1000)}ms")


@bot.command(name="userinfo")
async def userinfo(ctx: commands.Context, member: discord.Member | None = None) -> None:
    member = member or ctx.author
    embed = discord.Embed(title=f"Usuario: {member}", color=discord.Color.gold())
    embed.add_field(name="ID", value=str(member.id), inline=True)
    embed.add_field(name="Cuenta creada", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
    embed.add_field(name="Entró al server", value=discord.utils.format_dt(member.joined_at, "R"), inline=True)
    embed.add_field(name="Roles", value=str(len(member.roles) - 1), inline=True)
    await ctx.send(embed=embed)


@bot.command(name="serverinfo")
async def serverinfo(ctx: commands.Context) -> None:
    guild = ctx.guild
    embed = discord.Embed(title=f"Servidor: {guild.name}", color=discord.Color.teal())
    embed.add_field(name="Miembros", value=str(guild.member_count), inline=True)
    embed.add_field(name="Canales texto", value=str(len(guild.text_channels)), inline=True)
    embed.add_field(name="Roles", value=str(len(guild.roles)), inline=True)
    embed.add_field(name="Owner", value=str(guild.owner), inline=False)
    await ctx.send(embed=embed)


@bot.event
async def on_member_join(member: discord.Member) -> None:
    cfg = get_guild_settings(member.guild.id)
    now = time.time()

    joins = join_events[member.guild.id]
    joins.append(now)
    while joins and now - joins[0] > cfg["raid"]["window_seconds"]:
        joins.popleft()

    if len(joins) >= cfg["raid"]["max_joins"] and cfg["raid"]["enable_lockdown"] and not cfg["emergency_mode"]:
        await set_emergency_mode(member.guild, True, reason="Raid detectada automáticamente")
        if member.guild.system_channel:
            await member.guild.system_channel.send("🚨 Raid detectada: modo emergencia activado automáticamente.")
        await log_mod(member.guild, "🚨 Raid detectada y modo emergencia activado automáticamente.")

    if cfg["verification"]["enabled"]:
        code = generate_captcha_code()
        expiration = now + max(120, _safe_int(cfg["verification"].get("captcha_expire_minutes"), 15) * 60)
        verification_pending[(member.guild.id, member.id)] = {
            "code": code,
            "expires_at": expiration,
        }

        verification_channel_id = _safe_int(cfg["verification"].get("channel_id"), 0)
        channel = member.guild.get_channel(verification_channel_id)
        if isinstance(channel, discord.TextChannel):
            await channel.send(
                f"👋 {member.mention}, para verificarte escribe en este servidor: `!verificar {code}`\n"
                f"⏳ Este código expira en {_safe_int(cfg['verification'].get('captcha_expire_minutes'), 15)} minutos."
            )


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or not message.guild:
        return

    member = message.author
    if not isinstance(member, discord.Member):
        await bot.process_commands(message)
        return

    if is_privileged(member):
        await bot.process_commands(message)
        return

    cfg = get_guild_settings(message.guild.id)
    now = time.time()
    content_lower = message.content.lower()

    key = (message.guild.id, member.id)
    user_msgs = message_events[key]
    user_msgs.append(now)
    while user_msgs and now - user_msgs[0] > cfg["spam"]["window_seconds"]:
        user_msgs.popleft()

    if len(user_msgs) >= cfg["spam"]["max_messages"]:
        try:
            result = await apply_action(member, cfg["spam"], cfg["spam"]["action"], "AutoMod: spam detectado")
            await message.channel.send(f"🛡️ {member.mention} sancionado por spam ({result}).")
            await log_mod(message.guild, f"🛡️ Spam: {member} -> {result}")
        except discord.Forbidden:
            await message.channel.send("⚠️ No pude sancionar por falta de permisos.")
        user_msgs.clear()
        await bot.process_commands(message)
        return

    if len(message.mentions) >= cfg["mentions"]["max_mentions"]:
        try:
            result = await apply_action(member, cfg["mentions"], cfg["mentions"]["action"], "AutoMod: abuso de menciones")
            await message.channel.send(f"🛡️ {member.mention} sancionado por menciones ({result}).")
            await log_mod(message.guild, f"🛡️ Menciones: {member} -> {result}")
        except discord.Forbidden:
            await message.channel.send("⚠️ No pude aplicar sanción por menciones.")

    has_url = "http://" in content_lower or "https://" in content_lower
    has_invite = "discord.gg/" in content_lower or "discord.com/invite/" in content_lower

    letters = [c for c in message.content if c.isalpha()]
    caps_percent = 0
    if len(letters) >= max(1, _safe_int(cfg["automod"]["min_caps_length"], 12)):
        uppercase = sum(1 for c in letters if c.isupper())
        caps_percent = int((uppercase / len(letters)) * 100)

    automod_triggered = (
        (cfg["automod"]["block_links"] and has_url)
        or (cfg["automod"]["block_invites"] and has_invite)
        or (caps_percent >= _safe_int(cfg["automod"]["max_caps_percent"], 80))
    )

    if automod_triggered:
        try:
            await message.delete()
        except discord.Forbidden:
            pass

        try:
            result = await apply_action(member, cfg["automod"], cfg["automod"]["action"], "AutoMod: regla de contenido")
            await message.channel.send(f"🧹 Mensaje de {member.mention} eliminado y acción aplicada: {result}")
            await log_mod(message.guild, f"🧹 AutoMod contenido: {member} -> {result}")
        except discord.Forbidden:
            await message.channel.send("⚠️ No pude aplicar acción de automod por permisos insuficientes.")

    await bot.process_commands(message)


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel) -> None:
    await handle_nuke_event(channel.guild, discord.AuditLogAction.channel_delete)


@bot.event
async def on_guild_role_delete(role: discord.Role) -> None:
    await handle_nuke_event(role.guild, discord.AuditLogAction.role_delete)


async def handle_nuke_event(guild: discord.Guild, action_type: discord.AuditLogAction) -> None:
    cfg = get_guild_settings(guild.id)
    try:
        entry = None
        async for log_entry in guild.audit_logs(limit=5, action=action_type):
            entry = log_entry
            break

        if not entry or not isinstance(entry.user, discord.Member):
            return

        executor = entry.user
        if is_privileged(executor) or executor.id == bot.user.id:
            return

        now = time.time()
        key = (guild.id, executor.id)
        actions = nuke_events[key]
        actions.append(now)

        while actions and now - actions[0] > cfg["nuke"]["window_seconds"]:
            actions.popleft()

        if len(actions) >= cfg["nuke"]["max_actions"]:
            if cfg["nuke"]["action"] == "ban":
                await guild.ban(executor, reason="AutoMod: posible nuke detectado")
                result = "baneado"
            else:
                await guild.kick(executor, reason="AutoMod: posible nuke detectado")
                result = "expulsado"
            actions.clear()
            if guild.system_channel:
                await guild.system_channel.send(f"🚨 Usuario {executor} {result} por actividad de nuke.")
            await log_mod(guild, f"🚨 Anti-nuke: {executor} {result} automáticamente.")
    except discord.Forbidden:
        return


@helpmod.error
@setup_proteccion.error
@estado_proteccion.error
@set_log.error
@config_spam.error
@config_spam_tiempos.error
@config_mentions.error
@config_automod.error
@config_raid.error
@config_nuke.error
@config_warns.error
@setup_verificacion.error
@emergencia.error
@ban_member.error
@kick_member.error
@mute_member.error
@unmute_member.error
@warn_member.error
@warnings_count.error
@clear_warnings.error
@purge_messages.error
async def admin_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ No tienes permisos para usar este comando.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Argumentos inválidos. Revisa el formato del comando.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Faltan argumentos. Usa `!helpmod` para ver ejemplos.")
    else:
        await ctx.send(f"⚠️ Error: {error}")


async def main() -> None:
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
