import os
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.constants import ParseMode

# ============ Модели данных ============

class UserState(Enum):
    WAITING = "waiting"
    TAGGED = "tagged"
    RESPONDED = "responded"
    GIFT_RECEIVED = "gift_received"

@dataclass
class QueueUser:
    user_id: int
    username: str
    first_name: str
    state: UserState = UserState.WAITING
    times_tagged: int = 0
    tagged_at: Optional[datetime] = None
    responded_at: Optional[datetime] = None

@dataclass
class Queue:
    name: str
    chat_id: int
    special_user_id: int
    users: Dict[int, QueueUser] = field(default_factory=dict)
    user_order: List[int] = field(default_factory=list)
    current_index: int = 0
    is_searching: bool = False
    search_message_id: Optional[int] = None
    max_tags: int = 5
    response_timeout_minutes: int = 10

# ============ Хранилище ============

class Storage:
    def __init__(self):
        self.queues: Dict[str, Dict[int, Queue]] = {}
        self.admins: set = set()
        
    def get_queue(self, queue_name: str, chat_id: int) -> Optional[Queue]:
        if queue_name in self.queues and chat_id in self.queues[queue_name]:
            return self.queues[queue_name][chat_id]
        return None
    
    def create_queue(self, queue_name: str, chat_id: int, special_user_id: int) -> Queue:
        if queue_name not in self.queues:
            self.queues[queue_name] = {}
        queue = Queue(name=queue_name, chat_id=chat_id, special_user_id=special_user_id)
        self.queues[queue_name][chat_id] = queue
        return queue

storage = Storage()

# ============ Клавиатуры ============

def get_respond_keyboard(queue_name: str, user_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "✅ Я здесь! (откликнуться)", 
            callback_data=f"respond_{queue_name}_{user_id}"
        )]
    ])

def get_gift_received_keyboard(queue_name: str, user_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🎁 Подарок получен", 
            callback_data=f"gift_{queue_name}_{user_id}"
        )]
    ])

def get_join_queue_keyboard(queue_name: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "📝 Записаться в очередь", 
            callback_data=f"join_{queue_name}"
        )]
    ])

# ============ Основная логика ============

async def start_queue_search(queue: Queue, context: ContextTypes.DEFAULT_TYPE):
    if queue.is_searching:
        return
    
    if not queue.user_order:
        return
    
    queue.is_searching = True
    start_index = queue.current_index
    checked_count = 0
    
    while checked_count < len(queue.user_order):
        if not queue.user_order:
            queue.is_searching = False
            return
            
        user_id = queue.user_order[queue.current_index % len(queue.user_order)]
        user = queue.users.get(user_id)
        
        if user and user.state == UserState.WAITING:
            user.state = UserState.TAGGED
            user.times_tagged += 1
            user.tagged_at = datetime.now()
            
            if user.times_tagged >= queue.max_tags:
                await remove_user_from_queue(queue, user_id, context, reason="превышен лимит тегов")
                checked_count += 1
                continue
            
            try:
                message = await context.bot.send_message(
                    chat_id=queue.chat_id,
                    text=f"🔔 <a href='tg://user?id={user_id}'>{user.first_name}</a>, "
                         f"вы первый в очереди '{queue.name}'!\n"
                         f"У вас есть {queue.response_timeout_minutes} минут чтобы откликнуться.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=get_respond_keyboard(queue.name, user_id)
                )
                
                queue.search_message_id = message.message_id
                
                context.job_queue.run_once(
                    check_response_timeout,
                    timedelta(minutes=queue.response_timeout_minutes),
                    data={
                        'queue_name': queue.name,
                        'chat_id': queue.chat_id,
                        'user_id': user_id,
                        'message_id': message.message_id
                    },
                    name=f"timeout_{queue.name}_{queue.chat_id}_{user_id}"
                )
            except Exception as e:
                print(f"Error sending message: {e}")
                user.state = UserState.WAITING
                queue.is_searching = False
            return
        
        queue.current_index = (queue.current_index + 1) % len(queue.user_order)
        checked_count += 1
    
    queue.is_searching = False

async def check_response_timeout(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    queue = storage.get_queue(job_data['queue_name'], job_data['chat_id'])
    
    if not queue:
        return
    
    user = queue.users.get(job_data['user_id'])
    
    if user and user.state == UserState.TAGGED:
        user.state = UserState.WAITING
        queue.is_searching = False
        
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=queue.chat_id,
                message_id=job_data['message_id'],
                reply_markup=None
            )
        except:
            pass
        
        await start_queue_search(queue, context)

async def remove_user_from_queue(queue: Queue, user_id: int, context: ContextTypes.DEFAULT_TYPE, reason: str = ""):
    if user_id in queue.users:
        del queue.users[user_id]
    if user_id in queue.user_order:
        queue.user_order.remove(user_id)
    
    reason_text = f" ({reason})" if reason else ""
    try:
        await context.bot.send_message(
            chat_id=queue.chat_id,
            text=f"👤 Пользователь удален из очереди '{queue.name}'{reason_text}"
        )
    except Exception as e:
        print(f"Error sending removal message: {e}")

# ============ Обработчики команд ============

async def process_join(update: Update, context: ContextTypes.DEFAULT_TYPE, queue_name: str):
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    queue = storage.get_queue(queue_name, chat_id)
    if not queue:
        message = update.effective_message
        if message:
            await message.reply_text(
                f"❌ Очередь '{queue_name}' не существует."
            )
        return
    
    if user.id in queue.users and queue.users[user.id].state != UserState.GIFT_RECEIVED:
        message = update.effective_message
        if message:
            await message.reply_text(f"❌ Вы уже в очереди '{queue_name}'")
        return
    
    queue_user = QueueUser(
        user_id=user.id,
        username=user.username or "",
        first_name=user.first_name
    )
    queue.users[user.id] = queue_user
    queue.user_order.append(user.id)
    
    message = update.effective_message
    if message:
        await message.reply_text(
            f"✅ {user.first_name}, вы записаны в очередь '{queue_name}'\n"
            f"Позиция: {len(queue.user_order)}"
        )
    
    if not queue.is_searching:
        await start_queue_search(queue, context)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    if data.startswith("join_"):
        queue_name = data[5:]
        await process_join(update, context, queue_name)
        
    elif data.startswith("respond_"):
        parts = data.split("_")
        queue_name = parts[1]
        target_user_id = int(parts[2])
        
        queue = storage.get_queue(queue_name, chat_id)
        if not queue:
            await query.answer("❌ Очередь не найдена")
            return
        
        if user.id != target_user_id:
            await query.answer("❌ Эта кнопка не для вас!", show_alert=True)
            return
        
        queue_user = queue.users.get(user.id)
        if not queue_user or queue_user.state != UserState.TAGGED:
            await query.answer("❌ Вы не были отмечены в очереди", show_alert=True)
            return
        
        queue_user.state = UserState.RESPONDED
        queue_user.responded_at = datetime.now()
        queue.is_searching = False
        
        current_jobs = context.job_queue.get_jobs_by_name(
            f"timeout_{queue_name}_{chat_id}_{user.id}"
        )
        for job in current_jobs:
            job.schedule_removal()
        
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🎉 <a href='tg://user?id={queue.special_user_id}'>Специальный пользователь</a>, "
                     f"пользователь <a href='tg://user?id={user.id}'>{user.first_name}</a> "
                     f"откликнулся из очереди '{queue_name}'!",
                parse_mode=ParseMode.HTML,
                reply_markup=get_gift_received_keyboard(queue.name, user.id)
            )
        except Exception as e:
            print(f"Error notifying special user: {e}")
        
        try:
            await query.edit_message_text(
                f"✅ {user.first_name} откликнулся!",
                reply_markup=None
            )
        except:
            pass
        
    elif data.startswith("gift_"):
        parts = data.split("_")
        queue_name = parts[1]
        target_user_id = int(parts[2])
        
        queue = storage.get_queue(queue_name, chat_id)
        if not queue:
            await query.answer("❌ Очередь не найдена")
            return
        
        if user.id != target_user_id:
            await query.answer("❌ Эта кнопка не для вас!", show_alert=True)
            return
        
        queue_user = queue.users.get(user.id)
        if not queue_user or queue_user.state != UserState.RESPONDED:
            await query.answer("❌ Вы не откликались в очереди", show_alert=True)
            return
        
        await remove_user_from_queue(queue, user.id, context, reason="подарок получен")
        
        try:
            await query.edit_message_text(
                f"🎁 {user.first_name} получил подарок и покинул очередь '{queue_name}'!",
                reply_markup=None
            )
        except:
            pass
        
        await start_queue_search(queue, context)

async def join_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Используйте: /join <название_очереди>",
            reply_markup=get_join_queue_keyboard("default")
        )
        return
    
    queue_name = context.args[0]
    await process_join(update, context, queue_name)

async def create_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in storage.admins:
        await update.message.reply_text("❌ Только администраторы могут создавать очереди")
        return
    
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Используйте: /create_queue <название> @username"
        )
        return
    
    queue_name = context.args[0]
    special_user_mention = context.args[1]
    
    if not special_user_mention.startswith('@'):
        await update.message.reply_text("Укажите пользователя через @username")
        return
    
    chat_id = update.effective_chat.id
    
    try:
        special_user = await context.bot.get_chat(special_user_mention)
        special_user_id = special_user.id
        
        storage.create_queue(queue_name, chat_id, special_user_id)
        
        await update.message.reply_text(
            f"✅ Очередь '{queue_name}' создана!\n"
            f"Пользователи могут записаться: /join {queue_name}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: пользователь не найден или бот не может с ним взаимодействовать")

async def start_search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Используйте: /start_search <название_очереди>")
        return
    
    queue_name = context.args[0]
    chat_id = update.effective_chat.id
    
    queue = storage.get_queue(queue_name, chat_id)
    if not queue:
        await update.message.reply_text(f"❌ Очередь '{queue_name}' не существует")
        return
    
    if queue.is_searching:
        await update.message.reply_text("🔍 Поиск уже запущен")
        return
    
    queue.current_index = 0
    await start_queue_search(queue, context)

async def queue_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    if not context.args:
        queues_in_chat = []
        for queue_name, chat_queues in storage.queues.items():
            if chat_id in chat_queues:
                queue = chat_queues[chat_id]
                queues_in_chat.append(f"• {queue_name}: {len(queue.users)} участников")
        
        if queues_in_chat:
            await update.message.reply_text(
                "📋 Очереди в этом чате:\n" + "\n".join(queues_in_chat)
            )
        else:
            await update.message.reply_text("В этом чате нет очередей")
        return
    
    queue_name = context.args[0]
    queue = storage.get_queue(queue_name, chat_id)
    
    if not queue:
        await update.message.reply_text(f"❌ Очередь '{queue_name}' не найдена")
        return
    
    users_list = []
    for i, user_id in enumerate(queue.user_order, 1):
        user = queue.users[user_id]
        state_emoji = {
            UserState.WAITING: "⏳",
            UserState.TAGGED: "🔔",
            UserState.RESPONDED: "✅",
            UserState.GIFT_RECEIVED: "🎁"
        }.get(user.state, "❓")
        
        users_list.append(f"{i}. {state_emoji} {user.first_name} (тегов: {user.times_tagged})")
    
    status = "🔍 Идет поиск" if queue.is_searching else "⏸️ Ожидание"
    await update.message.reply_text(
        f"📋 Очередь '{queue_name}'\n"
        f"Статус: {status}\n"
        f"Участники:\n" + ("\n".join(users_list) if users_list else "Пусто")
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
🤖 <b>Бот управления очередями</b>

<b>Основные команды:</b>
/join <очередь> - записаться в очередь
/start_search <очередь> - запустить поиск вручную
/queue_info [очередь] - информация об очереди

<b>Админ:</b>
/create_queue <имя> @user - создать очередь
"""
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

# ============ Запуск ============

def main():
    # ID администраторов (получите у @getmyid_bot)
    storage.admins = {123456789}  # ЗАМЕНИТЕ НА СВОЙ ID
    
    application = Application.builder().token(os.getenv("BOT_TOKEN")).build()
    
    application.add_handler(CommandHandler("start", help_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("join", join_command))
    application.add_handler(CommandHandler("start_search", start_search_command))
    application.add_handler(CommandHandler("create_queue", create_queue))
    application.add_handler(CommandHandler("queue_info", queue_info))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    print("Бот запущен...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
