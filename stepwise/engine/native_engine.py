from __future__ import annotations
import base64,copy,hashlib,importlib.util,json,os,secrets,shutil,subprocess,sys,threading,time,urllib.error
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
sys.dont_write_bytecode=True
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from stepwise_support_fast import build_prompt,assemble,PROMPT_VERSION
from jsonschema import Draft202012Validator
from harness.protocol import OutputProtocol,SourceCatalog,parse_wire_json
from study.materials import EvidenceStore
from responses_transport import forward_responses,route_request
import platform_runtime
MODEL='deepseek-v4.1-flash'
current_runtime=platform_runtime.current_runtime

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
_ALOCKS={}
_ALOCK_G=threading.Lock()

def _alock(p):
    with _ALOCK_G:
        return _ALOCKS.setdefault(str(p),threading.Lock())

def _rm(p):
 # 清理临时文件；失败时保留残留文件，继续处理后续步骤。
 try:os.remove(str(p))
 except OSError:pass


def atomic(p,d):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
 txt=json.dumps(d,ensure_ascii=False,indent=2)
 with _alock(p):
  last=None
  for _i in range(8):
   q=p.with_name(p.name+'.'+secrets.token_hex(4)+'.tmp')
   try:
    with open(q,'w',encoding='utf8') as f:
     f.write(txt);f.flush();os.fsync(f.fileno())
    os.replace(q,p);return
   except OSError as e:
    last=e
    _rm(q)
    wn=getattr(e,'winerror',None);en=getattr(e,'errno',None)
    if wn not in (5,32,33) and en not in (13,):
     raise
    time.sleep(0.05*(_i+1))
  try:
   p.write_text(txt,encoding='utf8')
  except OSError:
   if last is not None:raise last
   raise



def page_complete(text,normalized):
 """判断整页是否进了上下文；容忍换行差异与旧版 GBK 误解码。"""
 t=text.replace('\r\n','\n').strip()
 if t in normalized:return True
 try:
  legacy=t.encode('utf-8').decode('gbk',errors='replace').replace('\r\n','\n').strip()
  if legacy and legacy in normalized:return True
 except Exception:pass
 return False

def validator(skill):
 spec=importlib.util.spec_from_file_location('d60_result_validator',HERE/'validate_result.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

import re as _re
# 沙箱初始化与连接错误的分类特征；实际模型调用以调用日志为准。
INFRA_PAT = _re.compile(r'failed to create session|native mxc is unavailable|failed to prepare fs sandbox|'
                        r'error creating thread|code -32603|failed to prepare MXC sandbox|'
                        r'502 bad gateway|tunnel connection failed|bad gateway|connection reset by peer|'
                        r'sandbox-exec|sandbox_apply|seatbelt', _re.I)
def normalize_refs(prediction):
 """删掉引用了不存在 evidence id 的位置；success 时恢复机会置 not_applicable。"""
 evs=prediction.get('evidence') or []
 known={e.get('id') for e in evs if isinstance(e,dict)}
 def _clean(node):
  if isinstance(node,dict):
   for k,v in list(node.items()):
    if k=='evidence_ids' and isinstance(v,list):
     node[k]=[x for x in v if x in known]
    else:_clean(v)
  elif isinstance(node,list):
   for x in node:_clean(x)
 _clean(prediction)
 if ((prediction.get('outcome') or {}).get('decision'))=='success':
  ro=prediction.get('recovery_opportunity')
  if isinstance(ro,dict):
   for k in ('exists','attempted','outcome'):
    if k in ro:ro[k]='not_applicable'
 return prediction

def normalize_locator(prediction,case=None):
 """locator 里的每个文件名都必须与来源一致；不一致的删掉，只留匹配项与区域描述。

 wire 层 evidence 用的是 source_id（Sxxxx），不是 source 路径。
 早期版本只读 ev['source']，取不到就 continue，等于这条归一化从未生效。
 现在先用 case.inventory 把 source_id 映射回真实来源路径。
 """
 sid2src={}
 for e in (case or {}).get('inventory') or []:
  sid=e.get('source_id');s=e.get('source')
  if sid and s:sid2src[sid]=s
 pattern=_re.compile(r'(?<![\w.\-/\\])[\w\-]+(?:[/\\][\w\-]+)*\.(?:png|jpe?g|webp|json|txt|log)(?![\w.])',_re.I)
 for ev in prediction.get('evidence') or []:
  loc=ev.get('locator');src=str(ev.get('source') or sid2src.get(ev.get('source_id')) or '')
  if not isinstance(loc,str) or not loc:continue
  if not src:continue
  canonical=src.replace('\\','/')
  names={canonical}|{canonical.rsplit('/',1)[-1]}
  def _keep(m):
   name=m.group(0).replace('\\','/').lstrip('./')
   return m.group(0) if (canonical==name or canonical.endswith('/'+name) or name in names) else ''
  fixed=pattern.sub(_keep,loc)
  cleaned=' '.join(fixed.split())
  ev['locator']=cleaned or canonical
 return prediction

def normalize_steps(prediction,case):
 """evidence.step 以清单登记的来源步号为准；清单未登记（如图片）则不改。"""
 inv={}
 for e in case.get('inventory') or []:
  sid=e.get('source_id'); st=e.get('step')
  if sid and st is not None:inv[sid]=st
 for ev in prediction.get('evidence') or []:
  sid=ev.get('source_id')
  if sid in inv:ev['step']=inv[sid]
 return prediction

def normalize_actors(node):
 """把 actor 类字段统一成小写：Manager/manager 视为同一实体，属序列化差异。"""
 if isinstance(node,dict):
  for k,v in list(node.items()):
   if k in ('responsible_actor','actor') and isinstance(v,str) and v:
    node[k]=v.strip().lower()
   else:
    normalize_actors(v)
 elif isinstance(node,list):
  for x in node:normalize_actors(x)
 return node

def normalize_labels(prediction):
    """hierarchy 模板要求 labels 内 module/function/mechanism/object 显式 null。

    模型经常只写 l1/l2/l3 就交，schema 的 required 会整体拒收。
    output-contract 明确 hierarchy 模板下这四个要素应为 null，
    所以这里只"补 null 键"，不填任何取值，也不覆盖模型已给的值。
    """
    att = prediction.get('attribution') or {}
    if not isinstance(att, dict):
        return prediction
    for name in ('initial', 'recovery'):
        node = att.get(name)
        if not isinstance(node, dict):
            continue
        labels = node.get('labels')
        if not isinstance(labels, dict):
            continue
        for field in ('module', 'function', 'mechanism', 'object'):
            labels.setdefault(field, None)
    return prediction


def finish(out,case,skill,protocol,raw,receipt,meta):
 try:
  prediction,parse_audit=parse_wire_json(raw);atomic(out/'first_prediction.json',prediction)
  from normalization_audit import changes
  before_normalization=copy.deepcopy(prediction)
  normalize_refs(prediction)
  normalize_locator(prediction,case)
  normalize_steps(prediction,case)
  normalize_actors(prediction)
  normalize_labels(prediction)
  normalization={'deterministic_edits':changes(before_normalization,prediction),'compile_edits':None,'post_validator_model_repair':False}
  atomic(out/'normalization_audit.json',normalization)
  result=protocol.compile(prediction);atomic(out/'result.json',result)
  normalization['compile_edits']=changes(prediction,protocol.project(result))
  atomic(out/'normalization_audit.json',normalization)
  meta['normalization_changed']=bool(normalization['deterministic_edits'] or normalization['compile_edits'])
  errors=validator(skill).validate_result(result,references=skill/'references',evidence_root=Path(case['evidence_root']),access_receipt=out/'access_receipt.json')
  schema_errors=[e.message for e in Draft202012Validator(protocol.wire_schema()).iter_errors(prediction)]
  meta.update(state='validated' if not errors and not schema_errors else 'invalid',schema_errors=schema_errors,validation_errors=errors,parse_audit=parse_audit)
 except Exception as e:meta.update(state=('infra_failed' if INFRA_PAT.search(str(e)) else 'invalid'),error=type(e).__name__+': '+str(e)[:1800])
 atomic(out/'status.json',meta);return meta


def run_native(case,skill,sm,protocol,store,out,wall=850,*,command,endpoint,key):
 out.mkdir(parents=True,exist_ok=False)
 w=out/'workspace';w.mkdir(exist_ok=True);home=out/'home';home.mkdir(exist_ok=True)
 _sk=w/'.agents/skills/gui-trajectory-attribution'
 shutil.copytree(skill,_sk)
 for _f in (w/'.agents/skills/gui-trajectory-attribution/references').rglob('*.md'):
  try:_f.write_text(_f.read_text(encoding='utf-8-sig'),encoding='utf-8-sig')
  except Exception:pass
 (w/'identity.json').write_text(json.dumps(case['identity'],ensure_ascii=False,indent=2),encoding='utf8')
 # Deterministic textual serialization identical to the existing evidence reader.
 # Binary attachments outside the human-visible gallery are replaced with scope markers.
 pages=[];events=[];image_paths={};current='';members=[]
 def flush():
  nonlocal current,members
  if not members:return
  name='logs/page_%03d.txt'%(len(pages)+1);p=w/name;p.parent.mkdir(exist_ok=True)
  content='BEGIN_LOG_PAGE '+name+'\n'+current+'\nEND_LOG_PAGE '+name+'\n';p.write_text(content,encoding='utf-8-sig')
  pages.append({'path':name,'text':content,'members':members});current='';members=[]
 for e in sorted(case['inventory'],key=lambda e:(-1 if e.get('step') is None else e['step'],e['source_id'])):
  if e['kind']!='image' and members and members[-1]['entry'].get('step')!=e.get('step'):flush()
  if e['kind']=='image':
   img=next(x for x in case['images'] if x['source_id']==e['source_id']);src=Path(img['delivery_path'])
   if sha(src)!=img['delivery_sha256']:raise ValueError('Delivery cache changed')
   dst=w/e['source'];dst.parent.mkdir(exist_ok=True,parents=True);shutil.copyfile(src,dst)
   image_paths[str(dst.resolve()).casefold()]={**e,'delivery_sha256':img['delivery_sha256'],'delivery_path':str(dst)}
  else:
   blocks,_=store.read(e['source_id']);t='\n'.join(b['text'] for b in blocks if b['type']=='text')
   # Split very long file text into deterministic ranges; record complete coverage only
   # when every range, not merely a read invocation, reaches the main model.
   for offset in range(0,len(t),14000):
    part=t[offset:offset+14000];fragment={'entry':e,'offset':offset,'end':offset+len(part),'total':len(t),'text':part}
    marker='BEGIN_SOURCE_RANGE '+e['source_id']+' '+str(offset)+' '+str(offset+len(part))+'\n'+part+'\nEND_SOURCE_RANGE '+e['source_id']+'\n'
    if len(current)+len(marker)>18000:flush()
    current+=marker;members.append(fragment)
 flush()
 index={'identity':case['identity'],'available_steps':case['available_steps'],'sources':case['inventory'],'log_pages':[{'path':x['path'],'characters':len(x['text'])} for x in pages], 'images':[{'path':e['source'],'source_id':e['source_id']} for e in case['inventory'] if e['kind']=='image'],'normalization':'Original textual fields retained. In-log binary attachments outside the website gallery are replaced deterministically by the shared existing reader. Every named image has the existing q85 bytes at original dimensions.'}
 (w/'input_index.json').write_text(json.dumps(index,ensure_ascii=False,indent=2),encoding='utf8')
 prompt=build_prompt(case,protocol,pages,w)
 (out/'prompt.txt').write_text(prompt,encoding='utf8')
 token=secrets.token_urlsafe(28);aud=[];lock=threading.Lock();start=time.monotonic();deadline=start+wall;viewed=set();ranges={};page_hits=set()
 def clean(s):return s.replace(key,'[REDACTED]').replace(token,'[REDACTED_LOCAL]')
 def public(v):
  if isinstance(v,list):return [public(x) for x in v if not(isinstance(x,dict) and x.get('type')=='reasoning')]
  if isinstance(v,dict):return {k:public(x) for k,x in v.items() if k not in ('reasoning_content','encrypted_content')}
  return v
 def receipt():return store.receipt(events)
 def restore_and_audit(body):
  calls={};restores=[];newimgs=[];newpages=[]
  for item in body.get('input',[]):
   if item.get('type')=='function_call':calls[item['call_id']]=item
   elif item.get('type')=='function_call_output':
    call=calls.get(item.get('call_id'),{});output=item.get('output');name=call.get('name','')
    if name=='view_image' and isinstance(output,list):
     a=json.loads(call['arguments']);p=Path(a['path']);p=p if p.is_absolute() else w/p
     e=image_paths.get(str(p.resolve()).casefold());imgs=[b for b in output if b.get('type')=='input_image']
     if imgs:
      if e is None or len(imgs)!=1:raise ValueError('Native image not in current case gallery')
      raw=Path(e['delivery_path']).read_bytes();mime='image/webp' if raw[:4]==b'RIFF' else 'image/png'
      before=imgs[0]['image_url'];imgs[0]['image_url']='data:'+mime+';base64,'+base64.b64encode(raw).decode()
      restores.append({'source':e['source'],'q85_sha256':e['delivery_sha256'],'native_uri_sha256':hashlib.sha256(before.encode()).hexdigest(),'changed':before!=imgs[0]['image_url']})
      newimgs.append((e,call['call_id']))
    elif isinstance(output,str):
     normalized=output.replace('\r\n','\n')
     for page in pages:
      if page_complete(page['text'],normalized):newpages.append((page,call.get('call_id')))
  return restores,newimgs,newpages
 class Handler(BaseHTTPRequestHandler):
  def log_message(self,*a):pass
  def do_POST(self):
   item=None;headers_sent=False
   try:
    if self.headers.get('Authorization')!='Bearer '+token:self.send_error(403);return
    if self.path!='/v1/responses':self.send_error(404);return
    with lock:
     n=len(aud)+1
     if n>160:raise RuntimeError('Bounded160 upstream-call limit')
     item={'call':n,'time':time.time(),'sent_upstream':False};aud.append(item)
    d=out/'calls'/('%03d'%n);d.mkdir(parents=True)
    body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
    restorations,newimgs,newpages=restore_and_audit(body)
    body,route=route_request(body,MODEL);item.update(**route,image_restorations=restorations)
    # Keep hashes and a bounded public request, not duplicated base64 blobs per round.
    pub=public(body)
    def omit_images(v):
     if isinstance(v,list):return [omit_images(x) for x in v]
     if isinstance(v,dict):return {k:({'uri_sha256':hashlib.sha256(x.encode()).hexdigest(),'characters':len(x)} if k=='image_url' and isinstance(x,str) and x.startswith('data:') else omit_images(x)) for k,x in v.items()}
     return v
    atomic(d/'responses_public.json',omit_images(pub));atomic(d/'route.json',route)
    if os.environ.get('NATIVE_RECOVERY_OFFLINE')=='1':raise RuntimeError('OFFLINE_PREFLIGHT_NO_UPSTREAM_CALL')
    item['sent_upstream']=True;atomic(out/'bridge_audit.json',aud)
    def opened(status,content_type):
     nonlocal headers_sent
     self.send_response(status);self.send_header('Content-Type',content_type);self.send_header('Connection','close');self.end_headers();headers_sent=True
    def chunk(data):
     self.wfile.write(data);self.wfile.flush()
    try:
     forward_responses(endpoint,key,body,item,deadline,opened,chunk)
    except urllib.error.HTTPError as e:
     error_body=clean(e.read(16384).decode(errors='replace'));item.update(http_status=e.code,error_body=error_body[:1500])
     self.send_response(e.code);self.send_header('Content-Type','application/json');self.send_header('Connection','close');self.end_headers();headers_sent=True;self.wfile.write(error_body.encode());self.wfile.flush();raise
    if route['purpose']=='main_agent':
     for page,cid in newpages:
      if page['path'] not in page_hits:
       page_hits.add(page['path'])
       for f in page['members']:
        sid=f['entry']['source_id'];ranges.setdefault(sid,[]).append((f['offset'],f['end'],f['total']))
       item.setdefault('new_complete_pages',[]).append(page['path'])
     for e,cid in newimgs:
      if e['source'] not in viewed:
       viewed.add(e['source']);events.append({'source':e['source'],'sha256':e['sha256'],'operation':'deliver_image','success':True,'content_complete':True,'tool_call_id':cid,'delivery_sha256':e['delivery_sha256'],'truncated':False})
     for e in case['inventory']:
      if e['kind']=='image' or any(ev['source']==e['source'] for ev in events):continue
      rs=sorted(set(ranges.get(e['source_id'],[])))
      if rs and rs[0][0]==0 and all(a[1]==b[0] for a,b in zip(rs,rs[1:])) and rs[-1][1]==rs[-1][2]:events.append({'source':e['source'],'sha256':e['sha256'],'operation':'read_text','success':True,'content_complete':True,'tool_call_id':'complete-pages-call-'+str(n),'delivered_ranges':rs,'truncated':False})
     atomic(out/'access_receipt.json',receipt())
   except Exception as e:
    if item is not None:item['error']=clean(type(e).__name__+': '+str(e))[:1400]
    try:
     if not headers_sent:self.send_response(502);self.send_header('Connection','close');self.end_headers();self.wfile.write(json.dumps({'error':{'message':clean(str(e))[:300]}}).encode())
    except OSError:pass
   finally:
    self.close_connection=True
    if item is not None:item['finished_at']=time.time()
    atomic(out/'bridge_audit.json',aud)
 server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True;threading.Thread(target=server.serve_forever,daemon=True).start()
 runtime=platform_runtime.current_runtime()
 env={k:v for k,v in os.environ.items() if not k.upper().startswith('CODEX_') and not any(s in k.upper() for s in ('API_KEY','TOKEN','SECRET'))};env.update(CODEX_HOME=str(home),HOME=str(home),D60_LOCAL_KEY=token)
 if runtime.is_windows():env['USERPROFILE']=str(home)
 cmd=command+['exec','--ignore-user-config','--skip-git-repo-check']+runtime.approval_args()+['--json','-C',str(w),'-m',MODEL,'-o',str(out/'answer.txt')]
 read_roots=[Path(sys.executable).resolve().parent,Path(sys.prefix).resolve(),Path(getattr(sys,'base_prefix',sys.prefix)).resolve()] if not runtime.is_windows() else []
 if not runtime.is_windows():
  for part in command:
   path=Path(part).expanduser()
   if path.is_absolute() and path.exists():
    path=path.resolve();read_roots.append(path if path.is_dir() else path.parent)
 opts={'model_provider':'d60_company','model_providers.d60_company.name':'Company DeepSeek; explicit same-model approval surrogate','model_providers.d60_company.base_url':f'http://127.0.0.1:{server.server_port}/v1','model_providers.d60_company.env_key':'D60_LOCAL_KEY','model_providers.d60_company.wire_api':'responses','model_providers.d60_company.requires_openai_auth':False,'model_providers.d60_company.request_max_retries':1,'model_providers.d60_company.stream_max_retries':1,'model_providers.d60_company.stream_idle_timeout_ms':650000,'tool_output_token_limit':50000,'features.enable_request_compression':False,'features.skip_host_skill_discovery':True,'features.plugins':False,'features.remote_plugin':False,'features.multi_agent':False}
 runtime_dep=os.environ.get('CODEX_RUNTIME_DEP') if runtime.is_windows() else None
 opts.update(runtime.sandbox_options(w,home,runtime_dep,read_roots));opts.update(runtime.approval_options())
 def toml(v):
  if isinstance(v,dict):return '{'+','.join(json.dumps(k)+'='+toml(x) for k,x in v.items())+'}'
  return json.dumps(v,ensure_ascii=False)
 for k,v in opts.items():cmd+=['-c',k+'='+toml(v)]
 cmd+=['-'];atomic(out/'launch.json',{'argv':cmd,'family':'native_d60','platform':runtime.name,'submission_helper':runtime.helper_name,'skill_manifest_sha256':sha(skill.parent/'manifest.json'),'wall_seconds':wall,'max_calls':160,'permission_policy':'root deny with workspace and minimal runtime allow','native_instructions':True,'approval_surrogate':MODEL})
 proc=subprocess.Popen(cmd,cwd=w,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf8',errors='replace',**runtime.popen_kwargs())
 atomic(out/'pid.json',{'pid':proc.pid,'start':time.time()});timed=False
 try:stdout,stderr=proc.communicate(prompt,timeout=max(1,deadline-time.monotonic()))
 except subprocess.TimeoutExpired:
  timed=True;runtime.kill_process_tree(proc);stdout,stderr=proc.communicate(timeout=20)
 finally:server.shutdown();server.server_close()
 (out/'stdout.jsonl').write_text(clean(stdout),encoding='utf8');(out/'stderr.txt').write_text(clean(stderr),encoding='utf8')
 atomic(out/'access_receipt.json',receipt())
 raw=(out/'answer.txt').read_text(encoding='utf8') if (out/'answer.txt').exists() else ''
 try:
  raw=json.dumps(assemble(w,case,receipt()),ensure_ascii=False)
 except Exception as exc:
  terminal_error=str(aud[-1].get('error') or '') if aud else ''
  network_interruption=bool(proc.returncode or timed) and any(word in terminal_error.lower() for word in ('getaddrinfo','connection refused','connection reset','stream total deadline','name resolution','timed out','502 bad gateway','certificate_verify_failed','certificate verify failed','unexpected_eof','ssl:','winerror 10054','winerror 10060'))
  failure={'case_id':case['case_id'],'state':'infra_failed' if network_interruption else 'invalid','protocol':'stepwise_fast_v2','prompt_version':PROMPT_VERSION,'validator_patch_version':'answer-delivery-direction-v1','validator_sha256':sha(HERE/'validate_result.py'),'error':terminal_error if network_interruption else 'Step assembly: '+str(exc),'assembly_error':str(exc),'returncode':proc.returncode,'timed_out':timed,'seconds':round(time.monotonic()-start,2),'post_validator_model_repair':False}
  atomic(out/'status.json',failure);return failure
 complete=len({e['source'] for e in events})==len(case['inventory'])
 meta={'case_id':case['case_id'],'family':'native_d60','platform':runtime.name,'submission_helper':runtime.helper_name,'model':MODEL,'version':sm['skill_version'],'returncode':proc.returncode,'timed_out':timed,'coverage_complete':complete,'delivered_sources':len(events),'required_sources':len(case['inventory']),'delivered_images':len(viewed),'required_images':case['image_count'],'completed_pages':len(page_hits),'required_pages':len(pages),'upstream_calls':sum(bool(x.get('sent_upstream')) for x in aud),'bridge_requests':len(aud),'seconds':round(time.monotonic()-start,2),'run_directory':str(out),'approval_surrogate_not_official_model':True,'protocol':'stepwise_fast_v2','prompt_version':PROMPT_VERSION,'validator_patch_version':'answer-delivery-direction-v1','validator_sha256':sha(HERE/'validate_result.py'),'post_validator_model_repair':False}
 if (proc.returncode or timed) and not raw:
  _err=(stderr[-2200:] if stderr else 'No final output')
  meta.update(state='infra_failed' if INFRA_PAT.search(_err) else 'runtime_failed',error=_err)
  atomic(out/'status.json',meta);return meta
 return finish(out,case,skill,protocol,raw,receipt(),meta)
