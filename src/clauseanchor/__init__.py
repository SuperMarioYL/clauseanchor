"""ClauseAnchor — anchor every conclusion to the clause it came from."""

from clauseanchor.anchors import Anchor, AnchorError, resolve, verify_quote
from clauseanchor.document import Clause, Document, DocumentError, load

__all__ = [
    "Anchor",
    "AnchorError",
    "Clause",
    "Document",
    "DocumentError",
    "load",
    "resolve",
    "verify_quote",
]
