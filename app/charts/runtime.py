from __future__ import annotations


# Kept inline so an exported report remains fully functional without a CDN,
# local static files, raster images, or network access.
REPORT_CHART_RUNTIME = r"""
(function installReportChartRuntime(){
  const finite=value=>typeof value==='number'&&Number.isFinite(value);
  const key=chart=>chart.chart_id||chart.id;
  const font='-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC",sans-serif';
  const readVisuals=container=>{
    const root=container.querySelector('[data-report-visuals]');
    if(!root)return null;
    try{return{root,charts:(JSON.parse(root.dataset.reportVisuals).charts||[]).filter(item=>item.status!=='skipped')}}catch{return null}
  };
  const formatAxisTick=(value,unit='')=>{
    if(!finite(value))return '—';
    const ratio=/(%|率|比例|margin|roe|roa)/i.test(unit);
    if(ratio&&Math.abs(value)<=2)return `${(value*100).toFixed(Math.abs(value*100)>=10?0:1)}%`;
    const absolute=Math.abs(value);
    if(absolute>=1e8)return `${(value/1e8).toFixed(absolute>=1e9?1:2)}亿`;
    if(absolute>=1e4)return `${(value/1e4).toFixed(absolute>=1e5?1:2)}万`;
    if(absolute>=1000)return value.toLocaleString('zh-CN',{maximumFractionDigits:0});
    if(absolute>=10)return value.toFixed(absolute%1?1:0);
    return value.toFixed(absolute&&absolute<1?2:1).replace(/\.0$/,'');
  };
  window.formatAxisTick=formatAxisTick;
  const range=(items,{zero=false,fixed=null}={})=>{
    if(fixed)return fixed;
    const values=items.flatMap(item=>item.values||[]).filter(finite);
    if(!values.length)return[0,1];
    let low=Math.min(...values),high=Math.max(...values);
    if(zero){low=Math.min(low,0);high=Math.max(high,0)}
    if(low===high){const spread=Math.abs(low||1)*.12;low-=spread;high+=spread}
    const padding=(high-low)*.08;
    return[low-padding,high+padding];
  };
  const sparseIndexes=(count,maximum=6)=>{
    if(count<=0)return[];
    if(count<=maximum)return Array.from({length:count},(_,index)=>index);
    return Array.from({length:maximum},(_,index)=>Math.round(index*(count-1)/(maximum-1)));
  };
  const canvasSetup=canvas=>{
    const box=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1;
    const width=Math.max(320,box.width),height=Math.max(300,box.height||340);
    canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);
    const context=canvas.getContext('2d');
    context.setTransform(dpr,0,0,dpr,0,0);context.clearRect(0,0,width,height);
    return{context,width,height};
  };
  const drawLine=(context,points,color,{dashed=false,width=2.1}={})=>{
    context.save();context.strokeStyle=color;context.lineWidth=width;context.lineJoin='round';context.lineCap='round';
    if(dashed)context.setLineDash([6,4]);context.beginPath();let started=false;
    points.forEach(point=>{if(!point){started=false;return}started?context.lineTo(point[0],point[1]):context.moveTo(point[0],point[1]);started=true});
    context.stroke();context.restore();
  };
  const isTechnical=chart=>String(chart.plugin_id||'').startsWith('technical_');
  const technicalPanels=chart=>{
    const plugin=chart.plugin_id||'';
    if(plugin==='technical_macd')return[
      {name:'价格与均线',keys:['close','sma5','sma20','sma60','support20','support60','resistance20','resistance60'],top:.06,bottom:.48,unit:'价格'},
      {name:'MACD',keys:['macd-dif','macd-dea','macd-hist'],top:.55,bottom:.73,unit:'MACD',zero:true},
      {name:'成交量',keys:['volume','volume-ma20'],top:.80,bottom:.93,unit:'成交量',zero:true},
    ];
    if(plugin==='technical_rsi')return[
      {name:'价格与均线',keys:['close','sma5','sma20','sma60','support20','support60','resistance20','resistance60'],top:.06,bottom:.48,unit:'价格'},
      {name:'RSI14',keys:['rsi14','rsi70','rsi30'],top:.55,bottom:.73,unit:'RSI',fixed:[0,100]},
      {name:'成交量',keys:['volume','volume-ma20'],top:.80,bottom:.93,unit:'成交量',zero:true},
    ];
    return[
      {name:'价格与均线',keys:['close','sma5','sma20','sma60','support20','support60','resistance20','resistance60'],top:.06,bottom:.67,unit:'价格'},
      {name:'成交量',keys:['volume','volume-ma20'],top:.75,bottom:.93,unit:'成交量',zero:true},
    ];
  };
  const technicalDash=series=>/^(support|resistance|rsi[37]0)/.test(series.series_id||'');
  const drawTechnicalChart=(canvas,chart)=>{
    const {context,width,height}=canvasSetup(canvas),labels=chart.labels||[],series=chart.series||[];
    if(!labels.length||!series.some(item=>(item.values||[]).some(finite)))return;
    const inset={left:66,right:28,top:10,bottom:42};
    const plotWidth=width-inset.left-inset.right;
    const x=index=>inset.left+plotWidth*(index+.5)/Math.max(labels.length,1);
    const byId=new Map(series.map(item=>[item.series_id,item]));
    const panels=technicalPanels(chart);
    const panelStates=panels.map(panel=>{
      const entries=panel.keys.map(id=>byId.get(id)).filter(Boolean);
      const top=inset.top+(height-inset.top-inset.bottom)*panel.top;
      const bottom=inset.top+(height-inset.top-inset.bottom)*panel.bottom;
      const valueRange=range(entries,{zero:panel.zero,fixed:panel.fixed});
      return{...panel,entries,top,bottom,valueRange,y:value=>top+(bottom-top)*(1-(value-valueRange[0])/(valueRange[1]-valueRange[0]))};
    });
    context.font=`11px ${font}`;
    panelStates.forEach((panel,panelIndex)=>{
      const heightPx=panel.bottom-panel.top;
      for(let index=0;index<4;index+=1){
        const gridY=panel.top+heightPx*index/3;
        context.strokeStyle='rgba(112,125,143,.18)';context.lineWidth=1;context.beginPath();context.moveTo(inset.left,gridY);context.lineTo(width-inset.right,gridY);context.stroke();
        const value=panel.valueRange[1]-(panel.valueRange[1]-panel.valueRange[0])*index/3;
        context.fillStyle='#7A8492';context.textAlign='right';context.textBaseline='middle';context.fillText(formatAxisTick(value,panel.unit),inset.left-9,gridY);
      }
      context.strokeStyle='rgba(74,85,101,.35)';context.beginPath();context.moveTo(inset.left,panel.top);context.lineTo(inset.left,panel.bottom);context.lineTo(width-inset.right,panel.bottom);context.stroke();
      context.fillStyle='#667085';context.textAlign='left';context.textBaseline='top';context.font=`600 10px ${font}`;context.fillText(panel.name,inset.left+4,panel.top+4);
      const bars=panel.entries.filter(item=>item.style==='bar');
      const barWidth=Math.max(2.5,plotWidth/Math.max(labels.length,1)/Math.max(1.8,bars.length+1));
      panel.entries.forEach(item=>{
        const values=item.values||[];
        if(item.style==='bar'){
          const baseline=panel.y(0);context.fillStyle=item.color||'#718096';context.globalAlpha=.72;
          values.forEach((value,index)=>{if(!finite(value))return;const center=x(index)+(bars.indexOf(item)-(bars.length-1)/2)*barWidth;const top=panel.y(value);context.fillRect(center-barWidth*.42,Math.min(top,baseline),barWidth*.84,Math.max(1,Math.abs(baseline-top)))});
          context.globalAlpha=1;return;
        }
        drawLine(context,values.map((value,index)=>finite(value)?[x(index),panel.y(value)]:null),item.color||'#0A84FF',{dashed:technicalDash(item),width:item.series_id==='close'?2.45:1.65});
      });
      if(panelIndex<panelStates.length-1){context.strokeStyle='rgba(112,125,143,.28)';context.setLineDash([2,3]);context.beginPath();context.moveTo(inset.left,panel.bottom+9);context.lineTo(width-inset.right,panel.bottom+9);context.stroke();context.setLineDash([])}
    });
    const bottomPanel=panelStates[panelStates.length-1];
    context.fillStyle='#667085';context.textAlign='right';context.textBaseline='middle';context.font=`11px ${font}`;
    sparseIndexes(labels.length).forEach(index=>{const text=String(labels[index]).slice(0,10);context.save();context.translate(x(index)+4,bottomPanel.bottom+20);context.rotate(-Math.PI/6);context.fillText(text,0,0);context.restore()});
    (chart.annotations||[]).forEach((annotation,index)=>{
      if(annotation.index<0||annotation.index>=labels.length)return;
      const plugin=chart.plugin_id||'';
      const panel=plugin==='technical_macd'?panelStates[1]:plugin==='technical_rsi'?panelStates[1]:plugin==='technical_volume_price'?panelStates[panelStates.length-1]:panelStates[0];
      const value=finite(annotation.value)?annotation.value:(panel.valueRange[0]+panel.valueRange[1])/2;
      const markerY=Math.min(panel.bottom-3,Math.max(panel.top+14,panel.y(value))),markerX=x(annotation.index);
      context.save();context.strokeStyle='#7B61FF';context.fillStyle='#7B61FF';context.setLineDash([4,4]);context.beginPath();context.moveTo(markerX,panel.top);context.lineTo(markerX,panel.bottom);context.stroke();context.setLineDash([]);context.beginPath();context.arc(markerX,markerY,4,0,Math.PI*2);context.fill();
      context.font=`600 11px ${font}`;context.textAlign=markerX>width*.72?'right':'left';context.textBaseline='top';context.fillText(String(annotation.label||'形态').slice(0,20),markerX+(markerX>width*.72?-8:8),panel.top+5+(index%2)*15);context.restore();
    });
    canvas.title=series.map(item=>`${item.name}: ${(item.values||[]).join(' / ')}`).join('\n');
  };
  const drawStandardChart=(canvas,chart)=>{
    const {context,width,height}=canvasSetup(canvas),seriesList=chart.series||[],labels=chart.labels||[];
    if(!seriesList.flatMap(series=>series.values||[]).some(finite)||!labels.length)return;
    const hasSecondary=seriesList.some(series=>(series.axis||'primary')==='secondary');
    const inset={left:64,right:hasSecondary?64:28,top:32,bottom:48};
    const barTypes=new Set(['bar','stacked_bar','waterfall','timeline']);
    const isBarSeries=series=>series.style==='bar'||barTypes.has(chart.chart_type);
    const primary=range(seriesList.filter(series=>(series.axis||'primary')==='primary'),{zero:seriesList.some(series=>(series.axis||'primary')==='primary'&&isBarSeries(series))});
    const secondary=range(seriesList.filter(series=>(series.axis||'primary')==='secondary'),{zero:seriesList.some(series=>(series.axis||'primary')==='secondary'&&isBarSeries(series))});
    const plotWidth=width-inset.left-inset.right,plotHeight=height-inset.top-inset.bottom;
    const x=index=>inset.left+plotWidth*(index+.5)/Math.max(labels.length,1);
    const y=(value,axis='primary')=>{const values=axis==='secondary'?secondary:primary;return inset.top+plotHeight*(1-(value-values[0])/(values[1]-values[0]))};
    context.font=`11px ${font}`;
    for(let index=0;index<5;index+=1){const gridY=inset.top+plotHeight*index/4;context.strokeStyle='rgba(99,110,125,.16)';context.beginPath();context.moveTo(inset.left,gridY);context.lineTo(width-inset.right,gridY);context.stroke();context.fillStyle='#7A8492';context.textAlign='right';context.textBaseline='middle';context.fillText(formatAxisTick(primary[1]-(primary[1]-primary[0])*index/4,chart.unit||''),inset.left-10,gridY);if(hasSecondary){context.textAlign='left';context.fillText(formatAxisTick(secondary[1]-(secondary[1]-secondary[0])*index/4,chart.secondary_unit||''),width-inset.right+10,gridY)}}
    context.strokeStyle='rgba(74,85,101,.32)';context.beginPath();context.moveTo(inset.left,inset.top);context.lineTo(inset.left,height-inset.bottom);context.lineTo(width-inset.right,height-inset.bottom);context.stroke();
    const bars=seriesList.filter(isBarSeries),barWidth=Math.max(3,plotWidth/Math.max(labels.length,1)/Math.max(bars.length+1,2));
    seriesList.forEach(series=>{const axis=series.axis||'primary',color=series.color||'#0A84FF',values=series.values||[];if(isBarSeries(series)){const barIndex=Math.max(0,bars.indexOf(series)),base=y(0,axis);context.fillStyle=color;context.globalAlpha=.82;values.forEach((value,index)=>{if(!finite(value))return;const center=x(index)+(barIndex-(bars.length-1)/2)*barWidth,top=y(value,axis);context.fillRect(center-barWidth*.4,Math.min(top,base),barWidth*.8,Math.max(1,Math.abs(base-top)))});context.globalAlpha=1;return}drawLine(context,values.map((value,index)=>finite(value)?[x(index),y(value,axis)]:null),color)});
    context.fillStyle='#7A8492';context.textAlign='center';context.textBaseline='top';const labelStep=Math.max(1,Math.ceil(labels.length/6));labels.forEach((label,index)=>{if(index%labelStep&&index!==labels.length-1)return;const text=String(label);context.fillText(text.length>11?`${text.slice(0,10)}…`:text,x(index),height-inset.bottom+13)});
    (chart.annotations||[]).forEach((annotation,index)=>{if(annotation.index<0||annotation.index>=labels.length)return;const annotationX=x(annotation.index),annotationY=finite(annotation.value)?y(annotation.value):inset.top+18;context.save();context.strokeStyle=index%2?'#7B61FF':'#0A84FF';context.fillStyle=context.strokeStyle;context.setLineDash([4,4]);context.beginPath();context.moveTo(annotationX,inset.top);context.lineTo(annotationX,height-inset.bottom);context.stroke();context.setLineDash([]);context.beginPath();context.arc(annotationX,Math.min(height-inset.bottom,Math.max(inset.top,annotationY)),4,0,Math.PI*2);context.fill();const label=String(annotation.label||'形态').slice(0,20);context.font=`600 11px ${font}`;context.textAlign=annotationX>width*.7?'right':'left';context.textBaseline='top';context.fillText(label,annotationX+(annotationX>width*.7?-7:7),inset.top+4+(index%3)*15);context.restore()});
    canvas.title=seriesList.map(series=>`${series.name}: ${(series.values||[]).join(' / ')}`).join('\n');
  };
  const drawChart=(canvas,chart)=>isTechnical(chart)?drawTechnicalChart(canvas,chart):drawStandardChart(canvas,chart);
  const installTooltip=(canvas,chart)=>{
    if(canvas.dataset.tooltipReady==='1')return;canvas.dataset.tooltipReady='1';
    const host=canvas.parentElement;host.style.position='relative';const tooltip=document.createElement('div');tooltip.hidden=true;tooltip.className='chart-tooltip';host.append(tooltip);
    canvas.addEventListener('mousemove',event=>{const rect=canvas.getBoundingClientRect(),ratio=(event.clientX-rect.left)/Math.max(rect.width,1),index=Math.max(0,Math.min(chart.labels.length-1,Math.floor(ratio*chart.labels.length)));tooltip.textContent=`${chart.labels[index]} · ${chart.series.map(series=>`${series.name}: ${series.values[index]??'—'}`).join(' | ')}`;tooltip.style.left=`${Math.min(rect.width-220,event.clientX-rect.left+12)}px`;tooltip.style.top=`${Math.max(8,event.clientY-rect.top-42)}px`;tooltip.hidden=false});
    canvas.addEventListener('mouseleave',()=>{tooltip.hidden=true});
  };
  const render=container=>{const payload=readVisuals(container);if(!payload)return;payload.root.querySelectorAll('canvas[data-chart]').forEach(canvas=>{const chart=payload.charts.find(item=>key(item)===canvas.dataset.chart);if(!chart)return;requestAnimationFrame(()=>drawChart(canvas,chart));installTooltip(canvas,chart)});};
  let resizeFrame=0;window.addEventListener('resize',()=>{cancelAnimationFrame(resizeFrame);resizeFrame=requestAnimationFrame(()=>render(document))},{passive:true});
  window.renderFinancialReportCharts=render;window.renderFinancialReportChartTooltips=()=>{};
  const boot=()=>requestAnimationFrame(()=>render(document));
  document.readyState==='loading'?document.addEventListener('DOMContentLoaded',boot,{once:true}):boot();
}());
""".strip()
