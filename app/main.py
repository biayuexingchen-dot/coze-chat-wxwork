from concurrent.futures import ThreadPoolExecutor
import time
import json
from fastapi import Depends, FastAPI, Request, HTTPException
from fastapi import BackgroundTasks  # 引入后台任务
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Generator
from ai import ai_reply, ai_reply_coze, async_ai_reply_coze
from config import LOGGER, WEWORK_CORPID, WEWORK_ENCODING_AES_KEY, WEWORK_TOKEN
from kv import get_cursor, get_msg_retry, set_msg_retry
from schema import WeChatMessage, WeChatTokenMessage, WechatMsgEntity, WechatMsgSendEntity
from util.wx_biz_json_msg_crypt import WXBizJsonMsgCrypt
from wework import check_signature, parse_wechat_message, select_msgs, send_text_msg, download_wechat_image, \
    _cachable_token, handle_image_msg
from wework import async_send_text_msg, async_handle_image
from call_coze_api import get_or_create_latest_conversation, call_coze_workflow, get_or_create_internal_user, \
    async_call_coze_workflow
import asyncio

# thread_pool = ThreadPoolExecutor(max_workers=5)  # 创建一个线程池，最大工作线程数为5

app = FastAPI(title="Wxwork Coze Chat API", version="1.0")
# 允许 WebUI 来源
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载静态目录
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=FileResponse)
async def root():
    return FileResponse("static/index.html")


@app.get("/ping")
async def ping():
    return {"message": "pong"}


'''
open-webui的相关API配置
'''


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": "wxwork-coze-chat",  # 模型名称（你随便定义）
                "object": "model",
                "owned_by": "owner"
            }
        ]
    }


class Message(BaseModel):
    role: str  # "system" / "user" / "assistant"
    content: str


class ChatRequest(BaseModel):
    model: Optional[str] = "wxwork-coze-chat"
    messages: List[Message]
    stream: Optional[bool] = False
    temperature: Optional[float] = 0.7


@app.post("/v1/chat/completions")
async def openai_chat(req: dict):
    """
    [异步版] 处理 Open-WebUI 请求
    """
    # 1. 解析请求
    messages = req.get("messages", [])
    if not messages:
        return {"error": "No messages provided"}

    user_message = messages[-1]["content"]

    # ⚠️ 关于 User ID 的建议：
    # WebUI 通常在 header 里不传真实用户ID。
    # 建议这里不要写死，而是结合 req 中的信息或者生成一个固定的 WebUI 专用 ID
    # 这里沿用你写的 ID，但在实际生产中建议区分不同 WebUI 用户
    user_id = "user_XXXXXXXXXX"
    DEFAULT_WEBUI_KFID = "wkx_XXXXXXXXXX"

    # 2. 处理特殊指令 (WebUI 的建议后续问题逻辑)
    if user_message.startswith("### Task:"):
        return {
            "id": f"follow-up-task-{int(time.time())}",
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"follow_ups": []})
                },
                "finish_reason": "stop"
            }]
        }

    LOGGER.info(f"[WebUI] 用户: {user_id} 提问: {user_message}")

    # =================================================================
    # 3. ✅ 异步优化：获取会话 ID (数据库操作放入线程池)
    # =================================================================
    try:
        # 注意：这里我们传入默认的 KFID，以获取对应的配置和记录
        conversation_id = await asyncio.to_thread(
            get_or_create_latest_conversation,
            user_id,
            DEFAULT_WEBUI_KFID
        )
    except Exception as e:
        LOGGER.error(f"[WebUI] 获取会话失败: {e}")
        return create_openai_error_response("Database Error")

    if not conversation_id:
        return create_openai_error_response("Failed to create conversation")

    # =================================================================
    # 4. ✅ 异步优化：调用 Coze (使用之前写好的异步函数)
    # =================================================================
    # 使用 await 调用 async_call_coze_workflow，释放 Event Loop
    assistant_reply = await async_call_coze_workflow(
        user_id=user_id,
        conversation_id=conversation_id,
        questions=user_message,
        open_kfid=DEFAULT_WEBUI_KFID  # 传入默认配置ID
    )

    if not assistant_reply:
        reply = "❌ 服务器异常，Coze 无响应"
    else:
        reply = assistant_reply

    # 5. 构造 OpenAI 格式响应
    return {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.get("model", "wxwork-coze-chat"),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": reply
                },
                "finish_reason": "stop"
            }
        ],
        "usage": {  # 选填，Open WebUI 有时会显示 token 消耗
            "prompt_tokens": len(user_message),
            "completion_tokens": len(reply),
            "total_tokens": len(user_message) + len(reply)
        }
    }


def create_openai_error_response(msg: str):
    """辅助函数：返回 OpenAI 格式的错误"""
    return {
        "id": "error",
        "choices": [{
            "message": {"role": "assistant", "content": f"Error: {msg}"},
            "finish_reason": "stop"
        }]
    }


'''
微信客服的相关API配置
'''


@app.get("/wechat/hook")
async def wechat_hook_verification(
        msg_signature: str, timestamp: str, nonce: str, echostr: str
):
    ret, sEchoStr = check_signature(msg_signature, timestamp, nonce, echostr)
    if ret == 0:
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(
            content=sEchoStr, media_type="text/plain;charset=utf-8"
        )
    else:
        return JSONResponse(content={"error": "Verification failed"}, status_code=400)


@app.post("/wechat/hook")
async def wechat_hook_event(
        msg_signature: str, timestamp: str, nonce: str,
        background_tasks: BackgroundTasks,  # ✅ 注入后台任务对象
        message: WeChatMessage = Depends(parse_wechat_message)
):
    LOGGER.info("Received WeChat message: %s", message)
    msg_crypt = WXBizJsonMsgCrypt(WEWORK_TOKEN, WEWORK_ENCODING_AES_KEY, WEWORK_CORPID)
    ret, xml_content = msg_crypt.DecryptMsg(
        message.Encrypt,
        msg_signature,
        timestamp,
        nonce
    )
    token_msg = WeChatTokenMessage.from_xml(xml_str=xml_content)
    LOGGER.info(f"Received WeChat token message: {token_msg.model_dump_json()}")
    cursor = get_cursor()
    # ✅ 传递 background_tasks 进去
    process_msg(token_msg.Token, cursor, background_tasks)
    return JSONResponse(content={"message": "Event received"})


def process_msg(token: str, cursor: str, background_tasks: BackgroundTasks):
    msg_entities, has_more, next_cursor = select_msgs(cursor=cursor, token=token)
    last_5 = msg_entities[-5:] if len(msg_entities) >= 5 else msg_entities
    for msg in last_5:
        # ---------------------------------------------------------
        # ✅ 修改点 1: 立即进行去重判断与标记
        # ---------------------------------------------------------
        if get_msg_retry(msg.msgid):
            LOGGER.debug(f"消息已处理过，跳过: msgid={msg.msgid}")
            continue

        # ⚡️ 核心：一旦决定处理，立刻标记！封死重试的空窗期。
        # 这里设置为 int(time.time()) 只要是非0值即可，代表“正在处理/已处理”
        set_msg_retry(msg.msgid, int(time.time()))

        # 获取消息类型
        msg_type = msg.msgtype

        # ==========================================
        # CASE 1: 处理文本消息
        # ==========================================
        if msg_type == 'text':
            # 只要判断 msg.text 是否非空，以及里面是否有 content
            if msg.text and msg.text.get('content'):
                content = msg.text.get('content')
                LOGGER.info(f"收到文本消息: msgid={msg.msgid}, content={content}")
                # thread_pool.submit(reply_msg, msg.msgid, msg.external_userid, msg.open_kfid, content)
                # ✅ 关键修改：添加到 FastAPI 后台任务队列，而不是线程池
                # 注意：这里调用的函数必须是 async 的，或者 FastAPI 会自动在线程池运行它
                background_tasks.add_task(async_reply_msg, msg.msgid, msg.external_userid, msg.open_kfid, content)

        # ==========================================
        # CASE 2: 处理图片消息
        # ==========================================
        elif msg_type == 'image':
            # print(msg) # 调试完可以注释掉，避免日志过多
            # ✅ 优化点：直接判断 msg.image 即可，不需要 hasattr 了
            if msg.image and msg.image.get('media_id'):
                media_id = msg.image.get('media_id')
                LOGGER.info(f"收到图片消息: msgid={msg.msgid}, media_id={media_id}")
                # ✅ 修改点 2: 不要在这里下载！直接提交给线程池
                # 将 耗时的“获取Token” 和 “下载图片” 都移出主线程
                # thread_pool.submit(handle_image_msg, msg, token)
                background_tasks.add_task(async_handle_image, msg)

        # ==========================================
        # CASE 3: 其他类型
        # ==========================================
        else:
            LOGGER.info(f"Skipping unsupported message type: msgid={msg.msgid}, msgtype={msg_type}")
            continue


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
        LOGGER.error(f"internal_user_id = {internal_user_id} ，映射失败，停止处理: external_userid={external_userid}")
        return

    LOGGER.info(f"[映射] ExtID：{external_userid} -> IntID：{internal_user_id}")
    LOGGER.info(f"[消息] 用户：{internal_user_id} 内容：{content}")
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


'''
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
gunicorn main:app -w 9 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 --timeout 120
'''

# if __name__ == "__main__":
#     import uvicorn

#     uvicorn.run(app, host="0.0.0.0", port=8000)
