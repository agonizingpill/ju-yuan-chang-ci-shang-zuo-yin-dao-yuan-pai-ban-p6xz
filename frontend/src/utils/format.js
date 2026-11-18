export const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const week=x=>['周日','周一','周二','周三','周四','周五','周六'][new Date(x+'T12:00:00').getDay()];
export const pretty=x=>x?x.replace('T',' ').slice(0,16):'—';
export const stamp=x=>{if(!x)return'—';const d=new Date(x);if(isNaN(d))return pretty(x);return new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(d).replace(/\//g,'-')};
export const formData=form=>Object.fromEntries(new FormData(form));
