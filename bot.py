import asyncio
import os
import re
from dataclasses import dataclass

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("BOT_PREFIX", "!")
DEFAULT_JOIN_ENABLED = os.getenv("JOIN_ENABLED", "true").lower() == "true"
DEFAULT_JOIN_MESSAGE = os.getenv("JOIN_MESSAGE", "Bienvenue {member} !")
# Optionnel : ID du rôle donné automatiquement à l'arrivée. Les réglages faits avec
# /autorole sont en mémoire et disparaissent au redémarrage (Render free) ;
# mettre AUTOROLE_ID dans les variables d'environnement les rend permanents.
_autorole_env = os.getenv("AUTOROLE_ID", "").strip()
DEFAULT_AUTOROLE_ID = int(_autorole_env) if _autorole_env.isdigit() else None

if not TOKEN:
    raise RuntimeError("La variable DISCORD_TOKEN est obligatoire.")

# IMPORTANT : intents.message_content doit AUSSI être activé sur le portail
# développeur Discord (https://discord.com/developers/applications -> ton app
# -> onglet "Bot" -> "Privileged Gateway Intents" -> MESSAGE CONTENT INTENT).
# Sans ça, toutes les commandes préfixées (!panel, !parler, !aide) sont
# ignorées silencieusement, même si le code ci-dessous les active.
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True


@dataclass
class GuildSettings:
    join_enabled: bool = DEFAULT_JOIN_ENABLED
    join_message: str = DEFAULT_JOIN_MESSAGE
    delete_delay: int = 3
    autorole_id: int | None = DEFAULT_AUTOROLE_ID


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


DISCORD_CDN_HOSTS = ("cdn.discordapp.com", "media.discordapp.net")


def normalize_image_url(value: str) -> str:
    """Retire les paramètres de redimensionnement (?width=, ?height=, ?size=)
    que Discord ajoute quand on copie le lien d'une image déjà envoyée dans
    un salon. Sans ça, l'image reste coincée en petite taille (souvent
    carrée) partout où le lien est réutilisé."""
    if "?" not in value:
        return value
    base, _, query = value.partition("?")
    if any(host in base for host in DISCORD_CDN_HOSTS):
        # media.discordapp.net a besoin du paramètre "format" pour certains
        # types de fichiers (ex: format=webp) ; on le garde s'il existe,
        # mais on enlève width/height/size qui forcent une résolution réduite.
        kept = [
            part for part in query.split("&")
            if not part.startswith(("width=", "height=", "size="))
        ]
        return base + ("?" + "&".join(kept) if kept else "")
    return value


def format_join_message(template: str, member: discord.Member) -> str:
    return template.replace("{member}", member.mention).replace("{username}", member.name)


async def delete_later(message: discord.Message, delay: int):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


def role_problem(guild: discord.Guild, role: discord.Role, user: discord.Member | None = None) -> str | None:
    """Retourne un message d'erreur si le bot (ou l'utilisateur) ne peut pas gérer ce rôle."""
    me = guild.me
    if role.is_default():
        return "Le rôle @everyone ne peut pas être utilisé."
    if role.managed:
        return "Ce rôle est géré par une intégration/un bot, je ne peux pas le donner."
    if role.permissions.administrator:
        return "Par sécurité, je refuse de distribuer automatiquement un rôle Administrateur."
    if me is None or not me.guild_permissions.manage_roles:
        return "Il me manque la permission `Gérer les rôles`."
    if role >= me.top_role:
        return "Mon rôle doit être **au-dessus** de ce rôle dans la liste des rôles du serveur."
    if user is not None and user.id != guild.owner_id and role >= user.top_role:
        return "Tu ne peux pas gérer un rôle supérieur ou égal à ton plus haut rôle."
    return None


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
        if image:
            image = normalize_image_url(image)

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
        # Sans ça, une exception dans un bouton laisse Discord sans réponse
        # pendant 3s -> "L'application n'a pas répondu à temps".
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
        await self.tree.sync()


bot = BotClient(command_prefix=PREFIX, intents=intents, help_command=None)


@bot.event
async def on_ready():
    print(f"Connecté : {bot.user}")


# --- Gestionnaire d'erreur GLOBAL -------------------------------------
# Sans ça, une commande qui échoue (mauvaise permission, argument
# manquant, etc.) ne répond RIEN sur Discord : l'erreur part juste dans
# les logs Docker, et on a l'impression que le bot "ne répond pas".
@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    error = getattr(error, "original", error)

    if isinstance(error, commands.CommandNotFound):
        return  # on ignore les commandes inconnues, pas besoin de spammer

    if isinstance(error, commands.MissingPermissions):
        return await ctx.send(
            "Il te manque la permission `Gérer les messages` pour ça.",
            delete_after=6,
        )

    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send(
            f"Il manque un argument : `{error.param.name}`. "
            f"Regarde `{PREFIX}aide`.", delete_after=8,
        )

    if isinstance(error, commands.NoPrivateMessage):
        return await ctx.send("Cette commande ne marche pas en message privé.", delete_after=6)

    if isinstance(error, discord.Forbidden):
        return await ctx.send(
            "Je n'ai pas la permission de faire ça dans ce salon.", delete_after=6
        )

    # Toute autre erreur : on log ET on prévient l'utilisateur au lieu de
    # rester silencieux.
    print(f"Erreur non gérée dans la commande {ctx.command} : {error!r}")
    try:
        await ctx.send("Une erreur est survenue pendant l'exécution de la commande.", delete_after=6)
    except discord.HTTPException:
        pass


@bot.event
async def on_member_join(member: discord.Member):
    config = get_settings(member.guild.id)

    # Autorole : donné avant tout le reste, indépendamment du message de bienvenue.
    if config.autorole_id:
        role = member.guild.get_role(config.autorole_id)
        if role and role_problem(member.guild, role) is None:
            try:
                await member.add_roles(role, reason="Autorole")
            except (discord.Forbidden, discord.HTTPException) as error:
                print(f"Autorole impossible pour {member} : {error!r}")
        elif role is None:
            print(f"Autorole : rôle {config.autorole_id} introuvable dans {member.guild}.")
        else:
            print(f"Autorole : {role_problem(member.guild, role)}")

    if not config.join_enabled or not member.guild.me:
        return
    for channel in member.guild.text_channels:
        permissions = channel.permissions_for(member.guild.me)
        if not permissions.view_channel or not permissions.send_messages:
            continue
        try:
            message = await channel.send(
                format_join_message(config.join_message, member),
                allowed_mentions=discord.AllowedMentions(
                    users=[member], everyone=False, roles=False
                ),
            )
            asyncio.create_task(delete_later(message, config.delete_delay))
            await asyncio.sleep(0.4)
        except (discord.Forbidden, discord.HTTPException):
            continue


# --- Commandes slash : /massrole et /autorole --------------------------
mass_locks: dict[int, asyncio.Lock] = {}


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        message = "Permission `Gérer les rôles` nécessaire."
    elif isinstance(error, app_commands.NoPrivateMessage):
        message = "Cette commande ne marche pas en message privé."
    else:
        print(f"Erreur commande slash : {error!r}")
        message = "Une erreur est survenue pendant la commande."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


@bot.tree.command(name="massrole", description="Donne un rôle à tous les membres du serveur")
@app_commands.describe(
    role="Le rôle à donner à tout le monde",
    inclure_bots="Donner aussi le rôle aux bots (non par défaut)",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_roles=True)
async def massrole(interaction: discord.Interaction, role: discord.Role, inclure_bots: bool = False):
    guild = interaction.guild
    problem = role_problem(guild, role, interaction.user)
    if problem:
        return await interaction.response.send_message(problem, ephemeral=True)

    lock = mass_locks.setdefault(guild.id, asyncio.Lock())
    if lock.locked():
        return await interaction.response.send_message(
            "Un massrole est déjà en cours sur ce serveur, attends qu'il se termine.",
            ephemeral=True,
        )

    await interaction.response.defer(ephemeral=True, thinking=True)
    async with lock:
        members = [m async for m in guild.fetch_members(limit=None)]
        targets = [m for m in members if role not in m.roles and (inclure_bots or not m.bot)]

        if not targets:
            return await interaction.followup.send(
                f"Tout le monde a déjà {role.mention}.", ephemeral=True
            )

        estimate = max(1, round(len(targets) * 0.5 / 60))
        await interaction.followup.send(
            f"Ajout de {role.mention} à **{len(targets)}** membre(s)... "
            f"(environ {estimate} min). Je te préviens à la fin.",
            ephemeral=True,
        )

        added = failed = 0
        for member in targets:
            try:
                await member.add_roles(role, reason=f"/massrole par {interaction.user}")
                added += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
            await asyncio.sleep(0.5)  # évite de saturer l'API Discord

    result = f"Terminé : {role.mention} ajouté à **{added}** membre(s)."
    if failed:
        result += f" **{failed}** échec(s) (membres au-dessus de moi, ou erreur Discord)."
    try:
        await interaction.followup.send(result, ephemeral=True)
    except discord.HTTPException:
        # Le jeton de l'interaction expire après 15 min : on prévient dans le salon.
        try:
            await interaction.channel.send(
                f"{interaction.user.mention} {result}",
                allowed_mentions=discord.AllowedMentions(users=[interaction.user]),
            )
        except discord.HTTPException:
            pass


autorole_group = app_commands.Group(
    name="autorole",
    description="Rôle donné automatiquement aux nouveaux membres",
    guild_only=True,
    default_permissions=discord.Permissions(manage_roles=True),
)


@autorole_group.command(name="definir", description="Choisit le rôle donné aux nouveaux membres")
@app_commands.describe(role="Le rôle à donner automatiquement à l'arrivée")
@app_commands.checks.has_permissions(manage_roles=True)
async def autorole_definir(interaction: discord.Interaction, role: discord.Role):
    problem = role_problem(interaction.guild, role, interaction.user)
    if problem:
        return await interaction.response.send_message(problem, ephemeral=True)
    get_settings(interaction.guild.id).autorole_id = role.id
    await interaction.response.send_message(
        f"Autorole activé : les nouveaux membres recevront {role.mention}.", ephemeral=True
    )


@autorole_group.command(name="desactiver", description="Désactive l'autorole")
@app_commands.checks.has_permissions(manage_roles=True)
async def autorole_desactiver(interaction: discord.Interaction):
    get_settings(interaction.guild.id).autorole_id = None
    await interaction.response.send_message("Autorole désactivé.", ephemeral=True)


@autorole_group.command(name="statut", description="Affiche l'autorole actuel")
@app_commands.checks.has_permissions(manage_roles=True)
async def autorole_statut(interaction: discord.Interaction):
    role_id = get_settings(interaction.guild.id).autorole_id
    role = interaction.guild.get_role(role_id) if role_id else None
    text = f"Autorole actuel : {role.mention}." if role else "Aucun autorole défini."
    await interaction.response.send_message(text, ephemeral=True)


bot.tree.add_command(autorole_group)


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


@panel_command.error
async def panel_error(ctx: commands.Context, error: commands.CommandError):
    await ctx.send(
        "Permission `Gérer les messages` nécessaire."
        if isinstance(error, commands.MissingPermissions)
        else "Erreur avec le panneau.",
        delete_after=5,
    )


@bot.command(name="parler")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def parler(ctx: commands.Context, *, message: str):
    # On efface la commande de l'utilisateur si possible, pour un rendu propre.
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


@parler.error
async def parler_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send(
            f"Utilisation : `{PREFIX}parler <texte à envoyer>`", delete_after=8
        )
    if isinstance(error, commands.MissingPermissions):
        return await ctx.send(
            "Permission `Gérer les messages` nécessaire pour utiliser `!parler`.",
            delete_after=6,
        )
    # Les autres cas remontent au gestionnaire global on_command_error.
    raise error


@bot.command(name="aide")
async def aide(ctx: commands.Context):
    await ctx.send(
        f"`{PREFIX}panel` / `{PREFIX}pannel` | "
        f"`{PREFIX}parler <texte>` | `{PREFIX}aide` | "
        "`/massrole` | `/autorole definir|desactiver|statut`", delete_after=15
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
