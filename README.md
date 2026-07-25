# Crisp Telegram Bot via Python

## 此项目已不再维护，请移步：<https://ai.haruka.cloud>

一个简单的项目，让 Crisp 客服系统支持透过 Telegram Bot 来快速回复。  
使用反馈、功能定制可加群：[https://t.me/dyaogroup](https://t.me/dyaogroup)

Python 版本需求 >= 3.9

## 更新

```bash
cd /var/www/Crisp-Telegram-Bot
git pull
source .venv/bin/activate
pip install -r requirements.txt
systemctl restart crisp-bot.service
journalctl -u crisp-bot.service -f
```

## 现有功能

- 基于 Crisp 客服系统
- 基于 Telegram 话题群将消息分栏
- 自动推送文字、图片到指定聊天
- 支持回复后推送回对应客户
- 兼容V2B以显示套餐信息
- 关键词回复以及基于GPT的智能回复

## 计划功能

- 回复图片功能（需要Crisp订阅）

## 常规使用

```bash
# 安装系统依赖
apt update
apt install -y git python3 python3-pip python3-venv python3-full

# 拉取项目，按需二选一
git clone https://github.com/DyAxy/Crisp-Telegram-Bot.git
git clone git@github.com:awkys/Crisp-Telegram-Bot.git

cd Crisp-Telegram-Bot

# Debian/Ubuntu 新版本不建议直接 pip3 install 到系统环境，使用虚拟环境部署
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

cp config.yml.example config.yml
nano config.yml

# 根据注释修改 Telegram、Crisp、OpenAI 等配置后，先前台测试
python3 bot.py
```

如果执行 `pip3 install -r requirements.txt` 出现 `externally-managed-environment`，说明当前系统启用了 PEP 668 保护。请使用上面的 `.venv` 虚拟环境方式安装，不建议使用 `--break-system-packages`。

## systemd 常驻运行

假设项目路径为 `/var/www/Crisp-Telegram-Bot`，并且已经按上方步骤创建 `.venv`、安装依赖、配置好 `config.yml`，可以创建 systemd 服务：

```bash
cat > /etc/systemd/system/crisp-bot.service <<'EOF'
[Unit]
Description=Crisp Telegram Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/var/www/Crisp-Telegram-Bot
ExecStart=/var/www/Crisp-Telegram-Bot/.venv/bin/python /var/www/Crisp-Telegram-Bot/bot.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now crisp-bot.service
systemctl status crisp-bot.service
```

查看日志：

```bash
journalctl -u crisp-bot.service -f
```

常用命令：

```bash
systemctl restart crisp-bot.service
systemctl stop crisp-bot.service
systemctl start crisp-bot.service
systemctl status crisp-bot.service
```

## 申请 Telegram Bot Token

1. 私聊 [https://t.me/BotFather](https://https://t.me/BotFather)
2. 输入 `/newbot`，并为你的bot起一个**响亮**的名字
3. 接着为你的bot设置一个username，但是一定要以bot结尾，例如：`v2board_bot`
4. 最后你就能得到bot的token了，看起来应该像这样：`123456789:gaefadklwdqojdoiqwjdiwqdo`

## 创建 Telegram Topic 群

1. 创建一个群聊，并将申请的 Bot 拉进去
2. 在管理群中，打开话题 (Topic)，并将 Bot 设为管理员
3. 将 # 的话题设为置顶 (Pin)

## 申请 Crisp 以及 MarketPlace 插件

1. 注册 [https://app.crisp.chat/initiate/signup](https://app.crisp.chat/initiate/signup)
2. 完成注册后，网站ID在浏览器中即可找到，看起来应该像这样：`https://app.crisp.chat/settings/website/12345678-1234-1234-1234-1234567890ab/`
3. 其中 `12345678-1234-1234-1234-1234567890ab` 就是网站ID
4. 前往 MarketPlace， 需要重新注册账号 [https://marketplace.crisp.chat/](https://marketplace.crisp.chat/)
5. 点击 New Plugin，选择 Private，输入名字以及描述。会获得开发者ID和Key，可能会不够用。
6. 需要Production Key，点击 Ask a production token，再点击Add a Scope。
7. 需要 2 条read和write权限：`website:conversation:sessions` 和 `website:conversation:messages`
8. 保存后即可获得ID和Key，此时点击右上角 Install Plugin on Website 即可。
