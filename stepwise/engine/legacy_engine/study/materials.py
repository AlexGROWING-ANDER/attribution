"""Frozen evidence catalogs and deterministic, shared input rendering."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import mimetypes
import re
import shutil
from datetime import datetime
from pathlib import Path

from harness.protocol import SourceCatalog, canonical_path, digest, strict_json
from harness.storage import atomic_json
from study.image_scope import normalize_inline_images, scoped_entries, validate_scope

IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp'}
TEXT_SUFFIXES = {'.json', '.jsonl', '.txt', '.log'}
EXCLUDED_NAMES = {'task_result.json', 'annotation.json', 'annotations.json'}


def sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover(root: Path) -> list[dict]:
    """Only current-attempt evidence, never outer batch reports or annotation files."""
    records = []
    for p in sorted(root.rglob('*')):
        if not p.is_file() or p.name in EXCLUDED_NAMES:
            continue
        rel = p.relative_to(root).as_posix()
        if not p.resolve().is_relative_to(root.resolve()):
            raise ValueError('Evidence symlink escapes attempt: ' + rel)
        if any(part.lower() in {'annotations', 'reviews', 'gt', '__pycache__'} for part in p.relative_to(root).parts):
            continue
        if p.suffix.lower() not in IMAGE_SUFFIXES | TEXT_SUFFIXES:
            continue
        match = re.search(r'(?:^|/)step_(\d+)(?:/|$)', rel)
        step = int(match[1]) if match else None
        if step == 0:
            raise ValueError('Zero-based logs need an explicit step mapping before freeze')
        rec = {'source': rel, 'kind': 'image' if p.suffix.lower() in IMAGE_SUFFIXES else 'log',
               'step': step, 'sha256': sha_file(p), 'bytes': p.stat().st_size}
        records.append(rec)
    if not records:
        raise ValueError('Empty attempt: ' + str(root))
    return SourceCatalog(records).entries


def add_public_input_record(destination: Path, inventory: list[dict], original: Path | None=None) -> list[dict]:
    """Shared, independently citable inventory and allowlisted runner provenance.

    Never include scores, task success, annotations or a guessed stop reason.
    A finish timestamp records a runner boundary, not task completion.
    """
    original=original or destination
    record={'record_kind':'organizer_input_inventory',
            'inventory_complete_for_supplied_attempt':True,
            'observed_files':[{k:r[k] for k in ('source','kind','step','sha256') if k in r} for r in inventory],
            'meaning':'Lists supplied files only. Absence does not by itself prove an expected file was lost.',
            'runner_record':None,'task_outcome_not_provided':True}
    source=original/'task_result.json'
    if source.is_file():
        raw=json.loads(source.read_text(encoding='utf-8-sig'))
        safe={}
        if isinstance(raw.get('finish_dtime'),str):
            try:datetime.fromisoformat(raw['finish_dtime']);safe['finish_dtime']=raw['finish_dtime']
            except ValueError:pass
        limit=raw.get('hit_step_limit')
        if type(limit) in (bool,int,float) and limit in (0,1):safe['hit_step_limit']=bool(limit)
        if safe:
            record['runner_record']={'source':'task_result.json','source_sha256':sha_file(source),
                                     'fields':safe,'raw_source_sent':False,
                                     'meaning':'Runner finish/step-limit facts only; no task-success evaluation.'}
    rel='zz_public_input_record.json';target=destination/rel
    if target.exists():raise ValueError('Public input record already exists')
    atomic_json(target,record)
    return SourceCatalog([*inventory,{'source':rel,'kind':'metadata','step':None,
                                    'sha256':sha_file(target),'bytes':target.stat().st_size,
                                    'allowed_evidence_kinds':['metadata']}]).entries


def runtime_files(root: Path) -> list[Path]:
    """Keep rule resources in a runtime snapshot, code/tests in the organizer tree."""
    return sorted(p for p in root.rglob('*') if p.is_file() and
                  (p.relative_to(root).parts[0] in {'references','agents'} or
                   p.relative_to(root).as_posix()=='SKILL.md'))


def snapshot_skill(source: Path, destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=False)
    files = {}
    for p in runtime_files(source):
        rel = p.relative_to(source)
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, target)
        files[rel.as_posix()] = sha_file(target)
    reg = strict_json((destination/'references/template-registry.json').read_text(encoding='utf-8-sig'))
    for entry in reg['templates'].values():
        if sha_file(destination/'references'/entry['taxonomy_path']) != entry['sha256']:
            raise ValueError('Taxonomy hash mismatch')
    return {'skill_version': reg['skill_version'], 'policy_id': reg['policy_id'],
            'files': files, 'tree_sha256': digest(files)}


def expand_embedded_images(root: Path, inventory: list[dict]) -> list[dict]:
    """Materialize every image present only inside an agent's JSON input log.

    Some agents saw a different encoded/scaled image from the saved screenshot.
    Keep both instead of pretending their bytes match or discarding either one.
    """
    records=list(inventory)
    known={r['sha256'] for r in records if r['kind']=='image'}
    extracted={}
    def visit(value,source,pointer):
        if isinstance(value,dict):
            for key,child in value.items():visit(child,source,pointer+'/'+str(key).replace('~','~0').replace('/','~1'))
        elif isinstance(value,list):
            for index,child in enumerate(value):visit(child,source,pointer+'/'+str(index))
        elif isinstance(value,str) and value.startswith('data:image/') and ';base64,' in value:
            raw=base64.b64decode(value.split(',',1)[1],validate=True)
            sha=hashlib.sha256(raw).hexdigest()
            if sha in known:return
            mime=value.split(';',1)[0][5:]
            suffix={'image/png':'png','image/jpeg':'jpg','image/webp':'webp'}.get(mime)
            if not suffix:raise ValueError('Unsupported embedded image MIME: '+mime)
            rel='_embedded_images/'+sha+'.'+suffix
            target=root/rel;target.parent.mkdir(parents=True,exist_ok=True)
            if target.exists() and sha_file(target)!=sha:raise ValueError('Embedded source collision')
            if not target.exists():target.write_bytes(raw)
            record=extracted.setdefault(sha,{'source':rel,'kind':'image','step':None,'sha256':sha,
                                            'bytes':len(raw),'embedded_provenance':[]})
            record['embedded_provenance'].append({'source':source,'json_pointer':pointer})
    for record in inventory:
        if record['kind']=='log' and Path(record['source']).suffix.lower()=='.json':
            value=json.loads((root/record['source']).read_text(encoding='utf-8-sig'))
            visit(value,record['source'],'')
    return SourceCatalog(records+list(extracted.values())).entries


def normalize_log(value, images: dict[str, str], audit: list[dict], scoped: bool=False):
    """Replace duplicated embedded image bytes only when the exact original is supplied."""
    if isinstance(value, dict):
        return {k: normalize_log(v, images, audit, scoped) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize_log(v, images, audit, scoped) for v in value]
    if isinstance(value, str):
        return normalize_inline_images(value, images, audit, scoped)
    return value


def build_lossless_delivery_cache(root: Path, inventory: list[dict], cache: Path) -> dict:
    """Encode only smaller pixel-identical WebP files; keep all source evidence."""
    from PIL import Image
    cache.mkdir(parents=True,exist_ok=False);records={}
    for entry in inventory:
        if entry['kind']!='image' or entry['sha256'] in records:continue
        raw=(root/entry['source']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=entry['sha256']:raise ValueError('Source changed during encoding')
        original=Image.open(io.BytesIO(raw));pixels=original.convert('RGBA').tobytes()
        output=io.BytesIO();original.save(output,format='WEBP',lossless=True,method=4)
        encoded=output.getvalue();decoded=Image.open(io.BytesIO(encoded))
        if decoded.size!=original.size or decoded.convert('RGBA').tobytes()!=pixels:
            raise ValueError('Lossless pixel verification failed')
        smaller=len(encoded)<len(raw)
        delivered=encoded if smaller else raw
        item={'source_sha256':entry['sha256'],'pixel_sha256':hashlib.sha256(pixels).hexdigest(),
              'width':original.width,'height':original.height,'source_bytes':len(raw),'delivery_bytes':len(delivered),
              'delivery_sha256':hashlib.sha256(delivered).hexdigest(),'format':'webp' if smaller else 'original'}
        if smaller:(cache/(entry['sha256']+'.webp')).write_bytes(encoded)
        records[entry['sha256']]=item
    atomic_json(cache/'manifest.json',{'images':records,'pixel_identity_verified':True})
    return {'mode':'lossless_webp_if_smaller','cache_root':str(cache.resolve()),'manifest_sha256':sha_file(cache/'manifest.json')}


class EvidenceStore:
    def __init__(self, root: Path, inventory: list[dict], attempt: str, image_delivery: dict | None=None,
                 image_scope: dict | None=None):
        self.root, self.catalog, self.attempt = root.resolve(), SourceCatalog(inventory), attempt
        self.image_scope=validate_scope(self.catalog,image_scope)
        self._scope_image_urls={}
        self.images = {r['sha256']: r['source_id'] for r in self.catalog.entries if r['kind']=='image'}
        self.image_delivery=image_delivery
        self.delivery_manifest=None
        self._cas_verified=set()
        self._derived_aliases=None
        if image_delivery:
            mode=image_delivery.get('mode')
            if mode not in ('lossless_webp_if_smaller','lossless_webp_if_smaller_cas','q85_webp_cas'):raise ValueError('Unknown image delivery mode')
            if mode=='lossless_webp_if_smaller':
                manifest=Path(image_delivery['cache_root'])/'manifest.json'
                if sha_file(manifest)!=image_delivery['manifest_sha256']:raise ValueError('Delivery manifest changed')
                self.delivery_manifest=json.loads(manifest.read_text(encoding='utf-8'))['images']

    def _aliases(self) -> dict:
        """Only remove derivatives exactly reconstructible from a supplied original.

        No similarity thresholds, no temporal-frame merging, no GT access.
        All sources remain individually named and retain their provenance.
        """
        if self._derived_aliases is not None:return self._derived_aliases
        self._derived_aliases={}
        if self.image_scope:return self._derived_aliases
        if not self.image_delivery or not self.image_delivery.get('deduplicate_derivatives'):return self._derived_aliases
        from PIL import Image
        originals=[e for e in self.catalog.entries if e['kind']=='image' and not e['source'].startswith('_embedded_images/')]
        derivatives=[e for e in self.catalog.entries if e['kind']=='image' and e['source'].startswith('_embedded_images/')]
        sizes=set()
        for e in derivatives:
            with Image.open(self.root/e['source']) as image:sizes.add(image.size)
        lookup={}
        for e in originals:
            if sha_file(self.root/e['source'])!=e['sha256']:raise ValueError('Original changed during alias verification')
            with Image.open(self.root/e['source']) as image:
                for size in sizes:
                    pixels=image.convert('RGB').resize(size,Image.Resampling.BICUBIC).tobytes()
                    lookup.setdefault((size,hashlib.sha256(pixels).hexdigest()),e)
        for e in derivatives:
            if sha_file(self.root/e['source'])!=e['sha256']:raise ValueError('Derivative changed during alias verification')
            with Image.open(self.root/e['source']) as image:
                # Transparent evidence needs a separate mapping policy.
                if image.convert('RGBA').getextrema()[3]!=(255,255):continue
                key=(image.size,hashlib.sha256(image.convert('RGB').tobytes()).hexdigest())
            if key in lookup:self._derived_aliases[e['source_id']]=lookup[key]
        return self._derived_aliases

    def _cas_image(self, entry: dict, raw: bytes) -> tuple[bytes,str,dict]:
        """Cache each deterministic encoding independently of an evolving global manifest.

        q85_webp_cas: lossy WebP quality=85 method=6. Pixel-identity verification is
        intentionally skipped (lossy by definition); delivery integrity stays bound to
        delivery_sha256. The lossy choice is recorded in per-image meta for audit."""
        from PIL import Image
        cache=Path(self.image_delivery['cache_root']);cache.mkdir(parents=True,exist_ok=True)
        meta=cache/(entry['sha256']+'.json');encoded_path=cache/(entry['sha256']+'.webp')
        lossy=self.image_delivery.get('mode')=='q85_webp_cas'
        with Image.open(io.BytesIO(raw)) as original:
            pixels=original.convert('RGBA').tobytes()
            pixel_sha=hashlib.sha256(pixels).hexdigest()
            if meta.exists():
                info=json.loads(meta.read_text(encoding='utf-8'))
                if info['source_sha256']!=entry['sha256']:raise ValueError('Wrong CAS image source')
                if bool(info.get('lossy_quality'))!=lossy:raise ValueError('CAS cache mode mismatch: lossy vs lossless')
                delivered=encoded_path.read_bytes() if info['format']=='webp' else raw
            else:
                if getattr(original,'n_frames',1)!=1:raise ValueError('Animated screenshot needs an explicit policy')
                output=io.BytesIO()
                if lossy:
                    original.save(output,format='WEBP',quality=85,method=6)
                else:
                    original.save(output,format='WEBP',lossless=True,exact=True,method=4)
                encoded=output.getvalue();smaller=len(encoded)<len(raw)
                delivered=encoded if smaller else raw
                info={'source_sha256':entry['sha256'],'pixel_sha256':pixel_sha,'width':original.width,'height':original.height,
                      'source_bytes':len(raw),'delivery_bytes':len(delivered),'delivery_sha256':hashlib.sha256(delivered).hexdigest(),
                      'format':'webp' if smaller else 'original'}
                if lossy:info['lossy_quality']=85
                if smaller:encoded_path.write_bytes(delivered)
                atomic_json(meta,info)
            if hashlib.sha256(delivered).hexdigest()!=info['delivery_sha256']:raise ValueError('CAS image changed')
            if not lossy and entry['sha256'] not in self._cas_verified:
                with Image.open(io.BytesIO(delivered)) as decoded:
                    if decoded.size!=original.size or decoded.convert('RGBA').tobytes()!=pixels:raise ValueError('CAS pixel identity failed')
                if info['pixel_sha256']!=pixel_sha:raise ValueError('CAS pixel hash changed')
                self._cas_verified.add(entry['sha256'])
            if lossy and entry['sha256'] not in self._cas_verified:
                with Image.open(io.BytesIO(delivered)) as decoded:
                    if decoded.size!=original.size:raise ValueError('CAS lossy image dimensions changed')
                self._cas_verified.add(entry['sha256'])
        mime='image/webp' if info['format']=='webp' else (mimetypes.guess_type(entry['source'])[0] or 'image/png')
        return delivered,mime,info

    def verify(self) -> None:
        self.catalog.verify_files(self.root)

    def read(self, source_id: str) -> tuple[list[dict], dict]:
        if source_id not in self.catalog.by_id:
            raise ValueError('Unknown source ID')
        entry = self.catalog.by_id[source_id]
        path = (self.root / canonical_path(entry['source'])).resolve()
        if not path.is_relative_to(self.root) or sha_file(path) != entry['sha256']:
            raise ValueError('Evidence changed or escaped attempt')
        raw = path.read_bytes()
        audit = []
        if entry['kind'] == 'image':
            caption=f"SOURCE {source_id}: {entry['source']}"
            if self.image_scope:
                index=next(i['display_index'] for i in self.image_scope['ordered_images'] if i['source']==entry['source'])
                caption+='\nHuman website gallery image '+str(index+1)+' of '+str(len(self.image_scope['ordered_images']))
            canonical=self._aliases().get(source_id)
            if canonical:
                raw=(self.root/canonical['source']).read_bytes()
                if hashlib.sha256(raw).hexdigest()!=canonical['sha256']:raise ValueError('Canonical source changed')
                audit.append({'kind':'exact_reconstructible_derivative','canonical_source_id':canonical['source_id'],
                              'canonical_source_sha256':canonical['sha256'],'derivative_sha256':entry['sha256'],
                              'operation':'RGB BICUBIC resize; exact output pixels verified'})
                caption+='\nThis source is an exactly reconstructible BICUBIC-resized derivative of '+canonical['source_id']+'. The full original pixels are supplied below; source provenance is retained.'
            delivery_entry=canonical or entry
            mime = mimetypes.guess_type(path.name)[0] or 'image/png'
            if self.image_delivery and self.image_delivery.get('mode') in ('lossless_webp_if_smaller_cas','q85_webp_cas'):
                raw,mime,encoded=self._cas_image(delivery_entry,raw)
                audit.append({'kind':'lossy_q85_webp_delivery' if self.image_delivery.get('mode')=='q85_webp_cas' else 'pixel_identical_delivery',**encoded})
            elif self.delivery_manifest:
                encoded=self.delivery_manifest[delivery_entry['sha256']]
                if encoded['format']=='webp':
                    raw=(Path(self.image_delivery['cache_root'])/(delivery_entry['sha256']+'.webp')).read_bytes()
                    mime='image/webp'
                if hashlib.sha256(raw).hexdigest()!=encoded['delivery_sha256']:raise ValueError('Delivery image changed')
                audit.append({'kind':'pixel_identical_delivery','source_sha256':entry['sha256'],
                              'delivery_sha256':encoded['delivery_sha256'],'pixel_sha256':encoded['pixel_sha256'],
                              'width':encoded['width'],'height':encoded['height'],'format':encoded['format']})
            content = [{'type':'text','text':caption},
                       {'type':'image_url','image_url':{'url':f'data:{mime};base64,'+base64.b64encode(raw).decode()}}]
            if self.image_scope:self._scope_image_urls[source_id]=content[1]['image_url']['url']
            operation = 'deliver_image'
        else:
            text = raw.decode('utf-8-sig')
            if path.suffix.lower() == '.json':
                # The source parser may accept a root array; never use GT fields.
                value = json.loads(text)
                text = json.dumps(normalize_log(value, self.images, audit, bool(self.image_scope)), ensure_ascii=False)
            elif self.image_scope:
                text=normalize_inline_images(text,self.images,audit,True)
            content = [{'type':'text','text':f"SOURCE {source_id}: {entry['source']}\n"+text}]
            operation = 'read_text'
        return content, {'source': entry['source'], 'sha256': entry['sha256'], 'operation': operation,
                         'content_complete': True, 'truncated': False, 'transformations': audit,
                         'delivered_sha256': digest(content)}

    def receipt(self, events: list[dict]) -> dict:
        return {'producer':'harness','attempt':self.attempt, 'inventory_complete':True,
                'inventory':self.catalog.entries,'events':events,
                'meaning':'Records transport delivery; does not establish comprehension'}

    def full_content(self) -> tuple[list[dict], list[dict]]:
        content, events = [], []
        for entry in scoped_entries(self.catalog,self.image_scope):
            blocks, event = self.read(entry['source_id'])
            content.extend(blocks)
            events.append(event)
        return content, events
