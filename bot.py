import os
import discord
from discord import app_commands
import asyncpg

TOKEN = os.environ["DISCORD_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or 0)
GUILD_ID = int(os.getenv("GUILD_ID") or 0)

# Table or view to read; see cruise_prices_view.sql for a view that also includes the tracker's own history
TABLE = os.getenv("PRICE_TABLE", "public.royal_carribean")
PERIODS = {
    "day": ("Past day", "1 day"),
    "week": ("Past week", "7 days"),
    "month": ("Past month", "1 month"),
    "year": ("Past year", "1 year"),
    "all": ("All time", None),
}
PERIOD_CHOICES = [app_commands.Choice(name=v[0], value=k) for k, v in PERIODS.items()]


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.pool: asyncpg.Pool | None = None

    async def setup_hook(self):
        self.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


bot = Bot()


# ---------- helpers ----------

def where(period: str, room: str | None, args: list) -> str:
    """Build a WHERE clause; appends bind values to args."""
    clauses = []
    interval = PERIODS[period][1]
    if interval:
        clauses.append(f"datetime >= now() - interval '{interval}'")
    if room:
        args.append(room)
        clauses.append(f"room_type = ${len(args)}")
    return ("WHERE " + " AND ".join(clauses)) if clauses else ""


def ts(dt) -> str:
    # Discord renders this in each viewer's own time zone
    return f"<t:{int(dt.timestamp())}:f>"


def money(v) -> str:
    return f"${v:,.2f}"


async def check_channel(interaction: discord.Interaction) -> bool:
    if CHANNEL_ID and interaction.channel_id != CHANNEL_ID:
        await interaction.response.send_message(
            f"Please use <#{CHANNEL_ID}> for cruise commands.", ephemeral=True
        )
        return False
    return True


async def room_autocomplete(interaction: discord.Interaction, current: str):
    rows = await bot.pool.fetch(
        f"SELECT DISTINCT room_type FROM {TABLE} WHERE room_type ILIKE $1 ORDER BY 1 LIMIT 25",
        f"%{current}%",
    )
    return [app_commands.Choice(name=r["room_type"], value=r["room_type"]) for r in rows]


# ---------- commands ----------

async def extreme(interaction, period, room, order, label):
    if not await check_channel(interaction):
        return
    args: list = []
    w = where(period, room, args)
    # one result per room type/cruise (the best price in the window), earliest time it was seen
    rows = await bot.pool.fetch(
        f"""SELECT DISTINCT ON (room_type, cruise_description) *
            FROM {TABLE} {w}
            ORDER BY room_type, cruise_description, price {order}, datetime ASC""",
        *args,
    )
    if not rows:
        return await interaction.response.send_message("No data for that period.")
    embed = discord.Embed(title=f"{label} prices — {PERIODS[period][0].lower()}", color=0x1E88E5)
    for r in rows:
        embed.add_field(
            name=f"{r['room_type']} · {r['cruise_description']}",
            value=f"**{money(r['price'])}**\nfirst seen at this price: {ts(r['datetime'])}",
            inline=False,
        )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="lowest", description="Lowest price seen in a time period")
@app_commands.choices(period=PERIOD_CHOICES)
@app_commands.autocomplete(room=room_autocomplete)
async def lowest(interaction: discord.Interaction, period: app_commands.Choice[str], room: str | None = None):
    await extreme(interaction, period.value, room, "ASC", "Lowest")


@bot.tree.command(name="highest", description="Highest price seen in a time period")
@app_commands.choices(period=PERIOD_CHOICES)
@app_commands.autocomplete(room=room_autocomplete)
async def highest(interaction: discord.Interaction, period: app_commands.Choice[str], room: str | None = None):
    await extreme(interaction, period.value, room, "DESC", "Highest")


@bot.tree.command(name="current", description="Most recent price for each room type")
async def current(interaction: discord.Interaction):
    if not await check_channel(interaction):
        return
    rows = await bot.pool.fetch(
        f"SELECT DISTINCT ON (room_type, cruise_description) * FROM {TABLE} "
        "ORDER BY room_type, cruise_description, datetime DESC"
    )
    if not rows:
        return await interaction.response.send_message("No data yet.")
    embed = discord.Embed(title="Current prices", color=0x43A047)
    for r in rows:
        embed.add_field(
            name=f"{r['room_type']} · {r['cruise_description']}",
            value=f"**{money(r['price'])}**\nlast alert: {ts(r['datetime'])}",
            inline=False,
        )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="stats", description="Min / average / max / count for a time period")
@app_commands.choices(period=PERIOD_CHOICES)
@app_commands.autocomplete(room=room_autocomplete)
async def stats(interaction: discord.Interaction, period: app_commands.Choice[str], room: str | None = None):
    if not await check_channel(interaction):
        return
    args: list = []
    w = where(period.value, room, args)
    rows = await bot.pool.fetch(
        f"""SELECT room_type, cruise_description, min(price) lo, round(avg(price), 2) av,
                   max(price) hi, count(*) n
            FROM {TABLE} {w} GROUP BY 1, 2 ORDER BY 1, 2""",
        *args,
    )
    if not rows:
        return await interaction.response.send_message("No data for that period.")
    embed = discord.Embed(title=f"Stats — {period.name.lower()}", color=0x8E24AA)
    for r in rows:
        embed.add_field(
            name=f"{r['room_type']} · {r['cruise_description']}",
            value=f"Min {money(r['lo'])} · Avg {money(r['av'])} · Max {money(r['hi'])} · {r['n']} alerts",
            inline=False,
        )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="changes", description="Recent price changes (each time the price moved)")
@app_commands.autocomplete(room=room_autocomplete)
async def changes(interaction: discord.Interaction, room: str | None = None, limit: app_commands.Range[int, 1, 20] = 10):
    if not await check_channel(interaction):
        return
    args: list = []
    room_filter = ""
    if room:
        args.append(room)
        room_filter = "WHERE room_type = $1"
    args.append(limit)
    rows = await bot.pool.fetch(
        f"""SELECT * FROM (
                SELECT *, lag(price) OVER (PARTITION BY room_type, cruise_description ORDER BY datetime) AS prev
                FROM {TABLE} {room_filter}
            ) t
            WHERE prev IS NOT NULL AND price <> prev
            ORDER BY datetime DESC LIMIT ${len(args)}""",
        *args,
    )
    if not rows:
        return await interaction.response.send_message("No price changes recorded.")
    lines = []
    for r in rows:
        diff = r["price"] - r["prev"]
        arrow = "🟢" if diff < 0 else "🔴"
        sign = "+" if diff > 0 else "-"
        lines.append(
            f"{arrow} {r['room_type']}: {money(r['prev'])} → **{money(r['price'])}** "
            f"({sign}{money(abs(diff))}) · {ts(r['datetime'])}"
        )
    await interaction.response.send_message(
        embed=discord.Embed(title="Recent price changes", description="\n".join(lines), color=0xFB8C00)
    )


@bot.tree.command(name="history", description="Distinct price points over time for a room type")
@app_commands.autocomplete(room=room_autocomplete)
async def history(interaction: discord.Interaction, room: str, limit: app_commands.Range[int, 1, 25] = 15):
    if not await check_channel(interaction):
        return
    rows = await bot.pool.fetch(
        f"""SELECT * FROM (
                SELECT *, lag(price) OVER (ORDER BY datetime) AS prev
                FROM {TABLE} WHERE room_type = $1
            ) t WHERE prev IS NULL OR price <> prev
            ORDER BY datetime DESC LIMIT $2""",
        room, limit,
    )
    if not rows:
        return await interaction.response.send_message("No data for that room type.")
    desc = "\n".join(f"{money(r['price'])} · from {ts(r['datetime'])}" for r in rows)
    await interaction.response.send_message(
        embed=discord.Embed(title=f"Price history — {room}", description=desc, color=0x00897B)
    )


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


bot.run(TOKEN)
