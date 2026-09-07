import os
import yaml
import logging
import requests
import mimetypes
from datetime import datetime
from threading import Lock

from openai import OpenAI
from crisp_api import Crisp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, Defaults, MessageHandler, filters, ContextTypes, CallbackQueryHandler, PicklePersistence
import time

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

# 配置管理类
class ConfigManager:
    def __init__(self, config_file='config.yml'):
        self.config_file = config_file
        self.config = None
        self.last_modified = None
        self.lock = Lock()
        self.crisp_client = None
        self.openai_client = None
        self.load_config()
    
    def load_config(self):
        """加载配置文件"""
        try:
            with self.lock:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    self.config = yaml.safe_load(f)
                self.last_modified = os.path.getmtime(self.config_file)
                logging.info(f"配置文件已加载: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                
                # 重新初始化客户端
                self._init_crisp()
                self._init_openai()
                
        except FileNotFoundError:
            logging.error('没有找到 config.yml，请复制 config.yml.example 并重命名为 config.yml')
            raise
        except Exception as e:
            logging.error(f'加载配置文件失败: {e}')
            raise
    
    def _init_crisp(self):
        """初始化 Crisp 客户端"""
        try:
            crisp_cfg = self.config['crisp']
            self.crisp_client = Crisp()
            self.crisp_client.set_tier("plugin")
            self.crisp_client.authenticate(crisp_cfg['id'], crisp_cfg['key'])
            self.crisp_client.plugin.get_connect_account()
            self.crisp_client.website.get_website(crisp_cfg['website'])
            logging.info('Crisp 客户端初始化成功')
        except Exception as e:
            logging.warning(f'无法连接 Crisp 服务: {e}')
            self.crisp_client = None
    
    def _init_openai(self):
        """初始化 OpenAI 客户端"""
        try:
            openai_cfg = self.config.get('openai', {})
            api_key = openai_cfg.get('apiKey', '')
            
            # 如果没有配置 API Key，跳过初始化
            if not api_key or api_key.strip() == '':
                logging.info('OpenAI API Key 未配置，AI 功能将不可用（如不需要可忽略）')
                self.openai_client = None
                return
            
            self.openai_client = OpenAI(
                api_key=api_key,
                base_url=openai_cfg.get('baseUrl', 'https://api.openai.com/v1')
            )
            # 测试连接
            self.openai_client.models.list()
            logging.info('OpenAI 客户端初始化成功')
        except Exception as e:
            # 只在有 API Key 但连接失败时才显示警告
            if openai_cfg.get('apiKey', '').strip():
                logging.warning(f'OpenAI 连接失败: {str(e)}')
            self.openai_client = None
    
    def check_and_reload(self):
        """检查配置文件是否更新，如果更新则重新加载"""
        try:
            current_modified = os.path.getmtime(self.config_file)
            if current_modified != self.last_modified:
                logging.info('检测到配置文件更新，正在重新加载...')
                self.load_config()
                return True
            return False
        except Exception as e:
            logging.error(f'检查配置文件更新失败: {e}')
            return False
    
    def get(self, *keys, default=None):
        """安全获取配置项（不自动检查更新）"""
        value = self.config
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key, default)
            else:
                return default
        return value if value is not None else default
    
    def get_with_reload(self, *keys, default=None):
        """获取配置项（自动检查更新）"""
        self.check_and_reload()
        return self.get(*keys, default=default)
    
    def get_crisp_client(self):
        """获取 Crisp 客户端"""
        self.check_and_reload()
        return self.crisp_client
    
    def get_openai_client(self):
        """获取 OpenAI 客户端"""
        self.check_and_reload()
        return self.openai_client

# 创建全局配置管理器
config_manager = ConfigManager()

# 为了兼容性，保留旧的访问方式（供其他模块导入）
config = config_manager.config  # 静态配置快照
client = config_manager.get_crisp_client()  # Crisp 客户端
openai = config_manager.get_openai_client()  # OpenAI 客户端

# 导出给其他模块使用
__all__ = ['config_manager', 'config', 'client', 'openai', 'changeButton']

def changeButton(sessionId, boolean):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(
                text='关闭 AI 回复' if boolean else '打开 AI 回复',
                callback_data=f'{sessionId},{boolean}'
            )]
        ]
    )

async def onReply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    
    # 动态获取配置
    group_id = config_manager.get('bot', 'groupId')
    crisp_client = config_manager.get_crisp_client()
    
    if msg.chat_id != group_id or crisp_client is None:
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
            crisp_client.website.send_message_in_conversation(
                config_manager.get('crisp', 'website'),
                sessionId,
                query
            )
            return

async def handleImage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message

    if msg.photo:
        file_id = msg.photo[-1].file_id
        file_name = f"telegram-photo-{msg.message_id}.jpg"
        mime_type = "image/jpeg"
    elif msg.document and (msg.document.mime_type or '').startswith('image/'):
        file_id = msg.document.file_id
        file_name = msg.document.file_name or f"telegram-image-{msg.message_id}"
        mime_type = msg.document.mime_type
    else:
        await msg.reply_text("请发送图片文件。")
        return

    try:
        image_bytes = await download_telegram_file(context, file_id)
        uploaded_url = upload_image_to_host(image_bytes, file_name, mime_type)
        logging.info(f"图片上传成功: {uploaded_url}")

        # 查找对应的 Crisp 会话 ID
        session_id = get_target_session_id(context, msg.message_thread_id)
        if session_id:
            # 将图片作为文件推送给客户
            send_image_to_client(session_id, uploaded_url)
            await msg.reply_text(f"图片已成功发送给客户！\n链接: {uploaded_url}")
        else:
            await msg.reply_text("未找到对应的 Crisp 会话，无法发送给客户。")

    except Exception as e:
        error_msg = f"图片上传失败: {str(e)}"
        # 使用 parse_mode=None 避免错误消息中的 HTML 标签导致发送失败
        try:
            await msg.reply_text(error_msg, parse_mode=None)
        except Exception:
            await msg.reply_text("图片上传失败，请查看服务器日志", parse_mode=None)
        logging.error(error_msg)

async def download_telegram_file(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> bytes:
    """使用 Telegram SDK 下载文件，避免直接拼 URL 在不同部署环境下失效。"""
    file = await context.bot.get_file(file_id)
    data = await file.download_as_bytearray()
    if not data:
        raise Exception("Telegram 文件下载为空")
    return bytes(data)

def upload_image_to_host(image_bytes, file_name, mime_type):
    """按配置上传图片，优先使用 EasyImages，失败时可回退到 imgbb。"""
    easyimages_cfg = config_manager.get('easyimages', default={}) or {}
    imgbb_cfg = config_manager.get('imgbb', default={}) or {}
    errors = []

    easyimages_url = easyimages_cfg.get('apiUrl', '')
    easyimages_token = easyimages_cfg.get('apiToken', '')
    if easyimages_url and easyimages_token:
        try:
            return upload_image_to_easyimages(
                image_bytes,
                file_name,
                mime_type,
                easyimages_url,
                easyimages_token
            )
        except Exception as e:
            errors.append(f"EasyImages: {e}")

    imgbb_api_key = imgbb_cfg.get('apiKey', '')
    if imgbb_api_key:
        try:
            return upload_image_to_imgbb(image_bytes, imgbb_api_key)
        except Exception as e:
            errors.append(f"imgbb: {e}")

    if errors:
        raise Exception("；".join(errors))

    raise Exception("未配置图床，请配置 easyimages.apiUrl/apiToken 或 imgbb.apiKey")

def upload_image_to_easyimages(image_bytes, file_name, mime_type, api_url, api_token):
    """上传图片到 EasyImages 图床"""
    guessed_type = mime_type or mimetypes.guess_type(file_name)[0] or "image/jpeg"
    files = {
        "image": (file_name or "image.jpg", image_bytes, guessed_type)
    }
    data = {
        "token": api_token
    }

    try:
        res = requests.post(api_url, files=files, data=data, timeout=30)
        res.raise_for_status()
        try:
            res_data = res.json()
        except ValueError as error:
            raise Exception(f"EasyImages 返回非 JSON: {res.text[:300]}") from error

        if res_data.get("result") == "success" and res_data.get("url"):
            return res_data["url"]

        raise Exception(f"EasyImages API 错误: {res_data}")
    except Exception as e:
        logging.error(f"上传图片到 EasyImages 失败: {e}")
        raise

def upload_image_to_imgbb(image_bytes, api_key):
    """上传图片到 imgbb 图床"""
    import base64
    try:
        # 转为 base64
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')
        
        # 调用 imgbb API
        url = "https://api.imgbb.com/1/upload"
        payload = {
            'key': api_key,
            'image': image_base64
        }
        
        res = requests.post(url, data=payload, timeout=30)
        res.raise_for_status()
        try:
            res_data = res.json()
        except ValueError as error:
            raise Exception(f"imgbb 返回非 JSON: {res.text[:300]}") from error
        
        if res_data.get("success"):
            return res_data["data"]["display_url"]
        else:
            raise Exception(f"imgbb API 错误: {res_data}")
    except Exception as e:
        logging.error(f"上传图片到 imgbb 失败: {e}")
        raise

def get_target_session_id(context, thread_id):
    for session_id, session_data in context.bot_data.items():
        if session_data.get('topicId') == thread_id:
            return session_id
    return None

def send_image_to_client(session_id, url):
    try:
        crisp_client = config_manager.get_crisp_client()
        if crisp_client is None:
            raise Exception("Crisp 客户端未初始化")
        
        # 构造文件消息
        query = {
            "type": "file",
            "content": {
                "url": url,
                "type": "image/jpg",  # 默认使用 jpg，也可以根据 URL 后缀判断
                "name": "image.jpg"
            },
            "from": "operator",
            "origin": "chat",
            "user": {
                "nickname": "人工客服",
                "avatar": "https://bpic.51yuansu.com/pic3/cover/03/47/92/65e3b3b1eb909_800.jpg"
            }
        }
        crisp_client.website.send_message_in_conversation(
            config_manager.get('crisp', 'website'),
            session_id,
            query
        )
        logging.info(f"图片链接已成功发送至 Crisp 会话 {session_id}")
    except Exception as e:
        logging.error(f"发送图片链接到 Crisp 失败: {e}")
        raise

async def onChange(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parses the CallbackQuery and updates the message text."""
    query = update.callback_query
    openai_client = config_manager.get_openai_client()

    if openai_client is None:
        await query.answer('无法设置此功能')
    else:
        data = query.data.split(',')
        session = context.bot_data.get(data[0])
        session["enableAI"] = not eval(data[1])
        await query.answer()
        try:
            await query.edit_message_reply_markup(changeButton(data[0], session["enableAI"]))
        except Exception as error:
            logging.error(f"更改按钮状态失败: {error}")

async def cleanup_old_topics(context: ContextTypes.DEFAULT_TYPE) -> None:
    """清理超过 3 天不活跃的 Telegram 话题"""
    group_id = config_manager.get('bot', 'groupId')
    if not group_id: 
        return
        
    # 3天，单位秒
    expire_time = 3 * 24 * 3600
    current_time = time.time()
    to_delete = []
    
    for session_id, session_data in list(context.bot_data.items()):
        if not isinstance(session_data, dict):
            continue
            
        last_active = session_data.get('last_active', 0)
        topic_id = session_data.get('topicId')
        
        # 针对之前没有 last_active 的历史数据，先赋值当前时间
        if last_active == 0:
            session_data['last_active'] = current_time
            continue

        if (current_time - last_active) > expire_time and topic_id:
            try:
                # 尝试删除话题（如果只是想关闭，可以使用 close_forum_topic）
                await context.bot.delete_forum_topic(chat_id=group_id, message_thread_id=topic_id)
                logging.info(f"已删除超过 3 天不活跃的话题，Topic ID: {topic_id}")
            except Exception as e:
                # 如果找不到或者没有权限，捕获错误并跳过
                logging.warning(f"删除话题失败 Topic ID {topic_id}: {e}")
            finally:
                to_delete.append(session_id)
                
    for session_id in to_delete:
        if session_id in context.bot_data:
            del context.bot_data[session_id]

def main():
    try:
        # 动态获取 Bot Token
        bot_token = config_manager.get('bot', 'token')
        if not bot_token:
            raise ValueError("Bot Token 未配置")
        
        # 持久化存储
        persistence = PicklePersistence(filepath='bot_data.pickle')
        
        app = Application.builder().token(bot_token).defaults(Defaults(parse_mode='HTML')).persistence(persistence).build()
        
        # 启动 Bot
        if os.getenv('RUNNER_NAME') is not None:
            return
        
        app.add_handler(MessageHandler(filters.TEXT, onReply))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handleImage))
        app.add_handler(CallbackQueryHandler(onChange))
        
        # 定时任务：每小时执行一次清理，第一次启动后 1 分钟执行
        app.job_queue.run_repeating(cleanup_old_topics, interval=3600, first=60)
        
        # 延迟导入 handler 模块，确保模块完全加载
        try:
            import handler
            if hasattr(handler, 'exec'):
                app.job_queue.run_once(handler.exec, 5, name='RTM')
                logging.info("Handler RTM 任务已添加")
            else:
                logging.warning("Handler 模块中未找到 exec 函数")
        except ImportError as e:
            logging.error(f"无法导入 handler 模块: {e}")
        
        logging.info("Telegram Bot 启动成功，配置支持热加载")
        app.run_polling(drop_pending_updates=True)
        
    except Exception as error:
        logging.error(f'无法启动 Telegram Bot: {error}')
        import traceback
        traceback.print_exc()
        exit(1)


if __name__ == "__main__":
    main()
