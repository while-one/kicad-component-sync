r"""A span-preserving S-expression parser for KiCad files.

Why this module exists instead of :mod:`sexpdata` or :mod:`kiutils`
-------------------------------------------------------------------
KiCad symbol libraries are hand-maintained, whitespace-sensitive files that
engineers read and review. A parser that re-serialises the whole tree will
silently reformat unrelated parts of the file, producing enormous, noisy diffs
and destroying comments and indentation.

This parser therefore keeps every node's ``start``/``end`` offsets into the
original source. Callers parse to *locate* structure and then splice bytes back
in, so a file is only ever modified at the exact spans that must change.
Everything outside those spans stays byte-for-byte identical.

The grammar handled is the subset KiCad emits:

* lists -- ``(head child ...)``
* bare symbols -- ``kicad_symbol_lib``, ``yes``, ``0``
* quoted strings -- ``"text with \\"escapes\\" and \\\\ backslashes"``
* ``;`` line comments

This is a hand written recursive-descent parser over a character scanner. No
regular expression is used to interpret structure.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from .exceptions import FileFormatError

__all__ = [
    "SExpNode",
    "SList",
    "SSymbol",
    "SString",
    "parse",
    "quote",
    "TextEdit",
    "apply_edits",
]


@dataclass(frozen=True)
class SExpNode:
    """Base class for parsed S-expression nodes.

    Attributes:
        start: Inclusive character offset of this node in the source text.
        end: Exclusive character offset of this node in the source text.
    """

    start: int
    end: int

    @property
    def span(self) -> tuple[int, int]:
        """Return this node's ``(start, end)`` offsets into the source text."""
        return (self.start, self.end)


@dataclass(frozen=True)
class SSymbol(SExpNode):
    """A bare, unquoted token such as ``yes`` or ``kicad_symbol_lib``.

    Attributes:
        value: The decoded token text.
    """

    value: str

    def __str__(self) -> str:
        """Return the decoded token, matching Python's string protocol."""
        return self.value


@dataclass(frozen=True)
class SString(SExpNode):
    """A double-quoted string, stored decoded.

    Attributes:
        value: The decoded string content, escapes resolved.
        raw: The original text including surrounding quotes.
    """

    value: str
    raw: str

    def __str__(self) -> str:
        """Return the decoded string content, ignoring the raw quoting."""
        return self.value


@dataclass(frozen=True)
class SList(SExpNode):
    """A parenthesised list.

    Attributes:
        head: The first child when it is a symbol or string, else ``None``.
        items: All children, including the head when present.
    """

    items: tuple[SExpNode, ...]

    @property
    def head(self) -> SSymbol | SString | None:
        """Return the leading symbol or string, or ``None`` for ``()``."""
        if self.items and isinstance(self.items[0], SSymbol | SString):
            head = self.items[0]
            if isinstance(head, SSymbol | SString):
                return head
        return None

    @property
    def head_name(self) -> str:
        """Return the decoded head token, or ``""`` for an empty list."""
        head = self.head
        return head.value if head is not None else ""

    def children(self, name: str) -> Iterator[SList]:
        """Iterate over direct children whose head matches ``name``.

        Args:
            name: Head token to match, for example ``"property"``.

        Yields:
            Each matching child list.
        """
        for item in self.items:
            if isinstance(item, SList) and item.head_name == name:
                yield item

    def first(self, name: str) -> SList | None:
        """Return the first direct child with head ``name``, else ``None``.

        Args:
            name: Head token to match.

        Returns:
            The matching child list, or ``None`` when absent.
        """
        return next(self.children(name), None)


_WHITESPACE = " \t\r\n"
_DELIMITERS = _WHITESPACE + "();"
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


class _Scanner:
    """Character scanner over the whole document.

    Args:
        text: The complete source document.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def skip_trivia(self) -> None:
        """Advance past whitespace and ``;`` comments."""
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char in _WHITESPACE:
                self.pos += 1
            elif char == ";":
                newline = self.text.find("\n", self.pos)
                self.pos = len(self.text) if newline == -1 else newline + 1
            else:
                return

    def parse_node(self) -> SExpNode:
        """Parse one node at the current position.

        Returns:
            The parsed node.

        Raises:
            FileFormatError: If the position does not begin a valid node.
        """
        self.skip_trivia()
        if self.pos >= len(self.text):
            raise FileFormatError("Unexpected end of input while parsing S-expression")
        char = self.text[self.pos]
        if char == "(":
            return self._parse_list()
        if char == ")":
            raise FileFormatError(f"Unbalanced ')' at offset {self.pos}")
        if char == '"':
            return self._parse_string()
        return self._parse_symbol()

    def _parse_list(self) -> SList:
        """Parse a parenthesised list starting at the current ``(``."""
        start = self.pos
        self.pos += 1
        items: list[SExpNode] = []
        while True:
            self.skip_trivia()
            if self.pos >= len(self.text):
                raise FileFormatError(f"Unterminated list opened at offset {start}")
            if self.text[self.pos] == ")":
                self.pos += 1
                return SList(start=start, end=self.pos, items=tuple(items))
            items.append(self.parse_node())

    def _parse_string(self) -> SString:
        """Parse a double-quoted string, resolving escape sequences."""
        start = self.pos
        self.pos += 1
        chunks: list[str] = []
        while True:
            if self.pos >= len(self.text):
                raise FileFormatError(f"Unterminated string opened at offset {start}")
            char = self.text[self.pos]
            if char == "\\":
                if self.pos + 1 >= len(self.text):
                    raise FileFormatError(f"Dangling escape at offset {self.pos}")
                chunks.append(_ESCAPES.get(self.text[self.pos + 1], self.text[self.pos + 1]))
                self.pos += 2
                continue
            if char == '"':
                self.pos += 1
                return SString(
                    start=start, end=self.pos, value="".join(chunks), raw=self.text[start:self.pos]
                )
            chunks.append(char)
            self.pos += 1

    def _parse_symbol(self) -> SSymbol:
        """Parse a bare token up to the next delimiter."""
        start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] not in _DELIMITERS:
            self.pos += 1
        if self.pos == start:
            raise FileFormatError(f"Cannot parse token at offset {start}")
        return SSymbol(start=start, end=self.pos, value=self.text[start:self.pos])


def parse(text: str) -> SList:
    """Parse a complete S-expression document.

    Args:
        text: The full document text. Trailing content after the root node is
            tolerated only when it is whitespace or comments.

    Returns:
        The root :class:`SList` node.

    Raises:
        FileFormatError: If the document is empty or has trailing garbage.
    """
    scanner = _Scanner(text)
    root = scanner.parse_node()
    scanner.skip_trivia()
    if scanner.pos < len(scanner.text):
        raise FileFormatError(f"Trailing content at offset {scanner.pos}")
    if not isinstance(root, SList):
        raise FileFormatError("Root node is not a list")
    return root


@dataclass(frozen=True)
class TextEdit:
    """A byte-span replacement to apply to the original document.

    Attributes:
        start: Inclusive start offset to replace.
        end: Exclusive end offset to replace.
        replacement: Text to substitute for the span.
    """

    start: int
    end: int
    replacement: str


def apply_edits(text: str, edits: list[TextEdit]) -> str:
    """Apply non-overlapping edits to ``text`` and return the new document.

    Edits are applied from the end of the document backwards so that earlier
    offsets stay valid. Overlapping edits are rejected rather than silently
    corrupting the file.

    Args:
        text: The original document.
        edits: The replacements to apply.

    Returns:
        The rewritten document.

    Raises:
        FileFormatError: If two edits overlap.
    """
    ordered = sorted(edits, key=lambda edit: edit.start, reverse=True)
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current.end > previous.start:
            raise FileFormatError(
                f"Overlapping edits: [{current.start},{current.end}) and "
                f"[{previous.start},{previous.end})"
            )
    result = text
    for edit in ordered:
        result = result[: edit.start] + edit.replacement + result[edit.end :]
    return result


def quote(value: str) -> str:
    """Return ``value`` escaped and wrapped in double quotes.

    Args:
        value: Raw text to encode.

    Returns:
        A quoted, escaped S-expression string token.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    return f'"{escaped}"'
