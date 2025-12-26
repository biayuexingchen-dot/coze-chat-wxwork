import json
import timeit
import httpx
import requests
from dotenv import load_dotenv
import os
import asyncio
import time
from database_operation import get_conversations_by_user, create_conversation, create_message, \
    get_conversations_by_user_and_open_kfid, get_user_by_external_id, create_user
from config import get_coze_config, generate_internal_uid, REDIS_CLIENT, LOGGER


def init_config():
    env_path = "./.env"
    load_dotenv(dotenv_path=env_path)
    config = {
        "token": "Bearer " + os.getenv("COZE_PAT", "").strip(),
        "workflow_id": os.getenv("COZE_WORKFLOW_ID", "7522357917102800930"),
        "app_id": os.getenv("COZE_APP_ID", "7522316251134771240")
    }
    # 校验配置完整性
    missing = [k for k, v in config.items() if not v]
    if missing:
        raise ValueError(f"❌ 缺少关键配置项: {', '.join(missing)}")
    print("🚀 初始化Coze环境变量")
    return config


def create_conversation_cozeAPI(conversation_name, open_kfid=None):
    if open_kfid:
        # ✅ 关键点：根据 open_kfid 动态获取配置
        config = get_coze_config(open_kfid)
    else:
        config = init_config()
    '''
    会话 -> 创建会话
    '''
    headers = {
        'Authorization': config.get('token', ''),
        'Content-Type': 'application/json',
    }

    json_data = {
        'name': conversation_name
    }

    response = requests.post('https://api.coze.cn/v1/conversation/create', headers=headers, json=json_data, timeout=60)
    if response.status_code != 200:
        print("❌ 创建会话失败，状态码:", response.status_code)
        print("响应内容:", response.text)
        return None

    info = response.json()
    if "data" not in info or "id" not in info["data"]:
        print("❌ 响应格式异常:", info)
        return None
    conversation_id = info["data"]["id"]
    print("✅ 新会话创建成功，会话ID:", conversation_id)
    return conversation_id


def insert_new_conversation(user_id, new_conversation_id, open_kfid=None):
    conv_data = {
        "conversation_id": new_conversation_id,
        "user_id": user_id,
        "user_device_id": None,
        "conversation_name": user_id,
        "comments": None,
        "open_kfid": open_kfid
    }
    new_conv = create_conversation(conv_data)
    if open_kfid:
        print(f"✅ 新会话创建成功: {new_conv.conversation_id} 对应用户🐧 ：{new_conv.user_id} 客服ID💬 ：{open_kfid}")
    else:
        print(f"✅ 新会话创建成功: {new_conv.conversation_id} 对应用户🐧 ：{new_conv.user_id} 客服ID💬  ：【默认】")


def insert_new_message(user_latest_question, bot_reply, user_id, conversation_id):
    msg_data = {
        'user_question': user_latest_question,
        'bot_reply': bot_reply,
        'user_id': user_id,
        "user_device_id": None,
        'conversation_id': conversation_id,
        "comments": None
    }
    new_message = create_message(msg_data)
    print(f"✅ 新消息创建成功: {new_message.id} 对应问题：{new_message.user_question}")


# 根据企微外部用户ID external_userid 获取或创建内部 user_id。
def get_or_create_internal_user(external_userid: str) -> str:
    """
    根据企微 external_userid 获取或创建内部 user_id。

    流程: Redis缓存 -> DB查询 -> (无则)创建 -> 写回缓存
    特性: 包含了并发注册时的冲突处理机制
    """
    if not external_userid:
        return None

    # =======================================================
    # 1. 查 Redis 缓存 (高性能的一级屏障)
    # =======================================================
    cache_key = f"map:ext_uid:{external_userid}"
    try:
        cached_id = REDIS_CLIENT.get(cache_key)
        if cached_id:
            # LOGGER.debug(f"用户映射命中缓存: {external_userid} -> {cached_id.decode('utf-8')}")
            LOGGER.info(f"⚡ 用户映射命中缓存: ExtID:{external_userid} -> IntID:{cached_id.decode('utf-8')}")
            return cached_id.decode('utf-8')
    except Exception as e:
        LOGGER.error(f"Redis 读取失败: {e}")
        # Redis 挂了不应阻断流程，继续查 DB

    # =======================================================
    # 2. 查数据库 (调用封装好的 CRUD)
    # =======================================================
    try:
        user = get_user_by_external_id(external_userid)

        if user:
            LOGGER.info(f"🐬 用户映射命中数据库: ExtID:{external_userid} -> IntID:{user.user_id}")
            internal_id = user.user_id
        else:
            # =======================================================
            # 3. 注册新用户 (处理并发冲突)
            # =======================================================
            LOGGER.info(f"🆕 检测到新用户，准备注册: 企微外部联系人ID: {external_userid}")

            new_internal_id = generate_internal_uid()  # 生成 user_xxx

            user_data = {
                "user_id": new_internal_id,
                "wechat_external_userid": external_userid,
                # "created_at": ... (数据库会自动处理)
            }

            try:
                # 尝试创建用户
                new_user = create_user(user_data)
                internal_id = new_user.user_id
                LOGGER.info(f"✅ 新用户注册成功: {internal_id}")

            except Exception as e:
                # ⚠️ 生产级并发处理：
                # 如果两个请求同时进来，A和B都发现用户不存在。
                # A创建成功了，B再创建时会因为 wechat_external_userid 唯一索引冲突报错。
                # 此时 B 应该重新去查一次数据库，而不是直接报错。
                LOGGER.warning(f"用户创建出现竞争或异常，尝试重新查询: {e}")

                # 二次查询 (Double Check)
                retry_user = get_user_by_external_id(external_userid)
                if retry_user:
                    internal_id = retry_user.user_id
                    LOGGER.info(f"✅ 二次查询找回用户: {internal_id}")
                else:
                    # 如果还是查不到，说明是真的数据库出问题了
                    LOGGER.error(f"❌ 用户注册彻底失败: {external_userid}")
                    raise e

        # =======================================================
        # 4. 写入 Redis 缓存
        # =======================================================
        try:
            # 过期时间设为 7 天 (604800秒)，热门用户会一直命中缓存
            REDIS_CLIENT.set(cache_key, internal_id, ex=604800)

        except Exception as e:
            LOGGER.error(f"Redis 写入失败: {e}")

        return internal_id

    except Exception as e:
        LOGGER.error(f"❌ 用户映射服务严重异常: {e}")
        # 这里的 raise 会被上层的 asyncio.to_thread 捕获
        raise e


# 获取或创建用户的最新会话
def get_or_create_latest_conversation(user_id, open_kfid=None):
    """
    user_id: 要查询的用户ID
    open_kfid: 企微客服账号ID，用于选择Coze配置
    """
    # 查询该用户是否已有会话
    if open_kfid:
        conversations = get_conversations_by_user_and_open_kfid(user_id, open_kfid)
        print(f"👤 用户ID：{user_id}，🙋 客服ID：{open_kfid}")
    else:
        conversations = get_conversations_by_user(user_id)
        print(f"👤 用户ID：{user_id}，🙋 客服ID：【默认】")
    # 有会话则返回最新一条的会话ID
    if conversations:
        # 返回最新一条会话的 ID
        conversation_id = conversations[0].conversation_id
        print(f"✅ 已找到用户ID：{user_id} 的最新会话：{conversation_id}")
    else:
        print(f"⚠️  该用户({user_id})没有会话，尝试创建新的会话...")
        new_conversation_id = create_conversation_cozeAPI(user_id, open_kfid)
        if new_conversation_id:
            conv_data = {
                "conversation_id": new_conversation_id,
                "user_id": user_id,
                "user_device_id": None,
                "conversation_name": user_id,
                "comments": None,
                "open_kfid": open_kfid
            }
            new_conv = create_conversation(conv_data)
            if open_kfid:
                print(f"✅ 新会话创建成功: {new_conv.conversation_id} 对应用户🧑 {new_conv.user_id} 客服ID🎧 {open_kfid}")
            else:
                print(f"✅ 新会话创建成功: {new_conv.conversation_id} 对应用户🧑 {new_conv.user_id} 客服ID🎧 【默认】")
            conversation_id = new_conv.conversation_id
        else:
            print("❌ 新会话创建失败")
            conversation_id = None
    return conversation_id


# 异常问题判断和解决
def error_judge_handling(error_code, error_msg, response, user_id, headers, json_data, conversation_id):
    assistant_reply = ''
    if error_msg:
        if error_code == 4002:
            print(f"⚠️ 会话：「{conversation_id}」 失效，尝试创建新的会话...")
            # 重新创建新的会话
            new_conversation_id = create_conversation_cozeAPI(user_id)
            if new_conversation_id:
                insert_new_conversation(user_id, new_conversation_id)
                json_data['conversation_id'] = new_conversation_id
                start = timeit.default_timer()
                response = requests.post('https://api.coze.cn/v1/workflows/chat', headers=headers, json=json_data,
                                         timeout=60)
                end = timeit.default_timer()
                print(f"⏳ Coze API二次调用耗时: {end - start:.2f}s")
                if response.status_code != 200:
                    print(f"❌ 请求失败：{response.status_code}")
                    print("❌ 响应内容：", response.text)
                else:
                    for line in response.iter_lines(decode_unicode=True):
                        if line.startswith("data:"):
                            data_str_retry = line[5:].strip()
                            try:
                                data_json_retry = json.loads(data_str_retry)
                                # 检查是否为错误信息
                                if "msg" in data_json_retry and "code" in data_json_retry:
                                    error_code = data_json_retry.get("code")
                                    error_msg = data_json_retry.get("msg")
                                    print(f"❌ [错误代码 {error_code}] [错误信息 {error_msg}]")
                                    break
                                # 检查是否为 assistant 回复
                                elif data_json_retry.get("role") == "assistant" and "content" in data_json_retry:
                                    assistant_reply = data_json_retry["content"].strip()
                                    break
                            except json.JSONDecodeError:
                                continue
        else:
            print(f"❌ [错误代码 {error_code}] [错误信息 {error_msg}]")
        return assistant_reply
    elif assistant_reply:
        return assistant_reply
    else:
        try:
            error_info = json.loads(response.text)
            if "msg" in error_info and "code" in error_info:
                error_msg = error_info.get("msg")
                error_code = error_info.get("code")
                print(f"❌ [错误代码 {error_code}] [错误信息 {error_msg}]")
        except json.JSONDecodeError:
            print("❌ 未检测到助手回复或错误信息")
        return assistant_reply


def call_coze_workflow(user_id, conversation_id, questions):
    config = init_config()
    """
    调用Coze API
    """
    headers = {
        'Authorization': config.get('token', ''),
        'Content-Type': 'application/json',
    }
    json_data = {
        'additional_messages': [],
        'parameters': {
            'user_id': user_id
        },
        'app_id': config.get('app_id', ''),
        'workflow_id': config.get('workflow_id', ''),
        'conversation_id': conversation_id,
    }
    # 根据questions类型构建相应的json_data
    user_latest_question = None
    if isinstance(questions, str) or isinstance(questions, int) or isinstance(questions, float):
        json_data['additional_messages'] = [
            {
                'content_type': 'text',
                'role': 'user',
                'content': questions
            }
        ]
        user_latest_question = questions
    elif isinstance(questions, list):
        if len(questions) == 0:
            print("❌ 问题列表为空，请输入问题")
            return ""
        elif len(questions) == 1:
            json_data['additional_messages'] = [
                {
                    'content_type': 'text',
                    'role': 'user',
                    'content': questions[0]
                }
            ]
            user_latest_question = questions[0]
        else:
            json_data['additional_messages'] = [
                {
                    'content_type': 'text',
                    'role': 'user',
                    'content': msg
                }
                for msg in questions
            ]
            user_latest_question = questions[-1]

    else:
        print("❌ 请输入问题字符串或问题列表")
        return ""

    if conversation_id:
        try:
            start = timeit.default_timer()
            response = requests.post('https://api.coze.cn/v1/workflows/chat', headers=headers, json=json_data,
                                     timeout=60)
            end = timeit.default_timer()
            print(f"⏳ Coze API 响应耗时: {end - start:.2f}s")

            if response.status_code != 200:
                try:
                    error_info_json = json.loads(response.text)
                    if "msg" in error_info_json and "code" in error_info_json:
                        error_msg = error_info_json.get("msg")
                        error_code = error_info_json.get("code")
                        print(f"❌ ❌ ❌ [错误代码 {error_code}] [错误信息 {error_msg}]")
                    return ""
                except json.JSONDecodeError:
                    print(f"❌ ❌ ❌ 请求失败：{response.status_code}")
                    print("❌ ❌ ❌ 响应内容：", response.text)
                    return ""

            response.encoding = 'utf-8'
            assistant_reply = ""
            error_msg = None
            error_code = None
            # print(response.text)

            for line in response.iter_lines(decode_unicode=True):
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    try:
                        data_json = json.loads(data_str)
                        # 检查是否为 assistant 回复
                        if data_json.get("role") == "assistant" and "content" in data_json:
                            assistant_reply = data_json["content"].strip()
                            break
                        # 检查是否为错误信息
                        elif "msg" in data_json and "code" in data_json:
                            error_msg = data_json.get("msg")
                            error_code = data_json.get("code")
                            break
                    except json.JSONDecodeError:
                        continue

            if assistant_reply:
                insert_new_message(user_latest_question, assistant_reply, user_id, conversation_id)
                print("🤖 bot回复：", assistant_reply)
                return assistant_reply
            else:
                error_reply = error_judge_handling(error_code, error_msg, response, user_id, headers, json_data,
                                                   conversation_id)
                if error_reply:
                    insert_new_message(user_latest_question, error_reply, user_id, conversation_id)
                    print("🤖 bot二次请求回复：", error_reply)
                return error_reply
        except requests.RequestException as e:
            print("❌ 网络异常：", e)
            return ""
    else:
        print("❌ 未检测到会话ID")
        return ""


'''
异步的错误处理和Coze工作流调用
'''


async def async_error_judge_handling(error_code, error_msg, user_id, headers, json_data, conversation_id, open_kfid):
    """
    [异步版] 错误处理与重试逻辑
    """
    assistant_reply = ''
    if error_msg:
        # -------------------------------------------------------------
        # Case 1: 会话失效 (4002)，尝试新建会话并重试
        # -------------------------------------------------------------
        if error_code == 4002:
            print(f"⚠️ 会话：「{conversation_id}」 失效，尝试创建新的会话...")
            # 1. 创建新会话
            # ✅ 优化：将同步的创建会话操作放入线程池，避免阻塞主循环
            try:
                new_conversation_id = await asyncio.to_thread(create_conversation_cozeAPI, user_id, open_kfid)
            except Exception as e:
                print(f"❌ 创建会话异常: {e}")
                new_conversation_id = None
            # new_conversation_id = create_conversation_cozeAPI(user_id)
            if new_conversation_id:
                # ✅ 优化：数据库写入放入线程池
                try:
                    await asyncio.to_thread(insert_new_conversation, user_id, new_conversation_id, open_kfid)
                except Exception as e:
                    print(f"❌ 数据库写入异常【insert_new_conversation】: {e}")
                # insert_new_conversation(user_id, new_conversation_id)
                # 更新请求体中的 conversation_id
                json_data['conversation_id'] = new_conversation_id

                # 2. 发起二次请求 (异步 httpx)
                try:
                    start = timeit.default_timer()
                    timeout = httpx.Timeout(60.0, connect=10.0)
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        async with client.stream('POST', 'https://api.coze.cn/v1/workflows/chat', headers=headers,
                                                 json=json_data) as response:
                            if response.status_code != 200:
                                resp_text = await response.aread()
                                print(f"❌ [重试] 请求失败：{response.status_code}")
                                print(f"❌ [重试] 响应内容：{resp_text.decode('utf-8')}")
                            else:
                                # 异步解析流式数据
                                async for line in response.aiter_lines():
                                    if line.startswith("data:"):
                                        data_str = line[5:].strip()
                                        try:
                                            data_json = json.loads(data_str)
                                            # 检查是否为 assistant 回复
                                            if data_json.get("role") == "assistant" and "content" in data_json:
                                                assistant_reply = data_json["content"].strip()
                                                break
                                            # 检查是否依然报错
                                            elif "msg" in data_json and "code" in data_json:
                                                e_code = data_json.get("code")
                                                e_msg = data_json.get("msg")
                                                print(f"❌ [重试失败] [错误代码:{e_code}] [错误信息:{e_msg}]")
                                                break
                                        except json.JSONDecodeError:
                                            continue

                            end = timeit.default_timer()
                            print(f"⏳ [重试] Coze API调用耗时: {end - start:.2f}s")

                except Exception as e:
                    print(f"❌ [重试] 网络异常: {e}")
            else:
                print("❌ 创建新会话失败，无法重试")
        else:
            print(f"❌ [错误代码 {error_code}] [错误信息 {error_msg}]")
        return assistant_reply
    else:
        print("❌ 未知错误：未检测到回复，也未检测到明确错误码。")
        return assistant_reply


async def async_call_coze_workflow(user_id, conversation_id, questions, open_kfid):
    # ✅ 关键点：根据 open_kfid 动态获取配置
    config = get_coze_config(open_kfid)
    """
    调用Coze API (异步版)
    """
    headers = {
        'Authorization': config.get('token', ''),
        'Content-Type': 'application/json',
    }
    json_data = {
        'additional_messages': [],
        'parameters': {
            'user_id': user_id
        },
        'app_id': config.get('app_id', ''),
        'workflow_id': config.get('workflow_id', ''),
        'conversation_id': conversation_id,
    }

    # --- 构建消息体逻辑 (保持不变) ---
    user_latest_question = None
    if isinstance(questions, (str, int, float)):
        json_data['additional_messages'] = [
            {
                'content_type': 'text',
                'role': 'user',
                'content': str(questions)
            }
        ]
        user_latest_question = str(questions)
    elif isinstance(questions, list):
        if len(questions) == 0:
            print("❌ 问题列表为空，请输入问题")
            return ""
        elif len(questions) == 1:
            json_data['additional_messages'] = [
                {'content_type': 'text', 'role': 'user', 'content': questions[0]}
            ]
            user_latest_question = questions[0]
        else:
            json_data['additional_messages'] = [
                {'content_type': 'text', 'role': 'user', 'content': msg}
                for msg in questions
            ]
            user_latest_question = questions[-1]
    else:
        print("❌ 请输入问题字符串或问题列表")
        return ""

    if not conversation_id:
        print("❌ 未检测到会话ID")
        return ""

    # --- ✅ 核心修改：使用 httpx 异步请求 ---
    try:
        # [1] 计时开始
        start_time = timeit.default_timer()

        # 设置超时：连接10秒，读取60秒
        timeout = httpx.Timeout(60.0, connect=10.0)

        async with httpx.AsyncClient(timeout=timeout) as client:
            # 使用 stream=True 处理流式响应 (SSE)
            # 注意：API 地址保持不变
            async with client.stream('POST', 'https://api.coze.cn/v1/workflows/chat', headers=headers,
                                     json=json_data) as response:

                # [2] 这里测量的是“连接耗时” (TTFB)
                # ttfb_time = timeit.default_timer()
                # print(f"⚡️ Coze 连接建立耗时: {ttfb_time - start_time:.2f}s")

                # 1. 处理 HTTP 错误状态码
                if response.status_code != 200:
                    # 获取完整响应内容
                    response_text = await response.aread()
                    try:
                        error_info_json = json.loads(response_text)
                        if "msg" in error_info_json and "code" in error_info_json:
                            error_msg = error_info_json.get("msg")
                            error_code = error_info_json.get("code")
                            print(f"❌ ❌ ❌ [错误代码 {error_code}] [错误信息 {error_msg}]")
                        return ""
                    except json.JSONDecodeError:
                        print(f"❌ ❌ ❌ 请求失败：{response.status_code}")
                        print("❌ ❌ ❌ 响应内容：", response_text.decode('utf-8'))
                        return ""

                # 2. 处理流式数据
                assistant_reply = ""
                error_msg = None
                error_code = None

                # ✅ 使用 aiter_lines 异步迭代行
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        try:
                            data_json = json.loads(data_str)

                            # 检查是否为 assistant 回复
                            if data_json.get("role") == "assistant" and "content" in data_json:
                                assistant_reply = data_json["content"].strip()
                                # 找到回复后，通常可以 break，除非你需要拼接流
                                # 如果 Coze 返回的是全量数据，break 即可；如果是 token 流，需要拼接
                                # 根据你之前的代码逻辑，看起来是直接取 content，假定是一次性返回或最后一条
                                break

                                # 检查是否为错误信息
                            elif "msg" in data_json and "code" in data_json:
                                error_msg = data_json.get("msg")
                                error_code = data_json.get("code")
                                break

                        except json.JSONDecodeError:
                            continue

                # [3] 循环结束后，才是真正的“总耗时”
                end_time = timeit.default_timer()
                total_duration = end_time - start_time
                print(f"⏳ Coze API 响应耗时: {total_duration:.2f}s")
                # 3. 处理结果
                if assistant_reply:
                    # ✅ 优化：数据库写入放入线程池，彻底解放 Event Loop
                    try:
                        await asyncio.to_thread(insert_new_message, user_latest_question, assistant_reply, user_id,
                                                conversation_id)
                    except Exception as e:
                        print(f"❌ 数据库写入异常【insert_new_message】: {e}")  # 记录日志但不影响回复用户
                    # insert_new_message(user_latest_question, assistant_reply, user_id, conversation_id)
                    print("🤖 bot回复：", assistant_reply)
                    return assistant_reply
                else:
                    # ⚠️ 注意：如果 error_judge_handling 内部使用了 response.json() 等同步方法，可能会报错
                    # 这里我们传入了 httpx 的 response 对象，需确保 helper 函数兼容
                    # 或者我们在这里读取完 body 再传进去
                    # 简单起见，这里假设 logic 还能复用
                    error_reply = await async_error_judge_handling(
                        error_code, error_msg, user_id, headers, json_data, conversation_id, open_kfid
                    )
                    if error_reply:
                        # ✅ 优化：数据库写入放入线程池
                        try:
                            await asyncio.to_thread(insert_new_message, user_latest_question, error_reply, user_id,
                                                    conversation_id)
                        except Exception as e:
                            print(f"❌ 数据库写入异常【insert_new_message】: {e}")
                        # insert_new_message(user_latest_question, error_reply, user_id, conversation_id)
                        print("🤖 bot二次请求回复：", error_reply)
                    return error_reply

    except httpx.RequestError as e:
        print(f"❌ 网络异常：{e}")
        return ""
    except Exception as e:
        print(f"❌ 未知异常：{e}")
        return ""
