"""Prepare the submission helper and render the unified stepwise prompt."""
from pathlib import Path
import stepwise_support_v2 as base

assemble = base.assemble
PROMPT_VERSION = base.PROMPT_VERSION
SUBMISSION_PROTOCOL_PATH = Path(__file__).resolve().parent.parent / "protocols/submission_protocol.md"


def build_prompt(case, protocol, pages, workspace):
    prompt = base.build_prompt(case, protocol, pages, workspace)
    script = Path(__file__).with_name("io.ps1").read_bytes()
    (Path(workspace) / "io.ps1").write_bytes(script)
    (Path(workspace) / "submission_protocol.md").write_bytes(SUBMISSION_PROTOCOL_PATH.read_bytes())
    return prompt
