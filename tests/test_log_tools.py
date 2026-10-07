import json
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from langgraph_tools import query_log_stats, query_log_templates, search_logs


class LogToolTests(unittest.TestCase):
    @patch("langgraph_tools._get", return_value="{}")
    def test_stats_schema_and_service_scope(self, get):
        self.assertNotIn("level", query_log_stats.args)
        self.assertNotIn("top_n", query_log_stats.args)
        query_log_stats.invoke({"start": 10, "end": 20})
        self.assertEqual(get.call_args.args, ("/logs/stats",))
        self.assertIsNone(get.call_args.kwargs["service"])
        self.assertNotIn("level", get.call_args.kwargs)
        self.assertNotIn("top_n", get.call_args.kwargs)
        query_log_stats.invoke({"service": "user", "route": "/users", "method": "GET"})
        self.assertEqual(get.call_args.kwargs["service"], "user")
        self.assertEqual(get.call_args.kwargs["route"], "/users")

    @patch("langgraph_tools._get")
    def test_stats_keeps_all_service_summaries(self, get):
        payload = {"window": {"start": 10, "end": 20}, "summaries": [
            {"service": "a", "error_count": 2}, {"service": "b", "error_count": 1}]}
        get.return_value = json.dumps(payload)
        self.assertEqual(json.loads(query_log_stats.invoke({})), payload)

    @patch("langgraph_tools._get")
    def test_templates_require_service_before_http(self, get):
        self.assertIn("service", query_log_templates.get_input_schema().model_json_schema()["required"])
        with self.assertRaises(ValidationError):
            query_log_templates.invoke({"start": 10, "end": 20})
        get.assert_not_called()

    @patch("langgraph_tools._get")
    def test_templates_forward_filters_and_preserve_truncation(self, get):
        payload = {"service": "user", "items": [], "has_more": True, "notices": ["partial templates"]}
        get.return_value = json.dumps(payload)
        result = query_log_templates.invoke({"service": "user", "level": "WARN", "limit": 1})
        self.assertEqual(json.loads(result), payload)
        self.assertEqual(get.call_args.args, ("/logs/templates",))
        self.assertEqual(get.call_args.kwargs["service"], "user")
        self.assertEqual(get.call_args.kwargs["level"], "WARN")
        self.assertEqual(get.call_args.kwargs["limit"], 1)

    @patch("langgraph_tools._get", return_value="{}")
    def test_search_still_allows_cross_service_trace_lookup(self, get):
        search_logs.invoke({"trace_id": "trace"})
        self.assertIsNone(get.call_args.kwargs["service"])
        self.assertEqual(get.call_args.kwargs["trace_id"], "trace")


if __name__ == "__main__":
    unittest.main()
