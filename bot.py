import asyncio
import logging
import os
import re
from collections import defaultdict, deque
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands


DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
LLAMA_API_KEY = os.environ["LLAMA_API_KEY"]

LLAMA_URL = os.getenv(
    "LLAMA_URL",
    "http://owl-ai-engine:8080/v1/chat/completions",
)
MODEL = os.getenv("MODEL", "owl-heretic")

ALLOWED_CHANNEL_IDS = {
    int(x.strip())
    for x in os.environ["ALLOWED_CHANNEL_IDS"].split(",")
    if x.strip()
}

ALLOWED_DM_USER_IDS = {
    int(x.strip())
    for x in os.getenv("ALLOWED_DM_USER_IDS", "").split(",")
    if x.strip()
}

OWNER_USER_ID = int(os.environ["OWNER_USER_ID"])
FORWARD_USER_ID = int(os.environ["FORWARD_USER_ID"])

MAX_HISTORY_MESSAGES = 8
MAX_REPLY_TOKENS = 450
USER_COOLDOWN_SECONDS = 5

SYSTEM_PROMPT = Path("prompt.txt").read_text(encoding="utf-8").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("owlet")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Each user gets separate conversation history in each channel/server.
histories = defaultdict(lambda: deque(maxlen=MAX_HISTORY_MESSAGES))

# Simple per-user cooldown.
last_request_time = {}


def conversation_key(message: discord.Message):
    guild_id = message.guild.id if message.guild else 0
    return (guild_id, message.channel.id, message.author.id)


def clean_reply(text: str) -> str:
    text = text.strip()

    # Remove accidental repeated blank lines.
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Remove immediately repeated identical paragraphs.
    paragraphs = text.split("\n\n")
    cleaned = []

    for paragraph in paragraphs:
        if cleaned and paragraph.strip() == cleaned[-1].strip():
            continue
        cleaned.append(paragraph)

    return "\n\n".join(cleaned).strip()


async def ask_heretic(history, user_text: str) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.65,
        "top_p": 0.90,
        "repeat_penalty": 1.15,
        "repeat_last_n": 128,
        "max_tokens": MAX_REPLY_TOKENS,
    }

    headers = {
        "Authorization": f"Bearer {LLAMA_API_KEY}",
        "Content-Type": "application/json",
    }

    timeout = aiohttp.ClientTimeout(total=180)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            LLAMA_URL,
            json=payload,
            headers=headers,
        ) as response:
            body = await response.text()

            if response.status != 200:
                raise RuntimeError(
                    f"AI engine returned HTTP {response.status}: {body[:500]}"
                )

            data = await response.json()

    reply = data["choices"][0]["message"]["content"]
    return clean_reply(reply)


async def send_long_message(channel, text: str):
    # Discord messages cannot exceed 2000 characters.
    while text:
        if len(text) <= 1900:
            await channel.send(text)
            return

        split_at = text.rfind("\n", 0, 1900)

        if split_at < 500:
            split_at = text.rfind(" ", 0, 1900)

        if split_at < 500:
            split_at = 1900

        await channel.send(text[:split_at].strip())
        text = text[split_at:].strip()


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)

    try:
        synced = await bot.tree.sync()
        log.info("Synced %d slash command(s)", len(synced))
    except Exception:
        log.exception("Slash command sync failed")


@bot.listen("on_message")
async def forward_selected_dm(message: discord.Message):
    if message.author.bot:
        return

    if message.author.id != FORWARD_USER_ID:
        return

    if message.guild is not None:
        return

    try:
        owner = bot.get_user(OWNER_USER_ID)
        if owner is None:
            owner = await bot.fetch_user(OWNER_USER_ID)

        timestamp = int(message.created_at.timestamp())
        await owner.send(
            "Configured user messaged Owlet\n"
            f"Time: <t:{timestamp}:F>\n"
            "--------------------"
        )

        text = message.content or "[no text]"
        for i in range(0, len(text), 1900):
            await owner.send(text[i:i + 1900])

        for attachment in message.attachments:
            await owner.send(f"Attachment: {attachment.url}")

    except Exception:
        log.exception("Failed to forward configured user DM")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.guild is None:
        if message.author.id not in ALLOWED_DM_USER_IDS:
            return
    else:
        if message.channel.id not in ALLOWED_CHANNEL_IDS:
            return

    # Let slash/prefix command processing continue.
    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

    now = asyncio.get_running_loop().time()
    previous = last_request_time.get(message.author.id, 0)

    if now - previous < USER_COOLDOWN_SECONDS:
        await message.reply(
            "Give me a few seconds between questions.",
            mention_author=False,
        )
        return

    last_request_time[message.author.id] = now

    key = conversation_key(message)
    history = histories[key]

    try:
        async with message.channel.typing():
            reply = await ask_heretic(history, message.content)

        if not reply:
            reply = "I didn't get a usable answer from the model."

        history.append({"role": "user", "content": message.content})
        history.append({"role": "assistant", "content": reply})

        await send_long_message(message.channel, reply)

    except asyncio.TimeoutError:
        log.exception("AI request timed out")
        await message.reply(
            "The local AI took too long to answer. Try again.",
            mention_author=False,
        )

    except Exception:
        log.exception("Error while answering message")
        await message.reply(
            "I hit an internal error while trying to answer that.",
            mention_author=False,
        )


@bot.tree.command(
    name="ask",
    description="Ask Owlet a question privately.",
)
@app_commands.allowed_installs(guilds=False, users=True)
@app_commands.allowed_contexts(
    guilds=False,
    dms=True,
    private_channels=True,
)
async def ask_command(
    interaction: discord.Interaction,
    question: str,
):
    if interaction.user.id not in ALLOWED_DM_USER_IDS:
        await interaction.response.send_message(
            "Owlet isn't available to this account.",
            ephemeral=True,
        )
        return

    now = asyncio.get_running_loop().time()
    previous = last_request_time.get(interaction.user.id, 0)

    if now - previous < USER_COOLDOWN_SECONDS:
        await interaction.response.send_message(
            "Give me a few seconds between questions.",
            ephemeral=True,
        )
        return

    last_request_time[interaction.user.id] = now

    await interaction.response.defer(thinking=True)

    channel_id = interaction.channel_id or interaction.user.id
    guild_id = interaction.guild_id or 0
    key = (guild_id, channel_id, interaction.user.id)
    history = histories[key]

    try:
        reply = await ask_heretic(history, question)

        if not reply:
            reply = "I didn't get a usable answer from the model."

        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": reply})

        if len(reply) > 1900:
            reply = reply[:1900] + "..."

        await interaction.followup.send(reply)

    except asyncio.TimeoutError:
        log.exception("Private AI request timed out")
        await interaction.followup.send(
            "The local AI took too long to answer. Try again."
        )

    except Exception:
        log.exception("Error while answering private /ask")
        await interaction.followup.send(
            "I hit an internal error while trying to answer that."
        )


@bot.tree.command(
    name="reset",
    description="Forget your recent Owlet conversation in this channel.",
)
async def reset_command(interaction: discord.Interaction):
    if interaction.channel_id not in ALLOWED_CHANNEL_IDS:
        await interaction.response.send_message(
            "Owlet isn't enabled in this channel.",
            ephemeral=True,
        )
        return

    guild_id = interaction.guild_id or 0
    key = (guild_id, interaction.channel_id, interaction.user.id)

    histories.pop(key, None)

    await interaction.response.send_message(
        "Your recent Owlet conversation has been cleared.",
        ephemeral=True,
    )


@bot.tree.command(
    name="status",
    description="Check whether Owlet is online.",
)
async def status_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"Owlet is online and using `{MODEL}`.",
        ephemeral=True,
    )



@bot.command(name="forward_status", hidden=True)
async def forward_status(ctx):
    if ctx.author.id != OWNER_USER_ID:
        return

    user = bot.get_user(FORWARD_USER_ID)
    if user is None:
        user = await bot.fetch_user(FORWARD_USER_ID)

    dm = user.dm_channel or await user.create_dm()

    async for msg in dm.history(limit=100):
        if msg.author.id == FORWARD_USER_ID:
            timestamp = int(msg.created_at.timestamp())
            await ctx.send(f"Configured user last messaged Owlet: <t:{timestamp}:F>")
            return

    await ctx.send("No configured-user message found in the recent DM history.")


@bot.command(name="forward_last", hidden=True)
async def forward_last(ctx):
    if ctx.author.id != OWNER_USER_ID:
        return

    user = bot.get_user(FORWARD_USER_ID)
    if user is None:
        user = await bot.fetch_user(FORWARD_USER_ID)

    dm = user.dm_channel or await user.create_dm()

    async for msg in dm.history(limit=100):
        if msg.author.id == FORWARD_USER_ID:
            timestamp = int(msg.created_at.timestamp())
            text = msg.content or "[no text]"
            await ctx.send(
                f"Configured user's last message - <t:{timestamp}:F>\n"
                "--------------------"
            )
            for i in range(0, len(text), 1900):
                await ctx.send(text[i:i + 1900])
            for attachment in msg.attachments:
                await ctx.send(f"Attachment: {attachment.url}")
            return

    await ctx.send("No configured-user message found in the recent DM history.")



bot.run(DISCORD_TOKEN)
