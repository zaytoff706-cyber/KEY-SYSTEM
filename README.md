# KEY-SYSTEM Discord bot

Bot Python + Docker pour Render.

Variables Render obligatoires :
- `DISCORD_TOKEN`
- `BOT_PREFIX` : `!`
- `JOIN_ENABLED` : `true`

Commandes :
- `!panel` : ouvre le panneau de création de messages
- `!parler <texte>` : envoie un message
- `!aide` : affiche l'aide

Le bot doit avoir les intents **Message Content** et **Server Members** activés dans le portail Discord. Il doit avoir `View Channel`, `Send Messages` et `Embed Links`.

URL UptimeRobot : `https://TON-SERVICE.onrender.com/health`
