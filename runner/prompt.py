"""最终提示词拼装：身份 + 证据清单 + 提交 schema。"""
import json

from harness.protocol import OutputProtocol


def public_instruction(case: dict, protocol: OutputProtocol, structured_submission: bool=False) -> str:
    image_note=''
    if case.get('image_scope'):
        image_note=('\nPUBLIC IMAGE INVENTORY: The following ordered sources are exactly the human-review website gallery. '
            'Only these sources are visual input. Binary image attachments inside logs are outside this declared visual scope, '
            'not missing gallery files. Repeated reads refer to the same public sources.\n'+
            json.dumps(case['image_scope']['ordered_images'],ensure_ascii=False)+'\n')
    return ('Current task identity and evidence catalog:\n'+json.dumps(case['identity'],ensure_ascii=False)+
            '\n'+json.dumps(protocol.catalog.entries,ensure_ascii=False)+image_note+
            '\nOriginal contents are attached or available through the scoped evidence tool. '
            'Inspect every catalogued log and image before the final answer and report actual coverage. '
            'A catalog is not the file contents. Source IDs name files, not proof of reading.\n'
            '\nPUBLIC SUBMISSION CONTRACT (identical for all experimental groups):\n'+
            ('The result object inside submit_attribution arguments is the prediction: exactly ONE JSON object matching the schema below. '
             'The outer function transport is {result: prediction}; that outer wrapper is not part of the result schema. '
             'After accepted=true, finish; final conversational prose is not a second prediction. '
             if structured_submission else 'The final answer must be exactly ONE JSON object matching the submission schema below. ')+
            'This final-answer format does not replace evidence retrieval: when contents are not yet '
            'provided, first use the available evidence tool. Do not submit an empty placeholder. '
            'Do not include introductory text, Markdown fences, summaries or text after the object. '
            'This is the only model-output schema; any storage-format field names in reference documents '
            'are not an additional output schema. Include every required field and no extra fields. '
            'The caller binds case identity, versions and static inventory after submission: do not emit '
            'case, schema_version, skill_version or coverage fields absent from this submission schema.\n'
            'Evidence records have unique observation IDs in evidence[].id and a catalog source_id. '
            'Every evidence_ids item must reference an existing evidence[].id, not an undeclared file ID. '
            'Source IDs Sxxxx are used in evidence[].source_id and coverage.screenshots_viewed. '
            'coverage.log_steps_read contains decision step numbers, not file IDs. '
            'Error candidates have unique error_candidates[].id values. Attribution cause_id and '
            'secondary_causes must reference those candidate IDs, not prose descriptions; '
            'attribution step, stage and responsible_actor must agree with the referenced candidate. '
            'For a hierarchy taxonomy, fill l1/l2/l3 with a legal label path and set '
            'module/function/mechanism/object to null. For a dimensions taxonomy, set l1/l2/l3 '
            'to null and use its dimension fields. Use only schema-defined enums, types and nulls.\n'+
                '\nMODEL SUBMISSION JSON SCHEMA:\n'+
            json.dumps(protocol.wire_schema(),ensure_ascii=False))

