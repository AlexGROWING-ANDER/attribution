from __future__ import annotations
import base64,copy,hashlib,importlib.util,json,os,secrets,shlex,shutil,subprocess,sys,threading,time,urllib.error
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
sys.dont_write_bytecode=True
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from stepwise_support_fast import build_prompt,assemble,PROMPT_VERSION
from jsonschema import Draft202012Validator
from PIL import Image
from harness.protocol import OutputProtocol,SourceCatalog,parse_wire_json
from harness.storage import atomic_json
from framework_architecture import install_framework_architecture,resolve_framework_architecture,select_framework_binding
from study.materials import EvidenceStore
from responses_transport import forward_responses,route_request
import platform_runtime
import normalization as canonical_normalization
DEFAULT_MODEL='deepseek-v4.1-flash'
current_runtime=platform_runtime.current_runtime
DERIVED_ROOT='derived_views'
DERIVED_SCHEMA='stepwise-derived-view-v1'
DERIVED_MIN_SCALE=1.0
DERIVED_MAX_SCALE=4.0
DERIVED_MAX_SIDE=8192
DERIVED_MAX_PIXELS=30_000_000
DERIVED_FIELDS={
 'schema','derived_view_id','source_id','parent_source','parent_source_sha256','parent_delivery_sha256',
 'parent_dimensions','box','scale','resampling','output_format','output','output_dimensions','output_sha256',
}


class DerivedViewError(ValueError):
 pass

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def validate_framework_snapshot_tree(root,source_files):
 expected={'snapshot.json','framework_index.json','framework_knowledge_contract.md'}|{f'source/{rel}' for rel in source_files}
 actual=set()
 for dirpath,dirnames,filenames in os.walk(root,followlinks=False):
  for name in dirnames+filenames:
   if (Path(dirpath)/name).is_symlink():raise ValueError('Framework knowledge snapshot contains a symlink')
  for name in filenames:
   actual.add((Path(dirpath)/name).relative_to(root).as_posix())
 if actual!=expected:
  raise ValueError('Framework knowledge snapshot tree mismatch: missing='+str(sorted(expected-actual))+' extra='+str(sorted(actual-expected)))

def resolve_framework_knowledge(binding,base,identity):
 """Validate an optional frozen framework source snapshot without making it case evidence."""
 if binding is None:return None
 if not isinstance(binding,dict) or binding.get('schema')!='framework-knowledge-binding-v1':
  raise ValueError('Unknown framework knowledge binding schema')
 framework=identity.get('framework')
 if binding.get('framework')!=framework:raise ValueError('Framework knowledge target differs from case identity')
 if binding.get('mode') not in ('hybrid','repo_only'):raise ValueError('Unsupported framework knowledge mode')
 root=Path(binding['snapshot_root']).expanduser()
 root=(root if root.is_absolute() else Path(base)/root).resolve()
 snapshot_path=root/'snapshot.json';index_path=root/'framework_index.json';contract_path=root/'framework_knowledge_contract.md'
 if not binding.get('index_sha256') or not binding.get('contract_sha256'):
  raise ValueError('Framework knowledge binding lacks index/contract hashes')
 for path in (snapshot_path,index_path,contract_path):
  if not path.is_file() or not path.resolve().is_relative_to(root):raise ValueError('Framework knowledge snapshot is incomplete')
 if hashlib.sha256(snapshot_path.read_bytes()).hexdigest()!=binding.get('snapshot_manifest_sha256'):
  raise ValueError('Framework knowledge snapshot metadata changed')
 snapshot=json.loads(snapshot_path.read_text(encoding='utf-8'))
 if snapshot.get('schema')!='framework-knowledge-snapshot-v1' or snapshot.get('framework')!=framework:
  raise ValueError('Framework knowledge snapshot identity mismatch')
 if snapshot.get('mode')!=binding.get('mode') or snapshot.get('repo_revision')!=binding.get('repo_revision'):
  raise ValueError('Framework knowledge binding metadata mismatch')
 if snapshot.get('tree_sha256')!=binding.get('tree_sha256'):raise ValueError('Framework knowledge tree digest mismatch')
 files=snapshot.get('files')
 if not isinstance(files,dict) or not files:raise ValueError('Framework knowledge snapshot has no source files')
 validate_framework_snapshot_tree(root,files)
 for rel,digest in files.items():
  path=(root/'source'/rel).resolve()
  if not path.is_relative_to(root/'source') or not path.is_file() or sha(path)!=digest:
   raise ValueError('Framework knowledge source changed: '+rel)
 index=json.loads(index_path.read_text(encoding='utf-8'))
 if index.get('schema')!='framework-source-index-v1' or index.get('framework')!=framework:
  raise ValueError('Framework source index identity mismatch')
 if sha(index_path)!=binding['index_sha256']:raise ValueError('Framework source index changed')
 if sha(contract_path)!=binding['contract_sha256']:raise ValueError('Framework knowledge contract changed')
 if index.get('tree_sha256')!=snapshot.get('tree_sha256') or index.get('repo_revision')!=snapshot.get('repo_revision') or index.get('mode')!=snapshot.get('mode'):
  raise ValueError('Framework source index digest mismatch')
 return {'snapshot_root':str(root),'mode':binding['mode'],'repo_revision':binding['repo_revision'],
         'tree_sha256':binding['tree_sha256'],'source_file_count':len(files),
         'snapshot_manifest_sha256':binding['snapshot_manifest_sha256'],
         'index_sha256':binding['index_sha256'],'contract_sha256':binding['contract_sha256']}

def install_framework_knowledge(case,workspace,out):
 knowledge=case.get('framework_knowledge')
 if not knowledge:return None
 source=Path(knowledge['snapshot_root']).resolve();dest=(Path(workspace)/'.framework-knowledge').resolve()
 if dest.exists():raise ValueError('Framework knowledge destination already exists')
 shutil.copytree(source,dest)
 snapshot=json.loads((dest/'snapshot.json').read_text(encoding='utf-8'))
 if sha(dest/'framework_index.json')!=knowledge['index_sha256'] or sha(dest/'framework_knowledge_contract.md')!=knowledge['contract_sha256']:
  raise ValueError('Installed framework knowledge index/contract changed')
 for rel,digest in snapshot['files'].items():
  path=(dest/'source'/rel).resolve()
  if not path.is_relative_to(dest/'source') or not path.is_file() or sha(path)!=digest:
   raise ValueError('Installed framework knowledge source changed: '+rel)
 binding={**knowledge,'path':'.framework-knowledge/framework_index.json',
          'contract_path':'.framework-knowledge/framework_knowledge_contract.md',
          'snapshot_manifest_sha256':sha(dest/'snapshot.json'),
          'authority':'framework implementation reference only; never current-attempt evidence'}
 atomic_json(out/'framework_knowledge_binding.json',binding)
 return binding


def install_framework_inputs(case,workspace,out):
 architecture=install_framework_architecture(case,workspace,out)
 knowledge=None if architecture else install_framework_knowledge(case,workspace,out)
 return knowledge,architecture


_ALOCKS={}
_ALOCK_G=threading.Lock()

def _alock(p):
    with _ALOCK_G:
        return _ALOCKS.setdefault(str(p),threading.Lock())

def _rm(p):
 # 清理临时文件；失败时保留残留文件，继续处理后续步骤。
 try:os.remove(str(p))
 except OSError:pass


def derived_output_size(size,box,scale):
 width=box[2]-box[0];height=box[3]-box[1]
 output=(max(1,int(width*scale+0.5)),max(1,int(height*scale+0.5)))
 if output[0]>DERIVED_MAX_SIDE or output[1]>DERIVED_MAX_SIDE:raise DerivedViewError('Derived view exceeds side limit')
 if output[0]*output[1]>DERIVED_MAX_PIXELS:raise DerivedViewError('Derived view exceeds pixel limit')
 return output


def load_derived_view(workspace,path,sources_by_id):
 """Load one registered derived view and prove it is a deterministic crop/resize of its parent."""
 workspace=Path(workspace).resolve();path=Path(path).resolve();root=(workspace/DERIVED_ROOT).resolve()
 if not path.is_relative_to(root) or path.suffix!='.png':raise DerivedViewError('Derived path must be a .png below derived_views/')
 metadata_path=path.with_name(path.name+'.json')
 if not metadata_path.is_file():raise DerivedViewError('Derived view has no registration metadata')
 resolved_metadata=metadata_path.resolve()
 if not resolved_metadata.is_relative_to(root):raise DerivedViewError('Derived view metadata is outside the derived view directory')
 try:metadata=json.loads(resolved_metadata.read_text(encoding='utf-8'))
 except Exception as exc:raise DerivedViewError('Derived view metadata is not valid JSON') from exc
 if not isinstance(metadata,dict) or set(metadata)!=DERIVED_FIELDS:raise DerivedViewError('Derived view metadata fields are invalid')
 if metadata['schema']!=DERIVED_SCHEMA:raise DerivedViewError('Derived view schema is invalid')
 source_id=metadata['source_id']
 if not isinstance(source_id,str):raise DerivedViewError('Derived view source ID is invalid')
 source=sources_by_id.get(source_id)
 if not source or source.get('kind')!='image':raise DerivedViewError('Derived view parent is not a gallery image')
 if metadata['parent_source']!=source.get('source'):raise DerivedViewError('Derived view parent source does not match the registry')
 if metadata['parent_source_sha256']!=source.get('sha256'):raise DerivedViewError('Derived view parent source SHA256 does not match the registry')
 registered_delivery=source.get('delivery_sha256')
 if registered_delivery is None:raise DerivedViewError('Derived view parent is not bound to a frozen delivery')
 parent=(workspace/str(source.get('source',''))).resolve()
 if not parent.is_relative_to(workspace) or not parent.is_file():raise DerivedViewError('Derived view parent is outside the workspace or missing')
 parent_delivery_sha256=hashlib.sha256(parent.read_bytes()).hexdigest()
 if parent_delivery_sha256!=registered_delivery or metadata['parent_delivery_sha256']!=registered_delivery:raise DerivedViewError('Derived view parent delivery bytes changed')
 relative=path.relative_to(workspace).as_posix()
 if metadata['derived_view_id']!=relative or metadata['output']!=relative:raise DerivedViewError('Derived view output path does not match its registration')
 if metadata['resampling'] not in ('none','lanczos') or metadata['output_format']!='png':raise DerivedViewError('Derived view operation is not supported')
 box=metadata['box']
 if (not isinstance(box,list) or len(box)!=4 or any(type(value) is not int for value in box)):raise DerivedViewError('Derived view box is invalid')
 scale=metadata['scale']
 if type(scale) not in (int,float) or not DERIVED_MIN_SCALE<=scale<=DERIVED_MAX_SCALE:raise DerivedViewError('Derived view scale is invalid')
 if (scale==1.0)!=(metadata['resampling']=='none'):raise DerivedViewError('Derived view resampling does not match its scale')
 try:
  with Image.open(parent) as parent_image:
   parent_image.load();parent_size=list(parent_image.size);width,height=parent_size;x0,y0,x1,y1=box
   if not (0<=x0<x1<=width and 0<=y0<y1<=height):raise DerivedViewError('Derived view box is outside the parent image')
   expected_size=derived_output_size(parent_size,box,scale)
   expected=parent_image.crop(box)
   if scale!=1.0:expected=expected.resize(expected_size,Image.Resampling.LANCZOS)
   expected_size=list(expected_size);expected_mode=expected.mode;expected_bytes=expected.tobytes()
  with Image.open(path) as actual:
   actual.load()
   if list(actual.size)!=expected_size or actual.mode!=expected_mode or actual.tobytes()!=expected_bytes:
    raise DerivedViewError('Derived view pixels are not the registered crop and scale')
 except DerivedViewError:raise
 except Exception as exc:raise DerivedViewError('Could not validate derived view image') from exc
 if metadata['parent_dimensions']!=parent_size:raise DerivedViewError('Derived view parent dimensions do not match')
 if metadata['output_dimensions']!=expected_size:raise DerivedViewError('Derived view output dimensions do not match')
 try:raw=path.read_bytes()
 except OSError as exc:raise DerivedViewError('Derived view bytes are unreadable') from exc
 if hashlib.sha256(raw).hexdigest()!=metadata['output_sha256']:raise DerivedViewError('Derived view bytes changed after registration')
 return metadata,raw


def resolve_native_image(path,workspace,image_paths,sources_by_id):
 resolved=Path(path).resolve()
 if str(resolved).casefold() in image_paths:return 'original',image_paths[str(resolved).casefold()],None
 root=(Path(workspace).resolve()/DERIVED_ROOT).resolve()
 if resolved.is_relative_to(root):
  try:return 'derived',load_derived_view(workspace,resolved,sources_by_id),None
  except Exception as exc:return 'rejected',None,type(exc).__name__+': '+str(exc)
 return 'rejected',None,'Image path is not a registered gallery image or derived view'


def image_rejection_message(reason):
 interpreter=str(Path(sys.executable).resolve())
 if os.name!='nt':interpreter=shlex.quote(interpreter)
 return ('Image rejected: only unchanged gallery images and registered derived views may be viewed. '
         'Deliver every original gallery image unchanged for evidence, or create a supplemental crop with '
         '`'+interpreter+' io.py derive --source-id <Sxxxx> --box x0,y0,x1,y1 --scale 2 --out derived_views/<name>.png` '
         'and view the returned output. Do not create images with other tools. '
         'Validation detail: '+str(reason))


def view_image_path(call,workspace):
 if not isinstance(call,dict):raise ValueError('Native view_image call is malformed')
 try:args=json.loads(call.get('arguments',''))
 except (TypeError,ValueError) as exc:raise ValueError('Native view_image arguments are not valid JSON') from exc
 if not isinstance(args,dict) or not isinstance(args.get('path'),str) or not args['path']:
  raise ValueError('Native view_image arguments must contain a path')
 path=Path(args['path'])
 return path if path.is_absolute() else Path(workspace)/path


def view_image_blocks(output):
 if not isinstance(output,list) or any(not isinstance(block,dict) for block in output):
  raise ValueError('Native view_image output is not a list of image objects')
 images=[block for block in output if block.get('type')=='input_image']
 if any(not isinstance(block.get('image_url'),str) for block in images):
  raise ValueError('Native view_image image entry has no image_url string')
 return images


def classify_posix_view_image(call,output,workspace,image_paths,sources_by_id):
 path=None;images=[]
 try:
  path=view_image_path(call,workspace);images=view_image_blocks(output)
  if not images:return 'ignored',None,None,path,images
  kind,payload,reason=resolve_native_image(path,workspace,image_paths,sources_by_id)
  if kind in ('original','derived') and len(images)!=1:
   return 'rejected',None,reason or 'Native view_image returned multiple images',path,images
  return kind,payload,reason,path,images
 except Exception as exc:return 'rejected',None,type(exc).__name__+': '+str(exc),path,images


def verified_delivery_bytes(entry):
 raw=Path(entry['delivery_path']).read_bytes()
 if hashlib.sha256(raw).hexdigest()!=entry['delivery_sha256']:raise ValueError('Original image delivery bytes changed')
 return raw


def restore_and_audit(body,workspace,pages,image_paths,sources_by_id,runtime,rejected_seen):
 calls={};restores=[];newimgs=[];newpages=[];derived=[];rejected=[]
 def reject_image(item,call,detail,path=None):
  item['output']=image_rejection_message(detail)
  cid=call.get('call_id') if isinstance(call,dict) else None
  if cid not in rejected_seen:
   rejected_seen.add(cid)
   if path is None:relative='<invalid view_image call>'
   else:
    try:relative=Path(path).resolve().relative_to(Path(workspace).resolve()).as_posix()
    except Exception:relative=str(path)
   rejected.append({'path':relative,'reason':detail,'tool_call_id':cid})
 for item in body.get('input',[]):
  if item.get('type')=='function_call':calls[item['call_id']]=item
  elif item.get('type')=='function_call_output':
   call=calls.get(item.get('call_id'),{});output=item.get('output');name=call.get('name','')
   if name=='view_image' and isinstance(output,list):
    if runtime.is_windows():
     a=json.loads(call['arguments']);p=Path(a['path']);p=p if p.is_absolute() else workspace/p
     e=image_paths.get(str(p.resolve()).casefold())
     imgs=[b for b in output if b.get('type')=='input_image']
     if imgs:
      if e is None or len(imgs)!=1:raise ValueError('Native image not in current case gallery')
      raw=Path(e['delivery_path']).read_bytes();mime='image/webp' if raw[:4]==b'RIFF' else 'image/png'
      before=imgs[0]['image_url'];imgs[0]['image_url']='data:'+mime+';base64,'+base64.b64encode(raw).decode()
      restores.append({'source':e['source'],'q85_sha256':e['delivery_sha256'],'native_uri_sha256':hashlib.sha256(before.encode()).hexdigest(),'changed':before!=imgs[0]['image_url']})
      newimgs.append((e,call['call_id']))
    else:
     p=None
     try:
      kind,payload,reason,p,imgs=classify_posix_view_image(call,output,workspace,image_paths,sources_by_id)
      if kind=='original' and len(imgs)==1:
       e=payload
       raw=verified_delivery_bytes(e)
       mime='image/webp' if raw[:4]==b'RIFF' else 'image/png'
       before=imgs[0]['image_url'];imgs[0]['image_url']='data:'+mime+';base64,'+base64.b64encode(raw).decode()
       restores.append({'source':e['source'],'q85_sha256':e['delivery_sha256'],'native_uri_sha256':hashlib.sha256(before.encode()).hexdigest(),'changed':before!=imgs[0]['image_url']})
       newimgs.append((e,call['call_id']))
      elif kind=='derived' and len(imgs)==1:
       metadata,raw=payload;before=imgs[0]['image_url'];imgs[0]['image_url']='data:image/png;base64,'+base64.b64encode(raw).decode()
       derived.append({'derived_view_id':metadata['derived_view_id'],'source_id':metadata['source_id'],'parent_source':metadata['parent_source'],'parent_source_sha256':metadata['parent_source_sha256'],'parent_delivery_sha256':metadata['parent_delivery_sha256'],'box':metadata['box'],'scale':metadata['scale'],'output_dimensions':metadata['output_dimensions'],'output_sha256':metadata['output_sha256'],'native_uri_sha256':hashlib.sha256(before.encode()).hexdigest(),'changed':before!=imgs[0]['image_url'],'tool_call_id':call['call_id']})
      elif kind!='ignored':reject_image(item,call,reason or 'Native view_image returned no usable image',p)
     except Exception as exc:reject_image(item,call,type(exc).__name__+': '+str(exc),p)
   elif isinstance(output,str):
    normalized=output.replace('\r\n','\n')
    for page in pages:
     if page_complete(page['text'],normalized):newpages.append((page,call.get('call_id')))
 return restores,newimgs,newpages,derived,rejected


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


def command_read_roots(command):
 """Allow both the invoked path and its resolved target inside the sandbox."""
 roots=[]
 command_paths = list(command)
 if sys.platform.startswith("linux"):
  sandbox_helper = shutil.which("codex-linux-sandbox")
  if sandbox_helper:
   command_paths.append(sandbox_helper)
 for part in command_paths:
  path=Path(part).expanduser()
  if not path.is_absolute() or not path.exists():continue
  invoked=path if path.is_dir() else path.parent
  resolved=path.resolve()
  target=resolved if resolved.is_dir() else resolved.parent
  for root in (invoked,target):
   if root not in roots:roots.append(root)
 return roots

import re as _re
# 沙箱初始化与连接错误的分类特征；实际模型调用以调用日志为准。
INFRA_PAT = _re.compile(r'failed to create session|native mxc is unavailable|failed to prepare fs sandbox|'
                        r'error creating thread|code -32603|failed to prepare MXC sandbox|'
                        r'502 bad gateway|tunnel connection failed|bad gateway|connection reset by peer|'
                        r'sandbox-exec|sandbox_apply|seatbelt', _re.I)
def normalize_refs(prediction):
 return canonical_normalization.normalize_refs(prediction)

def normalize_locator(prediction,case=None):
 return canonical_normalization.normalize_locator(prediction,case)

def normalize_steps(prediction,case):
 return canonical_normalization.normalize_steps(prediction,case)

def normalize_actors(node):
 return canonical_normalization.normalize_actors(node)

def normalize_labels(prediction):
 return canonical_normalization.normalize_labels(prediction)


def finish(out,case,skill,protocol,raw,receipt,meta,evidence_root=None):
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
  errors=validator(skill).validate_result(result,references=skill/'references',evidence_root=Path(evidence_root or case['evidence_root']),access_receipt=out/'access_receipt.json')
  # compile() already pruned undeclared fields and validated the wire contract.
  # Revalidating the pre-pruning prediction would reject a result that the
  # canonical protocol just accepted; validate the projected result instead.
  schema_errors=[e.message for e in Draft202012Validator(protocol.wire_schema()).iter_errors(protocol.project(result))]
  meta.update(state='validated' if not errors and not schema_errors else 'invalid',schema_errors=schema_errors,validation_errors=errors,parse_audit=parse_audit)
 except Exception as e:meta.update(state=('infra_failed' if INFRA_PAT.search(str(e)) else 'invalid'),error=type(e).__name__+': '+str(e)[:1800])
 atomic(out/'status.json',meta);return meta


def run_native(case,skill,sm,protocol,store,out,wall=850,*,command,endpoint,key,
               model=DEFAULT_MODEL,model_source='stepwise',model_provider=None,reasoning_effort=None):
 out.mkdir(parents=True,exist_ok=False)
 runtime=current_runtime()
 w=out/'workspace';w.mkdir(exist_ok=True);home=out/'home';home.mkdir(exist_ok=True)
 framework_knowledge,framework_architecture=install_framework_inputs(case,w,out)
 _sk=w/'.agents/skills/gui-trajectory-attribution'
 shutil.copytree(skill,_sk)
 for _f in (w/'.agents/skills/gui-trajectory-attribution/references').rglob('*.md'):
  try:_f.write_text(_f.read_text(encoding='utf-8-sig'),encoding='utf-8-sig')
  except Exception:pass
 (w/'identity.json').write_text(json.dumps(case['identity'],ensure_ascii=False,indent=2),encoding='utf8')
 # Deterministic textual serialization identical to the existing evidence reader.
 # Binary attachments outside the human-visible gallery are replaced with scope markers.
 pages=[];events=[];image_paths={};sources_by_id={};current='';members=[]
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
   entry={**e,'delivery_sha256':img['delivery_sha256'],'delivery_path':str(dst)}
   image_paths[str(dst.resolve()).casefold()]=entry;sources_by_id[e['source_id']]=entry
  else:
   sources_by_id[e['source_id']]=e
   blocks,_=store.read(e['source_id']);t='\n'.join(b['text'] for b in blocks if b['type']=='text')
   # Split very long file text into deterministic ranges; record complete coverage only
   # when every range, not merely a read invocation, reaches the main model.
   for offset in range(0,len(t),14000):
    part=t[offset:offset+14000];fragment={'entry':e,'offset':offset,'end':offset+len(part),'total':len(t),'text':part}
    marker='BEGIN_SOURCE_RANGE '+e['source_id']+' '+str(offset)+' '+str(offset+len(part))+'\n'+part+'\nEND_SOURCE_RANGE '+e['source_id']+'\n'
    if len(current)+len(marker)>18000:flush()
    current+=marker;members.append(fragment)
 flush()
 index={'identity':case['identity'],'available_steps':case['available_steps'],'sources':case['inventory'],
        'log_pages':[{'path':x['path'],'characters':len(x['text']),
                      'members':[{'source_id':m['entry']['source_id'],'offset':m['offset'],
                                  'end':m['end'],'total':m['total']} for m in x['members']]}
                     for x in pages],
        'images':[{'path':e['source'],'source_id':e['source_id']} for e in case['inventory'] if e['kind']=='image'],
        'normalization':'Original textual fields retained. In-log binary attachments outside the website gallery are replaced deterministically by the shared existing reader. Every named image has the existing q85 bytes at original dimensions.'}
 if framework_knowledge:index['framework_knowledge']=framework_knowledge
 if framework_architecture:index['framework_architecture']=framework_architecture
 (w/'input_index.json').write_text(json.dumps(index,ensure_ascii=False,indent=2),encoding='utf8')
 prompt=build_prompt(case,protocol,pages,w)
 (out/'prompt.txt').write_text(prompt,encoding='utf8')
 token=secrets.token_urlsafe(28);aud=[];lock=threading.Lock();start=time.monotonic();deadline=start+wall;viewed=set();ranges={};page_hits=set();derived_seen=set();rejected_seen=set();derived_audit=[]
 def clean(s):return s.replace(key,'[REDACTED]').replace(token,'[REDACTED_LOCAL]')
 def public(v):
  if isinstance(v,list):return [public(x) for x in v if not(isinstance(x,dict) and x.get('type')=='reasoning')]
  if isinstance(v,dict):return {k:public(x) for k,x in v.items() if k not in ('reasoning_content','encrypted_content')}
  return v
 def receipt():return store.receipt(events)
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
    restorations,newimgs,newpages,derived_images,rejected_images=restore_and_audit(body,w,pages,image_paths,sources_by_id,runtime,rejected_seen)
    new_derived=[entry for entry in derived_images if entry['tool_call_id'] not in derived_seen]
    body,route=route_request(body,model);item.update(**route,image_restorations=restorations,image_rejections=rejected_images)
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
       viewed.add(e['source']);item.setdefault('delivered_gallery_sources',[]).append(e['source_id']);events.append({'source':e['source'],'sha256':e['sha256'],'operation':'deliver_image','success':True,'content_complete':True,'tool_call_id':cid,'delivery_sha256':e['delivery_sha256'],'truncated':False})
     for entry in new_derived:
      if entry['tool_call_id'] not in derived_seen:
       derived_seen.add(entry['tool_call_id']);derived_audit.append(entry)
     item['derived_view_deliveries']=new_derived
     if new_derived:atomic(out/'derived_view_audit.json',{'schema':'stepwise-derived-view-audit-v1','deliveries':derived_audit})
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
 env={k:v for k,v in os.environ.items() if not k.upper().startswith('CODEX_') and not any(s in k.upper() for s in ('API_KEY','TOKEN','SECRET'))};env.update(CODEX_HOME=str(home),HOME=str(home),D60_LOCAL_KEY=token)
 if runtime.is_windows():env['USERPROFILE']=str(home)
 cmd=command+['exec','--ignore-user-config','--skip-git-repo-check']+runtime.approval_args()+['--json','-C',str(w),'-m',model,'-o',str(out/'answer.txt')]
 read_roots=[Path(sys.executable).resolve().parent,Path(sys.prefix).resolve(),Path(getattr(sys,'base_prefix',sys.prefix)).resolve()] if not runtime.is_windows() else []
 if not runtime.is_windows():read_roots.extend(command_read_roots(command))
 # The submission helper checks tickets, native receipt and bridge audit in the attempt root.
 read_roots.append(Path(out).resolve())
 opts={'model_provider':'d60_company','model_providers.d60_company.name':'Attribution recorder; explicit same-model approval surrogate','model_providers.d60_company.base_url':f'http://127.0.0.1:{server.server_port}/v1','model_providers.d60_company.env_key':'D60_LOCAL_KEY','model_providers.d60_company.wire_api':'responses','model_providers.d60_company.requires_openai_auth':False,'model_providers.d60_company.request_max_retries':1,'model_providers.d60_company.stream_max_retries':1,'model_providers.d60_company.stream_idle_timeout_ms':650000,'tool_output_token_limit':50000,'features.enable_request_compression':False,'features.skip_host_skill_discovery':True,'features.plugins':False,'features.remote_plugin':False,'features.multi_agent':False}
 if reasoning_effort is not None:opts['model_reasoning_effort']=reasoning_effort
 runtime_dep=os.environ.get('CODEX_RUNTIME_DEP') if runtime.is_windows() else None
 opts.update(runtime.sandbox_options(w,home,runtime_dep,read_roots));opts.update(runtime.approval_options())
 def toml(v):
  if isinstance(v,dict):return '{'+','.join(json.dumps(k)+'='+toml(x) for k,x in v.items())+'}'
  return json.dumps(v,ensure_ascii=False)
 for k,v in opts.items():cmd+=['-c',k+'='+toml(v)]
 cmd+=['-'];atomic(out/'launch.json',{'argv':cmd,'family':'native_d60','platform':runtime.name,'submission_helper':runtime.helper_name,'skill_manifest_sha256':sha(skill.parent/'manifest.json'),'wall_seconds':wall,'max_calls':160,'permission_policy':'root deny with workspace and minimal runtime allow','native_instructions':True,'approval_surrogate':model,'model_source':model_source,'model_provider':model_provider})
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
 meta={'case_id':case['case_id'],'family':'native_d60','platform':runtime.name,'submission_helper':runtime.helper_name,'model':model,'model_source':model_source,'model_provider':model_provider,'version':sm['skill_version'],'returncode':proc.returncode,'timed_out':timed,'coverage_complete':complete,'delivered_sources':len(events),'required_sources':len(case['inventory']),'delivered_images':len(viewed),'required_images':case['image_count'],'derived_views_delivered':len(derived_audit),'completed_pages':len(page_hits),'required_pages':len(pages),'upstream_calls':sum(bool(x.get('sent_upstream')) for x in aud),'bridge_requests':len(aud),'seconds':round(time.monotonic()-start,2),'run_directory':str(out),'approval_surrogate_not_official_model':True,'protocol':'stepwise_fast_v2','prompt_version':PROMPT_VERSION,'validator_patch_version':'answer-delivery-direction-v1','validator_sha256':sha(HERE/'validate_result.py'),'post_validator_model_repair':False}
 if (proc.returncode or timed) and not raw:
  _err=(stderr[-2200:] if stderr else 'No final output')
  meta.update(state='infra_failed' if INFRA_PAT.search(_err) else 'runtime_failed',error=_err)
  atomic(out/'status.json',meta);return meta
 return finish(out,case,skill,protocol,raw,receipt(),meta)
