"""MCP 툴 결과를 모델 컨텍스트에 넣기 전에 줄이는 계층.

핵심 아이디어 — 툴 결과 원문은 모델에게 절대 보여주지 않는다.
원문은 디스크로 흘려보내고(spill), 모델의 메시지에는 "무엇이 몇 건 들어왔다"는
영수증만 남긴다. 실제 내용은 session RAG(rag.py)가 질문에 맞는 조각만 골라
마지막 생성 단계에서 한 번 주입한다.

줄이는 순서는 '덜 파괴적인 것부터'다.
  1. 텍스트 블록 전부 회수 — TextContent 만 보면 EmbeddedResource 안의 본문을 통째로 놓친다.
  2. JSON 은 필드 투영으로 줄인다. 문자수로 자르면 파싱 불가능한 쓰레기가 된다.
  3. 산문은 노이즈(base64·추적 URL·HTML)를 먼저 걷어낸다. 자르기는 그다음이다.
  4. 그래도 넘치면 spill 하고 핸들만 남긴다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

# 내용이 아니라 배관(plumbing)인 키들. 투영에서 제외한다.
_DROP_KEYS = {
    "_meta", "annotations", "mimeType", "blob", "embedding", "vector",
    "raw", "html", "rawHtml", "base64", "thumbnail", "icon", "logo",
}
# 있으면 거의 항상 본문인 키들. 투영에서 우선 확보한다.
_TEXT_KEYS = ("content", "text", "body", "snippet", "summary", "abstract", "description", "value")
_TITLE_KEYS = ("title", "name", "heading", "subject", "documentName", "docTitle")
_URL_KEYS = ("url", "uri", "link", "source_url", "href")
_UID_KEYS = ("cite_uid", "citeUid", "uid", "id", "doc_id", "documentId", "node_id")

_BASE64_RE = re.compile(r"(?:[A-Za-z0-9+/]{120,}={0,2})")
_DATA_URI_RE = re.compile(r"data:[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+")
_SCRIPT_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]{1,200}>")
_TABLE_RULE_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$", re.M)
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
_WS_RE = re.compile(r"[ \t ]{2,}")
_NL_RE = re.compile(r"\n{3,}")


def strip_noise(text: str) -> str:
    """의미를 거의 잃지 않으면서 토큰만 먹는 것들을 걷어낸다."""
    if not text:
        return ""
    text = _DATA_URI_RE.sub("[data-uri]", text)
    text = _SCRIPT_RE.sub(" ", text)
    if text.count("<") > 8 and _TAG_RE.search(text):
        text = _TAG_RE.sub(" ", text)
    text = _BASE64_RE.sub("[blob]", text)
    text = _TABLE_RULE_RE.sub("", text)
    # 추적 파라미터가 붙은 URL 은 경로까지만 남긴다. utm_* 하나가 30 토큰씩 먹는다.
    text = _URL_RE.sub(lambda m: m.group(0).split("?")[0][:120], text)
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n\n", text)
    return text.strip()


@dataclass(slots=True)
class Document:
    """RAG 색인 단위가 되는, 인용 가능한 근거 조각 하나."""

    cite_uid: str
    text: str
    title: str = ""
    url: str = ""
    tool: str = ""

    def header(self) -> str:
        parts = [self.title.strip()] if self.title.strip() else []
        if self.url:
            parts.append(self.url)
        return " · ".join(parts)


def _first(value: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        found = value.get(key)
        if isinstance(found, str) and found.strip():
            return found.strip()
        if isinstance(found, (int, float)):
            return str(found)
    return ""


def _flatten_text(value: Any, depth: int = 0) -> str:
    """dict/list 안에 흩어진 문자열을 사람이 읽는 순서로 이어붙인다."""
    if depth > 6:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(part for part in (_flatten_text(v, depth + 1) for v in value) if part)
    if isinstance(value, dict):
        parts: list[str] = []
        for key, child in value.items():
            if key in _DROP_KEYS:
                continue
            rendered = _flatten_text(child, depth + 1)
            if not rendered:
                continue
            if key in _TEXT_KEYS or key in _TITLE_KEYS:
                parts.append(rendered)
            else:
                parts.append(f"{key}: {rendered}")
        return "\n".join(parts)
    return ""


def _iter_result_payloads(result: Any) -> Iterator[Any]:
    """MCP 결과에서 실제 내용이 들어 있는 노드를 모두 뽑는다.

    TextContent 만 읽으면 EmbeddedResource 안에 숨은 본문을 통째로 놓친다.
    실제로 어떤 서버는 31 토큰짜리 상태 줄만 TextContent 로 주고 19K 짜리 본문은
    resource 안에 넣는다.
    """
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                kind = item.get("type")
                if kind == "text" and isinstance(item.get("text"), str):
                    yield item["text"]
                elif kind in ("resource", "resource_link"):
                    resource = item.get("resource")
                    if isinstance(resource, dict):
                        if isinstance(resource.get("text"), str):
                            yield resource["text"]
                        else:
                            yield resource
                    else:
                        yield item
                elif kind is None:
                    yield item
        structured = result.get("structuredContent")
        if structured is not None:
            yield structured
        if not result.get("content") and structured is None:
            yield result
        return
    yield result


def _looks_like_document(value: dict[str, Any]) -> bool:
    has_uid = any(key in value for key in _UID_KEYS)
    has_text = any(key in value for key in _TEXT_KEYS)
    return has_uid and has_text


def _collect_documents(value: Any, tool: str, output: list[Document], depth: int = 0) -> None:
    if depth > 7:
        return
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                _collect_documents(json.loads(stripped), tool, output, depth + 1)
                return
            except json.JSONDecodeError:
                pass
        cleaned = strip_noise(stripped)
        if cleaned:
            output.append(Document(cite_uid="", text=cleaned, tool=tool))
        return
    if isinstance(value, list):
        for child in value:
            _collect_documents(child, tool, output, depth + 1)
        return
    if not isinstance(value, dict):
        return
    if _looks_like_document(value):
        # 제목·URL·uid 는 필드로 따로 들고 간다. 본문에 또 넣으면 같은 문자열에 두 번
        # 값을 치르고, 청크마다 제목이 반복돼 근거 예산을 갉아먹는다.
        skip = set(_UID_KEYS) | set(_URL_KEYS) | set(_TITLE_KEYS)
        body = strip_noise(_flatten_text({k: v for k, v in value.items() if k not in skip}))
        if not body:
            body = strip_noise(_flatten_text({k: v for k, v in value.items() if k not in _UID_KEYS}))
        if body:
            output.append(Document(
                cite_uid=_first(value, _UID_KEYS),
                text=body,
                title=_first(value, _TITLE_KEYS),
                url=_first(value, _URL_KEYS),
                tool=tool,
            ))
        return
    # 문서처럼 안 생겼으면 자식으로 내려간다. 자식에서도 못 찾으면 통째로 평탄화한다.
    before = len(output)
    for key, child in value.items():
        if key in _DROP_KEYS:
            continue
        if isinstance(child, (dict, list)):
            _collect_documents(child, tool, output, depth + 1)
    if len(output) == before:
        body = strip_noise(_flatten_text(value))
        if body:
            output.append(Document(
                cite_uid=_first(value, _UID_KEYS),
                text=body,
                title=_first(value, _TITLE_KEYS),
                url=_first(value, _URL_KEYS),
                tool=tool,
            ))


def extract_documents(tool: str, result: Any) -> list[Document]:
    """MCP 결과 하나 → 인용 가능한 Document 목록."""
    documents: list[Document] = []
    for payload in _iter_result_payloads(result):
        _collect_documents(payload, tool, documents, 0)

    merged: list[Document] = []
    seen: set[str] = set()
    for index, document in enumerate(documents):
        if not document.text:
            continue
        key = document.cite_uid or hashlib.sha1(document.text[:400].encode("utf-8")).hexdigest()[:12]
        if key in seen:
            continue
        seen.add(key)
        if not document.cite_uid:
            # 서버가 uid 를 안 주면 우리가 안정적인 것을 붙인다. 인용 번호를 매기려면
            # 무엇이든 식별자가 있어야 한다.
            document.cite_uid = f"{tool or 'mcp'}#{index + 1}"
        merged.append(document)
    return merged


@dataclass
class SpillStore:
    """대용량 툴 결과의 원문을 디스크에 둔다.

    두 가지를 얻는다.
      - 컨텍스트: 원문이 모델 메시지에 절대 안 실린다.
      - 재실행 무료화: 이 프로세스가 이미 부른 {tool, arguments} 는 상류를 다시 안 부른다.

    캐시 적중은 **이 프로세스가 쓴 핸들에 한정한다.** 디스크에 남은 남의 파일까지
    읽어 버리면, 어제 받아 둔 급여 고시를 오늘 답변의 근거로 쓰게 된다. 파일은 사후
    감사(scripts/context_audit.py)를 위해 남기는 것이지 캐시가 아니다.
    """

    root: Path = field(default_factory=lambda: Path(
        os.environ.get("LUNIT_SPILL_DIR") or os.path.join(tempfile.gettempdir(), "lunit-mcp-spill")
    ))
    _written: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.root = Path(tempfile.gettempdir())

    @staticmethod
    def key(tool: str, arguments: dict[str, Any]) -> str:
        payload = json.dumps(
            {"tool": tool, "arguments": arguments},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def path(self, handle: str) -> Path:
        return self.root / f"{handle}.json"

    def load(self, handle: str) -> Any | None:
        if handle not in self._written:
            return None
        target = self.path(handle)
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, handle: str, value: Any) -> str:
        target = self.path(handle)
        try:
            target.write_text(
                json.dumps(value, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except OSError:
            return ""
        self._written.add(handle)
        return str(target)
