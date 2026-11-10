import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tiny_multi_agent.providers import Agent, ProviderError, invoke, parse_answer
from tiny_multi_agent.workflow import run


FAKE_CLI = """
import json
import pathlib
import sys
import time

provider = pathlib.Path(sys.argv[0]).name
payload = json.load(sys.stdin)
if provider == "agy":
    assert payload["event"] == "user"
    payload = json.loads(payload["message"]["content"])

assert pathlib.Path.cwd().name.startswith("tiny-agent-")
assert sys.argv[sys.argv.index("--model") + 1] == "test-model"
stage = payload["stage"]
if stage == "timeout":
    time.sleep(10)

if provider == "codex":
    assert sys.argv[-1] == "-"
    print(json.dumps({"type": "item.completed", "item": {
        "type": "agent_message", "text": "intermediate message",
    }}))
    print(json.dumps({"type": "item.completed", "item": {
        "type": "agent_message", "text": "final codex",
    }}))
elif provider == "agy":
    print(json.dumps({"event": "init"}))
    print(json.dumps({"event": "result", "result": {
        "status": "SUCCESS", "response": "final agy",
    }}))
else:
    field = "result" if provider == "claude" else "response"
    print(json.dumps({field: f"final {provider}"}))
"""


class ProvidersTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for provider in ("codex", "claude", "gemini", "agy"):
            cli = directory / provider
            cli.write_text(f"#!{sys.executable}\n{FAKE_CLI}", encoding="utf-8")
            cli.chmod(0o700)
        self.enterContext(patch.dict(os.environ, {"PATH": str(directory)}))

    async def test_cli_protocols(self):
        for provider in ("codex", "claude", "gemini", "agy"):
            with self.subTest(provider=provider):
                result = await invoke(Agent(provider, "test-model"), '{"stage":"draft"}', 5)
                self.assertEqual(f"final {provider}", result)

    async def test_cli_timeout(self):
        with self.assertRaisesRegex(ProviderError, "timed out"):
            await invoke(Agent("codex", "test-model"), '{"stage":"timeout"}', 0.2)

    def test_bad_output_is_a_provider_error(self):
        with self.assertRaisesRegex(ProviderError, "invalid structured output"):
            parse_answer("codex", "bad json")
        with self.assertRaisesRegex(ProviderError, "reported an error"):
            parse_answer("claude", '{"is_error":true,"result":"unavailable"}')


class WorkflowTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_draft_and_bounded_concurrency(self):
        active = peak = 0
        synthesis = None

        async def fake_call(agent, prompt, timeout):
            nonlocal active, peak, synthesis
            data = json.loads(prompt)
            if data["stage"] == "draft":
                return "0123456789"
            if data["stage"] == "synthesis":
                synthesis = data
                return "final"
            self.assertEqual("0123…", data["draft"])
            self.assertNotIn("reviews", data)
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return f"{agent.model} feedback"

        lead = Agent("mock")
        reviewers = [Agent("mock", name) for name in ("alpha", "bravo", "charlie")]
        with patch("tiny_multi_agent.workflow.invoke", side_effect=fake_call):
            for jobs in (1, 2):
                with self.subTest(jobs=jobs):
                    peak = 0
                    report = await run("task", lead, reviewers, jobs=jobs, context_chars=5)
                    self.assertEqual(jobs, peak)
                    self.assertEqual("final", report.answer)
                    self.assertEqual(5, len(report.steps))
                    self.assertEqual(["alph…", "brav…", "char…"],
                                     [review["text"] for review in synthesis["reviews"]])

    async def test_reviewer_failure_can_preserve_the_draft(self):
        async def fake_call(agent, prompt, timeout):
            stage = json.loads(prompt)["stage"]
            if agent.model == "broken":
                raise ProviderError("missing")
            return stage

        lead = Agent("mock")
        bad = Agent("mock", "broken")
        good = Agent("mock", "working")
        with patch("tiny_multi_agent.workflow.invoke", side_effect=fake_call):
            partial = await run("task", lead, [bad, good])
            fallback = await run("task", lead, [bad])
            single = await run("task", lead, [])
        self.assertTrue(partial.degraded)
        self.assertEqual("synthesis", partial.answer)
        self.assertTrue(fallback.degraded)
        self.assertEqual("draft", fallback.answer)
        self.assertEqual(2, len(fallback.steps))
        self.assertFalse(single.degraded)
        self.assertEqual(1, len(single.steps))
