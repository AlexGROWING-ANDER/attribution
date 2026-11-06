from __future__ import annotations
import argparse,base64,copy,hashlib,importlib.util,json,mimetypes,os,secrets,shutil,subprocess,sys,threading,time,urllib.request,urllib.error,urllib.parse
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
sys.dont_write_bytecode=True
HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
# 目录约定：本仓 src/ 下放运行器，src/engine/ 是随仓携带的依赖（study/ 与 harness/）
sys.path.insert(0,str(HERE.parent/'engine'))  # 随仓携带的 study/ 与 harness/
sys.path.insert(0,str(HERE))
from dotenv import dotenv_values
from jsonschema import Draft202012Validator
from study.materials import EvidenceStore
from harness.protocol import OutputProtocol,SourceCatalog,parse_wire_json
from prompt import public_instruction
MODEL=os.environ.get('ATTRIBUTION_MODEL') or 'deepseek-v4.1-flash'
MAIN_MAX_TOKENS=int(os.environ.get('MAIN_MAX_TOKENS') or 24000)
READ_TIMEOUT=int(os.environ.get('UPSTREAM_READ_TIMEOUT') or 600)


def codex_binary():
 """定位 codex CLI。

 优先用环境变量 CODEX_BINARY（JSON 数组或单个可执行文件路径），
 否则依次尝试 npm 全局安装的 codex.js（需 node）与 PATH 里的 codex。
 """
 env=os.environ.get('CODEX_BINARY')
 if env:
  env=env.strip()
  return json.loads(env) if env.startswith('[') else [env]
 node=shutil.which('node') or shutil.which('node.exe')
 npm=Path(os.environ.get('APPDATA') or '')/'npm'/'node_modules'/'@openai'/'codex'/'bin'/'codex.js'
 if node and npm.exists():return [node,str(npm)]
 exe=shutil.which('codex') or shutil.which('codex.cmd')
 if exe:return [exe]
 raise SystemExit('找不到 codex CLI：请设置环境变量 CODEX_BINARY（例如 ["node",".../codex.js"]）')


BINARY=codex_binary()

def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def atomic(p,d):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);q=p.with_name(p.name+'.'+secrets.token_hex(4)+'.tmp');q.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf8');os.replace(q,p)
def load_inputs(cid,version):
 """
 读取某案例的输入材料与技能包。

 目录约定（均可用环境变量覆盖）：
   SELECTION_DIR  默认 <repo>/selection，需含 READY.json 与其指向的 prediction_manifest.json
   SKILL_ROOT     默认 <repo>/skill_candidates，其下按版本号放 manifest.json 与技能目录
 """
 sel=Path(os.environ.get('SELECTION_DIR') or (ROOT/'selection'))
 ready=read(sel/'READY.json');p=Path(ready['prediction_manifest'])
 if not p.is_absolute():p=sel/p
 if sha(p)!=ready['prediction_manifest_sha256']:raise ValueError('Prediction manifest hash changed')
 manifest=read(p);case=next(c for c in manifest['cases'] if c['case_id']==cid)
 base=Path(os.environ.get('SKILL_ROOT') or (HERE/'skill'))
 sr=base/version if (base/version).is_dir() else base   # 支持 skill/<version>/ 与 skill/ 两种布局
 skill=sr/'gui-trajectory-attribution';sm=read(sr/'manifest.json')
 for rel,h in sm['files'].items():
  if sha(skill/rel)!=h:raise ValueError('Skill changed: '+rel)
 schema=read(skill/'references/output.schema.json')
 protocol=OutputProtocol(schema,case['identity'],sm['skill_version'],SourceCatalog(case['inventory']),case['available_steps'])
 store=EvidenceStore(Path(case['evidence_root']),case['inventory'],case['identity']['attempt'],manifest['image_delivery'],case['image_scope'])
 return case,manifest,skill,sm,protocol,store

def credentials():
 if os.environ.get('ATTRIBUTION_OFFLINE')=='1' or os.environ.get('NATIVE_RECOVERY_OFFLINE')=='1':
  return 'https://offline.invalid/v1/responses','OFFLINE_SYNTHETIC_NOT_A_CREDENTIAL'
 # 凭证由外部提供：默认读 <repo>/config/model.env，也可用 MODEL_ENV_FILE 指定
 env_file=Path(os.environ.get('MODEL_ENV_FILE') or (ROOT/'config'/'model.env'))
 if not env_file.exists():raise ValueError('缺少凭证文件：'+str(env_file)+'（需要 GROUND_URL 与 GROUND_API_KEY）')
 c=dotenv_values(str(env_file));base=(c.get('GROUND_URL') or '').rstrip('/');key=c.get('GROUND_API_KEY');del c
 if not key or not base.startswith('https://'):raise ValueError('Company credential unavailable')
 return base,key

def validator(skill):
 spec=importlib.util.spec_from_file_location('d60_result_validator',skill/'scripts/validate_result.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def finish(out,case,skill,protocol,raw,receipt,meta):
 try:
  prediction,parse_audit=parse_wire_json(raw);atomic(out/'first_prediction.json',prediction)
  result=protocol.compile(prediction);atomic(out/'result.json',result)
  errors=validator(skill).validate_result(result,references=skill/'references',evidence_root=Path(case['evidence_root']),access_receipt=out/'access_receipt.json')
  schema_errors=[e.message for e in Draft202012Validator(protocol.wire_schema()).iter_errors(prediction)]
  meta.update(state='validated' if not errors and not schema_errors else 'invalid',schema_errors=schema_errors,validation_errors=errors,parse_audit=parse_audit)
 except Exception as e:meta.update(state='invalid',error=type(e).__name__+': '+str(e)[:1800])
 atomic(out/'status.json',meta);return meta

def run_native(case,manifest,skill,sm,protocol,store,out,wall=850):
 out.mkdir(parents=True,exist_ok=False);w=out/'workspace';w.mkdir();home=out/'home';home.mkdir()
 shutil.copytree(skill,w/'.agents/skills/gui-trajectory-attribution')
 (w/'identity.json').write_text(json.dumps(case['identity'],ensure_ascii=False,indent=2),encoding='utf8')
 # Deterministic textual serialization identical to the existing evidence reader.
 # Binary attachments outside the human-visible gallery are replaced with scope markers.
 pages=[];texts={};events=[];image_paths={};staged=[];current='';members=[]
 def flush():
  nonlocal current,members
  if not members:return
  name='logs/page_%03d.txt'%(len(pages)+1);p=w/name;p.parent.mkdir(exist_ok=True)
  content='BEGIN_LOG_PAGE '+name+'\n'+current+'\nEND_LOG_PAGE '+name+'\n';p.write_text(content,encoding='utf8')
  pages.append({'path':name,'text':content,'members':members});current='';members=[]
 for e in case['inventory']:
  if e['kind']=='image':
   img=next(x for x in case['images'] if x['source_id']==e['source_id']);src=Path(img['delivery_path'])
   if sha(src)!=img['delivery_sha256']:raise ValueError('Delivery cache changed')
   dst=w/e['source'];dst.parent.mkdir(exist_ok=True,parents=True);shutil.copyfile(src,dst)
   image_paths[str(dst.resolve()).casefold()]={**e,'delivery_sha256':img['delivery_sha256'],'delivery_path':str(dst)}
  else:
   blocks,ev=store.read(e['source_id']);t='\n'.join(b['text'] for b in blocks if b['type']=='text')
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
 prompt=('Use $gui-trajectory-attribution for the following current attempt. This is read-only attribution; do not execute the original task. '
 'Load its required rule references natively. input_index.json lists ALL logs and ALL website-visible images. '
 'Read every logs/page_*.txt completely with native exec_command (set max_output_tokens at least 16000 for each page; avoid combined output truncation), '
 'and call native view_image separately for EVERY image listed. Do not resize, generate contact sheets, skip repeats or use OCR in place of images. '
 'Keep all operations within this workspace. No network, other models, installed personal skills, GT, evaluation files, historical answers or external filesystem exploration. '
 'A page marker or file listing is not its content. Finish only after all pages and images have been delivered. '
 'Use the following submission schema as the sole final-output format; the caller compiles its identity and inventory fields. '
 'A canonical storage schema inside the Skill is not a second final format. Return one JSON object only.\n'+public_instruction(case,protocol,False))
 (out/'prompt.txt').write_text(prompt,encoding='utf8')
 endpoint,key=credentials();_u=urllib.parse.urlsplit(endpoint);origin=_u.scheme+'://'+_u.netloc;token=secrets.token_urlsafe(28);aud=[];lock=threading.Lock();start=time.monotonic();deadline=start+wall;viewed=set();ranges={};page_hits=set()
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
      if page['text'].replace('\r\n','\n').strip() in normalized:newpages.append((page,call.get('call_id')))
  return restores,newimgs,newpages
 class Handler(BaseHTTPRequestHandler):
  def log_message(self,*a):pass
  def do_POST(self):
   item=None
   try:
    if self.headers.get('Authorization')!='Bearer '+token:self.send_error(403);return
    with lock:
     n=len(aud)+1
     if n>160:raise RuntimeError('Bounded160 upstream-call limit')
     item={'call':n,'time':time.time(),'sent_upstream':False};aud.append(item)
    d=out/'calls'/('%03d'%n);d.mkdir(parents=True)
    body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
    restorations,newimgs,newpages=restore_and_audit(body)
    purpose='main_agent' if body.get('tools') else 'aux'
    item.update(purpose=purpose,incoming_model=body.get('model'),upstream_model=MODEL,image_restorations=restorations)
    body['model']=MODEL
    body.setdefault('max_output_tokens',MAIN_MAX_TOKENS)
    def omit_images(v):
     if isinstance(v,list):return [omit_images(x) for x in v]
     if isinstance(v,dict):return {k:({'uri_sha256':hashlib.sha256(x.encode()).hexdigest(),'characters':len(x)} if k=='image_url' and isinstance(x,str) and x.startswith('data:') else omit_images(x)) for k,x in v.items()}
     return v
    atomic(d/'responses_public.json',omit_images(public(body)))
    atomic(d/'route.json',{'purpose':purpose,'upstream_model':MODEL})
    # 送达记账：这一轮模型确实拿到的日志页与图片
    if purpose=='main_agent':
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
    data=json.dumps(body,ensure_ascii=False).encode();item['request_bytes']=len(data)
    timeout=min(READ_TIMEOUT,deadline-time.monotonic())
    if timeout<2:raise TimeoutError('Case wall deadline')
    if os.environ.get('ATTRIBUTION_OFFLINE')=='1' or os.environ.get('NATIVE_RECOVERY_OFFLINE')=='1':
     raise RuntimeError('OFFLINE_PREFLIGHT_NO_UPSTREAM_CALL')
    item['sent_upstream']=True;atomic(out/'upstream_audit.json',aud)
    # 原样转发；唯一被改写过的字段是图片字节
    req=urllib.request.Request(origin+self.path,data=data,headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    up=urllib.request.urlopen(req,timeout=timeout)
    item.update(http_status=up.status,returned_model=up.headers.get('x-model') or MODEL)
    self.send_response(up.status);self.send_header('Content-Type',up.headers.get('Content-Type','text/event-stream'));self.send_header('Connection','close');self.end_headers()
    while True:
     chunk=up.read(2048)
     if not chunk:break
     self.wfile.write(chunk);self.wfile.flush()
    item['delivered_to_cli']=True
   except Exception as e:
    if item is not None:item['error']=clean(type(e).__name__+': '+str(e))[:1400]
    try:self.send_response(502);self.end_headers();self.wfile.write(json.dumps({'error':{'message':clean(str(e))[:300]}}).encode())
    except OSError:pass
   finally:
    if item is not None:item['finished_at']=time.time()
    atomic(out/'upstream_audit.json',aud)
 server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True;threading.Thread(target=server.serve_forever,daemon=True).start()
 env={k:v for k,v in os.environ.items() if not k.upper().startswith('CODEX_') and not any(s in k.upper() for s in ('API_KEY','TOKEN','SECRET'))};env.update(CODEX_HOME=str(home),USERPROFILE=str(home),HOME=str(home),D60_LOCAL_KEY=token)
 cmd=BINARY+['exec','--ignore-user-config','--skip-git-repo-check','--approve-for-me','--json','-C',str(w),'-m',MODEL,'-o',str(out/'answer.txt')]
 # 沙箱里只读放行 codex 自带运行时依赖目录；位置因机器而异，可用 CODEX_RUNTIME_DEP 覆盖
 runtime_dep=os.environ.get('CODEX_RUNTIME_DEP') or str(Path.home()/'.cache'/'codex-runtimes'/'codex-primary-runtime'/'dependencies'/'native')
 opts={'windows.sandbox':'mxc','default_permissions':'d60-workspace','permissions.d60-workspace.extends':':workspace','permissions.d60-workspace.filesystem':{':root':'deny',':minimal':'read',str(w):'write',str(home):'write',runtime_dep:'read'},'permissions.d60-workspace.network.enabled':False,'model_provider':'d60_company','model_providers.d60_company.name':'Company DeepSeek; explicit same-model approval surrogate','model_providers.d60_company.base_url':f'http://127.0.0.1:{server.server_port}/v1','model_providers.d60_company.env_key':'D60_LOCAL_KEY','model_providers.d60_company.wire_api':'responses','model_providers.d60_company.requires_openai_auth':False,'model_providers.d60_company.request_max_retries':0,'model_providers.d60_company.stream_max_retries':0,'model_providers.d60_company.stream_idle_timeout_ms':650000,'tool_output_token_limit':50000,'features.enable_request_compression':False,'features.skip_host_skill_discovery':True,'features.plugins':False,'features.remote_plugin':False,'features.multi_agent':False}
 def toml(v):
  if isinstance(v,dict):return '{'+','.join(json.dumps(k)+'='+toml(x) for k,x in v.items())+'}'
  return json.dumps(v,ensure_ascii=False)
 for k,v in opts.items():cmd+=['-c',k+'='+toml(v)]
 cmd+=['-'];atomic(out/'launch.json',{'argv':cmd,'family':'native_d60','skill_manifest_sha256':sha(skill.parent/'manifest.json'),'wall_seconds':wall,'max_calls':160,'permission_policy':'root deny with workspace and minimal runtime allow','native_instructions':True,'approval_surrogate':MODEL})
 proc=subprocess.Popen(cmd,cwd=w,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf8',errors='replace',creationflags=subprocess.CREATE_NO_WINDOW)
 atomic(out/'pid.json',{'pid':proc.pid,'start':time.time()});timed=False
 try:stdout,stderr=proc.communicate(prompt,timeout=max(1,deadline-time.monotonic()))
 except subprocess.TimeoutExpired:
  timed=True;subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW);stdout,stderr=proc.communicate(timeout=20)
 finally:server.shutdown();server.server_close()
 (out/'stdout.jsonl').write_text(clean(stdout),encoding='utf8');(out/'stderr.txt').write_text(clean(stderr),encoding='utf8');atomic(out/'access_receipt.json',receipt())
 raw=(out/'answer.txt').read_text(encoding='utf8') if (out/'answer.txt').exists() else ''
 complete=len({e['source'] for e in events})==len(case['inventory'])
 meta={'case_id':case['case_id'],'family':'native_d60','model':MODEL,'version':sm['skill_version'],'returncode':proc.returncode,'timed_out':timed,'coverage_complete':complete,'delivered_sources':len(events),'required_sources':len(case['inventory']),'delivered_images':len(viewed),'required_images':case['image_count'],'completed_pages':len(page_hits),'required_pages':len(pages),'upstream_calls':sum(bool(x.get('sent_upstream')) for x in aud),'upstream_requests':len(aud),'seconds':round(time.monotonic()-start,2),'run_directory':str(out),'approval_surrogate_not_official_model':True}
 if proc.returncode or timed or not raw:
  meta.update(state='runtime_failed',error=(stderr[-2200:] if stderr else 'No final output'))
  atomic(out/'status.json',meta);return meta
 return finish(out,case,skill,protocol,raw,receipt(),meta)

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--case',required=True);ap.add_argument('--version',default='v1');ap.add_argument('--out',required=True);ap.add_argument('--wall',type=int,default=850);a=ap.parse_args()
 case,manifest,skill,sm,protocol,store=load_inputs(a.case,a.version)
 result=run_native(case,manifest,skill,sm,protocol,store,Path(a.out),a.wall)
 print(json.dumps(result,ensure_ascii=True),flush=True)
if __name__=='__main__':main()
