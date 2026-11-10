from __future__ import annotations
import copy,json,hashlib,secrets,time
from jsonschema import Draft202012Validator

MAIN_MODEL='deepseek-v4.1-flash'
# Route Codex approval requests to the configured DeepSeek surrogate.
ROUTES={'deepseek-v4.1-flash':{'purpose':'main_agent','upstream_model':'deepseek-v4.1-flash','read_timeout_seconds':180,'max_tokens':4096},
        'codex-auto-review':{'purpose':'approval_surrogate','upstream_model':'deepseek-v4.1-flash','read_timeout_seconds':70,'max_tokens':2048}}

def convert(body):
    incoming=body.get('model')
    if incoming not in ROUTES:raise ValueError('Unapproved incoming model route: '+str(incoming))
    route=copy.deepcopy(ROUTES[incoming]);route['incoming_model']=incoming
    if body.get('previous_response_id'):raise ValueError('Full conversation replay required')
    msgs=[];pending_images=[];alias={};unsupported_tools=[]
    def flush():
        if pending_images:msgs.append({'role':'user','content':list(pending_images)});pending_images.clear()
    def parts(items):
        if isinstance(items,str):return [{'type':'text','text':items}]
        out=[]
        for p in items or []:
            if p.get('type') in ('input_text','output_text','text'):out.append({'type':'text','text':p.get('text','')})
            elif p.get('type')=='input_image':
                uri=p.get('image_url')
                if not isinstance(uri,str) or not uri.startswith('data:image/'):raise ValueError('Only inline image bytes supported')
                image={'url':uri}
                if p.get('detail') is not None:image['detail']=p['detail']
                out.append({'type':'image_url','image_url':image})
            else:raise ValueError('Unsupported content type: '+str(p.get('type')))
        return out
    def function(t,ns=None):
        name=(ns+'__' if ns else '')+t['name'];alias[name]={'name':t['name'],'namespace':ns}
        return {'type':'function','function':{'name':name,'description':t.get('description',''),'parameters':t.get('parameters',{'type':'object','properties':{}})}}
    tools=[]
    for t in body.get('tools',[]):
        if t.get('type')=='function':tools.append(function(t))
        elif t.get('type')=='namespace':
            for f in t.get('tools',[]):
                if f.get('type')!='function':raise ValueError('Unsupported function in namespace')
                tools.append(function(f,t['name']))
        else:unsupported_tools.append({'type':t.get('type'),'name':t.get('name'),'disposition':'not expressible as Chat function; not offered upstream'})
    if body.get('instructions'):msgs.append({'role':'system','content':body['instructions']})
    inp=body.get('input',[])
    if isinstance(inp,str):inp=[{'type':'message','role':'user','content':inp}]
    seen=set()
    for i in inp:
        typ=i.get('type','message')
        if typ=='message':flush();msgs.append({'role':'system' if i.get('role')=='developer' else i.get('role','user'),'content':parts(i.get('content',[]))})
        elif typ=='function_call':
            flush();cid=i['call_id']
            if cid in seen:raise ValueError('Duplicate call_id')
            seen.add(cid);name=(i['namespace']+'__' if i.get('namespace') else '')+i['name']
            tc={'id':cid,'type':'function','function':{'name':name,'arguments':i['arguments']}}
            if msgs and msgs[-1]['role']=='assistant' and msgs[-1].get('tool_calls'):msgs[-1]['tool_calls'].append(tc)
            else:msgs.append({'role':'assistant','content':None,'tool_calls':[tc]})
        elif typ=='function_call_output':
            out=i['output']
            if isinstance(out,str):msgs.append({'role':'tool','tool_call_id':i['call_id'],'content':out})
            else:
                bs=parts(out);msgs.append({'role':'tool','tool_call_id':i['call_id'],'content':'\n'.join(p['text'] for p in bs if p['type']=='text')})
                imgs=[p for p in bs if p['type']=='image_url']
                if imgs:
                    pending_images.append({'type':'text','text':'Images returned by tool call '+i['call_id']})
                    pending_images.extend(bs)
        elif typ in ('reasoning','additional_tools'):
            # Reasoning state is provider-specific; additional_tools is host tool metadata.
            continue
        else:raise ValueError('Unsupported replay item '+str(typ))
    flush()
    merged=[]
    def text(c):return c if isinstance(c,str) else '\n'.join(b.get('text','') for b in c or [])
    for m in msgs:
        if m['role']=='assistant' and merged and merged[-1]['role']=='assistant' and (m.get('tool_calls') or merged[-1].get('tool_calls')):
            prev=merged[-1];prev['content']='\n'.join(t for t in [text(prev.get('content')),text(m.get('content'))] if t) or None;prev.setdefault('tool_calls',[]).extend(m.get('tool_calls') or [])
        else:merged.append(m)
    pending=set()
    for m in merged:
        if m['role']=='tool':
            if m['tool_call_id'] not in pending:raise ValueError('Unexpected/duplicate tool output ID')
            pending.remove(m['tool_call_id'])
        else:
            if pending:raise ValueError('Unanswered tool calls before next message')
            pending.update(t['id'] for t in m.get('tool_calls',[]))
    if pending:raise ValueError('Missing tool outputs')
    schema=(body.get('text') or {}).get('format',{})
    route['output_schema']=schema.get('schema') if schema.get('type')=='json_schema' else None
    if route['purpose']=='approval_surrogate':
        if not route['output_schema']:raise ValueError('Approval request requires its actual output schema')
        if tools:raise ValueError('This approval surrogate supports no autonomous tools')
    if route['output_schema']:
        Draft202012Validator.check_schema(route['output_schema'])
        # Transport output contract only: retain full original policy/action transcript.
        merged.append({'role':'system','content':'Return exactly one JSON object matching this supplied output schema. No Markdown or extra text. Do not skip the original instructions or security policy. Schema: '+json.dumps(route['output_schema'],ensure_ascii=False)})
    req={'model':route['upstream_model'],'messages':merged,'max_tokens':route['max_tokens']}
    if tools:req['tools']=tools
    # Explicit sampling controls require verified gateway support.
    for control in ['temperature','top_p','seed']:
        if control in body:raise ValueError('Gateway capability not confirmed for explicit '+control)
    if body.get('tool_choice','auto') not in ['auto','none']:raise ValueError('Unsupported explicit tool_choice')
    if tools:req['tool_choice']=body.get('tool_choice','auto')
    route.update(tool_aliases=alias,unsupported_tools=unsupported_tools,
        reasoning_control={'incoming':body.get('reasoning'),'mapped':False,'reason':'No confirmed company-DeepSeek reasoning-effort control; no equivalence claim'},
        structured_output_policy='schema-as-output-instruction plus deterministic validation; not decoder-constrained',
        role_conversion='developer to system',helper_model_identity_preserved=(incoming==route['upstream_model']))
    return req,route

def validate_reply(payload,route):
    if payload.get('model') not in {route['upstream_model'],'deepseek-v4-1-flash-261017'}:raise ValueError('Actual upstream model mismatch')
    choices=payload.get('choices') or []
    if len(choices)!=1:raise ValueError('Expected one upstream choice')
    ch=choices[0];msg=ch.get('message') or {};finish=ch.get('finish_reason')
    if finish not in ['stop','tool_calls']:raise ValueError('Nonterminal finish_reason '+str(finish))
    if msg.get('refusal'):raise ValueError('Upstream refusal')
    if not msg.get('content') and not msg.get('tool_calls'):raise ValueError('No public output; do not substitute reasoning')
    if route.get('output_schema'):
        if finish!='stop' or msg.get('tool_calls'):raise ValueError('Approval/structured result must be terminal JSON')
        value=json.loads(msg['content']);Draft202012Validator(route['output_schema']).validate(value)
    if msg.get('tool_calls'):
        valid=set(route['tool_aliases'])
        if any(t['function']['name'] not in valid for t in msg['tool_calls']):raise ValueError('Unknown returned tool')
    return {k:v for k,v in msg.items() if k!='reasoning_content'}

def response_events(msg,model,usage,aliases):
    rid='resp_'+secrets.token_hex(8);r={'id':rid,'object':'response','created_at':int(time.time()),'model':model,'status':'in_progress','output':[],'error':None};ev=[{'type':'response.created','response':copy.deepcopy(r)}];out=[]
    for c in msg.get('tool_calls') or []:
        a=aliases[c['function']['name']];item={'id':'fc_'+secrets.token_hex(6),'type':'function_call','status':'completed','call_id':c['id'],'name':a['name'],'arguments':c['function']['arguments']}
        if a['namespace']:item['namespace']=a['namespace']
        out.append(item)
    if msg.get('content'):out.append({'id':'msg_'+secrets.token_hex(6),'type':'message','status':'completed','role':'assistant','phase':'commentary' if msg.get('tool_calls') else 'final_answer','content':[{'type':'output_text','text':msg['content'],'annotations':[]}]})
    for j,item in enumerate(out):
        start=copy.deepcopy(item);start['status']='in_progress'
        if item['type']=='function_call':
            start['arguments']='';ev.append({'type':'response.output_item.added','output_index':j,'item':start});ev.extend([{'type':'response.function_call_arguments.delta','output_index':j,'item_id':item['id'],'delta':item['arguments']},{'type':'response.function_call_arguments.done','output_index':j,'item_id':item['id'],'arguments':item['arguments']}])
        else:
            start['content']=[];text=item['content'][0]['text'];ev.extend([{'type':'response.output_item.added','output_index':j,'item':start},{'type':'response.content_part.added','output_index':j,'item_id':item['id'],'content_index':0,'part':{'type':'output_text','text':'','annotations':[]}},{'type':'response.output_text.delta','output_index':j,'item_id':item['id'],'content_index':0,'delta':text},{'type':'response.output_text.done','output_index':j,'item_id':item['id'],'content_index':0,'text':text},{'type':'response.content_part.done','output_index':j,'item_id':item['id'],'content_index':0,'part':item['content'][0]}])
        ev.append({'type':'response.output_item.done','output_index':j,'item':copy.deepcopy(item)})
    r.update(status='completed',output=out)
    if usage:r['usage']={'input_tokens':usage.get('prompt_tokens',0),'output_tokens':usage.get('completion_tokens',0),'total_tokens':usage.get('total_tokens',0),'input_tokens_details':{'cached_tokens':(usage.get('prompt_tokens_details') or {}).get('cached_tokens',0)},'output_tokens_details':{'reasoning_tokens':(usage.get('completion_tokens_details') or {}).get('reasoning_tokens',0)}}
    ev.append({'type':'response.completed','response':r})
    for i,e in enumerate(ev):e['sequence_number']=i
    return ev
