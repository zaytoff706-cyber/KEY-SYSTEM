"""
╔══════════════════════════════════════════════════════╗
║           ZAYT EXPORT — BOT v3.0                    ║
║   IP Lookup | Logs fichier | Infos maximales        ║
╚══════════════════════════════════════════════════════╝
pip install discord.py flask requests
"""

import discord
from discord.ext import commands
import json, os, random, string, threading, requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify

# ══════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════
DISCORD_TOKEN   = "MTQ3OTk0MDQzMDEzNjIxNzcxMg.G18wsK.5AhA5IX4RooeFGreO86Yv5Hc5oPnuuKQClIqvA"
BOT_PREFIX      = "+"
OWNER_IDS       = [1302360384111640646]
ADMIN_ROLE_NAME = "Admin"
PORT            = 5000
KEYS_FILE       = "keys.json"
LOGS_FILE       = "logs.json"      # log de toutes les connexions
LOGS_TXT        = "logs.txt"       # version lisible sur ton PC

# ══════════════════════════════════════════════
#  GESTION CLÉS
# ══════════════════════════════════════════════
def load_keys():
    if not os.path.exists(KEYS_FILE): return {}
    with open(KEYS_FILE) as f: return json.load(f)

def save_keys(data):
    with open(KEYS_FILE, "w") as f: json.dump(data, f, indent=2)

def load_logs():
    if not os.path.exists(LOGS_FILE): return []
    with open(LOGS_FILE) as f: return json.load(f)

def save_log(entry):
    """Sauvegarde dans logs.json ET logs.txt lisible"""
    logs = load_logs()
    logs.append(entry)
    with open(LOGS_FILE, "w") as f:
        json.dump(logs, f, indent=2)
    # Version texte lisible
    line = (
        f"[{entry['timestamp']}] "
        f"KEY={entry['key']} | "
        f"UserId={entry['userId']} | "
        f"Username={entry['username']} | "
        f"IP={entry['ip']} | "
        f"Pays={entry.get('ip_country','?')} | "
        f"Ville={entry.get('ip_city','?')} | "
        f"FAI={entry.get('ip_isp','?')} | "
        f"Jeu={entry.get('game','?')} | "
        f"PlaceId={entry.get('placeId','?')}\n"
    )
    with open(LOGS_TXT, "a", encoding="utf-8") as f:
        f.write(line)

def gen_key():
    parts = ["".join(random.choices(string.ascii_uppercase + string.digits, k=4)) for _ in range(3)]
    return "ZAYT-" + "-".join(parts)

def parse_duration(s):
    s = s.strip().lower()
    if s in ("perm","permanent","inf","forever","lifetime"): return None
    if s.endswith("d"): return timedelta(days=int(s[:-1]))
    if s.endswith("h"): return timedelta(hours=int(s[:-1]))
    if s.endswith("m"): return timedelta(minutes=int(s[:-1]))
    raise ValueError(f"Durée invalide : {s}")

def fmt_date(dt):
    if dt is None: return "♾️ Lifetime"
    return dt.strftime("%d/%m/%Y %H:%M")

def is_expired(kd):
    if kd.get("banned"): return True
    exp = kd.get("expires")
    if exp is None: return False
    return datetime.utcnow() > datetime.fromisoformat(exp)

# ══════════════════════════════════════════════
#  IP LOOKUP (ip-api.com — gratuit, 45 req/min)
# ══════════════════════════════════════════════
def ip_lookup(ip):
    """Retourne toutes les infos publiques d'une IP"""
    try:
        # On évite de lookup les IPs locales
        if ip in ("127.0.0.1", "localhost", "::1") or ip.startswith("192.168") or ip.startswith("10."):
            return {"error": "IP locale"}
        r = requests.get(
            f"http://ip-api.com/json/{ip}",
            params={"fields": "status,country,countryCode,regionName,city,zip,lat,lon,timezone,isp,org,as,query"},
            timeout=5
        )
        if r.status_code == 200:
            d = r.json()
            if d.get("status") == "success":
                return d
    except Exception:
        pass
    return None

# ══════════════════════════════════════════════
#  ROBLOX API
# ══════════════════════════════════════════════
def get_roblox_info(user_id):
    try:
        r = requests.get(f"https://users.roblox.com/v1/users/{user_id}", timeout=5)
        if r.status_code == 200:
            d = r.json()
            return {
                "username":    d.get("name","?"),
                "displayName": d.get("displayName","?"),
                "created":     d.get("created","?")[:10] if d.get("created") else "?",
                "isBanned":    d.get("isBanned", False),
                "description": (d.get("description","") or "")[:80],
            }
    except Exception:
        pass
    return None

def get_roblox_friends_count(user_id):
    try:
        r = requests.get(f"https://friends.roblox.com/v1/users/{user_id}/friends/count", timeout=5)
        if r.status_code == 200:
            return r.json().get("count", "?")
    except Exception:
        pass
    return "?"

def get_roblox_badges_count(user_id):
    try:
        r = requests.get(f"https://accountinformation.roblox.com/v1/users/{user_id}/roblox-badges", timeout=5)
        if r.status_code == 200:
            return len(r.json())
    except Exception:
        pass
    return "?"

# ══════════════════════════════════════════════
#  SERVEUR FLASK
# ══════════════════════════════════════════════
app = Flask(__name__)

@app.route("/verify", methods=["POST"])
def verify():
    body     = request.get_json(force=True, silent=True) or {}
    key      = str(body.get("key","")).upper().strip()
    user_id  = str(body.get("userId","")).strip()
    username = str(body.get("username","?")).strip()
    game_name= str(body.get("game","?")).strip()
    place_id = str(body.get("placeId","?")).strip()
    ip_addr  = request.headers.get("X-Forwarded-For", request.remote_addr)
    if "," in ip_addr:
        ip_addr = ip_addr.split(",")[0].strip()

    keys = load_keys()
    if key not in keys:
        return jsonify({"valid":False,"message":"Clé introuvable"}), 200

    kd = keys[key]
    if kd.get("banned"):
        return jsonify({"valid":False,"message":"Clé bannie"}), 200

    exp = kd.get("expires")
    if exp and datetime.utcnow() > datetime.fromisoformat(exp):
        return jsonify({"valid":False,"message":"Clé expirée"}), 200

    # IP Lookup
    ip_info = ip_lookup(ip_addr)

    # Lock UserId
    locked_id = kd.get("locked_userid")
    if locked_id is None:
        if not user_id:
            return jsonify({"valid":False,"message":"UserId manquant"}), 200
        kd["locked_userid"]   = user_id
        kd["locked_username"] = username
        kd["locked_at"]       = datetime.utcnow().isoformat()
        kd["first_ip"]        = ip_addr
        kd["first_ip_info"]   = ip_info
    elif locked_id != user_id:
        return jsonify({"valid":False,"message":"Clé déjà utilisée par quelqu'un d'autre"}), 200

    # Mise à jour infos
    kd["last_use"]      = datetime.utcnow().isoformat()
    kd["last_username"] = username
    kd["last_ip"]       = ip_addr
    kd["last_ip_info"]  = ip_info
    kd["uses"]          = kd.get("uses",0) + 1

    save_keys(keys)

    # Log entrée
    log_entry = {
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "key":       key,
        "userId":    user_id,
        "username":  username,
        "ip":        ip_addr,
        "game":      game_name,
        "placeId":   place_id,
        "ip_country": ip_info.get("country","?")    if ip_info else "?",
        "ip_city":    ip_info.get("city","?")        if ip_info else "?",
        "ip_isp":     ip_info.get("isp","?")         if ip_info else "?",
        "ip_region":  ip_info.get("regionName","?")  if ip_info else "?",
        "ip_timezone":ip_info.get("timezone","?")    if ip_info else "?",
        "ip_lat":     ip_info.get("lat","?")         if ip_info else "?",
        "ip_lon":     ip_info.get("lon","?")         if ip_info else "?",
    }
    save_log(log_entry)

    exp_display = fmt_date(datetime.fromisoformat(exp) if exp else None)
    return jsonify({"valid":True,"message":"OK","expires":exp_display,"uses":kd["uses"],"note":kd.get("note","")}), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status":"online","keys":len(load_keys())}), 200

def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ══════════════════════════════════════════════
#  BOT DISCORD
# ══════════════════════════════════════════════
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents, help_command=None)

def is_owner_or_admin(ctx):
    if ctx.author.id in OWNER_IDS: return True
    return any(r.name == ADMIN_ROLE_NAME for r in ctx.author.roles)

def zayt_embed(title, desc, color=0x9B3FFF):
    e = discord.Embed(title=title, description=desc, color=color)
    e.set_footer(text="Zayt Export System")
    e.timestamp = datetime.utcnow()
    return e

def fmt_ip_block(ip, ip_info):
    """Formate un bloc d'infos IP proprement"""
    if not ip_info:
        return f"||`{ip}`||"
    flag = ""
    code = ip_info.get("countryCode","")
    if code:
        # Convertit code pays en emoji drapeau
        try:
            flag = chr(ord(code[0])+127397) + chr(ord(code[1])+127397) + " "
        except Exception:
            flag = ""
    return (
        f"||`{ip}`||\n"
        f"{flag}{ip_info.get('country','?')} — {ip_info.get('regionName','?')} — {ip_info.get('city','?')}\n"
        f"📡 FAI : {ip_info.get('isp','?')}\n"
        f"🏢 Org : {ip_info.get('org','?')}\n"
        f"🕐 Timezone : {ip_info.get('timezone','?')}\n"
        f"📍 Coords : {ip_info.get('lat','?')}, {ip_info.get('lon','?')}"
    )

# ──────────────────────────────────────────────
#  +add
# ──────────────────────────────────────────────
@bot.command(name="add")
async def cmd_add(ctx, duration: str = "lifetime", *, note: str = ""):
    if not is_owner_or_admin(ctx):
        await ctx.send(embed=zayt_embed("❌ Refusé","Tu n'as pas la permission.",0xFF4444)); return
    try: delta = parse_duration(duration)
    except ValueError as e:
        await ctx.send(embed=zayt_embed("❌ Erreur",str(e),0xFF4444)); return
    keys = load_keys()
    key  = gen_key()
    expires_dt  = (datetime.utcnow() + delta) if delta else None
    keys[key] = {
        "created": datetime.utcnow().isoformat(), "expires": expires_dt.isoformat() if expires_dt else None,
        "banned": False, "note": note, "uses": 0, "created_by": str(ctx.author),
        "locked_userid": None, "locked_username": None, "locked_at": None,
        "first_ip": None, "first_ip_info": None,
        "last_use": None, "last_username": None, "last_ip": None, "last_ip_info": None,
    }
    save_keys(keys)
    e = zayt_embed("✅ Clé créée", f"```\n{key}\n```")
    e.add_field(name="⏱ Durée",   value=duration.upper(),      inline=True)
    e.add_field(name="📅 Expire", value=fmt_date(expires_dt),   inline=True)
    e.add_field(name="🔒 Lock",   value="Au 1er usage",         inline=True)
    if note: e.add_field(name="📝 Note", value=note, inline=False)
    await ctx.send(embed=e)

# ──────────────────────────────────────────────
#  +remove / +ban / +unban / +unlock
# ──────────────────────────────────────────────
@bot.command(name="remove")
async def cmd_remove(ctx, key: str = ""):
    if not is_owner_or_admin(ctx): return
    key = key.upper().strip(); keys = load_keys()
    if key not in keys:
        await ctx.send(embed=zayt_embed("❌ Introuvable",f"`{key}` n'existe pas.",0xFF4444)); return
    del keys[key]; save_keys(keys)
    await ctx.send(embed=zayt_embed("🗑 Supprimée",f"```\n{key}\n```",0xFF8800))

@bot.command(name="ban")
async def cmd_ban(ctx, key: str = ""):
    if not is_owner_or_admin(ctx): return
    key = key.upper().strip(); keys = load_keys()
    if key not in keys:
        await ctx.send(embed=zayt_embed("❌ Introuvable","Clé introuvable.",0xFF4444)); return
    keys[key]["banned"] = True; save_keys(keys)
    await ctx.send(embed=zayt_embed("🔨 Bannie",f"```\n{key}\n```",0xFF4444))

@bot.command(name="unban")
async def cmd_unban(ctx, key: str = ""):
    if not is_owner_or_admin(ctx): return
    key = key.upper().strip(); keys = load_keys()
    if key not in keys:
        await ctx.send(embed=zayt_embed("❌ Introuvable","Clé introuvable.",0xFF4444)); return
    keys[key]["banned"] = False; save_keys(keys)
    await ctx.send(embed=zayt_embed("✅ Débannie",f"```\n{key}\n```",0x44FF88))

@bot.command(name="unlock")
async def cmd_unlock(ctx, key: str = ""):
    if not is_owner_or_admin(ctx): return
    key = key.upper().strip(); keys = load_keys()
    if key not in keys:
        await ctx.send(embed=zayt_embed("❌ Introuvable","Clé introuvable.",0xFF4444)); return
    old = keys[key].get("locked_username","?")
    keys[key].update({"locked_userid":None,"locked_username":None,"locked_at":None})
    save_keys(keys)
    e = zayt_embed("🔓 Lock retiré",f"```\n{key}\n```",0x9B3FFF)
    e.add_field(name="Ancien user",value=old,inline=True)
    await ctx.send(embed=e)

@bot.command(name="time")
async def cmd_time(ctx, key: str = "", duration: str = ""):
    if not is_owner_or_admin(ctx): return
    key = key.upper().strip(); keys = load_keys()
    if key not in keys:
        await ctx.send(embed=zayt_embed("❌ Introuvable","Clé introuvable.",0xFF4444)); return
    sign=1; dur=duration
    if duration.startswith("+"): dur=duration[1:]
    elif duration.startswith("-"): sign=-1; dur=duration[1:]
    try: delta=parse_duration(dur)
    except ValueError as e:
        await ctx.send(embed=zayt_embed("❌ Erreur",str(e),0xFF4444)); return
    kd=keys[key]
    if delta is None:
        kd["expires"]=None; save_keys(keys)
        await ctx.send(embed=zayt_embed("♾️ Lifetime",f"```\n{key}\n```",0x9B3FFF)); return
    exp=kd.get("expires")
    current=datetime.fromisoformat(exp) if exp else datetime.utcnow()
    new_exp=current+(sign*delta); kd["expires"]=new_exp.isoformat(); save_keys(keys)
    e=zayt_embed(f"⏱ Temps {'ajouté' if sign==1 else 'retiré'}",f"```\n{key}\n```")
    e.add_field(name="Modification",value=f"`{duration}`",inline=True)
    e.add_field(name="Nouvelle expiration",value=fmt_date(new_exp),inline=True)
    await ctx.send(embed=e)

# ──────────────────────────────────────────────
#  +info — VERSION MAXIMALE
# ──────────────────────────────────────────────
@bot.command(name="info")
async def cmd_info(ctx, key: str = ""):
    if not is_owner_or_admin(ctx):
        await ctx.send(embed=zayt_embed("❌ Refusé","Tu n'as pas la permission.",0xFF4444)); return

    key = key.upper().strip(); keys = load_keys()
    if key not in keys:
        await ctx.send(embed=zayt_embed("❌ Introuvable","Clé introuvable.",0xFF4444)); return

    kd = keys[key]
    expired = is_expired(kd)
    banned  = kd.get("banned",False)
    status  = "🔨 Bannie" if banned else ("❌ Expirée" if expired else "✅ Active")
    color   = 0xFF4444 if banned else (0xFF8800 if expired else 0x44FF88)

    e = zayt_embed("🔑 Info clé", f"```\n{key}\n```", color)

    # ── Infos clé ─────────────────────────────
    e.add_field(name="📊 Statut",       value=status,                                               inline=True)
    e.add_field(name="🔢 Utilisations", value=str(kd.get("uses",0)),                                inline=True)
    e.add_field(name="📅 Expiration",   value=fmt_date(datetime.fromisoformat(kd["expires"]) if kd.get("expires") else None), inline=True)
    e.add_field(name="🛠️ Créée par",   value=kd.get("created_by","?"),                             inline=True)
    e.add_field(name="📆 Créée le",     value=kd.get("created","?")[:19],                          inline=True)
    if kd.get("note"):
        e.add_field(name="📝 Note",     value=kd["note"],                                           inline=True)

    locked_id = kd.get("locked_userid")
    if locked_id:
        # ── Infos Roblox ──────────────────────
        e.add_field(name="\u200b", value="**━━━ 🎮 COMPTE ROBLOX ━━━**", inline=False)
        e.add_field(name="🆔 UserId",          value=f"`{locked_id}`",                             inline=True)
        e.add_field(name="🔒 Locké le",        value=(kd.get("locked_at","?") or "?")[:19],        inline=True)
        e.add_field(name="🔗 Profil",
            value=f"[Voir profil](https://www.roblox.com/users/{locked_id}/profile)",              inline=True)

        rblx = get_roblox_info(locked_id)
        if rblx:
            e.add_field(name="👤 Username",      value=rblx["username"],                           inline=True)
            e.add_field(name="📛 Display Name",  value=rblx["displayName"],                        inline=True)
            e.add_field(name="📅 Compte créé",   value=rblx["created"],                            inline=True)
            e.add_field(name="🚫 Compte banni",  value="⚠️ OUI" if rblx["isBanned"] else "Non",   inline=True)
            friends = get_roblox_friends_count(locked_id)
            e.add_field(name="👥 Amis",          value=str(friends),                               inline=True)
            if rblx["description"]:
                e.add_field(name="📄 Bio",       value=rblx["description"],                        inline=False)

        # ── IP Première connexion ─────────────
        first_ip      = kd.get("first_ip","?")
        first_ip_info = kd.get("first_ip_info")
        e.add_field(name="\u200b", value="**━━━ 🌐 PREMIÈRE CONNEXION ━━━**", inline=False)
        e.add_field(name="🌐 IP",  value=fmt_ip_block(first_ip, first_ip_info), inline=False)

        # ── Dernière connexion ────────────────
        last_ip      = kd.get("last_ip","?")
        last_ip_info = kd.get("last_ip_info")
        e.add_field(name="\u200b", value="**━━━ 🕐 DERNIÈRE CONNEXION ━━━**", inline=False)
        e.add_field(name="🕐 Date",     value=(kd.get("last_use","Jamais") or "Jamais")[:19],      inline=True)
        e.add_field(name="👤 Username", value=kd.get("last_username","?"),                         inline=True)
        e.add_field(name="\u200b",      value="\u200b",                                            inline=True)
        if last_ip != first_ip:
            e.add_field(name="🌐 IP",   value=fmt_ip_block(last_ip, last_ip_info),                 inline=False)
        else:
            e.add_field(name="🌐 IP",   value="Identique à la première connexion",                 inline=False)
    else:
        e.add_field(name="🔓 Statut lock", value="Pas encore utilisée — en attente du 1er usage", inline=False)

    await ctx.send(embed=e)

# ──────────────────────────────────────────────
#  +logs [n]  — Affiche les dernières connexions
# ──────────────────────────────────────────────
@bot.command(name="logs")
async def cmd_logs(ctx, nb: int = 10):
    if not is_owner_or_admin(ctx):
        await ctx.send(embed=zayt_embed("❌ Refusé","Tu n'as pas la permission.",0xFF4444)); return

    logs = load_logs()
    if not logs:
        await ctx.send(embed=zayt_embed("📋 Aucun log","Aucune connexion enregistrée.")); return

    recent = logs[-nb:][::-1]  # nb derniers, plus récent en premier
    lines = []
    for l in recent:
        flag = ""
        code = (l.get("ip_country") or "")
        # Emoji drapeau depuis le nom du pays (approximatif)
        lines.append(
            f"**{l['timestamp']}**\n"
            f"👤 `{l['username']}` (ID: `{l['userId']}`)\n"
            f"🌐 `{l['ip']}` — {l.get('ip_country','?')} / {l.get('ip_city','?')}\n"
            f"📡 {l.get('ip_isp','?')}\n"
            f"🎮 {l.get('game','?')} (`{l.get('placeId','?')}`)\n"
            f"🔑 `{l['key']}`\n"
            f"{'─'*30}"
        )

    # Découper si trop long
    desc = "\n".join(lines)
    if len(desc) > 3900:
        desc = desc[:3900] + "\n*...(tronqué)*"

    e = zayt_embed(f"📋 {min(nb,len(logs))} dernières connexions", desc, 0x9B3FFF)
    e.set_footer(text=f"Total logs : {len(logs)} | Zayt Export System")
    await ctx.send(embed=e)

# ──────────────────────────────────────────────
#  +list
# ──────────────────────────────────────────────
@bot.command(name="list")
async def cmd_list(ctx):
    if not is_owner_or_admin(ctx): return
    keys = load_keys()
    if not keys:
        await ctx.send(embed=zayt_embed("📋 Aucune clé","Aucune clé dans la base.")); return
    lines = []
    for k,v in list(keys.items())[:20]:
        exp  = fmt_date(datetime.fromisoformat(v["expires"])) if v.get("expires") else "Lifetime"
        icon = "🔨" if v.get("banned") else ("❌" if is_expired(v) else "✅")
        lock = v.get("locked_username") or "Non utilisée"
        lines.append(f"{icon} `{k}`\n   👤 {lock} — {exp} — {v.get('uses',0)} uses")
    extra = f"\n*+ {len(keys)-20} autres...*" if len(keys) > 20 else ""
    await ctx.send(embed=zayt_embed(f"📋 {len(keys)} clés","\n".join(lines)+extra))

# ──────────────────────────────────────────────
#  +help
# ──────────────────────────────────────────────
@bot.command(name="help")
async def cmd_help(ctx):
    e = zayt_embed("📖 Zayt Export v3.0 — Commandes","")
    e.add_field(name="+add <durée> [note]",      value="`+add lifetime VIP` / `+add 30d`",              inline=False)
    e.add_field(name="+info <clé>",              value="Infos complètes : Roblox + IP lookup + logs",   inline=False)
    e.add_field(name="+logs [n]",                value="Dernières connexions (défaut: 10)",              inline=False)
    e.add_field(name="+ban / +unban <clé>",      value="Bannir / débannir une clé",                     inline=False)
    e.add_field(name="+unlock <clé>",            value="Retire le lock UserId",                          inline=False)
    e.add_field(name="+remove <clé>",            value="Supprime une clé",                              inline=False)
    e.add_field(name="+time <clé> <+/-durée>",   value="`+time ZAYT-XXXX +30d`",                        inline=False)
    e.add_field(name="+list",                    value="Liste toutes les clés",                         inline=False)
    e.add_field(name="📅 Durées",                value="`7d` `30d` `365d` `12h` `perm` `lifetime`",     inline=False)
    e.add_field(name="📂 Fichiers locaux",       value="`logs.json` + `logs.txt` sur ton PC",           inline=False)
    await ctx.send(embed=e)

# ══════════════════════════════════════════════
#  EVENTS
# ══════════════════════════════════════════════
@bot.event
async def on_ready():
    print(f"╔══════════════════════════════╗")
    print(f"║  Zayt Export Bot v3 — ✅    ║")
    print(f"║  {bot.user}  ║")
    print(f"╚══════════════════════════════╝")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name="Zayt Export 🔑"))

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound): return
    await ctx.send(embed=zayt_embed("❌ Erreur",str(error),0xFF4444))

# ══════════════════════════════════════════════
#  LANCEMENT
# ══════════════════════════════════════════════
if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print(f"[Flask] Port {PORT} — prêt")
    bot.run(DISCORD_TOKEN)
