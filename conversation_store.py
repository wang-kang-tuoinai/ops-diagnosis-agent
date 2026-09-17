"""MySQL 展示历史与执行索引；LangGraph checkpoint 内容不写入这些表。"""
import json
import time

import aiomysql


def now_ms():
    return time.time_ns() // 1_000_000


class MissingConversation(Exception):
    pass


class RunConflict(Exception):
    def __init__(self, message, run_id):
        super().__init__(message)
        self.run_id = run_id


class MySQLConversationStore:
    def __init__(self, pool):
        self.pool = pool

    @classmethod
    async def connect(cls, settings):
        pool = await aiomysql.create_pool(
            host=settings.mysql_host, port=settings.mysql_port, user=settings.mysql_user,
            password=settings.mysql_password, db=settings.mysql_database,
            charset="utf8mb4", autocommit=True, minsize=1, maxsize=5,
            connect_timeout=5, pool_recycle=1800,
        )
        return cls(pool)

    async def close(self):
        self.pool.close()
        await self.pool.wait_closed()

    async def execute(self, sql, args=(), fetch=False):
        async with self.pool.acquire() as connection:
            async with connection.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(sql, args)
                return await cursor.fetchall() if fetch else cursor.rowcount

    async def setup(self):
        await self.execute("""CREATE TABLE IF NOT EXISTS agent_conversations (
            conversation_id VARCHAR(36) PRIMARY KEY,
            title VARCHAR(200) NOT NULL,
            created_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL,
            checkpoint_id VARCHAR(128) NOT NULL,
            active_run_id VARCHAR(36) NULL,
            INDEX idx_agent_conversations_updated (updated_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        await self.execute("""CREATE TABLE IF NOT EXISTS agent_runs (
            seq BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            run_id VARCHAR(36) NOT NULL UNIQUE,
            conversation_id VARCHAR(36) NOT NULL,
            request_id VARCHAR(36) NOT NULL,
            question TEXT NOT NULL,
            status VARCHAR(20) NOT NULL,
            events JSON NOT NULL,
            error TEXT NULL,
            created_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL,
            finished_at BIGINT NULL,
            UNIQUE KEY idx_agent_request (conversation_id, request_id),
            INDEX idx_agent_history (conversation_id, seq),
            FOREIGN KEY (conversation_id) REFERENCES agent_conversations(conversation_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")

    async def recover(self):
        # 单实例启动：上次进程退出时尚未结束的执行不自动重跑。
        async with self.pool.acquire() as connection:
            await connection.begin()
            try:
                async with connection.cursor() as cursor:
                    await cursor.execute("""UPDATE agent_runs SET status='interrupted',
                        error='服务重启，上一轮执行已中断', updated_at=%s, finished_at=%s
                        WHERE status='running'""", (now_ms(), now_ms()))
                    await cursor.execute("UPDATE agent_conversations SET active_run_id=NULL WHERE active_run_id IS NOT NULL")
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def create(self, conversation_id, title, checkpoint_id):
        timestamp = now_ms()
        await self.execute("""INSERT INTO agent_conversations
            (conversation_id,title,checkpoint_id,created_at,updated_at) VALUES (%s,%s,%s,%s,%s)""",
                           (conversation_id, title, checkpoint_id, timestamp, timestamp))
        return await self.get(conversation_id)

    async def get(self, conversation_id):
        rows = await self.execute("SELECT * FROM agent_conversations WHERE conversation_id=%s", (conversation_id,), True)
        if not rows:
            raise MissingConversation()
        return rows[0]

    async def list(self, limit, offset):
        return await self.execute("""SELECT * FROM agent_conversations
            ORDER BY updated_at DESC, conversation_id DESC LIMIT %s OFFSET %s""", (limit, offset), True)

    async def reserve(self, conversation_id, run_id, request_id, question):
        async with self.pool.acquire() as connection:
            await connection.begin()
            try:
                async with connection.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute("SELECT * FROM agent_conversations WHERE conversation_id=%s FOR UPDATE", (conversation_id,))
                    conversation = await cursor.fetchone()
                    if conversation is None:
                        raise MissingConversation()
                    await cursor.execute("SELECT run_id FROM agent_runs WHERE conversation_id=%s AND request_id=%s", (conversation_id, request_id))
                    existing = await cursor.fetchone()
                    if existing:
                        raise RunConflict("该请求已提交，请读取原执行记录，不要重复生成", existing["run_id"])
                    if conversation["active_run_id"]:
                        raise RunConflict("该会话正在生成，请等待结束或取消", conversation["active_run_id"])
                    timestamp = now_ms()
                    await cursor.execute("""INSERT INTO agent_runs
                        (run_id,conversation_id,request_id,question,status,events,created_at,updated_at)
                        VALUES (%s,%s,%s,%s,'running','[]',%s,%s)""",
                                         (run_id, conversation_id, request_id, question, timestamp, timestamp))
                    await cursor.execute("UPDATE agent_conversations SET active_run_id=%s,updated_at=%s WHERE conversation_id=%s",
                                         (run_id, timestamp, conversation_id))
                await connection.commit()
                return conversation
            except BaseException:
                await connection.rollback()
                raise

    async def save_events(self, run_id, events):
        await self.execute("UPDATE agent_runs SET events=%s,updated_at=%s WHERE run_id=%s AND status='running'",
                           (json.dumps(events, ensure_ascii=False), now_ms(), run_id))

    async def finish(self, conversation_id, run_id, status, events, error=None, checkpoint_id=None):
        async with self.pool.acquire() as connection:
            await connection.begin()
            try:
                async with connection.cursor() as cursor:
                    timestamp = now_ms()
                    await cursor.execute("""UPDATE agent_runs SET status=%s,events=%s,error=%s,updated_at=%s,finished_at=%s
                        WHERE run_id=%s AND status='running'""",
                                         (status, json.dumps(events, ensure_ascii=False), error, timestamp, timestamp, run_id))
                    if status == "completed":
                        if not checkpoint_id:
                            raise ValueError("成功执行缺少 checkpoint_id")
                        await cursor.execute("""UPDATE agent_conversations SET checkpoint_id=%s,active_run_id=NULL,updated_at=%s
                            WHERE conversation_id=%s AND active_run_id=%s""",
                                             (checkpoint_id, timestamp, conversation_id, run_id))
                    else:
                        await cursor.execute("""UPDATE agent_conversations SET active_run_id=NULL,updated_at=%s
                            WHERE conversation_id=%s AND active_run_id=%s""", (timestamp, conversation_id, run_id))
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    @staticmethod
    def decode(row):
        row = dict(row)
        if isinstance(row["events"], (str, bytes)):
            row["events"] = json.loads(row["events"])
        return row

    async def history(self, conversation_id, limit, after):
        await self.get(conversation_id)
        rows = await self.execute("""SELECT * FROM agent_runs WHERE conversation_id=%s AND seq>%s
            ORDER BY seq ASC LIMIT %s""", (conversation_id, after, limit), True)
        return [self.decode(row) for row in rows]

    async def get_run(self, conversation_id, run_id):
        rows = await self.execute("SELECT * FROM agent_runs WHERE conversation_id=%s AND run_id=%s",
                                  (conversation_id, run_id), True)
        if not rows:
            raise MissingConversation()
        return self.decode(rows[0])
