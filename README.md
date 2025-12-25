
# WxWork Coze Bridge | 企业微信客服 & Coze AI 智能对接系统

本项目是一个高性能的中间件，用于将企业微信客服的消息转发给 Coze (扣子) 的 AI 工作流处理，并将 AI 回复异步推送回微信用户。它解决了微信服务器 5 秒超时限制的问题，并实现了用户身份的持久化映射。

---

## 📖 目录

* [✨ 核心功能](#-核心功能)
* [🏗️ 系统架构](#系统架构)
* [🚀 快速开始](#-快速开始)
* [1. 环境准备](#1.环境准备)
* [2. 获取代码](2.获取代码)
* [3. 基础配置](3.基础配置)
* [4. Nginx 与 SSL 配置](#4.Nginx-与-SSL-配置)
* [5. 进阶配置：多账号路由 (可选)](#5.进阶配置：多账号路由 (可选))
* [6. 启动服务](#6.启动服务)
* [7. 初始化数据库](#7.初始化数据库)


* [📂 项目目录结构](#-项目目录结构)
* [🔧 关键逻辑说明](#-关键逻辑说明)
* [📝 API 接口说明](#-API-接口说明)
* [⚠️ 注意事项](#-注意事项)

---

## ✨ 核心功能

* **⚡ 全异步架构**：利用 Python `asyncio` 和 FastAPI `BackgroundTasks` 处理消息，防止阻塞主线程，确保快速响应微信服务器的回调验证。
* **🛡️ 消息去重与幂等性**：基于 **Redis** 实现消息 ID 锁（Locking），有效防止因微信服务器重试机制导致的 AI 重复回复。
* **🤖 Coze 多账号路由**：支持根据微信客服 ID (`OpenKfId`) 自动路由到不同的 Coze 机器人/工作流。
* **🔌 双模接口支持**：
* **微信客服模式**：处理 XML/JSON 加密消息，支持文本与图片消息。
* **Open-WebUI 模式**：兼容 OpenAI `/v1/chat/completions` 接口格式，可对接 Open-WebUI 等前端。
* **🐳 生产级部署**：集成 Gunicorn + Uvicorn，配套 Nginx 网关、MySQL 存储和 Redis 缓存，使用 Docker Compose 一键拉起。

## 🏗️ 系统架构

```text
graph LR
    User[微信用户/WebUI] --> Nginx[Nginx 网关]
    Nginx --> App[FastAPI 后端]
    App --> Redis[Redis (去重/缓存)]
    App --> MySQL[MySQL (用户映射/日志)]
    App -- 异步调用 --> Coze[Coze API (AI 处理)]
    Coze -- 回调/响应 --> App
    App -- 异步推送 --> WxServer[企业微信服务器]
    WxServer --> User

```

## 🚀 快速开始

### 1. 环境准备

* Docker & Docker Compose
* 企业微信后台管理员权限 (获取 Token, EncodingAESKey, CorpID)
* Coze 平台账号 (获取 API Token, Bot ID/Workflow ID)

### 2. 获取代码

```bash
git clone https://github.com/biayuexingchen-dot/coze-chat-wxwork.git
cd coze-chat-wxwork

```

### 3. 基础配置

在项目根目录创建 `.env` 文件（参考以下配置）：

```ini
# --- 数据库配置 ---
DB_NAME=coze_db
DB_PASSWORD=your_mysql_password

# --- Redis 配置 ---
REDIS_PASSWORD=your_redis_password

# --- 企业微信客服配置 ---
WEWORK_CORPID=ww12345678...
WEWORK_TOKEN=your_token
WEWORK_ENCODING_AES_KEY=your_aes_key...

# --- Coze 默认配置 (当 config.py 未匹配到特定ID时使用) ---
COZE_API_TOKEN=pat_...
COZE_WORKFLOW_ID=...

```

### 4. Nginx 与 SSL 配置

#### 4.1 准备 SSL 证书

请确保项目根目录下的 `ssl` 文件夹结构与 Nginx 配置一致（请将 `dxgrobot.com` 替换为你的实际域名）：

```text
ssl/
└── dxgrobot.com/
    ├── dxgrobot.com.key
    └── dxgrobot.com.pem

```

#### 4.2 配置 Nginx

在 `config/nginx/` 目录下创建配置文件（例如 `dxgrobot.conf`），用于反向代理和 SSL 卸载。

> ⚠️ **注意**：请将下文中的 `dxgrobot.com` 替换为您自己的实际域名。

```nginx
server {
    listen 80;
    server_name dxgrobot.com www.dxgrobot.com;
    # 强制跳转 HTTPS
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name dxgrobot.com www.dxgrobot.com;
    ......
}

```

### 5. 进阶配置：多账号路由 (可选)

本项目支持**多客服账号分流**功能。你可以将不同的企业微信客服 ID (`OpenKfId`) 映射到不同的 Coze 机器人（不同的 Workflow/Bot）。

请修改 `app/config.py` 文件中的 `COZE_BOT_CONFIGS` 字典进行配置：

```python
# app/config.py

# 1. 多账号配置映射表
# Key: 微信客服 ID (以 wkx 开头)
# Value: 对应的 Coze 机器人配置
COZE_BOT_CONFIGS = {
    # 🤖 场景 A: 售前咨询机器人
    "wkx_PRE_SALES_ID": {
        "name": "售前小助手",
        "token": "pat_xt...",        # Coze API Token
        "workflow_id": "7522...",    # Coze Workflow ID
        "app_id": "7522..."          # Coze Bot ID (可选)
    },

    # 🛡️ 默认/兜底配置 (读取 .env)
    "default": {
        "name": "默认Bot",
        "token": os.getenv("COZE_PAT", "").strip(),
        "workflow_id": os.getenv("COZE_WORKFLOW_ID", ""),
        "app_id": os.getenv("COZE_APP_ID", "")
    }
}

```

### 6. 启动服务

使用 Docker Compose 一键启动所有服务（后端、MySQL、Redis、Nginx）：

```bash
docker-compose up -d

```

启动后，服务将在以下端口运行：

* **Nginx (入口)**: `80` (HTTP) / `443` (HTTPS)
* **FastAPI**: `8000` (仅内部网络访问)

### 7. 初始化数据库

首次启动后，MySQL 容器会自动创建空数据库。请执行以下命令导入初始表结构：

```bash
# 将备份文件导入到 MySQL 容器中
docker exec -i coze_mysql mysql -uroot -p"${DB_PASSWORD}"  < app/backup.sql

```

## 📂 项目目录结构

```text
.
├── app/
│   ├── main.py              # FastAPI 主入口 (路由、消息处理核心逻辑)
│   ├── requirements.txt     # Python 依赖
│   ├── config.py            # 配置加载 (LOGGER, 密钥等)
│   ├── kv.py                # Redis 操作封装
│   ├── wework.py            # 企业微信 API 封装 (解密、发送消息、图片处理)
│   ├── call_coze_api.py     # Coze API 调用封装
│   ├── schema.py            # Pydantic 数据模型
│   ├── backup.sql           # 数据库初始化 SQL 文件
│   └── static/              # 静态文件 (HTML 等)
├── config/
│   └── nginx/               # Nginx 配置文件挂载源
├── ssl/                     # SSL 证书存放目录 (对应 docker-compose 挂载)
│   └── dxgrobot.com/        # 建议按域名建立子文件夹
│       ├── dxgrobot.com.key # 私钥文件
│       └── dxgrobot.com.pem # 证书文件
├── logs/                    # 运行日志
├── data/                    # 数据库持久化数据
├── Dockerfile               # 后端镜像构建文件
├── docker-compose.yml       # 容器编排文件
└── .env                     # 环境变量 (不要提交到 Git)

```

## 🔧 关键逻辑说明

### 1. 解决微信 5 秒超时

微信客服接口要求在 5 秒内响应，否则会发起重试。Coze 的 AI 生成通常耗时较长。

* **解决方案**：`main.py` 中的 `/wechat/hook` 接收到请求后，利用 FastAPI `BackgroundTasks` 将处理逻辑放入后台运行，并立即返回 HTTP 200 给微信服务器。

### 2. 消息去重 (Redis)

代码位置：`process_msg` 函数。

* 当收到消息时，首先检查 Redis 中是否存在 `msgid`。
* 如果存在且状态为“处理中”，则直接丢弃（防止并发重试）。
* 处理完毕后更新状态。

### 3. 用户 ID 映射

代码位置：`async_reply_msg` -> `get_or_create_internal_user`。

* 微信使用的是 `external_userid`。
* 为了在不同渠道（如 Web 和 微信）保持统一的用户画像，系统维护了一套内部 `user_id`，并在调用 Coze 前进行转换。

## 📝 API 接口说明

### 微信回调

* **GET /wechat/hook**: 用于企业微信后台配置时的 URL 验证（校验签名）。
* **POST /wechat/hook**: 接收用户发送的消息事件。

### Open-WebUI 兼容

* **GET /v1/models**: 获取可用模型列表。
* **POST /v1/chat/completions**: OpenAI 格式的对话接口。

### 运维

* **GET /ping**: 健康检查。

## ⚠️ 注意事项

1. **Gunicorn 配置**: `Dockerfile` 中设置了 `timeout 120`，这是为了防止 AI 生成时间过长导致 Gunicorn 杀掉 Worker。
2. **Nginx 配置**:
* **HTTPS**: 企业微信回调地址必须是 HTTPS。请确保 SSL 证书路径正确。
* **文件上传**: 请确保配置了 `client_max_body_size 20M;`，否则用户无法发送图片。


3. **静态文件**: `docker-compose.yml` 中 Nginx 和 Backend 共享了 `static` 卷，确保 Web 访问静态资源时无需经过 Python 处理，提高效率。