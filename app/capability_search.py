"""Local Chinese-aware lexical search of the pinned business catalog. No model calls."""
from collections import Counter, OrderedDict
from copy import deepcopy
import json
import math
import re
import time
import unicodedata

from .capability_catalog import CatalogError

STOP = re.compile(r'查询|查找|读取|获取|当前|工具|能力|帮我|请问|一下|模拟|数据|设置|打开|关闭|如何|怎么')


def terms(text):
    value = unicodedata.normalize('NFKC', str(text)).casefold()
    value = STOP.sub(' ', value)
    found = re.findall(r'[a-z][a-z0-9]*', value)
    for part in re.findall(r'[\u3400-\u9fff]+', value):
        for width in (2,3):
            found.extend(part[i:i+width] for i in range(len(part)-width+1))
    return found


def directory_hints(snapshot, ids):
    paths={tuple(d['path']) for d in snapshot.directories.values() if len(d['path'])==1}
    for tid in ids:
        path=snapshot.tools[tid]['category_path']
        paths.update(tuple(path[:i]) for i in range(1,len(path)+1))
    return [{'path':list(p),'title':snapshot.directories[p]['title']} for p in sorted(paths)]


def related_queries(snapshot, ids):
    tails={tuple(snapshot.tools[i]['category_path'][1:]) for i in ids}
    explicit=[]
    for tid in ids:
        path=snapshot.tools[tid]['category_path']
        for depth in range(1,len(path)+1):
            explicit.extend(tuple(p) for p in snapshot.directories[tuple(path[:depth])].get('related_query_paths',[]))
    choices=[]
    for tool in snapshot.tools.values():
        if not tool['enabled'] or tool['kind']=='control':continue
        path=tuple(tool['category_path'])
        related=tuple(path[1:]) in tails
        extra=any(path[:len(p)]==p for p in explicit)
        if related or extra:
            choices.append((0 if related and tool['kind']=='status' else 1 if extra and tool['kind']=='status' else 2,tool['id']))
    return [tid for _,tid in sorted(choices)]


def encoded_size(value):
    return len(json.dumps(value,ensure_ascii=False,separators=(',',':')))


class CapabilitySearch:
    def __init__(self,max_chars=12000):
        self.max_chars=max(1024,min(max_chars,64000));self.cache=OrderedDict()

    def index(self,snapshot):
        if snapshot.revision in self.cache:
            self.cache.move_to_end(snapshot.revision);return self.cache[snapshot.revision],True
        docs=[];frequency=Counter()
        for t in snapshot.tools.values():
            if not t['enabled']:continue
            d=snapshot.directories[tuple(t['category_path'])]
            metadata=[t['description'],t['id'].replace('_',' ').replace('.',' '),d['title'],d['description'],*d['aliases']]
            metadata += [json.dumps(t[k],ensure_ascii=False) for k in ['input_schema','output_schema']]
            ts=terms(t['title'])*3+terms(' '.join(t['aliases']))*2+terms(' '.join(x['instruction'] for x in t['examples']))*2+terms(' '.join(metadata))
            docs.append((t['id'],Counter(ts),len(ts)));frequency.update(set(ts))
        result=(docs,frequency,sum(n for _,_,n in docs)/max(len(docs),1))
        self.cache[snapshot.revision]=result
        if len(self.cache)>8:self.cache.popitem(last=False)
        return result,False

    def search(self,snapshot,query,kind=None,limit=4):
        start=time.perf_counter()
        query=query.strip()
        if not query or len(query)>300 or not 1<=limit<=6 or kind not in {None,'control','manual','status'}:
            raise CatalogError('INVALID_SEARCH：检索词、类型或数量不合法')
        (docs,frequency,avg),hit=self.index(snapshot);qs=set(terms(query));ranked=[]
        n=len(docs)
        for tid,counts,length in docs:
            tool=snapshot.tools[tid]
            if kind and tool['kind']!=kind:continue
            overlap=qs & counts.keys()
            if not overlap:continue
            coverage=len(overlap)/max(len(qs),1)
            if coverage<.28 and query.casefold()!=tid.casefold():continue
            score=sum(math.log(1+(n-frequency[q]+.5)/(frequency[q]+.5))*counts[q]*2.2/(counts[q]+1.2*(.25+.75*length/max(avg,1))) for q in overlap)
            if query.casefold()==tid.casefold():score+=1000
            ranked.append((score,tid))
        ranked.sort(key=lambda x:(-x[0],x[1]));selected=ranked[:limit]
        result={'catalog_revision':snapshot.revision,'query':query,'kind':kind,'method':'bm25_cjk_ngrams','matches':[],'tools':[],'directory_hints':[], 'omitted_schema_ids':[], 'matched_count':len(ranked),'truncated':len(ranked)>limit,'cache_hit':hit}
        # Budget the complete response, not just the schemas. No data queries run here.
        budget=self.max_chars-160
        for score,tid in selected:
            t=snapshot.tools[tid]
            summary={k:deepcopy(t[k]) for k in ['id','title','description','kind','category_path']};summary['score']=round(score,4)
            trial=deepcopy(result);trial['matches'].append(summary)
            if encoded_size(trial)>budget:
                result['truncated']=True;continue
            result=trial
            schema=snapshot.describe([tid])['tools'][0]
            trial=deepcopy(result);trial['tools'].append(schema)
            if encoded_size(trial)<=budget:result=trial
            else:
                trial={**result,'omitted_schema_ids':[*result['omitted_schema_ids'],tid],'truncated':True}
                if encoded_size(trial)<=budget:result=trial
                else:result['truncated']=True
        hints=directory_hints(snapshot,[t['id'] for t in result['matches']])
        for hint in hints:
            trial={**result,'directory_hints':[*result['directory_hints'],hint]}
            if encoded_size(trial)<=budget:result=trial
        if not result['matches']:
            result['message']='未找到可靠匹配；可换用更明确的能力描述或浏览目录。这不证明车辆不支持。'
        result['elapsed_ms']=round((time.perf_counter()-start)*1000,3)
        result['payload_chars']=0
        for _ in range(3): result['payload_chars']=encoded_size(result)
        return result
