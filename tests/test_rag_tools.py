import copy
import json
import unittest
from unittest.mock import Mock, patch

import requests
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_openai.chat_models.base import _convert_message_to_dict
from langgraph.prebuilt import ToolNode
from langgraph.graph import END, START, MessagesState, StateGraph

from langgraph_agent_msgstream import build_app
from langgraph_tools import tools
from rag_tools import RAG_API_BASE, project_knowledge, search_ops_knowledge


def sample_response():
    return {"items": [
        {"doc_id": "runbook-redis", "title": "缓存失败排查", "source": "runbooks/redis.md",
         "score": 0.99, "content": "# 排查手册\n完整正文不能截断。", "content_mode": "full",
         "doc_type": "runbook", "snapshot_id": "a" * 64, "matched_sections": ["验证步骤"]},
        {"doc_id": "redis-tech", "title": "Redis 延迟", "source": "technology/redis.md",
         "score": 0.98, "content": "文档标题：Redis 延迟\n章节标题：测量延迟\n\n原始片段。",
         "content_mode": "chunk", "doc_type": "technology", "chunk_id": "redis-tech::section::1",
         "component": "redis", "source_url": "https://example.com/redis", "section": "测量延迟",
         "chunk_index": 1}], "notices": ["精排分数不是置信度", "一条结果因预算不足被省略"]}


def call(tool_id="rag-call", **args):
    return {"type": "tool_call", "id": tool_id, "name": "search_ops_knowledge",
            "args": {"query": "Redis 读取超时后如何排查", **args}}


class RagToolTests(unittest.TestCase):
    def setUp(self):
        self.payload = sample_response()
        self.response = Mock(status_code=200)
        self.response.json.return_value = self.payload
        patcher = patch('rag_tools.requests.post', return_value=self.response)
        self.post = patcher.start()
        self.addCleanup(patcher.stop)

    def test_registered_schema_and_default_request(self):
        self.assertEqual(sum(t.name == 'search_ops_knowledge' for t in tools), 1)
        schema = search_ops_knowledge.tool_call_schema.model_json_schema()
        self.assertEqual(set(schema['properties']), {'query', 'doc_type', 'top_k'})
        message = search_ops_knowledge.invoke(call(query='  Redis 超时  '))
        self.assertIsInstance(message, ToolMessage)
        self.assertEqual(message.status, 'success')
        self.post.assert_called_once_with(f'{RAG_API_BASE}/knowledge/search',
                                         json={'query': 'Redis 超时', 'top_k': 3}, timeout=(5, 60))

    def test_projection_artifact_and_llm_serialization(self):
        original = copy.deepcopy(self.payload)
        message = search_ops_knowledge.invoke(call(doc_type='technology', top_k=2))
        slim = json.loads(message.content)
        self.assertEqual(slim['notices'], original['notices'])
        self.assertEqual(set(slim['items'][0]), {'ref', 'title', 'doc_type', 'content_mode', 'content', 'matched_sections'})
        self.assertEqual(set(slim['items'][1]), {'ref', 'title', 'doc_type', 'content_mode', 'content', 'source_url'})
        for index, item in enumerate(slim['items']):
            self.assertEqual(item['content'], original['items'][index]['content'])
            self.assertEqual(message.artifact['ref_to_item'][item['ref']], index)
        self.assertEqual(message.artifact['response'], original)
        self.assertEqual(self.payload, original)
        wire = _convert_message_to_dict(message)
        self.assertNotIn('artifact', wire)
        self.assertNotIn('snapshot_id', wire['content'])
        self.assertNotIn('score', wire['content'])
        self.assertEqual(self.post.call_args.kwargs['json']['doc_type'], 'technology')

    def test_references_stable_across_order_scores_and_parallel_calls(self):
        first, _ = project_knowledge(self.payload)
        reordered = copy.deepcopy(self.payload)
        reordered['items'].reverse()
        reordered['items'][0]['score'] = 0.1
        second, _ = project_knowledge(reordered)
        first_refs = [item['ref'] for item in json.loads(first)['items']]
        self.assertEqual(first_refs, [item['ref'] for item in json.loads(second)['items']][::-1])
        changed = copy.deepcopy(self.payload)
        changed['items'][0]['content'] += '更新内容'
        third, _ = project_knowledge(changed)
        self.assertNotEqual(first_refs[0], json.loads(third)['items'][0]['ref'])
        graph = StateGraph(MessagesState)
        graph.add_node('tools', ToolNode([search_ops_knowledge]))
        graph.add_edge(START, 'tools')
        graph.add_edge('tools', END)
        result = graph.compile().invoke({'messages': [AIMessage(content='', tool_calls=[call('one'), call('two')])]})
        messages = [message for message in result['messages'] if isinstance(message, ToolMessage)]
        self.assertEqual(len(messages), 2)
        for message in messages:
            self.assertEqual(json.loads(message.content)['items'][0]['ref'], first_refs[0])
            self.assertIsNotNone(message.artifact)

    def test_invalid_parameters_fail_before_http(self):
        for args in ({'query': ' '}, {'query': 'x' * 2001}, {'doc_type': 'mysql'},
                     {'top_k': 0}, {'top_k': 6}, {'top_k': True}, {'component': 'redis'}):
            with self.subTest(args=list(args)):
                message = search_ops_knowledge.invoke(call(**args))
                self.assertEqual(message.status, 'error')
        self.post.assert_not_called()

    def test_transport_and_bad_response_errors_remain_tool_messages(self):
        for error in (requests.Timeout(), requests.ConnectionError()):
            self.post.side_effect = error
            self.assertEqual(search_ops_knowledge.invoke(call()).status, 'error')
        self.post.side_effect = None
        self.response.status_code = 503
        message = search_ops_knowledge.invoke(call())
        self.assertEqual(message.status, 'error')
        self.assertIn('503', message.content)
        self.response.status_code = 200
        self.response.json.side_effect = ValueError('invalid JSON')
        self.assertEqual(search_ops_knowledge.invoke(call()).status, 'error')
        self.response.json.side_effect = None
        for payload in ({}, {'items': [], 'notices': 'bad'}, {'items': [{}], 'notices': []}):
            self.response.json.return_value = payload
            self.assertEqual(search_ops_knowledge.invoke(call()).status, 'error')

    def test_empty_results_keep_notices(self):
        self.response.json.return_value = {'items': [], 'notices': ['没有匹配的候选资料']}
        message = search_ops_knowledge.invoke(call())
        self.assertEqual(message.status, 'success')
        self.assertEqual(json.loads(message.content), self.response.json.return_value)

    def test_graph_executes_tool_and_passes_only_projection_to_answer_step(self):
        calls = []

        def fake_llm(messages):
            calls.append(messages)
            if len(calls) == 1:
                return AIMessage(content='', tool_calls=[call()])
            tool_message = messages[-1]
            self.assertIsInstance(tool_message, ToolMessage)
            evidence = json.loads(tool_message.content)
            self.assertNotIn('doc_id', evidence['items'][0])
            self.assertIsNotNone(tool_message.artifact)
            return AIMessage(content=f"参考 [{evidence['items'][0]['ref']}]，继续核对实际日志。")

        result = build_app(RunnableLambda(fake_llm)).invoke({'messages': [HumanMessage(content='Redis 超时如何排查')]})
        self.assertEqual(len(calls), 2)
        self.assertIn('[K-', result['messages'][-1].content)


if __name__ == '__main__':
    unittest.main()
