"""Telegram-бот: анкеты с AI-рейтингом лица (0-5), мнения и батлы.

ТОКЕН: вставь его ОДИН РАЗ в BOT_TOKEN ниже (или задай переменную окружения
BOT_TOKEN на Railway - так безопаснее). Если нигде не задан, бот спросит его
в консоли при первом запуске и сохранит в файл .token - дальше читает сам.
"""
import asyncio
import html
import io
import json
import logging
import os
import sys
from collections import defaultdict

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InputMediaPhoto, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

import db
import face

# =====================================================================
8684862282:AAEW24gWNwiiSCvm040snJ45z-LoR4ZLUOU = ""  # <-- ВСТАВЬ ТОКЕН ОТ @BotFather СЮДА (один раз)
# =====================================================================
TOKEN_FILE = ".token"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")


def get_token() -> str:
    tok = os.getenv("BOT_TOKEN", "").strip() or BOT_TOKEN.strip()
    if tok:
        return tok
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="utf-8") as f:
            tok = f.read().strip()
        if tok:
            return tok
    try:
        tok = input("Введи токен бота от @BotFather: ").strip()
    except EOFError:
        tok = ""
    if not tok:
        sys.exit("Токен не задан: впиши его в BOT_TOKEN в bot.py или задай переменную BOT_TOKEN.")
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(tok)
    return tok


# ---------------------------------------------------------------- типы
TYPES = ["Sub 3", "Sub 5", "ITN", "MTN", "HTN", "Chadlite", "Chad", "True Adam"]
THRESH = [(1.0, "Sub 3"), (1.8, "Sub 5"), (2.5, "ITN"), (3.2, "MTN"),
          (3.8, "HTN"), (4.4, "Chadlite"), (4.8, "Chad")]


def ai_type(score: float) -> str:
    for lim, t in THRESH:
        if score < lim:
            return t
    return "True Adam"


def esc(s) -> str:
    return html.escape(str(s))


def stars(s: float) -> str:
    n = max(0, min(5, round(s)))
    return "★" * n + "☆" * (5 - n)


def card(u: dict) -> str:
    return (f"<b>{esc(u['name'])}</b> · {esc(u['grp'])} · {u['course']} курс\n"
            f"Пользователь думает, что он: <b>{esc(u['self_type'])}</b>\n"
            f"🤖 ИИ: <b>{u['ai_score']:.1f}/5</b> {stars(u['ai_score'])} → <b>{esc(u['ai_type'])}</b>")


def metrics_text(metrics: dict) -> str:
    lines = ["<b>📊 Характеристики (оценка алгоритма)</b>"]
    for k, (title, fmt) in face.METRIC_INFO.items():
        m = metrics[k]
        n = round(m["score"] * 10)
        bar = "█" * n + "░" * (10 - n)
        lines.append(f"{title}: {fmt.format(m['value'])}\n<code>{bar}</code> {m['score'] * 100:.0f}%")
    lines.append("\n<i>Это развлекательная эвристика по пропорциям лица, а не научный вердикт.</i>")
    return "\n".join(lines)


def aggregate(photos: list) -> tuple:
    n = len(photos)
    score = sum(p["score"] for p in photos) / n
    metrics = {
        k: {"value": sum(p["metrics"][k]["value"] for p in photos) / n,
            "score": sum(p["metrics"][k]["score"] for p in photos) / n}
        for k in face.METRIC_INFO
    }
    return round(score, 2), metrics


# ---------------------------------------------------------------- UI
BTN_RATE = "🔥 Оценивать"
BTN_BATTLE = "⚔️ Батл"
BTN_ME = "👤 Моя анкета"
BTN_TOP = "🏆 Топ"
BTN_REG = "📝 Заполнить анкету"
BTN_DEL = "🗑 Удалить анкету"


def main_kb():
    b = ReplyKeyboardBuilder()
    for t in (BTN_RATE, BTN_BATTLE, BTN_ME, BTN_TOP, BTN_REG, BTN_DEL):
        b.button(text=t)
    b.adjust(2, 2, 2)
    return b.as_markup(resize_keyboard=True)


def kb_vote(uid: int):
    b = InlineKeyboardBuilder()
    b.button(text="✅ Согласен", callback_data=f"agree:{uid}")
    b.button(text="❌ Не согласен", callback_data=f"dis:{uid}")
    b.button(text="📊 Характеристики", callback_data=f"met:{uid}")
    b.button(text="⏭ Пропустить", callback_data=f"skip:{uid}")
    b.adjust(2, 2)
    return b.as_markup()


def kb_types(uid: int):
    b = InlineKeyboardBuilder()
    for i, t in enumerate(TYPES):
        b.button(text=t, callback_data=f"pick:{uid}:{i}")
    b.adjust(2)
    return b.as_markup()


def kb_next():
    b = InlineKeyboardBuilder()
    b.button(text="➡️ Дальше", callback_data="next")
    return b.as_markup()


class Reg(StatesGroup):
    name = State()
    group = State()
    course = State()
    stype = State()
    photos = State()


class Comment(StatesGroup):
    text = State()


router = Router()
locks: dict = defaultdict(asyncio.Lock)

WELCOME = (
    "Привет! Здесь можно создать анкету (имя, группа, курс, 3 фото), получить <b>AI-рейтинг лица 0-5</b> "
    "с характеристиками, оценивать других и участвовать в батлах 1 на 1.\n\n"
    "⚠️ Это <b>развлечение</b>: алгоритм смотрит на пропорции и симметрию лица и ничего не говорит "
    "о твоей ценности как человека."
)

CONSENT = (
    "Перед анкетой подтверди:\n"
    "• мне есть 18 лет;\n"
    "• я согласен(на), что мои фото и данные увидят другие участники бота;\n"
    "• анкету можно удалить в любой момент кнопкой «🗑 Удалить анкету»."
)


async def need_profile(m: Message) -> bool:
    await m.answer("Сначала заполни анкету 👇", reply_markup=main_kb())
    return False


# ---------------------------------------------------------------- меню
@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(WELCOME, reply_markup=main_kb())
    if not await db.get_user(m.from_user.id):
        await ask_consent(m)


async def ask_consent(m: Message):
    b = InlineKeyboardBuilder()
    b.button(text="✅ Мне 18+, согласен(на)", callback_data="consent")
    await m.answer(CONSENT, reply_markup=b.as_markup())


@router.message(F.text == BTN_REG)
@router.message(Command("reg"))
async def menu_reg(m: Message, state: FSMContext):
    await state.clear()
    await ask_consent(m)


@router.message(F.text == BTN_RATE)
@router.message(Command("rate"))
async def menu_rate(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    if not await db.get_user(m.from_user.id):
        return await need_profile(m)
    await show_next(m.chat.id, m.from_user.id, bot)


@router.message(F.text == BTN_BATTLE)
@router.message(Command("battle"))
async def menu_battle(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    if not await db.get_user(m.from_user.id):
        return await need_profile(m)
    await show_battle(m.chat.id, m.from_user.id, bot)


@router.message(F.text == BTN_ME)
@router.message(Command("me"))
async def menu_me(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    u = await db.get_user(m.from_user.id)
    if not u:
        return await need_profile(m)
    photos = json.loads(u["photos"])
    await bot.send_media_group(m.chat.id, [
        InputMediaPhoto(media=p, caption=card(u) if i == 0 else None) for i, p in enumerate(photos)])
    text = metrics_text(json.loads(u["metrics"]))
    cons = await db.consensus(u["user_id"])
    if cons["total"]:
        parts = ", ".join(f"{esc(t)}: {c}" for t, c in cons["types"])
        text += (f"\n\n<b>🗳 Мнения других ({cons['total']})</b>\n{parts}\n"
                 f"Согласились с твоей самооценкой: {cons['agree']}")
    else:
        text += "\n\n🗳 Пока никто не оценил твою анкету."
    coms = await db.comments(u["user_id"])
    if coms:
        text += "\n\n<b>💬 Комментарии</b>"
        for c in coms:
            mark = "✅" if c["agree"] else f"❌ ({esc(c['vote_type'])})"
            text += f"\n{mark} {esc(c['comment'])}"
    text += f"\n\n⚔️ Батлы: {u['wins']}/{u['battles']} побед · Elo {round(u['elo'])}"
    await m.answer(text)


@router.message(F.text == BTN_TOP)
@router.message(Command("top"))
async def menu_top(m: Message, state: FSMContext):
    await state.clear()
    ai = await db.top_ai(10)
    el = await db.top_elo(10)
    text = "<b>🏆 Топ по AI-рейтингу</b>\n"
    text += "\n".join(f"{i}. {esc(u['name'])} ({esc(u['grp'])}) — {u['ai_score']:.1f} · {esc(u['ai_type'])}"
                      for i, u in enumerate(ai, 1)) or "пока пусто"
    text += "\n\n<b>⚔️ Топ батлов (Elo)</b>\n"
    text += "\n".join(f"{i}. {esc(u['name'])} ({esc(u['grp'])}) — {round(u['elo'])} · {u['wins']}/{u['battles']}"
                      for i, u in enumerate(el, 1)) or "батлов ещё не было"
    await m.answer(text)


@router.message(F.text == BTN_DEL)
@router.message(Command("delete"))
async def menu_del(m: Message, state: FSMContext):
    await state.clear()
    if not await db.get_user(m.from_user.id):
        return await m.answer("У тебя нет анкеты.")
    b = InlineKeyboardBuilder()
    b.button(text="Да, удалить", callback_data="del:yes")
    b.button(text="Отмена", callback_data="del:no")
    await m.answer("Удалить анкету, фото и все оценки навсегда?", reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("del:"))
async def cb_del(c: CallbackQuery):
    if c.data == "del:yes":
        await db.delete_user(c.from_user.id)
        await c.message.edit_text("🗑 Анкета удалена.")
    else:
        await c.message.edit_text("Отменено.")
    await c.answer()


# ---------------------------------------------------------------- регистрация
@router.callback_query(F.data == "consent")
async def cb_consent(c: CallbackQuery, state: FSMContext):
    await state.set_state(Reg.name)
    await state.update_data(photos=[])
    await c.message.edit_text("Как тебя зовут? (до 40 символов)")
    await c.answer()


@router.message(Reg.name, F.text)
async def reg_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if not 1 <= len(name) <= 40:
        return await m.answer("Имя от 1 до 40 символов.")
    await state.update_data(name=name)
    await state.set_state(Reg.group)
    await m.answer("Твоя группа? (например, ИС-21)")


@router.message(Reg.group, F.text)
async def reg_group(m: Message, state: FSMContext):
    grp = m.text.strip()
    if not 1 <= len(grp) <= 20:
        return await m.answer("Группа до 20 символов.")
    await state.update_data(grp=grp)
    await state.set_state(Reg.course)
    b = InlineKeyboardBuilder()
    for i in range(1, 7):
        b.button(text=str(i), callback_data=f"course:{i}")
    b.adjust(6)
    await m.answer("Какой курс?", reply_markup=b.as_markup())


@router.callback_query(Reg.course, F.data.startswith("course:"))
async def reg_course(c: CallbackQuery, state: FSMContext):
    await state.update_data(course=int(c.data.split(":")[1]))
    await state.set_state(Reg.stype)
    b = InlineKeyboardBuilder()
    for i, t in enumerate(TYPES):
        b.button(text=t, callback_data=f"stype:{i}")
    b.adjust(2)
    await c.message.edit_text("Кем ты сам себя считаешь? Другие смогут согласиться или не согласиться.",
                              reply_markup=b.as_markup())
    await c.answer()


@router.callback_query(Reg.stype, F.data.startswith("stype:"))
async def reg_stype(c: CallbackQuery, state: FSMContext):
    t = TYPES[int(c.data.split(":")[1])]
    await state.update_data(self_type=t, photos=[])
    await state.set_state(Reg.photos)
    await c.message.edit_text(
        f"Ты считаешь, что ты: <b>{esc(t)}</b>.\n\n"
        "Теперь пришли <b>ровно 3 фото лица</b>: анфас, хороший свет, без очков, масок и фильтров. "
        "Можно по одному или альбомом. Отправляй именно как фото, не как файл.")
    await c.answer()


@router.message(Reg.photos, F.photo)
async def reg_photo(m: Message, state: FSMContext, bot: Bot):
    async with locks[m.from_user.id]:
        data = await state.get_data()
        photos = data.get("photos", [])
        if len(photos) >= 3:
            return
        ph = m.photo[-1]
        buf = io.BytesIO()
        await bot.download(ph, destination=buf)
        try:
            res = await asyncio.to_thread(face.analyze, buf.getvalue())
        except face.FaceError as e:
            return await m.answer(f"❌ {esc(e)}\nПришли другое фото.")
        except Exception:
            log.exception("analyze failed")
            return await m.answer("Не получилось проанализировать фото, попробуй другое.")
        photos.append({"id": ph.file_id, "score": res["score"], "metrics": res["metrics"]})
        await state.update_data(photos=photos)
        if len(photos) < 3:
            return await m.answer(f"✅ Фото {len(photos)}/3 принято. Осталось: {3 - len(photos)}.")

        score, metrics = aggregate(photos)
        typ = ai_type(score)
        await db.save_user(
            m.from_user.id, m.from_user.username, data["name"], data["grp"], data["course"],
            data["self_type"], score, typ, json.dumps(metrics),
            json.dumps([p["id"] for p in photos]))
        await state.clear()
        u = await db.get_user(m.from_user.id)
        await m.answer("🎉 Анкета готова!\n\n" + card(u) + "\n\n" + metrics_text(metrics),
                       reply_markup=main_kb())


@router.message(Reg.photos)
async def reg_photo_wrong(m: Message):
    await m.answer("Нужно именно фото (не файл и не текст). Пришли 3 фото лица.")


@router.message(F.photo)
async def stray_photo(m: Message):
    await m.answer("Фото принимаются только при заполнении анкеты: «📝 Заполнить анкету».")


# ---------------------------------------------------------------- оценивание
async def show_next(chat_id: int, viewer: int, bot: Bot):
    u = await db.next_profile(viewer)
    if not u:
        return await bot.send_message(
            chat_id, "Пока больше некого оценивать. Загляни позже или позови друзей 😉")
    photos = json.loads(u["photos"])
    await bot.send_media_group(chat_id, [
        InputMediaPhoto(media=p, caption=card(u) if i == 0 else None) for i, p in enumerate(photos)])
    await bot.send_message(
        chat_id,
        f"Пользователь думает, что он: <b>{esc(u['self_type'])}</b>\nТвоё мнение?",
        reply_markup=kb_vote(u["user_id"]))


async def after_vote(c: CallbackQuery, state: FSMContext, target: int, text: str):
    await state.set_state(Comment.text)
    await state.update_data(target=target)
    await c.message.edit_text(
        text + "\n\n💬 Напиши комментарий (до 300 символов) или жми «Дальше».",
        reply_markup=kb_next())


@router.callback_query(F.data.startswith("agree:"))
async def cb_agree(c: CallbackQuery, state: FSMContext, bot: Bot):
    uid = int(c.data.split(":")[1])
    u = await db.get_user(uid)
    await c.answer()
    if not u:
        return await show_next(c.message.chat.id, c.from_user.id, bot)
    await db.save_vote(c.from_user.id, uid, u["self_type"], 1)
    await after_vote(c, state, uid, f"✅ Ты согласен: <b>{esc(u['self_type'])}</b>")


@router.callback_query(F.data.startswith("dis:"))
async def cb_dis(c: CallbackQuery):
    uid = int(c.data.split(":")[1])
    await c.message.edit_text("Кто он по-твоему?", reply_markup=kb_types(uid))
    await c.answer()


@router.callback_query(F.data.startswith("pick:"))
async def cb_pick(c: CallbackQuery, state: FSMContext, bot: Bot):
    _, uid, idx = c.data.split(":")
    uid, t = int(uid), TYPES[int(idx)]
    await c.answer()
    if not await db.get_user(uid):
        return await show_next(c.message.chat.id, c.from_user.id, bot)
    await db.save_vote(c.from_user.id, uid, t, 0)
    await after_vote(c, state, uid, f"❌ Твоё мнение: <b>{esc(t)}</b>")


@router.callback_query(F.data.startswith("skip:"))
async def cb_skip(c: CallbackQuery, bot: Bot):
    uid = int(c.data.split(":")[1])
    await db.save_vote(c.from_user.id, uid, None, None)
    await c.message.edit_text("⏭ Пропущено.")
    await c.answer()
    await show_next(c.message.chat.id, c.from_user.id, bot)


@router.callback_query(F.data.startswith("met:"))
async def cb_met(c: CallbackQuery):
    u = await db.get_user(int(c.data.split(":")[1]))
    await c.answer()
    if u:
        await c.message.answer(metrics_text(json.loads(u["metrics"])))


@router.callback_query(F.data == "next")
async def cb_next(c: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    await c.message.edit_reply_markup(reply_markup=None)
    await c.answer()
    await show_next(c.message.chat.id, c.from_user.id, bot)


@router.message(Comment.text, F.text)
async def comment_text(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await db.set_comment(m.from_user.id, data["target"], m.text.strip()[:300])
    await state.clear()
    await m.answer("💬 Комментарий сохранён.")
    await show_next(m.chat.id, m.from_user.id, bot)


# ---------------------------------------------------------------- батлы
async def show_battle(chat_id: int, voter: int, bot: Bot):
    pair = await db.random_pair(voter)
    if len(pair) < 2:
        return await bot.send_message(chat_id, "Для батла нужно минимум 2 других участника. Загляни позже.")
    a, b = pair
    media = []
    for i, u in enumerate((a, b), 1):
        first = json.loads(u["photos"])[0]
        media.append(InputMediaPhoto(
            media=first, caption=f"{i}️⃣ <b>{esc(u['name'])}</b> · {esc(u['grp'])}"))
    await bot.send_media_group(chat_id, media)
    kb = InlineKeyboardBuilder()
    kb.button(text="1️⃣", callback_data=f"bt:{a['user_id']}:{b['user_id']}:1")
    kb.button(text="2️⃣", callback_data=f"bt:{a['user_id']}:{b['user_id']}:2")
    kb.button(text="⏭ Другая пара", callback_data=f"bt:{a['user_id']}:{b['user_id']}:0")
    kb.adjust(2, 1)
    await bot.send_message(chat_id, "⚔️ Кто лучше выглядит?", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("bt:"))
async def cb_battle(c: CallbackQuery, bot: Bot):
    _, a, b, w = c.data.split(":")
    a, b, w = int(a), int(b), int(w)
    await c.answer()
    if w == 0:
        await c.message.edit_text("⏭ Пропущено.")
    else:
        winner = a if w == 1 else b
        res = await db.record_battle(a, b, winner, c.from_user.id)
        wu = await db.get_user(winner)
        if res and wu:
            await c.message.edit_text(f"✅ Твой выбор: <b>{esc(wu['name'])}</b>")
        else:
            await c.message.edit_text("Один из участников уже удалил анкету.")
    await show_battle(c.message.chat.id, c.from_user.id, bot)


# ---------------------------------------------------------------- запуск
async def main():
    await db.init()
    bot = Bot(get_token(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.get_me()
    log.info("Бот запущен: @%s", me.username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
