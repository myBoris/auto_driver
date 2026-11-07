"""仅在本机查看采集图像和保存人工标签；不连接 Center，也不发送驾驶指令。"""

import asyncio
import json
import os
from pathlib import Path

from aiohttp import web

from .dataset import BUTTON_NAMES, DEFAULT_LANE_Y, image_path, load_samples, validate_action, validate_lane, write_samples
from src.domain.types import finite_number


PAGE = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>驾驶训练数据标注</title>
<style>body{margin:0;background:#0d141c;color:#d9e4ef;font:14px system-ui}main{max-width:1280px;margin:auto;padding:22px}h1{font-size:20px}p{color:#9db0c2;line-height:1.7}button,select{background:#203041;color:inherit;border:1px solid #445a70;border-radius:5px;padding:8px;cursor:pointer}button:disabled{opacity:.45;cursor:default}.bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:12px 0}.grid{display:grid;grid-template-columns:minmax(0,1fr) 280px;gap:18px}.preview{background:#080c11;align-self:start}canvas{display:block;width:100%;height:auto;touch-action:none}.panel{background:#15202b;border:1px solid #2a3e51;padding:15px;border-radius:8px}.field{display:block;margin:16px 0}.field span{display:flex;justify-content:space-between}input[type=range]{width:100%;accent-color:#91ebba}.buttons{display:flex;gap:8px;flex-wrap:wrap}.buttons label{padding:5px;background:#203041;border-radius:4px}#message{min-height:22px;color:#a7edc6}#identity{word-break:break-all;font-size:12px;color:#9db0c2}#save{background:#2b5b43}kbd{color:#e2bf83}a{color:#91ebba}@media(max-width:760px){.grid{grid-template-columns:1fr}main{padding:12px}}</style></head><body><main>
<h1>驾驶训练数据标注</h1><p>可先自动预标注道路边界，再检查和拖动修正；候选保留“未复核”状态，保存后成为人工标签。也可点击左右道路边缘，每侧至少两个不同高度的点。点会吸附到水平参考线；无道路时选“没有道路”。操作标签可沿用 Center 采集值，或手动修正为这张画面应执行的动作。</p>
<div class="bar"><button id="previous">上一张</button><select id="sample"></select><button id="next">下一张</button><span id="progress"></span><button id="save">保存标签</button><button id="save-next">保存并下一张</button></div><div id="message" role="status"></div>
<div class="grid"><div class="preview"><canvas id="canvas"></canvas></div><aside class="panel"><div id="identity"></div>
<label class="field">车道标签 <select id="lane-state"><option value="unknown">尚未标注</option><option value="visible">有道路</option><option value="absent">没有道路</option></select></label>
<div id="lane-origin" role="status">车道尚未标注</div><div class="bar"><button id="auto-lane">自动预标注当前图片</button></div>
<details><summary>预标注范围与置信度</summary><label class="field"><span>最小置信度 <output id="auto-confidence-value">75%</output></span><input id="auto-confidence" type="range" min="0" max="100" value="75"></label><label class="field"><span>检测区域顶部 <output id="auto-top-value">40%</output></span><input id="auto-top" type="range" min="0" max="95" value="40"></label><label class="field"><span>检测区域底部 <output id="auto-bottom-value">70%</output></span><input id="auto-bottom" type="range" min="5" max="100" value="70"></label><p>范围以完整图片高度为准。缩小到前方道路，避开天空、引擎盖和仪表盘。未检出候选时保留原标签，不自动标记为“没有道路”。</p></details>
<div class="bar"><label><input type="radio" name="side" value="left" checked>左边界</label><label><input type="radio" name="side" value="right">右边界</label></div><div class="bar"><button id="undo">撤销当前侧末点</button><button id="clear">清空边界</button></div>
<label><input type="checkbox" id="action-enabled">保存驾驶操作标签</label><div id="axes"></div><div class="buttons" id="buttons"></div><p>转向负值向左、正值向右。LB 为离合开关，X / B 为降挡 / 升挡。这里仅修改离线标签。</p>
</aside></div></main><script>
'use strict';
let samples=[], index=0, points={left:[],right:[]}, loaded=null, dragging=null, laneDirty=false, autoRunning=false;
const ys=[.95,.85,.75,.65,.55,.45], canvas=document.getElementById('canvas'),ctx=canvas.getContext('2d'),picture=new Image();
const $=id=>document.getElementById(id), note=(text,error=false)=>{$('message').textContent=text;$('message').style.color=error?'#ffad99':'#a7edc6';};
async function request(path,options){const r=await fetch(path,{cache:'no-store',...options});const data=await r.json();if(!r.ok)throw Error(data.error||'请求失败');return data;}
function draw(){if(!picture.complete||!picture.naturalWidth)return;ctx.clearRect(0,0,canvas.width,canvas.height);ctx.drawImage(picture,0,0);ctx.lineWidth=Math.max(1,canvas.width/800);for(const y of ys){ctx.strokeStyle='#ffffff44';ctx.setLineDash([6,8]);ctx.beginPath();ctx.moveTo(0,y*canvas.height);ctx.lineTo(canvas.width,y*canvas.height);ctx.stroke();}ctx.setLineDash([]);for(const side of ['left','right']){const ordered=[...points[side]].sort((a,b)=>a[1]-b[1]);ctx.strokeStyle=ctx.fillStyle=side==='left'?'#86f0b8':'#ffcc77';ctx.lineWidth=Math.max(2,canvas.width/400);ctx.beginPath();ordered.forEach(([x,y],i)=>{if(i)ctx.lineTo(x*canvas.width,y*canvas.height);else ctx.moveTo(x*canvas.width,y*canvas.height);});ctx.stroke();for(const [x,y]of ordered){ctx.beginPath();ctx.arc(x*canvas.width,y*canvas.height,Math.max(4,canvas.width/160),0,2*Math.PI);ctx.fill();}}}
function laneStatus(row){if(!row.lane)return'车道待标';return row.lane_source==='auto'&&row.lane_annotation?.reviewed!==true?'车道自动·未复核':'车道✓';}
function status(row){return laneStatus(row)+' / '+(row.action?'操作✓':'操作待标');}
function showLaneOrigin(){if(!loaded)return;const annotation=loaded.lane_annotation;let text=!loaded.lane?'车道尚未标注':loaded.lane_source==='auto'&&annotation?.reviewed!==true?'自动候选 · 未复核':'人工已保存';if(annotation&&typeof annotation.confidence==='number')text+=` · 候选置信 ${Math.round(annotation.confidence*100)}%`;if(annotation?.method)text+=` · 来源 ${annotation.method}`;$('lane-origin').textContent=text;$('lane-origin').style.color=loaded.lane_source==='auto'&&annotation?.reviewed!==true?'#ffd18a':'#9db0c2';$('auto-lane').disabled=autoRunning||!!(loaded.lane&&loaded.lane_source!=='auto');$('auto-lane').title=loaded.lane&&loaded.lane_source!=='auto'?'已有保存的车道标签，自动预标注不会覆盖':'仅生成当前图片的候选；请检查后保存';}
function refreshOptions(){const select=$('sample');select.replaceChildren();samples.forEach((row,i)=>{const option=document.createElement('option');option.value=String(i);option.textContent=`${i+1} · ${status(row)} · ${row.sequence}`;select.add(option);});select.value=String(index);$('progress').textContent=`${index+1} / ${samples.length}`;}
async function load(i){try{loaded=await request('/api/samples/'+i);index=i;laneDirty=false;points={left:loaded.lane?.left?.map(p=>[...p])||[],right:loaded.lane?.right?.map(p=>[...p])||[]};$('lane-state').value=loaded.lane?(loaded.lane.visible?'visible':'absent'):'unknown';$('action-enabled').checked=!!loaded.action;for(const axis of ['steering','throttle','brake']){$(axis).value=String(Math.round((loaded.action?.[axis]||0)*100));$(axis+'-value').textContent=$(axis).value+'%';}for(const button of document.querySelectorAll('[data-button]'))button.checked=loaded.action?.buttons.includes(button.dataset.button)||false;$('identity').textContent=`${loaded.episode_id} · 帧 ${loaded.sequence} · ${loaded.action_source||'无操作来源'}`;refreshOptions();showLaneOrigin();$('previous').disabled=index===0;$('next').disabled=index===samples.length-1;picture.src='/api/image/'+i;note('');}catch(e){note(e.message,true);}}
picture.onload=()=>{canvas.width=picture.naturalWidth;canvas.height=picture.naturalHeight;draw();};picture.onerror=()=>note('图片无法解码',true);
for(const [axis,label,min]of [['steering','转向',-100],['throttle','油门',0],['brake','刹车',0]]){const field=document.createElement('label');field.className='field';const heading=document.createElement('span');heading.textContent=label;const output=document.createElement('output');output.id=axis+'-value';heading.append(output);const input=document.createElement('input');Object.assign(input,{id:axis,type:'range',min:String(min),max:'100',value:'0'});input.oninput=()=>{output.textContent=input.value+'%';$('action-enabled').checked=true;};field.append(heading,input);$('axes').append(field);}
async function init(){try{const result=await request('/api/samples');samples=result.samples;for(const name of result.buttons){const label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.dataset.button=name;input.onchange=()=>{$('action-enabled').checked=true;};label.append(input,document.createTextNode(name));$('buttons').append(label);}if(!samples.length){note('尚无采集样本，请先停止标注服务并从 Center 采集数据。');return;}await load(0);}catch(e){note(e.message,true);}}
function position(event){const box=canvas.getBoundingClientRect();return [Math.max(0,Math.min(1,(event.clientX-box.left)/box.width)),Math.max(0,Math.min(1,(event.clientY-box.top)/box.height))];}
canvas.onpointerdown=event=>{if(!loaded||event.button!==0||autoRunning)return;laneDirty=true;const side=document.querySelector('[name=side]:checked').value;const [x,y]=position(event),anchor=ys.reduce((a,b)=>Math.abs(b-y)<Math.abs(a-y)?b:a);let i=points[side].findIndex(p=>Math.abs(p[1]-anchor)<.015);if(i<0){i=points[side].length;points[side].push([x,anchor]);}else points[side][i]=[x,anchor];dragging={side,index:i};canvas.setPointerCapture(event.pointerId);$('lane-state').value='visible';draw();};
canvas.onpointermove=event=>{if(!dragging)return;points[dragging.side][dragging.index][0]=position(event)[0];draw();};for(const type of ['pointerup','pointercancel','lostpointercapture'])canvas.addEventListener(type,()=>{dragging=null;});
$('undo').onclick=()=>{laneDirty=true;points[document.querySelector('[name=side]:checked').value].pop();draw();};$('clear').onclick=()=>{laneDirty=true;points={left:[],right:[]};$('lane-state').value='unknown';draw();};$('lane-state').onchange=()=>{laneDirty=true;if($('lane-state').value!=='visible')points={left:[],right:[]};draw();};
async function save(next){if(!loaded||autoRunning)return;try{const laneState=$('lane-state').value;const payload={lane:laneState==='unknown'?null:laneState==='absent'?{visible:false}:{visible:true,left:points.left,right:points.right},action:$('action-enabled').checked?{steering:Number($('steering').value)/100,throttle:Number($('throttle').value)/100,brake:Number($('brake').value)/100,buttons:[...document.querySelectorAll('[data-button]:checked')].map(el=>el.dataset.button),duration_ms:loaded.action?.duration_ms||100}:null};loaded=await request('/api/samples/'+index,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});laneDirty=false;samples[index]=loaded;refreshOptions();showLaneOrigin();note('标签已保存为人工复核标签');if(next&&index<samples.length-1)await load(index+1);}catch(e){note(e.message,true);}}
for(const name of ['confidence','top','bottom'])$('auto-'+name).oninput=()=>$('auto-'+name+'-value').textContent=$('auto-'+name).value+'%';
$('auto-lane').onclick=async()=>{if(!loaded||autoRunning)return;if(laneDirty){note('当前车道有未保存修改，请先保存或重新选择图片后再预标注。',true);return;}const target=index;autoRunning=true;showLaneOrigin();note('正在检测当前图片的道路边界…');try{const result=await request('/api/samples/'+target+'/auto-lane',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({min_confidence:Number($('auto-confidence').value)/100,roi_top:Number($('auto-top').value)/100,roi_bottom:Number($('auto-bottom').value)/100})});samples[target]=result.sample;if(index===target){loaded=result.sample;points={left:loaded.lane?.left?.map(p=>[...p])||[],right:loaded.lane?.right?.map(p=>[...p])||[]};$('lane-state').value=loaded.lane?(loaded.lane.visible?'visible':'absent'):'unknown';refreshOptions();draw();note(result.detected?'已生成自动候选，尚未复核；请检查、修正后保存。':result.message||'未检出可靠候选，原标签保持不变。',!result.detected);}}catch(e){note(e.message,true);}finally{autoRunning=false;showLaneOrigin();}};
$('save').onclick=()=>save(false);$('save-next').onclick=()=>save(true);$('sample').onchange=()=>load(Number($('sample').value));$('previous').onclick=()=>load(Math.max(0,index-1));$('next').onclick=()=>load(Math.min(samples.length-1,index+1));init();
</script></body></html>'''


def create_annotation_app(dataset_dir: Path):
    """同源页面只接受样本编号，图片路径始终由受验证的清单提供。"""
    root = Path(dataset_dir).resolve()
    load_samples(root)
    app = web.Application(client_max_size=128 * 1024)
    mutation_lock = asyncio.Lock()

    def sample_for(request):
        try:
            samples = load_samples(root)
            index = int(request.match_info["index"])
            if index < 0 or index >= len(samples):
                raise IndexError
            return samples, index, samples[index]
        except (ValueError, IndexError) as exc:
            raise web.HTTPNotFound(text="样本不存在") from exc

    async def page(request):
        return web.Response(text=PAGE, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def listing(request):
        return web.json_response({"samples": load_samples(root), "buttons": BUTTON_NAMES,
                                  "lane_y": DEFAULT_LANE_Y}, headers={"Cache-Control": "no-store"})

    async def read_sample(request):
        return web.json_response(sample_for(request)[2], headers={"Cache-Control": "no-store"})

    async def read_image(request):
        sample = sample_for(request)[2]
        return web.FileResponse(image_path(root, sample["image"]), headers={"Cache-Control": "no-store"})

    async def save(request):
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.scheme}://{request.host}":
            return web.json_response({"error": "只能从本地标注页面保存"}, status=403)
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or payload.keys() - {"action", "lane"}:
                raise ValueError("只接受 action 和 lane 标签")
            action = validate_action(payload["action"]) if payload.get("action") is not None else None
            lane = validate_lane(payload["lane"]) if payload.get("lane") is not None else None
            async with mutation_lock:
                try:
                    descriptor = os.open(root / ".collection.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    return web.json_response({"error": "数据正在采集或保存，请先停止采集后再标注"}, status=409)
                try:
                    os.close(descriptor)
                    samples, index, previous = sample_for(request)
                    updated = {**previous, "action": action, "lane": lane}
                    if action is not None:
                        # 即使没有修改采集值，也明确保存这次人工核对的标签来源。
                        updated["action_source"] = "human"
                    else:
                        updated.pop("action_source", None)
                    updated["lane_source"] = "human" if lane is not None else None
                    if lane is not None and isinstance(previous.get("lane_annotation"), dict):
                        updated["lane_annotation"] = {**previous["lane_annotation"], "reviewed": True}
                    elif lane is None:
                        updated.pop("lane_annotation", None)
                    samples[index] = updated
                    write_samples(root, samples)
                finally:
                    (root / ".collection.lock").unlink(missing_ok=True)
            return web.json_response(updated)
        except (TypeError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def auto_lane(request):
        """当前图片的视觉候选保留自动来源；失败不等同于人工确认没有道路。"""
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.scheme}://{request.host}":
            return web.json_response({"error": "只能从本地标注页面生成预标注"}, status=403)
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or payload.keys() - {"min_confidence", "roi_top", "roi_bottom"}:
                raise ValueError("只接受 min_confidence、roi_top 和 roi_bottom 预标注参数")
            from .auto_lanes import AutoLaneConfig, AutoLaneLabeler
            config = AutoLaneConfig(**payload)
            async with mutation_lock:
                try:
                    descriptor = os.open(root / ".collection.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    return web.json_response({"error": "数据正在采集或保存，请先停止采集后再预标注"}, status=409)
                try:
                    os.close(descriptor)
                    samples, index, previous = sample_for(request)
                    if previous.get("lane") is not None and previous.get("lane_source") != "auto":
                        return web.json_response({"error": "已有人工或已保存车道标签，自动预标注不会覆盖"}, status=409)
                    jpeg = image_path(root, previous["image"]).read_bytes()
                    candidate = await asyncio.to_thread(AutoLaneLabeler(config).annotate, jpeg)
                    if candidate is None:
                        return web.json_response({"detected": False, "sample": previous,
                                                  "message": "未检出可靠道路候选；可调整检测范围、降低阈值或手工标注，原标签未改变。"})
                    lane = validate_lane(candidate["lane"])
                    metadata = candidate.get("lane_annotation")
                    if not lane["visible"] or not isinstance(metadata, dict):
                        raise ValueError("自动预标注必须返回可见道路和候选置信度")
                    confidence = finite_number(metadata.get("confidence"), "confidence", 0, 1)
                    method = metadata.get("method")
                    if not isinstance(method, str) or not 1 <= len(method) <= 128:
                        raise ValueError("自动预标注缺少有效 method 来源")
                    updated = {**previous, "lane": lane, "lane_source": "auto",
                               "lane_annotation": {**metadata, "method": method, "confidence": confidence, "reviewed": False}}
                    samples[index] = updated
                    write_samples(root, samples)
                finally:
                    (root / ".collection.lock").unlink(missing_ok=True)
            return web.json_response({"detected": True, "sample": updated})
        except (TypeError, ValueError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except RuntimeError as exc:
            return web.json_response({"error": str(exc)}, status=503)

    app.add_routes([web.get("/", page), web.get("/api/samples", listing),
                    web.get("/api/samples/{index}", read_sample), web.get("/api/image/{index}", read_image),
                    web.post("/api/samples/{index}", save),
                    web.post("/api/samples/{index}/auto-lane", auto_lane)])
    return app


def run_annotation_server(dataset_dir: Path, host="127.0.0.1", port=8766):
    """标注服务绑定本机，停止后标签保留在 samples.jsonl。"""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("标注服务仅支持本机监听")
    web.run_app(create_annotation_app(dataset_dir), host=host, port=port)
