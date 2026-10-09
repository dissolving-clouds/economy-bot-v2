"""Сапёр (/miner): поле 10x10, 10–12 бомб.

Discord разрешает максимум 25 кнопок в сообщении, поэтому поле 10x10 устроено так:
сверху всё поле рисуется эмодзи, игрок выбирает строку (A–J) в меню и жмёт кнопку столбца (1–10).
За каждую пустую клетку выигрыш растёт на miner_step % от ставки (настраивается в /panel → «Казино и дуэли»).
«Уйти с выигрышем» — забрать текущий выигрыш. Бомба — «Бум!!!», ставка сгорает.
"""
import random

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import delete_later, send

SIZE = 10
ROWS = "ABCDEFGHIJ"
ROW_EMOJI = ["🇦", "🇧", "🇨", "🇩", "🇪", "🇫", "🇬", "🇭", "🇮", "🇯"]
COL_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
HIDDEN, SAFE, BOMB, BOOM = "🟦", "💎", "💣", "💥"


class MinerGame(discord.ui.View):
    def __init__(self, cog, user: discord.Member, bet: int, bombs: int, step: int):
        super().__init__(timeout=config.MINER_TIMEOUT)
        self.cog, self.user, self.bet, self.step = cog, user, bet, step
        cells = [(r, c) for r in range(SIZE) for c in range(SIZE)]
        self.bombs = set(random.sample(cells, bombs))
        self.opened: set[tuple[int, int]] = set()
        self.row = 0
        self.finished = False
        self.exploded: tuple[int, int] | None = None
        self.message: discord.Message | None = None

        self.row_select = discord.ui.Select(placeholder="Строка", row=0, options=[
            discord.SelectOption(label=f"Строка {ROWS[i]}", value=str(i), emoji=ROW_EMOJI[i], default=(i == 0))
            for i in range(SIZE)
        ])
        self.row_select.callback = self.on_row
        self.add_item(self.row_select)
        self.col_buttons = []
        for c in range(SIZE):
            btn = discord.ui.Button(label=str(c + 1), style=discord.ButtonStyle.secondary, row=1 + c // 5)
            btn.callback = self._make_click(c)
            self.col_buttons.append(btn)
            self.add_item(btn)
        self.cash_btn = discord.ui.Button(label="Уйти с выигрышем", emoji="💰", style=discord.ButtonStyle.success,
                                          row=3)
        self.cash_btn.callback = self.on_cash
        self.add_item(self.cash_btn)
        self.sync_buttons()

    # ---------- расчёты ----------
    @property
    def multiplier(self) -> float:
        return 1 + self.step / 100 * len(self.opened)

    @property
    def win(self) -> int:
        return int(self.bet * self.multiplier)

    def grid(self) -> str:
        lines = ["⬛" + "".join(COL_EMOJI)]
        for r in range(SIZE):
            row = []
            for c in range(SIZE):
                cell = (r, c)
                if cell == self.exploded:
                    row.append(BOOM)
                elif cell in self.opened:
                    row.append(SAFE)
                elif self.finished and cell in self.bombs:
                    row.append(BOMB)
                else:
                    row.append(HIDDEN)
            mark = " ◀️" if (r == self.row and not self.finished) else ""
            lines.append(ROW_EMOJI[r] + "".join(row) + mark)
        return "\n".join(lines)

    async def embed(self, status: str = "", color=None) -> discord.Embed:
        db = self.cog.bot.db
        e = discord.Embed(title="💣 Сапёр", description=self.grid(), color=color or discord.Color.blurple())
        e.add_field(name="Ставка", value=await db.fmt(self.bet))
        e.add_field(name="Бомб на поле", value=str(len(self.bombs)))
        e.add_field(name="Открыто клеток", value=str(len(self.opened)))
        e.add_field(name="Множитель", value=f"x{self.multiplier:.2f}")
        e.add_field(name="Тотальный выигрыш", value=f"**{await db.fmt(self.win)}**")
        e.add_field(name="За клетку", value=f"+{self.step}% от ставки")
        if status:
            e.add_field(name="​", value=status, inline=False)
        e.set_footer(text=f"{self.user.display_name} · выбери строку, затем нажми номер столбца")
        return e

    def sync_buttons(self):
        for opt in self.row_select.options:
            opt.default = opt.value == str(self.row)
        for c, btn in enumerate(self.col_buttons):
            cell = (self.row, c)
            btn.emoji = None
            btn.label = str(c + 1)
            btn.style = discord.ButtonStyle.secondary
            btn.disabled = self.finished
            if cell == self.exploded:
                btn.label, btn.style = "Бум!!!", discord.ButtonStyle.danger
                btn.emoji = BOOM
            elif cell in self.opened:
                btn.emoji, btn.style, btn.disabled = SAFE, discord.ButtonStyle.success, True
            elif self.finished and cell in self.bombs:
                btn.emoji, btn.style = BOMB, discord.ButtonStyle.danger
        self.row_select.disabled = self.finished
        self.cash_btn.disabled = self.finished or not self.opened
        self.cash_btn.label = f"Уйти с выигрышем ({self.win})" if self.opened else "Уйти с выигрышем"

    # ---------- кнопки ----------
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("⛔ Это чужая игра. Начни свою: /miner", ephemeral=True)
            return False
        return True

    async def on_row(self, interaction: discord.Interaction):
        self.row = int(self.row_select.values[0])
        self.sync_buttons()
        await interaction.response.edit_message(embed=await self.embed(), view=self)

    def _make_click(self, col: int):
        async def callback(interaction: discord.Interaction):
            await self.click(interaction, (self.row, col))
        return callback

    async def click(self, interaction: discord.Interaction, cell):
        if self.finished or cell in self.opened:
            return await interaction.response.defer()
        if cell in self.bombs:
            self.exploded = cell
            await self.finish()
            self.sync_buttons()
            balance = await self.cog.bot.db.get_balance(self.user.id)
            status = (f"💥 **Бум!!!** Клетка {ROWS[cell[0]]}{cell[1] + 1} — бомба.\n"
                      f"Ставка {await self.cog.bot.db.fmt(self.bet)} сгорела. Баланс: {balance}")
            await interaction.response.edit_message(embed=await self.embed(status, discord.Color.red()), view=self)
            return delete_later(self.message, 60)
        self.opened.add(cell)
        if len(self.opened) == SIZE * SIZE - len(self.bombs):  # открыто всё — автоматически забираем
            return await self.cash_out(interaction, "🏆 Открыты все пустые клетки!")
        self.sync_buttons()
        await interaction.response.edit_message(
            embed=await self.embed(f"💎 {ROWS[cell[0]]}{cell[1] + 1} — пусто! Можно идти дальше или забрать выигрыш."),
            view=self,
        )

    async def on_cash(self, interaction: discord.Interaction):
        await self.cash_out(interaction, "💰 Ты ушёл с выигрышем!")

    async def cash_out(self, interaction: discord.Interaction | None, title: str):
        if self.finished:
            return
        db = self.cog.bot.db
        win = self.win if self.opened else self.bet  # ничего не открыл — ставка возвращается
        await self.finish()
        await db.add_balance(self.user.id, win)
        self.sync_buttons()
        balance = await db.get_balance(self.user.id)
        status = (f"{title}\nВыигрыш: **{await db.fmt(win)}** (ставка {await db.fmt(self.bet)}, "
                  f"чистыми {'+' if win >= self.bet else ''}{win - self.bet}). Баланс: {balance}")
        embed = await self.embed(status, discord.Color.green())
        if interaction is not None:
            await interaction.response.edit_message(embed=embed, view=self)
        elif self.message is not None:
            try:
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass
        delete_later(self.message, 60)

    async def finish(self):
        self.finished = True
        self.stop()
        self.cog.active.discard(self.user.id)

    async def on_timeout(self):
        await self.cash_out(None, "⌛ Время вышло — выигрыш забран автоматически.")


class Miner(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.active: set[int] = set()

    @app_commands.command(name="miner", description="Сапёр: открывай клетки, не попади на бомбу")
    @app_commands.describe(bet="Ставка")
    @app_commands.guild_only()
    async def miner(self, interaction: discord.Interaction, bet: app_commands.Range[int, 1]):
        db = self.bot.db
        if interaction.user.id in self.active:
            return await send(interaction, "❌ У тебя уже идёт игра в сапёра — доиграй её.")
        max_bet = await db.get_int("max_bet")
        if bet > max_bet:
            return await send(interaction, f"❌ Максимальная ставка: {await db.fmt(max_bet)}.")
        if not await db.try_remove(interaction.user.id, bet):
            return await send(interaction, f"❌ {interaction.user.mention}, у тебя не хватает монет.")
        lo = max(1, await db.get_int("miner_bombs_min"))
        hi = max(1, await db.get_int("miner_bombs_max"))
        lo, hi = min(lo, hi), max(lo, hi)
        bombs = min(random.randint(lo, hi), SIZE * SIZE - 1)
        step = max(1, await db.get_int("miner_step"))
        self.active.add(interaction.user.id)
        game = MinerGame(self, interaction.user, bet, bombs, step)
        await interaction.response.send_message(embed=await game.embed(), view=game)
        game.message = await interaction.original_response()


async def setup(bot):
    await bot.add_cog(Miner(bot))
