import asyncio
import os
import re
from typing import Optional

import discord
from aiohttp import web
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("BOT_PREFIX", "!")
JOIN_ENABLED = os.getenv("JOIN_ENABLED", "true").lower() == "true"

if not TOKEN:
    raise RuntimeError("La variable DISCORD_TOKEN est obligatoire.")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)


def parse_color(value: str) -> discord.Color:
    value = value.strip().replace("#", "")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        raise ValueError("La couleur doit être au format #5865F2.")
    return discord.Color(int(value, 16))


def valid_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


async def delete_later(message: discord.Message):
    await asyncio.sleep(3)
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


class MessageModal(discord.ui.Modal, title="Créer un message"):
    content = discord.ui.TextInput(
        label="Message normal (facultatif)", max_length=2000,
        style=discord.TextStyle.paragraph, required=False,
    )
    title_field = discord.ui.TextInput(
        label="Titre (facultatif)", max_length=256, required=False,
    )
    description = discord.ui.TextInput(
        label="Description (facultative)", max_length=4096,
        style=discord.TextStyle.paragraph, required=False,
    )
    color = discord.ui.TextInput(
        label="Couleur hexadécimale", default="#5865F2",
        max_length=7, required=False,
    )
    image = discord.ui.TextInput(
        label="URL image (facultative)", max_length=500, required=False,
    )

    def __init__(self, channel_id: Optional[int] = None):
        super().__init__()
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction):
        content = str(self.content.value).strip()
        title = str(self.title_field.value).strip()
        description = str(self.description.value).strip()
        color = str(self.color.value).strip() or "#5865F2"
        image = str(self.image.value).strip()

        if not any((content, title, description, image)):
            return await interaction.response.send_message(
                "Remplis au moins un champ.", ephemeral=True
            )
        if image and not valid_url(image):
            return await interaction.response.send_message(
                "L'URL doit commencer par http:// ou https://.", ephemeral=True
            )
        try:
            embed_color = parse_color(color)
        except ValueError as error:
            return await interaction.response.send_message(str(error), ephemeral=True)

        channel = interaction.guild.get_channel(self.channel_id) if self.channel_id else interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Salon textuel introuvable.", ephemeral=True)
        if not channel.permissions_for(interaction.guild.me).send_messages:
            return await interaction.response.send_message("Je ne peux pas parler dans ce salon.", ephemeral=True)

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
            await channel.send(content=content or None, embed=embed)
            await interaction.response.send_message(f"Message envoyé dans {channel.mention}.", ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message("Discord a refusé l'envoi.", ephemeral=True)


class Panel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Créer un message", style=discord.ButtonStyle.primary, emoji="✍️", custom_id="panel:create")
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MessageModal())

    @discord.ui.button(label="Dans ce salon", style=discord.ButtonStyle.success, emoji="📨", custom_id="panel:current")
    async def current(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MessageModal(interaction.channel.id))


@bot.event
async def on_ready():
    print(f"Connecté : {bot.user}")
    bot.add_view(Panel())


@bot.event
async def on_member_join(member: discord.Member):
    if not JOIN_ENABLED or not member.guild.me:
        return
    for channel in member.guild.text_channels:
        permissions = channel.permissions_for(member.guild.me)
        if not permissions.view_channel or not permissions.send_messages:
            continue
        try:
            message = await channel.send(
                f"Bienvenue {member.mention} !",
                allowed_mentions=discord.AllowedMentions(users=[member]),
            )
            asyncio.create_task(delete_later(message))
            await asyncio.sleep(0.4)
        except (discord.Forbidden, discord.HTTPException):
            continue


@bot.command(name="panel")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def panel_command(ctx: commands.Context):
    embed = discord.Embed(
        title="Panneau de création",
        description="Utilise un bouton. Tous les champs sont facultatifs, mais il faut en remplir au moins un.",
        color=discord.Color.blurple(),
    )
    await ctx.send(embed=embed, view=Panel())


@panel_command.error
async def panel_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Permission `Gérer les messages` nécessaire.", delete_after=5)
    else:
        await ctx.send("Erreur avec le panneau.", delete_after=5)


@bot.command(name="parler")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def parler(ctx: commands.Context, *, message: str):
    await ctx.send(message)


@bot.command(name="aide")
async def aide(ctx: commands.Context):
    await ctx.send(f"`{PREFIX}panel` panneau | `{PREFIX}parler <texte>` message | `{PREFIX}aide` aide", delete_after=15)


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
