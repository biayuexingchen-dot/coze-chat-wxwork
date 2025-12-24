from openai import OpenAI

from call_coze_api import call_coze_workflow,async_call_coze_workflow
from config import OPENAI_API_KEY

client = OpenAI(
    api_key=OPENAI_API_KEY,
    base_url="https://openai.apifast.org/v1"
)


def ai_reply(content):
    response = client.chat.completions.create(
        model="gpt-4o-mini",  # 或其他适合的模型
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": content}
        ]
    )
    return response.choices[0].message.content


def ai_reply_coze(content: str, user_id: str, conversation_id: str):
    """
    用 Coze Workflow 替代 OpenAI 调用
    content(questions): 用户消息
    user_id: 企微 external_userid
    conversation_id: 对话ID（如果你有多轮上下文）
    """
    if conversation_id:
        assistant_reply = call_coze_workflow(
            user_id=user_id,
            conversation_id=conversation_id,
            questions=content
        )
        if assistant_reply:
            reply = assistant_reply
        else:
            reply = "❌ 服务器异常，请稍后再试"
    else:
        reply = "❌ 服务器异常，请稍后再试"

    return reply

async def async_ai_reply_coze(content: str, user_id: str, conversation_id: str, open_kfid: str):
    """
    用 Coze Workflow 替代 OpenAI 调用 (异步版)
    """
    if conversation_id:
        # ✅ 添加 await
        assistant_reply = await async_call_coze_workflow(
            user_id=user_id,
            conversation_id=conversation_id,
            questions=content,
            open_kfid=open_kfid
        )
        if assistant_reply:
            reply = assistant_reply
        else:
            reply = "❌ 服务器异常，请稍后再试"
    else:
        reply = "❌ 服务器异常，请稍后再试"

    return reply