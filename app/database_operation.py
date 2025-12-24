from sqlalchemy import Column, String, BigInteger, Text, Integer, DateTime, ForeignKey, func, create_engine
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.dialects.mysql import LONGTEXT  # 👈 关键：引入 MySQL 专用类型
from config import generate_internal_uid, LOGGER
import os
from urllib.parse import quote_plus  # 用于处理密码中的特殊符号

Base = declarative_base()


# ================= ✅ 新增 User 表 =================
class User(Base):
    __tablename__ = 'user'

    user_id = Column(String(64), primary_key=True, default=generate_internal_uid, comment='系统内部用户ID')
    wechat_external_userid = Column(String(64), unique=True, nullable=True, comment='企微外部联系人ID')
    wechat_openid = Column(String(64), unique=True, nullable=True, comment='其他渠道用户ID')
    created_at = Column(DateTime, server_default=func.current_timestamp(), comment='注册时间')
    comments = Column(String(64), nullable=True, comment='备注')

    # ✅ 显式定义：User 拥有多个 Conversation
    # 注意：这里用 back_populates (不是 backref)
    conversations = relationship('Conversation', back_populates='user', cascade='all, delete-orphan')

    def __repr__(self):
        return f"<User(uid='{self.user_id}', wx_ext='{self.wechat_external_userid}')>"


# ================= Conversation 表 =================
class Conversation(Base):
    __tablename__ = 'conversation'

    conversation_id = Column(String(64), primary_key=True, comment='会话ID')
    # ✅ 修改点 1: 添加 ForeignKey 指向 user 表
    user_id = Column(String(64), ForeignKey('user.user_id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False,
                     index=True, comment='用户ID(Internal)')
    user_device_id = Column(String(64), nullable=True, comment='用户设备号')
    conversation_name = Column(String(64), nullable=True, comment='会话名称')
    comments = Column(String(64), nullable=True, comment='备注')
    created_at = Column(DateTime, server_default=func.current_timestamp(), comment='创建时间')
    updated_at = Column(DateTime, server_default=func.current_timestamp(), onupdate=func.current_timestamp(),
                        comment='最后对话时间')
    open_kfid = Column(String(64), nullable=True, comment='企微客服ID')

    # ✅ 显式定义：Conversation 属于一个 User
    # 注意：这里也用 back_populates，指向 User 表里的属性名 'conversations'
    user = relationship('User', back_populates='conversations')

    messages = relationship('MessageRecord', back_populates='conversation', cascade='all, delete')

    def __repr__(self):
        return f"<Conversation(id='{self.conversation_id}', user='{self.user_id}')>"


# ================= Message 表 =================
class MessageRecord(Base):
    __tablename__ = 'message_record'

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment='聊天记录ID')
    user_question = Column(LONGTEXT, nullable=False, comment='用户问题')
    bot_reply = Column(LONGTEXT, nullable=False, comment='机器人回复')
    user_id = Column(String(64), nullable=False, comment='用户ID')
    user_device_id = Column(String(64), nullable=True, comment='用户设备号')
    conversation_id = Column(String(64),
                             ForeignKey('conversation.conversation_id', onupdate='CASCADE', ondelete='CASCADE'),
                             nullable=False, comment='会话ID')
    comments = Column(String(64), nullable=True, comment='备注')
    sorting = Column(Integer, nullable=True, comment='排序')
    created_time = Column(DateTime, server_default=func.current_timestamp(), nullable=False, comment='创建时间')

    conversation = relationship('Conversation', back_populates='messages')

    def __repr__(self):
        return f"<Message(id={self.id}, conv='{self.conversation_id}', user='{self.user_id}', question='{self.user_question[:20]}...'), reply='{self.bot_reply[:20]}...'>"


# ================= 数据库连接 =================
# 1. 从环境变量读取
db_user = os.getenv("DB_USER", "root")
db_password = os.getenv("DB_PASSWORD", "Chenyunmolu521!")
db_host = os.getenv("DB_HOST", "mysql")
db_port = int(os.getenv("DB_PORT", 3306))
db_name = os.getenv("DB_NAME", "conversation_history")

# 2. 对密码进行 URL 编码 (防止密码里有 @ / : 等符号导致连接串解析失败)
# 虽然您的密码里的 '!' 通常没问题，但编码一下是更稳妥的做法
encoded_pass = quote_plus(db_password)

# 3. 拼接 URL
db_url = f'mysql+pymysql://{db_user}:{encoded_pass}@{db_host}:{db_port}/{db_name}'

# db_url = 'mysql+pymysql://root:Chenyunmolu521!@localhost:3306/conversation_history'
engine = create_engine(db_url, echo=False, pool_pre_ping=True, pool_recycle=3600, pool_size=20, max_overflow=40)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

# 创建表
Base.metadata.create_all(engine)


# ================= 封装 User Session 的 CRUD =================

# 1. Create User (创建新用户)
def create_user(user_data: dict):
    """
    创建一个新用户
    user_data: 包含 user_id, wechat_external_userid 等字段的字典
    """
    session = SessionLocal()
    try:
        user = User(**user_data)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user
    except Exception as e:
        session.rollback()
        # 生产环境建议记录日志: LOGGER.error(f"创建用户失败: {e}")
        LOGGER.error(f"创建内部用户失败: {e}")
        raise e
    finally:
        session.close()


# 2. Read User by Internal ID (根据内部 UUID 查询)
def get_user(user_id: str):
    """
    根据内部 user_id (UUID) 获取用户信息
    """
    session = SessionLocal()
    try:
        return session.query(User).filter_by(user_id=user_id).first()
    finally:
        session.close()


# 3. Read User by External ID (根据企微 ID 查询) -> ✅ 最常用的查询
def get_user_by_external_id(external_userid: str):
    """
    根据企微 external_userid 获取用户信息
    用于身份映射逻辑：External -> Internal
    """
    session = SessionLocal()
    try:
        return session.query(User).filter_by(wechat_external_userid=external_userid).first()
    finally:
        session.close()


# 4. Update User (更新用户信息)
def update_user(user_id: str, update_data: dict):
    """
    更新用户信息
    """
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(user_id=user_id).first()
        if user:
            for key, value in update_data.items():
                # 防止修改 user_id 主键
                if key != 'user_id':
                    setattr(user, key, value)
            session.commit()
            session.refresh(user)
        return user
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()


# 5. Delete User (删除用户)
def delete_user(user_id: str):
    """
    删除用户 (注意：由于设置了级联删除，这会同时删除该用户的所有会话和消息记录)
    """
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(user_id=user_id).first()
        if user:
            session.delete(user)
            session.commit()
            return True
        return False
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ================= 封装 Session 的 CRUD 示例 =================

# Create Conversation
def create_conversation(conv_data):
    session = SessionLocal()
    try:
        conv = Conversation(**conv_data)
        session.add(conv)
        session.commit()
        session.refresh(conv)
        return conv
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()


# Read Conversation
def get_conversation(conv_id):
    session = SessionLocal()
    try:
        return session.query(Conversation).filter_by(conversation_id=conv_id).first()
    finally:
        session.close()


# Read Conversations by User ID
def get_conversations_by_user_and_open_kfid(user_id, open_kfid):
    session = SessionLocal()
    try:
        return (
            session.query(Conversation)
                .filter_by(user_id=user_id, open_kfid=open_kfid)
                .order_by(Conversation.updated_at.desc())
                .all()
        )
    finally:
        session.close()


def get_conversations_by_user(user_id):
    session = SessionLocal()
    try:
        return (
            session.query(Conversation)
                .filter_by(user_id=user_id)
                .order_by(Conversation.updated_at.desc())
                .all()
        )
    finally:
        session.close()


# Update Conversation
def update_conversation(conv_id, update_data):
    session = SessionLocal()
    try:
        conv = session.query(Conversation).filter_by(conversation_id=conv_id).first()
        if conv:
            for key, value in update_data.items():
                setattr(conv, key, value)
            session.commit()
        return conv
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()


# Delete Conversation
def delete_conversation(conv_id):
    session = SessionLocal()
    try:
        conv = session.query(Conversation).filter_by(conversation_id=conv_id).first()
        if conv:
            session.delete(conv)
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Create Message
def create_message(msg_data):
    session = SessionLocal()
    try:
        msg = MessageRecord(**msg_data)
        session.add(msg)
        session.commit()
        session.refresh(msg)
        return msg
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()


# Read Messages by Conversation
def get_messages_by_conversation(conv_id):
    session = SessionLocal()
    try:
        return (
            session.query(MessageRecord)
                .filter_by(conversation_id=conv_id)
                .order_by(MessageRecord.sorting.asc())
                .all()
        )
    finally:
        session.close()


# Read Messages by User ID
def get_messages_by_user(user_id):
    session = SessionLocal()
    try:
        return (
            session.query(MessageRecord)
                .filter_by(user_id=user_id)
                .order_by(MessageRecord.created_time.asc())
                .all()
        )
    finally:
        session.close()


# Update Message
def update_message(msg_id, update_data):
    session = SessionLocal()
    try:
        msg = session.query(MessageRecord).filter_by(id=msg_id).first()
        if msg:
            for key, value in update_data.items():
                setattr(msg, key, value)
            session.commit()
        return msg
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()


# Delete Message
def delete_message(msg_id):
    session = SessionLocal()
    try:
        msg = session.query(MessageRecord).filter_by(id=msg_id).first()
        if msg:
            session.delete(msg)
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == '__main__':
    session = SessionLocal()
    msg_data = {
        'user_question': '你好',
        'bot_reply': '你好',
        'user_id': '1',
        'user_device_id': '1',

    }
