from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from scripts import healthbench_e2e


class ScenarioTest(unittest.TestCase):
    def test_default_scenarios_cover_both_languages_and_emergencies(self) -> None:
        scenarios = healthbench_e2e.default_scenarios()

        self.assertEqual(
            {scenario.name for scenario in scenarios},
            {"en_multiturn", "ko_multiturn", "en_emergency", "ko_emergency"},
        )
        self.assertTrue(all(scenario.messages[-1]["role"] == "user" for scenario in scenarios))
        self.assertEqual(
            {scenario.name for scenario in scenarios if scenario.emergency},
            {"en_emergency", "ko_emergency"},
        )


class EmergencyCheckTest(unittest.TestCase):
    def test_english_emergency_check_accepts_korean_emergency_number(self) -> None:
        self.assertTrue(healthbench_e2e._has_emergency_action("Call 119 immediately.", "en"))


class ServiceCheckTest(unittest.TestCase):
    def test_service_checks_send_full_multiturn_conversations(self) -> None:
        seen_messages: list[list[dict[str, str]]] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def do_GET(self) -> None:
                if self.path == "/health":
                    payload = {"status": "ok"}
                elif self.path == "/v1/models":
                    payload = {"data": [{"id": "Lunit/L2-preview"}]}
                else:
                    self.send_error(404)
                    return
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self) -> None:
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode())
                messages = payload["messages"]
                seen_messages.append(messages)
                latest = messages[-1]["content"]
                if "가슴" in latest:
                    content = "지금 119에 연락하거나 응급실로 가세요."
                elif "chest" in latest:
                    content = "Call local emergency services now or go to the emergency department."
                elif "진통제" in latest:
                    content = "현재 복용 중인 약과 성분을 확인해 보세요."
                else:
                    content = "Please check the active ingredient before combining medicines."
                raw = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            results = healthbench_e2e.run_service_checks(
                f"http://127.0.0.1:{server.server_address[1]}", timeout_s=1
            )
        finally:
            server.shutdown()
            server.server_close()

        self.assertTrue(all(result.ok for result in results), results)
        self.assertEqual(len(seen_messages), 4)
        self.assertEqual(seen_messages[0], list(healthbench_e2e.default_scenarios()[0].messages))


class MCPDocumentCheckTest(unittest.TestCase):
    def test_document_roundtrip_finds_page_content_and_citation(self) -> None:
        calls: list[tuple[str, str | None]] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode())
                method = request["method"]
                tool_name = (request.get("params") or {}).get("name")
                calls.append((method, tool_name))
                if method == "tools/list":
                    result = {"tools": [
                        {"name": "index_get_relevant_nodes"},
                        {"name": "index_get_page_content"},
                    ]}
                elif tool_name == "index_get_relevant_nodes":
                    result = {"content": [{"type": "text", "text": json.dumps([
                        {"doc_id": "doc-1", "range": [3, 4]},
                    ])}]}
                elif tool_name == "index_get_page_content":
                    result = {"content": [{"type": "text", "text": json.dumps({
                        "cite_uid": "cite-test", "pages": [{"text": "source text"}],
                    })}]}
                else:
                    self.send_error(400)
                    return
                raw = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            result = healthbench_e2e.run_mcp_document_check(
                f"http://127.0.0.1:{server.server_address[1]}/mcp", timeout_s=1
            )
        finally:
            server.shutdown()
            server.server_close()

        self.assertTrue(result.ok, result)
        self.assertEqual(
            calls,
            [
                ("tools/list", None),
                ("tools/call", "index_get_relevant_nodes"),
                ("tools/call", "index_get_page_content"),
            ],
        )


class CommandLineTest(unittest.TestCase):
    def test_skip_mcp_runs_only_service_checks(self) -> None:
        service_results = [healthbench_e2e.CheckResult("service_health", True, "ok")]
        with patch.object(healthbench_e2e, "run_service_checks", return_value=service_results), patch.object(
            healthbench_e2e, "run_mcp_document_check"
        ) as mcp_check, patch("builtins.print"):
            exit_code = healthbench_e2e.main(["--base-url", "http://service.test", "--skip-mcp"])

        self.assertEqual(exit_code, 0)
        mcp_check.assert_not_called()

    def test_failed_check_maps_to_nonzero_exit(self) -> None:
        failed = [healthbench_e2e.CheckResult("en_emergency", False, "missing emergency action")]
        with patch.object(healthbench_e2e, "run_service_checks", return_value=failed), patch("builtins.print"):
            exit_code = healthbench_e2e.main(["--skip-mcp"])

        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
