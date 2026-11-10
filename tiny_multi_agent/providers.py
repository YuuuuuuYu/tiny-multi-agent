"""Translate between a shared text prompt and each vendor's CLI."""

import asyncio
import json
import os
import shutil
import signal
from contextlib import suppress
from dataclasses import dataclass
from tempfile import TemporaryDirectory


@dataclass(frozen=True)
class Agent:
    provider: str
    model: str | None = None

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}" if self.model else self.provider


class ProviderError(Exception):
    """A CLI could not produce an answer."""


COMMANDS = {
    "codex": (
        "codex", "--ask-for-approval", "never", "exec",
        "--sandbox", "read-only", "--skip-git-repo-check",
        "--ephemeral", "--json", "-c", 'sqlite_home="."',
    ),
    "claude": (
        "claude", "--print", "--output-format", "json",
        "--no-session-persistence", "--safe-mode", "--tools", "",
    ),
    "gemini": (
        "gemini", "--approval-mode", "plan", "--output-format", "json",
        "--extensions", "none",
    ),
    "agy": (
        "agy", "--mode", "plan", "--disable-slash-commands",
        "--input-format", "stream-json", "--output-format", "stream-json",
    ),
}


def parse_answer(provider: str, stdout: str) -> str:
    try:
        if provider in {"codex", "agy"}:
            answer = None
            # Read the final answer without forwarding tool or progress events.
            for line in stdout.split("\n"):
                if not line.strip():
                    continue
                event = json.loads(line)
                if provider == "agy" and event.get("event") == "result":
                    result = event["result"]
                    if result["status"] != "SUCCESS":
                        raise ProviderError(f'CLI reported status {result["status"]}')
                    answer = result["response"]
                elif event.get("type") == "turn.failed":
                    raise ProviderError("Codex reported a failed turn")
                elif event.get("type") == "item.completed":
                    item = event["item"]
                    if item.get("type") == "agent_message":
                        answer = item["text"]
        else:
            result = json.loads(stdout)
            if result.get("is_error") or result.get("error"):
                raise ProviderError("CLI reported an error in its JSON response")
            field = "result" if provider == "claude" else "response"
            answer = result.get(field)
    except (ValueError, AttributeError, KeyError, TypeError) as error:
        raise ProviderError("CLI returned invalid structured output") from error

    if not isinstance(answer, str) or not answer.strip():
        raise ProviderError("CLI returned no final answer")
    return answer.strip()


async def invoke(agent: Agent, prompt: str, timeout: float) -> str:
    if agent.provider == "mock":
        try:
            return await asyncio.wait_for(mock_answer(agent, prompt), timeout)
        except TimeoutError as error:
            raise ProviderError(f"timed out after {timeout:g}s") from error

    command = list(COMMANDS[agent.provider])
    executable = shutil.which(command[0])
    if executable is None:
        raise ProviderError(f"{command[0]} is missing from PATH")
    command[0] = os.path.abspath(executable)
    if agent.model:
        command.extend(["--model", agent.model])
    if agent.provider == "codex":
        command.append("-")  # Explicitly read the prompt from stdin.
    elif agent.provider == "agy":
        prompt = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
    elif agent.provider == "gemini":
        command.extend(["--prompt", "Follow the instructions in the supplied JSON."])
    else:
        command.extend(["--", "Follow the instructions in the supplied JSON."])

    # Run outside the caller's repository. Vendor CLIs still use local auth.
    with TemporaryDirectory(prefix="tiny-agent-") as cwd:
        try:
            process = await asyncio.create_subprocess_exec(
                *command, cwd=cwd, start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise ProviderError(f"cannot start CLI: {error.strerror}") from error

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(prompt.encode()), timeout,
            )
        except (TimeoutError, asyncio.CancelledError) as error:
            # Kill the process group so a CLI child cannot keep its pipes open.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.communicate()
            if isinstance(error, TimeoutError):
                raise ProviderError(f"timed out after {timeout:g}s") from error
            raise

    if process.returncode:
        diagnostic = stderr.decode(errors="replace").strip().splitlines()
        detail = f": {diagnostic[-1][:300]}" if diagnostic else ""
        raise ProviderError(f"CLI exited with code {process.returncode}{detail}")
    return parse_answer(agent.provider, stdout.decode(errors="replace"))


async def mock_answer(agent: Agent, prompt: str) -> str:
    """Produce deterministic example text and a visible timing difference."""
    data = json.loads(prompt)
    if data["stage"] == "review":
        await asyncio.sleep(0.2)
        return f"{agent.model or 'mock'} review: add a concrete example."
    await asyncio.sleep(0.05)
    if data["stage"] == "synthesis":
        return f'{data["draft"]}\n\nApplied {len(data["reviews"])} review(s).'
    return f'Mock draft for: {data["task"]}'
