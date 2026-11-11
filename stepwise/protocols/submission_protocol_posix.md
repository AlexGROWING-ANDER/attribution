# Per-step submission protocol

Use the Skill for analysis and labels; this file defines submission mechanics. Keep evidence read-only and outputs in this workspace. Do not execute the phone task or access network, GT, previous predictions, other models or personal skills.

## Input and tool interface

- input_index.json lists sources; step_index.json maps steps to log_pages and lists other_log_pages and gallery images. IDs do not establish image timing.
- Deliver every gallery image separately through native view_image, including duplicates. Keep frozen cache bytes and dimensions; no resizing, contact sheets or OCR substitution.
- Read text files fully using native exec_command, one file per call, max_output_tokens at least 32000. Read other_log_pages before the step pass and assigned log_pages before submitting each step. Listings are not file contents.
- Independent text reads may share a turn; independent view_image calls may be batched 4-6 per turn, one call per image.

## Local records and submission

The two schema files define fields, enums and nulls. Per-step verdicts are local records; final attribution follows the Skill. Keep records compact, usually 1-3 evidence observations. Use registered source IDs; the runner binds source kinds and evidence IDs.

Submit steps in numeric order through io.py, which checks the next pending step, saves step_judgments/step_NNN.json and returns evidence IDs. Read the helper once if needed; do not edit it or overwrite committed steps. Use a real here-document:

```bash
python3 io.py step <<'JSON'
{small JSON record}
JSON
```

Submit one step at a time. Its save and next-step log reads may share a turn; wait for both before processing the next step. Correct rejected pending submissions; errors remain recorded.

After all materials and steps are complete, perform the Skill's synthesis and final review. Allow at most TWO targeted raw-source recheck batches; do not restart full reading.

New direct observations may be appended in step_judgments/revision_NNN.json. Keep the original step, verdict and evidence prefix, and explain additions in limitations. Preserve base records.

Submit global_summary.json using the global mode and the global schema. Reference returned evidence IDs; appended observations follow E{step:02d}_{j}. The caller constructs evidence, process, coverage and result.json; do not repeat those sections or write result.json/result.part*.json.

```bash
python3 io.py global <<'JSON'
{small JSON record}
JSON
```

## Check and completion

Run `python3 io.py check` once. On rejection, allow one targeted correction through the permitted paths and one recheck; preserve original steps. Do not search for alternative Python/Node tools, install dependencies or run/build a strict validator. The caller runs strict validation once, with no model repair afterwards.

Reply exactly DONE when materials and files are complete and the local check passes; otherwise report the blocker. Local corrections and normalization are recorded separately.
