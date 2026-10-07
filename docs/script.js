const body=document.body;
document.querySelector('#theme').addEventListener('click',()=>body.classList.toggle('dark-mode'));
document.querySelectorAll('.filters button').forEach(button=>button.addEventListener('click',()=>{document.querySelectorAll('.filters button').forEach(b=>b.classList.remove('active'));button.classList.add('active');const filter=button.dataset.filter;document.querySelectorAll('.insights article').forEach(card=>{card.hidden=filter!=='all'&&card.dataset.type!==filter})}));

async function hydrateLiveData(){
  try{
    let data=window.DASHBOARD_DATA;
    if(!data){
      const response=await fetch('data.json',{cache:'no-store'});
      if(!response.ok) throw new Error('snapshot unavailable');
      data=await response.json();
    }
    const metrics=document.querySelectorAll('.metric');
    const signals=document.querySelectorAll('.signal-grid b');
    const funnel=document.querySelectorAll('.funnel article strong');
    if(data.engineering && signals.length>=3){
      signals[0].textContent=`${data.engineering.intents} intents tracked.`;
      signals[1].textContent=data.pnl?.available?'Trading edge measurable.':'Trading edge unproven.';
      signals[2].textContent=data.pnl?.available?'Review realized R by cohort.':'Close the measurement gap.';
      if(data.engineering.execution_success_pct!=null){
        document.querySelector('.funnel article:nth-of-type(4) p').textContent=`${data.engineering.execution_success_pct}% executed · ${data.engineering.failure_pct}% failed`;
      }
    }
    if(data.funnel && funnel.length>=5){
      [data.funnel.signals,data.funnel.intents,data.funnel.plans,data.funnel.actions,data.funnel.strategies].forEach((value,index)=>{funnel[index].textContent=Number(value).toLocaleString()});
    }
    if(data.risk?.risk_per_trade_pct!=null) metrics[2].querySelector('strong').innerHTML=`${data.risk.risk_per_trade_pct}<small>%</small>`;
    if(data.pnl?.available){
      metrics[3].querySelector('label em').textContent='LIVE SNAPSHOT';
      metrics[3].querySelector('strong').innerHTML=`${data.pnl.realized_pnl_usdt.toFixed(2)}<small> USDT</small>`;
      metrics[3].querySelector('h3').textContent='Realized PnL';
      metrics[3].querySelector('p').textContent=`${data.pnl.record_count} reconciled closed trades · ${data.pnl.positive_count} positive · ${data.pnl.negative_count} negative.`;
      metrics[3].querySelector('footer').innerHTML=`synced <b>${new Date(data.pnl.last_synced_at).toLocaleString()}</b>`;
      metrics[1].querySelector('h3').textContent='Win rate';
      metrics[1].querySelector('strong').innerHTML=`${data.pnl.win_rate_pct}<small>%</small>`;
      metrics[1].querySelector('p').textContent=`${data.pnl.positive_count} winners from ${data.pnl.record_count} reconciled closed trades.`;
      metrics[1].querySelector('footer').innerHTML=`avg win <b>${data.pnl.avg_win_usdt.toFixed(2)} USDT</b>`;
      metrics[0].querySelector('h3').textContent='Realized PnL';
      metrics[0].querySelector('strong').innerHTML=`${data.pnl.realized_pnl_usdt.toFixed(2)}<small> USDT</small>`;
      metrics[0].querySelector('p').textContent=`${data.pnl.negative_count} losing trades · avg loss ${data.pnl.avg_loss_usdt.toFixed(2)} USDT.`;
      metrics[0].querySelector('footer').innerHTML=`history <b>${new Date(data.pnl.history_start_at).toLocaleDateString()}</b>`;
      if(data.pnl.avg_win_to_loss_ratio!=null){
        document.querySelector('.rr header').firstChild.textContent='Observed outcome ratio ';
        document.querySelector('.rr header span').textContent=`1 : ${data.pnl.avg_win_to_loss_ratio.toFixed(2)}`;
        document.querySelector('.rr>small').textContent=`Observed outcome data: ${data.pnl.profit_factor.toFixed(2)} profit factor · ${data.pnl.expectancy_usdt.toFixed(2)} USDT expectancy/trade. Average winner divided by absolute average loser; not planned R.`;
      }
    }
    document.querySelector('.kicker').textContent=`● live production snapshot · ${new Date(data.generated_at).toLocaleString()}`;
  }catch(error){ console.info('Dashboard is using the authored evidence view:',error.message); }
}
if(window.DASHBOARD_DATA){
  hydrateLiveData();
}else{
  const dataScript=document.createElement('script');
  dataScript.src='data.js';
  dataScript.onload=hydrateLiveData;
  dataScript.onerror=hydrateLiveData;
  document.head.appendChild(dataScript);
}
