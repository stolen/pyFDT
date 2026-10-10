# Copyright 2026 Danil Zagoskin
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Explicit device tree overlay model.

An overlay is an ordered list of :class:`Fragment` objects, each a target
(``&label`` / ``&{/path}``) plus a body node.  The ``fragment@N`` /
``__overlay__`` / ``__symbols__`` / ``__fixups__`` / ``__local_fixups__`` node
form exists only as the projection produced here when a ``.dtbo`` is encoded or
decoded, never as the in-memory model.
"""

from .items import Node, PropWords, PropStrings, Ref
from .misc import line_offset

UNRESOLVED_PHANDLE = 0xFFFFFFFF

_METADATA_NODES = ('__symbols__', '__fixups__', '__local_fixups__')
_PHANDLE_PROPERTIES = ('phandle', 'linux,phandle')


class Fragment:
    """One overlay fragment: a target plus the ``__overlay__`` body."""

    def __init__(self, target=None, target_path=None, body=None, index=0, props=None):
        self.target = target
        self.target_path = target_path
        self.body = body if body is not None else Node('__overlay__')
        self.index = index
        self.props = props if props is not None else []

    def __eq__(self, obj):
        return (isinstance(obj, Fragment) and self.target == obj.target and
                self.target_path == obj.target_path and self.body == obj.body and
                self.index == obj.index and self.props == obj.props)

    def copy(self):
        return Fragment(
            target=self.target.copy() if isinstance(self.target, Ref) else self.target,
            target_path=self.target_path,
            body=self.body.copy(),
            index=self.index,
            props=[prop.copy() for prop in self.props],
        )

    def target_name(self):
        """DTS sugar name (``&label`` / ``&{/path}``) or ``None``."""
        if isinstance(self.target, Ref):
            if self.target.label is not None:
                return '&' + self.target.label
            if self.target.path is not None:
                return '&{' + self.target.path + '}'
        if self.target_path is not None:
            return '&{' + self.target_path + '}'
        return None


class Overlay:
    """An ordered list of fragments plus the metadata needed to project a dtbo."""

    def __init__(self, fragments=None):
        self.fragments = fragments if fragments is not None else []
        self.symbols_node = None
        self.fixups_node = None
        self.local_fixups_node = None

    def __eq__(self, obj):
        return isinstance(obj, Overlay) and self.fragments == obj.fragments


def _join(base, name):
    if base in ('', '/'):
        return '/' + name
    return base.rstrip('/') + '/' + name


def _node_phandle(node):
    for name in _PHANDLE_PROPERTIES:
        prop = node.get_property(name)
        if isinstance(prop, PropWords) and prop.data:
            first = prop.data[0]
            return first.value if isinstance(first, Ref) else first
    return None


def _split_fixup(entry):
    path, prop, offset = entry.rsplit(':', 2)
    return path, prop, int(offset, 0)


def _iter_nodes(node, path):
    yield node, path
    for child in node.nodes:
        yield from _iter_nodes(child, _join(path, child.name))


def _walk_paths(node, path, callback):
    callback(node, path)
    for child in node.nodes:
        _walk_paths(child, _join(path, child.name), callback)


def detect(root):
    """True when ``root`` structurally looks like an overlay."""
    if root is None:
        return False
    for child in root.nodes:
        if child.name.startswith('fragment@') and child.get_subnode('__overlay__') is not None:
            return True
    return False


def _collect_local(node, path, out):
    for prop in node.props:
        offsets = list(prop.data) if getattr(prop, 'data', None) else []
        if offsets:
            out.append((path, prop.name, offsets))
    for child in node.nodes:
        _collect_local(child, _join(path, child.name), out)


def _set_cell(node, prop_name, offset, label=None, path=None, value=None):
    if node is None:
        return
    prop = node.get_property(prop_name)
    if not isinstance(prop, PropWords):
        return
    index = offset // 4
    if index < 0 or index >= len(prop.data):
        return
    cell = prop.data[index]
    current = cell.value if isinstance(cell, Ref) else cell
    if value is None:
        value = current
    prop.data[index] = Ref(label=label, path=path, value=value)


def from_tree(fdt_obj):
    """Build an :class:`Overlay` from a parsed fragment tree and strip metadata."""
    root = fdt_obj.root
    overlay = Overlay()

    node_by_path = {}
    _walk_paths(root, '/', lambda node, path: node_by_path.__setitem__(path, node))

    symbols_node = root.get_subnode('__symbols__')
    path_to_label = {}
    if symbols_node is not None:
        overlay.symbols_node = symbols_node.copy()
        for prop in symbols_node.props:
            value = getattr(prop, 'value', None)
            if isinstance(value, str):
                path_to_label[value] = prop.name
                node = node_by_path.get(value)
                if node is not None:
                    node.set_label(prop.name)

    fixups_node = root.get_subnode('__fixups__')
    if fixups_node is not None:
        overlay.fixups_node = fixups_node.copy()
        for prop in fixups_node.props:
            for entry in (list(prop.data) if isinstance(prop, PropStrings) else []):
                path, prop_name, offset = _split_fixup(entry)
                _set_cell(node_by_path.get(path), prop_name, offset, label=prop.name)

    local_node = root.get_subnode('__local_fixups__')
    if local_node is not None:
        overlay.local_fixups_node = local_node.copy()
        entries = []
        _collect_local(local_node, '/', entries)
        handle_to_path = {}
        for path, node in node_by_path.items():
            handle = _node_phandle(node)
            if handle is not None:
                handle_to_path.setdefault(handle, path)
        for path, prop_name, offsets in entries:
            for offset in offsets:
                node = node_by_path.get(path)
                prop = node.get_property(prop_name) if node is not None else None
                value = None
                if isinstance(prop, PropWords):
                    index = offset // 4
                    if 0 <= index < len(prop.data):
                        cell = prop.data[index]
                        value = cell.value if isinstance(cell, Ref) else cell
                ref_path = handle_to_path.get(value)
                label = path_to_label.get(ref_path) if ref_path else None
                _set_cell(node, prop_name, offset, label=label,
                          path=ref_path if label is None else None, value=value)

    fragments = []
    for child in root.nodes:
        if not child.name.startswith('fragment@'):
            continue
        try:
            index = int(child.name.split('@', 1)[1])
        except ValueError:
            index = len(fragments)
        target_prop = child.get_property('target')
        target_path_prop = child.get_property('target-path')
        target = None
        target_path = None
        if isinstance(target_prop, PropWords) and target_prop.data:
            first = target_prop.data[0]
            if isinstance(first, Ref):
                target = first.copy()
            else:
                target = Ref(value=first)
        elif isinstance(target_path_prop, PropStrings):
            target_path = target_path_prop.value
        extra = [prop for prop in child.props if prop.name not in ('target', 'target-path')]
        body = child.get_subnode('__overlay__')
        if body is None:
            body = Node('__overlay__')
        fragments.append(Fragment(target=target, target_path=target_path,
                                 body=body, index=index, props=extra))
    overlay.fragments = fragments

    for child in list(root.nodes):
        if child.name in _METADATA_NODES or child.name.startswith('fragment@'):
            root.remove_subnode(child.name)

    return overlay


def finalize(fdt_obj):
    """Resolve references and build overlay metadata for a parsed overlay source."""
    overlay = fdt_obj.overlay
    root = fdt_obj.root

    label_to_node = {}
    label_to_path = {}

    def visit(node, path):
        if node.label is not None:
            label_to_node.setdefault(node.label, node)
            label_to_path.setdefault(node.label, path)
        for child in node.nodes:
            visit(child, _join(path, child.name))

    if root is not None:
        if root.label is not None:
            label_to_node.setdefault(root.label, root)
            label_to_path.setdefault(root.label, '/')
        for child in root.nodes:
            visit(child, _join('/', child.name))
    for fragment in overlay.fragments:
        base = '/fragment@{}/__overlay__'.format(fragment.index)
        if fragment.body.label is not None:
            label_to_node.setdefault(fragment.body.label, fragment.body)
            label_to_path.setdefault(fragment.body.label, base)
        for child in fragment.body.nodes:
            visit(child, _join(base, child.name))

    label_to_handle = {}
    handle_to_path = {}
    for label, node in label_to_node.items():
        handle = _node_phandle(node)
        if handle is not None:
            label_to_handle[label] = handle
            handle_to_path.setdefault(handle, label_to_path[label])
            if handle > fdt_obj.last_handle:
                fdt_obj.last_handle = handle

    def assign(label):
        if label in label_to_handle:
            return label_to_handle[label]
        node = label_to_node.get(label)
        if node is None:
            return None
        handle = fdt_obj.add_label(label)
        node.set_property('phandle', handle)
        label_to_handle[label] = handle
        handle_to_path.setdefault(handle, label_to_path[label])
        return handle

    def scan_node(node):
        for prop in node.props:
            if isinstance(prop, PropWords):
                for cell in prop.data:
                    if isinstance(cell, Ref) and cell.label is not None:
                        assign(cell.label)

    if root is not None:
        for node, _ in _iter_nodes(root, '/'):
            scan_node(node)
    for fragment in overlay.fragments:
        if isinstance(fragment.target, Ref) and fragment.target.label is not None:
            assign(fragment.target.label)
        for node, _ in _iter_nodes(fragment.body, '/fragment@{}/__overlay__'.format(fragment.index)):
            scan_node(node)

    for label in label_to_node:
        assign(label)

    fixup_map = {}
    local = []

    def resolve_node(node, path):
        for prop in node.props:
            if not isinstance(prop, PropWords):
                continue
            for index, cell in enumerate(prop.data):
                if not isinstance(cell, Ref):
                    continue
                offset = index * 4
                if cell.label is not None and cell.label in label_to_handle:
                    cell.value = label_to_handle[cell.label]
                    local.append((path, prop.name, offset))
                else:
                    cell.value = UNRESOLVED_PHANDLE
                    if cell.label is not None:
                        fixup_map.setdefault(cell.label, []).append(
                            '{}:{}:{}'.format(path, prop.name, offset))

    if root is not None:
        for node, path in _iter_nodes(root, '/'):
            resolve_node(node, path)
    for fragment in overlay.fragments:
        if isinstance(fragment.target, Ref):
            if fragment.target.label is not None and fragment.target.label in label_to_handle:
                fragment.target.value = label_to_handle[fragment.target.label]
            else:
                fragment.target.value = UNRESOLVED_PHANDLE
                if fragment.target.label is not None:
                    fixup_map.setdefault(fragment.target.label, []).append(
                        '/fragment@{}:target:0'.format(fragment.index))
        base = '/fragment@{}/__overlay__'.format(fragment.index)
        for node, path in _iter_nodes(fragment.body, base):
            resolve_node(node, path)

    if label_to_path:
        symbols = Node('__symbols__')
        for label, path in label_to_path.items():
            symbols.set_property(label, path)
        overlay.symbols_node = symbols
    if fixup_map:
        fixups = Node('__fixups__')
        for label, paths in fixup_map.items():
            fixups.set_property(label, paths)
        overlay.fixups_node = fixups
    if local:
        local_fixups = Node('__local_fixups__')
        for path, prop_name, offset in local:
            _add_local(local_fixups, path, prop_name, offset)
        overlay.local_fixups_node = local_fixups


def _add_local(node, path, prop_name, offset):
    current = node
    for part in [p for p in path.strip('/').split('/') if p]:
        child = current.get_subnode(part)
        if child is None:
            child = Node(part)
            current.append(child)
        current = child
    prop = current.get_property(prop_name)
    if prop is None:
        current.set_property(prop_name, [offset])
    elif offset not in prop.data:
        prop.data.append(offset)


def project(fdt_obj):
    """Project the overlay model into the on-disk fragment/metadata node tree."""
    overlay = fdt_obj.overlay
    root = Node('/')
    if fdt_obj.root is not None:
        for prop in fdt_obj.root.props:
            root.append(prop.copy())
        for child in fdt_obj.root.nodes:
            root.append(child.copy())
    for fragment in overlay.fragments:
        fragment_node = Node('fragment@{}'.format(fragment.index))
        if fragment.target is not None:
            fragment_node.append(PropWords('target', fragment.target))
        elif fragment.target_path is not None:
            fragment_node.append(PropStrings('target-path', fragment.target_path))
        for prop in fragment.props:
            fragment_node.append(prop.copy())
        fragment_node.append(fragment.body.copy())
        root.append(fragment_node)
    if overlay.symbols_node is not None:
        root.append(overlay.symbols_node.copy())
    if overlay.fixups_node is not None:
        root.append(overlay.fixups_node.copy())
    if overlay.local_fixups_node is not None:
        root.append(overlay.local_fixups_node.copy())
    return root


def sugar_eligible(fdt_obj):
    """True when the overlay can be rendered as dtc-style reference sugar."""
    overlay = fdt_obj.overlay
    if overlay is None:
        return False
    if fdt_obj.root is not None and (fdt_obj.root.props or fdt_obj.root.nodes):
        return False
    indices = [fragment.index for fragment in overlay.fragments]
    if sorted(indices) != list(range(len(overlay.fragments))):
        return False
    for fragment in overlay.fragments:
        if fragment.props:
            return False
        if (fragment.target is None) == (fragment.target_path is None):
            return False
        if fragment.target is not None:
            if not isinstance(fragment.target, Ref):
                return False
            if fragment.target.label is None and fragment.target.path is None:
                return False
        if fragment.target_path is not None and not isinstance(fragment.target_path, str):
            return False
    return True


def _node_to_sugar(node, tabsize, depth):
    label = (node.label + ': ') if node.label is not None else ''
    result = line_offset(tabsize, depth, label + node.name + ' {\n')
    for prop in node.props:
        if node.label is not None and prop.name in _PHANDLE_PROPERTIES:
            continue
        result += prop.to_dts(tabsize, depth + 1)
    for child in node.nodes:
        result += _node_to_sugar(child, tabsize, depth + 1)
    result += line_offset(tabsize, depth, '};\n')
    return result


def render_sugar(fdt_obj, tabsize=4):
    """Render the overlay in root-level reference sugar, or ``None``."""
    if not sugar_eligible(fdt_obj):
        return None
    result = ''
    for fragment in fdt_obj.overlay.fragments:
        name = fragment.target_name()
        result += line_offset(tabsize, 0, name + ' {\n')
        for prop in fragment.body.props:
            result += prop.to_dts(tabsize, 1)
        for child in fragment.body.nodes:
            result += _node_to_sugar(child, tabsize, 1)
        result += line_offset(tabsize, 0, '};\n')
    return result
