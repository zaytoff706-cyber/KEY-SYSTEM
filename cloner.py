"""
cloner.py : extension pour ton bot (discord.py 2.x, Python 3.10+).

Fonctions
  * Clonage complet d'un serveur (nom, icône, rôles, catégories, salons, emojis)
  * Clonage d'UNE catégorie (avec tous ses salons)
  * Backups avec ID (code) importables sur n'importe quel serveur
  * Stats arrivées / départs persistantes (survivent aux restarts)
  * Panneau de config en embeds + boutons + menus

Stockage sans base de données : un salon privé Discord (STORAGE_CHANNEL_ID).
Sans ce salon : fichiers locaux dans ./data (effacés à chaque redéploiement Render).
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import io
import json
import os
import secrets
import statistics
import time
from collections import Counter
from typing import Any, Optional

import discord
from discord.ext import commands, tasks

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x.isdigit()}
STORAGE_CHANNEL_ID = int(os.getenv("STORAGE_CHANNEL_ID", "0") or 0)
DATA_DIR = os.getenv("DATA_DIR", "data")
MAX_BACKUPS = 25
MAX_EVENTS = 20000
REASON = "Cloner Pro"

BLURPLE, GREEN, RED, YELLOW, DARK = 0x5865F2, 0x57F287, 0xED4245, 0xFEE75C, 0x2B2D31
FOOTER = "Cloner Pro"

TYPEMAP = {
    discord.ChannelType.text: "text",
    discord.ChannelType.voice: "voice",
    discord.ChannelType.stage_voice: "stage",
    discord.ChannelType.forum: "forum",
    discord.ChannelType.news: "news",
}
CH_ICON = {"text": "💬", "voice": "🔊", "stage": "🎙️", "forum": "🗂️", "news": "📣"}

RUNNING: set[int] = set()


# --------------------------------------------------------------------------
# Petits helpers
# --------------------------------------------------------------------------
def now() -> int:
    return int(time.time())


def bar(value: float, total: float, width: int = 10) -> str:
    if total <= 0:
        return "░" * width
    n = max(0, min(width, round(width * value / total)))
    return "█" * n + "░" * (width - n)


def pack(obj: Any) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode("utf-8"), 6)


def unpack(blob: bytes) -> Any:
    return json.loads(gzip.decompress(blob).decode("utf-8"))


def fmt_dur(seconds: float) -> str:
    s = int(seconds)
    for unit, div in (("j", 86400), ("h", 3600), ("min", 60)):
        if s >= div:
            return f"{s // div}{unit}"
    return f"{s}s"


def embed(title: str, desc: str = "", color: int = BLURPLE) -> discord.Embed:
    e = discord.Embed(title=title, description=desc, color=color)
    e.set_footer(text=FOOTER)
    return e


def err(desc: str) -> discord.Embed:
    return embed("❌ Impossible", desc, RED)


def admin_in(user_id: int, guild: discord.Guild) -> bool:
    if user_id in OWNER_IDS:
        return True
    m = guild.get_member(user_id)
    return bool(m and m.guild_permissions.administrator)


def missing_perms(guild: discord.Guild) -> list[str]:
    p = guild.me.guild_permissions
    if p.administrator:
        return []
    need = {
        "Gérer les rôles": p.manage_roles,
        "Gérer les salons": p.manage_channels,
        "Gérer le serveur": p.manage_guild,
    }
    return [name for name, ok in need.items() if not ok]


# --------------------------------------------------------------------------
# Stockage persistant (salon Discord privé ou fichiers locaux)
# --------------------------------------------------------------------------
class Storage:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.mode = "discord" if STORAGE_CHANNEL_ID else "local"
        self.channel: Optional[discord.abc.Messageable] = None
        self.state_msg: Optional[discord.Message] = None
        self.data: dict[str, Any] = {"guilds": {}, "backups": {}}
        self.dirty = False
        self.lock = asyncio.Lock()

    # ---- chargement
    async def load(self) -> None:
        if self.mode == "discord":
            try:
                self.channel = self.bot.get_channel(STORAGE_CHANNEL_ID) or await self.bot.fetch_channel(
                    STORAGE_CHANNEL_ID
                )
            except discord.HTTPException:
                print("[cloner] Salon de stockage introuvable -> stockage local (non persistant sur Render).")
                self.mode = "local"

        if self.mode == "discord":
            msg = await self._find_state()
            if msg and msg.attachments:
                self.state_msg = msg
                self.data.update(unpack(await msg.attachments[0].read()))
        else:
            os.makedirs(DATA_DIR, exist_ok=True)
            path = os.path.join(DATA_DIR, "state.json.gz")
            if os.path.exists(path):
                with open(path, "rb") as f:
                    self.data.update(unpack(f.read()))
        print(f"[cloner] Stockage : {self.mode} | serveurs suivis : {len(self.data['guilds'])}")

    async def _find_state(self) -> Optional[discord.Message]:
        me = self.bot.user.id
        try:
            res = self.channel.pins()
            pins = [p async for p in res] if hasattr(res, "__aiter__") else await res
            for p in pins:
                m = getattr(p, "message", p)
                if m.author.id == me and m.content.startswith("STATE"):
                    return m
        except discord.HTTPException:
            pass
        try:
            async for m in self.channel.history(limit=200):
                if m.author.id == me and m.content.startswith("STATE"):
                    return m
        except discord.HTTPException:
            pass
        return None

    # ---- sauvegarde de l'état (stats)
    async def flush(self) -> None:
        if not self.dirty:
            return
        async with self.lock:
            self.dirty = False
            blob = pack(self.data)
            try:
                if self.mode == "discord":
                    f = discord.File(io.BytesIO(blob), filename="state.json.gz")
                    content = f"STATE • maj <t:{now()}:R>"
                    if self.state_msg:
                        self.state_msg = await self.state_msg.edit(content=content, attachments=[f])
                    else:
                        self.state_msg = await self.channel.send(content, file=f)
                        try:
                            await self.state_msg.pin()
                        except discord.HTTPException:
                            pass
                else:
                    os.makedirs(DATA_DIR, exist_ok=True)
                    tmp = os.path.join(DATA_DIR, "state.tmp")
                    with open(tmp, "wb") as fh:
                        fh.write(blob)
                    os.replace(tmp, os.path.join(DATA_DIR, "state.json.gz"))
            except discord.NotFound:
                self.state_msg = None
                self.dirty = True
            except Exception as e:  # noqa: BLE001
                self.dirty = True
                print(f"[cloner] Sauvegarde échouée : {e!r}")

    # ---- fichiers (backups)
    async def put_blob(self, name: str, blob: bytes) -> str:
        if self.mode == "discord":
            m = await self.channel.send(
                f"BACKUP {name}", file=discord.File(io.BytesIO(blob), filename=f"{name}.json.gz")
            )
            return str(m.id)
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, f"{name}.json.gz"), "wb") as f:
            f.write(blob)
        return name

    async def get_blob(self, ref: str) -> bytes:
        if self.mode == "discord":
            m = await self.channel.fetch_message(int(ref))
            return await m.attachments[0].read()
        with open(os.path.join(DATA_DIR, f"{ref}.json.gz"), "rb") as f:
            return f.read()

    async def del_blob(self, ref: str) -> None:
        try:
            if self.mode == "discord":
                m = await self.channel.fetch_message(int(ref))
                await m.delete()
            else:
                os.remove(os.path.join(DATA_DIR, f"{ref}.json.gz"))
        except (discord.HTTPException, OSError):
            pass

    @property
    def file_limit(self) -> int:
        if self.mode == "discord" and getattr(self.channel, "guild", None):
            return self.channel.guild.filesize_limit - 4096
        return 8 * 1024 * 1024


# --------------------------------------------------------------------------
# Snapshot d'un serveur
# --------------------------------------------------------------------------
def ser_ow(ch: discord.abc.GuildChannel) -> list[dict]:
    out = []
    for target, ow in ch.overwrites.items():
        if isinstance(target, discord.Role):
            allow, deny = ow.pair()
            out.append({"r": target.id, "n": target.name, "a": allow.value, "d": deny.value})
    return out


def ser_channel(ch: discord.abc.GuildChannel, kind: str) -> dict:
    d: dict[str, Any] = {"name": ch.name, "type": kind, "ow": ser_ow(ch)}
    if kind in ("text", "news", "forum"):
        d["topic"] = getattr(ch, "topic", None)
        d["nsfw"] = getattr(ch, "nsfw", False)
        d["slow"] = getattr(ch, "slowmode_delay", 0)
    if kind in ("voice", "stage"):
        d["bitrate"] = ch.bitrate
        d["limit"] = ch.user_limit
    return d


async def snapshot_guild(guild: discord.Guild, assets: bool = True) -> dict:
    snap: dict[str, Any] = {
        "v": 2,
        "gid": guild.id,
        "name": guild.name,
        "ts": now(),
        "everyone": guild.default_role.permissions.value,
        "icon": None,
        "roles": [],
        "layout": [],
        "emojis": [],
    }
    if assets and guild.icon:
        try:
            snap["icon"] = base64.b64encode(await guild.icon.read()).decode()
        except discord.HTTPException:
            pass

    roles = [r for r in guild.roles if not r.is_default() and not r.managed]
    for r in sorted(roles, key=lambda r: r.position, reverse=True):
        snap["roles"].append(
            {
                "id": r.id,
                "name": r.name,
                "color": r.colour.value,
                "hoist": r.hoist,
                "mention": r.mentionable,
                "perms": r.permissions.value,
            }
        )

    for cat, chans in guild.by_category():
        items = [ser_channel(c, TYPEMAP[c.type]) for c in chans if c.type in TYPEMAP]
        snap["layout"].append(
            {"cat": {"name": cat.name, "ow": ser_ow(cat)} if cat else None, "channels": items}
        )

    if assets:
        sem = asyncio.Semaphore(8)

        async def grab(e: discord.Emoji):
            async with sem:
                try:
                    return {
                        "name": e.name,
                        "animated": e.animated,
                        "img": base64.b64encode(await e.read()).decode(),
                    }
                except discord.HTTPException:
                    return None

        snap["emojis"] = [x for x in await asyncio.gather(*(grab(e) for e in guild.emojis)) if x]
    return snap


def snap_counts(snap: dict) -> tuple[int, int, int, int]:
    cats = sum(1 for l in snap["layout"] if l["cat"])
    chans = sum(len(l["channels"]) for l in snap["layout"])
    return len(snap["roles"]), cats, chans, len(snap["emojis"])


# --------------------------------------------------------------------------
# Progression (embed mis à jour sans spam d'API)
# --------------------------------------------------------------------------
class Progress:
    LABELS = {
        "read": "Lecture du serveur source",
        "clean": "Nettoyage du serveur",
        "identity": "Nom & icône",
        "roles": "Rôles",
        "channels": "Catégories & salons",
        "emojis": "Emojis",
    }

    def __init__(self, interaction: discord.Interaction, title: str):
        self.i = interaction
        self.title = title
        self.steps = {k: {"on": False, "active": False, "done": 0, "total": 0} for k in self.LABELS}
        self.last = 0.0

    def begin(self, key: str, total: int) -> None:
        s = self.steps[key]
        s.update(on=True, active=True, done=0, total=total)

    async def advance(self, key: str, n: int = 1) -> None:
        self.steps[key]["done"] += n
        await self.push()

    async def finish(self, key: str) -> None:
        s = self.steps[key]
        s.update(active=False, done=s["total"])
        await self.push(force=True)

    def embed(self) -> discord.Embed:
        lines = []
        for k, s in self.steps.items():
            if not s["on"]:
                continue
            icon = "✅" if (not s["active"] and s["on"]) else "⏳"
            lines.append(f"{icon} **{self.LABELS[k]}**\n`{bar(s['done'], s['total'], 14)}` {s['done']}/{s['total']}")
        e = embed(self.title, "\n\n".join(lines) or "Démarrage…", YELLOW)
        e.add_field(name="ℹ️", value="Ne ferme pas ce message. Les pauses sont normales (anti rate-limit).")
        return e

    async def push(self, force: bool = False) -> None:
        if not force and time.monotonic() - self.last < 1.5:
            return
        self.last = time.monotonic()
        try:
            await self.i.edit_original_response(embed=self.embed(), view=None)
        except discord.HTTPException:
            pass


# --------------------------------------------------------------------------
# Moteur de clonage
# --------------------------------------------------------------------------
class Engine:
    def __init__(self, target: discord.Guild, snap: dict, opts: dict, only_cat: Optional[int],
                 progress: Progress, keep_ids: set[int]):
        self.t = target
        self.snap = snap
        self.opts = opts
        self.only_cat = only_cat
        self.p = progress
        self.keep = keep_ids
        self.rmap: dict[int, discord.Role] = {}
        self.roles_by_id = {r["id"]: r for r in snap["roles"]}
        self.res = {"roles": 0, "categories": 0, "channels": 0, "emojis": 0, "skipped": 0, "warnings": []}

    def warn(self, msg: str) -> None:
        if len(self.res["warnings"]) < 12:
            self.res["warnings"].append(msg)

    async def run(self) -> dict:
        full = self.only_cat is None
        if full and self.opts["clean"]:
            await self.clean()
        if full and self.opts["identity"]:
            await self.identity()
        if full and self.opts["roles"]:
            await self.roles()
        if self.opts["channels"] or not full:
            await self.channels()
        if full and self.opts["emojis"]:
            await self.emojis()
        return self.res

    # ---- étapes
    async def clean(self) -> None:
        t = self.t
        chans = [c for c in t.channels if c.id not in self.keep]
        top = t.me.top_role.position
        roles = [r for r in t.roles if not r.is_default() and not r.managed and r.position < top]
        self.p.begin("clean", len(chans) + len(roles))
        for c in chans:
            try:
                await c.delete(reason=REASON)
            except discord.HTTPException:
                self.warn(f"Salon non supprimable : #{c.name}")
            await self.p.advance("clean")
            await asyncio.sleep(0.5)
        for r in roles:
            try:
                await r.delete(reason=REASON)
            except discord.HTTPException:
                self.warn(f"Rôle non supprimable : @{r.name}")
            await self.p.advance("clean")
            await asyncio.sleep(0.5)
        await self.p.finish("clean")

    async def identity(self) -> None:
        self.p.begin("identity", 1)
        kw: dict[str, Any] = {"name": self.snap["name"], "reason": REASON}
        if self.snap.get("icon"):
            kw["icon"] = base64.b64decode(self.snap["icon"])
        try:
            await self.t.edit(**kw)
        except discord.HTTPException as e:
            self.warn(f"Nom/icône refusés ({e.status})")
        await self.p.advance("identity")
        await self.p.finish("identity")

    async def create_role(self, rd: dict) -> Optional[discord.Role]:
        me_perms = self.t.me.guild_permissions
        perms = discord.Permissions(rd["perms"])
        if not me_perms.administrator:
            perms = discord.Permissions(perms.value & me_perms.value)
        try:
            role = await self.t.create_role(
                name=rd["name"], permissions=perms, colour=discord.Colour(rd["color"]),
                hoist=rd["hoist"], mentionable=rd["mention"], reason=REASON,
            )
        except discord.HTTPException as e:
            self.warn(f"Rôle @{rd['name']} refusé ({e.status})")
            return None
        self.rmap[rd["id"]] = role
        self.res["roles"] += 1
        await asyncio.sleep(0.7)
        return role

    async def roles(self) -> None:
        t = self.t
        self.p.begin("roles", len(self.snap["roles"]))
        try:
            perms = discord.Permissions(self.snap["everyone"])
            if not t.me.guild_permissions.administrator:
                perms = discord.Permissions(perms.value & t.me.guild_permissions.value)
            await t.default_role.edit(permissions=perms, reason=REASON)
        except discord.HTTPException:
            self.warn("Permissions de @everyone non appliquées")

        existing = {r.name.lower(): r for r in t.roles if not r.is_default() and not r.managed}
        for rd in self.snap["roles"]:
            found = existing.get(rd["name"].lower())
            if found:
                self.rmap[rd["id"]] = found
                self.res["skipped"] += 1
            else:
                await self.create_role(rd)
            await self.p.advance("roles")
        await self.p.finish("roles")

    async def resolve_role(self, old_id: int, name: str) -> Optional[discord.Role]:
        if old_id == self.snap["gid"]:
            return self.t.default_role
        if old_id in self.rmap:
            return self.rmap[old_id]
        found = discord.utils.find(
            lambda r: r.name.lower() == name.lower() and not r.managed, self.t.roles
        )
        if found:
            self.rmap[old_id] = found
            return found
        rd = self.roles_by_id.get(old_id)
        if rd and (self.only_cat is not None or self.opts["roles"]):
            return await self.create_role(rd)
        return None

    async def build_ow(self, ow_list: list[dict]) -> dict:
        out = {}
        for o in ow_list:
            role = await self.resolve_role(o["r"], o["n"])
            if role:
                out[role] = discord.PermissionOverwrite.from_pair(
                    discord.Permissions(o["a"]), discord.Permissions(o["d"])
                )
        return out

    async def _create(self, cd: dict, cat: Optional[discord.CategoryChannel], ow: dict):
        t, kind = self.t, cd["type"]
        base: dict[str, Any] = {"name": cd["name"], "overwrites": ow, "reason": REASON}
        if cat:
            base["category"] = cat
        topic, nsfw, slow = cd.get("topic"), cd.get("nsfw", False), cd.get("slow", 0)

        if kind in ("text", "news"):
            if kind == "news":
                try:
                    return await t.create_text_channel(topic=topic, nsfw=nsfw, slowmode_delay=slow, news=True, **base)
                except discord.HTTPException:
                    pass
            return await t.create_text_channel(topic=topic, nsfw=nsfw, slowmode_delay=slow, **base)

        if kind == "forum":
            try:
                return await t.create_forum(topic=topic or None, nsfw=nsfw, slowmode_delay=slow, **base)
            except discord.HTTPException:
                return await t.create_text_channel(topic=topic, nsfw=nsfw, slowmode_delay=slow, **base)

        bitrate = min(cd.get("bitrate", 64000), int(t.bitrate_limit))
        if kind == "stage":
            try:
                return await t.create_stage_channel(topic=topic or cd["name"], **base)
            except discord.HTTPException:
                pass
        return await t.create_voice_channel(bitrate=bitrate, user_limit=cd.get("limit", 0), **base)

    async def make_channel(self, cd: dict, cat: Optional[discord.CategoryChannel]):
        ow = await self.build_ow(cd["ow"])
        try:
            return await self._create(cd, cat, ow)
        except discord.HTTPException as e:
            if ow:
                try:
                    return await self._create(cd, cat, {})
                except discord.HTTPException as e2:
                    e = e2
            self.warn(f"#{cd['name']} refusé ({e.status})")
            return None

    async def channels(self) -> None:
        t = self.t
        layout = self.snap["layout"]
        if self.only_cat is not None:
            layout = [layout[self.only_cat]]
        total = sum(1 for l in layout if l["cat"]) + sum(len(l["channels"]) for l in layout)
        self.p.begin("channels", total)

        for entry in layout:
            cat_obj: Optional[discord.CategoryChannel] = None
            if entry["cat"]:
                cd = entry["cat"]
                cat_obj = discord.utils.find(lambda c: c.name.lower() == cd["name"].lower(), t.categories)
                if cat_obj:
                    self.res["skipped"] += 1
                else:
                    ow = await self.build_ow(cd["ow"])
                    try:
                        cat_obj = await t.create_category(cd["name"], overwrites=ow, reason=REASON)
                    except discord.HTTPException:
                        try:
                            cat_obj = await t.create_category(cd["name"], reason=REASON)
                        except discord.HTTPException as e:
                            self.warn(f"Catégorie {cd['name']} refusée ({e.status})")
                    if cat_obj:
                        self.res["categories"] += 1
                    await asyncio.sleep(0.8)
                await self.p.advance("channels")
                if cat_obj is None:
                    await self.p.advance("channels", len(entry["channels"]))
                    continue

            siblings = cat_obj.channels if cat_obj else [
                c for c in t.channels if c.category is None and not isinstance(c, discord.CategoryChannel)
            ]
            for cd in entry["channels"]:
                exists = any(c.name == cd["name"] and TYPEMAP.get(c.type) == cd["type"] for c in siblings)
                if exists:
                    self.res["skipped"] += 1
                elif await self.make_channel(cd, cat_obj):
                    self.res["channels"] += 1
                    await asyncio.sleep(0.8)
                await self.p.advance("channels")
        await self.p.finish("channels")

    async def emojis(self) -> None:
        t = self.t
        items = self.snap["emojis"]
        self.p.begin("emojis", len(items))
        have = {e.name for e in t.emojis}
        counts = {False: sum(1 for e in t.emojis if not e.animated), True: sum(1 for e in t.emojis if e.animated)}
        for ed in items:
            if ed["name"] in have:
                self.res["skipped"] += 1
            elif counts[ed["animated"]] >= t.emoji_limit:
                self.warn("Limite d'emojis du serveur atteinte")
                break
            else:
                try:
                    await t.create_custom_emoji(
                        name=ed["name"], image=base64.b64decode(ed["img"]), reason=REASON
                    )
                    counts[ed["animated"]] += 1
                    self.res["emojis"] += 1
                except discord.HTTPException as e:
                    self.warn(f"Emoji :{ed['name']}: refusé ({e.status})")
                await asyncio.sleep(1.2)
            await self.p.advance("emojis")
        await self.p.finish("emojis")


async def execute_clone(interaction: discord.Interaction, cog: "Cloner", parent: "ConfigView") -> None:
    target = interaction.guild
    if target.id in RUNNING:
        return await interaction.response.send_message(embed=err("Un clonage est déjà en cours ici."), ephemeral=True)
    miss = missing_perms(target)
    if miss:
        return await interaction.response.send_message(
            embed=err("Il me manque : " + ", ".join(f"`{m}`" for m in miss) + "\nDonne-moi **Administrateur** pour que ça marche à 100 %."),
            ephemeral=True,
        )

    RUNNING.add(target.id)
    prog = Progress(interaction, "🧬 Clonage en cours")
    opts = dict(parent.opts)
    only_cat = parent.only_cat
    await interaction.response.edit_message(embed=prog.embed(), view=None)
    started = time.monotonic()
    res: Optional[dict] = None
    try:
        prog.begin("read", 1)
        await prog.push(force=True)
        snap = await parent.resolve_snap()
        await prog.finish("read")
        keep = {interaction.channel_id}
        if STORAGE_CHANNEL_ID:
            keep.add(STORAGE_CHANNEL_ID)
        res = await Engine(target, snap, opts, only_cat, prog, keep).run()
    except Exception as exc:  # noqa: BLE001
        print(f"[cloner] Erreur clonage : {exc!r}")
        final = err("Le clonage a été interrompu par une erreur inattendue. Relance-le : les éléments déjà créés sont ignorés.")
    else:
        final = embed("✅ Clonage terminé", f"Durée : **{fmt_dur(time.monotonic() - started)}**", GREEN)
        final.add_field(
            name="Créé",
            value=f"🎭 {res['roles']} rôles\n📁 {res['categories']} catégories\n💬 {res['channels']} salons\n😀 {res['emojis']} emojis",
            inline=True,
        )
        final.add_field(name="Ignoré (déjà présent)", value=str(res["skipped"]), inline=True)
        if opts["clean"] and only_cat is None:
            final.add_field(
                name="🧹 Note",
                value="J'ai gardé le salon où tu as lancé la commande : supprime-le toi-même si besoin.",
                inline=False,
            )
        if res["warnings"]:
            final.add_field(name="⚠️ Avertissements", value="\n".join(f"• {w}" for w in res["warnings"])[:1000], inline=False)
    finally:
        RUNNING.discard(target.id)

    try:
        await interaction.edit_original_response(embed=final, view=None)
    except discord.HTTPException:
        try:
            await interaction.user.send(embed=final)
        except discord.HTTPException:
            pass


# --------------------------------------------------------------------------
# Interface : panneau, config, backups
# --------------------------------------------------------------------------
class Toggle(discord.ui.Button):
    def __init__(self, key: str, label: str, emoji: str, danger: bool = False):
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, row=0)
        self.key, self.danger = key, danger

    async def callback(self, interaction: discord.Interaction):
        v: ConfigView = self.view
        v.opts[self.key] = not v.opts[self.key]
        v.refresh()
        await interaction.response.edit_message(embed=v.embed(), view=v)


class CategorySelect(discord.ui.Select):
    def __init__(self, layout: list[dict]):
        options = [discord.SelectOption(label="Tout le serveur", value="all", emoji="🌍",
                                        description="Clone tout selon les options ci-dessus")]
        for i, l in enumerate(layout):
            if l["cat"] and len(options) < 25:
                options.append(discord.SelectOption(
                    label=l["cat"]["name"][:100], value=str(i), emoji="📁",
                    description=f"{len(l['channels'])} salon(s)"))
        super().__init__(placeholder="📁 Cloner UNE seule catégorie (optionnel)", min_values=0,
                         max_values=1, options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        v: ConfigView = self.view
        v.only_cat = int(self.values[0]) if self.values and self.values[0] != "all" else None
        await interaction.response.edit_message(embed=v.embed(), view=v)


class ConfirmView(discord.ui.View):
    def __init__(self, parent: "ConfigView"):
        super().__init__(timeout=120)
        self.parent = parent

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.parent.user_id

    @discord.ui.button(label="Oui, tout effacer puis cloner", style=discord.ButtonStyle.danger, emoji="💥")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await execute_clone(interaction, interaction.client.get_cog("Cloner"), self.parent)

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.parent.embed(), view=self.parent)


class ConfigView(discord.ui.View):
    def __init__(self, cog: "Cloner", user_id: int, snap: dict, source_guild: Optional[discord.Guild] = None,
                 backup_code: Optional[str] = None):
        super().__init__(timeout=900)
        self.cog, self.user_id, self.snap = cog, user_id, snap
        self.source_guild, self.backup_code = source_guild, backup_code
        self.opts = {"identity": True, "roles": True, "channels": True, "emojis": False, "clean": False}
        self.only_cat: Optional[int] = None

        self.toggles = [
            Toggle("identity", "Nom/Icône", "🪪"),
            Toggle("roles", "Rôles", "🎭"),
            Toggle("channels", "Salons", "💬"),
            Toggle("emojis", "Emojis", "😀"),
            Toggle("clean", "Nettoyer", "🧹", danger=True),
        ]
        for t in self.toggles:
            self.add_item(t)
        self.add_item(CategorySelect(snap["layout"]))
        if not backup_code:
            self.remove_item(self.delete_backup)
        self.refresh()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(embed=err("Ce panneau appartient à quelqu'un d'autre."), ephemeral=True)
            return False
        return True

    def refresh(self) -> None:
        single = self.only_cat is not None
        for t in self.toggles:
            on = self.opts[t.key]
            t.style = (discord.ButtonStyle.danger if t.danger else discord.ButtonStyle.success) if on else discord.ButtonStyle.secondary
            t.disabled = single

    def embed(self) -> discord.Embed:
        roles, cats, chans, emojis = snap_counts(self.snap)
        src = f"Backup `{self.backup_code}`" if self.backup_code else f"Serveur **{self.snap['name']}**"
        e = embed("🧬 Configuration du clonage", f"**Source :** {src}\n**Cible :** ce serveur", BLURPLE)
        e.add_field(name="📦 Contenu de la source",
                    value=f"🎭 {roles} rôles\n📁 {cats} catégories\n💬 {chans} salons\n😀 {emojis if self.snap['emojis'] else '—'} emojis", inline=True)
        if self.only_cat is not None:
            l = self.snap["layout"][self.only_cat]
            lines = "\n".join(f"{CH_ICON[c['type']]} {c['name']}" for c in l["channels"][:12]) or "(vide)"
            e.add_field(name=f"📁 Mode catégorie : {l['cat']['name']}", value=lines[:900], inline=True)
            e.add_field(name="ℹ️", value="Les rôles utilisés par les permissions sont réutilisés s'ils existent (même nom), sinon créés.", inline=False)
        else:
            mark = lambda k: "🟢" if self.opts[k] else "⚫"
            e.add_field(name="⚙️ Options", value="\n".join([
                f"{mark('identity')} Nom & icône", f"{mark('roles')} Rôles & permissions @everyone",
                f"{mark('channels')} Catégories & salons", f"{mark('emojis')} Emojis (lent, limité par Discord)",
                f"{'🔴' if self.opts['clean'] else '⚫'} Nettoyer d'abord (supprime tout !)"]), inline=True)
        e.add_field(name="🛡️ Sécurité", value="Les éléments déjà présents (même nom) sont ignorés : tu peux relancer sans doublons.", inline=False)
        return e

    async def resolve_snap(self) -> dict:
        if self.source_guild and self.only_cat is None and (self.opts["identity"] or self.opts["emojis"]):
            fresh = await snapshot_guild(self.source_guild, assets=True)
            self.snap["icon"], self.snap["emojis"] = fresh["icon"], fresh["emojis"]
        return self.snap

    @discord.ui.button(label="Lancer le clonage", style=discord.ButtonStyle.success, emoji="🚀", row=2)
    async def launch(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not admin_in(interaction.user.id, interaction.guild):
            return await interaction.response.send_message(embed=err("Il faut être administrateur de ce serveur."), ephemeral=True)
        if self.source_guild and self.source_guild.id == interaction.guild.id:
            return await interaction.response.send_message(embed=err("Source et cible sont le même serveur."), ephemeral=True)
        if self.opts["clean"] and self.only_cat is None:
            warn = embed("💥 Confirmation requise",
                         f"Tous les salons et rôles de **{interaction.guild.name}** (sauf celui-ci) vont être **supprimés** avant le clonage.\nCette action est irréversible.", RED)
            return await interaction.response.edit_message(embed=warn, view=ConfirmView(self))
        await execute_clone(interaction, self.cog, self)

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=embed("Annulé", "Rien n'a été modifié.", DARK), view=None)
        self.stop()

    @discord.ui.button(label="Supprimer ce backup", style=discord.ButtonStyle.danger, emoji="🗑️", row=2)
    async def delete_backup(self, interaction: discord.Interaction, button: discord.ui.Button):
        meta = self.cog.store.data["backups"].pop(self.backup_code, None)
        if meta:
            await self.cog.store.del_blob(meta["ref"])
            self.cog.store.dirty = True
            await self.cog.store.flush()
        await interaction.response.edit_message(embed=embed("🗑️ Backup supprimé", f"`{self.backup_code}` n'existe plus.", DARK), view=None)
        self.stop()

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        print(f"[cloner] Erreur vue config : {error!r}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=err("Erreur inattendue sur ce bouton."), ephemeral=True)
            else:
                await interaction.response.send_message(embed=err("Erreur inattendue sur ce bouton."), ephemeral=True)
        except discord.HTTPException:
            pass


class BackupSelect(discord.ui.Select):
    def __init__(self, cog: "Cloner", user_id: int, metas: list[tuple[str, dict]]):
        self.cog, self.user_id = cog, user_id
        options = [
            discord.SelectOption(label=f"{m['name']}"[:100], value=code, emoji="💾",
                                 description=f"{code} • {m['roles']} rôles • {m['channels']} salons"[:100])
            for code, m in metas[:25]
        ]
        super().__init__(placeholder="Choisis un backup à importer ici", options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(embed=err("Pas ton panneau."), ephemeral=True)
        code = self.values[0]
        await interaction.response.defer()
        try:
            snap = unpack(await self.cog.store.get_blob(self.cog.store.data["backups"][code]["ref"]))
        except Exception:  # noqa: BLE001
            return await interaction.edit_original_response(embed=err("Fichier du backup introuvable."), view=None)
        view = ConfigView(self.cog, self.user_id, snap, backup_code=code)
        await interaction.edit_original_response(embed=view.embed(), view=view)


class SourceModal(discord.ui.Modal, title="Cloner un serveur"):
    sid = discord.ui.TextInput(label="ID du serveur à copier", placeholder="123456789012345678",
                               min_length=17, max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        cog: Cloner = interaction.client.get_cog("Cloner")
        await interaction.response.defer(ephemeral=True)
        value = str(self.sid.value).strip()
        src = interaction.client.get_guild(int(value)) if value.isdigit() else None
        if not src:
            return await interaction.followup.send(embed=err("Je ne suis pas dans ce serveur. Invite-moi dessus puis réessaie."), ephemeral=True)
        if src.id == interaction.guild.id:
            return await interaction.followup.send(embed=err("C'est le serveur actuel."), ephemeral=True)
        if not admin_in(interaction.user.id, src):
            return await interaction.followup.send(embed=err("Tu dois être administrateur du serveur source."), ephemeral=True)
        snap = await snapshot_guild(src, assets=False)
        view = ConfigView(cog, interaction.user.id, snap, source_guild=src)
        await interaction.followup.send(embed=view.embed(), view=view, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        print(f"[cloner] Erreur modal : {error!r}")


class ClonePanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not admin_in(interaction.user.id, interaction.guild):
            await interaction.response.send_message(embed=err("Réservé aux administrateurs."), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Cloner un serveur", style=discord.ButtonStyle.primary, emoji="🧬", custom_id="cl:clone", row=0)
    async def clone(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SourceModal())

    @discord.ui.button(label="Backup de ce serveur", style=discord.ButtonStyle.success, emoji="💾", custom_id="cl:backup", row=0)
    async def backup(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: Cloner = interaction.client.get_cog("Cloner")
        await interaction.response.defer(ephemeral=True)
        try:
            code, meta = await cog.make_backup(interaction.guild, interaction.user.id)
        except RuntimeError as e:
            return await interaction.followup.send(embed=err(str(e)), ephemeral=True)
        await interaction.followup.send(embed=cog.backup_embed(code, meta), ephemeral=True)

    @discord.ui.button(label="Mes backups", style=discord.ButtonStyle.secondary, emoji="📦", custom_id="cl:list", row=0)
    async def backups(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: Cloner = interaction.client.get_cog("Cloner")
        metas = cog.visible_backups(interaction.user.id)
        if not metas:
            return await interaction.response.send_message(embed=embed("📦 Backups", "Aucun backup pour l'instant. Clique sur **Backup de ce serveur**.", DARK), ephemeral=True)
        e = embed("📦 Tes backups", "Choisis-en un pour l'importer **dans ce serveur**.", BLURPLE)
        for code, m in metas[:10]:
            e.add_field(name=f"💾 {m['name']}", value=f"`{code}` • <t:{m['ts']}:R>\n🎭 {m['roles']} • 📁 {m['cats']} • 💬 {m['channels']}", inline=True)
        v = discord.ui.View(timeout=300)
        v.add_item(BackupSelect(cog, interaction.user.id, metas))
        await interaction.response.send_message(embed=e, view=v, ephemeral=True)

    @discord.ui.button(label="Statistiques", style=discord.ButtonStyle.secondary, emoji="📊", custom_id="cl:stats", row=0)
    async def stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: Cloner = interaction.client.get_cog("Cloner")
        await cog.ready.wait()
        await interaction.response.send_message(embed=cog.stats_embed(interaction.guild), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        print(f"[cloner] Erreur panneau : {error!r}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=err("Erreur inattendue."), ephemeral=True)
            else:
                await interaction.response.send_message(embed=err("Erreur inattendue."), ephemeral=True)
        except discord.HTTPException:
            pass


# --------------------------------------------------------------------------
# Cog principal : stats + backups + commandes
# --------------------------------------------------------------------------
class Cloner(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = Storage(bot)
        self.known: dict[int, set[int]] = {}
        self.ready = asyncio.Event()
        self._boot_task: Optional[asyncio.Task] = None

    # ---- cycle de vie
    async def cog_load(self) -> None:
        self.bot.add_view(ClonePanel())
        self._boot_task = asyncio.create_task(self._boot())

    async def cog_unload(self) -> None:
        if self._boot_task:
            self._boot_task.cancel()
        if self.flusher.is_running():
            self.flusher.cancel()
        await self.flush_all()

    async def _boot(self) -> None:
        await self.bot.wait_until_ready()
        await self.store.load()
        for guild in self.bot.guilds:
            await self.sync_guild(guild)
        self.ready.set()
        await self.flush_all()
        self.flusher.start()

    async def flush_all(self) -> None:
        for gid, ids in self.known.items():
            g = self.store.data["guilds"].get(str(gid))
            if g is not None:
                g["known"] = sorted(ids)
        await self.store.flush()

    @tasks.loop(seconds=45)
    async def flusher(self) -> None:
        await self.flush_all()

    # ---- stats : état par serveur
    def gs(self, guild: discord.Guild) -> dict:
        return self.store.data["guilds"].setdefault(
            str(guild.id),
            {"init": False, "since": now(), "base": 0, "joins": 0, "leaves": 0, "off_joins": 0,
             "off_leaves": 0, "ev": [], "known": []},
        )

    def record(self, g: dict, kind: int, ts: int, stay: int = 0) -> None:
        g["joins" if kind == 0 else "leaves"] += 1
        g["ev"].append([ts, kind, stay])
        if len(g["ev"]) > MAX_EVENTS:
            del g["ev"][: len(g["ev"]) - MAX_EVENTS]
        self.store.dirty = True

    async def sync_guild(self, guild: discord.Guild) -> None:
        """Compare la liste actuelle à la dernière sauvegardée -> rattrape ce qui s'est passé hors-ligne."""
        try:
            if not guild.chunked:
                await guild.chunk()
        except Exception as e:  # noqa: BLE001
            print(f"[cloner] Chunk impossible pour {guild.name}: {e!r}")
            return
        current = {m.id for m in guild.members}
        expected = guild.member_count or len(current)
        if abs(len(current) - expected) > max(5, expected * 0.02):
            print(f"[cloner] [{guild.name}] cache membres incomplet ({len(current)}/{expected}) : synchro ignorée.")
            return

        g = self.gs(guild)
        if not g["init"]:
            g.update(init=True, since=now(), base=len(current))
        else:
            known = set(g["known"])
            ts = now()
            for _ in known - current:
                self.record(g, 1, ts)
                g["off_leaves"] += 1
            for _ in current - known:
                self.record(g, 0, ts)
                g["off_joins"] += 1
        self.known[guild.id] = current
        self.store.dirty = True

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self.ready.wait()
        await self.sync_guild(guild)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self.ready.wait()
        ids = self.known.setdefault(member.guild.id, set())
        if member.id in ids:
            return
        ids.add(member.id)
        self.record(self.gs(member.guild), 0, now())

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await self.ready.wait()
        ids = self.known.setdefault(member.guild.id, set())
        if member.id not in ids:
            return
        ids.discard(member.id)
        stay = max(0, now() - int(member.joined_at.timestamp())) if member.joined_at else 0
        self.record(self.gs(member.guild), 1, now(), stay)

    def stats_embed(self, guild: discord.Guild) -> discord.Embed:
        g = self.gs(guild)
        cur = guild.member_count or len(guild.members)
        bots = sum(1 for m in guild.members if m.bot)
        joins, leaves = g["joins"], g["leaves"]
        total = cur + leaves
        retention = cur / total * 100 if total else 100.0

        def window(secs: int) -> tuple[int, int]:
            cut, j, l = now() - secs, 0, 0
            for ts, kind, _ in g["ev"]:
                if ts >= cut:
                    j, l = (j + 1, l) if kind == 0 else (j, l + 1)
            return j, l

        rows = ["Période    Arr.  Dép.  Net"]
        for label, secs in (("24 heures", 86400), ("7 jours", 7 * 86400), ("30 jours", 30 * 86400)):
            j, l = window(secs)
            rows.append(f"{label:<10} {j:>4}  {l:>4}  {j - l:>+4}")

        stays = [ev[2] for ev in g["ev"] if ev[1] == 1 and ev[2] > 0]
        stay_txt = "—"
        if stays:
            quick = sum(1 for s in stays if s < 86400)
            stay_txt = f"Médiane : **{fmt_dur(statistics.median(stays))}**\nPartis en < 24 h : **{quick}**"

        months = Counter(m.joined_at.strftime("%m/%Y") for m in guild.members if m.joined_at)
        last = sorted(months, key=lambda k: (k[3:], k[:2]))[-6:]
        peak = max((months[k] for k in last), default=0)
        month_txt = "\n".join(f"`{k}` {bar(months[k], peak, 8)} {months[k]}" for k in last) or "—"

        e = embed(f"📊 Statistiques — {guild.name}", f"Suivi actif depuis <t:{g['since']}:D> (<t:{g['since']}:R>)", BLURPLE)
        e.add_field(name="👥 Membres", value=f"**{cur:,}** actuels\n🧑 {cur - bots:,} humains\n🤖 {bots:,} bots", inline=True)
        e.add_field(name="📥 Arrivées suivies", value=f"**{joins:,}**\ndont {g['off_joins']} détectées au restart", inline=True)
        e.add_field(name="📤 Départs suivis", value=f"**{leaves:,}**\ndont {g['off_leaves']} détectés au restart", inline=True)
        e.add_field(name="🧮 Si personne n'était parti", value=f"**{total:,}** membres\nRétention **{retention:.1f}%**\n`{bar(cur, total, 14)}`", inline=False)
        e.add_field(name="📈 Évolution", value="```\n" + "\n".join(rows) + "\n```", inline=False)
        e.add_field(name="⏱️ Durée avant départ", value=stay_txt, inline=True)
        e.add_field(name="🗓️ Arrivées des membres actuels", value=month_txt, inline=True)
        e.set_footer(text="Les départs d'avant l'installation ne sont pas récupérables : Discord ne les expose pas.")
        return e

    # ---- backups
    def visible_backups(self, user_id: int) -> list[tuple[str, dict]]:
        out = []
        for code, m in self.store.data["backups"].items():
            src = self.bot.get_guild(m["gid"])
            if user_id in OWNER_IDS or (src and admin_in(user_id, src)):
                out.append((code, m))
        return sorted(out, key=lambda x: x[1]["ts"], reverse=True)

    async def make_backup(self, guild: discord.Guild, user_id: int) -> tuple[str, dict]:
        await self.ready.wait()
        snap = await snapshot_guild(guild, assets=True)
        blob = pack(snap)
        if len(blob) > self.store.file_limit:  # trop lourd : on retire les emojis
            snap["emojis"] = []
            blob = pack(snap)
        if len(blob) > self.store.file_limit:
            raise RuntimeError("Le backup dépasse la taille maximale d'un fichier du salon de stockage.")

        code = secrets.token_hex(3).upper()
        while code in self.store.data["backups"]:
            code = secrets.token_hex(3).upper()
        ref = await self.store.put_blob(code, blob)
        roles, cats, chans, emojis = snap_counts(snap)
        meta = {"gid": guild.id, "name": guild.name, "ts": now(), "by": user_id, "ref": ref,
                "size": len(blob), "roles": roles, "cats": cats, "channels": chans, "emojis": emojis}
        backups = self.store.data["backups"]
        backups[code] = meta
        while len(backups) > MAX_BACKUPS:
            oldest = min(backups, key=lambda c: backups[c]["ts"])
            await self.store.del_blob(backups.pop(oldest)["ref"])
        self.store.dirty = True
        await self.store.flush()
        return code, meta

    def backup_embed(self, code: str, m: dict) -> discord.Embed:
        e = embed("💾 Backup créé", f"Ton code : **`{code}`**\nImporte-le sur un autre serveur avec `!loadbackup {code}` ou via le panneau.", GREEN)
        e.add_field(name="Contenu", value=f"🎭 {m['roles']} rôles\n📁 {m['cats']} catégories\n💬 {m['channels']} salons\n😀 {m['emojis']} emojis", inline=True)
        e.add_field(name="Taille", value=f"{m['size'] / 1024:.0f} Ko", inline=True)
        if self.store.mode == "local":
            e.add_field(name="⚠️ Stockage local", value="Sans `STORAGE_CHANNEL_ID`, ce backup disparaît au prochain redéploiement.", inline=False)
        return e

    # ---- commandes
    @commands.command(name="clonepanel", aliases=["clonepannel"])
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def clonepanel(self, ctx: commands.Context):
        e = embed("🧬 Cloner Pro — Panneau de contrôle",
                  "Copie, sauvegarde et importe des serveurs en quelques clics.", BLURPLE)
        e.add_field(name="🧬 Cloner un serveur", value="Entre l'ID d'un serveur où je suis : tout ou **une seule catégorie**.", inline=False)
        e.add_field(name="💾 Backup", value="Crée un code (ex. `A1B2C3`) pour ré-importer la structure n'importe où.", inline=False)
        e.add_field(name="📦 Mes backups", value="Liste, importe ou supprime tes sauvegardes.", inline=False)
        e.add_field(name="📊 Statistiques", value="Arrivées, départs, rétention : persistants même après restart.", inline=False)
        await ctx.send(embed=e, view=ClonePanel())

    @commands.command(name="clone")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def clone_cmd(self, ctx: commands.Context, source_id: int):
        src = self.bot.get_guild(source_id)
        if not src:
            return await ctx.send(embed=err("Je ne suis pas dans ce serveur."), delete_after=10)
        if src.id == ctx.guild.id:
            return await ctx.send(embed=err("C'est le serveur actuel."), delete_after=10)
        if not admin_in(ctx.author.id, src):
            return await ctx.send(embed=err("Tu dois être administrateur du serveur source."), delete_after=10)
        view = ConfigView(self, ctx.author.id, await snapshot_guild(src, assets=False), source_guild=src)
        await ctx.send(embed=view.embed(), view=view)

    @commands.command(name="backup")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def backup_cmd(self, ctx: commands.Context):
        async with ctx.typing():
            try:
                code, meta = await self.make_backup(ctx.guild, ctx.author.id)
            except RuntimeError as e:
                return await ctx.send(embed=err(str(e)))
        await ctx.send(embed=self.backup_embed(code, meta))

    @commands.command(name="loadbackup")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def loadbackup_cmd(self, ctx: commands.Context, code: str):
        code = code.upper()
        allowed = dict(self.visible_backups(ctx.author.id))
        if code not in allowed:
            return await ctx.send(embed=err("Backup introuvable (ou tu n'as pas accès au serveur d'origine)."), delete_after=10)
        snap = unpack(await self.store.get_blob(allowed[code]["ref"]))
        view = ConfigView(self, ctx.author.id, snap, backup_code=code)
        await ctx.send(embed=view.embed(), view=view)

    @commands.command(name="stats")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def stats_cmd(self, ctx: commands.Context):
        await self.ready.wait()
        await ctx.send(embed=self.stats_embed(ctx.guild))


async def setup(bot: commands.Bot):
    await bot.add_cog(Cloner(bot))
