"""Step-wise judgments and deterministic output assembly; no GT or old answers."""
from pathlib import Path
import copy,hashlib,json,re,shutil
from jsonschema import Draft202012Validator

STEP_SCHEMA={"type":"object","additionalProperties":False,"required":["step","verdict","summary","evidence"],"properties":{
 "step":{"type":"integer","minimum":1},"verdict":{"enum":["reasonable","error","uncertain"]},
 "summary":{"type":"string","minLength":1},"evidence":{"type":"array","minItems":1,"items":{"type":"object","additionalProperties":False,"required":["source_id","phase","locator","observation"],"properties":{
 "source_id":{"type":"string","minLength":1},"phase":{"enum":["before_action","after_action","decision","execution","metadata","final_observation","unknown"]},
 "locator":{"type":"string","minLength":1},"observation":{"type":"string","minLength":1}}}}}}

CHECKPOINT_SCHEMA={"type":"object","additionalProperties":False,"required":["schema","case_id","basis","checkpoints"],"properties":{
 "schema":{"const":"case-checkpoint-plan-v1"},"case_id":{"type":"string","minLength":1},
 "basis":{"type":"array","minItems":1,"uniqueItems":True,"items":{"enum":["instruction","identity","task_policy"]}},
 "checkpoints":{"type":"array","minItems":1,"maxItems":8,"items":{"type":"object","additionalProperties":False,
  "required":["id","type","object","requirement","required","depends_on","acceptance","evidence_expectation"],"properties":{
   "id":{"type":"string","pattern":"^CP[0-9]+$"},"type":{"enum":["goal","precondition","constraint","delivery"]},
   "object":{"type":"string","minLength":1},"requirement":{"type":"string","minLength":1},"required":{"type":"boolean"},
   "depends_on":{"type":"array","uniqueItems":True,"items":{"type":"string","pattern":"^CP[0-9]+$"}},
   "acceptance":{"type":"string","minLength":1},
   "evidence_expectation":{"type":"array","minItems":1,"uniqueItems":True,"items":{"enum":["screenshot","execution_result","module_output","metadata"]}}}}}}}


def validate_checkpoint_plan(plan,case_id=None,source_ids=()):
    """Validate task-level checkpoints without accepting trajectory evidence."""
    validator=Draft202012Validator(CHECKPOINT_SCHEMA)
    error=next(validator.iter_errors(plan),None)
    if error:
        path='.'.join(str(part) for part in error.absolute_path) or '$'
        raise ValueError(f'Checkpoint plan schema error at {path}: {error.message}')
    if plan['case_id']!=case_id:raise ValueError('Checkpoint plan case_id does not match the current case')
    checkpoints=plan['checkpoints'];ids=[item['id'] for item in checkpoints]
    if len(ids)!=len(set(ids)):raise ValueError('Checkpoint IDs must be unique')
    required=[item['id'] for item in checkpoints if item['required']]
    if not required:raise ValueError('At least one checkpoint must be required')
    known=set(ids);dependencies={item['id']:item['depends_on'] for item in checkpoints}
    for identifier,deps in dependencies.items():
        missing=[dep for dep in deps if dep not in known]
        if missing:raise ValueError(f'Checkpoint {identifier} depends on unknown {missing[0]}')
    visiting,visited=set(),set()
    def visit(identifier):
        if identifier in visiting:raise ValueError('Checkpoint dependencies must not contain a cycle')
        if identifier in visited:return
        visiting.add(identifier)
        for dependency in dependencies[identifier]:visit(dependency)
        visiting.remove(identifier);visited.add(identifier)
    for identifier in ids:visit(identifier)
    serialized=json.dumps(plan,ensure_ascii=False)
    for source_id in source_ids:
        source_id=str(source_id)
        if source_id and re.search(rf'(?<![A-Za-z0-9]){re.escape(source_id)}(?![A-Za-z0-9])',serialized):
            raise ValueError('Checkpoint plan must not cite trajectory evidence IDs')
    return required


def align_checkpoint_goals(plan,goals):
    """Require final goals to preserve required checkpoint ids and acceptance text."""
    if not isinstance(goals,list):raise ValueError('goals must be a list')
    goal_items=[item for item in goals if isinstance(item,dict)]
    for checkpoint in plan['checkpoints']:
        matches=[goal for goal in goal_items if goal.get('id')==checkpoint['id']]
        if not matches:
            if checkpoint['required']:raise ValueError(f"Required checkpoint {checkpoint['id']} is missing from final goals")
            continue
        if len(matches)!=1:raise ValueError(f"Checkpoint {checkpoint['id']} must map to exactly one final goal")
        goal=matches[0]
        if goal.get('object')!=checkpoint['object'] or goal.get('requirement')!=checkpoint['requirement']:
            raise ValueError(f"Goal {checkpoint['id']} must preserve the checkpoint object and requirement")


def bind_goal_steps(goals,evidence):
    """Expose concrete attempt steps and their evidence-derived span."""
    evidence_steps={item['id']:item['step'] for item in evidence if type(item.get('step')) is int}
    for goal in goals:
        if not isinstance(goal,dict):continue
        references=goal.get('evidence_ids') if isinstance(goal.get('evidence_ids'),list) else []
        steps={evidence_steps[ref] for ref in references if ref in evidence_steps}
        first=goal.get('first_seen_step')
        if type(first) is int and first>=1:steps.add(first)
        goal['steps']=sorted(steps)
        goal['step_range']=[goal['steps'][0],goal['steps'][-1]] if goal['steps'] else None


def load_submitted_checkpoint_plan(workspace,case):
    """Load the immutable plan and prove it was accepted before later helper work."""
    workspace=Path(workspace);path=workspace/'checkpoint_plan.json'
    if not path.is_file():raise ValueError('checkpoint_plan.json is missing; submit it before step judgments')
    plan=json.loads(path.read_text(encoding='utf-8-sig'))
    source_ids=[entry['source_id'] for entry in case['inventory']]
    validate_checkpoint_plan(plan,case['identity']['id'],source_ids)
    events_path=workspace/'io_events.jsonl'
    events=[]
    if events_path.is_file():
        for line in events_path.read_text(encoding='utf-8-sig').splitlines():
            try:event=json.loads(line)
            except json.JSONDecodeError as exc:raise ValueError('Malformed io_events.jsonl') from exc
            if not isinstance(event,dict):raise ValueError('Malformed io_events.jsonl')
            events.append(event)
    accepted=[event for event in events if event.get('mode')=='checkpoint' and event.get('accepted') is True]
    if len(accepted)!=1:raise ValueError('Exactly one accepted checkpoint submission is required')
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    if accepted[0].get('detail')!=f'Saved checkpoint plan sha256={digest}':
        raise ValueError('checkpoint_plan.json changed after its accepted submission')
    return plan,digest,events,accepted[0]


def _first_event_at(events,mode,accepted=True):
    values=[int(event['at']) for event in events if event.get('mode')==mode and (event.get('accepted') is True)==accepted and isinstance(event.get('at'),int)]
    return min(values) if values else None


def _first_evidence_delivery_at(workspace):
    path=Path(workspace).parent/'bridge_audit.json'
    if not path.is_file():return None
    try:audit=json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError,json.JSONDecodeError):return None
    times=[]
    for item in audit if isinstance(audit,list) else []:
        delivered=item.get('new_complete_pages') or item.get('delivered_gallery_sources') or item.get('derived_view_deliveries')
        if delivered and isinstance(item.get('time'),(int,float)):times.append(int(item['time']*1000))
    return min(times) if times else None


def maps_step(entry, step):
    return entry.get('step') == step or step in entry.get('steps', [])

def stage_contract(case,protocol,pages,workspace):
    workspace=Path(workspace)
    schema=copy.deepcopy(protocol.wire_schema())
    for key in ('coverage','evidence','process'):
        schema['properties'].pop(key);schema['required'].remove(key)
    goal_schema=schema.get('properties',{}).get('goals',{}).get('items')
    if isinstance(goal_schema,dict):
        for key in ('steps','step_range'):
            goal_schema.get('properties',{}).pop(key,None)
            if key in goal_schema.get('required',[]):goal_schema['required'].remove(key)
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
      'checkpoint_plan':{'path':'checkpoint_plan.json','schema_path':'checkpoint_plan.schema.json','submission_mode':'checkpoint',
                         'required_before':['step','derive','global','check']},
      'image_timing_policy':'Gallery order alone does not establish before/after. A source with registered steps belongs to each listed step; otherwise keep image evidence.step null unless the visible UI establishes timing.'}
    for name,value in [('step_index.json',index),('step_record.schema.json',STEP_SCHEMA),('global_summary.schema.json',schema),('checkpoint_plan.schema.json',CHECKPOINT_SCHEMA)]:
        (workspace/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    (workspace/'step_judgments').mkdir()
    return index

PROMPT_VERSION = "stepwise-prompt-r5"
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


def stage_local_preflight(workspace):
    """Stage the frozen assembler/validator support used by io.py check."""
    engine = Path(__file__).resolve().parent
    skill = engine.parent / "skills/v3"
    destination = Path(workspace) / ".preflight"
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("preflight.py", "stepwise_support_v2.py", "validate_result.py", "normalization.py"):
        shutil.copyfile(engine / name, destination / name)
    shutil.copytree(engine / "harness", destination / "harness",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(skill / "gui-trajectory-attribution/references", destination / "references")
    shutil.copyfile(skill / "manifest.json", destination / "skill_manifest.json")
    return destination

def assemble(workspace,case,receipt,*,write_audit=True):
    workspace=Path(workspace);schema_validator=Draft202012Validator(STEP_SCHEMA)
    checkpoint_plan,checkpoint_sha,io_events,checkpoint_event=load_submitted_checkpoint_plan(workspace,case)
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
            if source['source'] not in delivered:raise ValueError(
                f"Undelivered evidence source {source['source_id']}; read its page or image and confirm the delivery ticket before citing it")
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
    align_checkpoint_goals(checkpoint_plan,projection.get('goals'))
    bind_goal_steps(projection['goals'],evidence)
    missing=[e['source'] for e in case['inventory'] if e['source'] not in delivered]
    log_steps=[step for step in case['available_steps'] if any(e['kind']=='log' and maps_step(e,step) for e in case['inventory']) and all(e['source'] in delivered for e in case['inventory'] if e['kind']=='log' and maps_step(e,step))]
    projection.update(evidence=evidence,process=process,coverage={
       'log_steps_read':log_steps,'screenshots_viewed':[e['source_id'] for e in case['inventory'] if e['kind']=='image' and e['source'] in delivered],
       'missing':missing,'truncations':[],'unresolved':[],
       'scan_complete':not missing and set(log_steps)==set(case['available_steps'])})
    checkpoint_at=checkpoint_event.get('at')
    first_step_at=_first_event_at(io_events,'step')
    first_derive_at=_first_event_at(io_events,'derive')
    first_delivery_at=_first_evidence_delivery_at(workspace)
    def before(checkpoint_time,other_time):
        return None if checkpoint_time is None or other_time is None else checkpoint_time<=other_time
    if write_audit:
        audit={
          'records':len(records),'evidence_items':len(evidence),'process_items':len(process),'applied_additive_revisions':applied_revisions,
          'source_step_mapping':'original inventory only; null image steps preserved','coverage_from_runtime_receipt':True,
          'model_semantic_fields_unchanged':True,'no_prior_prediction_or_gt':True,'post_validator_model_repair':False,
          'checkpoint_plan_sha256':checkpoint_sha,'checkpoint_count':len(checkpoint_plan['checkpoints']),
          'required_checkpoint_ids':[item['id'] for item in checkpoint_plan['checkpoints'] if item['required']],
          'checkpoint_steps':{item.get('id'):item.get('steps',[]) for item in projection['goals'] if isinstance(item,dict)},
          'checkpoint_step_ranges':{item.get('id'):item.get('step_range') for item in projection['goals'] if isinstance(item,dict)},
          'final_goal_ids':[item.get('id') for item in projection['goals'] if isinstance(item,dict)],
          'checkpoint_goal_alignment':'exact_id_object_requirement_for_required_checkpoints',
          'checkpoint_submitted_before_step':before(checkpoint_at,first_step_at),
          'checkpoint_submitted_before_first_derived_view':before(checkpoint_at,first_derive_at),
          'checkpoint_submitted_before_first_evidence_delivery':before(checkpoint_at,first_delivery_at),
          'checkpoint_gate_scope':'helper submissions and derived views only; raw file reads are not prevented',
          'local_verdict_counts':{k:sum(r['verdict']==k for r in records) for k in ('reasonable','error','uncertain')}
        }
        (workspace.parent/'stepwise_assembly_audit.json').write_text(json.dumps(
            audit,ensure_ascii=False,indent=2),encoding='utf-8')
    return projection
