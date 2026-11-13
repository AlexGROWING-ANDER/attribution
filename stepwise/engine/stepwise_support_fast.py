"""Prepare the platform submission helper and render the unified stepwise prompt."""
from pathlib import Path
import platform_runtime
import stepwise_support_v2 as base

assemble = base.assemble
PROMPT_VERSION = base.PROMPT_VERSION
RUNTIME = platform_runtime.current_runtime()
HELPER_NAME = RUNTIME.helper_name
SUBMISSION_PROTOCOL_PATH = RUNTIME.protocol_path


def build_prompt(case, protocol, pages, workspace):
    prompt = base.build_prompt(case, protocol, pages, workspace)
    script = Path(__file__).with_name(HELPER_NAME).read_bytes()
    (Path(workspace) / HELPER_NAME).write_bytes(script)
    (Path(workspace) / "submission_protocol.md").write_bytes(RUNTIME.submission_protocol_bytes())
    return prompt
