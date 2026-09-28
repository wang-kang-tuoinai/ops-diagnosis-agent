import json
import unittest
from unittest.mock import patch

from langgraph_tools import TRACE_ENTRY_SERVICE, query_trace_stats, search_traces, get_trace_detail


class TraceToolTests(unittest.TestCase):
    @patch("langgraph_tools._get", return_value="{}")
    def test_stats_service_default_and_override(self, get):
        query_trace_stats.invoke({"start": 1000, "end": 2000})
        self.assertEqual(get.call_args.kwargs["service"], TRACE_ENTRY_SERVICE)
        query_trace_stats.invoke({"service": "gateway", "operation": "GET /users", "start": 1000, "end": 2000})
        self.assertEqual(get.call_args.args, ("/traces/stats",))
        self.assertEqual(get.call_args.kwargs["service"], "gateway")
        self.assertEqual(query_trace_stats.args["service"]["default"], TRACE_ENTRY_SERVICE)

    @patch("langgraph_tools._get")
    def test_stats_preserves_call_counts_and_candidate_metadata(self, get):
        payload = {"service": "user", "stats": {"total_calls": 2,
                   "by_status": {"ok": 1, "degraded": 1, "failed": 0}},
                   "meta": {"fetched_traces": 1, "fetch_limit": 200},
                   "notices": ["partial trace"]}
        get.return_value = json.dumps(payload)
        response = query_trace_stats.invoke({"service": "user", "start": 1000, "end": 2000})
        self.assertEqual(json.loads(response), payload)
        self.assertEqual(get.call_args.kwargs["service"], "user")

    @patch("langgraph_tools._get")
    def test_search_preserves_service_entry_and_evidence(self, get):
        payload = {"items": [{"trace_id": "t", "entry_span_id": "u", "service": "user",
                              "error_summary": {"service": "profile", "span_id": "db", "message": "timeout"}}]}
        get.return_value = json.dumps(payload)
        response = search_traces.invoke({"service": "user", "operation": "GET /users", "status": "degraded", "min_duration_ms": 50})
        self.assertEqual(json.loads(response), payload)
        self.assertEqual(get.call_args.kwargs["service"], "user")
        self.assertEqual(get.call_args.kwargs["operation"], "GET /users")
        self.assertEqual(get.call_args.kwargs["min_duration_ms"], 50)

    @patch("langgraph_tools._get")
    def test_detail_keeps_fragments_and_raw_expected_error(self, get):
        payload = {"status": "unknown", "incomplete": True, "fragments": [
            {"span_id": "u", "expected_error": "handled_mysql_duplicate_key", "error": "duplicated key not allowed"}]}
        get.return_value = json.dumps(payload)
        self.assertEqual(json.loads(get_trace_detail.invoke({"trace_id": "abc"})), payload)
        self.assertEqual(get.call_args.args, ("/traces/abc",))


if __name__ == "__main__":
    unittest.main()
