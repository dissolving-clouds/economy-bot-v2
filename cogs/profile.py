"""Профиль участника + подсчёт сообщений и времени в голосовых каналах."""
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import quests
from cogs.rooms import extend_room
from profile_card import build_profile_card
from utils import fmt_num, fmt_voice, send


class ProfileView(discord.ui.View):
    """Кнопка продления любовной комнаты. Постоянная: работает и после перезапуска бота."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Продлить любовную комнату", emoji="💞", style=discord.ButtonStyle.primary,
                       custom_id="profile:extend_room")
    async def extend(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, text = await extend_room(self.bot, interaction.user)
        await send(interaction, text)


class Profile(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(ProfileView(self.bot))
        self.voice_tick.start()

    async def cog_unload(self):
        self.voice_tick.cancel()

    # ---------- сбор статистики ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        await self.bot.db.add_message(message.author.id)

    @tasks.loop(seconds=60)
    async def voice_tick(self):
        """Раз в минуту добавляет по 60 секунд (и монеты за онлайн) всем, кто сидит в голосовых.
        AFK-канал не считается."""
        try:
            ids = []
            for guild in self.bot.guilds:
                for channel in [*guild.voice_channels, *guild.stage_channels]:
                    if channel == guild.afk_channel:
                        continue
                    ids.extend(m.id for m in channel.members if not m.bot)
            if ids:
                reward = await self.bot.db.get_int("voice_reward")
                await self.bot.db.add_voice(ids, 60, reward)
                await self.bot.db.add_daily_voice(ids, quests.today(), 60)  # для топа дня
                quests_cog = self.bot.get_cog("Quests")
                if quests_cog is not None:
                    await quests_cog.add_voice(ids, 60)  # прогресс ежедневных заданий
                cases_cog = self.bot.get_cog("Cases")
                if cases_cog is not None:
                    await cases_cog.add_voice(ids)  # шанс получить кейс
        except Exception:
            logging.exception("Ошибка подсчёта голосовых")

    @voice_tick.before_loop
    async def before_voice_tick(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(60)

    # ---------- /profile ----------
    @app_commands.command(name="profile", description="Профиль участника")
    @app_commands.describe(member="Чей профиль показать (по умолчанию твой)")
    @app_commands.guild_only()
    async def profile(self, interaction: discord.Interaction, member: discord.Member | None = None):
        db = self.bot.db
        member = member or interaction.user
        await interaction.response.defer()  # обычный (публичный) ответ — его видят все

        room = await db.get_room_of(member.id)
        # картинка профиля с реальными данными из базы
        file = await build_profile_card(self.bot, member)
        # кнопка продления только владельцу комнаты, смотрящему свой профиль
        view = ProfileView(self.bot) if room else None
        await interaction.followup.send(file=file, view=view) if view else await interaction.followup.send(file=file)  # профиль не удаляется


async def setup(bot):
    await bot.add_cog(Profile(bot))
