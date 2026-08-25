"""支持 DeepSeek 思考模式（reasoning_content）的 ChatOpenAI 子类。

langchain-openai 的 ChatOpenAI 只面向 OpenAI 官方协议，第三方字段
reasoning_content 既不会在接收时提取，也不会在发送时回传。这里补两件事：

1. 接收：把响应里的 reasoning_content 捞到 additional_kwargs["reasoning_content"]。
2. 发送：把上一轮的 reasoning_content 原样回传（DeepSeek 思考模式要求，否则下一轮请求会被拒绝）。

思考模式本身通过构造参数开启：extra_body={"thinking": {"type": "enabled"}} 和 reasoning_effort。
"""
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_openai import ChatOpenAI

DEBUG_PAYLOAD = False  # 调试开关：为 True 时每次发请求前打印完整上下文，调试完改成 False


def _debug_print_payload(payload: dict) -> None:
    """调试用：打印即将发给模型的请求，重点展示 messages 上下文。"""
    print("\n" + "=" * 72)
    print(f"发送请求 | model={payload.get('model')} | stream={payload.get('stream')}")
    msgs = payload.get("messages", [])
    print(f"messages 共 {len(msgs)} 条:")
    for i, m in enumerate(msgs):
        content = m.get("content")
        if isinstance(content, str) and len(content) > 160:
            content = content[:160] + f"...(共{len(m['content'])}字)"
        line = f"  [{i}] {m.get('role')}: {content!r}"
        if m.get("tool_calls"):
            names = [tc.get("function", {}).get("name") for tc in m["tool_calls"]]
            line += f" | tool_calls={names}"
        if m.get("reasoning_content"):
            line += f" | reasoning={m['reasoning_content'][:80]!r}"
        print(line)
    print("=" * 72 + "\n")


class DeepSeekChatOpenAI(ChatOpenAI):
    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ):
        """流式路径：从原始 delta 里把 reasoning_content 捞出来，注入到 chunk 的 additional_kwargs。"""
        gen_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if gen_chunk is None:
            return gen_chunk
        choices = chunk.get("choices", []) or chunk.get("chunk", {}).get("choices", [])
        if not choices:
            return gen_chunk
        delta = choices[0].get("delta") or {}
        reasoning = delta.get("reasoning_content")
        if reasoning and isinstance(gen_chunk.message, AIMessageChunk):
            msg = gen_chunk.message
            # 增量拼接：一个流里 reasoning_content 是分片到达的
            msg.additional_kwargs["reasoning_content"] = (
                msg.additional_kwargs.get("reasoning_content", "") + reasoning
            )
        return gen_chunk

    def _create_chat_result(self, response: Any, generation_info: dict | None = None):
        """非流式路径：同样把 reasoning_content 捞到 additional_kwargs，保证 invoke 也可用。"""
        chat_result = super()._create_chat_result(response, generation_info=generation_info)
        if isinstance(response, dict):
            choices = response.get("choices") or []
        else:
            choices = getattr(response, "choices", None) or []
        for i, choice in enumerate(choices):
            message = (
                choice.get("message") if isinstance(choice, dict)
                else getattr(choice, "message", None)
            )
            reasoning = (
                message.get("reasoning_content") if isinstance(message, dict)
                else getattr(message, "reasoning_content", None)
            )
            if reasoning and i < len(chat_result.generations):
                msg = chat_result.generations[i].message
                if isinstance(msg, AIMessage):
                    msg.additional_kwargs["reasoning_content"] = reasoning
        return chat_result

    def _get_request_payload(self, input_: Any, *, stop: Any = None, **kwargs: Any) -> dict:
        """发送路径：把 assistant 消息里保存的 reasoning_content 回传到请求里。"""
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if "messages" not in payload:
            return payload  # responses api 路径，不处理
        src_messages = self._convert_input(input_).to_messages()
        for src, out in zip(src_messages, payload["messages"]):
            if isinstance(src, AIMessage) and out.get("role") == "assistant":
                reasoning = src.additional_kwargs.get("reasoning_content")
                if reasoning:
                    out["reasoning_content"] = reasoning
        if DEBUG_PAYLOAD:
            _debug_print_payload(payload)
        return payload
