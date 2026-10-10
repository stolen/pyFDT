# Copyright 2017 Martin Olejar
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

"""Tokenizer and recursive-descent parser for Device Tree Source (DTS).

The parser produces (or populates) an :class:`~fdt.FDT` object.  It understands
base device trees and ``/plugin/`` overlays, including root-level reference
sugar (``&label { ... }`` / ``&{/path} { ... }``) and symbolic references.
Preprocessing is external: cpp line markers are ignored and unresolved
preprocessor directives are reported as errors.
"""

import os

from .items import Node, Property, PropWords, PropBytes, PropStrings, PropIncBin, Ref
from .misc import get_version_info
from . import overlay as _overlay


class DtsSyntaxError(Exception):
    """Raised when the DTS input cannot be tokenized or parsed."""

    def __init__(self, message, line=None, column=None):
        self.line = line
        self.column = column
        if line is not None:
            message = "{} at line {} column {}".format(message, line, column)
        super().__init__(message)


class Token:
    __slots__ = ("kind", "value", "line", "column")

    def __init__(self, kind, value, line, column):
        self.kind = kind
        self.value = value
        self.line = line
        self.column = column

    def __repr__(self):
        return "Token({!r}, {!r})".format(self.kind, self.value)


_DIRECTIVE_WORDS = frozenset({
    "dts-v1", "plugin", "memreserve", "delete-node", "delete-property", "incbin", "bits",
})

_HASH_DIRECTIVES = frozenset({
    "include", "define", "if", "ifdef", "ifndef", "else", "elif", "endif",
    "undef", "pragma", "error", "warning",
})

_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.,@#+?*$")

_PUNCT = frozenset("{};=<>[],:()")

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

_ESCAPES = {
    'a': '\a', 'b': '\b', 't': '\t', 'n': '\n', 'v': '\v', 'f': '\f',
    'r': '\r', '\\': '\\', '"': '"', "'": "'", '0': '\0',
}


def tokenize(text):
    """Turn DTS source text into a list of :class:`Token` (terminated by eof)."""
    tokens = []
    i = 0
    n = len(text)
    line = 1
    column = 1
    in_bytes = 0

    def error(message):
        raise DtsSyntaxError(message, line, column)

    while i < n:
        c = text[i]
        if c == '\n':
            i += 1
            line += 1
            column = 1
            continue
        if c in ' \t\r':
            i += 1
            column += 1
            continue
        if text.startswith('/*', i):
            j = text.find('*/', i + 2)
            if j < 0:
                error("Unterminated block comment")
            segment = text[i:j + 2]
            newlines = segment.count('\n')
            if newlines:
                line += newlines
                column = len(segment) - segment.rfind('\n')
            else:
                column += len(segment)
            i = j + 2
            continue
        if text.startswith('//', i):
            j = text.find('\n', i)
            i = n if j < 0 else j
            continue
        if c == '"':
            value, i, line, column = _scan_string(text, i, line, column)
            tokens.append(Token('string', value, line, column))
            continue
        if c == '&':
            value, i, column = _scan_ref(text, i, column, error)
            tokens.append(Token('ref', value, line, column))
            continue
        if c == '/':
            j = i + 1
            while j < n and text[j] not in '/ \t\r\n':
                j += 1
            word = text[i + 1:j]
            if j < n and text[j] == '/' and word in _DIRECTIVE_WORDS:
                tokens.append(Token('directive', word, line, column))
                column += (j + 1 - i)
                i = j + 1
                continue
            tokens.append(Token('name', '/', line, column))
            i += 1
            column += 1
            continue
        if c == '#':
            if column == 1 and (text.startswith('# ', i) or text.startswith('#line', i)):
                j = text.find('\n', i)
                i = n if j < 0 else j
                continue
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == '_'):
                j += 1
            word = text[i + 1:j]
            if word in _HASH_DIRECTIVES:
                tokens.append(Token('hash', word, line, column))
                column += (j - i)
                i = j
                continue
            k = i
            while k < n and text[k] in _NAME_CHARS:
                k += 1
            tokens.append(Token('name', text[i:k], line, column))
            column += (k - i)
            i = k
            continue
        if in_bytes and c in _HEX_DIGITS:
            k = i
            while k < n and text[k] in _HEX_DIGITS:
                k += 1
            tokens.append(Token('hexbyte', int(text[i:k], 16), line, column))
            column += (k - i)
            i = k
            continue
        if c in _PUNCT:
            if c == '[':
                in_bytes += 1
            elif c == ']':
                in_bytes = max(0, in_bytes - 1)
            tokens.append(Token('punct', c, line, column))
            i += 1
            column += 1
            continue
        if c.isdigit():
            value, i, column = _scan_number(text, i, column)
            tokens.append(Token('number', value, line, column))
            continue
        if c in _NAME_CHARS:
            k = i
            while k < n and text[k] in _NAME_CHARS:
                k += 1
            tokens.append(Token('name', text[i:k], line, column))
            column += (k - i)
            i = k
            continue
        error("Unexpected character {!r}".format(c))

    tokens.append(Token('eof', None, line, column))
    return tokens


def _scan_string(text, i, line, column):
    n = len(text)
    i += 1
    buf = []
    while i < n and text[i] != '"':
        ch = text[i]
        if ch == '\\':
            i += 1
            if i >= n:
                raise DtsSyntaxError("Unterminated string", line, column)
            e = text[i]
            if e in _ESCAPES:
                buf.append(_ESCAPES[e])
            elif e == 'x':
                j = i + 1
                k = j
                while k < n and k < j + 2 and text[k] in _HEX_DIGITS:
                    k += 1
                buf.append(chr(int(text[j:k], 16)))
                i = k
                continue
            else:
                buf.append(e)
            i += 1
        else:
            if ch == '\n':
                line += 1
                column = 1
            buf.append(ch)
            i += 1
    if i >= n:
        raise DtsSyntaxError("Unterminated string", line, column)
    return ''.join(buf), i + 1, line, column


def _scan_ref(text, i, column, error):
    n = len(text)
    if i + 1 < n and text[i + 1] == '{':
        j = text.find('}', i + 2)
        if j < 0:
            error("Unterminated path reference")
        return ('path', text[i + 2:j]), j + 1, column + (j + 1 - i)
    j = i + 1
    while j < n and (text[j].isalnum() or text[j] in '_-.'):
        j += 1
    label = text[i + 1:j]
    if not label:
        error("Invalid reference")
    return ('label', label), j, column + (j - i)


def _scan_number(text, i, column):
    n = len(text)
    if text.startswith(('0x', '0X'), i):
        j = i + 2
        while j < n and text[j] in _HEX_DIGITS:
            j += 1
        return int(text[i:j], 16), j, column + (j - i)
    if text.startswith(('0b', '0B'), i):
        j = i + 2
        while j < n and text[j] in '01':
            j += 1
        return int(text[i:j], 2), j, column + (j - i)
    j = i
    while j < n and text[j].isdigit():
        j += 1
    value = text[i:j]
    if value.startswith('0') and len(value) > 1:
        return int(value, 8), j, column + (j - i)
    return int(value, 10), j, column + (j - i)


def _make_ref(value):
    if value[0] == 'label':
        return Ref(label=value[1])
    return Ref(path=value[1])


class _Parser:

    def __init__(self, tokens, fdt_obj, root_dir):
        self.tokens = tokens
        self.pos = 0
        self.fdt = fdt_obj
        self.root_dir = root_dir
        self.plugin = False
        self.fragments = []

    def _peek(self, ahead=0):
        index = self.pos + ahead
        if index >= len(self.tokens):
            return self.tokens[-1]
        return self.tokens[index]

    def _next(self):
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def _at(self, kind, value=None):
        token = self._peek()
        return token.kind == kind and (value is None or token.value == value)

    def _expect(self, kind, value=None):
        token = self._peek()
        if token.kind != kind or (value is not None and token.value != value):
            raise DtsSyntaxError(
                "Expected {} {!r}, got {} {!r}".format(kind, value, token.kind, token.value),
                token.line, token.column)
        return self._next()

    def parse(self):
        while not self._at('eof'):
            token = self._peek()
            if token.kind == 'directive':
                self._parse_directive(token)
            elif token.kind == 'punct' and token.value == ';':
                self._next()
            elif token.kind == 'hash':
                raise DtsSyntaxError(
                    "Unresolved preprocessor directive '#{}'; preprocess with cpp first".format(token.value),
                    token.line, token.column)
            else:
                self._parse_top_item()
        return self.fdt

    def _parse_directive(self, token):
        if token.value == 'dts-v1':
            self._next()
            self._expect('punct', ';')
        elif token.value == 'plugin':
            self._next()
            self._expect('punct', ';')
            self.plugin = True
        elif token.value == 'memreserve':
            self._next()
            address = self._expect('number').value
            size = self._expect('number').value
            self._expect('punct', ';')
            self.fdt.entries.append({'address': address, 'size': size})
        else:
            raise DtsSyntaxError(
                "Unexpected directive /{}/".format(token.value), token.line, token.column)

    def _parse_top_item(self):
        label = None
        token = self._peek()
        if token.kind == 'name' and self._peek(1).kind == 'punct' and self._peek(1).value == ':':
            label = token.value
            self._next()
            self._next()
            token = self._peek()
        if token.kind == 'ref':
            self._next()
            self._parse_reference_block(token.value)
            return
        if token.kind == 'name':
            name = token.value
            self._next()
            self._expect('punct', '{')
            if name == '/':
                if self.fdt.root is None:
                    self.fdt.root = Node('/')
                node = self.fdt.root
            else:
                node = Node(name)
                if label is not None:
                    node.set_label(label)
                if self.fdt.root is None:
                    self.fdt.root = Node('/')
                self.fdt.root.append(node)
            self._parse_node_body(node)
            self._maybe_semicolon()
            return
        raise DtsSyntaxError(
            "Unexpected token {} {!r}".format(token.kind, token.value), token.line, token.column)

    def _parse_reference_block(self, ref_value):
        body = Node('__overlay__')
        fragment = _overlay.Fragment(
            body=body,
            index=len(self.fragments),
            target=_make_ref(ref_value) if ref_value[0] == 'label' else None,
            target_path=ref_value[1] if ref_value[0] == 'path' else None,
        )
        self._expect('punct', '{')
        self._parse_node_body(body)
        self._maybe_semicolon()
        self.fragments.append(fragment)

    def _parse_node_body(self, node):
        while True:
            token = self._peek()
            if token.kind == 'punct' and token.value == '}':
                self._next()
                self._close_node(node)
                return
            if token.kind == 'eof':
                raise DtsSyntaxError("Unexpected end of input, expected '}'", token.line, token.column)
            if token.kind == 'directive':
                if token.value in ('delete-node', 'delete-property'):
                    self._next()
                    while not (self._peek().kind == 'punct' and self._peek().value == ';'):
                        if self._peek().kind == 'eof':
                            raise DtsSyntaxError("Unexpected end of input", token.line, token.column)
                        self._next()
                    self._next()
                    continue
                raise DtsSyntaxError(
                    "Unexpected directive /{}/".format(token.value), token.line, token.column)
            if token.kind == 'hash':
                raise DtsSyntaxError(
                    "Unresolved preprocessor directive '#{}'; preprocess with cpp first".format(token.value),
                    token.line, token.column)
            label = None
            if token.kind == 'name' and self._peek(1).kind == 'punct' and self._peek(1).value == ':':
                label = token.value
                self._next()
                self._next()
                token = self._peek()
            if token.kind == 'name':
                nxt = self._peek(1)
                if nxt.kind == 'punct' and nxt.value == '=':
                    self._parse_property(node, token.value)
                    continue
                if nxt.kind == 'punct' and nxt.value == ';':
                    self._next()
                    self._next()
                    node.append(Property(token.value))
                    continue
                if nxt.kind == 'punct' and nxt.value == '{':
                    self._next()
                    self._next()
                    child = Node(token.value)
                    if label is not None:
                        child.set_label(label)
                    self._parse_node_body(child)
                    node.append(child)
                    self._maybe_semicolon()
                    continue
                raise DtsSyntaxError(
                    "Unexpected token after name {!r}".format(token.value), token.line, token.column)
            raise DtsSyntaxError(
                "Unexpected token {} {!r}".format(token.kind, token.value), token.line, token.column)

    def _parse_property(self, node, name):
        self._next()
        self._expect('punct', '=')
        prop = self._parse_value(name)
        self._expect('punct', ';')
        node.append(prop)

    def _parse_value(self, name):
        token = self._peek()
        if token.kind == 'punct' and token.value == '<':
            return self._parse_cells(name)
        if token.kind == 'punct' and token.value == '[':
            return self._parse_bytes(name)
        if token.kind == 'directive' and token.value == 'incbin':
            return self._parse_incbin(name)
        if token.kind == 'directive' and token.value == 'bits':
            raise NotImplementedError("Not implemented property value: /bits/")
        if token.kind == 'string':
            return self._parse_strings(name)
        raise DtsSyntaxError(
            "Invalid property value {} {!r}".format(token.kind, token.value), token.line, token.column)

    def _parse_cells(self, name):
        prop = PropWords(name)
        while True:
            self._expect('punct', '<')
            while not (self._peek().kind == 'punct' and self._peek().value == '>'):
                token = self._peek()
                if token.kind == 'number':
                    self._next()
                    prop.append(token.value)
                elif token.kind == 'ref':
                    self._next()
                    reference = _make_ref(token.value)
                    if self.plugin:
                        prop.append(reference)
                    elif reference.label is not None:
                        prop.append(self.fdt.add_label(reference.label))
                    else:
                        raise DtsSyntaxError(
                            "Path reference is only valid in an overlay", token.line, token.column)
                else:
                    raise DtsSyntaxError(
                        "Invalid cell value {} {!r}".format(token.kind, token.value),
                        token.line, token.column)
            self._expect('punct', '>')
            if (self._peek().kind == 'punct' and self._peek().value == ',' and
                    self._peek(1).kind == 'punct' and self._peek(1).value == '<'):
                self._next()
                continue
            break
        return prop

    def _parse_bytes(self, name):
        self._expect('punct', '[')
        prop = PropBytes(name)
        while not (self._peek().kind == 'punct' and self._peek().value == ']'):
            token = self._peek()
            if token.kind == 'hexbyte':
                self._next()
                prop.append(token.value)
            else:
                raise DtsSyntaxError(
                    "Invalid byte value {} {!r}".format(token.kind, token.value),
                    token.line, token.column)
        self._expect('punct', ']')
        return prop

    def _parse_strings(self, name):
        prop = PropStrings(name)
        while True:
            token = self._expect('string')
            prop.append(token.value)
            if self._peek().kind == 'punct' and self._peek().value == ',':
                self._next()
                continue
            break
        return prop

    def _parse_incbin(self, name):
        self._next()
        self._expect('punct', '(')
        parts = [self._expect('string').value]
        while self._peek().kind == 'punct' and self._peek().value == ',':
            self._next()
            parts.append(self._expect('number').value)
        self._expect('punct', ')')
        file_path = os.path.join(self.root_dir, parts[0].strip())
        if not os.path.exists(file_path):
            raise DtsSyntaxError("File path doesn't exist: {}".format(file_path))
        file_offset = parts[1] if len(parts) > 1 else 0
        file_size = parts[2] if len(parts) > 2 else 0
        with open(file_path, "rb") as handle:
            handle.seek(file_offset)
            prop_data = handle.read(file_size) if file_size > 0 else handle.read()
        return PropIncBin(name, prop_data, os.path.split(file_path)[1])

    def _close_node(self, node):
        if not self.plugin and node.label is not None and node.get_property('phandle') is None:
            node.set_property('phandle', self.fdt.add_label(node.label))

    def _maybe_semicolon(self):
        if self._peek().kind == 'punct' and self._peek().value == ';':
            self._next()


def parse_into(fdt_obj, text, root_dir=''):
    """Parse DTS ``text`` and populate ``fdt_obj`` (root, entries, header, overlay)."""
    version = get_version_info(text)
    parser = _Parser(tokenize(text), fdt_obj, root_dir)
    parser.parse()

    if 'version' in version:
        fdt_obj.header.version = version['version']
    elif parser.plugin:
        fdt_obj.header.version = fdt_obj.header.MAX_VERSION
    if 'last_comp_version' in version:
        fdt_obj.header.last_comp_version = version['last_comp_version']
    if 'boot_cpuid_phys' in version:
        fdt_obj.header.boot_cpuid_phys = version['boot_cpuid_phys']

    if parser.fragments:
        fdt_obj.overlay = _overlay.Overlay(parser.fragments)
        _overlay.finalize(fdt_obj)
    elif parser.plugin:
        root = fdt_obj.root
        compiled = root is not None and any(
            root.get_subnode(name) is not None for name in _overlay._METADATA_NODES)
        if compiled:
            fdt_obj.overlay = _overlay.from_tree(fdt_obj)
        else:
            if root is not None and _overlay.detect(root):
                fdt_obj.overlay = _overlay.from_tree(fdt_obj)
            else:
                fdt_obj.overlay = _overlay.Overlay([])
            _overlay.finalize(fdt_obj)
    elif fdt_obj.root is not None and _overlay.detect(fdt_obj.root):
        fdt_obj.overlay = _overlay.from_tree(fdt_obj)
    return fdt_obj
