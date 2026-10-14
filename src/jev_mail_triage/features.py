"""Local text features for the gate.

Only one-way hashes of tokens and MinHash signatures are derived here. Neither is
reversible into the original text, although a hashed token can be confirmed by
guessing the word, so stored counts still reveal which words occur in training mail.
"""

from __future__ import annotations

import hashlib
import re
import struct
from itertools import pairwise
from urllib.parse import urlsplit

from .models import PreparedMessage

_WORD_RE = re.compile(r"[^\W\d_]{2,30}")
_DIGITS_RE = re.compile(r"\d+")
_MAX_FEATURE_CHARS = 20_000
_SIGNATURE_SIZE = 64
_SHINGLE_WORDS = 3
_MIN_SHINGLES = 8
_MERSENNE_PRIME = (1 << 61) - 1
_PERMUTATIONS = tuple(
    (
        int.from_bytes(hashlib.blake2b(f"a{index}".encode(), digest_size=8).digest()) | 1,
        int.from_bytes(hashlib.blake2b(f"b{index}".encode(), digest_size=8).digest()),
    )
    for index in range(_SIGNATURE_SIZE)
)


def token_hash(token: str) -> int:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest) >> 1


def link_domain(url: str) -> str | None:
    try:
        host = urlsplit(url.strip()).hostname
    except ValueError:
        return None
    if not host:
        return None
    labels = host.casefold().rstrip(".").split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else labels[0]


def link_domains(prepared: PreparedMessage) -> set[str]:
    return {domain for link in prepared.links if (domain := link_domain(link))}


def link_token(domain: str) -> int:
    return token_hash(f"url:{domain}")


def sender_domain(prepared: PreparedMessage) -> str | None:
    address = prepared.sender_address
    return address.rsplit("@", 1)[1].rstrip(".") if "@" in address else None


def tokens(prepared: PreparedMessage) -> set[int]:
    """Presence-only hashed features: subject words, body words and bigrams, domains."""
    features: set[str] = set()
    for word in _WORD_RE.findall(prepared.subject.casefold()):
        features.add(f"s:{word}")
    words = _WORD_RE.findall(prepared.body[:_MAX_FEATURE_CHARS].casefold())
    features.update(f"w:{word}" for word in words)
    features.update(f"b:{first} {second}" for first, second in pairwise(words))
    domain = sender_domain(prepared)
    if domain:
        features.add(f"from:{domain}")
    features.update(f"url:{domain}" for domain in link_domains(prepared))
    return {token_hash(feature) for feature in features}


def signature(prepared: PreparedMessage) -> bytes | None:
    """MinHash of word shingles; digits are normalized to catch per-recipient variants."""
    text = _DIGITS_RE.sub("0", f"{prepared.subject}\n{prepared.body[:_MAX_FEATURE_CHARS]}")
    words = _WORD_RE.findall(text.casefold()) or text.casefold().split()
    shingles = {
        " ".join(words[index : index + _SHINGLE_WORDS])
        for index in range(max(0, len(words) - _SHINGLE_WORDS + 1))
    }
    if len(shingles) < _MIN_SHINGLES:
        return None
    hashed = [token_hash(shingle) for shingle in shingles]
    minimums = [
        min((a * value + b) % _MERSENNE_PRIME for value in hashed) for a, b in _PERMUTATIONS
    ]
    return struct.pack(f">{_SIGNATURE_SIZE}Q", *minimums)


def similarity(first: bytes, second: bytes) -> float:
    """Estimated Jaccard similarity of two MinHash signatures."""
    if len(first) != len(second) or not first:
        return 0.0
    left = struct.unpack(f">{_SIGNATURE_SIZE}Q", first)
    right = struct.unpack(f">{_SIGNATURE_SIZE}Q", second)
    return sum(a == b for a, b in zip(left, right)) / _SIGNATURE_SIZE
