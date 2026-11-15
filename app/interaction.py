"""Shared text/voice interaction coordination; no mandatory routing model call."""
import asyncio
import json
import re
import time

from .store import TERMINAL


class InteractionMixin:
    def init_interactions(self):
        self.input_locks = {}
        with self.store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS conversation_requests(
                    session_id TEXT NOT NULL,event_id TEXT NOT NULL,request TEXT NOT NULL,response TEXT NOT NULL,
                    PRIMARY KEY(session_id,event_id));
                CREATE TABLE IF NOT EXISTS conversation_dialogs(
                    session_id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,
                    question TEXT NOT NULL,expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS conversation_plan_focus(
                    session_id TEXT PRIMARY KEY,plan_id TEXT NOT NULL);
            ''')

    def dialog(self, sid):
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM conversation_dialogs WHERE session_id=?',(sid,)).fetchone()
            if row and row['expires'] <= time.time():
                db.execute('DELETE FROM conversation_dialogs WHERE session_id=?',(sid,));return None
            return {**dict(row),'payload':json.loads(row['payload'])} if row else None

    def clear_dialog(self, sid):
        with self.store.connect() as db:
            db.execute('DELETE FROM conversation_dialogs WHERE session_id=?',(sid,))

    def ask_dialog(self, sid, kind, payload, question):
        with self.store.connect() as db:
            db.execute('INSERT INTO conversation_dialogs VALUES(?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET kind=excluded.kind,payload=excluded.payload,question=excluded.question,expires=excluded.expires',
                       (sid,kind,json.dumps(payload,ensure_ascii=False),question,time.time()+300))
        return {'kind':'clarify','status':'awaiting_input','message':question,'dialog_kind':kind}

    def set_plan_focus(self, sid, plan_id):
        with self.store.connect() as db:
            db.execute('INSERT INTO conversation_plan_focus VALUES(?,?) ON CONFLICT(session_id) DO UPDATE SET plan_id=excluded.plan_id',(sid,plan_id))
        self.router.clear(sid)

    def focused_plan(self, sid):
        with self.store.connect() as db:
            row=db.execute('SELECT plan_id FROM conversation_plan_focus WHERE session_id=?',(sid,)).fetchone()
        if row:
            try:return self.plans.get(sid,row[0])
            except KeyError:return None
        return None

    def choose_plan(self, sid, text=''):
        plans=self.plans.list(sid)
        words=re.findall(r'车窗|空调|胎压|电量|座椅|悬架|天窗|后视镜|车况|灯光|多媒体',text)
        if words:
            matched=[p for p in plans if all(w in p['goal'] for w in words)]
            return matched[0] if len(matched)==1 else None
        return self.focused_plan(sid)

    async def plan_action(self, sid, plan_id, action, revision, **changes):
        previous=self.plans.get(sid,plan_id)
        if changes.get('profile_id') is not None and changes['profile_id'] not in self.catalog.snapshot().profiles:
            raise ValueError('所选模拟配置不存在')
        if changes.get('schedule',{}).get('timezone',self.plans.timezone)!=self.plans.timezone:
            raise ValueError('周期计划使用运行环境的当地时区')
        if action=='cancel':
            return self.ask_dialog(sid,'cancel_scope',{'plan_id':plan_id,'revision':revision},'要取消这一次，还是取消整个计划？')
        result=self.plans.update(sid,plan_id,revision,'edit' if action=='update' else action,now=time.time(),**changes)
        self.set_plan_focus(sid,plan_id)
        # Service atomically invalidates occurrences before any remote awaits.
        for tid in result.get('stopped_task_ids',[]):
            task=self.store.task(tid,sid)
            job=self.jobs.get(task['id'])
            if job and not job.done():job.cancel()
            await self.stop_remote(task.get('run_id'))
            await self.notify_task_invalidated(task)
        plan=result.get('plan',result)
        self.suppress_plan_action_notice(plan)
        for listener in tuple(self.plan_change_listeners):
            try:await listener(plan)
            except Exception:pass
        self.ensure_scheduler()
        labels={'pause':'计划已暂停，当前执行已停止','resume':'计划已恢复，只安排后续触发',
                'cancel_once':'本次执行已取消，后续计划保留','cancel_plan':'整个计划已取消，当前执行已停止', 'update':'计划已更新，修改从后续执行生效'}
        return {'kind':'plan_updated','status':plan['status'],'plan':plan,'message':labels.get(action,'计划已处理')}

    def create_plan(self, sid, goal, schedule, request_key, profile_id=None):
        snapshot=self.catalog.snapshot()
        if schedule.get('timezone',self.plans.timezone)!=self.plans.timezone:
            raise ValueError('周期计划使用运行环境的当地时区')
        profile_id=profile_id or self.store.session_profile(sid) or 'demo-ready'
        if profile_id not in snapshot.profiles:raise ValueError('所选模拟配置不存在')
        plan,_=self.plans.create(sid,request_key,goal,schedule,profile_id=profile_id,now=time.time())
        self.suppress_plan_action_notice(plan)
        self.set_plan_focus(sid,plan['id']);self.clear_dialog(sid);self.ensure_scheduler()
        return {'kind':'plan_created','status':plan['status'],'plan':plan,'message':'周期计划已创建，按显示的当地时间执行；车辆操作仍为模拟。'}

    def suppress_plan_action_notice(self, plan):
        # User initiated operations have a direct text/voice response. Only
        # unsolicited skipped/missed events need a separate notification.
        with self.store.connect() as db:
            db.execute("UPDATE plan_events SET notify=0 WHERE plan_id=? AND plan_revision=? AND kind IN ('plan.created','plan.pause','plan.resume','plan.cancel_once','plan.cancel_plan','plan.edit')",(plan['id'],plan['revision']))

    async def notify_task_invalidated(self, task):
        for listener in tuple(self.change_listeners):
            try:await listener(task)
            except Exception:
                self.store.event(task['id'],'task.voice_invalidation_failed',{'message':'语音连接未确认清理失效内容'})

    async def dispatch_plans(self):
        snapshot=self.catalog.snapshot()
        batch=self.plans.due(snapshot.revision,'demo-ready' if 'demo-ready' in snapshot.profiles else next(iter(snapshot.profiles)),
                             'auto' if self.settings.candidate_preload_enabled else 'off',
                             'auto' if self.settings.direct_completion_enabled else 'agent',now=time.time())
        for task in batch['tasks']:
            current=self.store.task(task['id'])
            if current['status']!='running' or current['revision']!=task['revision']:
                continue
            if current['profile_id'] not in snapshot.profiles:
                self.store.finish(task['id'],'failed','周期计划所选模拟配置已不存在，本次未执行。')
                continue
            self.loop_events.accept(current['session_id'],'schedule-'+current['id'],'schedule_due',current['id'],current['revision'],{'goal':current['goal']})
            self.loop_events.mark_handled(current['session_id'],'schedule-'+current['id'])
            self.launch(current)
        return batch

    async def expire_questions(self):
        expired=self.router.expire_waiting(timeout=300)
        for task in expired:
            job=self.jobs.get(task['id'])
            if job and not job.done():
                job.cancel();await self.stop_remote(task.get('run_id'))
            self.loop_events.cancel_waits(task['id'],task['session_id'])
            await self.notify_task_invalidated(task)
        return expired

    async def handle_input(self, sid, text, event_id, task_id=None, task_revision=None):
        if not text.strip():raise ValueError('输入不能为空')
        request=json.dumps({'text':text,'task_id':task_id,'task_revision':task_revision},sort_keys=True,ensure_ascii=False)
        async with self.input_locks.setdefault(sid,asyncio.Lock()):
            await self.expire_questions()
            with self.store.connect() as db:
                prior=db.execute('SELECT request,response FROM conversation_requests WHERE session_id=? AND event_id=?',(sid,event_id)).fetchone()
            if prior:
                if prior['request']!=request:raise ValueError('输入标识已用于其他内容')
                response=json.loads(prior['response'])
                if response.get('task'):response['task']=self.store.task(response['task']['id'],sid)
                if response.get('plan'):response['plan']=self.plans.get(sid,response['plan']['id'])
                return response
            response=await self._handle_input(sid,text.strip(),event_id,task_id,task_revision)
            with self.store.connect() as db:
                db.execute('INSERT INTO conversation_requests VALUES(?,?,?,?)',(sid,event_id,request,json.dumps(response,ensure_ascii=False)))
            return response

    async def _handle_input(self, sid, text, event_id, task_id, task_revision):
        from .recurring import parse_schedule_request, complete_schedule_request
        from .conversation_routing import is_cancellation
        if re.search(r'(?:不要|别|不用|不想|不是说|不是要|不需要|暂时不|不)(?:再|去)?(?:取消|暂停|恢复|修改)',text):
            return {'kind':'chat','status':'unchanged','message':'好，保持现有任务和计划，不执行取消或修改。'}
        # Explicit task selection is authoritative; a plan dialog cannot capture it.
        dialog=self.dialog(sid) if task_id is None else None
        managing_plan = task_id is None and '计划' in text and bool(re.search(r'取消|暂停|恢复|继续|改成|改为|改到|调整为|不用了|算了',text))
        if dialog and dialog['kind']=='cancel_scope':
            if re.fullmatch(r'(?:就|只)?(?:这|本)?(?:一)?次[吧。！! ]*',text) or text in {'取消一次','只取消这一次'}:
                action='cancel_once'
            elif re.fullmatch(r'(?:取消)?(?:整个|全部)(?:计划)?[吧。！! ]*',text):action='cancel_plan'
            elif is_cancellation(text):
                self.clear_dialog(sid)
                return {'kind':'chat','status':'unchanged','message':'已退出这次确认，计划没有被取消。'}
            else:return {'kind':'clarify','status':'awaiting_input','message':dialog['question']}
            payload=dialog['payload']
            response=await self.plan_action(sid,payload['plan_id'],action,payload['revision'])
            self.clear_dialog(sid);return response
        if dialog and dialog['kind'] in {'schedule_creation','schedule_edit'}:
            if is_cancellation(text):
                self.clear_dialog(sid)
                return {'kind':'cancel','status':'cancelled','message':'已退出本次计划设置，未应用新设置。'}
            parsed=complete_schedule_request(dialog['payload']['parsed'],text)
        else:
            parsed=parse_schedule_request(text,timezone=self.plans.timezone) if task_id is None and not managing_plan else None
        if parsed and parsed.get('status')=='not_recurring':parsed=None
        if parsed is not None:
            if parsed.get('question'):
                self.router.clear(sid)
                return self.ask_dialog(sid,dialog['kind'] if dialog and dialog['kind']=='schedule_edit' else 'schedule_creation',{**(dialog['payload'] if dialog else {}),'text':text,'parsed':parsed},parsed['question'])
            if dialog and dialog['kind']=='schedule_edit':
                response=await self.plan_action(sid,dialog['payload']['plan_id'],'update',dialog['payload']['revision'],goal=parsed['goal'],schedule=parsed['schedule'])
                self.clear_dialog(sid);return response
            return self.create_plan(sid,parsed['goal'],parsed['schedule'],'input-'+event_id)
        followup=re.fullmatch(r'(?:做完|完成后)(?:再)?(.+)',text)
        if followup:
            target=self.store.task(task_id,sid) if task_id else self.router.current(sid)
            if not target or target['status'] in TERMINAL:
                return {'kind':'clarify','status':'awaiting_input','message':'请明确要在哪个未结束任务之后追加。'}
            result=self.append_followup(sid,target['id'],text,task_revision if task_revision is not None else target['revision'],'input-'+event_id)
            self.router.focus(sid,target['id'])
            return {**result,'kind':'append_followup','message':'已排队，当前任务完成后处理。'}
        if task_id is None:
            if re.search(r'查看.*计划|有哪些.*计划|列出.*计划|周期计划',text) and not re.search(r'暂停|恢复|取消|修改',text):
                return {'kind':'plans','status':'ok','plans':self.plans.list(sid),'message':'已返回当前周期计划。'}
            edit=re.search(r'(?:改成|改为|改到|调整为|调整到)(.+)$',text)
            if '计划' in text and edit:
                plan=self.choose_plan(sid,text)
                if not plan:return {'kind':'clarify','status':'awaiting_input','message':'请先明确要修改哪个计划。','plans':self.plans.list(sid)}
                if re.search(r'计划.*(?:任务|内容).*改',text):
                    return await self.plan_action(sid,plan['id'],'update',plan['revision'],goal=edit[1].strip())
                revised=parse_schedule_request(edit[1].strip(),timezone=self.plans.timezone)
                if revised['status']=='not_recurring':
                    draft={'status':'needs_info','goal':plan['goal'],'schedule':{k:v for k,v in plan['schedule'].items() if k!='time'},'missing':['time']}
                    revised=complete_schedule_request(draft,edit[1].strip())
                elif revised.get('missing')==['goal']:
                    revised=complete_schedule_request(revised,plan['goal'])
                if revised.get('question'):
                    return self.ask_dialog(sid,'schedule_edit',{'plan_id':plan['id'],'revision':plan['revision'],'parsed':revised,'text':text},revised['question'])
                return await self.plan_action(sid,plan['id'],'update',plan['revision'],goal=revised['goal'],schedule=revised['schedule'])
            action='pause' if text.startswith('暂停') else 'resume' if text.startswith(('恢复','继续')) and '计划' in text else 'cancel' if '取消' in text and '计划' in text else None
            if not action and '计划' in text and re.search(r'不用了|算了|不做了',text):action='cancel'
            if not action and self.focused_plan(sid) and self.router.current(sid) is None:
                if re.fullmatch(r'恢复|继续',text):action='resume'
                elif re.fullmatch(r'取消(?:这|本)?一次|取消整个(?:计划)?',text):action='cancel'
            if action or (is_cancellation(text) and self.router.current(sid) is None and self.focused_plan(sid)):
                plan=self.choose_plan(sid,text)
                if not plan:return {'kind':'clarify','status':'awaiting_input','message':'请明确是哪一个周期计划。','plans':self.plans.list(sid)}
                if action=='cancel' and re.search(r'整个|全部|以后.*不',text):action='cancel_plan'
                elif action=='cancel' and re.search(r'一次|这次|本次',text):action='cancel_once'
                return await self.plan_action(sid,plan['id'],action or 'cancel',plan['revision'])
        route=self.router.resolve(sid,text,explicit_task_id=task_id)
        kind=route['kind']
        if kind in {'clarify','chat','historical'}:
            route.setdefault('status','awaiting_input' if kind=='clarify' else 'ok')
            route.setdefault('message','不客气。' if re.search(r'谢谢|多谢',text) else '好的。' if kind=='chat' else '已找到对应历史任务。')
            if kind=='historical' and route.get('task_id'):
                route['task']=self.store.task(route['task_id'],sid)
                if route['task']['status']=='expired':
                    route.update(status='expired',message='等待补充已超过五分钟，原任务已过期；如需继续，请新建任务。')
                self.router.focus(sid,route['task_id'])
            return route
        target=route.get('task_id')
        expected=task_revision if task_revision is not None else route.get('task_revision')
        if kind=='cancel':
            task=await self.cancel(sid,target,expected_revision=expected)
            message='原任务已取消，不再继续执行。'
        elif kind=='answer_question':
            task=await self.resume(sid,target,route.get('text',text),expected)
            message='原任务已取消。' if task['status']=='cancelled' else '已收到补充，继续原任务。'
        elif kind=='revise_goal':
            task=await self.revise(sid,target,route.get('text',text),expected,'input-'+event_id)
            message='改口已登记，等待新目标结果。'
        else:
            self.clear_dialog(sid)
            task=self.submit(sid,text,'input-'+event_id)
            message='已受理，尚未完成。'
        return {'kind':kind,'status':task['status'],'task':task,'message':message}
