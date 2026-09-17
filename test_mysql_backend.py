"""显式启用的 MySQL 集成测试；仅创建/删除随机命名的独立测试库。"""
import asyncio
from dataclasses import replace
import os
from pathlib import Path
import re
import tempfile
import unittest
from uuid import uuid4

import aiomysql
import httpx
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from conversation_store import MySQLConversationStore, RunConflict
from diagnosis_runtime import DiagnosisManager
from langgraph_agent_msgstream import build_app
from server import Settings, create_app
from test_backend import FakeChat, inspect_dependency, parse_sse


@unittest.skipUnless(os.getenv('AGENT_TEST_MYSQL') == '1', '设置 AGENT_TEST_MYSQL=1 才运行真实 MySQL 测试')
class MySQLBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        config = Settings()
        self.database = 'test_ops_agent_' + uuid4().hex
        assert re.fullmatch(r'test_ops_agent_[0-9a-f]{32}', self.database)
        self.admin = await aiomysql.connect(host=config.mysql_host, port=config.mysql_port,
            user=config.mysql_user, password=config.mysql_password, autocommit=True)
        async with self.admin.cursor() as cursor:
            await cursor.execute(f'CREATE DATABASE `{self.database}` CHARACTER SET utf8mb4')
        self.addAsyncCleanup(self.remove_database)
        self.settings = replace(config, mysql_database=self.database)
        self.store = await MySQLConversationStore.connect(self.settings)
        self.addAsyncCleanup(self.close_store)
        await self.store.setup()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'checkpoints.sqlite'
        self.saver_context = AsyncSqliteSaver.from_conn_string(str(self.path))
        self.saver = await self.saver_context.__aenter__()
        self.addAsyncCleanup(self.close_saver)
        self.llm = FakeChat()
        self.manager = DiagnosisManager(build_app(self.llm, checkpointer=self.saver, tool_list=[inspect_dependency]), self.store)
        self.addAsyncCleanup(self.manager.close)

    async def close_saver(self):
        await self.saver_context.__aexit__(None, None, None)

    async def close_store(self):
        await self.store.close()

    async def remove_database(self):
        assert re.fullmatch(r'test_ops_agent_[0-9a-f]{32}', self.database)
        async with self.admin.cursor() as cursor:
            await cursor.execute(f'DROP DATABASE `{self.database}`')
        self.admin.close()

    async def test_http_mysql_persistence_and_restart(self):
        app = create_app(manager=self.manager)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                created = await client.post('/api/v1/conversations', json={'title': "中文诊断 ' 引号"})
                self.assertEqual(created.status_code, 201, created.text)
                cid = created.json()['conversation_id']
                request_id = str(uuid4())
                response = await client.post(f'/api/v1/conversations/{cid}/messages', json={'question': 'tools', 'request_id': request_id})
                self.assertEqual(response.status_code, 200, response.text)
                events = parse_sse(response.text)
                self.assertEqual(events[-1]['data']['status'], 'completed')
                history = (await client.get(f'/api/v1/conversations/{cid}/messages')).json()['items']
                self.assertEqual(len(history), 1)
                self.assertTrue(any(e['type'] == 'tool_end' and e['data']['artifact'] for e in history[0]['events']))
                duplicate = await client.post(f'/api/v1/conversations/{cid}/messages', json={'question': 'tools', 'request_id': request_id})
                self.assertEqual(duplicate.status_code, 409)
        checkpoint = (await self.store.get(cid))['checkpoint_id']
        abandoned_id = str(uuid4())
        await self.store.reserve(cid, abandoned_id, str(uuid4()), 'abandoned')
        await self.store.close()
        await self.saver_context.__aexit__(None, None, None)
        self.store = await MySQLConversationStore.connect(self.settings)
        await self.store.recover()
        self.assertEqual((await self.store.get_run(cid, abandoned_id))['status'], 'interrupted')
        self.assertEqual((await self.store.get(cid))['checkpoint_id'], checkpoint)
        self.saver_context = AsyncSqliteSaver.from_conn_string(str(self.path))
        self.saver = await self.saver_context.__aenter__()
        self.manager.graph = build_app(self.llm, checkpointer=self.saver, tool_list=[inspect_dependency])
        self.manager.store = self.store
        run = await self.manager.start(cid, 'continue')
        await run.task
        self.assertEqual((await self.store.get_run(cid, run.run_id))['status'], 'completed')
        questions = [m.content for m in self.llm._seen[-1] if m.type == 'human']
        self.assertEqual(questions, ['tools', 'continue'])

    async def test_transaction_prevents_concurrent_reservations(self):
        conversation = await self.manager.create_conversation('并发测试')
        cid = conversation['conversation_id']
        run_ids = [str(uuid4()), str(uuid4())]
        results = await asyncio.gather(*(self.store.reserve(cid, rid, str(uuid4()), 'question') for rid in run_ids), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, RunConflict) for r in results), 1)
        winner = next(rid for rid, result in zip(run_ids, results) if isinstance(result, dict))
        await self.store.finish(cid, winner, 'cancelled', [], error='test')
        self.assertIsNone((await self.store.get(cid))['active_run_id'])


if __name__ == '__main__':
    unittest.main()
