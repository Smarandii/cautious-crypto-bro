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
      signals[1].textContent=data.strategy_pnl?.available?'Measured; profitability uncertain.':'Whole-position edge unavailable.';
      signals[2].textContent=data.strategy_pnl?.available?'Review realized R by cohort.':'Attach a reconciled V2 ledger.';
      if(data.engineering.execution_success_pct!=null){
        const outcomes=data.engineering.intent_outcomes;
        if(outcomes){
          const reasons=outcomes.failed_by_reason;
          const safetyStops=reasons.risk_budget_guard+reasons.ownership_guard+reasons.minimum_order_guard;
          const otherErrors=reasons.exchange_or_other+reasons.uncategorized;
          document.querySelector('.funnel article:nth-of-type(4) p').textContent=`${outcomes.executed} executed · ${safetyStops} safety stops · ${reasons.stale_request} stale · ${otherErrors} other errors · ${outcomes.skipped} skipped`;
        }else{
          document.querySelector('.funnel article:nth-of-type(4) p').textContent=`${data.engineering.execution_success_pct}% executed · ${data.engineering.failure_pct}% failed`;
        }
      }
    }
    if(data.funnel && funnel.length>=5){
      [data.funnel.signals,data.funnel.intents,data.funnel.plans,data.funnel.actions,data.funnel.strategies].forEach((value,index)=>{funnel[index].textContent=Number(value).toLocaleString()});
    }
    if(data.risk?.risk_per_trade_pct!=null) metrics[2].querySelector('strong').innerHTML=`${data.risk.risk_per_trade_pct}<small>%</small>`;
    renderLongRiskExperiment(data.risk?.demo_long_experiment);
    renderDemoExitExperiment(data.risk?.demo_exit_experiment);
  renderStrategyPnl(data.strategy_pnl);
  renderSideCohorts(data.strategy_pnl?.by_side);
    renderOperations(data);
    renderBenchmark(data);
    document.querySelector('.kicker').textContent=`● Bybit Demo snapshot · ${new Date(data.generated_at).toLocaleString()}`;
  }catch(error){ console.info('Dashboard is using the authored evidence view:',error.message); }
}

function renderLongRiskExperiment(experiment){
  if(!experiment?.active) return;
  const metric=document.querySelectorAll('.metric')[2];
  const completed=experiment.completed_position_count;
  const target=experiment.review_target_completed_longs;
  metric.querySelector('h3').textContent='Demo risk & payoff trials';
  metric.querySelector('p').textContent=`${experiment.planned_count} planned · ${experiment.executed_plan_count} executed · ${experiment.filled_position_count??'—'} filled; LONG risk ${experiment.effective_long_risk_pct}% capital, SHORT ${experiment.base_risk_pct}%.`;
  metric.querySelector('footer').innerHTML=`${completed==null?'—':completed}/${target} completed LONGs <b>${experiment.status.replaceAll('_',' ')}</b>`;
}

function renderDemoExitExperiment(experiment){
  if(!experiment?.active) return;
  const metric=document.querySelectorAll('.metric')[2];
  const completed=experiment.completed_position_count;
  const target=experiment.review_target_completed_positions;
  metric.querySelector('p').textContent+=` Payoff exits: ${experiment.planned_count} planned · ${experiment.executed_plan_count} executed.`;
  metric.querySelector('footer').innerHTML+=` · Exit ${completed==null?'—':completed}/${target} <b>${experiment.status.replaceAll('_',' ')}</b>`;
}

function renderStrategyPnl(pnl){
  const metrics=document.querySelectorAll('.metric');
  if(!pnl?.available){
    metrics[3].querySelector('label em').textContent='V2 LEDGER NEEDED';
    metrics[3].querySelector('h3').textContent='Profit factor';
    metrics[3].querySelector('p').textContent='Requires fully reconciled whole-position outcomes.';
    metrics[4].querySelector('label em').textContent='V2 LEDGER NEEDED';
    metrics[4].querySelector('strong').textContent='—';
    metrics[4].querySelector('h3').textContent='Average win';
    metrics[4].querySelector('p').textContent='Not inferred from partial closed-PnL rows.';
    metrics[4].querySelector('footer').textContent='awaiting reconciled positions';
    return;
  }

  metrics[3].querySelector('label em').textContent='RECONCILED V2';
  metrics[3].querySelector('strong').innerHTML=Number.isFinite(pnl.profit_factor)?`${pnl.profit_factor.toFixed(2)}<small>×</small>`:'—';
  metrics[3].querySelector('h3').textContent='Profit factor';
  metrics[3].querySelector('p').textContent=`${pnl.expectancy_r.toFixed(3)}R expectancy · ${pnl.expectancy_usdt.toFixed(2)} USDT per position.`;
  metrics[3].querySelector('footer').textContent=`${pnl.position_count} complete positions`;
  metrics[4].querySelector('label em').textContent='RECONCILED V2';
  metrics[4].querySelector('strong').innerHTML=pnl.avg_win_usdt==null?'—':`${pnl.avg_win_usdt.toFixed(2)}<small> USDT</small>`;
  metrics[4].querySelector('h3').textContent='Average win';
  metrics[4].querySelector('p').textContent=`Mean net result across ${pnl.positive_count} winning positions.`;
  metrics[4].querySelector('footer').innerHTML=`average loss <b>${pnl.avg_loss_usdt==null?'—':pnl.avg_loss_usdt.toFixed(2)} USDT</b>`;
  metrics[1].querySelector('h3').textContent='Win rate';
  metrics[1].querySelector('strong').innerHTML=`${pnl.win_rate_pct.toFixed(2)}<small>%</small>`;
  metrics[1].querySelector('p').textContent=`${pnl.positive_count} winners from ${pnl.position_count} complete positions.`;
  metrics[1].querySelector('footer').innerHTML=`avg win <b>${pnl.avg_win_usdt==null?'—':pnl.avg_win_usdt.toFixed(2)} USDT</b>`;
  metrics[0].querySelector('h3').textContent='Realized PnL';
  metrics[0].querySelector('strong').innerHTML=`${pnl.net_pnl_usdt.toFixed(2)}<small> USDT</small>`;
  metrics[0].querySelector('p').textContent=`${pnl.negative_count} losing positions · avg loss ${pnl.avg_loss_usdt==null?'—':pnl.avg_loss_usdt.toFixed(2)} USDT.`;
  metrics[0].querySelector('footer').textContent=`${pnl.position_count} complete positions · fees included`;

  if(pnl.avg_win_to_loss_ratio>0){
    const ratio=pnl.avg_win_to_loss_ratio;
    const breakEven=pnl.break_even_win_rate_pct;
    const panel=document.querySelector('.rr');
    panel.querySelector('header').firstChild.textContent='Observed avg win / avg loss ';
    panel.querySelector('header span').textContent=`${ratio.toFixed(2)}× · 1:${(1/ratio).toFixed(2)}`;
    panel.querySelectorAll('p').forEach((row,index)=>{
      const winRate=50+index*10;
      const factor=(winRate/100*ratio-(1-winRate/100));
      row.querySelector('i').style.display='none';
      row.querySelector('strong').textContent=`${factor>=0?'+':''}${(factor*100).toFixed(1)}%`;
      row.childNodes[0].textContent=`At ${winRate}% wins `;
      row.querySelector('strong').title=`Expectancy relative to average loss at ${winRate}% win rate`;
    });
    const blockCi=pnl.performance_bootstrap_95ci?.circular_block_4;
    const breakEvenTargets=pnl.break_even_avg_win_usdt_at_observed_counts!=null
      ? ` At this observed win/loss count, break-even requires avg win ≥${pnl.break_even_avg_win_usdt_at_observed_counts.toFixed(2)} USDT (+${pnl.avg_win_increase_pct_to_break_even.toFixed(1)}%) or avg loss ≤${pnl.break_even_avg_loss_usdt_at_observed_counts.toFixed(2)} USDT (${pnl.avg_loss_reduction_pct_to_break_even.toFixed(1)}% smaller), holding the other outcome constant.`
      : '';
    const riskSkew=pnl.avg_initial_risk_win_usdt!=null&&pnl.avg_initial_risk_loss_usdt!=null
      ? ` Mean initial stop-risk was ${pnl.avg_initial_risk_win_usdt.toFixed(2)} USDT on winners versus ${pnl.avg_initial_risk_loss_usdt.toFixed(2)} USDT on losers; sizing/fill mix therefore matters alongside exit payoff.`
      : '';
    const uncertainty=blockCi?.mean_net_pnl_usdt&&blockCi?.mean_net_r
      ? ` Four-trade block-bootstrap 95% intervals for mean outcome include zero: ${blockCi.mean_net_pnl_usdt[0].toFixed(2)} to ${blockCi.mean_net_pnl_usdt[1].toFixed(2)} USDT and ${blockCi.mean_net_r[0].toFixed(3)}R to ${blockCi.mean_net_r[1].toFixed(3)}R; edge remains uncertain.`
      : '';
    panel.querySelector('.rr > small').textContent=`Whole-position net average: winner ${pnl.avg_win_usdt.toFixed(2)} USDT, loser ${pnl.avg_loss_usdt.toFixed(2)} USDT. Net break-even win rate: ${breakEven.toFixed(1)}%; observed: ${pnl.win_rate_pct.toFixed(2)}%. Profit factor: ${pnl.profit_factor.toFixed(2)} · expectancy: ${pnl.expectancy_usdt.toFixed(2)} USDT per position. Partial exits are grouped; fees and funding are included.${breakEvenTargets}${riskSkew}${uncertainty}`;
  }
}

function renderSideCohorts(cohorts){
  const panel=document.querySelector('.side-cohorts');
  if(!panel||!cohorts) return;
  const value=(number,digits=2)=>number==null?'—':number.toFixed(digits);
  panel.hidden=false;
  panel.innerHTML=`<h3>Direction breakdown</h3><p class="side-caution">Complete reconciled positions only · small, retrospective cohorts; not a sizing signal.</p><div class="side-grid">${['LONG','SHORT'].map(side=>{const p=cohorts[side];return `<article><b>${side}</b><span>n=${p.position_count}</span><strong>${value(p.win_rate_pct)}% wins</strong><small>Net ${value(p.net_pnl_usdt)} USDT · expectancy ${value(p.expectancy_usdt)} USDT/position</small><small>Avg win/loss ${value(p.avg_win_usdt)} / ${value(p.avg_loss_usdt)} · PF ${value(p.profit_factor)}×</small><small>Break-even WR ${value(p.break_even_win_rate_pct)}%</small></article>`}).join('')}</div>`;
}

function renderBenchmark(data){
  if(document.querySelector('.benchmark') || !data.benchmark) return;
  const b=data.benchmark;
  const section=document.createElement('section');
  section.className='benchmark shell';
  section.innerHTML=`<div><small class="eyebrow">PASSIVE BENCHMARK</small><h2>What if we just bought and held BTC?</h2><p>Same start window as the reconciled ledger. Spot BTCUSDT, hourly Bybit candles, no leverage, fees, or rebalancing.</p></div><div class="benchmark-values"><div><strong>${b.return_pct.toFixed(2)}%</strong><small>BTC return</small></div><div><strong>${b.hypothetical_1000_usdt_pnl>=0?'+':''}${b.hypothetical_1000_usdt_pnl.toFixed(2)}</strong><small>USDT on $1,000</small></div><div><strong>${b.start_price_usdt.toLocaleString()}</strong><small>start price</small></div><div><strong>${b.end_price_usdt.toLocaleString()}</strong><small>end price</small></div></div><footer>Window: ${new Date(b.start_at).toLocaleDateString()} → ${new Date(b.end_at).toLocaleString()} · benchmark is not directly comparable to realized PnL without starting capital.</footer>`;
  document.querySelector('#method').before(section);
}

function renderOperations(data){
  if(document.querySelector('.live-ops') || !data.engineering || !data.account_pnl_records) return;
  const e=data.engineering, f=data.funnel, p=data.account_pnl_records;
  const o=data.account_open_positions;
  const section=document.createElement('section');
  section.className='live-ops shell';
  const openExposure=o?.snapshot_available?`<div class="open-exposure"><div><strong>${o.position_count}</strong><small>open account positions · ${o.long_count} long / ${o.short_count} short</small></div><div><strong>${o.unrealized_pnl_usdt==null?'—':`${o.unrealized_pnl_usdt>=0?'+':''}${o.unrealized_pnl_usdt.toFixed(2)} USDT`}</strong><small>unrealized P&amp;L · not realized strategy P&amp;L</small></div><div><strong>${o.estimated_stop_risk_usdt==null?'—':`${o.estimated_stop_risk_usdt.toFixed(2)} USDT`}</strong><small>gross mark-to-stop estimate · ${o.positions_with_stop}/${o.position_count} stops</small></div><div class="exposure-asof">Exchange snapshot · ${o.as_of?new Date(o.as_of).toLocaleString():'time unavailable'}</div></div>`:'';
  const accountSync=p.last_synced_at?new Date(p.last_synced_at).toLocaleString():'unknown';
  section.innerHTML=`<div class="ops-heading"><div><small class="eyebrow">LIVE OPERATIONS</small><h2>Everything the ledger knows right now</h2></div><span>account P&amp;L rows · not whole trades · cache synced ${accountSync}</span></div><div class="ops-grid"><div><strong>${e.source_messages.toLocaleString()}</strong><small>source messages</small></div><div><strong>${f.intents.toLocaleString()}</strong><small>intents</small></div><div><strong>${f.plans.toLocaleString()}</strong><small>plans</small></div><div><strong>${f.actions.toLocaleString()}</strong><small>actions</small></div><div><strong>${f.strategies.toLocaleString()}</strong><small>active strategies</small></div><div><strong>${e.execution_success_pct}%</strong><small>execution success</small></div><div><strong>${p.positive_count} / ${p.negative_count}</strong><small>positive / negative rows</small></div><div><strong>${p.avg_win_usdt?.toFixed(2)??'—'}</strong><small>avg positive row · USDT</small></div><div><strong>${p.avg_loss_usdt?.toFixed(2)??'—'}</strong><small>avg negative row · USDT</small></div></div>${openExposure}`;
  document.querySelector('#funnel').before(section);
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
