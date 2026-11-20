"""Live smoke test (m4-contract §10): seed 01 in a real sandbox, real models through Portkey.

Answers research questions RQ1 (can the second model continue the first one's conversation and
call tools?) and RQ2 (what does a call cost; free models should report 0). Skipped, with a reason,
unless `PORTKEY_API_KEY` is set; also needs Docker and network. Run with `-m live`.

Writes `<worktree>/.m4-smoke/trajectory.json` and `costs.json` (neither contains the key).
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

import pytest
from m4_fakes import FAILING_TEST, SEED_REPO, FakeChannel, run_agent

from swf.agent.model import PhaseModel
from swf.agent.web_agent import WebAgent
from swf.config import load_agents_file
from swf.egress import EgressConfig, EgressInfo
from swf.sandbox import open_task_sandbox
from swf.worker import EnvRequest

pytestmark = [
    pytest.mark.live,
    pytest.mark.docker,
    pytest.mark.network,
    pytest.mark.skipif(
        not os.environ.get("PORTKEY_API_KEY"), reason="PORTKEY_API_KEY is not set (live models)"
    ),
]

SMOKE_DIR = Path(__file__).resolve().parent.parent / ".m4-smoke"


def test_both_models_call_tools_in_one_conversation(
    egress: EgressInfo, egress_config: EgressConfig, run_id: str, tmp_path: Path
) -> None:
    key = os.environ["PORTKEY_API_KEY"]
    workspace = tmp_path / "ws" / "repo"
    shutil.copytree(SEED_REPO, workspace)
    agents = load_agents_file(workspace)
    SMOKE_DIR.mkdir(exist_ok=True)
    trajectory = SMOKE_DIR / "trajectory.json"

    request = EnvRequest(
        task_id=f"swf-test-{run_id}-smoke-{uuid.uuid4().hex[:6]}",
        image=agents.config.image,
        workspace=workspace,
        memory_mb=agents.config.resources.memory_mb,
        cpus=agents.config.resources.cpus,
        worker_id=f"smoke-{run_id}",
    )
    env = open_task_sandbox(egress_config, request)
    try:
        for command in agents.config.setup:
            done = env.execute({"command": command}, timeout=600)
            assert done["returncode"] == 0, done

        model = PhaseModel.from_env()
        channel = FakeChannel(answers=lambda question: "Use your best judgement and continue.")
        agent = WebAgent(
            model,
            env,
            channel=channel,
            test_command=agents.config.test,
            allow=agents.config.allow,
            failing_test=FAILING_TEST,
            step_limit=40,
            approval_timeout_seconds=120,
            poll_seconds=0.1,
            output_path=trajectory,
            outputs_dir=SMOKE_DIR / "outputs",
        )
        result = run_agent(agent, agents_prose=agents.prose, test_command=agents.config.test)
    finally:
        env.cleanup()

    text = trajectory.read_text(encoding="utf-8")
    assert key not in text
    data = json.loads(text)
    replies = [m for m in data["messages"] if m.get("role") == "assistant"]
    assert replies, f"the models never answered (exit: {result})"

    def tool_calls_from(model_fragment: str) -> int:
        return sum(
            1
            for m in replies
            if model_fragment in str(m.get("extra", {}).get("model", ""))
            and m.get("extra", {}).get("actions")
        )

    investigate = tool_calls_from("ernie-5.5-flash")
    execute = tool_calls_from("glm-4.7-flash")
    costs = [float(m.get("extra", {}).get("cost", 0.0)) for m in replies]
    (SMOKE_DIR / "costs.json").write_text(
        json.dumps(
            {
                "exit_status": result.get("exit_status"),
                "tool_call_responses": {"ernie-5.5-flash": investigate, "glm-4.7-flash": execute},
                "cost_per_call_usd": costs,
                "total_usd": sum(costs),
                "statuses": channel.statuses(),
            },
            indent=2,
        ),
        encoding="utf-8",
        newline="\n",
    )
    assert all(c >= 0.0 for c in costs)  # RQ2: free models report 0, never a negative or a crash
    assert investigate >= 1, "RQ1: the investigate/plan model never produced a tool call"
    assert execute >= 1, "RQ1: the execute/verify model never produced a tool call"
