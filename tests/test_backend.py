import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import PrivateAttr

from conversation_store import MissingConversation, RunConflict, now_ms
from diagnosis_runtime import DiagnosisManager
from langgraph_agent_msgstream import build_app
from server import create_app


class MemoryStore:
    """测试替身：不替代正式后端的 MySQL，checkpoint 始终使用真实 SQLite。"""
    def __init__(self):
        self.conversations, self.runs = {}, {}
        self.lock = asyncio.Lock()

    async def create(self, conversation_id, title, checkpoint_id):
        self.conversations[conversation_id] = dict(conversation_id=conversation_id, title=title,
            checkpoint_id=checkpoint_id, created_at=now_ms(), updated_at=now_ms(), active_run_id=None)
        return await self.get(conversation_id)

    async def get(self, conversation_id):
        if conversation_id not in self.conversations:
            raise MissingConversation()
        return copy.deepcopy(self.conversations[conversation_id])

    async def list(self, limit, offset):
        return sorted(self.conversations.values(), key=lambda c: c['updated_at'], reverse=True)[offset:offset+limit]

    async def reserve(self, conversation_id, run_id, request_id, question):
        async with self.lock:
            conversation = await self.get(conversation_id)
            for run in self.runs.values():
                if run['conversation_id'] == conversation_id and run['request_id'] == request_id:
                    raise RunConflict('duplicate', run['run_id'])
            if conversation['active_run_id']:
                raise RunConflict('busy', conversation['active_run_id'])
            self.conversations[conversation_id]['active_run_id'] = run_id
            self.runs[run_id] = dict(seq=len(self.runs)+1, run_id=run_id, conversation_id=conversation_id,
                request_id=request_id, question=question, status='running', events=[], error=None,
                created_at=now_ms(), updated_at=now_ms(), finished_at=None)
            return conversation

    async def save_events(self, run_id, events):
        self.runs[run_id]['events'] = copy.deepcopy(events)

    async def finish(self, conversation_id, run_id, status, events, error=None, checkpoint_id=None):
        self.runs[run_id].update(status=status, events=copy.deepcopy(events), error=error,
                                 updated_at=now_ms(), finished_at=now_ms())
        self.conversations[conversation_id]['active_run_id'] = None
        if status == 'completed':
            self.conversations[conversation_id]['checkpoint_id'] = checkpoint_id

    async def get_run(self, conversation_id, run_id):
        if run_id not in self.runs or self.runs[run_id]['conversation_id'] != conversation_id:
            raise MissingConversation()
        return copy.deepcopy(self.runs[run_id])

    async def history(self, conversation_id, limit, before=None):
        await self.get(conversation_id)
        rows = [copy.deepcopy(r) for r in self.runs.values()
                if r['conversation_id'] == conversation_id and (before is None or r['seq'] < before)]
        return sorted(rows, key=lambda r: r['seq'], reverse=True)[:limit]


@tool(response_format='content_and_artifact')
async def inspect_dependency(component: str):
    """测试工具：返回组件状态。"""
    if component == 'hold':
        await asyncio.sleep(60)
    await asyncio.sleep(0.01)
    return f'{component}: observed', {'raw': {'component': component, 'trace_id': 'trace-1'}}


class FakeChat(BaseChatModel):
    _seen: list = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self):
        return 'test-chat'

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError('测试使用异步流')

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self._seen.append(messages)
        question = next(m.content for m in reversed(messages) if isinstance(m, HumanMessage))
        yield ChatGenerationChunk(message=AIMessageChunk(content='', additional_kwargs={'reasoning_content': '检查证据。'}))
        if question == 'hold':
            yield ChatGenerationChunk(message=AIMessageChunk(content='部分回答'))
            await asyncio.sleep(60)
        if question == 'crash':
            raise RuntimeError('模拟模型故障')
        if question == 'tool_hold' and not isinstance(messages[-1], ToolMessage):
            yield ChatGenerationChunk(message=AIMessageChunk(content='', tool_call_chunks=[
                {'name': 'inspect_dependency', 'args': '{"component":"hold"}', 'id': 'tc-hold', 'index': 0},
            ]))
        elif question == 'tools' and not isinstance(messages[-1], ToolMessage):
            yield ChatGenerationChunk(message=AIMessageChunk(content='', tool_call_chunks=[
                {'name': 'inspect_dependency', 'args': '{"component":"redis"}', 'id': 'tc-1', 'index': 0},
                {'name': 'inspect_dependency', 'args': '{"component":"mysql"}', 'id': 'tc-2', 'index': 1},
            ]))
        else:
            for text in ('诊断', '完成。'):
                yield ChatGenerationChunk(message=AIMessageChunk(content=text))


def parse_sse(body):
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith('data: ')]


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'checkpoints.sqlite'
        self.saver_context = AsyncSqliteSaver.from_conn_string(str(self.path))
        self.saver = await self.saver_context.__aenter__()
        await self.saver.setup()
        self.llm = FakeChat()
        self.graph = build_app(self.llm, keep_turns=2, checkpointer=self.saver, tool_list=[inspect_dependency])
        self.store = MemoryStore()
        self.manager = DiagnosisManager(self.graph, self.store)
        self.app = create_app(manager=self.manager)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        await self.saver_context.__aexit__(None, None, None)
        self.tmp.cleanup()

    async def conversation(self):
        response = await self.client.post('/api/v1/conversations', json={'title': '测试诊断'})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn('checkpoint_id', response.json())
        return response.json()['conversation_id']

    async def ask(self, cid, question, **kwargs):
        response = await self.client.post(f'/api/v1/conversations/{cid}/messages', json={'question': question, **kwargs})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('text/event-stream', response.headers['content-type'])
        return parse_sse(response.text)

    async def test_sse_tools_history_and_references(self):
        cid = await self.conversation()
        events = await self.ask(cid, 'tools')
        self.assertEqual(events[0]['type'], 'meta')
        self.assertEqual(events[-1]['data']['status'], 'completed')
        self.assertTrue(any(e['type'] == 'thinking' for e in events))
        self.assertEqual(''.join(e['data']['delta'] for e in events if e['type'] == 'content'), '诊断完成。')
        starts = [e for e in events if e['type'] == 'tool_start']
        ends = [e for e in events if e['type'] == 'tool_end']
        self.assertEqual({e['data']['tool_call_id'] for e in starts}, {'tc-1', 'tc-2'})
        self.assertEqual({e['data']['tool_call_id'] for e in ends}, {'tc-1', 'tc-2'})
        self.assertTrue(all(e['data']['artifact']['raw']['trace_id'] == 'trace-1' for e in ends))
        history = await self.client.get(f'/api/v1/conversations/{cid}/messages')
        self.assertEqual(history.status_code, 200, history.text)
        run = history.json()['items'][0]
        self.assertEqual(run['question'], 'tools')
        self.assertEqual(run['status'], 'completed')
        self.assertTrue(any(e['type'] == 'tool_end' for e in run['events']))
        self.assertEqual(len([e for e in run['events'] if e['type'] == 'content']), 1)

    async def test_multiturn_isolation_and_input_only_trimming(self):
        cid, other = await self.conversation(), await self.conversation()
        for question in ('first', 'second', 'third'):
            await self.ask(cid, question)
        inputs = [m.content for m in self.llm._seen[-1] if isinstance(m, HumanMessage)]
        self.assertEqual(inputs, ['second', 'third'])
        state = await self.graph.aget_state({'configurable': {'thread_id': cid}})
        self.assertEqual([m.content for m in state.values['messages'] if isinstance(m, HumanMessage)], ['first', 'second', 'third'])
        await self.ask(other, 'other')
        self.assertEqual([m.content for m in self.llm._seen[-1] if isinstance(m, HumanMessage)], ['other'])
        history = (await self.client.get(f'/api/v1/conversations/{cid}/messages?limit=1')).json()
        self.assertTrue(history['has_more'])
        self.assertEqual([r['question'] for r in history['items']], ['third'])
        more = (await self.client.get(f"/api/v1/conversations/{cid}/messages?before={history['next_cursor']}")).json()
        self.assertEqual([r['question'] for r in more['items']], ['first', 'second'])
        self.assertFalse(more['has_more'])
        self.assertIsNone(more['next_cursor'])

    async def test_history_backward_pagination_boundaries(self):
        cid = await self.conversation()
        url = f'/api/v1/conversations/{cid}/messages'
        empty = (await self.client.get(url)).json()
        self.assertEqual(empty['items'], [])
        self.assertFalse(empty['has_more'])
        self.assertIsNone(empty['next_cursor'])
        for question in ('first', 'second', 'third', 'fourth'):
            await self.ask(cid, question)
        page = (await self.client.get(url, params={'limit': 2})).json()
        self.assertEqual([r['question'] for r in page['items']], ['third', 'fourth'])
        self.assertTrue(page['has_more'])
        self.assertEqual(page['next_cursor'], page['items'][0]['seq'])
        # 翻页间新增消息不能导致旧页重复或漏项。
        await self.ask(cid, 'fifth')
        older = (await self.client.get(url, params={'limit': 2, 'before': page['next_cursor']})).json()
        self.assertEqual([r['question'] for r in older['items']], ['first', 'second'])
        self.assertFalse(older['has_more'])
        self.assertIsNone(older['next_cursor'])
        end = (await self.client.get(url, params={'before': older['items'][0]['seq']})).json()
        self.assertEqual(end['items'], [])
        self.assertIsNone(end['next_cursor'])
        for invalid in (0, -1, 'invalid'):
            self.assertEqual((await self.client.get(url, params={'before': invalid})).status_code, 422)

    async def test_restart_restores_sqlite_context(self):
        cid = await self.conversation()
        await self.ask(cid, 'before-restart')
        await self.saver_context.__aexit__(None, None, None)
        self.saver_context = AsyncSqliteSaver.from_conn_string(str(self.path))
        self.saver = await self.saver_context.__aenter__()
        graph = build_app(self.llm, checkpointer=self.saver, tool_list=[inspect_dependency])
        self.manager.graph = graph
        await self.ask(cid, 'after-restart')
        self.assertEqual([m.content for m in self.llm._seen[-1] if isinstance(m, HumanMessage)], ['before-restart', 'after-restart'])

    async def test_failed_or_cancelled_turn_not_in_next_context(self):
        cid = await self.conversation()
        await self.ask(cid, 'good')
        initial_checkpoint = self.store.conversations[cid]['checkpoint_id']
        events = await self.ask(cid, 'crash')
        self.assertEqual(events[-1]['data']['status'], 'failed')
        self.assertEqual(self.store.conversations[cid]['checkpoint_id'], initial_checkpoint)
        session = await self.manager.start(cid, 'hold')
        while not any(e['type'] == 'content' for e in session.events):
            await asyncio.sleep(0.01)
        busy = await self.client.post(f'/api/v1/conversations/{cid}/messages', json={'question': 'duplicate'})
        self.assertEqual(busy.status_code, 409)
        response = await self.client.post(f'/api/v1/conversations/{cid}/runs/{session.run_id}/cancel')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'cancelled')
        self.assertTrue(any(e['type'] == 'content' for e in response.json()['events']))
        self.assertEqual(self.store.conversations[cid]['checkpoint_id'], initial_checkpoint)
        await self.ask(cid, 'continue')
        self.assertEqual([m.content for m in self.llm._seen[-1] if isinstance(m, HumanMessage)], ['good', 'continue'])

    async def test_immediate_cancel_validation_and_idempotency(self):
        cid = await self.conversation()
        session = await self.manager.start(cid, 'hold')
        await session.cancel()
        self.assertEqual(self.store.runs[session.run_id]['status'], 'cancelled')
        request_id = str(uuid4())
        await self.ask(cid, 'one', request_id=request_id)
        duplicate = await self.client.post(f'/api/v1/conversations/{cid}/messages', json={'question': 'one', 'request_id': request_id})
        self.assertEqual(duplicate.status_code, 409)
        for body in ({'question': ' '}, {'question': 'x', 'unknown': 1}):
            self.assertEqual((await self.client.post(f'/api/v1/conversations/{cid}/messages', json=body)).status_code, 422)
        self.assertEqual((await self.client.get(f'/api/v1/conversations/{uuid4()}/messages')).status_code, 404)

    async def test_disconnect_cancels_generation(self):
        cid = await self.conversation()
        disconnected = asyncio.Event()
        request_sent = False
        body = json.dumps({'question': 'hold'}).encode()

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            await disconnected.wait()
            return {'type': 'http.disconnect'}

        async def send(message):
            if message['type'] == 'http.response.body' and b'event: content' in message.get('body', b''):
                disconnected.set()

        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.0'}, 'http_version': '1.1',
                 'method': 'POST', 'scheme': 'http', 'path': f'/api/v1/conversations/{cid}/messages',
                 'raw_path': b'/', 'query_string': b'', 'headers': [(b'content-type', b'application/json')],
                 'client': ('127.0.0.1', 1), 'server': ('test', 80), 'root_path': ''}
        await asyncio.wait_for(self.app(scope, receive, send), timeout=5)
        history = await self.store.history(cid, 10)
        self.assertEqual(history[0]['status'], 'cancelled')

    async def test_cancel_emits_terminal_event_for_running_tool(self):
        cid = await self.conversation()
        session = await self.manager.start(cid, 'tool_hold')
        async def wait_for_tool():
            while not session.open_tools:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait_for_tool(), timeout=5)
        await session.cancel()
        events = parse_sse(''.join([part async for part in session.stream()]))
        ended = [e for e in events if e['type'] == 'tool_end']
        self.assertEqual(len(ended), 1)
        self.assertEqual(ended[0]['data']['tool_call_id'], 'tc-hold')
        self.assertEqual(ended[0]['data']['status'], 'cancelled')
        self.assertEqual(events[-1]['data']['status'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
