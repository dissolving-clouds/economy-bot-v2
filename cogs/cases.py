"""Кейсы: выпадают за сообщения и голосовой онлайн, открываются в /case.

Призы попадают в инвентарь (/inventory → «Инвентарь»):
  • монеты — копятся в «монетах инвентаря», забираются на баланс кнопкой «Забрать монеты»;
  • роль / роль с правами — «Перенести в хранилище», дальше надевается/снимается как обычная роль;
  • кастомная комната — «Использовать» → окно с названием и цветом (как при покупке);
  • любовная комната — «Использовать» → выбор партнёра → предложение в #корзина.

Шансы и призы настраиваются в /panel_case (config.CASE_ADMIN_IDS).
Роли из кейсов и срок роли с правами можно менять и из /panel (кнопка «Роли из кейсов»).
"""
import asyncio
import logging
import random
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
from cards import RARITY_NAMES, build_case_card, fmt_n
from checks import is_bot_admin
from database import RARITIES
from utils import TempView, add_role, delete_later, fetch_member, gif_embed, gif_file, role_is_manageable, send

RARITY_EMOJI = {"common": "⚪", "rare": "🔵", "legendary": "🟡"}
RARITY_COLOR = {
    "common": discord.Color.light_grey(),
    "rare": discord.Color.blue(),
    "legendary": discord.Color.gold(),
}
KIND_NAMES = {
    "coins": "Монеты",
    "role": "Роль",
    "permrole": "Роль с правами",
    "croom": "Кастомная комната",
    "loveroom": "Любовная комната",
}
KIND_EMOJI = {"coins": "💰", "role": "🎭", "permrole": "⚡", "croom": "🔊", "loveroom": "💞"}
PERM_WARNING = ("⚠️ **Роль с правами** выдана для удовольствия, а не для наказаний. Не злоупотребляй правами — "
                "администрация может снять эту роль в любой момент без возврата.")


def is_case_admin(user) -> bool:
    return user.id in config.CASE_ADMIN_IDS


def days_text(days: int) -> str:
    return "навсегда" if days <= 0 else f"{days} дн."


def prize_label(kind: str, role: discord.Role | None = None, days: int = 0, amount: int = 0) -> str:
    if kind == "coins":
        return f"{fmt_n(amount)} монет"
    if kind in ("role", "permrole"):
        name = role.name if role else "удалённая роль"
        prefix = "Роль с правами" if kind == "permrole" else "Роль"
        return f"{prefix} «{name}» ({days_text(days)})"
    return KIND_NAMES.get(kind, kind)


def loot_label(entry, guild: discord.Guild) -> str:
    kind = entry["kind"]
    if kind == "coins":
        lo, hi = entry["amount_min"], entry["amount_max"]
        return f"Монеты {fmt_n(lo)}–{fmt_n(hi)}" if lo != hi else f"Монеты {fmt_n(lo)}"
    if kind in ("role", "permrole"):
        role = guild.get_role(entry["role_id"])
        name = f"@{role.name}" if role else f"удалённая роль {entry['role_id']}"
        days = "срок из настроек" if kind == "permrole" else days_text(entry["days"])
        return f"{KIND_NAMES[kind]} {name} ({days})"
    return KIND_NAMES.get(kind, kind)


def item_label(item, guild: discord.Guild) -> str:
    role = guild.get_role(item["role_id"]) if item["role_id"] else None
    return f"{KIND_EMOJI.get(item['kind'], '🎁')} {prize_label(item['kind'], role, item['days'])}"


def _valid_loot(loot, guild: discord.Guild):
    """Убирает призы-роли, которых больше нет на сервере (чтобы не выдать «пустышку»)."""
    out = []
    for e in loot:
        if e["weight"] <= 0:
            continue
        if e["kind"] in ("role", "permrole") and guild.get_role(e["role_id"]) is None:
            continue
        out.append(e)
    return out


async def grant_prize(bot, guild: discord.Guild, user: discord.Member, rarity: str, entry, chance: float):
    """Кладёт приз в инвентарь. Возвращает (подпись, доп. текст)."""
    db = bot.db
    now = int(time.time())
    kind = entry["kind"]
    extra = ""
    if kind == "coins":
        lo, hi = sorted((entry["amount_min"], entry["amount_max"]))
        amount = random.randint(lo, hi)
        await db.add_wallet(user.id, amount)
        label = prize_label("coins", amount=amount)
        extra = "Монеты лежат в /inventory → «Инвентарь» → «Забрать монеты»."
    else:
        role = guild.get_role(entry["role_id"]) if entry["role_id"] else None
        days = entry["days"]
        if kind == "permrole":
            days = await db.get_int("case_perm_role_days")
        await db.add_case_item(user.id, kind, entry["role_id"], days, rarity, now)
        label = prize_label(kind, role, days)
        if kind in ("role", "permrole"):
            extra = "Забери её в /inventory → «Инвентарь» → «Перенести в хранилище», потом надень."
        else:
            extra = "Используй её в /inventory → «Инвентарь» → «Использовать»."
        if kind == "permrole":
            extra += "\n\n" + PERM_WARNING
    await db.add_case_log(now, user.id, rarity, kind, label, chance)
    return label, extra


# ================================================================ /case

class CaseView(TempView):
    def __init__(self, cog, user: discord.Member, cases: dict):
        super().__init__(owner_id=user.id, timeout=config.MENU_LIFETIME)
        self.cog = cog
        self.user = user
        self.buttons = {}
        styles = {"common": discord.ButtonStyle.secondary, "rare": discord.ButtonStyle.primary,
                  "legendary": discord.ButtonStyle.success}
        for rarity in RARITIES:
            btn = discord.ui.Button(style=styles[rarity], emoji=RARITY_EMOJI[rarity])
            btn.callback = self._make_callback(rarity)
            self.buttons[rarity] = btn
            self.add_item(btn)
        self.update_labels(cases)

    def update_labels(self, cases: dict):
        for rarity, btn in self.buttons.items():
            n = cases[rarity]["count"]
            btn.label = f"Открыть {RARITY_NAMES[rarity].lower()} ({n})"
            btn.disabled = n <= 0

    def _make_callback(self, rarity):
        async def callback(interaction: discord.Interaction):
            await self.cog.open_case(interaction, rarity, self)
        return callback


class Cases(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.msg_cooldown: dict[int, float] = {}
        self.opening: set[int] = set()

    # ---------- выпадение ----------
    async def roll_drop(self, user_id: int, source: str):
        """source: msg / voice. Выпадает максимум один кейс за раз, начиная с самого редкого."""
        db = self.bot.db
        for rarity in ("legendary", "rare", "common"):
            p = await db.get_float(f"case_{source}_{rarity}")
            if p > 0 and random.random() * 100 < p:
                await db.add_cases(user_id, rarity, 1)
                await self._notify_drop(user_id, rarity)
                return

    async def _notify_drop(self, user_id: int, rarity: str):
        channel = self.bot.get_channel(config.CART_CHANNEL_ID)
        if channel is None:
            return
        embed = discord.Embed(
            description=f"🎁 <@{user_id}>, тебе выпал **{RARITY_NAMES[rarity].lower()} кейс**! Открыть — /case",
            color=RARITY_COLOR[rarity],
        )
        kwargs = {"delete_after": config.CASE_NOTIFY_LIFETIME} if config.CASE_NOTIFY_LIFETIME > 0 else {}
        try:
            await channel.send(content=f"<@{user_id}>", embed=embed,
                               allowed_mentions=discord.AllowedMentions(users=True), **kwargs)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        now = time.monotonic()
        if now - self.msg_cooldown.get(message.author.id, 0) < config.CASE_MESSAGE_COOLDOWN:
            return
        self.msg_cooldown[message.author.id] = now
        try:
            await self.roll_drop(message.author.id, "msg")
        except Exception:
            logging.exception("Ошибка выпадения кейса (сообщения)")

    async def add_voice(self, user_ids: list[int]):
        """Вызывается из voice_tick раз в минуту."""
        try:
            for uid in set(user_ids):
                await self.roll_drop(uid, "voice")
        except Exception:
            logging.exception("Ошибка выпадения кейса (голосовые)")

    # ---------- открытие ----------
    async def open_case(self, interaction: discord.Interaction, rarity: str, view: CaseView | None = None):
        db, guild, user = self.bot.db, interaction.guild, interaction.user
        if user.id in self.opening:
            return await send(interaction, "⏳ Подожди, предыдущий кейс ещё открывается.", ephemeral=True)
        loot = _valid_loot(await db.get_loot(rarity), guild)
        total = sum(e["weight"] for e in loot)
        if not loot or total <= 0:
            return await send(interaction, "📭 Этот кейс пока пуст — администрация ещё не настроила призы.",
                              ephemeral=True)
        if not await db.take_case(user.id, rarity):
            return await send(interaction, f"❌ У тебя нет кейсов «{RARITY_NAMES[rarity]}».", ephemeral=True)

        self.opening.add(user.id)
        try:
            entry = random.choices(loot, weights=[e["weight"] for e in loot], k=1)[0]
            chance = entry["weight"] / total
            label, extra = await grant_prize(self.bot, guild, user, rarity, entry, chance)

            # гифка, потом результат
            title = f"{RARITY_EMOJI[rarity]} {RARITY_NAMES[rarity]} кейс"
            embed = gif_embed("case", title, f"{user.mention} открывает кейс…", RARITY_COLOR[rarity])
            file = gif_file("case")
            if file:
                await interaction.response.send_message(embed=embed, file=file)
            else:
                await interaction.response.send_message(embed=embed)
            message = await interaction.original_response()
            await asyncio.sleep(config.GIF_SECONDS.get("case", 3))

            result = discord.Embed(
                title=f"{title} открыт!",
                description=f"{user.mention} получает: **{label}**\nШанс: {chance * 100:.2f}%\n\n{extra}",
                color=RARITY_COLOR[rarity],
            )
            try:
                await interaction.edit_original_response(embed=result, attachments=[])
            except discord.HTTPException:
                message = await interaction.followup.send(embed=result, wait=True)
            delete_later(message, 90)
        finally:
            self.opening.discard(user.id)

        if view is not None and view.message is not None:  # обновляем картинку и кнопки /case
            try:
                view.update_labels(await db.get_cases(user.id))
                await view.message.edit(attachments=[await build_case_card(self.bot, user)], view=view)
            except discord.HTTPException:
                pass

    @app_commands.command(name="case", description="Твои кейсы: открыть и посмотреть")
    @app_commands.guild_only()
    async def case(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user = interaction.user
        file = await build_case_card(self.bot, user)
        view = CaseView(self, user, await self.bot.db.get_cases(user.id))
        view.message = await interaction.followup.send(file=file, view=view, wait=True)

    # ---------- панель кейсов ----------
    @app_commands.command(name="panel_case", description="Настройка кейсов: шансы и призы")
    @app_commands.guild_only()
    async def panel_case(self, interaction: discord.Interaction):
        if not is_case_admin(interaction.user):
            return await send(interaction, "⛔ Панель кейсов тебе недоступна.", ephemeral=True)
        await open_case_panel(self.bot, interaction, roles_only=False)


# ================================================================ инвентарь предметов (используется в /inventory)

class ItemsView(TempView):
    """Предметы из кейсов и монеты инвентаря. Открывается кнопкой «Инвентарь» в /inventory."""

    def __init__(self, bot, guild: discord.Guild, user: discord.Member):
        super().__init__(owner_id=user.id, timeout=config.MENU_LIFETIME)
        self.bot = bot
        self.guild = guild
        self.user = user
        self.selected: int | None = None
        self.select_item: discord.ui.Select | None = None

    async def build(self) -> discord.Embed:
        db = self.bot.db
        items = await db.get_case_items(self.user.id)
        wallet = await db.get_wallet(self.user.id)
        if self.select_item is not None:
            self.remove_item(self.select_item)
            self.select_item = None
        options = []
        for it in items[:25]:
            options.append(discord.SelectOption(
                label=item_label(it, self.guild)[:100], value=str(it["id"]),
                description=f"Из кейса: {RARITY_NAMES.get(it['rarity'], '')}"[:100],
                default=(it["id"] == self.selected),
            ))
        if options:
            sel = discord.ui.Select(placeholder="Выбери предмет…", options=options, row=0)

            async def on_select(interaction: discord.Interaction):
                self.selected = int(sel.values[0])
                await interaction.response.edit_message(embed=await self.build(), view=self)

            sel.callback = on_select
            self.select_item = sel
            self.add_item(sel)

        lines = [f"💰 **Монеты в инвентаре:** {await db.fmt(wallet)}", ""]
        if items:
            lines += [f"{i + 1}. {item_label(it, self.guild)}" for i, it in enumerate(items[:25])]
            if len(items) > 25:
                lines.append(f"…и ещё {len(items) - 25}")
        else:
            lines.append("Предметов нет. Они выпадают из кейсов — /case")
        embed = discord.Embed(title="📦 Инвентарь", description="\n".join(lines), color=discord.Color.blurple())
        embed.set_footer(text="Роли: выбери → «Перенести в хранилище». Комнаты: выбери → «Использовать».")
        return embed

    async def refresh(self, interaction: discord.Interaction):
        embed = await self.build()
        try:
            if interaction.response.is_done():
                await self.message.edit(embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)
        except (discord.HTTPException, AttributeError):
            pass

    async def selected_item(self, interaction: discord.Interaction, kinds: tuple):
        if self.selected is None:
            await interaction.response.send_message("Сначала выбери предмет в списке.", ephemeral=True)
            return None
        item = await self.bot.db.get_case_item(self.selected)
        if item is None or item["user_id"] != self.user.id:
            await interaction.response.send_message("❌ Этого предмета уже нет.", ephemeral=True)
            return None
        if item["kind"] not in kinds:
            what = "перенести в хранилище можно только роли" if "role" in kinds else "использовать можно только комнаты"
            await interaction.response.send_message(f"❌ Этот предмет так не работает: {what}.", ephemeral=True)
            return None
        return item

    @discord.ui.button(label="Перенести в хранилище", emoji="📦", style=discord.ButtonStyle.primary, row=1)
    async def to_storage(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = await self.selected_item(interaction, ("role", "permrole"))
        if not item:
            return
        db = self.bot.db
        role = self.guild.get_role(item["role_id"])
        if role is None:
            await db.take_case_item(item["id"], self.user.id)
            await interaction.response.send_message("❌ Эта роль удалена с сервера — предмет убран.", ephemeral=True)
            return await self.refresh(interaction)
        if not await db.take_case_item(item["id"], self.user.id):
            return await interaction.response.send_message("❌ Этого предмета уже нет.", ephemeral=True)
        now = int(time.time())
        days = item["days"]
        exp = 0 if days <= 0 else now + days * 86400
        existing = await db.get_item(self.user.id, role.id)
        if existing and (existing["expires_at"] == 0 or existing["expires_at"] > now):
            if existing["expires_at"] == 0:
                exp = 0
            elif exp:
                exp = max(existing["expires_at"], now) + days * 86400
            await db.set_item_expiry(self.user.id, role.id, exp)
        else:
            await db.add_item(self.user.id, role.id, exp, 0)
        term = "навсегда" if exp == 0 else f"до <t:{exp}:D>"
        text = (f"✅ Роль **{role.name}** перенесена в хранилище ({term}). "
                f"Надеть её можно в /inventory: выбери роль в списке → «Надеть». Дарить её нельзя.")
        if item["kind"] == "permrole":
            text += "\n\n" + PERM_WARNING
        await interaction.response.send_message(text, ephemeral=True)
        self.selected = None
        await self.refresh(interaction)

    @discord.ui.button(label="Использовать", emoji="✨", style=discord.ButtonStyle.success, row=1)
    async def use(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = await self.selected_item(interaction, ("croom", "loveroom"))
        if not item:
            return
        db = self.bot.db
        if item["kind"] == "croom":
            from cogs.custom_rooms import BuyRoomModal  # здесь, чтобы не было циклического импорта
            if await db.get_croom(self.user.id):
                return await interaction.response.send_message(
                    "❌ У тебя уже есть кастомная комната (/room). Предмет останется в инвентаре.", ephemeral=True)
            self.selected = None
            return await interaction.response.send_modal(BuyRoomModal(self.bot, item_id=item["id"], items_view=self))
        # любовная комната
        if await db.get_room_of(self.user.id):
            return await interaction.response.send_message(
                "❌ У тебя уже есть любовная комната. Предмет останется в инвентаре.", ephemeral=True)
        view = discord.ui.View(timeout=120)
        view.add_item(LovePartnerSelect(self.bot, item["id"]))
        await interaction.response.send_message("💞 **Выберите партнёра:**", view=view, ephemeral=True)

    @discord.ui.button(label="Забрать монеты", emoji="💰", style=discord.ButtonStyle.secondary, row=1)
    async def withdraw(self, interaction: discord.Interaction, button: discord.ui.Button):
        wallet = await self.bot.db.get_wallet(self.user.id)
        if wallet <= 0:
            return await interaction.response.send_message("💰 Монет в инвентаре нет.", ephemeral=True)
        await interaction.response.send_modal(WithdrawModal(self, wallet))


class WithdrawModal(discord.ui.Modal, title="Забрать монеты на баланс"):
    def __init__(self, items_view: ItemsView, wallet: int):
        super().__init__()
        self.items_view = items_view
        self.amount = discord.ui.TextInput(label=f"Сколько забрать? (доступно {wallet})", default=str(wallet),
                                           max_length=12)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        db = self.items_view.bot.db
        try:
            amount = int(self.amount.value.strip().replace(" ", ""))
            if amount <= 0:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("❌ Нужно целое число больше нуля.", ephemeral=True)
        user = interaction.user
        if not await db.wallet_to_balance(user.id, amount):
            left = await db.get_wallet(user.id)
            return await interaction.response.send_message(f"❌ В инвентаре только {await db.fmt(left)}.",
                                                           ephemeral=True)
        balance = await db.get_balance(user.id)
        left = await db.get_wallet(user.id)
        await interaction.response.send_message(
            f"💰 {user.mention} забрал(а) **{await db.fmt(amount)}** из инвентаря на баланс.\n"
            f"Баланс: **{await db.fmt(balance)}** · осталось в инвентаре: **{await db.fmt(left)}**"
        )
        delete_later(await interaction.original_response())
        await self.items_view.refresh(interaction)


class LovePartnerSelect(discord.ui.UserSelect):
    def __init__(self, bot, item_id: int):
        super().__init__(placeholder="Выберите партнёра")
        self.bot = bot
        self.item_id = item_id

    async def callback(self, interaction: discord.Interaction):
        from cogs.rooms import post_love_offer
        db = self.bot.db
        owner = interaction.user
        partner = await fetch_member(interaction.guild, self.values[0].id)
        item = await db.get_case_item(self.item_id)
        if item is None or item["user_id"] != owner.id:
            return await interaction.response.edit_message(content="❌ Этого предмета уже нет.", view=None)
        if partner is None or partner.bot or partner.id == owner.id:
            return await interaction.response.send_message("❌ Выбери другого человека (не себя и не бота).",
                                                           ephemeral=True)
        if await db.get_room_of(owner.id) or await db.get_room_of(partner.id):
            return await interaction.response.send_message("❌ У тебя или у партнёра уже есть любовная комната.",
                                                           ephemeral=True)
        ok = await post_love_offer(self.bot, owner, partner, item_id=self.item_id)
        if not ok:
            return await interaction.response.send_message("❌ Не удалось отправить предложение.", ephemeral=True)
        await interaction.response.edit_message(
            content=f"💌 Предложение для {partner.mention} отправлено в <#{config.CART_CHANNEL_ID}>. "
                    f"Комната появится, когда партнёр согласится (предмет спишется только тогда).",
            view=None,
        )


# ================================================================ панель кейсов

async def build_case_panel_embed(bot, guild: discord.Guild, rarity: str, roles_only: bool) -> discord.Embed:
    db = bot.db
    title = "🎁 Роли из кейсов" if roles_only else "🎁 Панель кейсов"
    embed = discord.Embed(title=title, color=RARITY_COLOR[rarity])
    if not roles_only:
        lines = []
        for r in RARITIES:
            lines.append(f"{RARITY_EMOJI[r]} **{RARITY_NAMES[r]}**: "
                         f"сообщение — {await db.get_float(f'case_msg_{r}'):g}% · "
                         f"минута в голосовом — {await db.get_float(f'case_voice_{r}'):g}%")
        lines.append(f"\nЗащита от спама: не чаще 1 шанса в {config.CASE_MESSAGE_COOLDOWN} сек. за сообщения")
        embed.add_field(name="Шанс получить кейс", value="\n".join(lines), inline=False)
    embed.add_field(name="Срок роли с правами", value=days_text(await db.get_int("case_perm_role_days")),
                    inline=False)

    loot = await db.get_loot(rarity)
    total = sum(e["weight"] for e in loot if e["weight"] > 0)
    rows = []
    for e in loot:
        pct = (e["weight"] / total * 100) if total and e["weight"] > 0 else 0
        rows.append(f"`#{e['id']}` {KIND_EMOJI.get(e['kind'], '🎁')} {loot_label(e, guild)} — "
                    f"вес {e['weight']:g} (**{pct:.2f}%**)")
    embed.add_field(
        name=f"Призы: {RARITY_EMOJI[rarity]} {RARITY_NAMES[rarity]} кейс",
        value="\n".join(rows)[:1024] if rows else "Пусто — добавь призы кнопками ниже.",
        inline=False,
    )
    embed.set_footer(text="Шанс приза = его вес / сумма весов в этом кейсе. Вес можно дробным: 0.5")
    return embed


class CasePanel(discord.ui.View):
    def __init__(self, bot, roles_only: bool):
        super().__init__(timeout=900)
        self.bot = bot
        self.roles_only = roles_only
        self.rarity = "common"
        self.message = None
        self.add_item(RaritySelect(self))
        if roles_only:  # из /panel: только роли и срок роли с правами
            for item in (self.add_coins, self.add_croom, self.add_love, self.chances_msg, self.chances_voice):
                self.remove_item(item)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_case_admin(interaction.user) or (self.roles_only and is_bot_admin(interaction.user)):
            return True
        await interaction.response.send_message("⛔ Нет доступа.", ephemeral=True)
        return False

    async def refresh(self, guild: discord.Guild):
        if self.message is None:
            return
        try:
            await self.message.edit(
                embed=await build_case_panel_embed(self.bot, guild, self.rarity, self.roles_only), view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Монеты", emoji="➕", style=discord.ButtonStyle.success, row=1)
    async def add_coins(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LootModal(self, "coins"))

    @discord.ui.button(label="Роль", emoji="➕", style=discord.ButtonStyle.success, row=1)
    async def add_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Выбери роль-приз:", view=PickLootRole(self, "role"), ephemeral=True)

    @discord.ui.button(label="Роль с правами", emoji="⚡", style=discord.ButtonStyle.success, row=1)
    async def add_perm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Выбери роль с правами:", view=PickLootRole(self, "permrole"),
                                                ephemeral=True)

    @discord.ui.button(label="Кастомная комната", emoji="🔊", style=discord.ButtonStyle.success, row=1)
    async def add_croom(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LootModal(self, "croom"))

    @discord.ui.button(label="Любовная комната", emoji="💞", style=discord.ButtonStyle.success, row=1)
    async def add_love(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LootModal(self, "loveroom"))

    @discord.ui.button(label="Изменить приз", emoji="✏️", style=discord.ButtonStyle.primary, row=2)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick_entry(interaction, "edit")

    @discord.ui.button(label="Удалить приз", emoji="🗑", style=discord.ButtonStyle.danger, row=2)
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick_entry(interaction, "delete")

    @discord.ui.button(label="Шансы: сообщения", emoji="💬", style=discord.ButtonStyle.secondary, row=2)
    async def chances_msg(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(await _chance_modal(self, "msg"))

    @discord.ui.button(label="Шансы: голосовой", emoji="🎙", style=discord.ButtonStyle.secondary, row=2)
    async def chances_voice(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(await _chance_modal(self, "voice"))

    @discord.ui.button(label="Срок роли с правами", emoji="⏳", style=discord.ButtonStyle.secondary, row=2)
    async def perm_days(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PermDaysModal(self, await self.bot.db.get_int("case_perm_role_days")))

    @discord.ui.button(label="Обновить", emoji="🔄", style=discord.ButtonStyle.secondary, row=3)
    async def reload(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=await build_case_panel_embed(self.bot, interaction.guild, self.rarity, self.roles_only), view=self)

    async def _pick_entry(self, interaction: discord.Interaction, action: str):
        loot = await self.bot.db.get_loot(self.rarity)
        if self.roles_only:
            loot = [e for e in loot if e["kind"] in ("role", "permrole")]
        if not loot:
            return await interaction.response.send_message("В этом кейсе нет подходящих призов.", ephemeral=True)
        view = discord.ui.View(timeout=120)
        view.add_item(LootEntrySelect(self, loot[:25], action, interaction.guild))
        what = "изменить" if action == "edit" else "удалить"
        await interaction.response.send_message(f"Какой приз {what}?", view=view, ephemeral=True)


class RaritySelect(discord.ui.Select):
    def __init__(self, panel: CasePanel):
        options = [discord.SelectOption(label=f"{RARITY_NAMES[r]} кейс", value=r, emoji=RARITY_EMOJI[r],
                                        default=(r == "common")) for r in RARITIES]
        super().__init__(placeholder="Какой кейс настраиваем?", options=options, row=0)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        self.panel.rarity = self.values[0]
        for opt in self.options:
            opt.default = opt.value == self.panel.rarity
        await interaction.response.edit_message(
            embed=await build_case_panel_embed(self.panel.bot, interaction.guild, self.panel.rarity,
                                               self.panel.roles_only),
            view=self.panel,
        )


def _parse_weight(raw: str) -> float | None:
    try:
        w = float(raw.strip().replace(",", "."))
    except ValueError:
        return None
    return w if 0 < w <= 1_000_000 else None


class LootModal(discord.ui.Modal):
    """Добавление/изменение приза. entry=None — новый приз."""

    def __init__(self, panel: CasePanel, kind: str, role: discord.Role | None = None, entry=None):
        super().__init__(title=("Изменить приз" if entry else f"Новый приз: {KIND_NAMES[kind]}")[:45])
        self.panel, self.kind, self.role, self.entry = panel, kind, role, entry
        self.weight = discord.ui.TextInput(label="Вес (чем больше, тем чаще выпадает)",
                                           default=f"{entry['weight']:g}" if entry else "10", max_length=12)
        self.add_item(self.weight)
        self.amin = self.amax = self.days = None
        if kind == "coins":
            self.amin = discord.ui.TextInput(label="Монет от", default=str(entry["amount_min"]) if entry else "100",
                                             max_length=12)
            self.amax = discord.ui.TextInput(label="Монет до", default=str(entry["amount_max"]) if entry else "300",
                                             max_length=12)
            self.add_item(self.amin)
            self.add_item(self.amax)
        if kind == "role":
            self.days = discord.ui.TextInput(label="Срок роли в днях (0 = навсегда)",
                                             default=str(entry["days"]) if entry else "7", max_length=5)
            self.add_item(self.days)

    async def on_submit(self, interaction: discord.Interaction):
        db = self.panel.bot.db
        weight = _parse_weight(self.weight.value)
        if weight is None:
            return await interaction.response.send_message("❌ Вес — число больше 0 (можно дробное: 0.5).",
                                                           ephemeral=True)
        fields = {"weight": weight}
        try:
            if self.kind == "coins":
                lo, hi = int(self.amin.value), int(self.amax.value)
                if lo < 1 or hi < 1:
                    raise ValueError
                fields["amount_min"], fields["amount_max"] = min(lo, hi), max(lo, hi)
            if self.kind == "role":
                fields["days"] = int(self.days.value)
                if not 0 <= fields["days"] <= 3650:
                    raise ValueError
        except ValueError:
            return await interaction.response.send_message("❌ Проверь числа: монеты ≥ 1, дни от 0 до 3650.",
                                                           ephemeral=True)
        if self.entry:
            await db.update_loot(self.entry["id"], **fields)
            text = "✅ Приз изменён."
        else:
            await db.add_loot(self.panel.rarity, self.kind, role_id=self.role.id if self.role else 0, **fields)
            text = f"✅ Приз добавлен в {RARITY_NAMES[self.panel.rarity].lower()} кейс."
        await interaction.response.send_message(text, ephemeral=True)
        await self.panel.refresh(interaction.guild)


class PickLootRole(discord.ui.View):
    def __init__(self, panel: CasePanel, kind: str):
        super().__init__(timeout=120)
        self.panel, self.kind = panel, kind

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Выбери роль")
    async def pick(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0]
        if not role_is_manageable(interaction.guild, role):
            return await interaction.response.send_message(
                "❌ Я не смогу выдать эту роль: она выше роли бота или служебная.", ephemeral=True)
        if role.permissions.administrator:
            return await interaction.response.send_message(
                "❌ Роль с правом «Администратор» выдавать из кейса слишком опасно — выбери роль попроще.",
                ephemeral=True)
        await interaction.response.send_modal(LootModal(self.panel, self.kind, role=role))


class LootEntrySelect(discord.ui.Select):
    def __init__(self, panel: CasePanel, loot, action: str, guild: discord.Guild):
        options = [discord.SelectOption(label=f"#{e['id']} {loot_label(e, guild)}"[:100], value=str(e["id"]),
                                        description=f"вес {e['weight']:g}") for e in loot]
        super().__init__(placeholder="Выбери приз", options=options)
        self.panel, self.action = panel, action

    async def callback(self, interaction: discord.Interaction):
        db = self.panel.bot.db
        entry = await db.get_loot_entry(int(self.values[0]))
        if entry is None:
            return await interaction.response.edit_message(content="Этого приза уже нет.", view=None)
        if self.action == "delete":
            await db.delete_loot(entry["id"])
            await interaction.response.edit_message(content=f"🗑 Приз #{entry['id']} удалён.", view=None)
            return await self.panel.refresh(interaction.guild)
        await interaction.response.send_modal(LootModal(self.panel, entry["kind"], entry=entry))


class ChanceModal(discord.ui.Modal):
    def __init__(self, panel: CasePanel, source: str):
        super().__init__(title="Шанс кейса за сообщение, %" if source == "msg" else "Шанс кейса за минуту войса, %")
        self.panel, self.source = panel, source
        self.inputs = {}

    async def on_submit(self, interaction: discord.Interaction):
        db = self.panel.bot.db
        values = {}
        for rarity, field in self.inputs.items():
            try:
                v = float(field.value.strip().replace(",", "."))
            except ValueError:
                return await interaction.response.send_message("❌ Шанс — число от 0 до 100 (например 0.5).",
                                                               ephemeral=True)
            if not 0 <= v <= 100:
                return await interaction.response.send_message("❌ Шанс — число от 0 до 100.", ephemeral=True)
            values[rarity] = v
        for rarity, v in values.items():
            await db.set(f"case_{self.source}_{rarity}", f"{v:g}")
        await interaction.response.send_message("✅ Шансы сохранены.", ephemeral=True)
        await self.panel.refresh(interaction.guild)


class PermDaysModal(discord.ui.Modal, title="Срок роли с правами"):
    def __init__(self, panel: CasePanel, current: int):
        super().__init__()
        self.panel = panel
        self.days = discord.ui.TextInput(label="Сколько дней действует (1–365)", default=str(current), max_length=3)
        self.add_item(self.days)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            days = int(self.days.value)
            if not 1 <= days <= 365:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("❌ Нужно число от 1 до 365.", ephemeral=True)
        await self.panel.bot.db.set("case_perm_role_days", str(days))
        await interaction.response.send_message(f"✅ Роль с правами теперь выдаётся на {days} дн.", ephemeral=True)
        await self.panel.refresh(interaction.guild)


async def _chance_modal(panel: CasePanel, source: str) -> ChanceModal:
    modal = ChanceModal(panel, source)
    for rarity in RARITIES:
        current = await panel.bot.db.get_float(f"case_{source}_{rarity}")
        field = discord.ui.TextInput(label=f"{RARITY_NAMES[rarity]} кейс, %", default=f"{current:g}", max_length=10)
        modal.inputs[rarity] = field
        modal.add_item(field)
    return modal


async def open_case_panel(bot, interaction: discord.Interaction, roles_only: bool):
    panel = CasePanel(bot, roles_only)
    embed = await build_case_panel_embed(bot, interaction.guild, panel.rarity, roles_only)
    await send(interaction, embed=embed, view=panel, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Cases(bot))
