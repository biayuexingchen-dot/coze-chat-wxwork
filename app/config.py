import logging
import os
from dotenv import load_dotenv
import redis
import uuid
import base64
import struct

# 加载 .env 文件中的环境变量
load_dotenv()

# WeWork 配置
WEWORK_TOKEN_API = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
WEWORK_CORPID = os.getenv("WEWORK_CORPID")
WEWORK_CORPSECRET = os.getenv("WEWORK_CORPSECRET")
WEWORK_ENCODING_AES_KEY = os.getenv("WEWORK_ENCODING_AES_KEY")
WEWORK_TOKEN = os.getenv("WEWORK_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# 应用配置
DEBUG = os.getenv("DEBUG", "False").lower() == "true"

# Mysql 配置
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "root")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", 3306))
DB_NAME = os.getenv("DB_NAME", "conversation_history")

# Redis 配置
REDISHOST = os.getenv("REDISHOST", "redis")
REDISPORT = int(os.getenv("REDISPORT", 6379))
REDIS_DB = int(os.getenv("REDIS_DB", 0))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "redis")

REDIS_CLIENT = redis.Redis(host=REDISHOST, port=REDISPORT, db=REDIS_DB, password=REDIS_PASSWORD)

# 日志配置
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
LOGGER = logging.getLogger(__name__)

# 上传图片的 URL
SERVER_BASE_URL = "https://testrobot.com"
TEMP_IMAGE_DIR = "static/images"
os.makedirs(TEMP_IMAGE_DIR, exist_ok=True)


# 构造内部用户ID
def generate_internal_uid(prefix="user"):
    """
    构造类似微信风格的 ID
    格式: prefix + base64(uuid bytes)
    例如: usr_X9s8f7D...
    """
    # 1. 生成一个标准的 UUID (128 bit / 16 bytes)
    uid = uuid.uuid4()

    # 2. 获取二进制数据 (bytes)
    uid_bytes = uid.bytes

    # 3. 进行 URL 安全的 Base64 编码
    # 结果类似: b'7wNZhwlhRddYqmkuul8mDw=='
    b64_uid = base64.urlsafe_b64encode(uid_bytes).decode('utf-8')

    # 4. 去掉末尾的填充符号 '=' (Base64生成长度固定，不需要padding)
    b64_uid = b64_uid.rstrip('=')

    # 5. 拼接前缀
    return f"{prefix}_{b64_uid}"


# Coze 工作流相关配置
# ==============================================================================
# 1. 多账号配置映射表
# ==============================================================================
# 这里的 Key 是微信客服的 OpenKfId (wk开头)
# Value 是对应的 Coze 机器人配置
COZE_BOT_CONFIGS = {
    # 🤖 账号 A: 测试1 (生产环境)
    "wkx_XXXXXXXXXXX": {
        "name": "测试1",
        "token": "pat_XXXXXXXXXX",
        "workflow_id": "XXXXXXXXXX",
        "app_id": "XXXXXXXXXX"
    },

    # 🤖 账号 B: 测试2 (生产环境)
    "wkx_XXXXXXXXXXXXXXXXXXXXXX": {
        "name": "测试2",
        "token": "pat_XXXXXXXXXX",
        "workflow_id": "XXXXXXXXXX",
        "app_id": "XXXXXXXXXX"
    },

    # 🛡️ 默认/兜底配置
    # 优先读取 .env 文件，如果没配 .env，则使用代码里的硬编码值
    "default": {
        "name": "默认Bot",
        # 尝试从环境变量读取，如果没有则使用硬编码
        "token": os.getenv("COZE_PAT", "pat_XXXXXXXXXX").strip(),
        "workflow_id": os.getenv("COZE_WORKFLOW_ID", "XXXXXXXXXX"),
        "app_id": os.getenv("COZE_APP_ID", "XXXXXXXXXX")
    }
}


# ==============================================================================
# 2. 综合获取配置函数 (替代原来的 init_config 和 get_coze_config)
# ==============================================================================
def get_coze_config(open_kfid: str = None) -> dict:
    """
    根据 OpenKfId 获取最终的 Coze 配置字典。

    功能特点：
    1. 自动路由：根据 open_kfid 匹配不同机器人。
    2. 自动兜底：匹配不到 ID 时，返回 default 配置。
    3. 自动补全：确保 token 包含 'Bearer ' 前缀。
    4. 安全校验：检查关键参数是否为空。
    """

    # 1. 获取原始配置字典
    raw_config = COZE_BOT_CONFIGS.get(open_kfid)
    print("🚀 初始化Coze环境变量")
    if raw_config:
        print(f"🎯 [Config] 命中特定配置: {raw_config['name']} (ID: {open_kfid})")
        pass
    else:
        # 没找到 ID，使用默认配置
        raw_config = COZE_BOT_CONFIGS["default"]
        if open_kfid:  # 只有当传入了ID但没找到时才打印警告
            print(f"⚠️ [Config] 未知客服ID [{open_kfid}]，降级使用默认配置: {raw_config['name']}")

    # 2. 处理 Token 格式 (统一添加 Bearer 前缀)
    # 许多人容易在这个细节出错，这里统一处理最稳妥
    token = raw_config.get("token", "").strip()
    if token and not token.startswith("Bearer "):
        final_token = f"Bearer {token}"
    else:
        final_token = token

    # 3. 构造最终配置对象
    final_config = {
        "name": raw_config.get("name", "Unknown"),
        "token": final_token,
        "workflow_id": raw_config.get("workflow_id", ""),
        "app_id": raw_config.get("app_id", "")
    }

    # 4. 完整性校验
    # 检查是否有空值
    missing_keys = [k for k, v in final_config.items() if not v]
    if missing_keys:
        error_msg = f"❌ 配置错误: 客服账号 [{final_config['name']}] 缺少关键参数: {', '.join(missing_keys)}"
        print(error_msg)
        # 在生产环境中，这里可以选择抛出异常，或者返回空字典让调用方处理
        # raise ValueError(error_msg)
        return {}

    return final_config


if __name__ == "__main__":
    result = generate_internal_uid()  # 测试生成内部用户ID
    print("生成的内部用户ID:", result)