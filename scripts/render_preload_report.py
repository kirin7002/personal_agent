"""Render complete A/B records, including failures, without running any model calls."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import statistics
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent.parent
LABELS={'simple':'简单指令','complex':'组合/跨域任务','failure':'失败查证','clarification':'缺参追问接续','invalid_argument':'参数越界'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--input',type=Path,default=ROOT/'test-results/preload-ab-direct.json')
    parser.add_argument('--output',type=Path,default=ROOT/'docs/candidate-preload-benchmark.md')
    args=parser.parse_args()
    data=json.loads(args.input.read_text());summary=data['summary'];rows=data['trials']
    expected=summary['case_count']*summary['repeats']*2
    if len(rows)!=expected or len({(r['id'],r['repeat'],r['mode']) for r in rows})!=expected:
        raise SystemExit('对照测试尚未完整结束，拒绝生成完成报告')
    date=datetime.fromtimestamp(summary['created_at_unix'],ZoneInfo('Asia/Shanghai')).date()
    groups=summary['groups']
    public=ROOT/'benchmarks/results'/f'preload-ab-{date}.json'
    public.parent.mkdir(parents=True,exist_ok=True)
    public.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    lines=[f'# 候选预加载 A/B 对比：{date}','',
        f"模型：`{summary['model']}`。目录：`{summary['catalog_revision']}`。",'',
        f"固定 {summary['case_count']} 条指令，每种模式重复 {summary['repeats']} 次，共 {len(rows)} 次真实 Hermes 任务。业务工具均为模拟数据。",'',
        '## 方法','',
        '- 同一指令、模型、目录版本、模拟 profile 和通用系统提示，唯一业务差异是是否预加载候选 Schema。',
        '- 串行成对运行，交替 off/auto 先后顺序；每次使用新任务和新 Hermes 会话，不复用对话历史。',
        '- 正式测试统一使用本机与百炼直连，避免临时开发代理失效。此前中断的26次记录另存，未纳入本表。',
        '- 主要耗时为服务端任务创建至完成或追问；不含 S2S 识别、VAD、播放、浏览器渲染和人工思考时间。缺参用例由脚本立即补充答案。',
        '- 正确性检查工具与参数匹配、组合任务完整性、失败后的查询证据、无错误控制成功，以及越界参数被拒绝或追问。不是对全部自然语言解释做语义评分。','',
        '## 结果','',
        '| 场景 | 每组次数 | 完整目录中位数 | 预加载中位数 | 完整目录正确 | 预加载正确 |',
        '| --- | ---: | ---: | ---: | ---: | ---: |']
    for group in ['simple','complex','failure','clarification','invalid_argument']:
        pair=groups[group];old,new=pair['off'],pair['auto']
        lines.append(f"| {LABELS[group]} | {old['n']} | {old['median_seconds']:.2f}s | {new['median_seconds']:.2f}s | {old['passed']}/{old['n']} | {new['passed']}/{new['n']} |")
    simple=groups['simple']
    lines += ['',f"简单指令中位耗时下降 **{summary['simple_median_reduction_percent']}%**；目录调用中位数从 **{simple['off']['median_directory_calls']}** 次变为 **{simple['auto']['median_directory_calls']}** 次。这里统计的是目录工具调用，不是直接测量模型 API 请求次数。",'',
        '未命中预加载的组合或追问样本仍走同一流程；这些样本的耗时差异主要反映模型路径与网络波动，不能全部归因于优化。','',
        '## 简单指令逐项结果','',
        '| 指令 | 完整目录中位数 | 预加载中位数 | 预加载正确 |',
        '| --- | ---: | ---: | ---: |']
    for cid in sorted({r['id'] for r in rows if r['group']=='simple'}):
        pair={m:[r for r in rows if r['id']==cid and r['mode']==m] for m in ['off','auto']}
        lines.append(f"| {pair['off'][0]['goal']} | {statistics.median(r['server_seconds'] for r in pair['off']):.2f}s | {statistics.median(r['server_seconds'] for r in pair['auto']):.2f}s | {sum(r['passed'] for r in pair['auto'])}/{len(pair['auto'])} |")
    failures=summary['failures']
    lines += ['', '## 失败与回退', '']
    if failures:
        for f in failures: lines.append(f"- {f['id']} / 第{f['repeat']}次 / {f['mode']}：{', '.join(f['reasons'])}。")
    else:
        lines.append('- 正式测试中，所有样本通过了上述正确性断言。')
    failed_auto=[r for r in rows if r['group']=='failure' and r['mode']=='auto']
    lines += [f"- 失败查证样本中，预加载组 {sum(r['fallback'] for r in failed_auto)}/{len(failed_auto)} 次记录了转回完整目录的轨迹。",'- 看似单句的“播放指定曲目”仍需选择曲目与开始播放两个工具，不能把预加载到的一个候选当作任务全部。','- 预实验发现模型可能仅凭控制错误提前结束，现已对两组统一增加查询证据约束，不把少查证计作性能收益。','',
        '## 使用与局限','',
        '- 默认自动启用，API 可通过 `discovery_mode=off` 关闭以复现对照。',
        '- 预加载从配置元数据召回，不生成业务参数、不绕过 Schema、前置条件、幂等和取消检查。',
        '- 小样本、同一台机器、同一模型版本的结果，不是生产 SLA，也不代表任意表达都能得到相同比例收益。',
        '- 参数不完整、未知工具、低置信度和明确复杂句式仍走完整目录。',
        f"- 完整原始记录：[JSON](../benchmarks/results/{public.name})；用例定义：[固定用例](../benchmarks/preload-cases.json)。",'']
    args.output.write_text('\n'.join(lines))
    print(json.dumps({'report':str(args.output),'trials':len(rows),'failures':len(failures),'simple_reduction_percent':summary['simple_median_reduction_percent']},ensure_ascii=False))


if __name__=='__main__':main()
