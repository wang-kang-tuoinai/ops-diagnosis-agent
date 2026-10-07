# 测试

所有命令从 **ops-diagnosis-agent 根目录**运行，使用已安装项目依赖的 Python 环境。

## 默认测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

| 文件 | 覆盖范围 |
| --- | --- |
| `test_backend.py` | 会话接口、SSE、并发冲突、取消、临时 SQLite 上下文及恢复 |
| `test_system_prompt.py` | 当前提示词注入、旧 system 消息处理、完整轮次与工具消息配对 |
| `test_log_tools.py` | 日志工具参数与返回透传 |
| `test_trace_tools.py` | Trace 工具参数与统计契约 |
| `test_rag_tools.py` | 检索参数、字段投影、稳定引用、artifact 与正式诊断图中的工具调用 |
| `test_mysql_backend.py` | 显式启用的真实 MySQL 存储与接口集成测试 |

普通测试不请求真实模型或观测/知识服务，不访问真实 MySQL。SQLite 使用临时文件。MySQL 测试默认跳过；若环境中已设置 `AGENT_TEST_MYSQL=1`，发现命令也会执行它。仅执行普通回归可显式指定模块：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_backend tests.test_system_prompt tests.test_log_tools tests.test_trace_tools tests.test_rag_tools -v
```

单独运行某一组：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_rag_tools -v
```

`test_mysql_backend.py` 通过 `tests.test_backend` 复用测试替身。请使用模块或 discover 命令，不直接执行 `python tests/test_*.py`。

## MySQL 集成测试

需要可连接的 MySQL，以及能够创建、删除测试数据库的账户。下面示例使用根 Compose 的业务 MySQL 开发管理员，只使用其连接，不在业务库中写入测试数据：

```powershell
$env:AGENT_TEST_MYSQL = "1"
$env:AGENT_MYSQL_HOST = "127.0.0.1"
$env:AGENT_MYSQL_PORT = "3306"
$env:AGENT_MYSQL_USER = "root"
$env:AGENT_MYSQL_PASSWORD = "root"
try {
    .\.venv\Scripts\python.exe -m unittest tests.test_mysql_backend -v
} finally {
    Remove-Item Env:AGENT_TEST_MYSQL
}
```

测试创建随机命名的 `test_ops_agent_<UUID hex>` 数据库，结束时删除；模型仍为模拟模型。强制终止测试进程可能留下该临时库。

这些测试验证接口、状态管理和工具协议。真实模型选工具、检索质量与诊断准确性需要另做场景验收，见 [提示词回归说明](../docs/prompt-design.md)。
