"""Step-wise judgments and deterministic output assembly; no GT or old answers."""
from pathlib import Path
import copy,json,os
from jsonschema import Draft202012Validator

STEP_SCHEMA={"type":"object","additionalProperties":False,"required":["step","verdict","summary","evidence"],"properties":{
 "step":{"type":"integer","minimum":1},"verdict":{"enum":["reasonable","error","uncertain"]},
 "summary":{"type":"string","minLength":1},"evidence":{"type":"array","minItems":1,"items":{"type":"object","additionalProperties":False,"required":["source_id","phase","locator","observation"],"properties":{
 "source_id":{"type":"string","minLength":1},"phase":{"enum":["before_action","after_action","decision","execution","metadata","final_observation","unknown"]},
 "locator":{"type":"string","minLength":1},"observation":{"type":"string","minLength":1}}}}}}


def maps_step(entry, step):
    return entry.get('step') == step or step in entry.get('steps', [])

def stage_contract(case,protocol,pages,workspace):
    workspace=Path(workspace)
    schema=copy.deepcopy(protocol.wire_schema())
    for key in ('coverage','evidence','process'):
        schema['properties'].pop(key);schema['required'].remove(key)
    mapping=[]
    for step in case['available_steps']:
        entries=[e for e in case['inventory'] if maps_step(e,step)]
        page_names=[p['path'] for p in pages if any(maps_step(f['entry'],step) for f in p['members'])]
        mapping.append({'step':step,'log_pages':page_names,'source_ids':[e['source_id'] for e in entries]})
    mapped_pages={p['path'] for step in case['available_steps'] for p in pages if any(maps_step(f['entry'],step) for f in p['members'])}
    index={'steps':mapping,'other_log_pages':[p['path'] for p in pages if p['path'] not in mapped_pages],
      'images':[{'path':e['source'],'source_id':e['source_id'],'registered_step':e.get('step'),
                 'registered_steps':e.get('steps',[]),'display_index':e.get('display_index')}
                for e in case['inventory'] if e['kind']=='image'],
      'image_timing_policy':'Gallery order alone does not establish before/after. A source with registered steps belongs to each listed step; otherwise keep image evidence.step null unless the visible UI establishes timing.'}
    for name,value in [('step_index.json',index),('step_record.schema.json',STEP_SCHEMA),('global_summary.schema.json',schema)]:
        (workspace/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    (workspace/'step_judgments').mkdir()
    return index

PROMPT_VERSION = "stepwise-prompt-r2"
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts/stepwise_en.txt"


def render_prompt(identity):
    template = PROMPT_PATH.read_text(encoding="utf-8-sig")
    marker = "__TASK_IDENTITY_JSON__"
    if template.count(marker) != 1:
        raise ValueError("Prompt template must contain exactly one task identity marker")
    return template.replace(marker, json.dumps(identity, ensure_ascii=False))


def build_prompt(case, protocol, pages, workspace):
    stage_contract(case, protocol, pages, workspace)
    return render_prompt(case["identity"])

def assemble(workspace,case,receipt):
    workspace=Path(workspace);schema_validator=Draft202012Validator(STEP_SCHEMA)
    records=[];evidence=[];process=[];catalog={e['source_id']:e for e in case['inventory']}
    expected={f'step_{step:03d}.json' for step in case['available_steps']}
    actual={p.name for p in (workspace/'step_judgments').glob('step_*.json')}
    if expected!=actual:raise ValueError('Step records missing/unexpected: '+str(sorted(expected^actual)))
    revision_paths=list((workspace/'step_judgments').glob('revision_*.json'))
    allowed_revisions={f'revision_{step:03d}.json' for step in case['available_steps']}
    if any(p.name not in allowed_revisions for p in revision_paths):raise ValueError('Unexpected revision file')
    applied_revisions=[]
    delivered={e['source'] for e in receipt['events'] if e.get('success') and e.get('content_complete') and not e.get('truncated')}
    for step in case['available_steps']:
        value=json.loads((workspace/'step_judgments'/f'step_{step:03d}.json').read_text(encoding='utf-8-sig'))
        schema_validator.validate(value)
        if value['step']!=step:raise ValueError('Wrong step id in per-step file')
        revision_path=workspace/'step_judgments'/f'revision_{step:03d}.json'
        if revision_path.exists():
            revised=json.loads(revision_path.read_text(encoding='utf-8-sig'));schema_validator.validate(revised)
            if revised['step']!=step:raise ValueError('Revision step mismatch')
            if revised['verdict']!=value['verdict']:raise ValueError('This protocol permits additive evidence revisions only, not a silent verdict change')
            if revised['evidence'][:len(value['evidence'])]!=value['evidence']:raise ValueError('Revision changed original evidence or evidence-id positions')
            applied_revisions.append({'step':step,'file':str(revision_path),'added_evidence':len(revised['evidence'])-len(value['evidence']),'original_preserved':True})
            value=revised
        ids=[]
        for j,observation in enumerate(value['evidence'],1):
            source=catalog.get(observation['source_id'])
            if source is None:raise ValueError('Unknown evidence source_id')
            if source['source'] not in delivered:raise ValueError('Undelivered evidence source')
            identifier=f'E{step:02d}_{j}';ids.append(identifier)
            source_step=source.get('step')
            evidence_step=source_step if source_step is not None else (step if step in source.get('steps',[]) else None)
            evidence.append({'id':identifier,'source_id':source['source_id'],
              'kind':{'image':'screenshot','log':'module_output','metadata':'metadata'}[source['kind']],
              'step':evidence_step,'phase':observation['phase'],'locator':observation['locator'],'observation':observation['observation']})
        process.append({'start_step':step,'end_step':step,'summary':value['summary'],'evidence_ids':ids});records.append(value)
    projection=json.loads((workspace/'global_summary.json').read_text(encoding='utf-8-sig'))
    forbidden={'evidence','coverage','process'}&projection.keys()
    if forbidden:raise ValueError('Model repeated caller-owned sections: '+str(forbidden))
    missing=[e['source'] for e in case['inventory'] if e['source'] not in delivered]
    log_steps=[step for step in case['available_steps'] if any(e['kind']=='log' and maps_step(e,step) for e in case['inventory']) and all(e['source'] in delivered for e in case['inventory'] if e['kind']=='log' and maps_step(e,step))]
    projection.update(evidence=evidence,process=process,coverage={
       'log_steps_read':log_steps,'screenshots_viewed':[e['source_id'] for e in case['inventory'] if e['kind']=='image' and e['source'] in delivered],
       'missing':missing,'truncations':[],'unresolved':[],
       'scan_complete':not missing and set(log_steps)==set(case['available_steps'])})
    (workspace.parent/'stepwise_assembly_audit.json').write_text(json.dumps({
      'records':len(records),'evidence_items':len(evidence),'process_items':len(process),'applied_additive_revisions':applied_revisions,
      'source_step_mapping':'original inventory only; null image steps preserved','coverage_from_runtime_receipt':True,
      'model_semantic_fields_unchanged':True,'no_prior_prediction_or_gt':True,'post_validator_model_repair':False,
      'local_verdict_counts':{k:sum(r['verdict']==k for r in records) for k in ('reasonable','error','uncertain')}
    },ensure_ascii=False,indent=2),encoding='utf-8')
    return projection
