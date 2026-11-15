"""Local, metadata-only candidate recall. It neither fills arguments nor authorizes calls."""
from collections import Counter, OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import json
import math
import re
import time
import unicodedata

from .capability_catalog import CatalogSnapshot


def normalize(text):
    text = unicodedata.normalize("NFKC", str(text)).casefold()
    # Values must not determine tool choice (23 vs 26 degrees, two vs three levels).
    text = re.sub(r"\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokens(text):
    text = normalize(text)
    found = set(re.findall(r"[a-z]+", text))
    for segment in re.findall(r"[\u3400-\u9fff]+", text):
        for width in (2, 3):
            found.update(segment[i:i+width] for i in range(len(segment)-width+1))
    return found


def complex_request(goal):
    # Conservative discourse checks, independent of any tool id or business parameters.
    # False negatives only supply hints; the full catalog remains available to the model.
    markers = r"如果|否则|失败|原因|为什么|根据|查询|查一下|看看|说明|是否|能否|怎么|多少|什么|先.+再|然后|同时|并且|以及|顺便|而且|但是|并把|并将|并开启|和|、|\b(if|then|and|also|why|check|query|manual|status)\b"
    if re.search(markers, goal, re.I):
        return True
    clauses = [part.strip() for part in re.split(r"[，,；;。！？!?\n]", goal) if part.strip()]
    return len(clauses) > 1


def explicit_control_target(snapshot, selected, goal):
    """Conservative retrieval policy, never a parameter generator or authorization."""
    if len(selected) != 1 or complex_request(goal):
        return False
    if re.search(r"增加|减少|降低|升高|调高|调低|提高|比|当前|现在|原来|一点|一些|再|\b(increase|decrease|more|less|relative)\b", goal, re.I):
        return False
    normalized=normalize(goal).rstrip('。.!！ ')
    tool=snapshot.tools[selected[0]]
    if any(normalized==normalize(e['instruction']).rstrip('。.!！ ') for e in tool['examples']):
        return True
    fixed=re.search(r"设(?:置|定)?(?:为|到|成)|调(?:整)?(?:到|至)|改为|\bset\b.*\bto\b",goal,re.I)
    return bool(fixed and re.search(r"\d|[零〇一二两三四五六七八九十百]",goal))


@dataclass
class RecallIndex:
    entries: list
    idf: dict


class CandidatePreloader:
    def __init__(self, max_candidates=3, max_chars=12000, min_score=0.70, related_max_tools=4):
        self.max_candidates = max(1, min(max_candidates, 8))
        self.max_chars = max(256, max_chars)
        self.min_score = min_score
        self.cache = OrderedDict()
        self.related_max_tools = max(0,min(related_max_tools,4))

    def index(self, snapshot):
        if snapshot.revision in self.cache:
            self.cache.move_to_end(snapshot.revision)
            return self.cache[snapshot.revision], True
        entries, frequency = [], Counter()
        for tool in snapshot.tools.values():
            if not tool["enabled"]:
                continue
            fields = [(tokens(tool["title"]), .9), (tokens(" ".join(tool["aliases"])), .85), (tokens(tool["description"]), .55), (tokens(tool["id"].replace(".", " ").replace("_", " ")), .65)]
            fields += [(tokens(example["instruction"]), 1.0) for example in tool["examples"]]
            all_tokens = set().union(*(field for field, _ in fields))
            frequency.update(all_tokens)
            phrase = re.sub(r"^(设置|调节|调整|控制|查询|选择|读取|set\s+|adjust\s+|query\s+|read\s+)", "", normalize(tool["title"]))
            entries.append({"id": tool["id"], "kind": tool["kind"], "fields": fields, "phrase": phrase, "examples": {normalize(x["instruction"]) for x in tool["examples"]}, "category": tuple(tool["category_path"])})
        total = len(entries)
        index = RecallIndex(entries, {token: math.log((total+1)/(count+1))+1 for token, count in frequency.items()})
        self.cache[snapshot.revision] = index
        if len(self.cache) > 8:
            self.cache.popitem(last=False)
        return index, False

    @staticmethod
    def similarity(query, field, idf):
        overlap = query & field
        if not overlap:
            return 0.0
        weight = lambda term: idf.get(term, 3.0)**2
        return sum(weight(t) for t in overlap) / math.sqrt(sum(weight(t) for t in query)*sum(weight(t) for t in field))

    def prepare(self, snapshot: CatalogSnapshot, goal, mode="auto", task_revision=0):
        start = time.perf_counter()
        result = {"status": "skipped", "reason": "", "catalog_revision": snapshot.revision, "task_revision": task_revision, "tool_ids": [], "tools": [], "scores": [], "payload_chars": 0, "cache_hit": False}
        def finish(reason):
            result["reason"] = reason
            if mode != "off" and not task_revision and result["status"] == "skipped" and reason in {"complex_or_query", "query_domain", "low_confidence", "ambiguous_candidates", "too_many_candidates"}:
                from .capability_search import CapabilitySearch, encoded_size
                if not hasattr(self, "searcher"):
                    self.searcher = CapabilitySearch(self.max_chars)
                found = self.searcher.search(snapshot,goal[:300],limit=4)
                if found["tools"]:
                    controls=[t for t in found["tools"] if t["kind"]=="control"]
                    related=[t for t in found["tools"] if t["kind"]!="control"]
                    payload={"catalog_revision":snapshot.revision,"tools":controls+related,"primary_tool_ids":[t["id"] for t in controls],"related_tool_ids":[t["id"] for t in related],"directory_hints":found["directory_hints"]}
                    if encoded_size(payload)<=self.max_chars:
                        result.update(status="preloaded",reason="global_recall",tool_ids=payload["primary_tool_ids"],tools=controls,related_tools=related,related_tool_ids=payload["related_tool_ids"],directory_hints=payload["directory_hints"],payload_chars=encoded_size(payload),scores=[])
            result["elapsed_ms"] = round((time.perf_counter()-start)*1000, 3)
            return result
        if mode == "off":
            return finish("disabled")
        if task_revision:
            return finish("continuation")
        if complex_request(goal):
            return finish("complex_or_query")
        query = tokens(goal)
        if not query:
            return finish("low_confidence")
        index, result["cache_hit"] = self.index(snapshot)
        ranked = []
        normalized = normalize(goal)
        for entry in index.entries:
            score = max((self.similarity(query, field, index.idf)*weight for field, weight in entry["fields"] if field), default=0)
            if normalized in entry["examples"]:
                score = 1.0
            elif len(entry["phrase"]) >= 4 and entry["phrase"] in normalized:
                # An exact, specific phrase derived from the title can bridge a
                # paraphrased verb without maintaining a business-specific router.
                score = max(score, .82)
            ranked.append((score, entry))
        ranked.sort(key=lambda x: (-x[0], x[1]["id"]))
        if not ranked or ranked[0][0] < self.min_score:
            return finish("low_confidence")
        best, entry = ranked[0]
        if entry["kind"] != "control":
            return finish("query_domain")
        near = [(score, e) for score, e in ranked if score >= max(self.min_score, best-.12)]
        if any(e["kind"] != "control" or e["category"] != entry["category"] for _, e in near):
            return finish("ambiguous_candidates")
        if len(near) > self.max_candidates:
            return finish("too_many_candidates")
        selected = [e["id"] for _, e in near]
        schemas = snapshot.describe(selected)["tools"]
        size = len(json.dumps(schemas, ensure_ascii=False, separators=(",", ":")))
        if size > self.max_chars:
            return finish("schema_budget")
        from .capability_search import related_queries, directory_hints, encoded_size
        related=[];related_ids=[];hints=[];omitted=[]
        primary_only = best >= .82 and explicit_control_target(snapshot,selected,goal)
        def payload(extra, paths):
            return {"catalog_revision":snapshot.revision,"tools":schemas+extra,"directory_hints":paths,
                    "primary_tool_ids":selected,"related_tool_ids":[t["id"] for t in extra]}
        if encoded_size(payload([],[]))>self.max_chars:
            return finish("schema_budget")
        for tid in ([] if primary_only else related_queries(snapshot,selected)):
            schema=snapshot.describe([tid])["tools"][0]
            if len(related)>=self.related_max_tools or encoded_size(payload(related+[schema],hints))>self.max_chars:
                omitted.append(tid);continue
            related.append(schema);related_ids.append(tid)
        for hint in ([] if primary_only else directory_hints(snapshot,selected+related_ids)):
            if encoded_size(payload(related,hints+[hint]))<=self.max_chars:hints.append(hint)
        result.update(status="preloaded", tool_ids=selected, tools=deepcopy(schemas), scores=[round(score, 4) for score, _ in near],
                      related_tool_ids=related_ids,related_tools=related,directory_hints=hints,omitted_related_ids=omitted,
                      preload_policy="primary_only" if primary_only else "with_related_queries",
                      payload_chars=encoded_size(payload(related,hints)))
        return finish("metadata_match")
