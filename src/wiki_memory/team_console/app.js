const token=document.querySelector('#token');
const items=document.querySelector('#items');
const status=document.querySelector('#status');

async function request(path,options={}){
  const response=await fetch(path,{
    ...options,
    headers:{
      'Authorization':'Bearer '+token.value,
      'Content-Type':'application/json',
      ...(options.headers||{}),
    },
  });
  if(!response.ok)throw new Error(`${response.status} ${await response.text()}`);
  return response.json();
}

function esc(value){
  const node=document.createElement('div');
  node.textContent=String(value??'');
  return node.innerHTML;
}

async function load(){
  items.innerHTML='';
  status.className='meta';
  status.textContent='Chargement…';
  try{
    const data=await request('/v1/proposals?limit=100');
    status.textContent=`${data.proposals.length} proposition(s)`;
    if(!data.proposals.length)items.innerHTML='<div class="empty">Aucune proposition en attente.</div>';
    for(const proposal of data.proposals){
      const article=document.createElement('article');
      article.innerHTML=`<div class="meta">${esc(proposal.spaceId)} · ${esc(proposal.recordedAt)} · ${esc(proposal.actor.id)}</div><h2>${esc(proposal.payload.title||proposal.payload.assertionId||'Proposition')}</h2><pre>${esc(JSON.stringify(proposal.payload,null,2))}</pre><div class="actions"><button data-decision="accept" class="primary">Accepter</button><button data-decision="reject">Rejeter</button><button data-decision="dispute">Contester</button></div>`;
      article.querySelectorAll('button').forEach(button=>button.onclick=async()=>{
        button.disabled=true;
        try{
          await request(`/v1/proposals/${proposal.eventId}/review`,{
            method:'POST',
            body:JSON.stringify({decision:button.dataset.decision}),
          });
          await load();
        }catch(error){
          status.className='error';
          status.textContent=error.message;
        }finally{
          button.disabled=false;
        }
      });
      items.append(article);
    }
  }catch(error){
    status.className='error';
    status.textContent=error.message;
  }
}

document.querySelector('#load').onclick=load;
document.querySelector('#logout').onclick=()=>{
  token.value='';
  items.innerHTML='';
  status.className='meta';
  status.textContent='Jeton effacé.';
};
