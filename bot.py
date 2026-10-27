import asyncio
import hmac
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger('ugc')
GUILD = int(os.environ['DISCORD_GUILD_ID'])
CHANNEL = int(os.environ['DISCORD_CHANNEL_ID'])
GROUP = int(os.environ['ROBLOX_GROUP_ID'])
PLACE = int(os.environ['ROBLOX_PLACE_ID'])
SECRET = os.environ['BRIDGE_SECRET']
if len(SECRET) < 32:
    raise ValueError('BRIDGE_SECRET must be at least 32 characters')
path = Path(os.getenv('DATA_DIR', './data'))
path.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(path / 'bot.sqlite')
db.execute('CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY)')
db.execute('CREATE TABLE IF NOT EXISTS seasons (id TEXT PRIMARY KEY, name TEXT, ends INTEGER)')
db.commit()
lock = asyncio.Lock()


def seen(key):
    return db.execute('SELECT 1 FROM sent WHERE key=?', (key,)).fetchone() is not None


def mark(key):
    db.execute('INSERT OR IGNORE INTO sent VALUES (?)', (key,))
    db.commit()


class Bot(discord.Client):
    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        tree.copy_global_to(guild=discord.Object(id=GUILD))
        await tree.sync(guild=discord.Object(id=GUILD))
        app = web.Application(client_max_size=4096)
        app.router.add_post('/event', event)
        app.router.add_get('/health', health)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, '0.0.0.0', int(os.getenv('PORT', '8080'))).start()
        watch.start()

    async def close(self):
        watch.cancel()
        await self.runner.cleanup()
        await self.http_session.close()
        await super().close()


intents = discord.Intents.none()
intents.guilds = True
bot = Bot(intents=intents)
tree = app_commands.CommandTree(bot)


async def announce(key, title, description, asset=None, ping=True):
    async with lock:
        if seen(key):
            return False
        channel = await bot.fetch_channel(CHANNEL)
        if channel.guild.id != GUILD:
            raise ValueError('Channel is outside configured server')
        embed = discord.Embed(title=title[:256], description=description[:4096], color=0x9257FF)
        embed.set_footer(text='ROLL TO CLAIM • UGC ALERTS')
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label='Play game', url=f'https://www.roblox.com/games/{PLACE}'))
        if asset:
            view.add_item(discord.ui.Button(label='View item', url=f'https://www.roblox.com/catalog/{asset}'))
        await channel.send(content='@everyone' if ping else None, embed=embed, view=view,
                           allowed_mentions=discord.AllowedMentions(everyone=ping, users=False, roles=False))
        mark(key)
        return True


async def health(request):
    return web.json_response({'ready': bot.is_ready()}, status=200 if bot.is_ready() else 503)


async def event(request):
    if not hmac.compare_digest(request.headers.get('X-UGC-Key', ''), SECRET):
        raise web.HTTPUnauthorized()
    if not bot.is_ready():
        raise web.HTTPServiceUnavailable()
    try:
        payload = await request.json()
        kind = payload['kind']
        event_id = payload['id']
        name = payload['name']
        if not all(isinstance(x, str) and 0 < len(x) <= 150 for x in (kind, event_id, name)):
            raise ValueError()
        if kind not in ('reward', 'upload'):
            raise ValueError()
        asset = int(payload['asset_id'])
        if asset <= 0:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise web.HTTPBadRequest(text='Expected kind reward/upload, stable id, name, positive asset_id')
    title = '🎲 New UGC available in game!' if kind == 'reward' else '✨ New group UGC uploaded!'
    await announce(f'{kind}:{event_id}', title, discord.utils.escape_markdown(name), asset)
    return web.json_response({'ok': True})


async def catalog_scan():
    # Complete each traversal before updating the baseline: failures cannot seed partial data.
    items = {}
    cursor = None
    while True:
        params = dict(Category=11, CreatorType=2, CreatorTargetId=GROUP, SortType=3, Limit=30)
        if cursor:
            params['Cursor'] = cursor
        async with bot.http_session.get('https://catalog.roblox.com/v1/search/items/details', params=params) as res:
            res.raise_for_status()
            payload = await res.json()
        for item in payload['data']:
            if item.get('itemType') == 'Asset':
                items[str(item['id'])] = item['name']
        cursor = payload.get('nextPageCursor')
        if not cursor:
            break
        await asyncio.sleep(1)
    baseline = f'baseline:{GROUP}'
    if not seen(baseline):
        for asset in items:
            mark(f'catalog:{GROUP}:{asset}')
        mark(baseline)
        log.info('Group catalog baseline saved; existing items will not ping.')
        return
    for asset, name in items.items():
        await announce(f'catalog:{GROUP}:{asset}', '✨ New group item discovered!',
                       discord.utils.escape_markdown(name), asset)


@tasks.loop(seconds=60)
async def watch():
    now = int(time.time())
    for sid, name, ends in db.execute('SELECT id,name,ends FROM seasons').fetchall():
        try:
            if now >= ends:
                await announce(f'end:{sid}', '🏁 Season ended!', f'**{discord.utils.escape_markdown(name)}** has ended.')
            elif ends - now <= 86400:
                await announce(f'warning:{sid}', '⏳ Season ending soon!',
                               f'**{discord.utils.escape_markdown(name)}** ends <t:{ends}:R> — <t:{ends}:F>.')
        except Exception:
            log.exception('Season delivery failed; will retry')
    if time.monotonic() - getattr(bot, 'last_scan', -1000) >= 300:
        bot.last_scan = time.monotonic()
        try:
            await catalog_scan()
        except Exception:
            log.exception('Catalog check failed; will retry in five minutes')


@watch.before_loop
async def ready():
    await bot.wait_until_ready()


@tree.command(name='ugc-test', description='Send a preview without pinging everyone')
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def test(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await announce(f'test:{interaction.id}', '🎲 UGC alerts are ready!', 'New rewards and season updates will appear here.', ping=False)
    await interaction.followup.send('Preview sent without a ping.', ephemeral=True)


@tree.command(name='ugc-reward', description='Announce a reward that is now available in the game')
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def reward(interaction: discord.Interaction, release_id: str, name: str, asset_id: str):
    await interaction.response.defer(ephemeral=True)
    if not asset_id.isdigit() or int(asset_id) <= 0 or len(name) > 150 or len(release_id) > 150:
        return await interaction.followup.send('Use a positive asset ID and names under 150 characters.', ephemeral=True)
    sent = await announce(f'reward:{release_id}', '🎲 New UGC available in game!', discord.utils.escape_markdown(name), asset_id)
    await interaction.followup.send('Announcement sent.' if sent else 'Already announced this release ID.', ephemeral=True)


@tree.command(name='ugc-season', description='Schedule season end and a reminder within the final 24 hours')
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def season(interaction: discord.Interaction, season_id: str, name: str, ends_at: str):
    try:
        dt = datetime.fromisoformat(ends_at.replace('Z', '+00:00'))
        if dt.tzinfo is None or dt.timestamp() <= time.time() or len(name) > 150:
            raise ValueError()
    except ValueError:
        return await interaction.response.send_message('Use a future date with timezone, e.g. 2026-11-01T18:00:00+00:00.', ephemeral=True)
    if db.execute('SELECT 1 FROM seasons WHERE id=?', (season_id,)).fetchone():
        return await interaction.response.send_message('This season ID exists. Cancel it first; use a new ID if already announced.', ephemeral=True)
    db.execute('INSERT INTO seasons VALUES (?,?,?)', (season_id, name, int(dt.timestamp())))
    db.commit()
    await interaction.response.send_message(f'Season saved. Ends <t:{int(dt.timestamp())}:F>.', ephemeral=True)


@tree.command(name='ugc-cancel-season', description='Cancel a scheduled season')
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def cancel(interaction: discord.Interaction, season_id: str):
    db.execute('DELETE FROM seasons WHERE id=?', (season_id,))
    db.commit()
    await interaction.response.send_message('Schedule removed. Previous announcement history is retained.', ephemeral=True)


@tree.error
async def error(interaction, exc):
    log.error('Command failed: %s', type(exc).__name__)
    text = 'Admin permission required, or operation failed. Check bot logs.'
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


if __name__ == '__main__':
    bot.run(os.environ['DISCORD_TOKEN'])
