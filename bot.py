import asyncio
import os
import re
from dataclasses import dataclass

import discord
from aiohttp import web
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("BOT_PREFIX", "!")
DEFAULT_JOIN_ENABLED = os.getenv("JOIN_ENABLED", "true").lower() == "true"
DEFAULT_JOIN_MESSAGE = os.getenv("JOIN_MESSAGE", "Bienvenue {member} !")

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


async def delete_later(message: discord.Message, delay: int):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


class MessageModal(discord.ui.Modal, title="Créer un message"):
    content = discord.ui.TextInput(label="Message normal (facultatif)", max_length=2000, required=False, style=discord.TextStyle.paragraph)
    title_field = discord.ui.TextInput(label="Titre (facultatif)", max_length=256, required=False)
    description = discord.ui.TextInput(label="Description (facultative)", max_length=4096, required=False, style=discord.TextStyle.paragraph)
    color = discord.ui.TextInput(label="Couleur hexadécimale", default="#5865F2", max_length=7, required=False)
    image = discord.ui.TextInput(label="URL de l'image (facultative)", max_length=500, required=False)

    def __init__(self, target_channel: discord.TextChannel):
        super().__init__()
        self.target_channel = target_channel

    async def on_submit(self, interaction: discord.Interaction):
        # Réponse dans les premières secondes, sinon Discord affiche « Échec de l'interaction ».
        await interaction.response.defer(ephemeral=True)
        content = str(self.content.value).strip()
        title = str(self.title_field.value).strip()
        description = str(self.description.value).strip()
        color = str(self.color.value).strip() or "#5865F2"
        image = str(self.image.value).strip()

        if not any((content, title, description, image)):
            return await interaction.followup.send("Remplis au moins un champ.", ephemeral=True)
        if image and not valid_url(image):
            return await interaction.followup.send("L'URL doit commencer par http:// ou https://.", ephemeral=True)
        try:
            embed_color = parse_color(color)
        except ValueError as error:
            return await interaction.followup.send(str(error), ephemeral=True)

        me = interaction.guild.me
        permissions = self.target_channel.permissions_for(me) if me else None
        if not permissions or not permissions.send_messages:
            return await interaction.followup.send("Je ne peux pas envoyer de message dans ce salon.", ephemeral=True)

        embed = None
        if title or description or image:
            embed = discord.Embed(color=embed_color)
            if title:
                embed.title = title
            if description:
                embed.description = description
            if image:
                embed.set_image(url=image)

        try:
            await self.target_channel.send(content=content or None, embed=embed)
            await interaction.followup.send(f"Message envoyé dans {self.target_channel.mention}.", ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send("Discord a refusé l'envoi. Vérifie les permissions du bot.", ephemeral=True)

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
    message = discord.ui.TextInput(label="Message ({member} = mention)", default=DEFAULT_JOIN_MESSAGE, max_length=2000, required=False, style=discord.TextStyle.paragraph)
    delay = discord.ui.TextInput(label="Délai en secondes (1-60)", default="3", max_length=2, required=False)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        config = get_settings(interaction.guild.id)
        config.join_message = str(self.message.value).strip() or DEFAULT_JOIN_MESSAGE
        try:
            delay = int(str(self.delay.value).strip() or "3")
            if not 1 <= delay <= 60:
                raise ValueError
        except ValueError:
            return await interaction.followup.send("Le délai doit être entre 1 et 60 secondes.", ephemeral=True)
        config.delete_delay = delay
        await interaction.followup.send("Réglages sauvegardés.", ephemeral=True)


class Panel(discord.ui.View):
    def __init__(self, prefix: str = "panel"):
        super().__init__(timeout=None)
        self.prefix = prefix

    async def open_message_modal(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("Utilise ce panneau dans un salon textuel.", ephemeral=True)
        await interaction.response.send_modal(MessageModal(interaction.channel))

    @discord.ui.button(label="Créer ici", style=discord.ButtonStyle.primary, emoji="✍️", custom_id="panel:create_here")
    async def create_here(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_message_modal(interaction)

    @discord.ui.button(label="Réglages bienvenue", style=discord.ButtonStyle.secondary, emoji="⚙️", custom_id="panel:welcome_settings")
    async def welcome_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("Permission `Gérer le serveur` nécessaire.", ephemeral=True)
        await interaction.response.send_modal(WelcomeSettingsModal())

    @discord.ui.button(label="Activer/Désactiver bienvenue", style=discord.ButtonStyle.success, emoji="👋", custom_id="panel:welcome_toggle")
    async def welcome_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("Permission `Gérer le serveur` nécessaire.", ephemeral=True)
        config = get_settings(interaction.guild.id)
        config.join_enabled = not config.join_enabled
        status = "activés" if config.join_enabled else "désactivés"
        await interaction.response.send_message(f"Messages de bienvenue {status}.", ephemeral=True)


class LegacyPanel(discord.ui.View):
    """Garde les anciens panneaux fonctionnels après une mise à jour."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Créer un message", style=discord.ButtonStyle.primary, emoji="✍️", custom_id="panel:create")
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        if isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_modal(MessageModal(interaction.channel))
        else:
            await interaction.response.send_message("Utilise ce panneau dans un salon textuel.", ephemeral=True)

    @discord.ui.button(label="Dans ce salon", style=discord.ButtonStyle.success, emoji="📨", custom_id="panel:current")
    async def current(self, interaction: discord.Interaction, button: discord.ui.Button):
        if isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_modal(MessageModal(interaction.channel))
        else:
            await interaction.response.send_message("Utilise ce panneau dans un salon textuel.", ephemeral=True)


class BotClient(commands.Bot):
    async def setup_hook(self):
        # Obligatoire : enregistre les boutons avant toute interaction Discord.
        self.add_view(Panel())
        self.add_view(LegacyPanel())


bot = BotClient(command_prefix=PREFIX, intents=intents, help_command=None)


@bot.event
async def on_ready():
    print(f"Connecté : {bot.user}")


@bot.event
async def on_member_join(member: discord.Member):
    config = get_settings(member.guild.id)
    if not config.join_enabled or not member.guild.me:
        return
    for channel in member.guild.text_channels:
        permissions = channel.permissions_for(member.guild.me)
        if not permissions.view_channel or not permissions.send_messages:
            continue
        try:
            message = await channel.send(
                format_join_message(config.join_message, member),
                allowed_mentions=discord.AllowedMentions(users=[member], everyone=False, roles=False),
            )
            asyncio.create_task(delete_later(message, config.delete_delay))
            await asyncio.sleep(0.4)
        except (discord.Forbidden, discord.HTTPException):
            continue


@bot.command(name="panel", aliases=["pannel"])
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def panel_command(ctx: commands.Context):
    await ctx.send(embed=discord.Embed(
        title="Panneau du bot",
        description="**Créer ici** : formulaire de message.\n**Réglages bienvenue** : texte et délai.\n**Activer/Désactiver** : messages à l'arrivée.\n\nLe bot utilise tous les salons où il a `Voir le salon` et `Envoyer des messages`.",
        color=discord.Color.blurple(),
    ), view=Panel())


@panel_command.error
async def panel_error(ctx: commands.Context, error: commands.CommandError):
    await ctx.send("Permission `Gérer les messages` nécessaire." if isinstance(error, commands.MissingPermissions) else "Erreur avec le panneau.", delete_after=5)


@bot.command(name="parler")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def parler(ctx: commands.Context, *, message: str):
    await ctx.send(message)


@bot.command(name="aide")
async def aide(ctx: commands.Context):
    await ctx.send(f"`{PREFIX}panel` / `{PREFIX}pannel` | `{PREFIX}parler <texte>` | `{PREFIX}aide`", delete_after=15)


async def health(request: web.Request):
    return web.json_response({"status": "ok", "bot_ready": not bot.is_closed(), "bot_user": str(bot.user) if bot.user else None})


async def main():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000"))).start()
    await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
