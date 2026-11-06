"""Caller-bound human gallery images; no causal rules or model judgements."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re

from harness.protocol import canonical_path, digest

IMAGE_URI = re.compile(r'data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/]+={0,2}')
SOURCE_CAPTION = re.compile(r'^SOURCE (S\d+): ([^\r\n]+)')


def validate_scope(catalog, scope):
    if scope is None:
        return None
    if scope.get('mode') != 'human_visible_images':
        raise ValueError('Unsupported public image scope')
    images = scope.get('ordered_images')
    if not isinstance(images, list) or not images:
        raise ValueError('An ordered human image list is required')
    paths = []
    for index, item in enumerate(images):
        if type(item.get('display_index')) is not int or item['display_index'] != index:
            raise ValueError('Website image indices must be contiguous and ordered')
        source = canonical_path(item['source'])
        entry = catalog.by_path.get(source)
        if not entry or entry['kind'] != 'image' or entry['sha256'] != item.get('sha256'):
            raise ValueError('Human image scope/catalog mismatch: ' + source)
        paths.append(source)
    if len(paths) != len(set(paths)) or set(paths) != set(catalog.image_paths):
        raise ValueError('Public image catalog must exactly equal the human gallery')
    if scope.get('sha256') != digest(images):
        raise ValueError('Public image scope hash mismatch')
    return copy.deepcopy(scope)


def scoped_entries(catalog, scope):
    if scope is None:
        return catalog.entries
    ordered = [catalog.by_path[i['source']] for i in scope['ordered_images']]
    # Keep the ordinary log order, replacing the image group by gallery order.
    result, inserted = [], False
    for entry in catalog.entries:
        if entry['kind'] == 'image':
            if not inserted:
                result.extend(ordered)
                inserted = True
        else:
            result.append(entry)
    return result


def image_reference(uri, images, audit, scoped):
    raw = base64.b64decode(uri.split(',', 1)[1], validate=True)
    sha = hashlib.sha256(raw).hexdigest()
    if sha in images:
        audit.append({'kind': 'embedded_image_reference', 'image_sha256': sha,
                      'source_id': images[sha], 'text_omitted': False})
        return '[Original image supplied separately: ' + images[sha] + ']'
    if not scoped:
        raise ValueError('Embedded image absent from complete image inventory: ' + sha)
    audit.append({'kind': 'image_attachment_outside_human_gallery', 'image_sha256': sha,
                  'image_delivered': False, 'nonimage_log_text_omitted': False})
    return '[Image attachment outside the shared human-visible gallery; SHA256 ' + sha + '; not supplied as an additional image]'


def normalize_inline_images(text, images, audit, scoped):
    return IMAGE_URI.sub(lambda match: image_reference(match.group(), images, audit, scoped), text)


def scoped_request_images(request, store):
    """Keep each gallery source once; distinct equal-pixel gallery files survive.

    C/D can revisit public sources. Replayed views of the same source do not
    become additional gallery frames. Every actual image must be source-bound.
    """
    # A caller can reuse the same Python message object while constructing
    # history. JSON has independent occurrences; mirror that before mutation.
    result = {key:copy.deepcopy(value) for key,value in request.items() if key!='messages'}
    result['messages']=[copy.deepcopy(message) for message in request.get('messages',[])]
    seen, audit = {}, []
    for mi, message in enumerate(result.get('messages', [])):
        parts = message.get('content')
        if not isinstance(parts, list):
            continue
        current = None
        for pi, part in enumerate(parts):
            if part.get('type') == 'text':
                match = SOURCE_CAPTION.match(part.get('text', ''))
                current = None
                if match:
                    entry = store.catalog.by_id.get(match[1])
                    if entry and entry['kind'] == 'image' and match[2] == entry['source']:
                        current = entry['source_id']
                if IMAGE_URI.search(part.get('text', '')):
                    raise ValueError('Inline binary image escaped public-scope normalization')
            elif part.get('type') == 'image_url':
                if current is None:
                    raise ValueError('Public image lacks its explicit source caption')
                expected=store._scope_image_urls.get(current)
                if expected is None:
                    raise ValueError('Public image has not been returned by the scoped evidence store')
                if part['image_url']['url'] != expected:
                    raise ValueError('Image differs from the bound human-visible source')
                if current in seen:
                    first = seen[current]
                    parts[pi] = {'type': 'text', 'text': 'Public image source ' + current + ' was already supplied earlier in this conversation; this is a repeated view of that same source.'}
                    audit.append({'kind': 'same_source_repeated_view', 'source_id': current,
                                  'message': mi, 'part': pi, 'first_message': first[0], 'first_part': first[1]})
                else:
                    seen[current] = (mi, pi)
                current = None
    return result, audit
