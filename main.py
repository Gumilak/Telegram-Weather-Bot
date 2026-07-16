import telebot
import requests
import json
import os
import time
import logging
import sqlite3
from datetime import datetime
from dotenv import load_dotenv
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineQueryResultArticle, InputTextMessageContent
)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# --------------------------------------------
# 1. НАСТРОЙКА
# --------------------------------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
API_KEY = os.getenv('WEATHER_API_KEY')

if not BOT_TOKEN or not API_KEY:
    logging.error("Не найдены BOT_TOKEN или WEATHER_API_KEY в .env файле!")
    exit(1)

bot = telebot.TeleBot(BOT_TOKEN)

# Меню команд
bot.set_my_commands([
    telebot.types.BotCommand("start", "Запустить бота"),
    telebot.types.BotCommand("help", "Помощь"),
    telebot.types.BotCommand("settings", "Настройки единиц измерения"),
    telebot.types.BotCommand("location", "Погода по геолокации"),
    telebot.types.BotCommand("favorites", "Мои избранные города"),
    telebot.types.BotCommand("daily", "Почасовой прогноз на сегодня"),
    telebot.types.BotCommand("subscribe", "Подписаться на ежедневную рассылку"),
    telebot.types.BotCommand("unsubscribe", "Отписаться от рассылки")
])

# --------------------------------------------
# 2. БАЗА ДАННЫХ (С ПРОВЕРКОЙ СТРУКТУРЫ)
# --------------------------------------------
DB_NAME = "favorites.db"
last_city = {}

def init_db():
    conn = sqlite3.connect(DB_NAME, timeout=10)
    c = conn.cursor()
    # Таблица избранного
    c.execute('''
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            city_name TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            lang TEXT NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Таблица подписок (создаём, если не существует)
    c.execute('''
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            city_name TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            lang TEXT NOT NULL,
            hour INTEGER,
            minute INTEGER,
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Проверяем и добавляем недостающие колонки
    c.execute("PRAGMA table_info(subscriptions)")
    columns = [col[1] for col in c.fetchall()]
    if 'hour' not in columns:
        c.execute("ALTER TABLE subscriptions ADD COLUMN hour INTEGER")
    if 'minute' not in columns:
        c.execute("ALTER TABLE subscriptions ADD COLUMN minute INTEGER")
    if 'active' not in columns:
        c.execute("ALTER TABLE subscriptions ADD COLUMN active INTEGER DEFAULT 1")
    conn.commit()
    conn.close()

def add_favorite(user_id, city_name, lat, lon, lang):
    conn = sqlite3.connect(DB_NAME, timeout=10)
    c = conn.cursor()
    c.execute('SELECT id FROM favorites WHERE user_id=? AND lat=? AND lon=?', (user_id, lat, lon))
    if c.fetchone():
        conn.close()
        return False
    c.execute('INSERT INTO favorites (user_id, city_name, lat, lon, lang) VALUES (?,?,?,?,?)',
              (user_id, city_name, lat, lon, lang))
    conn.commit()
    conn.close()
    return True

def remove_favorite(user_id, lat, lon):
    conn = sqlite3.connect(DB_NAME, timeout=10)
    c = conn.cursor()
    c.execute('DELETE FROM favorites WHERE user_id=? AND lat=? AND lon=?', (user_id, lat, lon))
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    return deleted

def get_favorites(user_id):
    conn = sqlite3.connect(DB_NAME, timeout=10)
    c = conn.cursor()
    c.execute('SELECT city_name, lat, lon, lang FROM favorites WHERE user_id=? ORDER BY added_at', (user_id,))
    rows = c.fetchall()
    conn.close()
    return [{'city_name': row[0], 'lat': row[1], 'lon': row[2], 'lang': row[3]} for row in rows]

def is_favorite(user_id, lat, lon):
    conn = sqlite3.connect(DB_NAME, timeout=10)
    c = conn.cursor()
    c.execute('SELECT id FROM favorites WHERE user_id=? AND lat=? AND lon=?', (user_id, lat, lon))
    exists = c.fetchone() is not None
    conn.close()
    return exists

def add_subscription(user_id, city_name, lat, lon, lang, hour, minute):
    conn = sqlite3.connect(DB_NAME, timeout=10)
    c = conn.cursor()
    # Удаляем старую подписку
    c.execute('DELETE FROM subscriptions WHERE user_id=?', (user_id,))
    c.execute('''
        INSERT INTO subscriptions (user_id, city_name, lat, lon, lang, hour, minute, active)
        VALUES (?,?,?,?,?,?,?,1)
    ''', (user_id, city_name, lat, lon, lang, hour, minute))
    conn.commit()
    conn.close()
    return True

def remove_subscription(user_id):
    conn = sqlite3.connect(DB_NAME, timeout=10)
    c = conn.cursor()
    c.execute('DELETE FROM subscriptions WHERE user_id=?', (user_id,))
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    return deleted

def get_subscription(user_id):
    conn = sqlite3.connect(DB_NAME, timeout=10)
    c = conn.cursor()
    c.execute('SELECT city_name, lat, lon, lang, hour, minute FROM subscriptions WHERE user_id=? AND active=1', (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {'city_name': row[0], 'lat': row[1], 'lon': row[2], 'lang': row[3], 'hour': row[4], 'minute': row[5]}
    return None

def get_all_active_subscriptions():
    conn = sqlite3.connect(DB_NAME, timeout=10)
    c = conn.cursor()
    c.execute('SELECT user_id, city_name, lat, lon, lang, hour, minute FROM subscriptions WHERE active=1')
    rows = c.fetchall()
    conn.close()
    return [{'user_id': row[0], 'city_name': row[1], 'lat': row[2], 'lon': row[3], 'lang': row[4], 'hour': row[5], 'minute': row[6]} for row in rows]

init_db()

# --------------------------------------------
# 3. КЭШИ И НАСТРОЙКИ
# --------------------------------------------
cache_current = {}
cache_forecast = {}
cache_hourly = {}
CACHE_TTL = 600
user_settings = {}

WEEKDAYS_RU = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}
WEEKDAYS_EN = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}

WEATHER_EMOJI = {
    "clear sky": "☀️",
    "few clouds": "🌤",
    "scattered clouds": "⛅",
    "broken clouds": "☁️",
    "overcast clouds": "☁️",
    "light rain": "🌦",
    "moderate rain": "🌧",
    "heavy intensity rain": "🌧",
    "very heavy rain": "🌧",
    "rain": "🌧",
    "thunderstorm": "⛈",
    "snow": "❄️",
    "mist": "🌫",
    "fog": "🌫",
}

def detect_lang(text):
    return 'ru' if any('а' <= ch <= 'я' or 'А' <= ch <= 'Я' for ch in text) else 'en'

def get_weather_emoji(desc):
    desc_lower = desc.lower()
    for key, emoji in WEATHER_EMOJI.items():
        if key in desc_lower:
            return emoji
    return "🌡"

def get_user_settings(chat_id):
    if chat_id not in user_settings:
        user_settings[chat_id] = {'units': 'metric'}
    return user_settings[chat_id]

def convert_temperature(temp_c, units):
    if units == 'imperial':
        return round(temp_c * 9/5 + 32, 1)
    return round(temp_c, 1)

def convert_speed(speed_ms, units):
    if units == 'imperial':
        return round(speed_ms * 2.23694, 2)
    return round(speed_ms, 2)

def format_temp(temp_c, units):
    val = convert_temperature(temp_c, units)
    return f"{val}°{'F' if units == 'imperial' else 'C'}"

def format_speed(speed_ms, units):
    val = convert_speed(speed_ms, units)
    unit = "mph" if units == 'imperial' else "м/с"
    return f"{val} {unit}"

# --------------------------------------------
# 4. ЗАПРОСЫ К API
# --------------------------------------------
def fetch_current_weather(lat, lon, lang):
    url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={API_KEY}&units=metric&lang={lang}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    temp = data['main']['temp']
    feels = data['main']['feels_like']
    desc = data['weather'][0]['description'].capitalize()
    hum = data['main']['humidity']
    wind = data['wind']['speed']
    return temp, feels, desc, hum, wind

def fetch_forecast_data(lat, lon, lang):
    url = f"https://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}&appid={API_KEY}&units=metric&lang={lang}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return data['list']

def get_forecast_by_day(lat, lon, lang):
    data_list = fetch_forecast_data(lat, lon, lang)
    forecast_by_day = {}
    for item in data_list:
        dt = datetime.fromtimestamp(item['dt'])
        day_key = dt.date()
        if day_key not in forecast_by_day:
            forecast_by_day[day_key] = {'temps': [], 'descs': []}
        forecast_by_day[day_key]['temps'].append(item['main']['temp'])
        forecast_by_day[day_key]['descs'].append(item['weather'][0]['description'])
    result = []
    sorted_days = sorted(forecast_by_day.keys())
    today = datetime.now().date()
    future_days = [d for d in sorted_days if d > today][:5]
    if len(future_days) < 5:
        future_days = sorted_days[:5]
    for day in future_days:
        temps = forecast_by_day[day]['temps']
        descs = forecast_by_day[day]['descs']
        avg_temp = sum(temps) / len(temps)
        desc = max(set(descs), key=descs.count).capitalize()
        emoji = get_weather_emoji(desc)
        result.append({'date': day, 'temp': avg_temp, 'desc': desc, 'emoji': emoji})
    return result

def get_hourly_forecast_today(lat, lon, lang):
    data_list = fetch_forecast_data(lat, lon, lang)
    today = datetime.now().date()
    hourly = []
    for item in data_list:
        dt = datetime.fromtimestamp(item['dt'])
        if dt.date() == today:
            time_str = dt.strftime("%H:%M")
            temp = item['main']['temp']
            desc = item['weather'][0]['description'].capitalize()
            emoji = get_weather_emoji(desc)
            hourly.append({'time': time_str, 'temp': temp, 'desc': desc, 'emoji': emoji})
    hourly.sort(key=lambda x: x['time'])
    return hourly

def save_to_cache(cache_dict, key, data):
    cache_dict[key] = {'data': data, 'timestamp': time.time()}

# --------------------------------------------
# 5. РАССЫЛКА И ПЛАНИРОВЩИК
# --------------------------------------------
def send_daily_weather(user_id, city_name, lat, lon, lang):
    try:
        settings = get_user_settings(user_id)
        units = settings['units']
        temp, feels, desc, hum, wind = fetch_current_weather(lat, lon, lang)
        temp_str = format_temp(temp, units)
        feels_str = format_temp(feels, units)
        wind_str = format_speed(wind, units)
        emoji = get_weather_emoji(desc)
        if lang == 'ru':
            message = (
                f"🌅 *Доброе утро!* ☕️\n"
                f"Погода в *{city_name}* сегодня:\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"🌡 {temp_str} (ощущается {feels_str})\n"
                f"{emoji} {desc}\n"
                f"💧 Влажность: {hum}%\n"
                f"💨 Ветер: {wind_str}\n\n"
                f"Хорошего дня! ☀️"
            )
        else:
            message = (
                f"🌅 *Good morning!* ☕️\n"
                f"Weather in *{city_name}* today:\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"🌡 {temp_str} (feels like {feels_str})\n"
                f"{emoji} {desc}\n"
                f"💧 Humidity: {hum}%\n"
                f"💨 Wind: {wind_str}\n\n"
                f"Have a great day! ☀️"
            )
        bot.send_message(user_id, message, parse_mode='Markdown')
        logging.info(f"Daily weather sent to user {user_id}")
    except Exception as e:
        logging.error(f"Error sending daily weather: {e}")

scheduler = BackgroundScheduler()

def schedule_subscription(sub):
    trigger = CronTrigger(hour=sub['hour'], minute=sub['minute'])
    scheduler.add_job(
        send_daily_weather,
        trigger,
        args=[sub['user_id'], sub['city_name'], sub['lat'], sub['lon'], sub['lang']],
        id=f"daily_{sub['user_id']}",
        replace_existing=True
    )

def restore_subscriptions():
    subs = get_all_active_subscriptions()
    for sub in subs:
        schedule_subscription(sub)
    logging.info(f"Restored {len(subs)} subscriptions")

scheduler.start()
restore_subscriptions()

# --------------------------------------------
# 6. ОТПРАВКА ПОГОДЫ
# --------------------------------------------
def send_current_weather(message, location_data, lang, from_inline=False):
    try:
        chat_id = message.chat.id if hasattr(message, 'chat') else None
        units = 'metric'
        if chat_id:
            settings = get_user_settings(chat_id)
            units = settings['units']
            last_city[chat_id] = {
                'lat': location_data['lat'],
                'lon': location_data['lon'],
                'lang': lang,
                'city_name': location_data.get('local_names', {}).get(lang, location_data['name'])
            }
        lat = location_data['lat']
        lon = location_data['lon']
        city_name = location_data.get('local_names', {}).get(lang, location_data['name'])
        cache_key = f"current_{lat},{lon},{lang}"
        now = time.time()
        if cache_key in cache_current:
            cached = cache_current[cache_key]
            if now - cached['timestamp'] < CACHE_TTL:
                temp = cached['data']['temp']
                feels = cached['data']['feels']
                desc = cached['data']['desc']
                hum = cached['data']['hum']
                wind = cached['data']['wind']
            else:
                del cache_current[cache_key]
                temp, feels, desc, hum, wind = fetch_current_weather(lat, lon, lang)
                save_to_cache(cache_current, cache_key, {'temp': temp, 'feels': feels, 'desc': desc, 'hum': hum, 'wind': wind})
        else:
            temp, feels, desc, hum, wind = fetch_current_weather(lat, lon, lang)
            save_to_cache(cache_current, cache_key, {'temp': temp, 'feels': feels, 'desc': desc, 'hum': hum, 'wind': wind})
        temp_str = format_temp(temp, units)
        feels_str = format_temp(feels, units)
        wind_str = format_speed(wind, units)
        emoji = get_weather_emoji(desc)
        if lang == 'ru':
            answer = (
                f"📍 *{city_name}*\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"🌡 *Температура:* {temp_str} (ощущается {feels_str})\n"
                f"{emoji} *{desc}*\n"
                f"💧 *Влажность:* {hum}%\n"
                f"💨 *Ветер:* {wind_str}"
            )
        else:
            answer = (
                f"📍 *{city_name}*\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"🌡 *Temperature:* {temp_str} (feels like {feels_str})\n"
                f"{emoji} *{desc}*\n"
                f"💧 *Humidity:* {hum}%\n"
                f"💨 *Wind:* {wind_str}"
            )
        if from_inline:
            return answer
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton(
                "📅 Прогноз на 5 дней" if lang == 'ru' else "📅 5-day forecast",
                callback_data=f"forecast|{lat}|{lon}|{lang}|{city_name}"
            ),
            InlineKeyboardButton(
                "⏰ Сегодня по часам" if lang == 'ru' else "⏰ Hourly today",
                callback_data=f"hourly|{lat}|{lon}|{lang}|{city_name}"
            )
        )
        if chat_id:
            if is_favorite(chat_id, lat, lon):
                markup.add(InlineKeyboardButton(
                    "🗑 Удалить из избранного" if lang == 'ru' else "🗑 Remove from favorites",
                    callback_data=f"remove_fav|{lat}|{lon}|{lang}"
                ))
            else:
                markup.add(InlineKeyboardButton(
                    "⭐ Добавить в избранное" if lang == 'ru' else "⭐ Add to favorites",
                    callback_data=f"add_fav|{lat}|{lon}|{lang}|{city_name}"
                ))
        bot.reply_to(message, answer, parse_mode='Markdown', reply_markup=markup)
    except Exception as e:
        logging.error(f"send_current_weather error: {e}")
        if not from_inline:
            bot.reply_to(message, "⚠️ Не удалось получить погоду." if lang == 'ru' else "⚠️ Could not get weather.")

def send_forecast(message, lat, lon, lang, city_name):
    try:
        chat_id = message.chat.id
        settings = get_user_settings(chat_id)
        units = settings['units']
        cache_key = f"forecast_{lat},{lon},{lang}"
        now = time.time()
        if cache_key in cache_forecast:
            cached = cache_forecast[cache_key]
            if now - cached['timestamp'] < CACHE_TTL:
                forecast_data = cached['data']
            else:
                del cache_forecast[cache_key]
                forecast_data = get_forecast_by_day(lat, lon, lang)
                save_to_cache(cache_forecast, cache_key, forecast_data)
        else:
            forecast_data = get_forecast_by_day(lat, lon, lang)
            save_to_cache(cache_forecast, cache_key, forecast_data)
        if lang == 'ru':
            lines = [f"📅 *Прогноз на 5 дней для {city_name}:*"]
        else:
            lines = [f"📅 *5-day forecast for {city_name}:*"]
        lines.append("━━━━━━━━━━━━━━━━━━━━━")
        for day in forecast_data:
            if lang == 'ru':
                weekday = WEEKDAYS_RU[day['date'].weekday()]
                date_str = f"{weekday} {day['date'].strftime('%d.%m')}"
            else:
                weekday = WEEKDAYS_EN[day['date'].weekday()]
                date_str = f"{weekday} {day['date'].strftime('%m/%d')}"
            temp_str = format_temp(day['temp'], units)
            emoji = day['emoji']
            lines.append(f"• *{date_str}*: {temp_str} {emoji} {day['desc']}")
        answer = "\n".join(lines)
        bot.send_message(message.chat.id, answer, parse_mode='Markdown')
    except Exception as e:
        logging.error(f"send_forecast error: {e}")
        bot.send_message(message.chat.id, "⚠️ Не удалось получить прогноз." if lang == 'ru' else "⚠️ Could not get forecast.")

def send_hourly_forecast(message, lat, lon, lang, city_name):
    try:
        chat_id = message.chat.id
        settings = get_user_settings(chat_id)
        units = settings['units']
        cache_key = f"hourly_{lat},{lon},{lang}"
        now = time.time()
        if cache_key in cache_hourly:
            cached = cache_hourly[cache_key]
            if now - cached['timestamp'] < CACHE_TTL:
                hourly_data = cached['data']
            else:
                del cache_hourly[cache_key]
                hourly_data = get_hourly_forecast_today(lat, lon, lang)
                save_to_cache(cache_hourly, cache_key, hourly_data)
        else:
            hourly_data = get_hourly_forecast_today(lat, lon, lang)
            save_to_cache(cache_hourly, cache_key, hourly_data)
        if not hourly_data:
            bot.send_message(chat_id, "Нет данных на сегодня." if lang == 'ru' else "No data for today.")
            return
        if lang == 'ru':
            lines = [f"⏰ *Почасовой прогноз на сегодня для {city_name}:*"]
        else:
            lines = [f"⏰ *Hourly forecast for today for {city_name}:*"]
        lines.append("━━━━━━━━━━━━━━━━━━━━━")
        for item in hourly_data:
            temp_str = format_temp(item['temp'], units)
            lines.append(f"• *{item['time']}*: {temp_str} {item['emoji']} {item['desc']}")
        answer = "\n".join(lines)
        bot.send_message(chat_id, answer, parse_mode='Markdown')
    except Exception as e:
        logging.error(f"send_hourly_forecast error: {e}")
        bot.send_message(message.chat.id, "⚠️ Не удалось получить почасовой прогноз." if lang == 'ru' else "⚠️ Could not get hourly forecast.")

# --------------------------------------------
# 7. ОБРАБОТЧИКИ КОМАНД
# --------------------------------------------
@bot.message_handler(commands=['start'])
def start(message):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    markup.add(KeyboardButton("📍 Отправить местоположение", request_location=True))
    markup.add(KeyboardButton("⭐ Избранное"))
    markup.add(KeyboardButton("🔔 Подписка"))
    bot.send_message(message.chat.id, "Приветствую! 🌍 Введи город, отправь геопозицию или выбери избранное.", reply_markup=markup)

@bot.message_handler(commands=['location'])
def location_command(message):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(KeyboardButton("📍 Отправить местоположение", request_location=True))
    bot.send_message(message.chat.id, "Нажми кнопку, чтобы отправить свою геопозицию:", reply_markup=markup)

@bot.message_handler(commands=['help'])
def help_command(message):
    help_text = (
        "🌍 *Доступные команды:*\n\n"
        "🔹 /start – запустить бота\n"
        "🔹 /help – показать эту справку\n"
        "🔹 /settings – настройка единиц измерения (°C/м/с или °F/mph)\n"
        "🔹 /location – погода по геолокации\n"
        "🔹 /favorites – мои избранные города\n"
        "🔹 /daily [город] – почасовой прогноз на сегодня\n"
        "🔹 /subscribe – подписаться на ежедневную рассылку\n"
        "🔹 /unsubscribe – отписаться от рассылки\n\n"
        "📌 *Как использовать:*\n"
        "• Введите название города – бот покажет текущую погоду.\n"
        "• Под погодой есть кнопки: прогноз на 5 дней и почасовой на сегодня.\n"
        "• Добавляйте города в избранное через кнопку под погодой.\n"
        "• В настройках можно переключить единицы измерения.\n"
        "• Нажмите *🔔 Подписка* в главном меню, чтобы настроить ежедневную рассылку.\n\n"
        "💡 *Инлайн-режим:* В любом чате напишите `@имя_бота город` – получите краткую сводку."
    )
    bot.send_message(message.chat.id, help_text, parse_mode='Markdown')

@bot.message_handler(commands=['settings'])
def settings_command(message):
    chat_id = message.chat.id
    settings = get_user_settings(chat_id)
    current_units = settings['units']
    markup = InlineKeyboardMarkup(row_width=2)
    metric_btn = InlineKeyboardButton(
        "🌡 Цельсий, м/с" + (" ✅" if current_units == 'metric' else ""),
        callback_data="set_metric"
    )
    imperial_btn = InlineKeyboardButton(
        "🌡 Фаренгейт, mph" + (" ✅" if current_units == 'imperial' else ""),
        callback_data="set_imperial"
    )
    markup.add(metric_btn, imperial_btn)
    markup.add(InlineKeyboardButton("❌ Закрыть", callback_data="close_settings"))
    bot.send_message(chat_id, "Выберите систему единиц:", reply_markup=markup)

@bot.message_handler(commands=['favorites'])
def favorites_command(message):
    show_favorites(message)

@bot.message_handler(commands=['daily'])
def daily_command(message):
    chat_id = message.chat.id
    text = message.text.strip()
    parts = text.split(maxsplit=1)
    if len(parts) > 1:
        city = parts[1]
        try:
            geo_url = f"http://api.openweathermap.org/geo/1.0/direct?q={city}&appid={API_KEY}&limit=1"
            resp = requests.get(geo_url, timeout=10)
            resp.raise_for_status()
            geo_data = resp.json()
            if not geo_data:
                bot.reply_to(message, "Город не найден.")
                return
            location = geo_data[0]
            lang = detect_lang(city)
            send_hourly_forecast(message, location['lat'], location['lon'], lang,
                                 location.get('local_names', {}).get(lang, location['name']))
        except Exception as e:
            logging.error(f"Daily command error: {e}")
            bot.reply_to(message, "⚠️ Ошибка при поиске города.")
        return
    else:
        if chat_id in last_city:
            city_info = last_city[chat_id]
            send_hourly_forecast(message, city_info['lat'], city_info['lon'],
                                 city_info['lang'], city_info['city_name'])
        else:
            bot.reply_to(message, "Вы ещё не запрашивали погоду. Укажите город, например: `/daily Москва`", parse_mode='Markdown')

# --------------------------------------------
# 8. ПОДПИСКА
# --------------------------------------------
@bot.message_handler(commands=['subscribe'])
def subscribe_command(message):
    chat_id = message.chat.id
    sub = get_subscription(chat_id)
    if sub:
        lang = sub['lang']
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("🕒 Изменить время" if lang == 'ru' else "🕒 Change time", callback_data="sub_change_time"),
            InlineKeyboardButton("❌ Отписаться" if lang == 'ru' else "❌ Unsubscribe", callback_data="sub_unsubscribe")
        )
        bot.send_message(chat_id,
                         f"Вы уже подписаны на рассылку для {sub['city_name']} в {sub['hour']:02d}:{sub['minute']:02d}." if lang == 'ru' else f"You are subscribed to {sub['city_name']} at {sub['hour']:02d}:{sub['minute']:02d}.",
                         reply_markup=markup)
        return
    favorites = get_favorites(chat_id)
    if favorites:
        markup = InlineKeyboardMarkup(row_width=2)
        for fav in favorites:
            markup.add(InlineKeyboardButton(fav['city_name'], callback_data=f"sub_city|{fav['lat']}|{fav['lon']}|{fav['lang']}|{fav['city_name']}"))
        markup.add(InlineKeyboardButton("❌ Отмена", callback_data="cancel_sub"))
        bot.send_message(chat_id, "Выберите город для рассылки (из избранного):", reply_markup=markup)
    elif chat_id in last_city:
        city_info = last_city[chat_id]
        ask_time_selection(message, city_info['lat'], city_info['lon'], city_info['lang'], city_info['city_name'])
    else:
        bot.send_message(chat_id, "У вас нет избранных городов и вы ещё не запрашивали погоду. Сначала запросите погоду для города или добавьте его в избранное.")

def ask_time_selection(message, lat, lon, lang, city_name):
    chat_id = message.chat.id
    markup = InlineKeyboardMarkup(row_width=4)
    for hour in range(6, 13):
        markup.add(InlineKeyboardButton(f"{hour:02d}:00", callback_data=f"sub_time|{lat}|{lon}|{lang}|{city_name}|{hour}|0"))
        markup.add(InlineKeyboardButton(f"{hour:02d}:30", callback_data=f"sub_time|{lat}|{lon}|{lang}|{city_name}|{hour}|30"))
    markup.add(InlineKeyboardButton("❌ Отмена", callback_data="cancel_sub"))
    bot.send_message(chat_id, "Выберите время для ежедневной рассылки:", reply_markup=markup)

@bot.message_handler(commands=['unsubscribe'])
def unsubscribe_command(message):
    chat_id = message.chat.id
    if remove_subscription(chat_id):
        job_id = f"daily_{chat_id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        bot.send_message(chat_id, "✅ Вы отписаны от ежедневной рассылки.")
    else:
        bot.send_message(chat_id, "Вы не были подписаны.")

# --------------------------------------------
# 9. ИЗБРАННОЕ
# --------------------------------------------
def show_favorites(message):
    chat_id = message.chat.id
    favorites = get_favorites(chat_id)
    if not favorites:
        bot.send_message(chat_id, "У вас пока нет избранных городов. Добавьте их через кнопку под погодой.")
        return
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    for fav in favorites:
        markup.add(KeyboardButton(fav['city_name']))
    markup.add(KeyboardButton("🔙 Назад"))
    bot.send_message(chat_id, "Выберите город из избранного:", reply_markup=markup)

@bot.message_handler(func=lambda message: message.text == "⭐ Избранное")
def favorites_button(message):
    show_favorites(message)

@bot.message_handler(func=lambda message: message.text == "🔔 Подписка")
def subscription_button(message):
    subscribe_command(message)

@bot.message_handler(func=lambda message: message.text == "🔙 Назад")
def back_button(message):
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(KeyboardButton("📍 Отправить местоположение", request_location=True))
    markup.add(KeyboardButton("⭐ Избранное"))
    markup.add(KeyboardButton("🔔 Подписка"))
    bot.send_message(message.chat.id, "Главное меню:", reply_markup=markup)

# --------------------------------------------
# 10. ГЕОЛОКАЦИЯ
# --------------------------------------------
@bot.message_handler(content_types=['location'])
def handle_location(message):
    lat = message.location.latitude
    lon = message.location.longitude
    lang = detect_lang(message.text or "ru")
    try:
        reverse_url = f"http://api.openweathermap.org/geo/1.0/reverse?lat={lat}&lon={lon}&appid={API_KEY}&limit=1"
        rev_resp = requests.get(reverse_url, timeout=10)
        rev_resp.raise_for_status()
        rev_data = rev_resp.json()
        if not rev_data:
            bot.reply_to(message, "Не удалось определить город по координатам.")
            return
        location_data = rev_data[0]
        send_current_weather(message, location_data, lang)
    except Exception as e:
        logging.error(f"Location error: {e}")
        bot.reply_to(message, "⚠️ Ошибка при определении города по геолокации.")

# --------------------------------------------
# 11. ТЕКСТОВЫЕ СООБЩЕНИЯ
# --------------------------------------------
@bot.message_handler(content_types=['text'])
def user_message(message):
    city = message.text.strip()
    if not city:
        return
    chat_id = message.chat.id
    favorites = get_favorites(chat_id)
    for fav in favorites:
        if fav['city_name'] == city:
            location_data = {'lat': fav['lat'], 'lon': fav['lon'], 'name': fav['city_name'], 'local_names': {fav['lang']: fav['city_name']}}
            send_current_weather(message, location_data, fav['lang'])
            return
    lang = detect_lang(city)
    error_msg = {'ru': "⚠️ Произошла ошибка. Попробуйте позже.", 'en': "⚠️ An error occurred. Please try again later."}
    try:
        geo_url = f"http://api.openweathermap.org/geo/1.0/direct?q={city}&appid={API_KEY}&limit=5"
        geo_resp = requests.get(geo_url, timeout=10)
        geo_resp.raise_for_status()
        geo_data = geo_resp.json()
        if not geo_data:
            bot.reply_to(message, "Город не найден." if lang == 'ru' else "City not found.")
            return
        if len(geo_data) == 1:
            send_current_weather(message, geo_data[0], lang)
            return
        markup = InlineKeyboardMarkup(row_width=1)
        for item in geo_data[:5]:
            display_name = item.get('local_names', {}).get(lang, item['name'])
            country = item.get('country', '')
            button_text = f"{display_name}, {country}" if country else display_name
            callback_data = f"{item['lat']}|{item['lon']}|{lang}"
            markup.add(InlineKeyboardButton(button_text, callback_data=callback_data))
        markup.add(InlineKeyboardButton("❌ Отмена" if lang == 'ru' else "❌ Cancel", callback_data=f"cancel|{lang}"))
        bot.reply_to(message, "Уточните, какой город вы имели в виду:" if lang == 'ru' else "Please specify which city you meant:", reply_markup=markup)
    except Exception as e:
        logging.error(f"User message error: {e}")
        bot.reply_to(message, error_msg[lang])

# --------------------------------------------
# 12. ИНЛАЙН
# --------------------------------------------
@bot.inline_handler(lambda query: True)
def inline_query_handler(query):
    city = query.query.strip()
    lang = detect_lang(city) if city else 'ru'
    if not city:
        result = InlineQueryResultArticle(
            id="empty",
            title="🌍 Введите название города",
            description="Например: Москва, London, Paris",
            input_message_content=InputTextMessageContent("🌍 Введите название города, чтобы узнать погоду.")
        )
        bot.answer_inline_query(query.id, [result], cache_time=60)
        return
    try:
        geo_url = f"http://api.openweathermap.org/geo/1.0/direct?q={city}&appid={API_KEY}&limit=1"
        geo_resp = requests.get(geo_url, timeout=5)
        geo_resp.raise_for_status()
        geo_data = geo_resp.json()
        if not geo_data:
            result = InlineQueryResultArticle(
                id="not_found",
                title="❌ Город не найден",
                description="Проверьте написание",
                input_message_content=InputTextMessageContent("❌ Город не найден. Проверьте написание." if lang == 'ru' else "❌ City not found. Check spelling.")
            )
            bot.answer_inline_query(query.id, [result], cache_time=60)
            return
        location_data = geo_data[0]
        lat = location_data['lat']
        lon = location_data['lon']
        city_name = location_data.get('local_names', {}).get(lang, location_data['name'])
        cache_key = f"current_{lat},{lon},{lang}"
        now = time.time()
        if cache_key in cache_current:
            cached = cache_current[cache_key]
            if now - cached['timestamp'] < CACHE_TTL:
                temp = cached['data']['temp']
                feels = cached['data']['feels']
                desc = cached['data']['desc']
                hum = cached['data']['hum']
                wind = cached['data']['wind']
            else:
                del cache_current[cache_key]
                temp, feels, desc, hum, wind = fetch_current_weather(lat, lon, lang)
                save_to_cache(cache_current, cache_key, {'temp': temp, 'feels': feels, 'desc': desc, 'hum': hum, 'wind': wind})
        else:
            temp, feels, desc, hum, wind = fetch_current_weather(lat, lon, lang)
            save_to_cache(cache_current, cache_key, {'temp': temp, 'feels': feels, 'desc': desc, 'hum': hum, 'wind': wind})
        units = 'metric'
        temp_str = format_temp(temp, units)
        feels_str = format_temp(feels, units)
        wind_str = format_speed(wind, units)
        emoji = get_weather_emoji(desc)
        if lang == 'ru':
            title = f"🌤 {city_name}: {temp_str}"
            description = f"{emoji} {desc}, влажность {hum}%, ветер {wind_str}"
            reply_text = f"📍 *{city_name}*\n━━━━━━━━━━━━━━━━━━━━━\n🌡 *Температура:* {temp_str} (ощущается {feels_str})\n{emoji} *{desc}*\n💧 *Влажность:* {hum}%\n💨 *Ветер:* {wind_str}"
        else:
            title = f"🌤 {city_name}: {temp_str}"
            description = f"{emoji} {desc}, humidity {hum}%, wind {wind_str}"
            reply_text = f"📍 *{city_name}*\n━━━━━━━━━━━━━━━━━━━━━\n🌡 *Temperature:* {temp_str} (feels like {feels_str})\n{emoji} *{desc}*\n💧 *Humidity:* {hum}%\n💨 *Wind:* {wind_str}"
        result = InlineQueryResultArticle(
            id=f"{city}_{lang}_{int(time.time())}",
            title=title,
            description=description,
            input_message_content=InputTextMessageContent(reply_text, parse_mode='Markdown')
        )
        bot.answer_inline_query(query.id, [result], cache_time=300)
    except Exception as e:
        logging.error(f"Inline error: {e}")

# --------------------------------------------
# 13. CALLBACK QUERY
# --------------------------------------------
@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    chat_id = call.message.chat.id

    # Настройки
    if call.data == "set_metric":
        user_settings[chat_id] = {'units': 'metric'}
        bot.answer_callback_query(call.id, "Установлены °C и м/с")
        bot.edit_message_text("✅ Выбрана метрическая система (Цельсий, м/с).", chat_id=chat_id, message_id=call.message.message_id)
        return
    elif call.data == "set_imperial":
        user_settings[chat_id] = {'units': 'imperial'}
        bot.answer_callback_query(call.id, "Установлены °F и mph")
        bot.edit_message_text("✅ Выбрана имперская система (Фаренгейт, mph).", chat_id=chat_id, message_id=call.message.message_id)
        return
    elif call.data == "close_settings":
        bot.delete_message(chat_id, call.message.message_id)
        bot.answer_callback_query(call.id, "Окно закрыто")
        return

    # Отмена
    if call.data.startswith("cancel"):
        parts = call.data.split('|')
        lang = parts[1] if len(parts) > 1 else 'ru'
        bot.answer_callback_query(call.id, "Отменено" if lang == 'ru' else "Cancelled")
        bot.edit_message_text("❌ Выбор отменён." if lang == 'ru' else "❌ Selection cancelled.", chat_id=chat_id, message_id=call.message.message_id)
        return

    # Прогноз на 5 дней
    if call.data.startswith("forecast"):
        try:
            parts = call.data.split('|')
            lat = float(parts[1])
            lon = float(parts[2])
            lang = parts[3]
            city_name = parts[4] if len(parts) > 4 else "город"
            bot.answer_callback_query(call.id)
            send_forecast(call.message, lat, lon, lang, city_name)
        except Exception as e:
            logging.error(f"Forecast callback error: {e}")
            bot.answer_callback_query(call.id, "Ошибка" if lang == 'ru' else "Error")
            bot.send_message(chat_id, "⚠️ Не удалось загрузить прогноз." if lang == 'ru' else "⚠️ Could not load forecast.")
        return

    # Почасовой
    if call.data.startswith("hourly"):
        try:
            parts = call.data.split('|')
            lat = float(parts[1])
            lon = float(parts[2])
            lang = parts[3]
            city_name = parts[4] if len(parts) > 4 else "город"
            bot.answer_callback_query(call.id)
            send_hourly_forecast(call.message, lat, lon, lang, city_name)
        except Exception as e:
            logging.error(f"Hourly callback error: {e}")
            bot.answer_callback_query(call.id, "Ошибка" if lang == 'ru' else "Error")
            bot.send_message(chat_id, "⚠️ Не удалось загрузить почасовой прогноз." if lang == 'ru' else "⚠️ Could not load hourly forecast.")
        return

    # Подписка – выбор города
    if call.data.startswith("sub_city"):
        try:
            parts = call.data.split('|')
            lat = float(parts[1])
            lon = float(parts[2])
            lang = parts[3]
            city_name = parts[4]
            bot.answer_callback_query(call.id)
            ask_time_selection(call.message, lat, lon, lang, city_name)
            bot.delete_message(chat_id, call.message.message_id)
        except Exception as e:
            logging.error(f"sub_city error: {e}")
            bot.answer_callback_query(call.id, "Ошибка", show_alert=True)
        return

    # Подписка – выбор времени
    if call.data.startswith("sub_time"):
        try:
            parts = call.data.split('|')
            lat = float(parts[1])
            lon = float(parts[2])
            lang = parts[3]
            city_name = parts[4]
            hour = int(parts[5])
            minute = int(parts[6])
            add_subscription(chat_id, city_name, lat, lon, lang, hour, minute)
            schedule_subscription({
                'user_id': chat_id,
                'city_name': city_name,
                'lat': lat,
                'lon': lon,
                'lang': lang,
                'hour': hour,
                'minute': minute
            })
            bot.answer_callback_query(call.id, f"Подписка оформлена на {hour:02d}:{minute:02d}" if lang == 'ru' else f"Subscribed at {hour:02d}:{minute:02d}")
            bot.edit_message_text(
                f"✅ Вы подписались на ежедневную рассылку погоды для {city_name} в {hour:02d}:{minute:02d}." if lang == 'ru' else f"✅ You subscribed to daily weather for {city_name} at {hour:02d}:{minute:02d}.",
                chat_id=chat_id, message_id=call.message.message_id
            )
        except Exception as e:
            logging.error(f"sub_time error: {e}")
            bot.answer_callback_query(call.id, "Ошибка", show_alert=True)
        return

    # Отписка
    if call.data == "sub_unsubscribe":
        sub = get_subscription(chat_id)
        lang = sub['lang'] if sub else 'ru'
        try:
            if remove_subscription(chat_id):
                job_id = f"daily_{chat_id}"
                if scheduler.get_job(job_id):
                    scheduler.remove_job(job_id)
                bot.answer_callback_query(call.id, "Отписка выполнена" if lang == 'ru' else "Unsubscribed")
                bot.edit_message_text("✅ Вы отписались от ежедневной рассылки.", chat_id=chat_id, message_id=call.message.message_id)
            else:
                bot.answer_callback_query(call.id, "Вы не были подписаны", show_alert=True)
        except Exception as e:
            logging.error(f"sub_unsubscribe error: {e}")
            bot.answer_callback_query(call.id, "Ошибка", show_alert=True)
        return

    # Изменить время
    if call.data == "sub_change_time":
        try:
            sub = get_subscription(chat_id)
            if sub:
                bot.answer_callback_query(call.id)
                ask_time_selection(call.message, sub['lat'], sub['lon'], sub['lang'], sub['city_name'])
                bot.delete_message(chat_id, call.message.message_id)
            else:
                bot.answer_callback_query(call.id, "Нет активной подписки", show_alert=True)
        except Exception as e:
            logging.error(f"sub_change_time error: {e}")
            bot.answer_callback_query(call.id, "Ошибка", show_alert=True)
        return

    # Отмена выбора времени (cancel_sub)
    if call.data == "cancel_sub":
        # Определяем язык из текста сообщения
        lang = detect_lang(call.message.text)
        bot.answer_callback_query(call.id, "Отменено" if lang == 'ru' else "Cancelled")
        bot.delete_message(chat_id, call.message.message_id)
        return

    # Добавление в избранное
    if call.data.startswith("add_fav"):
        try:
            parts = call.data.split('|')
            lat = float(parts[1])
            lon = float(parts[2])
            lang = parts[3]
            city_name = parts[4]
            if add_favorite(chat_id, city_name, lat, lon, lang):
                bot.answer_callback_query(call.id, "Город добавлен в избранное!")
                markup = InlineKeyboardMarkup(row_width=2)
                markup.add(
                    InlineKeyboardButton("📅 Прогноз на 5 дней" if lang == 'ru' else "📅 5-day forecast", callback_data=f"forecast|{lat}|{lon}|{lang}|{city_name}"),
                    InlineKeyboardButton("⏰ Сегодня по часам" if lang == 'ru' else "⏰ Hourly today", callback_data=f"hourly|{lat}|{lon}|{lang}|{city_name}")
                )
                markup.add(InlineKeyboardButton("🗑 Удалить из избранного" if lang == 'ru' else "🗑 Remove from favorites", callback_data=f"remove_fav|{lat}|{lon}|{lang}"))
                bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=markup)
            else:
                bot.answer_callback_query(call.id, "Уже в избранном", show_alert=True)
        except Exception as e:
            logging.error(f"Add fav error: {e}")
            bot.answer_callback_query(call.id, "Ошибка добавления", show_alert=True)
        return

    # Удаление из избранного
    if call.data.startswith("remove_fav"):
        try:
            parts = call.data.split('|')
            lat = float(parts[1])
            lon = float(parts[2])
            lang = parts[3]
            if remove_favorite(chat_id, lat, lon):
                bot.answer_callback_query(call.id, "Город удалён из избранного.")
                markup = InlineKeyboardMarkup(row_width=2)
                markup.add(
                    InlineKeyboardButton("📅 Прогноз на 5 дней" if lang == 'ru' else "📅 5-day forecast", callback_data=f"forecast|{lat}|{lon}|{lang}|город"),
                    InlineKeyboardButton("⏰ Сегодня по часам" if lang == 'ru' else "⏰ Hourly today", callback_data=f"hourly|{lat}|{lon}|{lang}|город")
                )
                bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=markup)
            else:
                bot.answer_callback_query(call.id, "Не найден в избранном", show_alert=True)
        except Exception as e:
            logging.error(f"Remove fav error: {e}")
            bot.answer_callback_query(call.id, "Ошибка удаления", show_alert=True)
        return

    # Выбор города из геокодинга
    try:
        parts = call.data.split('|')
        lat = float(parts[0])
        lon = float(parts[1])
        lang = parts[2]
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        reverse_url = f"http://api.openweathermap.org/geo/1.0/reverse?lat={lat}&lon={lon}&appid={API_KEY}&limit=1"
        rev_resp = requests.get(reverse_url, timeout=10)
        rev_resp.raise_for_status()
        rev_data = rev_resp.json()
        if rev_data:
            send_current_weather(call.message, rev_data[0], lang)
        else:
            bot.send_message(chat_id, "Не удалось определить город." if lang == 'ru' else "Could not identify city.")
        bot.answer_callback_query(call.id)
    except Exception as e:
        logging.error(f"City selection callback error: {e}")
        msg_text = call.message.text
        lang = detect_lang(msg_text)
        bot.answer_callback_query(call.id, "Ошибка" if lang == 'ru' else "Error")
        bot.send_message(chat_id, "⚠️ Произошла ошибка." if lang == 'ru' else "⚠️ An error occurred.")

# --------------------------------------------
# 14. ЗАПУСК
# --------------------------------------------
if __name__ == '__main__':
    logging.info("Бот запущен и готов к работе.")
    try:
        bot.polling(none_stop=True)
    except Exception as e:
        logging.error(f"Polling error: {e}")
    finally:
        scheduler.shutdown()