
import os
import yaml
import logging
import requests
import mimetypes

from openai import OpenAI
from crisp_api import Crisp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, Defaults, MessageHandler, filters, ContextTypes, CallbackQueryHandler

import handler

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

# Load Config
try:
    f = open('config.yml', 'r')
    config = yaml.safe_load(f)
except FileNotFoundError as error:
    logging.warning('没有找到 config.yml，请复制 config.yml.example 并重命名为 config.yml')
    exit(1)

# Connect Crisp
try:
    crispCfg = config['crisp']
    client = Crisp()
    client.set_tier("plugin")
    client.authenticate(crispCfg['id'], crispCfg['key'])
    client.plugin.get_connect_account()
    client.website.get_website(crispCfg['website'])
except Exception as error:
    logging.warning('无法连接 Crisp 服务，请确认 Crisp 配置项是否正确')
    exit(1)

# Connect OpenAI
try:
    openai = OpenAI(api_key=config['openai']['apiKey'],base_url='https://api.openai.com/v1')
    openai.models.list()
except Exception as error:
    logging.warning('无法连接 OpenAI 服务，智能化回复将不会使用')
    openai = None

def changeButton(sessionId,boolean):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(
                text='关闭 AI 回复' if boolean else '打开 AI 回复',
                callback_data=f'{sessionId},{boolean}'
                )
            ]
        ]
    )

async def onReply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message

    if msg.chat_id != config['bot']['groupId']:
        return
    for sessionId in context.bot_data:
        if context.bot_data[sessionId]['topicId'] == msg.message_thread_id:
            query = {
                "type": "text",
                "content": msg.text,
                "from": "operator",
                "origin": "chat",
                "user": {
                    "nickname": '人工客服',
                    "avatar": 'https://bpic.51yuansu.com/pic3/cover/03/47/92/65e3b3b1eb909_800.jpg'
                }
            }
            client.website.send_message_in_conversation(
                config['crisp']['website'],
                sessionId,
                query
            )
            return

# Image upload config
IMGBB_API_URL = 'https://api.imgbb.com/1/upload'
IMGBB_API_KEY = config.get('imgbb', {}).get('apiKey', '')
EASYIMAGES_API_URL = config.get('easyimages', {}).get('apiUrl', '')
EASYIMAGES_API_TOKEN = config.get('easyimages', {}).get('apiToken', '')

async def handleImage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message

    if msg.chat_id != config['bot']['groupId']:
        return

    if msg.photo:
        file_id = msg.photo[-1].file_id
        filename = f'{file_id}.jpg'
        mime_type = 'image/jpeg'
    elif msg.document and (msg.document.mime_type or '').startswith('image/'):
        file_id = msg.document.file_id
        filename = msg.document.file_name or 'image'
        mime_type = msg.document.mime_type
    else:
        await msg.reply_text("请发送图片文件。")
        return

    try:
        # 查找对应的 Crisp 会话 ID
        session_id = get_target_session_id(context, msg.message_thread_id)
        if not session_id:
            await msg.reply_text("未找到对应的 Crisp 会话，无法发送给客户。")
            return

        # 获取文件下载 URL
        file = await context.bot.get_file(file_id)
        file_url = file.file_path

        # 上传图片到已配置的图床
        uploaded_url = upload_image(file_url, filename, mime_type)

        # 将图片作为 Crisp 文件消息推送给客户
        send_image_to_client(session_id, uploaded_url, filename, mime_type)
        if msg.caption:
            send_text_to_client(session_id, msg.caption)
        await msg.reply_text("图片已成功发送给客户！")

    except Exception as e:
        await msg.reply_text("图片上传失败，请稍后重试。")
        logging.error(f"图片上传错误: {e}")

def upload_image(file_url, filename='image.jpg', mime_type=None):
    if IMGBB_API_KEY:
        return upload_image_to_imgbb(file_url, filename, mime_type)
    if EASYIMAGES_API_URL and EASYIMAGES_API_TOKEN:
        return upload_image_to_easyimages(file_url, filename, mime_type)
    raise ValueError("未配置图片上传服务，请配置 imgbb.apiKey 或 easyimages.apiUrl/apiToken")

def download_telegram_image(file_url):
    response = requests.get(file_url, timeout=30)
    response.raise_for_status()
    return response.content

def normalize_image_upload_meta(filename, mime_type):
    filename = filename or 'image.jpg'
    mime_type = mime_type or mimetypes.guess_type(filename)[0] or 'image/jpeg'
    return filename, mime_type

def upload_image_to_imgbb(file_url, filename='image.jpg', mime_type=None):
    try:
        filename, mime_type = normalize_image_upload_meta(filename, mime_type)
        image_data = download_telegram_image(file_url)

        files = {
            'image': (filename, image_data, mime_type),
        }
        res = requests.post(
            IMGBB_API_URL,
            params={'key': IMGBB_API_KEY},
            files=files,
            timeout=60,
        )
        res.raise_for_status()
        res_data = res.json()

        image_url = res_data.get('data', {}).get('url')
        if res_data.get('success') is True and image_url:
            return image_url
        raise Exception(f"ImgBB upload failed: {res_data}")
    except Exception as e:
        logging.error(f"Error uploading image to ImgBB: {e}")
        raise

def upload_image_to_easyimages(file_url, filename='image.jpg', mime_type=None):
    try:
        filename, mime_type = normalize_image_upload_meta(filename, mime_type)
        image_data = download_telegram_image(file_url)

        files = {
            'image': (filename, image_data, mime_type),
        }
        data = {
            'token': EASYIMAGES_API_TOKEN,
        }
        res = requests.post(EASYIMAGES_API_URL, data=data, files=files, timeout=60)
        res.raise_for_status()
        res_data = res.json()

        if res_data.get("result") == "success":
            return res_data["url"]
        else:
            raise Exception(f"Image upload failed: {res_data}")
    except Exception as e:
        logging.error(f"Error uploading image: {e}")
        raise

def get_target_session_id(context, thread_id):
    for session_id, session_data in context.bot_data.items():
        if session_data.get('topicId') == thread_id:
            return session_id
    return None

def send_text_to_client(session_id, content):
    try:
        query = {
            "type": "text",
            "content": content,
            "from": "operator",
            "origin": "chat",
            "user": {
                "nickname": "人工客服",
                "avatar": "https://bpic.51yuansu.com/pic3/cover/03/47/92/65e3b3b1eb909_800.jpg"
            }
        }
        client.website.send_message_in_conversation(
            config['crisp']['website'],
            session_id,
            query
        )
        logging.info(f"文本已成功发送至 Crisp 会话 {session_id}")
    except Exception as e:
        logging.error(f"发送文本到 Crisp 失败: {e}")
        raise

def send_image_to_client(session_id, image_url, filename='image.jpg', mime_type=None):
    try:
        filename, mime_type = normalize_image_upload_meta(filename, mime_type)
        query = {
            "type": "file",
            "content": {
                "name": filename,
                "url": image_url,
                "type": mime_type,
            },
            "from": "operator",
            "origin": "chat",
            "user": {
                "nickname": "人工客服",
                "avatar": "https://bpic.51yuansu.com/pic3/cover/03/47/92/65e3b3b1eb909_800.jpg"
            }
        }
        client.website.send_message_in_conversation(
            config['crisp']['website'],
            session_id,
            query
        )
        logging.info(f"图片已成功发送至 Crisp 会话 {session_id}")
    except Exception as e:
        logging.error(f"发送图片到 Crisp 失败: {e}")
        raise

async def onChange(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parses the CallbackQuery and updates the message text."""
    query = update.callback_query

    if openai is None:
        await query.answer('无法设置此功能')
    else:
        data = query.data.split(',')
        session = context.bot_data.get(data[0])
        session["enableAI"] = not eval(data[1])
        await query.answer()
        try:
             await query.edit_message_reply_markup(changeButton(data[0],session["enableAI"]))
        except Exception as error:
            print(error)

def main():
    try:
        app = (
            Application.builder()
            .token(config['bot']['token'])
            .defaults(Defaults(parse_mode='HTML'))
            .post_stop(handler.shutdown)
            .build()
        )
        # 启动 Bot
        if os.getenv('RUNNER_NAME') is not None:
            return
        app.add_handler(MessageHandler(filters.TEXT, onReply))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handleImage))
        app.add_handler(CallbackQueryHandler(onChange))
        app.job_queue.run_once(handler.exec,5,name='RTM')
        app.run_polling(drop_pending_updates=True)
    except Exception as error:
        logging.warning('无法启动 Telegram Bot，请确认 Bot Token 是否正确，或者是否能连接 Telegram 服务器')
        exit(1)


if __name__ == "__main__":
    main()
