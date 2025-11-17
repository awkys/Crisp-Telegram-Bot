#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Crisp Telegram Bot Handler
处理 Crisp 消息的转发和自动回复
"""

import bot
import json
import base64
import socketio
import requests
import logging
from telegram.ext import ContextTypes

# 使用新的配置管理器
config_manager = bot.config_manager

# 全局变量
callbackContext = None

# ==================== 辅助函数 ====================

def get_config():
    """获取最新配置"""
    return {
        'groupId': config_manager.get('bot', 'groupId'),
        'websiteId': config_manager.get('crisp', 'website'),
        'payload': config_manager.get('openai', 'payload', default='你是一个智能客服助手'),
        'autoreply': config_manager.get('autoreply', default={}),
        'crisp_id': config_manager.get('crisp', 'id'),
        'crisp_key': config_manager.get('crisp', 'key')
    }


def get_clients():
    """获取最新客户端"""
    return {
        'crisp': config_manager.get_crisp_client(),
        'openai': config_manager.get_openai_client()
    }


def getKey(content: str):
    """
    检查消息内容是否包含自动回复关键词
    
    Args:
        content: 消息内容
        
    Returns:
        tuple: (是否匹配, 回复内容)
    """
    cfg = get_config()
    autoreply_config = cfg['autoreply']
    
    if len(autoreply_config) > 0:
        for x in autoreply_config:
            keyword = x.split("|")
            for key in keyword:
                if key in content:
                    return True, autoreply_config[x]
    return False, None


def getMetas(sessionId):
    """
    获取会话元数据信息
    
    Args:
        sessionId: Crisp 会话 ID
        
    Returns:
        str: 格式化的会话信息
    """
    clients = get_clients()
    cfg = get_config()
    
    if clients['crisp'] is None:
        logging.warning("Crisp 客户端未连接，无法获取会话信息")
        return '无法获取信息：Crisp 客户端未连接'
    
    try:
        metas = clients['crisp'].website.get_conversation_metas(
            cfg['websiteId'], 
            sessionId
        )

        flow = ['📠<b>Crisp消息推送</b>', '']
        
        # 添加邮箱信息
        if metas.get("email") and len(metas["email"]) > 0:
            email = metas["email"]
            flow.append(f'📧<b>电子邮箱</b>：{email}')
        
        # 添加自定义数据
        if metas.get("data") and len(metas["data"]) > 0:
            if "Plan" in metas["data"]:
                Plan = metas["data"]["Plan"]
                flow.append(f"🪪<b>使用套餐</b>：{Plan}")
            
            if "UsedTraffic" in metas["data"] and "AllTraffic" in metas["data"]:
                UsedTraffic = metas["data"]["UsedTraffic"]
                AllTraffic = metas["data"]["AllTraffic"]
                flow.append(f"🗒<b>流量信息</b>：{UsedTraffic} / {AllTraffic}")
        
        if len(flow) > 2:
            return '\n'.join(flow)
        return '无额外信息'
        
    except Exception as e:
        logging.error(f"获取 Metas 失败: {e}")
        return '获取信息失败'


# ==================== 核心处理函数 ====================

async def createSession(data):
    """
    创建新的 Telegram 话题对应 Crisp 会话
    
    Args:
        data: Crisp 会话数据
    """
    if callbackContext is None:
        logging.error("callbackContext 未初始化")
        return
    
    tg_bot = callbackContext.bot
    botData = callbackContext.bot_data
    sessionId = data["session_id"]
    session = botData.get(sessionId)
    
    cfg = get_config()
    clients = get_clients()

    metas = getMetas(sessionId)
    
    if session is None:
        # 检查是否启用 AI
        enableAI = clients['openai'] is not None
        
        try:
            # 创建论坛话题
            topic = await tg_bot.create_forum_topic(
                cfg['groupId'], 
                data["user"]["nickname"]
            )
            
            # 发送初始消息
            msg = await tg_bot.send_message(
                cfg['groupId'],
                metas,
                message_thread_id=topic.message_thread_id,
                reply_markup=bot.changeButton(sessionId, enableAI)
            )
            
            # 保存会话信息
            botData[sessionId] = {
                'topicId': topic.message_thread_id,
                'messageId': msg.message_id,
                'enableAI': enableAI
            }
            
            logging.info(f"创建新会话: {sessionId} -> Topic {topic.message_thread_id}")
            
        except Exception as error:
            logging.error(f"创建会话失败: {error}")
    else:
        # 更新现有会话信息
        try:
            await tg_bot.edit_message_text(
                metas, 
                cfg['groupId'], 
                session['messageId']
            )
            logging.info(f"更新会话信息: {sessionId}")
        except Exception as error:
            logging.error(f"编辑消息失败: {error}")


async def sendMessage(data):
    """
    处理并发送消息到 Telegram
    
    Args:
        data: Crisp 消息数据
    """
    if callbackContext is None:
        logging.error("callbackContext 未初始化")
        return
    
    tg_bot = callbackContext.bot
    botData = callbackContext.bot_data
    sessionId = data["session_id"]
    session = botData.get(sessionId)
    
    if session is None:
        logging.warning(f"会话不存在: {sessionId}")
        return
    
    cfg = get_config()
    clients = get_clients()
    
    if clients['crisp'] is None:
        logging.error("Crisp 客户端未连接，无法处理消息")
        return

    try:
        # 标记消息为已读
        clients['crisp'].website.mark_messages_read_in_conversation(
            cfg['websiteId'], 
            sessionId,
            {
                "from": "user", 
                "origin": "chat", 
                "fingerprints": [data["fingerprint"]]
            }
        )
    except Exception as e:
        logging.error(f"标记消息已读失败: {e}")

    # 处理文本消息
    if data["type"] == "text":
        flow = ['📠<b>消息推送</b>', '']
        flow.append(f"🧾<b>消息内容</b>：{data['content']}")

        autoreply = None
        
        # 检查自动回复关键词
        result, autoreply = getKey(data["content"])
        
        if result is True:
            flow.append("")
            flow.append(f"💡<b>自动回复</b>：{autoreply}")
            logging.info(f"使用关键词自动回复: {sessionId}")
        
        # 使用 AI 生成回复
        elif clients['openai'] is not None and session.get("enableAI") is True:
            try:
                logging.info(f"调用 OpenAI 生成回复: {sessionId}")
                response = clients['openai'].chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "system", "content": cfg['payload']},
                        {"role": "user", "content": data["content"]}
                    ],
                    max_tokens=500,
                    temperature=0.7
                )
                autoreply = response.choices[0].message.content
                flow.append("")
                flow.append(f"💡<b>AI 回复</b>：{autoreply}")
                logging.info(f"AI 回复生成成功: {sessionId}")
            except Exception as e:
                logging.error(f"OpenAI 调用失败: {e}")
                autoreply = None
        
        # 发送自动回复到 Crisp
        if autoreply is not None:
            try:
                query = {
                    "type": "text",
                    "content": autoreply,
                    "from": "operator",
                    "origin": "chat",
                    "user": {
                        "nickname": '智能客服',
                        "avatar": 'https://img.ixintu.com/download/jpg/20210125/8bff784c4e309db867d43785efde1daf_512_512.jpg'
                    }
                }
                clients['crisp'].website.send_message_in_conversation(
                    cfg['websiteId'], 
                    sessionId, 
                    query
                )
                logging.info(f"自动回复已发送到 Crisp: {sessionId}")
            except Exception as e:
                logging.error(f"发送自动回复到 Crisp 失败: {e}")
        
        # 发送消息通知到 Telegram
        try:
            await tg_bot.send_message(
                cfg['groupId'],
                '\n'.join(flow),
                message_thread_id=session["topicId"]
            )
            logging.info(f"消息已转发到 Telegram: {sessionId}")
        except Exception as e:
            logging.error(f"发送消息到 Telegram 失败: {e}")
    
    # 处理图片消息
    elif data["type"] == "file" and str(data["content"]["type"]).count("image") > 0:
        try:
            await tg_bot.send_photo(
                cfg['groupId'],
                data["content"]["url"],
                message_thread_id=session["topicId"]
            )
            logging.info(f"图片已转发到 Telegram: {sessionId}")
        except Exception as e:
            logging.error(f"发送图片到 Telegram 失败: {e}")
    
    # 其他类型消息
    else:
        logging.warning(f"未处理的消息类型: {data['type']}")


# ==================== Socket.IO 事件处理 ====================

# 创建 Socket.IO 客户端
sio = socketio.AsyncClient(
    reconnection=True,
    reconnection_attempts=0,  # 无限重连
    reconnection_delay=1,
    reconnection_delay_max=5,
    logger=True,
    engineio_logger=False
)


@sio.on("connect")
async def on_connect():
    """Socket.IO 连接成功"""
    cfg = get_config()
    logging.info("正在进行 Crisp 认证...")
    
    try:
        await sio.emit("authentication", {
            "tier": "plugin",
            "username": cfg['crisp_id'],
            "password": cfg['crisp_key'],
            "events": [
                "message:send",
                "session:set_data"
            ]
        })
        logging.info("Crisp 认证请求已发送")
    except Exception as e:
        logging.error(f"发送认证请求失败: {e}")


@sio.on("authenticated")
async def on_authenticated():
    """认证成功"""
    logging.info("✓ Crisp 认证成功，开始监听消息")


@sio.on("unauthorized")
async def on_unauthorized(data):
    """认证失败"""
    logging.error(f"✗ Crisp 认证失败: {data}")


@sio.event
async def connect_error(data):
    """连接错误"""
    logging.error(f"Crisp 连接错误: {data}")


@sio.event
async def disconnect():
    """断开连接"""
    logging.warning("已断开与 Crisp RTM 服务器的连接")


@sio.on("message:send")
async def on_message_send(data):
    """
    接收到新消息
    
    Args:
        data: 消息数据
    """
    cfg = get_config()
    
    # 检查是否是当前网站的消息
    if data.get("website_id") != cfg['websiteId']:
        logging.debug(f"忽略其他网站的消息: {data.get('website_id')}")
        return
    
    logging.info(f"收到新消息: {data.get('session_id')}")
    
    try:
        await createSession(data)
        await sendMessage(data)
    except Exception as e:
        logging.error(f"处理消息失败: {e}")
        import traceback
        traceback.print_exc()


@sio.on("session:set_data")
async def on_session_set_data(data):
    """会话数据更新"""
    logging.info(f"会话数据已更新: {data.get('session_id')}")


# ==================== Crisp 连接 ====================

def getCrispConnectEndpoints():
    """
    获取 Crisp WebSocket 连接端点
    
    Returns:
        str: WebSocket 端点 URL
    """
    url = "https://api.crisp.chat/v1/plugin/connect/endpoints"
    
    cfg = get_config()
    authtier = base64.b64encode(
        (cfg['crisp_id'] + ":" + cfg['crisp_key']).encode("utf-8")
    ).decode("utf-8")
    
    headers = {
        "X-Crisp-Tier": "plugin", 
        "Authorization": "Basic " + authtier
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        endpoint = data.get("data", {}).get("socket", {}).get("app")
        
        if not endpoint:
            raise ValueError("无法从响应中获取端点")
        
        logging.info(f"获取到 Crisp 端点: {endpoint}")
        return endpoint
    except Exception as e:
        logging.error(f"获取 Crisp 端点失败: {e}")
        raise


# ==================== 主执行函数 ====================

async def exec(context: ContextTypes.DEFAULT_TYPE):
    """
    执行主任务：连接到 Crisp RTM 服务器
    
    Args:
        context: Telegram Bot 上下文
    """
    global callbackContext
    callbackContext = context
    
    logging.info("开始连接 Crisp RTM 服务器...")
    
    try:
        # 获取连接端点
        endpoint = getCrispConnectEndpoints()
        
        # 连接到 Crisp
        await sio.connect(
            endpoint,
            transports=["websocket"],
            wait_timeout=10,
        )
        
        logging.info("✓ 成功连接到 Crisp RTM 服务器")
        
        # 保持连接
        await sio.wait()
        
    except Exception as e:
        logging.error(f"✗ 连接 Crisp RTM 失败: {e}")
        import traceback
        traceback.print_exc()
        
        # 等待一段时间后重试
        import asyncio
        logging.info("10 秒后尝试重新连接...")
        await asyncio.sleep(10)
        await exec(context)


# ==================== 模块入口 ====================

if __name__ == "__main__":
    print("此模块不应直接运行，请运行 bot.py")