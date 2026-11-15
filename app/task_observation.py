"""Deterministic, bounded explanations of observed actions; never model reasoning."""
import json

LABELS = {
    'zone':'分区', 'temperature_c':'目标温度（℃）', 'driver_temperature_c':'主驾温度（℃）',
    'passenger_temperature_c':'副驾温度（℃）', 'rear_temperature_c':'后排温度（℃）',
    'fan_speed':'风量档位', 'airflow':'风向', 'ac_enabled':'制冷开启', 'action':'动作',
    'level':'档位', 'seat':'座位', 'window':'车窗', 'open_percent':'打开比例（%）',
    'volume_percent':'音量（%）', 'enabled':'开启', 'available':'可用', 'air_suspension':'空气悬架',
    'is_parked':'已驻车', 'speed_kmh':'车速（km/h）', 'equipment':'车辆配置', 'interfaces':'控制接口',
    'height':'悬架高度', 'mode':'模式', 'rules':'规则', 'description':'说明', 'title':'标题',
}
ENUMS = {'zone':{'all':'全部','driver':'主驾','passenger':'副驾','rear':'后排'},
         'seat':{'driver':'主驾','passenger':'副驾'},
         'action':{'raise':'升高','lower':'降低','play':'播放','pause':'暂停'},
         'airflow':{'face':'吹面','feet':'吹脚','face_feet':'吹面和吹脚','defrost':'除霜'}}


def short(value, limit=1000):
    value = str(value)
    return value if len(value) <= limit else value[:limit]+'…（展开数据查看）'


def facts(data, schema=None, prefix='', depth=0):
    if not isinstance(data, dict):
        return short(json.dumps(data, ensure_ascii=False), 1000)
    fields=[]
    schema=schema if isinstance(schema,dict) else {}
    properties=schema.get('properties', {})
    for key,value in data.items():
        if key in {'source','provenance','sample_observed_at'}:
            continue
        property_schema=properties.get(key,{})
        property_schema=property_schema if isinstance(property_schema,dict) else {}
        name=prefix+(property_schema.get('title') or LABELS.get(key, key))
        if isinstance(value, dict) and depth < 2:
            fields.append(facts(value,properties.get(key),name+' / ',depth+1))
            continue
        if isinstance(value, bool): value='是' if value else '否'
        elif value is None: value='未提供'
        elif isinstance(value,str): value=ENUMS.get(key,{}).get(value,value)
        elif isinstance(value,(list,dict)): value=json.dumps(value,ensure_ascii=False)
        fields.append(f'{name}：{short(value,240)}')
    return short('；'.join(fields),1000) or '无附加字段'


def path_name(path,snapshot):
    if not path: return '根目录'
    return ' / '.join(snapshot.directories.get(tuple(path[:i+1]),{}).get('title',part) for i,part in enumerate(path))


def observation(name,args,result,snapshot,legacy=False):
    """Only reads the public tool request/result and pinned catalog metadata."""
    error=result.get('error')
    out={'action':'','input':'','output':'','title':''}
    payload={}
    if name=='search_capabilities':
        matches=result.get('matches',[])
        loaded={t['id'] for t in result.get('tools',[])}
        out.update(title='全局检索车辆能力',action='直接搜索当前任务的全部可用工具定义，不查询车辆数据',
                   input='检索词：'+args.get('query','')+'；范围：'+{'control':'车控','status':'车况','manual':'车书'}.get(args.get('kind'),'全部领域'),
                   output='；'.join(m['title']+'（'+m['id']+'，'+('完整参数已提供' if m['id'] in loaded else '需读取参数定义')+'）' for m in matches) or result.get('message','没有可靠匹配'))
        payload={k:result[k] for k in ['matches','tools','directory_hints','omitted_schema_ids'] if k in result}
    elif name=='browse_capabilities':
        path=args.get('path',[])
        label=path_name(path,snapshot) if path is not None else '旧记录未保存路径'
        out.update(title=f'浏览目录：{label}', action=f'查找「{label}」下的可用能力',input='路径：'+('/'.join(path) or '/') if path is not None else '旧记录未保存请求路径')
        if 'query' in args: out['input']+=f'；关键词：{args["query"] or "无"}'
        elif legacy: out['input']+='；旧记录未保存筛选条件'
        if args.get('offset'): out['input']+=f'；从第 {args["offset"]+1} 项开始'
        items=result.get('items')
        out['output']=f'返回 {len(items)} 项：'+('、'.join(x.get('title',x.get('id','')) for x in items[:16]) or '没有匹配项') if items is not None else f'返回 {result.get("count",0)} 项；旧记录未保存条目清单'
        payload={'request':args, 'items':items} if items is not None else {'request':args}
    elif name=='describe_tools':
        ids=args.get('tool_ids',[])
        out.update(title='读取所选工具的参数规则',action='读取工具用途、必填参数、取值范围和版本',input='、'.join(snapshot.tools.get(i,{}).get('title',i) for i in ids))
        definitions=result.get('tools',[])
        lines=[]
        for tool in definitions:
            if tool.get('error'):
                lines.append(f'{tool.get("id","工具")}：{tool["error"].get("message","读取失败")}')
            else:
                schema=tool.get('input_schema',{})
                fields=[f'{key}（必填）' if key in schema.get('required',[]) else key for key in schema.get('properties',{})]
                lines.append(f'{tool.get("title",tool.get("id"))}：'+('、'.join(fields) or '无需参数'))
        out['output']=short('；'.join(lines))
        payload={'tools':definitions}
    elif name=='call_tools':
        receipts=result.get('results',[])
        lines=[];inputs=[];titles=[];has_control=False;has_query=False
        for r in receipts:
            tool=snapshot.tools.get(r.get('tool_id'),{})
            title=tool.get('title',r.get('tool_id','未知工具'));titles.append(title)
            inputs.append(f'{title}：'+(facts(r.get('arguments',{}),tool.get('input_schema')) if r.get('arguments') else '无需参数'))
            if r.get('status')=='error':
                e=r.get('error') or {}
                lines.append(f'{title}失败：{e.get("message","未知错误")}（{e.get("code","UNKNOWN")}）'+(f'；校验明细：{short(json.dumps(e["details"],ensure_ascii=False),500)}' if e.get('details') else ''))
            elif r.get('status')=='prepared':
                has_control=True
                lines.append(f'{title}：参数、版本和模拟前置条件校验通过，已生成控制结构；未执行真实车控')
            else:
                has_query=True
                lines.append(f'{title}（模拟数据）：'+facts(r.get('data'),tool.get('output_schema')))
        out.update(title=('生成控制参数' if has_control and not has_query else '查询结果' if has_query and not has_control else '调用能力')+'：'+'、'.join(titles), action='调用 '+ '、'.join(titles),input=short('\n'.join(inputs),1600),output=short('\n'.join(lines),2400))
        payload={'results':receipts}
    elif name=='request_user_input':
        out.update(title='向用户追问必要信息', action='暂停当前任务，等待补充信息',input='问题：'+args.get('question',''),output='已交给前台展示或播报；收到回答后继续原任务')
    elif name=='finish_task':
        out.update(title='提交任务结论',action='引用实际调用结果，提交当前任务的结论',input=f'结果类型：{args.get("outcome","旧记录未保存")}；引用 {len(args.get("result_ids",[]))} 条结果',output=short(result.get('message','已提交结论'),1800))
        payload={'result_ids':args.get('result_ids',[]),'unresolved':args.get('unresolved',[])}
        resolution=result.get('type_resolution')
        if resolution:
            labels={'prepared':'纯车控','answered':'纯查询','mixed':'查询＋车控'}
            out['title']='程序校验回执并提交结论'
            out['input']=f"引用 {len(args.get('result_ids',[]))} 条实际结果；类型由程序判定"
            out['output']=f"程序判定为{labels[resolution['resolved']]}：{resolution['control_results']} 条控制回执、{resolution['query_results']} 条查询回执。"+out['output']
            if resolution['label_changed']:
                out['output']+=' 原提交类型标签已按有效回执修正，未再次请求模型。'
            payload['type_resolution']=resolution
    if error:
        out['title']='操作失败：'+(out['title'] or name)
        out['output']=f'{error.get("message","操作失败")}（{error.get("code","UNKNOWN")}）'
        payload['error']=error
    if payload:
        encoded=json.dumps(payload,ensure_ascii=False,indent=2)
        out['data']=encoded[:24000]+('\n…数据过长；完整调用结果请查看任务 JSON。' if len(encoded)>24000 else '')
    return out


def history_observation(step,events,snapshot,receipts):
    """Recover facts from stored receipts; explicitly mark missing historical inputs."""
    d=step['data']; name=d.get('name'); start=d.get('started_at',step['created'])
    related=[e for e in events if e['id']<step['id'] and start<=e['created']<=step['created']]
    if name=='call_tools':
        ids={e['data'].get('result_id') for e in related if e['kind'] in {'tool.preview','tool.mock','tool.invalid'}}
        results=[r for r in receipts if r['result_id'] in ids]
        if results: return observation(name,{}, {'results':results},snapshot,True)
    if name=='browse_capabilities':
        browse=next((e['data'] for e in related if e['kind']=='catalog.browsed'),None)
        if browse: return observation(name,{'path':browse['path']}, {'count':browse['count']},snapshot,True)
        if d.get('status')=='error': return observation(name,{'path':None},{'error':{'message':d.get('detail',''), 'code':'旧记录未保存错误码'}},snapshot,True)
    if name=='describe_tools':
        desc=next((e['data'] for e in related if e['kind']=='catalog.described'),None)
        if desc: return observation(name,desc,snapshot.describe(desc['tool_ids']),snapshot,True)
    if name=='request_user_input':
        q=next((e['data'] for e in related if e['kind']=='task.question'),None)
        if q: return observation(name,{'question':q.get('message','')},{},snapshot,True)
    if name=='finish_task':
        end=next((e['data'] for e in related if e['kind'].startswith('task.') and 'outcome' in e['data']),None)
        if end:
            return {'title':'提交任务结论','action':'提交已有调用结果的结论','input':'旧记录未保存本次引用清单','output':short(end.get('message',''),1800)}
    return None
