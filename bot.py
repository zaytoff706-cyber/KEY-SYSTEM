import asyncio
import logging
import os
import re
import signal
from dataclasses import dataclass

import discord
from aiohttp import web
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

logging.getLogger("discord.http").setLevel(logging.ERROR)

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("BOT_PREFIX", "!")
DEFAULT_JOIN_ENABLED = os.getenv("JOIN_ENABLED", "true").lower() == "true"
DEFAULT_JOIN_MESSAGE = os.getenv("JOIN_MESSAGE", "Bienvenue {member} !")

VERIFY_KEYWORDS = [
    k.strip().lower()
    for k in os.getenv("VERIFY_CHANNEL_KEYWORDS", "verifiy,verify").split(",")
    if k.strip()
]


def parse_id_set(name: str) -> set[int]:
    raw = os.getenv(name, "").replace(" ", "")
    return {int(x) for x in raw.split(",") if x.isdigit()}


VIDEO_REPOST_USER_IDS = parse_id_set("VIDEO_REPOST_USER_IDS")
VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".wmv", ".flv")

PERMISSION_NAMES = {
    "administrator": "Administrateur",
    "manage_messages": "Gérer les messages",
    "manage_guild": "Gérer le serveur",
    "manage_channels": "Gérer les salons",
    "manage_roles": "Gérer les rôles",
}

if not TOKEN:
    raise RuntimeError("La variable DISCORD_TOKEN est obligatoire.")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True


@dataclass
class GuildSettings:
    join_enabled: bool = DEFAULT_JOIN_ENABLED
    join_message: str = DEFAULT_JOIN_MESSAGE
    delete_delay: int = 3


settings: dict[int, GuildSettings] = {}


def get_settings(guild_id: int) -> GuildSettings:
    return settings.setdefault(guild_id, GuildSettings())


def parse_color(value: str) -> discord.Color:
    value = value.strip().replace("#", "")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        raise ValueError("La couleur doit être au format #5865F2.")
    return discord.Color(int(value, 16))


def valid_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def format_join_message(template: str, member: discord.Member) -> str:
    return template.replace("{member}", member.mention).replace("{username}", member.name)


def is_verify_channel(channel: discord.abc.GuildChannel) -> bool:
    name = channel.name.lower()
    return any(keyword in name for keyword in VERIFY_KEYWORDS)


async def delete_later(message: discord.Message, delay: int):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


def is_video(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    return content_type.startswith("video/") or attachment.filename.lower().endswith(
        VIDEO_EXTENSIONS
    )


def can_repost(member: discord.Member) -> bool:
    if VIDEO_REPOST_USER_IDS:
        return member.id in VIDEO_REPOST_USER_IDS
    return member.guild_permissions.manage_messages


async def download_files(attachments: list[discord.Attachment]) -> list[discord.File]:
    return [await a.to_file(spoiler=a.is_spoiler()) for a in attachments]


async def resend_attachments(
    channel: discord.abc.Messageable,
    attachments: list[discord.Attachment],
    content: str | None,
) -> None:
    pending_content = content
    no_mentions = discord.AllowedMentions.none()

    for start in range(0, len(attachments), 10):
        group = attachments[start:start + 10]
        try:
            files = await download_files(group)
            await channel.send(
                content=pending_content, files=files,
                allowed_mentions=no_mentions, suppress_embeds=True,
            )
            pending_content = None
        except discord.HTTPException:
            for attachment in group:
                files = await download_files([attachment])
                await channel.send(
                    content=pending_content, files=files,
                    allowed_mentions=no_mentions, suppress_embeds=True,
                )
                pending_content = None


class MessageModal(discord.ui.Modal, title="Créer un embed"):
    content = discord.ui.TextInput(
        label="Message au-dessus (facultatif)", max_length=2000,
        required=False, style=discord.TextStyle.paragraph,
    )
    title_field = discord.ui.TextInput(
        label="Titre de l'embed (facultatif)", max_length=256, required=False,
    )
    description = discord.ui.TextInput(
        label="Description de l'embed (facultative)", max_length=4000,
        required=False, style=discord.TextStyle.paragraph,
    )
    color = discord.ui.TextInput(
        label="Couleur hexadécimale", default="#5865F2", max_length=7, required=False,
    )
    image = discord.ui.TextInput(
        label="URL directe de l'image (facultative)", max_length=500, required=False,
    )

    def __init__(self, target_channel: discord.TextChannel):
        super().__init__()
        self.target_channel = target_channel

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        content = str(self.content.value).strip()
        title = str(self.title_field.value).strip()
        description = str(self.description.value).strip()
        color_text = str(self.color.value).strip() or "#5865F2"
        image = str(self.image.value).strip()

        if not any((content, title, description, image)):
            return await interaction.followup.send(
                "Remplis au moins un champ.", ephemeral=True
            )

        if image and not valid_url(image):
            return await interaction.followup.send(
                "L'image doit être une URL publique qui commence par http:// ou https://.",
                ephemeral=True,
            )

        try:
            embed_color = parse_color(color_text)
        except ValueError as error:
            return await interaction.followup.send(str(error), ephemeral=True)

        me = interaction.guild.me
        permissions = self.target_channel.permissions_for(me) if me else None
        if not permissions or not permissions.send_messages:
            return await interaction.followup.send(
                "Je n'ai pas la permission `Envoyer des messages` dans ce salon.",
                ephemeral=True,
            )

        has_embed = bool(title or description or image)
        if has_embed and not permissions.embed_links:
            return await interaction.followup.send(
                "Je n'ai pas la permission `Intégrer des liens` dans ce salon. "
                "Active cette permission pour afficher l'embed.",
                ephemeral=True,
            )

        embed = None
        if has_embed:
            embed = discord.Embed(
                title=title or None,
                description=description or None,
                colour=embed_color,
            )
            if image:
                embed.set_image(url=image)

        try:
            await self.target_channel.send(
                content=content or None,
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            kind = "Embed envoyé" if has_embed else "Message envoyé"
            await interaction.followup.send(
                f"{kind} dans {self.target_channel.mention}.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Discord a refusé l'envoi. Vérifie `Envoyer des messages` et "
                "`Intégrer des liens`.", ephemeral=True,
            )
        except discord.HTTPException as error:
            print(f"Erreur Discord pendant l'envoi : {error!r}")
            await interaction.followup.send(
                "Discord a refusé l'embed. Vérifie que l'URL de l'image est publique "
                "et que le contenu ne dépasse pas les limites Discord.", ephemeral=True,
            )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"Erreur formulaire : {error!r}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Erreur dans le formulaire.", ephemeral=True)
            else:
                await interaction.response.send_message("Erreur dans le formulaire.", ephemeral=True)
        except discord.HTTPException:
            pass


class WelcomeSettingsModal(discord.ui.Modal, title="Réglages bienvenue"):
    message = discord.ui.TextInput(
        label="Message ({member} = mention)", default=DEFAULT_JOIN_MESSAGE,
        max_length=2000, required=False, style=discord.TextStyle.paragraph,
    )
    delay = discord.ui.TextInput(
        label="Délai en secondes (1-60)", default="3", max_length=2, required=False,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        config = get_settings(interaction.guild.id)
        config.join_message = str(self.message.value).strip() or DEFAULT_JOIN_MESSAGE
        try:
            delay = int(str(self.delay.value).strip() or "3")
            if not 1 <= delay <= 60:
                raise ValueError
        except ValueError:
            return await interaction.followup.send(
                "Le délai doit être entre 1 et 60 secondes.", ephemeral=True
            )
        config.delete_delay = delay
        await interaction.followup.send("Réglages sauvegardés.", ephemeral=True)


class Panel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        print(f"Erreur dans le panneau ({item}) : {error!r}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Une erreur est survenue avec ce bouton.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "Une erreur est survenue avec ce bouton.", ephemeral=True
                )
        except discord.HTTPException:
            pass

    async def open_message_modal(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message(
                "Utilise ce panneau dans un salon textuel.", ephemeral=True
            )
        await interaction.response.send_modal(MessageModal(interaction.channel))

    @discord.ui.button(
        label="Créer ici", style=discord.ButtonStyle.primary,
        emoji="✍️", custom_id="panel:create_here",
    )
    async def create_here(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_message_modal(interaction)

    @discord.ui.button(
        label="Réglages bienvenue", style=discord.ButtonStyle.secondary,
        emoji="⚙️", custom_id="panel:welcome_settings",
    )
    async def welcome_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                "Permission `Gérer le serveur` nécessaire.", ephemeral=True
            )
        await interaction.response.send_modal(WelcomeSettingsModal())

    @discord.ui.button(
        label="Activer/Désactiver bienvenue", style=discord.ButtonStyle.success,
        emoji="👋", custom_id="panel:welcome_toggle",
    )
    async def welcome_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                "Permission `Gérer le serveur` nécessaire.", ephemeral=True
            )
        config = get_settings(interaction.guild.id)
        config.join_enabled = not config.join_enabled
        status = "activés" if config.join_enabled else "désactivés"
        await interaction.response.send_message(
            f"Messages de bienvenue {status}.", ephemeral=True
        )


class LegacyPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        print(f"Erreur dans l'ancien panneau ({item}) : {error!r}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Une erreur est survenue avec ce bouton.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "Une erreur est survenue avec ce bouton.", ephemeral=True
                )
        except discord.HTTPException:
            pass

    @discord.ui.button(
        label="Créer un message", style=discord.ButtonStyle.primary,
        emoji="✍️", custom_id="panel:create",
    )
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        if isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_modal(MessageModal(interaction.channel))
        else:
            await interaction.response.send_message(
                "Utilise ce panneau dans un salon textuel.", ephemeral=True
            )

    @discord.ui.button(
        label="Dans ce salon", style=discord.ButtonStyle.success,
        emoji="📨", custom_id="panel:current",
    )
    async def current(self, interaction: discord.Interaction, button: discord.ui.Button):
        if isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_modal(MessageModal(interaction.channel))
        else:
            await interaction.response.send_message(
                "Utilise ce panneau dans un salon textuel.", ephemeral=True
            )


class BotClient(commands.Bot):
    async def setup_hook(self):
        self.add_view(Panel())
        self.add_view(LegacyPanel())
        try:
            await self.load_extension("cloner")
            print("Extension cloner chargée.")
        except Exception as error:
            print(f"Extension cloner NON chargée : {error!r}")


bot = BotClient(command_prefix=PREFIX, intents=intents, help_command=None)


@bot.event
async def on_ready():
    print(f"Connecté : {bot.user}")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    error = getattr(error, "original", error)

    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.MissingPermissions):
        names = ", ".join(
            f"`{PERMISSION_NAMES.get(p, p)}`" for p in error.missing_permissions
        )
        return await ctx.send(f"Il te manque la permission {names} pour ça.", delete_after=6)

    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send(
            f"Il manque un argument : `{error.param.name}`. "
            f"Regarde `{PREFIX}aide`.", delete_after=8,
        )

    if isinstance(error, commands.BadArgument):
        return await ctx.send(
            f"Argument invalide. Regarde `{PREFIX}aide`.", delete_after=8
        )

    if isinstance(error, commands.NoPrivateMessage):
        return await ctx.send("Cette commande ne marche pas en message privé.", delete_after=6)

    if isinstance(error, discord.Forbidden):
        return await ctx.send(
            "Je n'ai pas la permission de faire ça dans ce salon.", delete_after=6
        )

    print(f"Erreur non gérée dans la commande {ctx.command} : {error!r}")
    try:
        await ctx.send("Une erreur est survenue pendant l'exécution de la commande.", delete_after=6)
    except discord.HTTPException:
        pass


@bot.event
async def on_member_join(member: discord.Member):
    config = get_settings(member.guild.id)
    if not config.join_enabled or not member.guild.me:
        return

    targets = [
        channel
        for channel in member.guild.text_channels
        if is_verify_channel(channel)
        and channel.permissions_for(member.guild.me).view_channel
        and channel.permissions_for(member.guild.me).send_messages
    ]

    if not targets:
        print(
            f"[{member.guild.name}] Aucun salon de vérification trouvé "
            f"(mots-clés : {VERIFY_KEYWORDS}) ou permissions manquantes."
        )
        return

    for channel in targets:
        try:
            message = await channel.send(
                format_join_message(config.join_message, member),
                allowed_mentions=discord.AllowedMentions(
                    users=[member], everyone=False, roles=False
                ),
            )
            asyncio.create_task(delete_later(message, config.delete_delay))
        except (discord.Forbidden, discord.HTTPException):
            continue


@bot.listen("on_message")
async def repost_videos(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    if not any(is_video(a) for a in message.attachments):
        return
    if not can_repost(message.author):
        return

    me = message.guild.me
    permissions = message.channel.permissions_for(me) if me else None
    if not permissions or not (
        permissions.send_messages and permissions.attach_files and permissions.manage_messages
    ):
        print(
            f"[{message.guild.name}] Repost vidéo impossible dans #{message.channel}: "
            "il me faut Envoyer des messages, Joindre des fichiers et Gérer les messages."
        )
        return

    limit = message.guild.filesize_limit
    if any(a.size > limit for a in message.attachments):
        limit_mb = limit // (1024 * 1024)
        await message.channel.send(
            f"Vidéo trop lourde pour que je la renvoie (limite du serveur : {limit_mb} Mo). "
            "Je laisse ton message original.",
            delete_after=8,
        )
        return

    try:
        await resend_attachments(
            message.channel, list(message.attachments), message.content or None
        )
    except discord.HTTPException as error:
        print(f"Erreur pendant le repost des vidéos : {error!r}")
        await message.channel.send(
            "Je n'ai pas réussi à renvoyer la vidéo, je garde ton message original.",
            delete_after=8,
        )
        return

    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


@bot.command(name="panel", aliases=["pannel"])
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def panel_command(ctx: commands.Context):
    await ctx.send(
        embed=discord.Embed(
            title="Panneau du bot",
            description=(
                "**Créer ici** : crée un message ou un embed dans ce salon.\n"
                "**Réglages bienvenue** : modifie le texte et le délai.\n"
                "**Activer/Désactiver** : contrôle les messages à l'arrivée.\n\n"
                "Pour un embed, remplis le titre, la description, la couleur ou l'URL d'image."
            ),
            color=discord.Color.blurple(),
        ),
        view=Panel(),
    )


@bot.command(name="parler")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def parler(ctx: commands.Context, *, message: str):
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass

    me = ctx.guild.me
    permissions = ctx.channel.permissions_for(me) if me else None
    if not permissions or not permissions.send_messages:
        return await ctx.send(
            "Je n'ai pas la permission `Envoyer des messages` dans ce salon.",
            delete_after=6,
        )

    try:
        await ctx.send(message, allowed_mentions=discord.AllowedMentions.none())
    except discord.Forbidden:
        await ctx.send(
            "Discord a refusé l'envoi (permissions insuffisantes).", delete_after=6
        )
    except discord.HTTPException as error:
        print(f"Erreur Discord dans !parler : {error!r}")
        await ctx.send(
            "Discord a refusé le message (trop long ou invalide ?).", delete_after=6
        )


async def set_join_ping(ctx: commands.Context, enabled: bool):
    get_settings(ctx.guild.id).join_enabled = enabled

    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass

    status = "activé ✅" if enabled else "désactivé ❌"
    await ctx.send(f"Ping des nouveaux membres {status}.", delete_after=6)


@bot.command(name="on")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def ping_on(ctx: commands.Context):
    await set_join_ping(ctx, True)


@bot.command(name="off")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def ping_off(ctx: commands.Context):
    await set_join_ping(ctx, False)


@bot.command(name="aide", aliases=["help"])
async def aide(ctx: commands.Context):
    p = PREFIX
    embed = discord.Embed(
        title="📖 Liste des commandes",
        description="Les réponses du bot se suppriment automatiquement après quelques secondes.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="💬 Messages  •  Gérer les messages",
        value=(
            f"`{p}panel` : panneau pour créer un message ou un embed\n"
            f"`{p}parler <texte>` : le bot envoie ton texte\n"
            f"`{p}on` / `{p}off` : ping des nouveaux membres"
        ),
        inline=False,
    )
    embed.add_field(
        name="🧬 Clonage  •  Administrateur",
        value=(
            f"`{p}clonepanel` : panneau de contrôle complet\n"
            f"`{p}clone <id_serveur>` : clone un serveur (ou une seule catégorie) ici\n"
            f"`{p}backup` : sauvegarde ce serveur et donne un code\n"
            f"`{p}loadbackup <code>` : importe un backup ici"
        ),
        inline=False,
    )
    embed.add_field(
        name="📊 Statistiques  •  Gérer les messages",
        value=f"`{p}stats` : membres, arrivées, départs, rétention",
        inline=False,
    )
    embed.add_field(name="ℹ️ Divers", value=f"`{p}aide` : cette liste", inline=False)
    await ctx.send(embed=embed, delete_after=60)


async def health(request: web.Request):
    return web.json_response({
        "status": "ok",
        "bot_ready": not bot.is_closed(),
        "bot_user": str(bot.user) if bot.user else None,
    })


async def main():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(
        runner, "0.0.0.0", int(os.getenv("PORT", "10000"))
    ).start()

    loop = asyncio.get_running_loop()
    pending = []

    def request_shutdown():
        pending.append(asyncio.create_task(bot.close()))

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await bot.start(TOKEN)
    finally:
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
