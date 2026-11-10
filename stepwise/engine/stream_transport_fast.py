"""OpenAI-compatible Chat SSE accumulator; never persists reasoning text."""
import json,time,urllib.request,hashlib

def stream_chat(endpoint,key,request,event,deadline,checkpoint):
 request={**request,'stream':True,'stream_options':{'include_usage':True}}
 wire=json.dumps(request,ensure_ascii=False).encode();event.update(request_bytes=len(wire),request_sha256=hashlib.sha256(wire).hexdigest())
 req=urllib.request.Request(endpoint,data=wire,headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
 tools={};content=[];finish=None;usage=None;model=None;done=False;last_save=0
 event.update(transport='chat_sse_accumulator',stream_chunks=0,reasoning_characters=0,content_characters=0)
 with urllib.request.urlopen(req,timeout=min(120,max(2,deadline-time.monotonic()))) as response:
  event['http_status']=response.status
  for line in response:
   now=time.monotonic()
   if now>deadline:raise TimeoutError('Stream total deadline')
   s=line.decode('utf8').strip()
   if not s or s.startswith(':'):continue
   if not s.startswith('data:'):raise ValueError('Unexpected non-SSE upstream body')
   if s[5:].strip()=='[DONE]':done=True;break
   d=json.loads(s[5:]);event['stream_chunks']+=1
   if d.get('error'):raise ValueError('Upstream SSE error: '+str(d['error']))
   if d.get('model'):
    if model is not None and model!=d['model']:raise ValueError('Returned model changed midstream')
    model=d['model']
   if d.get('usage'):usage=d['usage'];event['usage']=usage
   for c in d.get('choices',[]):
    if c.get('index',0)!=0:raise ValueError('Multiple choices unsupported')
    delta=c.get('delta',{})
    if delta.get('content'):content.append(delta['content']);event['content_characters']+=len(delta['content'])
    if delta.get('reasoning_content'):event['reasoning_characters']+=len(delta['reasoning_content'])
    if delta.get('refusal'):raise ValueError('Provider refusal')
    for t in delta.get('tool_calls',[]):
     i=t['index'];v=tools.setdefault(i,{'id':'','type':'function','function':{'name':'','arguments':''}})
     if t.get('id'):
      if v['id'] and v['id']!=t['id']:raise ValueError('Tool call id changed')
      v['id']=t['id']
     fn=t.get('function',{});v['function']['name']+=fn.get('name','');v['function']['arguments']+=fn.get('arguments','')
    if c.get('finish_reason'):finish=c['finish_reason']
   if now-last_save>5:event['last_stream_chunk_at']=time.time();checkpoint();last_save=now
 if not finish:raise ValueError('Stream ended without terminal finish_reason')
 msg={'role':'assistant','content':''.join(content) or None}
 if tools:
  ordered=[tools[i] for i in sorted(tools)]
  for t in ordered:
   if not t['id'] or not t['function']['name']:raise ValueError('Incomplete streamed tool call')
   json.loads(t['function']['arguments'])
  msg['tool_calls']=ordered
 event.update(stream_done=done,returned_model=model,usage=usage,finish_reason=finish)
 checkpoint()
 return {'model':model,'usage':usage,'choices':[{'index':0,'finish_reason':finish,'message':msg}]}


def stream_chat_retry(endpoint,key,request,event,deadline,checkpoint,attempts=3):
    """Bounded network retries with unchanged TLS chain/hostname validation."""
    import ssl,socket,urllib.error
    last=None
    for number in range(attempts):
        if time.monotonic()>=deadline:raise TimeoutError('Deadline reached before retry')
        try:
            return stream_chat(endpoint,key,{**request},event,deadline,checkpoint)
        except Exception as exc:
            last=exc
            reason=getattr(exc,'reason',exc)
            message=str(exc).lower()
            retryable=isinstance(reason,(ssl.SSLError,socket.gaierror,ConnectionError,TimeoutError)) or any(word in message for word in ('stream ended','502','503','504','timed out','connection','remote end closed','incompleteread','urlopen error','getaddrinfo','winerror 10054','winerror 10060'))
            event.setdefault('network_retry_events',[]).append({'attempt':number+1,'error_type':type(reason).__name__,'error':str(exc)[:400],'tls_verification_disabled':False,'potential_partial_usage_unknown':bool(event.get('stream_chunks'))})
            checkpoint()
            if not retryable or number+1>=attempts:raise
            delay=min(8,4*(number+1))
            if time.monotonic()+delay>=deadline:raise
            time.sleep(delay)
    raise last
