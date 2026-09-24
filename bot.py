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
    # Discord autorise au maximum 5 champs dans un modal.
    content = discord.ui.TextInput(
        label="Message normal (facultatif)",
        max_length=2000,
        required=False,
        style=discord.TextStyle.paragraph,
    )
    title_field = discord.ui.TextInput(
        label="Titre (facultatif)", max_length=256, required=False
    )
    description = discord.ui.TextInput(
        label="Description (facultative)",
        max_length=4096,
        required=False,
        style=discord.TextStyle.paragraph,
    )
    color = discord.ui.TextInput(
        label="Couleur hexadécimale",
        default="#5865F2",
        max_length=7,
        required=False,
    )
    image = discord.ui.TextInput(
        label="URL de l'image (facultative)", max_length=500, required=False
    )

    def __init__(self, target_channel: discord.TextChannel):
        super().__init__()
        self.target_channel = target_channel

    async def on_submit(self, interaction: discord.Interaction):
        # Répondre immédiatement : Discord expire une interaction après ~3 secondes.
        await interaction.response.defer(ephemeral=True)

        content = str(self.content.value).strip()
        title = str(self.title_field.value).strip()
        description = str(self.description.value).strip()
        color = str(self.color.value).strip() or "#5865F2"
        image = str(self.image.value).strip()

        if not any((content, title, description, image)):
            return await interaction.followup.send(
                "Remplis au moins un champ.", ephemeral=True
            )

        if image and not valid_url(image):
            return await interaction.followup.send(
                "L'URL doit commencer par http:// ou https://.", ephemeral=True
            )

        try:
            embed_color = parse_color(color)
        except ValueError as error:
            return await interaction.followup.send(str(error), ephemeral=True)

        me = interaction.guild.me
        if me is None or not self.target_channel.permissions_for(me).send_messages:
            return await interaction.followup.send(
                "Je ne peux pas envoyer de message dans ce salon.", ephemeral=True
            )

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
            await interaction.followup.send(
                f"Message envoyé dans {self.target_channel.mention}.", ephemeral=True
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "Discord a refusé l'envoi. Vérifie mes permissions dans ce salon.",
                ephemeral=True,
            )
        except Exception:
            await interaction.followup.send(
                "Une erreur inattendue est survenue pendant l'envoi.", ephemeral=True
            )


class Panel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Créer ici", style=discord.ButtonStyle.primary,
        emoji="✍️", custom_id="panel:create_here",
    )
    async def create_here(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message(
                "Ce bouton doit être utilisé dans un salon textuel.", ephemeral=True
            )
        await interaction.response.send_modal(MessageModal(interaction.channel))

    @discord.ui.button(
        label="Dans ce salon", style=discord.ButtonStyle.success,
        emoji="📨", custom_id="panel:current",
    )
    async def send_in_current_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message(
                "Ce bouton doit être utilisé dans un salon textuel.", ephemeral=True
            )
        await interaction.response.send_modal(MessageModal(interaction.channel))


@bot.event
async def on_ready():
    print(f"Connecté : {bot.user}")


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


@bot.command(name="panel", aliases=["pannel"])
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def panel_command(ctx: commands.Context):
    embed = discord.Embed(
        title="Panneau de création",
        description=(
            "Les deux boutons ouvrent le formulaire et envoient le message "
            "dans le salon où se trouve le panneau. Remplis au moins un champ."
        ),
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
    await ctx.send(
        f"`{PREFIX}panel` / `{PREFIX}pannel` = panneau | "
        f"`{PREFIX}parler <texte>` = envoyer un message | `{PREFIX}aide` = aide",
        delete_after=15,
    )


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
    await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
