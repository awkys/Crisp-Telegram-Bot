import os
import yaml
import logging
import requests
from datetime import datetime
from threading import Lock

from openai import OpenAI
from crisp_api import Crisp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, Defaults, MessageHandler, filters, ContextTypes, CallbackQueryHandler

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
    elif msg.document and msg.document.mime_type.startswith('image/'):
        file_id = msg.document.file_id
    else:
        await msg.reply_text("请发送图片文件。")
        return

    try:
        # 获取文件下载 URL
        file = await context.bot.get_file(file_id)
        file_url = file.file_path

        # 动态获取 EasyImages 配置
        api_url = config_manager.get('easyimages', 'apiUrl', default='')
        api_token = config_manager.get('easyimages', 'apiToken', default='')
        
        if not api_url or not api_token:
            await msg.reply_text("EasyImages 配置不完整，无法上传图片。")
            return

        # 上传图片到 EasyImages
        uploaded_url = upload_image_to_easyimages(file_url, api_url, api_token)

        # 生成 Markdown 格式的链接
        markdown_link = f"![Image]({uploaded_url})"

        # 查找对应的 Crisp 会话 ID
        session_id = get_target_session_id(context, msg.message_thread_id)
        if session_id:
            # 将 Markdown 链接推送给客户
            send_markdown_to_client(session_id, markdown_link)
            await msg.reply_text("图片已成功发送给客户！")
        else:
            await msg.reply_text("未找到对应的 Crisp 会话，无法发送给客户。")

    except Exception as e:
        await msg.reply_text("图片上传失败，请稍后重试。")
        logging.error(f"图片上传错误: {e}")

def upload_image_to_easyimages(file_url, api_url, api_token):
    try:
        response = requests.get(file_url)
        response.raise_for_status()
        content = response.content
        
        # Calculate MD5
        import hashlib
        md5_hash = hashlib.md5(content).hexdigest()
        
        # Get extension
        ext = os.path.splitext(file_url)[1]
        if not ext:
            ext = '.jpg'
            
        filename = f"{md5_hash}{ext}"
        mime_type = response.headers.get('Content-Type', 'image/jpeg')

        files = {
            'image': (filename, content, mime_type),
            'token': (None, api_token)
        }
        res = requests.post(api_url, files=files)
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

def send_markdown_to_client(session_id, markdown_link):
    try:
        crisp_client = config_manager.get_crisp_client()
        if crisp_client is None:
            raise Exception("Crisp 客户端未初始化")
        
        query = {
            "type": "text",
            "content": markdown_link,
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

def main():
    try:
        # 动态获取 Bot Token
        bot_token = config_manager.get('bot', 'token')
        if not bot_token:
            raise ValueError("Bot Token 未配置")
        
        app = Application.builder().token(bot_token).defaults(Defaults(parse_mode='HTML')).build()
        
        # 启动 Bot
        if os.getenv('RUNNER_NAME') is not None:
            return
        
        app.add_handler(MessageHandler(filters.TEXT, onReply))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handleImage))
        app.add_handler(CallbackQueryHandler(onChange))
        
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