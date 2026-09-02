from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wiki_memory.engine import MemoryEngine
from wiki_memory.layout import init_memory
from wiki_memory.mcp_gateway import MCPGateway, MCP_PROTOCOL_VERSION, tool_definitions


class MCPGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "memory"
        init_memory(
            self.root,
            {
                "name": "Synthetic MCP",
                "language": "en",
                "sync_enabled": False,
                "vaults": [{"slug": "knowledge", "title": "Knowledge", "purpose": "Synthetic tests"}],
            },
        )
        self.gateway = MCPGateway(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _tool(self, request_id: int, name: str, arguments: dict) -> dict:
        response = self.gateway.handle(
            {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        )
        self.assertIsNotNone(response)
        assert response is not None
        self.assertNotIn("error", response)
        return json.loads(response["result"]["content"][0]["text"])

    def test_mcp_uses_the_canonical_private_capture_proposal_and_review_flow(self) -> None:
        initialized = self.gateway.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert initialized is not None
        self.assertEqual(initialized["result"]["protocolVersion"], MCP_PROTOCOL_VERSION)

        capture = self._tool(2, "memory_capture", {"vault": "knowledge", "text": "MCP sourced decision", "title": "MCP note"})
        captured_event = MemoryEngine(self.root).events.get(capture["event_id"])
        assert captured_event is not None
        self.assertEqual(captured_event.scope, "private")
        evidence_ref = capture["evidence_refs"][0]

        evidence = self._tool(3, "memory_get_evidence", {"reference": evidence_ref})
        self.assertIn("MCP sourced decision", evidence["content"])

        results = self._tool(4, "memory_search", {"query": "MCP sourced decision"})
        self.assertTrue(results)

        proposal = self._tool(
            5,
            "memory_propose_change",
            {
                "vault": "knowledge",
                "assertion": {"title": "MCP fact", "body": "Verified from MCP evidence"},
                "evidenceRefs": [evidence_ref],
            },
        )
        proposal_id = proposal["event"]["eventId"]
        accepted = self._tool(6, "memory_review", {"proposalEventId": proposal_id, "decision": "accept"})
        self.assertEqual(accepted["eventType"], "assertion.accepted")
        self.assertEqual(accepted["evidenceRefs"], [evidence_ref])

    def test_mcp_hides_review_when_the_capability_is_not_granted(self) -> None:
        self.assertIn("memory_review", {item["name"] for item in tool_definitions()})
        restricted = MCPGateway(self.root, include_review=False)
        tools = restricted.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert tools is not None
        self.assertNotIn("memory_review", {item["name"] for item in tools["result"]["tools"]})
        denied = restricted.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "memory_review", "arguments": {"proposalEventId": "missing", "decision": "accept"}},
            }
        )
        assert denied is not None
        self.assertEqual(denied["error"]["code"], -32000)


if __name__ == "__main__":
    unittest.main()
