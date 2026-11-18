from datetime import datetime,timedelta,timezone
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .lost_models import LostItem,ItemClaim,ClaimBatch,ClaimBatchItem
from .zones_service import performance_context
from .staff_service import locked
from .services import pack,audit
from .constants import BATCH_HOLD_MINUTES

def now():return datetime.now(timezone.utc)
def aware(v):return v if v is None or v.tzinfo else v.replace(tzinfo=timezone.utc)
def matching(c,item):return c.proof.strip().casefold()==item.identifying_detail.strip().casefold()

def view(x,u):
    d=pack(x)
    if u.role=='viewer':d['identifying_detail']='保管识别信息，仅场务核验'
    return d

def members(s,batch_id):
    return s.scalars(select(ClaimBatchItem).where(ClaimBatchItem.batch_id==batch_id).order_by(ClaimBatchItem.item_id)).all()

def lock_many(s,cls,ids):
    ids=sorted(set(ids))
    if not ids:return {}
    return {x.id:x for x in s.scalars(select(cls).where(cls.id.in_(ids)).order_by(cls.id).with_for_update().execution_options(populate_existing=True))}

def lock_batch(s,id):
    x=s.scalar(select(ClaimBatch).where(ClaimBatch.id==id).with_for_update().execution_options(populate_existing=True))
    if not x:raise HTTPException(404,'领取单不存在')
    return x

def effective(x):
    if x.state=='reserved' and aware(x.expires_at)<=now():return 'expired'
    return x.state

def blocking(s,b,rows,items=None,claims=None,require_active=True):
    items=items or {};claims=claims or {}
    for m in rows:
        c=claims.get(m.claim_id) or s.get(ItemClaim,m.claim_id);it=items.get(m.item_id) or s.get(LostItem,m.item_id)
        if not c or not it:return '领取单关联记录缺失'
        if require_active and not m.active:return '领取单成员已释放'
        if c.state!='pending':return f'申请 #{c.id} 已交付，不能整单领取'
        if it.state!='stored':return f'失物 {it.code} 已交付，不能整单领取'
        if c.claimant!=b.claimant:return '领取单内认领人不一致'
        if not matching(c,it):return f'失物 {it.code} 的凭据与保管特征不符'
        other=s.scalar(select(ClaimBatchItem.id).where(ClaimBatchItem.active==True,ClaimBatchItem.batch_id!=b.id,(ClaimBatchItem.item_id==m.item_id)|(ClaimBatchItem.claim_id==m.claim_id)))
        if other:return f'失物 {it.code} 已被其他领取单预约占用'
    return ''

def iso(v):return aware(v).astimezone(timezone.utc).isoformat() if isinstance(v,datetime) else v
def batch_view(s,x,u):
    rows=members(s,x.id);state=effective(x);listed=[]
    for m in rows:
        c=s.get(ItemClaim,m.claim_id);it=s.get(LostItem,m.item_id)
        if not c or not it:continue
        listed.append({'claim_id':c.id,'item_id':it.id,'code':it.code,'location':it.location,'claimant':c.claimant,'proof':c.proof,'claim_state':c.state,'item_state':it.state,'active':m.active})
    remaining=None if state!='reserved' else max(0,int((aware(x.expires_at)-now()).total_seconds()))
    reason=x.close_reason or ''
    if state=='expired' and not reason:reason='预约超时，整批释放'
    if state=='composed' and not reason:reason=blocking(s,x,rows,require_active=False)
    d={c.name:iso(getattr(x,c.name)) for c in x.__table__.columns}
    return d|{'state':state,'remaining_seconds':remaining,'blocking_reason':reason,'claim_ids':[r['claim_id'] for r in listed],'items':listed}

def expire_due(s):
    stamp=now();count=0
    due=s.scalars(select(ClaimBatch).where(ClaimBatch.state=='reserved',ClaimBatch.expires_at<=stamp).order_by(ClaimBatch.id)).all()
    for row in due:
        b=s.scalar(select(ClaimBatch).where(ClaimBatch.id==row.id).with_for_update().execution_options(populate_existing=True))
        if b and b.state=='reserved' and aware(b.expires_at)<=stamp:release(s,b,'expired','预约超时，整批释放',None);count+=1
    if count:s.commit()
    return count

def release(s,b,state,reason,by):
    for m in members(s,b.id):m.active=False
    b.state=state;b.close_reason=reason;b.closed_by=by;b.closed_at=now()

def reject(s,b,reason,u):
    release(s,b,'rejected',reason,u.email);audit(s,u,f'整批拒绝领取单 #{b.id}：{reason}')
    s.commit();raise HTTPException(409,reason)

def flush_guard(s,codes):
    try:s.flush()
    except IntegrityError:
        s.rollback();raise HTTPException(409,'并发预约冲突：'+('、'.join(codes) if codes else '失物')+'已被其他领取单占用或已交付，请刷新后重试')

def create(s,d,u):
    performance_context(s,d.performance_id);x=LostItem(**d.model_dump());s.add(x);s.flush();audit(s,u,'登记保管失物 '+x.code);return view(x,u)

def claim(s,d,u):
    expire_due(s)
    x=locked(s,LostItem,d.item_id)
    if x.state!='stored':raise HTTPException(409,'失物已交付，不能再次认领')
    if s.scalar(select(ClaimBatchItem.id).where(ClaimBatchItem.item_id==x.id,ClaimBatchItem.active==True)):raise HTTPException(409,'该失物已在领取单预约占用中，暂不能新增认领')
    if s.scalar(select(ItemClaim.id).where(ItemClaim.item_id==x.id,ItemClaim.claimant==d.claimant.strip(),ItemClaim.state=='pending')):raise HTTPException(409,'此认领人已有待核申请，请修订原凭据或加入领取单')
    c=ItemClaim(**d.model_dump());c.claimant=c.claimant.strip();s.add(c);s.flush();audit(s,u,'失物认领申请 '+x.code);return pack(c)

def revise(s,id,d,u):
    expire_due(s)
    pre=s.get(ItemClaim,id)
    if not pre:raise HTTPException(404,'认领申请不存在')
    item=locked(s,LostItem,pre.item_id);x=locked(s,ItemClaim,id)
    if x.state!='pending' or item.state!='stored':raise HTTPException(409,'已交付申请不能修改凭据')
    if d.item_id!=x.item_id or d.claimant.strip()!=x.claimant:raise HTTPException(409,'认领关联及姓名不可修改')
    x.proof=d.proof
    for m in s.scalars(select(ClaimBatchItem).where(ClaimBatchItem.claim_id==x.id).order_by(ClaimBatchItem.id.desc())):
        b=s.get(ClaimBatch,m.batch_id)
        if b and b.state in ('composed','reserved'):
            b=lock_batch(s,b.id)
            if b.state in ('composed','reserved'):release(s,b,'cancelled','凭据修订，整单失效并释放物品',u.email)
            break
    audit(s,u,'修订认领凭据 '+str(id));return pack(x)

def handover(s,id,u):
    expire_due(s)
    pre=s.get(ItemClaim,id)
    if not pre:raise HTTPException(404,'认领申请不存在')
    item=locked(s,LostItem,pre.item_id);x=locked(s,ItemClaim,id)
    if item.state!='stored' or x.state!='pending':raise HTTPException(409,'此失物已经交付，不能重复领取')
    if not matching(x,item):raise HTTPException(409,'认领描述凭据与保管识别信息不符，拒绝交付')
    held=s.scalar(select(ClaimBatchItem).where(ClaimBatchItem.item_id==item.id,ClaimBatchItem.active==True).order_by(ClaimBatchItem.id))
    if held:
        b=lock_batch(s,held.batch_id)
        if b.state=='reserved':
            if aware(b.expires_at)>now():raise HTTPException(409,'该失物已在领取单预约占用中，须整单核验交付或撤销领取单')
            release(s,b,'expired','预约超时，整批释放',None)
            audit(s,u,f'预约超时释放领取单 #{b.id}');s.commit()
            raise HTTPException(409,'领取单预约已超时释放，可按单件重新核验')
        held.active=False
    for m in s.scalars(select(ClaimBatchItem).where(ClaimBatchItem.claim_id==x.id).order_by(ClaimBatchItem.id.desc())):
        b=s.get(ClaimBatch,m.batch_id)
        if b and b.state in ('composed','reserved'):
            b=lock_batch(s,b.id)
            if b.state in ('composed','reserved'):release(s,b,'cancelled','成员已单件交付，整单失效',u.email)
            break
    x.state='handed_over';x.handed_by=u.email;item.state='handed_over'
    flush_guard(s,[item.code])
    audit(s,u,'经理核验交付 '+item.code+'→'+x.claimant);return pack(x)

def compose(s,d,u):
    expire_due(s)
    ids=list(dict.fromkeys(d.claim_ids))
    previews=[]
    for cid in ids:
        c=s.get(ItemClaim,cid)
        if not c:raise HTTPException(404,f'认领申请 #{cid} 不存在')
        previews.append(c)
    claimant=previews[0].claimant
    if any(c.claimant!=claimant for c in previews):raise HTTPException(409,'领取单只能包含同一认领人的待核申请')
    items=lock_many(s,LostItem,(c.item_id for c in previews));claims=lock_many(s,ItemClaim,ids)
    for c in claims.values():
        if c.claimant!=claimant:raise HTTPException(409,'领取单只能包含同一认领人的待核申请')
        if c.state!='pending':raise HTTPException(409,f'认领申请 #{c.id} 已交付，不能组单')
        it=items.get(c.item_id)
        if not it or it.state!='stored':raise HTTPException(409,'失物已交付，不能组单')
    rows=s.scalars(select(ClaimBatchItem).where((ClaimBatchItem.claim_id.in_(ids))|(ClaimBatchItem.item_id.in_(list(items)))).order_by(ClaimBatchItem.id)).all()
    composed=[];same=None
    for m in rows:
        b=s.get(ClaimBatch,m.batch_id)
        if not b or b.state not in ('composed','reserved'):continue
        if b.state=='reserved':raise HTTPException(409,'认领申请或失物已在预约占用的领取单中，请先撤销或处理该单')
        if b.claimant!=claimant:continue
        existing=set(s.scalars(select(ClaimBatchItem.claim_id).where(ClaimBatchItem.batch_id==b.id)))
        if existing==set(ids):same=b;continue
        if not existing<=set(ids):raise HTTPException(409,'该待核验领取单已含其他申请，请一并勾选后提交，或先撤销该单')
        if b not in composed:composed.append(b)
    if same:return batch_view(s,same,u)
    if composed:
        composed=sorted(composed,key=lambda b:b.id)
        b=lock_batch(s,composed[0].id)
        for other in composed[1:]:
            locked_other=lock_batch(s,other.id)
            for r in members(s,locked_other.id):s.add(ClaimBatchItem(batch_id=b.id,claim_id=r.claim_id,item_id=r.item_id,active=False))
            release(s,locked_other,'cancelled','并入领取单 #'+str(b.id),u.email)
        have=set(s.scalars(select(ClaimBatchItem.claim_id).where(ClaimBatchItem.batch_id==b.id)))
        for c in claims.values():
            if c.id not in have:s.add(ClaimBatchItem(batch_id=b.id,claim_id=c.id,item_id=c.item_id,active=False))
        flush_guard(s,[items[c.item_id].code for c in claims.values()])
        audit(s,u,f'补齐待核验领取单 #{b.id}（{len(claims)} 件）');return batch_view(s,b,u)
    b=ClaimBatch(claimant=claimant,state='composed');s.add(b)
    flush_guard(s,[])
    for c in claims.values():s.add(ClaimBatchItem(batch_id=b.id,claim_id=c.id,item_id=c.item_id,active=False))
    flush_guard(s,[items[c.item_id].code for c in claims.values()])
    audit(s,u,f'组成领取单 #{b.id}（{len(claims)} 件）');return batch_view(s,b,u)

def reserve(s,id,u):
    expire_due(s)
    pre=s.get(ClaimBatch,id)
    if not pre:raise HTTPException(404,'领取单不存在')
    rows=members(s,id);items=lock_many(s,LostItem,(m.item_id for m in rows));claims=lock_many(s,ItemClaim,(m.claim_id for m in rows))
    b=lock_batch(s,id);stamp=now()
    if b.state=='reserved':
        if aware(b.expires_at)>stamp:return batch_view(s,b,u)
        release(s,b,'expired','预约超时，整批释放',None);s.commit();raise HTTPException(409,'领取单预约已超时释放，请重新组单预约')
    if b.state=='handed_over':return batch_view(s,b,u)
    if b.state=='expired':s.commit();raise HTTPException(409,'预约已超时，整批释放；请重新组单预约')
    if b.state!='composed':raise HTTPException(409,'领取单已撤销、超时或拒绝，不能预约；请重新组单')
    rows=members(s,id);problem=blocking(s,b,rows,items,claims,require_active=False)
    if problem:reject(s,b,problem,u)
    for m in rows:m.active=True
    b.state='reserved';b.reserved_at=stamp;b.expires_at=stamp+timedelta(minutes=BATCH_HOLD_MINUTES)
    flush_guard(s,[items[m.item_id].code for m in rows])
    audit(s,u,f'整单核验预约 #{b.id}（{len(rows)} 件，{BATCH_HOLD_MINUTES} 分钟）');return batch_view(s,b,u)

def handover_batch(s,id,u):
    expire_due(s)
    pre=s.get(ClaimBatch,id)
    if not pre:raise HTTPException(404,'领取单不存在')
    rows=members(s,id);items=lock_many(s,LostItem,(m.item_id for m in rows));claims=lock_many(s,ItemClaim,(m.claim_id for m in rows))
    b=lock_batch(s,id);stamp=now()
    if b.state=='handed_over':return batch_view(s,b,u)
    if b.state=='expired':s.commit();raise HTTPException(409,'预约已超时，整批释放；请重新组单预约')
    if b.state!='reserved':raise HTTPException(409,'领取单未处于预约占用状态，不能整批交付')
    if aware(b.expires_at)<=stamp:
        release(s,b,'expired','预约超时，整批释放',None);s.commit();raise HTTPException(409,'预约已超过占用期限，整批释放；请重新组单预约')
    rows=members(s,id);problem=blocking(s,b,rows,items,claims,require_active=True)
    if problem:reject(s,b,problem,u)
    for m in rows:
        it=items[m.item_id];c=claims[m.claim_id]
        it.state='handed_over';c.state='handed_over';c.handed_by=u.email;m.active=False
    b.state='handed_over';b.handed_by=u.email;b.handed_at=stamp;b.close_reason=''
    flush_guard(s,[items[m.item_id].code for m in rows]);audit(s,u,f'整批核验交付 #{b.id}（{len(rows)} 件→{b.claimant}）');return batch_view(s,b,u)

def cancel_batch(s,id,u):
    expire_due(s)
    b=lock_batch(s,id)
    if b.state=='handed_over':raise HTTPException(409,'已交付领取单不能撤销')
    if b.state in ('cancelled','expired','rejected'):return batch_view(s,b,u)
    release(s,b,'cancelled','领取单已撤销，物品释放',u.email);audit(s,u,f'撤销领取单 #{b.id}');return batch_view(s,b,u)

def batches(s,u):
    expire_due(s)
    return [batch_view(s,x,u) for x in s.scalars(select(ClaimBatch).order_by(ClaimBatch.id.desc()))]
