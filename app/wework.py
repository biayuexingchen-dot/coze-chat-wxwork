#
import json
import time
from typing import List
import os
from fastapi import Request
from config import (
    LOGGER,
    WEWORK_CORPID,
    WEWORK_CORPSECRET,
    WEWORK_ENCODING_AES_KEY,
    WEWORK_TOKEN,
    WEWORK_TOKEN_API,
    TEMP_IMAGE_DIR,
    SERVER_BASE_URL,
    REDIS_CLIENT,
)
import requests
import httpx  # 引入 httpx
import asyncio
import xml.etree.ElementTree as ET
from kv import get_msg_retry, set_cursor, set_msg_retry
from schema import WeChatMessage, WechatMsgEntity, WechatMsgSendEntity
from util.wx_biz_json_msg_crypt import WXBizJsonMsgCrypt
from ai import ai_reply, ai_reply_coze, async_ai_reply_coze
from call_coze_api import get_or_create_latest_conversation, call_coze_workflow, get_or_create_internal_user


async def parse_wechat_message(request: Request) -> WeChatMessage:
    body = await request.body()
    xml_data = body.decode('utf-8')
    root = ET.fromstring(xml_data)

    message_dict = {
        'ToUserName': root.find('ToUserName').text,
        'AgentID': root.find('AgentID').text,
        'Encrypt': root.find('Encrypt').text
    }

    return WeChatMessage(**message_dict)


# 检查签名
def check_signature(msg_signature, timestamp, nonce, echostr):
    msg_crypt = WXBizJsonMsgCrypt(WEWORK_TOKEN, WEWORK_ENCODING_AES_KEY, WEWORK_CORPID)
    ret, sEchoStr = msg_crypt.VerifyURL(msg_signature, timestamp, nonce, echostr)
    return ret, sEchoStr


def select_msgs(cursor: str, token: str) -> List[WechatMsgEntity]:
    resp = requests.post(
        "https://qyapi.weixin.qq.com/cgi-bin/kf/sync_msg",
        params={
            "access_token": _cachable_token()
        },
        data=json.dumps(
            {
                "limit": 1000,
                "token": token
            }
        )
    )
    resp_data = resp.json()
    msgs = resp_data.get("msg_list", [])
    has_more = resp_data.get("has_more", 0)
    next_cursor = resp_data.get("next_cursor")
    msg_entities = [
        WechatMsgEntity(
            **{k: v for k, v in msg.items() if k not in ['open_kfid', 'external_userid']},
            open_kfid=msg.get('open_kfid', ''),
            external_userid=msg.get('external_userid', '')
        )
        for msg in msgs
    ]

    if next_cursor and has_more == 1:
        set_cursor(next_cursor)

    return msg_entities, has_more == 1, next_cursor


# 发送消息给用户
def send_text_msg(msg_id, external_user_id, kf_id, content):
    _send_msg(
        WechatMsgSendEntity(
            touser=external_user_id,
            open_kfid=kf_id,
            msgtype="text",
            text={
                "content": content
            }
        )
    )


def _send_msg(entity: WechatMsgSendEntity):
    payload = entity.model_dump_json()
    resp = requests.post(
        "https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg",
        params={
            "access_token": _cachable_token()
        },
        data=payload,
    )
    LOGGER.info(f"ok to send msg with resp: {resp.status_code}, content: {resp.content}")
    # LOGGER.info(f"ok to send msg with resp: {resp.status_code}, content: {resp.content}, payload: {payload}")
    return resp


'''
异步的发送消息函数
'''


# ✅ 修正后的异步发送函数：完全复用 Schema
async def async_send_text_msg(msg_id, external_user_id, kf_id, content):
    # 1. 使用 Pydantic 构建对象，这里会进行数据校验
    entity = WechatMsgSendEntity(
        touser=external_user_id,
        open_kfid=kf_id,
        msgtype="text",
        text={
            "content": content
        }
    )

    # 2. 调用底层的异步发送实现
    return await _async_send_msg(entity)


# ✅ 底层异步发送实现
async def _async_send_msg(entity: WechatMsgSendEntity):
    url = "https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg"

    # 获取 Token (Redis 操作通常很快，这里混用同步的 cached token 函数问题不大)
    # 如果追求极致，_cachable_token 最好也改成 async 的，但目前瓶颈主要在网络 IO
    token = _cachable_token()
    params = {"access_token": token}

    # 3. 序列化：使用 Pydantic 转为字典，让 httpx 处理 JSON 编码
    # 或者使用 entity.model_dump_json() 获取字符串然后传给 content 参数
    # 这里推荐用 json=entity.model_dump()，httpx 会自动处理 Content-Type
    payload = entity.model_dump()

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, params=params, json=payload)

            # 简单的日志记录
            if resp.status_code != 200:
                LOGGER.error(f"Async send failed: {resp.text}")
            else:
                LOGGER.info(f"Async send success: {resp.json().get('errmsg', 'ok')}")

            return resp
        except Exception as e:
            LOGGER.error(f"Async send exception: {e}")
            return None


# 定义 Token 在 Redis 中的 Key
REDIS_TOKEN_KEY = "wework:access_token"
TOKEN_TTL = 7000  # 微信有效期 7200秒，我们设短一点留余量


# 使用redis缓存token
def _cachable_token():
    """
    优先从 Redis 获取 Token，没有则从微信接口获取并存入 Redis
    """
    # 1. 尝试从 Redis 获取
    cached_token = REDIS_CLIENT.get(REDIS_TOKEN_KEY)

    if cached_token:
        # Redis 存的是 bytes，需要解码
        return cached_token.decode('utf-8')

    # 2. Redis 里没有（或已过期），重新向微信请求
    LOGGER.info("Redis中未找到有效Token，正在刷新...")
    new_token = _wework_token()

    if new_token:
        # 3. 存入 Redis 并设置过期时间 (自动过期，无需手动判断时间戳)
        REDIS_CLIENT.set(REDIS_TOKEN_KEY, new_token, ex=TOKEN_TTL)
        return new_token

    # 如果获取失败（虽然 _wework_token 会抛异常，这里兜底）
    return None


def _wework_token():
    # ... (保持原来的逻辑不变) ...
    response = requests.get(
        WEWORK_TOKEN_API,
        params={"corpid": WEWORK_CORPID, "corpsecret": WEWORK_CORPSECRET},
    )
    json_data = response.json()
    # 建议加个错误检查
    if json_data.get("errcode") != 0:
        LOGGER.error(f"获取Token失败: {json_data}")
        return None
    print(json_data)
    LOGGER.info(f"获取到新的 WeWork Access Token: {json_data['access_token']}")
    return json_data["access_token"]


# 假设你有一个获取当前 access_token 的函数
# from wework import get_token
def download_wechat_image(media_id: str, msg_id: str, access_token: str) -> str:
    """
    下载微信图片到本地 static 目录，并返回可访问的 HTTP URL
    """
    url = f"https://qyapi.weixin.qq.com/cgi-bin/media/get"
    params = {
        "access_token": access_token,
        "media_id": media_id
    }

    try:
        response = requests.get(url, params=params)
        response.raise_for_status()

        # 简单判断一下是否真的是图片（微信有时候会返回json错误）
        if "application/json" in response.headers.get("Content-Type", ""):
            LOGGER.error(f"下载图片失败，微信返回: {response.text}")
            return None

        # 保存图片，文件名使用 msgid 防止重复
        file_name = f"{msg_id}.jpg"
        file_path = os.path.join(TEMP_IMAGE_DIR, file_name)

        with open(file_path, "wb") as f:
            f.write(response.content)

        # 生成外部可访问的 URL
        public_url = f"{SERVER_BASE_URL}/{TEMP_IMAGE_DIR}/{file_name}"
        LOGGER.info(f"图片已转存: {public_url}")
        return public_url

    except Exception as e:
        LOGGER.error(f"图片下载异常: {e}")
        return None


# 解决图片消息的处理函数
def handle_image_msg(msg, token):
    """
    专门在线程中处理图片：获取Token -> 下载 -> 调用AI回复
    """
    try:
        # 1. 再次检查重试 (双重保险)
        # if get_msg_retry(msg.msgid) == "done": return

        media_id = msg.image.get('media_id')

        # 2. 获取 Token (耗时网络IO)
        api_access_token = _cachable_token()
        if not api_access_token:
            LOGGER.error("无法获取有效的 Access Token")
            return

        # 3. 下载图片 (耗时网络IO)
        image_url = download_wechat_image(media_id, msg.msgid, api_access_token)

        if image_url:
            # LOGGER.info(f"下载成功，开始调用Coze: {image_url}")
            # 4. 调用 AI 回复逻辑
            reply_msg(msg.msgid, msg.external_userid, msg.open_kfid, image_url)
        else:
            LOGGER.error(f"图片下载失败: {msg.msgid}")

    except Exception as e:
        LOGGER.error(f"图片异步处理异常: {e}")


def reply_msg(msgid: str, external_userid: str, open_kfid: str, content: str):
    if get_msg_retry(msgid) == b'0':
        return
    '''添加(修改)'''
    # 用户ID = external_userid
    user_id = external_userid
    LOGGER.info(f"[用户] {user_id} [用户消息] {content}")
    print("=" * 80, "Coze 智能体回复处理中", "=" * 80)

    # 每个用户绑定独立 conversation_id
    conversation_id = get_or_create_latest_conversation(user_id)

    # 调用 Coze 工作流
    reply_text = ai_reply_coze(
        content=content,
        user_id=user_id,
        conversation_id=conversation_id
    )
    print("=" * 80, "Coze 智能体回复完成", "=" * 80)
    send_text_msg(msgid, external_userid, open_kfid, reply_text)
    set_msg_retry(msgid, 0)


'''
异步的图片处理函数
'''


async def async_download_wechat_image(media_id: str, msg_id: str, access_token: str) -> str:
    """
    [异步版] 下载微信图片到本地 static 目录，并返回可访问的 HTTP URL
    """
    url = f"https://qyapi.weixin.qq.com/cgi-bin/media/get"
    params = {
        "access_token": access_token,
        "media_id": media_id
    }

    try:
        # ✅ 改动1: 使用 httpx 进行异步网络请求
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params)

            # httpx 的错误检查
            if response.status_code != 200:
                LOGGER.error(f"下载图片网络请求失败: {response.status_code}")
                return None

            # 简单判断 Content-Type (注意：httpx headers key 是不区分大小写的)
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                LOGGER.error(f"下载图片失败，微信返回不是图片: {response.text}")
                return None

            # 保存图片路径
            file_name = f"{msg_id}.jpg"
            file_path = os.path.join(TEMP_IMAGE_DIR, file_name)

            # ✅ 改动2: 文件写入是阻塞操作，必须扔到线程池里，否则会卡死整个 Event Loop
            # 使用 asyncio.to_thread (Python 3.9+) 将同步写入变为异步等待
            await asyncio.to_thread(_save_file_sync, file_path, response.content)

            # 生成外部可访问的 URL
            public_url = f"{SERVER_BASE_URL}/{TEMP_IMAGE_DIR}/{file_name}"
            LOGGER.info(f"图片已异步转存: {public_url}")
            return public_url

    except Exception as e:
        LOGGER.error(f"图片异步下载异常: {e}")
        return None


# 辅助同步函数：专门用于在线程池中写入文件
def _save_file_sync(path: str, content: bytes):
    with open(path, "wb") as f:
        f.write(content)


async def async_handle_image(msg):
    """
    [异步版] 专门在后台任务中处理图片：获取Token -> 下载 -> 调用AI回复
    """
    try:
        media_id = msg.image.get('media_id')

        # 1. 获取 Token
        # (Redis读取非常快，毫秒级，这里混用同步函数通常没问题)
        # (如果追求极致，也可以把 _cachable_token 改为 async)
        api_access_token = _cachable_token()

        if not api_access_token:
            LOGGER.error("无法获取有效的 Access Token")
            return

        # 2. ✅ 异步下载图片 (释放 CPU 给其他请求)
        image_url = await async_download_wechat_image(media_id, msg.msgid, api_access_token)

        if image_url:
            LOGGER.info(f"下载成功，准备调用回复: {image_url}")

            # 3. ✅ 调用异步回复函数 (async_reply_msg 必须已经是 async def)
            # 注意：这里调用的是上一轮修改过的 async_reply_msg
            # content 参数传入提示语，image_url 传给 AI 进行分析（如果 AI 支持）
            await async_reply_msg(
                msgid=msg.msgid,
                external_userid=msg.external_userid,
                open_kfid=msg.open_kfid,
                content=image_url  # 这里你可以决定是传 URL 还是传 "用户发送了一张图片"
            )
        else:
            LOGGER.error(f"图片下载失败: {msg.msgid}")

    except Exception as e:
        LOGGER.error(f"图片异步处理异常: {e}")


# ✅ 修改后的异步函数
async def async_reply_msg(msgid: str, external_userid: str, open_kfid: str, content: str):
    # 1. 这里的判断逻辑保留您的写法
    # 注意：get_msg_retry 是同步 Redis 操作，速度很快，这里暂时不用改异步
    if get_msg_retry(msgid) == b'0':
        return

    # =========================================================
    # ✅ 步骤 A: 身份转换 (External ID -> Internal ID)
    # =========================================================
    try:
        # 将同步的映射逻辑放入线程池运行
        internal_user_id = await asyncio.to_thread(get_or_create_internal_user, external_userid)
    except Exception as e:
        LOGGER.error(f"无法获取内部用户ID，停止处理: {e}")
        return

    if not internal_user_id:
        return

    LOGGER.info(f"[映射] ExtID:{external_userid} -> IntID:{internal_user_id}")
    LOGGER.info(f"[消息] 用户:{internal_user_id} 内容:{content}")
    print("=" * 80, "Coze 智能体回复处理中", "=" * 80)

    # =========================================================
    # ✅ 步骤 B: 获取会话 (传入 Internal ID)
    # =========================================================
    try:
        conversation_id = await asyncio.to_thread(get_or_create_latest_conversation, internal_user_id, open_kfid)
    except Exception as e:
        LOGGER.error(f"获取/创建会话ID失败: {e}")
        # 如果获取会话失败，可以选择 return 或者赋一个 None 继续尝试
        conversation_id = None

    # =========================================================
    # ✅ 步骤 C: 调用 AI (传入 Internal ID)
    # =========================================================
    # Coze 里的 user_id 参数现在是 "user_xxxx"，这很好，Coze 就能认出同一个用户
    reply_text = await async_ai_reply_coze(
        content=content,
        user_id=internal_user_id,
        conversation_id=conversation_id,
        open_kfid=open_kfid
    )

    print("=" * 80, "Coze 智能体回复完成", "=" * 80)

    # =========================================================
    # ✅ 步骤 D: 发送消息 (⚠️ 必须使用 External ID)
    # =========================================================
    # 发给微信接口时，微信只认 external_userid，千万别传内部 ID 过去
    await async_send_text_msg(msgid, external_userid, open_kfid, reply_text)

    # 5. 更新状态
    set_msg_retry(msgid, 0)