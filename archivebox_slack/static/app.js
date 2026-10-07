const $ = (s) => document.querySelector(s);
let csrf = '', current = null, dirty = false;
const fields = () => [...$('#settings-form').elements];
const lists = ['allowed_users', 'allowed_channels', 'ai_allowed_users'];
const features = [
 ['enable_new_urls','New URLs channel','Save links posted by people, tagged with their name + slack.'],
 ['enable_saved_urls','Saved URLs channel','One rich card for every sealed snapshot.'],
 ['enable_mentions','Thread mentions','Save links from the last 10 messages, plus the mention itself.'],
 ['enable_dms','Direct messages','Send links straight to the bot.'],
 ['upload_images','Screenshot & favicon previews','Upload previews so private ArchiveBox servers work too.'],
 ['allow_guests','Allow workspace guests','Guest users can submit links when enabled.'],
];
function toggle([name,title,desc],target){
 const row=document.createElement('label');row.className='toggle-row';
 const text=document.createElement('div'),strong=document.createElement('strong'),p=document.createElement('p');
 strong.textContent=title;p.textContent=desc;text.append(strong,p);
 const input=document.createElement('input');input.type='checkbox';input.name=name;input.setAttribute('form','settings-form');input.setAttribute('role','switch');
 row.append(text,input);$(target).append(row);
}
features.forEach(item=>toggle(item,'#feature-toggles'));
toggle(['enable_ai','Enable ArchiveBox AI','Only explicitly allowed users can start agent sessions.'],'#ai-toggle');
['help','save','search','status'].forEach(name=>{const label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.name='commands';input.value=name;input.setAttribute('form','settings-form');label.append(input,document.createTextNode('/'+name));$('#command-options').append(label);});
async function api(path,options={}) {
 const response=await fetch(path,{...options,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,...options.headers}});
 let data; try{data=await response.json();}catch{throw new Error('Unexpected response. Check the server connection.');}
 if(!response.ok){if(response.status===401){$('#console').hidden=true;$('#login').hidden=false;}throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail));}return data;
}
function toast(text,error=false){const el=$('#toast');el.textContent=text;el.classList.toggle('error',error);el.hidden=false;setTimeout(()=>el.hidden=true,7000);}
function platform(){const value=$('[name=platform]:checked')?.value||'slack';$('#slack-fields').hidden=value!=='slack';$('#zulip-fields').hidden=value!=='zulip';$('#slack-ai-fields').hidden=value!=='slack';$('#zulip-ai-fields').hidden=value!=='zulip';}
function fill(settings){fields().forEach(el=>{if(!el.name)return;const value=settings[el.name];if(el.name==='commands')el.checked=value.includes(el.value);else if(el.type==='radio')el.checked=el.value===value;else if(el.type==='checkbox')el.checked=Boolean(value);else if(lists.includes(el.name))el.value=value.join('\n');else el.value=value??'';if(el.type==='password'&&settings.configured_secrets.includes(el.name))el.placeholder='Connected · leave blank to keep';});platform();dirty=false;}
function read(){const data={};fields().forEach(el=>{if(!el.name||el.name==='commands')return;if(el.type==='radio'){if(el.checked)data[el.name]=el.value;}else if(el.type==='checkbox')data[el.name]=el.checked;else if(el.type==='number')data[el.name]=Number(el.value);else if(lists.includes(el.name))data[el.name]=el.value.split(/[\s,]+/).filter(Boolean);else data[el.name]=el.value.trim();});data.commands=fields().filter(el=>el.name==='commands'&&el.checked).map(el=>el.value);return data;}
async function save(){const data=read();await api('/api/settings',{method:'PUT',body:JSON.stringify(data)});dirty=false;await refresh(true);toast('Settings saved. Your connections are up to date.');}
function openTab(tab){document.querySelectorAll('.page').forEach(el=>el.hidden=el.id!==`page-${tab}`);document.querySelectorAll('.nav').forEach(el=>el.classList.toggle('active',el.dataset.tab===tab));$('#page-name').textContent=({overview:'Overview',connections:'Connections',preferences:'Bot preferences',activity:'Activity'})[tab];location.hash=tab;}
function renderJobs(jobs){const list=$('#jobs');list.replaceChildren();if(!jobs.length){const div=document.createElement('div');div.className='empty';div.textContent='Your first shared link starts the story.';list.append(div);return;}jobs.forEach(job=>{const row=document.createElement('div');row.className='job';const state=document.createElement('span');state.className='job-state '+job.state;state.textContent=job.state;const main=document.createElement('div');main.className='job-main';const title=document.createElement('div');title.className='job-title';title.textContent=job.title||job.kind;const meta=document.createElement('div');meta.className='job-meta';meta.textContent=`${job.kind==='announcement'?'Saved card':'Incoming message'} · ${new Date(job.created_at).toLocaleString()}`;main.append(title,meta);if(job.error){const error=document.createElement('div');error.className='job-error';error.textContent=job.error;main.append(error);}row.append(state,main);if(job.session_url){const link=document.createElement('a');link.className='button secondary';link.href=job.session_url;link.target='_blank';link.rel='noreferrer';link.textContent='Open agent session ↗';row.append(link);}if(['uncertain','failed'].includes(job.state)){const retry=document.createElement('button');retry.className='secondary';retry.textContent='Review & retry';retry.onclick=()=>{if(confirm('First inspect Slack/Zulip and ArchiveBox. Retry ONLY if no remote crawl, agent session, or message was created. Have you confirmed it is absent?'))action(async()=>{await api('/api/jobs/retry',{method:'POST',body:JSON.stringify({id:job.id,confirmed_remote_absent:true})});await refresh();});};row.append(retry);}list.append(row);});}
async function refresh(force=false){try{const state=await api('/api/state');current=state;csrf=state.csrf;$('#login').hidden=true;$('#console').hidden=false;if(force||!dirty)fill(state.settings);const s=state.settings;$('#open-archive').href=s.archivebox_public_url;$('#platform-label').textContent=s.platform==='slack'?'Slack workspace':'Zulip organization';$('#flow-platform').textContent=s.platform==='slack'?'Slack':'Zulip';const archive=s.configured_secrets.includes('archivebox_token'),chat=state.connections.capture?.ok,channels=s.new_channel&&s.saved_channel;$('#step-archive').textContent=archive?'CONFIGURED':'TO DO';$('#step-chat').textContent=chat?'CONNECTED':'TO DO';$('#step-channels').textContent=channels?'READY':'TO DO';const ready=state.connections.archivebox?.ok&&chat&&channels&&!state.error;$('#connection-badge').textContent=ready?'● Connected & listening':'○ Finish setup';$('#connection-badge').classList.toggle('connected',Boolean(ready));$('#done-count').textContent=state.stats.done||0;$('#activity-count').textContent=(state.stats.queued||0)+(state.stats.waiting||0)+(state.stats.running||0);$('#queue-summary').textContent=`${state.stats.waiting||0} captures in progress · ${state.stats.uncertain||0} need review`;$('#runtime-error').hidden=!state.error;$('#runtime-error').textContent=state.error;renderJobs(state.jobs);}catch(error){if($('#login').hidden)toast(error.message,true);}}
async function action(fn){try{await fn();}catch(error){toast(error.message,true);}}
$('#login-form').onsubmit=async event=>{event.preventDefault();try{const result=await api('/auth/login',{method:'POST',body:JSON.stringify({password:$('#password').value})});csrf=result.csrf;$('#password').value='';await refresh(true);}catch(error){$('#login-error').textContent=error.message;}};
$('#logout').onclick=()=>action(async()=>{await api('/auth/logout',{method:'POST'});$('#console').hidden=true;$('#login').hidden=false;});
document.querySelectorAll('[data-tab]').forEach(el=>el.onclick=()=>openTab(el.dataset.tab));document.querySelectorAll('[data-open]').forEach(el=>el.onclick=()=>openTab(el.dataset.open));document.querySelectorAll('.save').forEach(el=>el.onclick=()=>action(save));
fields().forEach(el=>el.addEventListener('input',()=>{dirty=true;if(el.name==='platform')platform();}));
$('[name=archivebox_url]').addEventListener('change',event=>{
 const publicInput=$('[name=archivebox_public_url]'),old=current.settings;
 const follows=publicInput.value===old.archivebox_url||(old.archivebox_url==='http://archivebox:5797'&&publicInput.value==='http://localhost:5797');
 try{const url=new URL(event.target.value);if(follows&&!['archivebox','host.docker.internal'].includes(url.hostname))publicInput.value=url.href.replace(/\/$/,'');}catch{}
});
$('#settings-form').onsubmit=e=>{e.preventDefault();action(save);};
for(const service of ['archive','chat'])$(`#check-${service}`).onclick=()=>action(async()=>{await save();const data=await api(`/api/check/${service==='archive'?'archivebox':'chat'}`,{method:'POST'});toast(service==='archive'?`ArchiveBox connected · ${data.snapshots} snapshots`:`Connected to ${data.team||data.bot_name||data.name}`);});
const channels=()=>action(async()=>{if(dirty)await save();await api('/api/channels',{method:'POST'});await refresh(true);toast('Your New URLs and Saved URLs channels are ready.');});$('#create-channels').onclick=channels;$('#setup-channels').onclick=channels;$('#refresh').onclick=()=>refresh();
openTab(['overview','connections','preferences','activity'].includes(location.hash.slice(1))?location.hash.slice(1):'overview');refresh();setInterval(()=>{if(!$('#console').hidden)refresh();},8000);

$('#change-password').onclick=()=>action(async()=>{await api('/api/password',{method:'POST',body:JSON.stringify({current_password:$('#current-password').value,new_password:$('#new-password').value})});$('#current-password').value='';$('#new-password').value='';toast('Password changed. Other console sessions were signed out.');});
