"""
╔══════════════════════════════════════════════════════╗
║           ZAYT EXPORT — BOT DISCORD + SERVEUR       ║
║   Lifetime lock sur UserId Roblox                   ║
║   +info avec toutes les infos utilisateur           ║
╚══════════════════════════════════════════════════════╝

INSTALLATION :
  pip install discord.py flask requests

LANCEMENT :
  python bot.py
"""

import discord
from discord.ext import commands
import json, os, random, string, threading, requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify

# ══════════════════════════════════════════════
#  CONFIG — CHANGE CES VALEURS
# ══════════════════════════════════════════════
DISCORD_TOKEN   = "MTQ3OTk0MDQzMDEzNjIxNzcxMg.G18wsK.5AhA5IX4RooeFGreO86Yv5Hc5oPnuuKQClIqvA"
BOT_PREFIX      = "+"
OWNER_IDS       = [1302360384111640646]
ADMIN_ROLE_NAME = "Admin"
PORT            = 5000
KEYS_FILE       = "keys.json"

# ══════════════════════════════════════════════
#  GESTION DES CLÉS
# ══════════════════════════════════════════════
def load_keys():
    if not os.path.exists(KEYS_FILE):
        return {}
    with open(KEYS_FILE, "r") as f:
        return json.load(f)

def save_keys(data):
    with open(KEYS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def gen_key():
    parts = ["".join(random.choices(string.ascii_uppercase + string.digits, k=4)) for _ in range(3)]
    return "ZAYT-" + "-".join(parts)

def parse_duration(s):
    s = s.strip().lower()
    if s in ("perm", "permanent", "inf", "forever", "lifetime"):
        return None
    if s.endswith("d"):
        return timedelta(days=int(s[:-1]))
    if s.endswith("h"):
        return timedelta(hours=int(s[:-1]))
    if s.endswith("m"):
        return timedelta(minutes=int(s[:-1]))
    raise ValueError(f"Durée invalide : {s}")

def fmt_date(dt):
    if dt is None:
        return "♾️ Lifetime"
    return dt.strftime("%d/%m/%Y %H:%M")

def is_expired(key_data):
    if key_data.get("banned"):
        return True
    expires = key_data.get("expires")
    if expires is None:
        return False
    return datetime.utcnow() > datetime.fromisoformat(expires)

# ══════════════════════════════════════════════
#  RÉCUPÉRER INFOS ROBLOX PAR USER ID
# ══════════════════════════════════════════════
def get_roblox_info(user_id):
    """Récupère username + infos publiques Roblox via l'API officielle"""
    try:
        r = requests.get(f"https://users.roblox.com/v1/users/{user_id}", timeout=5)
        if r.status_code == 200:
            data = r.json()
            return {
                "username":     data.get("name", "?"),
                "displayName":  data.get("displayName", "?"),
                "created":      data.get("created", "?")[:10] if data.get("created") else "?",
                "isBanned":     data.get("isBanned", False),
                "description":  data.get("description", "")[:80] if data.get("description") else "",
            }
    except Exception:
        pass
    return None

# ══════════════════════════════════════════════
#  SERVEUR FLASK
# ══════════════════════════════════════════════
app = Flask(__name__)

@app.route("/verify", methods=["POST"])
def verify():
    body    = request.get_json(force=True, silent=True) or {}
    key     = str(body.get("key", "")).upper().strip()
    user_id = str(body.get("userId", "")).strip()
    username= str(body.get("username", "?")).strip()
    ip_addr = request.headers.get("X-Forwarded-For", request.remote_addr)

    keys = load_keys()

    if key not in keys:
        return jsonify({"valid": False, "message": "Clé introuvable"}), 200

    kd = keys[key]

    # Vérifié si bannie
    if kd.get("banned"):
        return jsonify({"valid": False, "message": "Clé bannie"}), 200

    # Vérifier expiration
    expires_str = kd.get("expires")
    if expires_str:
        if datetime.utcnow() > datetime.fromisoformat(expires_str):
            return jsonify({"valid": False, "message": "Clé expirée"}), 200

    # ── LOCK USER ID ──────────────────────────────
    locked_id = kd.get("locked_userid")

    if locked_id is None:
        # 1ère utilisation → on lock sur cet UserId
        if not user_id or user_id == "":
            return jsonify({"valid": False, "message": "UserId manquant"}), 200

        kd["locked_userid"]   = user_id
        kd["locked_username"] = username
        kd["locked_at"]       = datetime.utcnow().isoformat()
        kd["first_ip"]        = ip_addr

    elif locked_id != user_id:
        # Quelqu'un d'autre essaie d'utiliser la clé
        return jsonify({
            "valid":   False,
            "message": "Cette clé est déjà utilisée par quelqu'un d'autre"
        }), 200

    # ── LOG CONNEXION ─────────────────────────────
    kd["last_use"]      = datetime.utcnow().isoformat()
    kd["last_username"] = username
    kd["last_ip"]       = ip_addr
    kd["uses"]          = kd.get("uses", 0) + 1

    save_keys(keys)

    expires_display = fmt_date(
        datetime.fromisoformat(expires_str) if expires_str else None
    )

    return jsonify({
        "valid":   True,
        "message": "OK",
        "expires": expires_display,
        "uses":    kd["uses"],
        "note":    kd.get("note", ""),
    }), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "online", "keys": len(load_keys())}), 200

def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ══════════════════════════════════════════════
#  BOT DISCORD
# ══════════════════════════════════════════════
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents, help_command=None)

def is_owner_or_admin(ctx):
    if ctx.author.id in OWNER_IDS:
        return True
    return any(r.name == ADMIN_ROLE_NAME for r in ctx.author.roles)

def zayt_embed(title, desc, color=0x9B3FFF):
    e = discord.Embed(title=title, description=desc, color=color)
    e.set_footer(text="Zayt Export System")
    e.timestamp = datetime.utcnow()
    return e

# ──────────────────────────────────────────────
#  +add <durée> [note]
#  Durée : 30d / 365d / perm / lifetime
# ──────────────────────────────────────────────
@bot.command(name="add")
async def cmd_add(ctx, duration: str = "lifetime", *, note: str = ""):
    if not is_owner_or_admin(ctx):
        await ctx.send(embed=zayt_embed("❌ Refusé", "Tu n'as pas la permission.", 0xFF4444))
        return

    try:
        delta = parse_duration(duration)
    except ValueError as e:
        await ctx.send(embed=zayt_embed("❌ Erreur", str(e), 0xFF4444))
        return

    keys = load_keys()
    key  = gen_key()

    expires_dt  = (datetime.utcnow() + delta) if delta else None
    expires_iso = expires_dt.isoformat() if expires_dt else None

    keys[key] = {
        "created":        datetime.utcnow().isoformat(),
        "expires":        expires_iso,
        "banned":         False,
        "note":           note,
        "uses":           0,
        "created_by":     str(ctx.author),
        "locked_userid":  None,   # sera rempli au 1er usage
        "locked_username":None,
        "locked_at":      None,
        "last_use":       None,
        "last_username":  None,
        "last_ip":        None,
        "first_ip":       None,
    }
    save_keys(keys)

    e = zayt_embed("✅ Clé créée", f"```\n{key}\n```")
    e.add_field(name="⏱ Durée",   value=duration.upper(),    inline=True)
    e.add_field(name="📅 Expire", value=fmt_date(expires_dt), inline=True)
    e.add_field(name="🔒 Lock",   value="Au 1er usage",       inline=True)
    if note:
        e.add_field(name="📝 Note", value=note, inline=False)
    await ctx.send(embed=e)

# ──────────────────────────────────────────────
#  +remove <clé>
# ──────────────────────────────────────────────
@bot.command(name="remove")
async def cmd_remove(ctx, key: str = ""):
    if not is_owner_or_admin(ctx):
        await ctx.send(embed=zayt_embed("❌ Refusé", "Tu n'as pas la permission.", 0xFF4444))
        return
    key = key.upper().strip()
    keys = load_keys()
    if key not in keys:
        await ctx.send(embed=zayt_embed("❌ Introuvable", f"La clé `{key}` n'existe pas.", 0xFF4444))
        return
    del keys[key]
    save_keys(keys)
    await ctx.send(embed=zayt_embed("🗑 Clé supprimée", f"```\n{key}\n```", 0xFF8800))

# ──────────────────────────────────────────────
#  +ban / +unban <clé>
# ──────────────────────────────────────────────
@bot.command(name="ban")
async def cmd_ban(ctx, key: str = ""):
    if not is_owner_or_admin(ctx):
        await ctx.send(embed=zayt_embed("❌ Refusé", "Tu n'as pas la permission.", 0xFF4444))
        return
    key = key.upper().strip()
    keys = load_keys()
    if key not in keys:
        await ctx.send(embed=zayt_embed("❌ Introuvable", f"Clé introuvable.", 0xFF4444))
        return
    keys[key]["banned"] = True
    save_keys(keys)
    await ctx.send(embed=zayt_embed("🔨 Clé bannie", f"```\n{key}\n```\nInutilisable désormais.", 0xFF4444))

@bot.command(name="unban")
async def cmd_unban(ctx, key: str = ""):
    if not is_owner_or_admin(ctx):
        await ctx.send(embed=zayt_embed("❌ Refusé", "Tu n'as pas la permission.", 0xFF4444))
        return
    key = key.upper().strip()
    keys = load_keys()
    if key not in keys:
        await ctx.send(embed=zayt_embed("❌ Introuvable", "Clé introuvable.", 0xFF4444))
        return
    keys[key]["banned"] = False
    save_keys(keys)
    await ctx.send(embed=zayt_embed("✅ Clé débannie", f"```\n{key}\n```", 0x44FF88))

# ──────────────────────────────────────────────
#  +unlock <clé>   → déverrouille le lock UserId
#  (utile si le mec change de compte)
# ──────────────────────────────────────────────
@bot.command(name="unlock")
async def cmd_unlock(ctx, key: str = ""):
    if not is_owner_or_admin(ctx):
        await ctx.send(embed=zayt_embed("❌ Refusé", "Tu n'as pas la permission.", 0xFF4444))
        return
    key = key.upper().strip()
    keys = load_keys()
    if key not in keys:
        await ctx.send(embed=zayt_embed("❌ Introuvable", "Clé introuvable.", 0xFF4444))
        return
    old_user = keys[key].get("locked_username", "?")
    keys[key]["locked_userid"]   = None
    keys[key]["locked_username"] = None
    keys[key]["locked_at"]       = None
    save_keys(keys)
    e = zayt_embed("🔓 Lock retiré", f"```\n{key}\n```", 0x9B3FFF)
    e.add_field(name="Ancien utilisateur", value=old_user, inline=True)
    e.add_field(name="Statut", value="La clé peut être réutilisée", inline=True)
    await ctx.send(embed=e)

# ──────────────────────────────────────────────
#  +time <clé> <+/-durée>
# ──────────────────────────────────────────────
@bot.command(name="time")
async def cmd_time(ctx, key: str = "", duration: str = ""):
    if not is_owner_or_admin(ctx):
        await ctx.send(embed=zayt_embed("❌ Refusé", "Tu n'as pas la permission.", 0xFF4444))
        return
    key = key.upper().strip()
    keys = load_keys()
    if key not in keys:
        await ctx.send(embed=zayt_embed("❌ Introuvable", "Clé introuvable.", 0xFF4444))
        return
    sign = 1
    dur = duration
    if duration.startswith("+"):
        dur = duration[1:]
    elif duration.startswith("-"):
        sign = -1
        dur = duration[1:]
    try:
        delta = parse_duration(dur)
    except ValueError as e:
        await ctx.send(embed=zayt_embed("❌ Erreur", str(e), 0xFF4444))
        return
    kd = keys[key]
    if delta is None:
        kd["expires"] = None
        save_keys(keys)
        await ctx.send(embed=zayt_embed("♾️ Rendue Lifetime", f"```\n{key}\n```", 0x9B3FFF))
        return
    expires_str = kd.get("expires")
    current = datetime.fromisoformat(expires_str) if expires_str else datetime.utcnow()
    new_exp = current + (sign * delta)
    kd["expires"] = new_exp.isoformat()
    save_keys(keys)
    action = "ajouté" if sign == 1 else "retiré"
    e = zayt_embed(f"⏱ Temps {action}", f"```\n{key}\n```")
    e.add_field(name="Modification",         value=f"`{duration}`",     inline=True)
    e.add_field(name="Nouvelle expiration",  value=fmt_date(new_exp),   inline=True)
    await ctx.send(embed=e)

# ──────────────────────────────────────────────
#  +info <clé>   ← VERSION AMÉLIORÉE
#  Affiche toutes les infos + infos Roblox
# ──────────────────────────────────────────────
@bot.command(name="info")
async def cmd_info(ctx, key: str = ""):
    if not is_owner_or_admin(ctx):
        await ctx.send(embed=zayt_embed("❌ Refusé", "Tu n'as pas la permission.", 0xFF4444))
        return

    key = key.upper().strip()
    keys = load_keys()

    if key not in keys:
        await ctx.send(embed=zayt_embed("❌ Introuvable", "Clé introuvable.", 0xFF4444))
        return

    kd      = keys[key]
    expired = is_expired(kd)
    banned  = kd.get("banned", False)

    if banned:
        status = "🔨 Bannie"
        color  = 0xFF4444
    elif expired:
        status = "❌ Expirée"
        color  = 0xFF8800
    else:
        status = "✅ Active"
        color  = 0x44FF88

    e = zayt_embed(f"🔑 Info clé", f"```\n{key}\n```", color)

    # ── Infos clé ─────────────────────────────
    e.add_field(name="📊 Statut",        value=status,                                                inline=True)
    e.add_field(name="🔢 Utilisations",  value=str(kd.get("uses", 0)),                                inline=True)
    e.add_field(name="📅 Expiration",    value=fmt_date(
        datetime.fromisoformat(kd["expires"]) if kd.get("expires") else None),                        inline=True)
    e.add_field(name="🛠️ Créée par",    value=kd.get("created_by", "?"),                             inline=True)
    e.add_field(name="📆 Créée le",      value=kd.get("created", "?")[:19],                          inline=True)
    if kd.get("note"):
        e.add_field(name="📝 Note",      value=kd["note"],                                            inline=True)

    # ── Infos utilisateur lockée ──────────────
    locked_id = kd.get("locked_userid")
    if locked_id:
        e.add_field(name="\u200b", value="**── Utilisateur Roblox ──**", inline=False)
        e.add_field(name="🆔 UserId Roblox",   value=f"`{locked_id}`",                               inline=True)
        e.add_field(name="👤 Username (lock)",  value=kd.get("locked_username", "?"),                 inline=True)
        e.add_field(name="🔒 Locké le",         value=kd.get("locked_at", "?")[:19] if kd.get("locked_at") else "?", inline=True)

        # Récupérer infos Roblox live
        rblx = get_roblox_info(locked_id)
        if rblx:
            e.add_field(name="🎮 Username actuel",  value=rblx["username"],                           inline=True)
            e.add_field(name="📛 Display Name",     value=rblx["displayName"],                        inline=True)
            e.add_field(name="📅 Compte créé",      value=rblx["created"],                            inline=True)
            e.add_field(name="🚫 Compte banni",     value="Oui" if rblx["isBanned"] else "Non",       inline=True)
            if rblx["description"]:
                e.add_field(name="📄 Bio",          value=rblx["description"],                        inline=False)
            # Lien profil
            e.add_field(name="🔗 Profil Roblox",
                value=f"[Voir le profil](https://www.roblox.com/users/{locked_id}/profile)",          inline=True)
        else:
            e.add_field(name="⚠️ Roblox API",      value="Infos indisponibles",                      inline=True)

        # Dernière connexion
        e.add_field(name="\u200b", value="**── Dernière connexion ──**",                              inline=False)
        e.add_field(name="🕐 Dernière use",     value=kd.get("last_use", "Jamais")[:19] if kd.get("last_use") else "Jamais", inline=True)
        e.add_field(name="👤 Dernier username", value=kd.get("last_username", "?"),                   inline=True)
        e.add_field(name="🌐 Dernière IP",      value=f"||`{kd.get('last_ip', '?')}`||",             inline=True)
        e.add_field(name="🌐 Première IP",      value=f"||`{kd.get('first_ip', '?')}`||",            inline=True)
    else:
        e.add_field(name="🔓 Lock",  value="Pas encore utilisée",  inline=False)

    await ctx.send(embed=e)

# ──────────────────────────────────────────────
#  +list
# ──────────────────────────────────────────────
@bot.command(name="list")
async def cmd_list(ctx):
    if not is_owner_or_admin(ctx):
        await ctx.send(embed=zayt_embed("❌ Refusé", "Tu n'as pas la permission.", 0xFF4444))
        return
    keys = load_keys()
    if not keys:
        await ctx.send(embed=zayt_embed("📋 Aucune clé", "Aucune clé dans la base."))
        return
    lines = []
    for k, v in list(keys.items())[:20]:
        exp  = fmt_date(datetime.fromisoformat(v["expires"])) if v.get("expires") else "Lifetime"
        icon = "🔨" if v.get("banned") else ("❌" if is_expired(v) else "✅")
        lock = v.get("locked_username") or "Non utilisée"
        lines.append(f"{icon} `{k}`\n   👤 {lock} — {exp} — {v.get('uses',0)} uses")
    extra = f"\n*+ {len(keys)-20} autres...*" if len(keys) > 20 else ""
    await ctx.send(embed=zayt_embed(f"📋 {len(keys)} clés", "\n".join(lines) + extra))

# ──────────────────────────────────────────────
#  +help
# ──────────────────────────────────────────────
@bot.command(name="help")
async def cmd_help(ctx):
    e = zayt_embed("📖 Zayt Export — Commandes", "")
    e.add_field(name="+add <durée> [note]",      value="Crée une clé  ex: `+add lifetime VIP` `+add 30d`",         inline=False)
    e.add_field(name="+remove <clé>",            value="Supprime une clé",                                          inline=False)
    e.add_field(name="+ban <clé>",               value="Banne une clé (invalide mais gardée)",                      inline=False)
    e.add_field(name="+unban <clé>",             value="Débanne une clé",                                           inline=False)
    e.add_field(name="+unlock <clé>",            value="Retire le lock UserId (si le mec change de compte)",        inline=False)
    e.add_field(name="+time <clé> <+/-durée>",   value="Ajoute/retire du temps  ex: `+time ZAYT-XXXX +365d`",      inline=False)
    e.add_field(name="+info <clé>",              value="Toutes les infos : UserId, username Roblox, IP, etc.",      inline=False)
    e.add_field(name="+list",                    value="Liste toutes les clés",                                     inline=False)
    e.add_field(name="📅 Formats durée",         value="`7d` `30d` `365d` `12h` `30m` `lifetime` `perm`",          inline=False)
    await ctx.send(embed=e)

# ──────────────────────────────────────────────
#  EVENTS
# ──────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"╔═══════════════════════════════════╗")
    print(f"║   Zayt Export Bot — ONLINE ✅     ║")
    print(f"║   Connecté : {bot.user}   ║")
    print(f"╚═══════════════════════════════════╝")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name="Zayt Export 🔑"))

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    await ctx.send(embed=zayt_embed("❌ Erreur", str(error), 0xFF4444))

# ══════════════════════════════════════════════
#  LANCEMENT
# ══════════════════════════════════════════════
if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print(f"[Flask] Serveur HTTP démarré sur le port {PORT}")
    bot.run(DISCORD_TOKEN)