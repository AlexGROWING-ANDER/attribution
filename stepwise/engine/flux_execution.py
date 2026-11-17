"""Local staging and validation for inference performed by native Flux agents.

This module never launches a model process or obtains model credentials.
Delivery receipts are explicit agent confirmations, not upstream HTTP receipts.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import time
import uuid

import native_engine as engine
from harness.storage import atomic_json
from reasoning_chain import write_reasoning_chain

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "stepwise"))
SCHEMA = "flux-attribution-session-v1"
ASSURANCE = "agent_confirmed_tool_delivery; not upstream transport verification"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@contextmanager
def session_lock(out):
    lock = Path(out) / "flux_state.lock"
    with lock.open("x", encoding="utf-8") as stream:
        stream.write("Exclusive delivery/finalization operation\n")
    try:
        yield
    finally:
        lock.unlink()


def staged_inventory(case):
    """Map the frozen index hashes to the bytes actually delivered in the workspace."""
    deliveries = {image["source_id"]: image for image in case["images"]}
    inventory = []
    for entry in case["inventory"]:
        if entry["kind"] == "image":
            image = deliveries[entry["source_id"]]
            inventory.append({
                **entry,
                "source_sha256": entry["sha256"],
                "sha256": image["delivery_sha256"],
                "delivery_sha256": image["delivery_sha256"],
            })
        else:
            inventory.append(entry)
    return inventory


def stage_workspace(case, skill, sm, protocol, store, out):
    w = out / "workspace"
    w.mkdir()
    framework_knowledge,framework_architecture=engine.install_framework_inputs(case,w,out)
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
      if engine.sha(src)!=img['delivery_sha256']:raise ValueError('Delivery cache changed')
      dst=w/e['source'];dst.parent.mkdir(exist_ok=True,parents=True);shutil.copyfile(src,dst)
      entry={**e,'delivery_sha256':img['delivery_sha256'],'delivery_path':str(dst)}
      image_paths[str(dst.resolve()).casefold()]=entry;sources_by_id[e['source_id']]=entry
     else:
      sources_by_id[e['source_id']]=e
      source_path=Path(case['evidence_root'])/e['source']
      if not source_path.is_file():raise ValueError('Evidence source is missing: '+e['source'])
      dst=w/e['source'];dst.parent.mkdir(exist_ok=True,parents=True);shutil.copyfile(source_path,dst)
      blocks,_=store.read(e['source_id']);t='\n'.join(b['text'] for b in blocks if b['type']=='text')
      # Split very long file text into deterministic ranges; record complete coverage only
      # when every range, not merely a read invocation, reaches the main model.
      for offset in range(0,len(t),14000):
       part=t[offset:offset+14000];fragment={'entry':e,'offset':offset,'end':offset+len(part),'total':len(t),'text':part}
       marker='BEGIN_SOURCE_RANGE '+e['source_id']+' '+str(offset)+' '+str(offset+len(part))+'\n'+part+'\nEND_SOURCE_RANGE '+e['source_id']+'\n'
       if len(current)+len(marker)>18000:flush()
       current+=marker;members.append(fragment)
    flush()
    index={'identity':case['identity'],'available_steps':case['available_steps'],'sources':staged_inventory(case),
           'log_pages':[{'path':x['path'],'characters':len(x['text']),
                         'members':[{'source_id':m['entry']['source_id'],'offset':m['offset'],
                                     'end':m['end'],'total':m['total']} for m in x['members']]}
                        for x in pages],
           'images':[{'path':e['source'],'source_id':e['source_id']} for e in case['inventory'] if e['kind']=='image'],
           'normalization':'Original textual fields retained. In-log binary attachments outside the website gallery are replaced deterministically by the shared existing reader. Every named image has the existing q85 bytes at original dimensions.'}
    if framework_knowledge:index['framework_knowledge']=framework_knowledge
    if framework_architecture:index['framework_architecture']=framework_architecture
    (w/'input_index.json').write_text(json.dumps(index,ensure_ascii=False,indent=2),encoding='utf8')
    prompt=engine.build_prompt(case,protocol,pages,w)
    (out/'prompt.txt').write_text(prompt,encoding='utf8')
    return pages, index


def prepare_case(loaded, out, binding, retry_context=None):
    case, _, skill, sm, protocol, store = loaded
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    if retry_context is not None:
        if (not isinstance(retry_context, dict)
                or retry_context.get("schema") != "flux-retry-context-v1"
                or retry_context.get("case_id") != case["case_id"]):
            raise ValueError("Retry context does not belong to the selected case")
        atomic_json(out / "retry_context.json", retry_context)
    pages, index = stage_workspace(case, skill, sm, protocol, store, out)
    workspace = out / "workspace"
    resources = {}
    for page in pages:
        resources[page["path"]] = {
            "kind": "text", "path": page["path"],
            "sha256": sha(workspace / page["path"]),
            "members": [{k: v for k, v in fragment.items() if k != "text"}
                        for fragment in page["members"]],
        }
    for image in case["images"]:
        resources[image["source_id"]] = {
            "kind": "image", "path": image["source"],
            "sha256": image["delivery_sha256"],
            "entry": next(e for e in case["inventory"]
                          if e["source_id"] == image["source_id"]),
        }
    frozen = {path.relative_to(workspace).as_posix(): sha(path)
              for path in workspace.rglob("*") if path.is_file()}
    session = {"schema": SCHEMA, "case_id": case["case_id"],
               "resources": resources, "frozen_workspace": frozen,
               "receipt_assurance": ASSURANCE}
    atomic_json(out / "flux_session.json", session)
    binding_record = {
        **binding, "case_id": case["case_id"],
        "session_sha256": sha(out / "flux_session.json"),
        "prompt_sha256": sha(out / "prompt.txt"),
        "execution_backend": "flux_native",
    }
    if retry_context is not None:
        binding_record.update(
            retry_of=retry_context.get("previous_attempt"),
            retry_context_sha256=sha(out / "retry_context.json"),
            attempt_index=retry_context.get("attempt_index"),
        )
    atomic_json(out / "input_binding.json", binding_record)
    task = render_task(out)
    (out / "flux_task.md").write_text(task, encoding="utf-8")
    status = {"case_id": case["case_id"], "state": "ready_for_flux",
              "execution_backend": "flux_native", "inference_started": False,
              "task_path": str(out / "flux_task.md"),
              "receipt_assurance": ASSURANCE}
    if retry_context is not None:
        status.update(attempt_index=retry_context.get("attempt_index"),
                      retry_of=retry_context.get("previous_attempt"),
                      retry_max_attempts=retry_context.get("max_attempts"))
    atomic_json(out / "status.json", status)
    return status


def validate_session(out):
    from run_case import load_engine, load_inputs, verify_release
    out = Path(out).resolve()
    verify_release()
    binding = read_json(out / "input_binding.json")
    if binding.get("execution_backend") != "flux_native":
        raise ValueError("Not a native Flux attribution session")
    if sha(REPO / "RELEASE_MANIFEST.json") != binding["runtime_manifest_sha256"]:
        raise ValueError("Runtime changed since Flux dispatch")
    if sha(REPO / "stepwise/skills/v3/manifest.json") != binding["skill_manifest_sha256"]:
        raise ValueError("Skill changed since Flux dispatch")
    if sha(out / "flux_session.json") != binding["session_sha256"]:
        raise ValueError("Flux session registry changed")
    if sha(out / "prompt.txt") != binding["prompt_sha256"]:
        raise ValueError("Prepared attribution prompt changed")
    if binding.get("retry_context_sha256"):
        context_path = out / "retry_context.json"
        if not context_path.is_file() or sha(context_path) != binding["retry_context_sha256"]:
            raise ValueError("Retry context changed")
        if read_json(context_path).get("case_id") != binding["case_id"]:
            raise ValueError("Retry context identity mismatch")
    session = read_json(out / "flux_session.json")
    if session.get("schema") != SCHEMA or session.get("case_id") != binding["case_id"]:
        raise ValueError("Flux session identity mismatch")
    workspace = out / "workspace"
    for name, expected in session["frozen_workspace"].items():
        path = (workspace / name).resolve()
        if not path.is_relative_to(workspace) or sha(path) != expected:
            raise ValueError("Frozen Flux input changed: " + name)
    loaded = load_inputs(load_engine(), Path(binding["manifest_path"]),
                         binding["case_id"], binding["manifest_sha256"])
    return session, loaded


def offer_delivery(out, resource):
    out = Path(out).resolve()
    with session_lock(out):
        session, _ = validate_session(out)
        item = resolve_resource(out, session, resource)
        workspace = out / "workspace"
        # Raw reads cannot be undone: enforce checkpoint order at the read facade.
        io = load_helper(workspace)
        io.require_checkpoint_submission(read_json(workspace / "input_index.json"))
        ticket = uuid.uuid4().hex
        atomic_json(out / "delivery_tickets" / (ticket + ".json"), {
            "ticket": ticket, "resource": resource, "kind": item["kind"],
            "sha256": item["sha256"], "confirmed": False,
        })
        result = {"ticket": ticket, "resource": resource, "kind": item["kind"],
                  "sha256": item["sha256"], "confirmed": False}
        path = workspace / item["path"]
        if item["kind"] == "text":
            result["text"] = path.read_text(encoding="utf-8-sig")
        else:
            result["path"] = str(path)
        return result


def acknowledge_delivery(out, ticket, native_tool, tool_ref):
    if not re.fullmatch(r"[0-9a-f]{32}", ticket):
        raise ValueError("Invalid delivery ticket")
    if not isinstance(tool_ref, str) or not tool_ref.strip():
        raise ValueError("A native tool completion reference is required")
    out = Path(out).resolve()
    with session_lock(out):
        session, _ = validate_session(out)
        path = out / "delivery_tickets" / (ticket + ".json")
        record = read_json(path)
        item = resolve_resource(out, session, record["resource"])
        expected_tool = "exec_command" if item["kind"] == "text" else "view_image"
        if (not item or record["ticket"] != ticket or record["sha256"] != item["sha256"]
                or record["kind"] != item["kind"] or native_tool != expected_tool):
            raise ValueError("Delivery confirmation differs from its frozen resource")
        if record["confirmed"]:
            if record.get("native_tool") != native_tool or record.get("tool_ref") != tool_ref:
                raise ValueError("Ticket was already confirmed by a different completion")
            return {"ticket": ticket, "confirmed": True, "preserved_existing": True}
        record.update(confirmed=True, native_tool=native_tool, tool_ref=tool_ref,
                      confirmed_at=time.time(), assurance=ASSURANCE)
        atomic_json(path, record)
        return {"ticket": ticket, "confirmed": True, "assurance": ASSURANCE}


def resolve_resource(out, session, resource):
    item = session["resources"].get(resource)
    if item is not None:
        return item
    workspace = Path(out) / "workspace"
    path = (workspace / resource).resolve()
    if not path.is_relative_to(workspace / engine.DERIVED_ROOT):
        raise ValueError("Resource is not a registered log page or gallery source")
    sources = {name: {**value["entry"], "delivery_sha256": value["sha256"]}
               for name, value in session["resources"].items() if value["kind"] == "image"}
    metadata, _ = engine.load_derived_view(workspace, path, sources)
    parent = metadata["source_id"]
    seen = any(read_json(p).get("resource") == parent and read_json(p).get("confirmed") is True
               and read_json(p).get("native_tool") == "view_image"
               for p in (Path(out) / "delivery_tickets").glob("*.json"))
    if not seen:
        raise ValueError("Original gallery image must be confirmed before a derived view")
    return {"kind": "derived_image", "path": path.relative_to(workspace).as_posix(),
            "sha256": metadata["output_sha256"], "metadata": metadata}


def load_helper(workspace):
    import importlib.util
    path = Path(workspace) / "io.py"
    spec = importlib.util.spec_from_file_location("flux_submission_helper", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def delivery_receipt(out, session, loaded):
    case, _, _, _, _, store = loaded
    confirmed = []
    for path in sorted((Path(out) / "delivery_tickets").glob("*.json")):
        record = read_json(path)
        if record.get("confirmed") is not True:
            continue
        item = resolve_resource(out, session, record.get("resource", ""))
        if (not item or record.get("sha256") != item["sha256"]
                or record.get("native_tool") != ("exec_command" if item["kind"] == "text" else "view_image")
                or not record.get("tool_ref")):
            raise ValueError("Invalid confirmed delivery ticket")
        confirmed.append((record, item))
    events, ranges = {}, {}
    for record, item in confirmed:
        if item["kind"] == "derived_image":
            continue
        if item["kind"] == "image":
            entry = item["entry"]
            events[entry["source"]] = {
                "source": entry["source"], "sha256": item["sha256"],
                "operation": "deliver_image", "success": True,
                "content_complete": True, "truncated": False,
                "delivery_sha256": item["sha256"],
                "tool_call_id": "flux-confirmation-" + record["ticket"],
                "native_tool_reference": record["tool_ref"],
            }
        else:
            for member in item["members"]:
                source = member["entry"]["source"]
                ranges.setdefault(source, []).append((member, record))
    for source, fragments in ranges.items():
        spans = sorted(set((f["offset"], f["end"], f["total"]) for f, _ in fragments))
        complete = (spans[0][0] == 0 and spans[-1][1] == spans[-1][2]
                    and all(a[1] == b[0] for a, b in zip(spans, spans[1:])))
        entry = fragments[0][0]["entry"]
        events[source] = {"source": source, "sha256": entry["sha256"],
                          "operation": "read_text", "success": True,
                          "content_complete": complete, "truncated": not complete,
                          "delivered_ranges": spans,
                          "tool_call_id": "flux-confirmation-" + fragments[0][1]["ticket"],
                          "native_tool_reference": fragments[0][1]["tool_ref"]}
    atomic_json(Path(out) / "derived_view_audit.json", {
        "schema": "flux-derived-view-confirmation-v1", "assurance": ASSURANCE,
        "deliveries": [{**item["metadata"], "native_tool_reference": record["tool_ref"]}
                       for record, item in confirmed if item["kind"] == "derived_image"],
    })
    receipt = store.receipt(list(events.values()))
    receipt["inventory"] = read_json(Path(out) / "workspace/input_index.json")["sources"]
    receipt.update(confirmation_producer="flux_agent", execution_backend="flux_native",
                   receipt_assurance=ASSURANCE,
                   meaning="Agent confirms completed native tool presentation. "
                           "No upstream HTTP, comprehension, or independent platform trace claim.")
    return receipt


def finalize_case(out):
    out = Path(out).resolve()
    with session_lock(out):
        session, loaded = validate_session(out)
        case, _, skill, _, protocol, _ = loaded
        prior = read_json(out / "status.json")

        def capture_reasoning():
            if (out / "reasoning_chain.meta.json").is_file():
                return
            try:
                write_reasoning_chain(
                    out, module="gui-trajectory-attribution", model=prior.get("model"),
                    discover_attempt=True, write_empty=False,
                )
            except Exception as exc:
                atomic_json(out / "reasoning_chain_error.json", {
                    "error": type(exc).__name__ + ": " + str(exc)[:1000],
                })
        if prior.get("state") in ("validated", "invalid", "runtime_failed", "infra_failed"):
            capture_reasoning()
            return prior
        workspace = out / "workspace"
        io = load_helper(workspace)
        events_path = workspace / "io_events.jsonl"
        events = io.io_events() if events_path.is_file() else []
        if not any(e.get("mode") == "check" and e.get("accepted") is True for e in events):
            return {"case_id": case["case_id"], "state": "ready_for_flux",
                    "reason": "Native Flux task has not completed the local submission check"}
        capture_reasoning()
        receipt = delivery_receipt(out, session, loaded)
        atomic_json(out / "access_receipt.json", receipt)
        complete = sum(e["content_complete"] for e in receipt["events"]) == len(case["inventory"])
        meta = {"case_id": case["case_id"], "execution_backend": "flux_native",
                "receipt_assurance": ASSURANCE, "coverage_complete": complete,
                "delivered_sources": len(receipt["events"]),
                "required_sources": len(case["inventory"]),
                "delivered_images": sum(e["operation"] == "deliver_image" for e in receipt["events"]),
                "required_images": case["image_count"],
                "prompt_version": engine.PROMPT_VERSION, "post_validator_model_repair": False,
                "isolation": "scoped_workspace; native platform permissions inherited"}
        for key in ("attempt_index", "retry_of", "retry_max_attempts"):
            if prior.get(key) is not None:
                meta[key] = prior[key]
        try:
            prediction = engine.assemble(workspace, case, receipt)
        except Exception as exc:
            meta.update(state="invalid", error="Step assembly: " + str(exc))
            engine.write_best_effort_result(out, case, protocol, receipt, meta["state"], meta["error"])
            engine.write_attribution_result(out, case, meta["state"], [meta["error"]])
            atomic_json(out / "status.json", meta)
            return meta
        return engine.finish(out, case, skill, protocol, json.dumps(prediction, ensure_ascii=False), receipt, meta,
                             evidence_root=workspace)


def render_task(out):
    facade = REPO / "stepwise/engine/flux_execution.py"
    retry = ""
    if (Path(out) / "retry_context.json").is_file():
        retry = f"""
## 自动重试上下文

本 attempt 是自动重试。开始分析前完整读取 `{Path(out) / 'retry_context.json'}`；
其中只有上一 attempt 的校验错误摘要和修复提示，没有上一轮预测。不要读取或复制
上一 attempt 目录下的 result.json、判断文件或其他历史预测。必须从本 attempt 的
原始材料、冻结 Skill 和 Prompt 重新完成全部分析、checkpoint、逐 step 提交和全局汇总。
上一轮列出的每个错误都必须在本地 `io.py check` 中消失后才能回复 DONE。
"""
    return f"""# Flux 原生归因执行任务

仅处理当前案例，工作区为 `{out / 'workspace'}`。继承 Flux 原生会话能力，不指定模型，
不启动模型 CLI、不请求模型 API、不读取登录文件或凭证。不要访问 GT、其他案例或历史预测。
{retry}
完整读取工作区内 `.agents/skills/gui-trajectory-attribution/SKILL.md` 并按原 Skill 分析。
完整读取 `{out / 'prompt.txt'}`，严格遵循其中的 checkpoint、逐步提交和全局汇总顺序。
原始归因算法、Prompt、标签、Schema 和 io.py 保持原样。
启动文档（本任务、Prompt、Skill、引用、Schema、input/step_index 及架构概览）
允许用原生 exec_command 逐文件 cat 全文，在读取轨迹之前完成；它们不经轨迹票据门面。
下述 read/acknowledge 门面只用于轨迹日志和图库，避免与 checkpoint 的启动顺序冲突。

读取轨迹材料使用本地工具门面：
`python3 {facade} read --out {out} --resource logs/page_001.txt`
返回 text 和 ticket。工具结果完全进入当前上下文后，再执行：
`python3 {facade} acknowledge --out {out} --ticket TICKET --native-tool exec_command --tool-ref COMPLETION_REFERENCE`
图片先使用 read 的 resource 参数传入图片 source_id，得到原图 path 和 ticket；
再对 path 调用 Flux 原生 view_image，确认图像实际展示后以 native-tool=view_image 确认 ticket。
工具支持 detail 参数时必须显式使用 original，并将原始图像转发到当前上下文，不能缩小或替换图片。
COMPLETION_REFERENCE 优先使用原生工具返回的 chunk_id/调用编号；工具未返回编号时，
使用明确描述本次成功展示的引用，例如 `view_image:S0001:completed`。
ticket 是本地确认关联编号，不冒充平台工具编号。截图未展示、文本截断或工具报错时禁止确认。
目录、元数据和 read 提供的图片路径本身不算图片已查看；文件准备也不算推理完成。

io.py 提交顺序及读取的页面/图库范围仍由原 submission_protocol.md 和 step_index.json 定义。
若使用 io.py derive 的受控视图，先确认原图库图像送达，派生图不能计入图库覆盖。
单案例最多两个原始证据重查批次。提交 global_summary 后必须运行本地 `io.py check`；
被拒时按返回的具体错误改正并再次 check（最多三个修正循环），通过后才回复 DONE。
本地 check 通过后不自行运行严格最终校验器。
完成后只回复 DONE；否则报告具体阻塞。最终组装、覆盖率计算和严格校验由父 Flux 任务执行。
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("read", "acknowledge"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--resource")
    parser.add_argument("--ticket")
    parser.add_argument("--native-tool", choices=("exec_command", "view_image"))
    parser.add_argument("--tool-ref")
    args = parser.parse_args()
    if args.action == "read":
        if not args.resource:
            parser.error("read requires --resource")
        value = offer_delivery(args.out, args.resource)
    else:
        if not all((args.ticket, args.native_tool, args.tool_ref)):
            parser.error("acknowledge requires --ticket, --native-tool and --tool-ref")
        value = acknowledge_delivery(args.out, args.ticket, args.native_tool, args.tool_ref)
    print(json.dumps(value, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
