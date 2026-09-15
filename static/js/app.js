const form=document.getElementById('analysisForm');
const mediaInput=document.getElementById('mediaInput');
const dropzone=document.getElementById('dropzone');
const fileName=document.getElementById('fileName');
const autoLine=document.getElementById('autoLine');
const manualFields=document.getElementById('manualFields');
const startInput=document.getElementById('start');
const endInput=document.getElementById('end');
const submitBtn=document.getElementById('submitBtn');
const statusText=document.getElementById('statusText');
const stateBadge=document.getElementById('stateBadge');
const progressBar=document.getElementById('progressBar');
const errorBox=document.getElementById('errorBox');
const previewEmpty=document.getElementById('previewEmpty');
const resultImage=document.getElementById('resultImage');
const resultVideo=document.getElementById('resultVideo');
const stats=document.getElementById('stats');
const openResult=document.getElementById('openResult');
const autoPanel=document.getElementById('autoPanel');
const manualPanel=document.getElementById('manualPanel');
const autoModeCard=document.getElementById('autoModeCard');
const manualModeCard=document.getElementById('manualModeCard');

// Visual calibration controls
const calibrationCanvas=document.getElementById('calibrationCanvas');
const calibrationPlaceholder=document.getElementById('calibrationPlaceholder');
const calibrationVideoSource=document.getElementById('calibrationVideoSource');
const knownDistance=document.getElementById('knownDistance');
const pickScaleBtn=document.getElementById('pickScaleBtn');
const pickLineBtn=document.getElementById('pickLineBtn');
const clearVisualBtn=document.getElementById('clearVisualBtn');
const visualCalibrationStatus=document.getElementById('visualCalibrationStatus');
const pixelsPerMeter=document.getElementById('pixelsPerMeter');

let timer=null;
let previewObjectUrl=null;
let baseFrame=null; // offscreen canvas storing the selected image / video frame
let calibrationMode=null; // 'scale' | 'line' | null
let scalePoints=[];
let linePoints=[];

function currentMode(){return form.querySelector('input[name="mode"]:checked')?.value||'auto'}

function syncLine(){
  const manual=!autoLine.checked;
  startInput.disabled=!manual;
  endInput.disabled=!manual;
  manualFields.classList.toggle('disabled',!manual);
}

function syncMode(){
  const manual=currentMode()==='manual';
  autoPanel.classList.toggle('hidden',manual);
  manualPanel.classList.toggle('hidden',!manual);
  autoModeCard.classList.toggle('active',!manual);
  manualModeCard.classList.toggle('active',manual);
  manualPanel.querySelectorAll('input').forEach(el=>{
    if(el===startInput||el===endInput)return;
    el.disabled=!manual;
  });
  manualPanel.querySelectorAll('button').forEach(el=>el.disabled=!manual);
  if(manual){
    autoLine.disabled=false;
    syncLine();
  }else{
    autoLine.disabled=true;
    startInput.disabled=true;
    endInput.disabled=true;
    setCalibrationMode(null);
  }
}

form.querySelectorAll('input[name="mode"]').forEach(r=>r.addEventListener('change',syncMode));
autoLine.addEventListener('change',syncLine);
syncMode();

function setVisualStatus(text,kind=''){
  visualCalibrationStatus.textContent=text;
  visualCalibrationStatus.className='visual-status'+(kind?' '+kind:'');
}

function setCalibrationMode(mode){
  calibrationMode=mode;
  pickScaleBtn.classList.toggle('active',mode==='scale');
  pickLineBtn.classList.toggle('active',mode==='line');
  if(mode==='scale'){
    scalePoints=[];
    setVisualStatus('Click mốc chuẩn thứ nhất, sau đó click mốc chuẩn thứ hai.','warn');
  }else if(mode==='line'){
    linePoints=[];
    autoLine.checked=false;
    syncLine();
    setVisualStatus('Click điểm đầu của đường đo, sau đó click điểm cuối.','warn');
  }
  redrawCalibrationCanvas();
}

function cleanupPreviewUrl(){
  if(previewObjectUrl){
    URL.revokeObjectURL(previewObjectUrl);
    previewObjectUrl=null;
  }
}

function setBaseFrameFromSource(source,width,height){
  const maxDimension=2200;
  let drawWidth=width;
  let drawHeight=height;
  const largest=Math.max(width,height);
  if(largest>maxDimension){
    const scale=maxDimension/largest;
    drawWidth=Math.round(width*scale);
    drawHeight=Math.round(height*scale);
  }

  // The canvas coordinates must match the original media coordinates. To preserve
  // that mapping even when a huge preview is downscaled, remember the scale.
  const c=document.createElement('canvas');
  c.width=drawWidth;
  c.height=drawHeight;
  const ctx=c.getContext('2d');
  ctx.drawImage(source,0,0,drawWidth,drawHeight);
  baseFrame={canvas:c,originalWidth:width,originalHeight:height,previewWidth:drawWidth,previewHeight:drawHeight};

  calibrationCanvas.width=drawWidth;
  calibrationCanvas.height=drawHeight;
  calibrationPlaceholder.style.display='none';
  redrawCalibrationCanvas();
  setVisualStatus(`Khung hình sẵn sàng (${width}×${height}). Chọn bước calibration bên dưới.`,'ok');
}

function loadVisualPreview(file){
  cleanupPreviewUrl();
  baseFrame=null;
  scalePoints=[];
  linePoints=[];
  calibrationMode=null;
  pickScaleBtn.classList.remove('active');
  pickLineBtn.classList.remove('active');
  calibrationPlaceholder.style.display='grid';
  calibrationPlaceholder.textContent='Đang tải khung hình để calibration...';
  calibrationCanvas.width=1;
  calibrationCanvas.height=1;

  if(!file){
    calibrationPlaceholder.textContent='Chọn ảnh/video ở bước 1 để bắt đầu calibration';
    return;
  }

  previewObjectUrl=URL.createObjectURL(file);
  const isVideo=file.type.startsWith('video/') || /\.(mp4|avi|mov|mkv|m4v)$/i.test(file.name);

  if(isVideo){
    calibrationVideoSource.src=previewObjectUrl;
    calibrationVideoSource.currentTime=0;
    const capture=()=>{
      if(!calibrationVideoSource.videoWidth||!calibrationVideoSource.videoHeight)return;
      try{
        setBaseFrameFromSource(calibrationVideoSource,calibrationVideoSource.videoWidth,calibrationVideoSource.videoHeight);
      }catch(err){
        setVisualStatus('Không thể tạo preview video: '+err.message,'warn');
      }
    };
    calibrationVideoSource.onloadeddata=()=>{
      try{
        const target=Math.min(0.15,Math.max(0,calibrationVideoSource.duration||0));
        if(target>0){
          calibrationVideoSource.currentTime=target;
        }else{
          capture();
        }
      }catch(_){capture();}
    };
    calibrationVideoSource.onseeked=capture;
    calibrationVideoSource.onerror=()=>setVisualStatus('Trình duyệt không đọc được preview video này. Bạn vẫn có thể nhập thông số bằng tay.','warn');
    calibrationVideoSource.load();
  }else{
    const img=new Image();
    img.onload=()=>setBaseFrameFromSource(img,img.naturalWidth,img.naturalHeight);
    img.onerror=()=>setVisualStatus('Không thể hiển thị preview ảnh. Bạn vẫn có thể nhập thông số bằng tay.','warn');
    img.src=previewObjectUrl;
  }
}

function displayToOriginalPoint(previewX,previewY){
  if(!baseFrame)return null;
  return {
    x:previewX*(baseFrame.originalWidth/baseFrame.previewWidth),
    y:previewY*(baseFrame.originalHeight/baseFrame.previewHeight)
  };
}

function originalToPreviewPoint(point){
  if(!baseFrame)return point;
  return {
    x:point.x*(baseFrame.previewWidth/baseFrame.originalWidth),
    y:point.y*(baseFrame.previewHeight/baseFrame.originalHeight)
  };
}

function redrawCalibrationCanvas(){
  if(!baseFrame)return;
  const ctx=calibrationCanvas.getContext('2d');
  ctx.clearRect(0,0,calibrationCanvas.width,calibrationCanvas.height);
  ctx.drawImage(baseFrame.canvas,0,0);

  function drawPoint(point,color,label){
    const p=originalToPreviewPoint(point);
    ctx.beginPath();
    ctx.arc(p.x,p.y,7,0,Math.PI*2);
    ctx.fillStyle=color;
    ctx.fill();
    ctx.lineWidth=2;
    ctx.strokeStyle='#ffffff';
    ctx.stroke();
    ctx.font='bold 13px Segoe UI, Arial';
    ctx.fillStyle='#ffffff';
    ctx.fillText(label,p.x+10,p.y-10);
  }

  function drawLine(points,color,label){
    if(points.length<1)return;
    const a=originalToPreviewPoint(points[0]);
    if(points.length>1){
      const b=originalToPreviewPoint(points[1]);
      ctx.beginPath();
      ctx.moveTo(a.x,a.y);
      ctx.lineTo(b.x,b.y);
      ctx.lineWidth=4;
      ctx.strokeStyle=color;
      ctx.stroke();
      const mx=(a.x+b.x)/2;
      const my=(a.y+b.y)/2;
      ctx.font='bold 13px Segoe UI, Arial';
      ctx.fillStyle=color;
      ctx.fillText(label,mx+8,my-8);
    }
  }

  drawLine(scalePoints,'#ffd166','Scale');
  scalePoints.forEach((p,i)=>drawPoint(p,'#ffd166','S'+(i+1)));
  drawLine(linePoints,'#4d8dff','Measurement');
  linePoints.forEach((p,i)=>drawPoint(p,'#4d8dff','M'+(i+1)));
}

function calculatePixelsPerMeter(){
  if(scalePoints.length!==2)return;
  const meters=Number(knownDistance.value);
  if(!Number.isFinite(meters)||meters<=0){
    setVisualStatus('Khoảng cách thật phải lớn hơn 0 mét.','warn');
    return;
  }
  const dx=scalePoints[1].x-scalePoints[0].x;
  const dy=scalePoints[1].y-scalePoints[0].y;
  const pixelDistance=Math.hypot(dx,dy);
  const ppm=pixelDistance/meters;
  pixelsPerMeter.value=ppm.toFixed(3);
  setVisualStatus(`Đã đo ${pixelDistance.toFixed(1)} px cho ${meters} m → ${ppm.toFixed(2)} pixels/m. Tiếp theo hãy đặt đường đo.`,'ok');
}

function applyMeasurementLine(){
  if(linePoints.length!==2)return;
  const a=linePoints[0];
  const b=linePoints[1];
  startInput.value=`${a.x.toFixed(1)} ${a.y.toFixed(1)}`;
  endInput.value=`${b.x.toFixed(1)} ${b.y.toFixed(1)}`;
  autoLine.checked=false;
  syncLine();
  setVisualStatus(`Đã đặt đường đo: (${a.x.toFixed(0)}, ${a.y.toFixed(0)}) → (${b.x.toFixed(0)}, ${b.y.toFixed(0)}). Bạn có thể Analyze hoặc lưu làm Automatic.`,'ok');
}

calibrationCanvas.addEventListener('click',e=>{
  if(currentMode()!=='manual')return;
  if(!baseFrame){
    setVisualStatus('Hãy chọn ảnh/video trước.','warn');
    return;
  }
  if(!calibrationMode){
    setVisualStatus('Chọn “Chọn 2 mốc chuẩn” hoặc “Chọn 2 điểm đường đo” trước.','warn');
    return;
  }
  const rect=calibrationCanvas.getBoundingClientRect();
  const previewX=(e.clientX-rect.left)*(calibrationCanvas.width/rect.width);
  const previewY=(e.clientY-rect.top)*(calibrationCanvas.height/rect.height);
  const point=displayToOriginalPoint(previewX,previewY);

  if(calibrationMode==='scale'){
    scalePoints.push(point);
    if(scalePoints.length===1){
      setVisualStatus('Đã chọn mốc chuẩn 1. Click mốc chuẩn thứ 2.','warn');
    }else if(scalePoints.length===2){
      calculatePixelsPerMeter();
      calibrationMode=null;
      pickScaleBtn.classList.remove('active');
    }
  }else if(calibrationMode==='line'){
    linePoints.push(point);
    if(linePoints.length===1){
      setVisualStatus('Đã chọn điểm đầu đường đo. Click điểm cuối.','warn');
    }else if(linePoints.length===2){
      applyMeasurementLine();
      calibrationMode=null;
      pickLineBtn.classList.remove('active');
    }
  }
  redrawCalibrationCanvas();
});

pickScaleBtn.addEventListener('click',()=>{
  if(!baseFrame){setVisualStatus('Hãy chọn ảnh/video trước.','warn');return;}
  setCalibrationMode('scale');
});

pickLineBtn.addEventListener('click',()=>{
  if(!baseFrame){setVisualStatus('Hãy chọn ảnh/video trước.','warn');return;}
  setCalibrationMode('line');
});

clearVisualBtn.addEventListener('click',()=>{
  scalePoints=[];
  linePoints=[];
  setCalibrationMode(null);
  setVisualStatus('Đã xóa các điểm trực quan. Các giá trị trong ô nhập vẫn được giữ để bạn chỉnh tay.');
  redrawCalibrationCanvas();
});

knownDistance.addEventListener('change',()=>{
  if(scalePoints.length===2)calculatePixelsPerMeter();
});

function handleSelectedFile(){
  const file=mediaInput.files[0];
  fileName.textContent=file?.name||'Chưa chọn file';
  loadVisualPreview(file);
}

mediaInput.addEventListener('change',handleSelectedFile);
['dragenter','dragover'].forEach(ev=>dropzone.addEventListener(ev,e=>{e.preventDefault();dropzone.classList.add('drag')}));
['dragleave','drop'].forEach(ev=>dropzone.addEventListener(ev,e=>{e.preventDefault();dropzone.classList.remove('drag')}));
dropzone.addEventListener('drop',e=>{
  if(e.dataTransfer.files.length){
    try{
      const dt=new DataTransfer();
      dt.items.add(e.dataTransfer.files[0]);
      mediaInput.files=dt.files;
    }catch(_){
      mediaInput.files=e.dataTransfer.files;
    }
    handleSelectedFile();
  }
});

function setBadge(text,kind='neutral'){stateBadge.textContent=text;stateBadge.className='badge '+kind}
function metric(label,value){return `<div class="metric"><span>${label}</span><strong>${value??'-'}</strong></div>`}
function resetResult(){errorBox.textContent='';progressBar.style.width='0%';previewEmpty.hidden=false;resultImage.hidden=true;resultVideo.hidden=true;resultImage.removeAttribute('src');resultVideo.removeAttribute('src');stats.hidden=true;stats.innerHTML='';openResult.hidden=true}
function renderStats(data){
  const s=data.stats||{};
  const mode=(data.mode||s.mode||'-').toUpperCase();
  if(data.media_type==='image'){
    const conf=s.confidence==null?'-':(s.confidence*100).toFixed(1)+'%';
    const level=s.water_level==null?'-':Number(s.water_level).toFixed(2)+' m';
    stats.innerHTML=metric('Mode',mode)+metric('Phát hiện nước',s.water_detected?'CÓ':'KHÔNG')+metric('Confidence',conf)+metric('Mực nước',level)+metric('Trạng thái',s.status||'-');
  }else{
    const max=s.max_water_level==null?'-':Number(s.max_water_level).toFixed(2)+' m';
    stats.innerHTML=metric('Mode',mode)+metric('Frames',s.processed_frames)+metric('Water frames',s.water_detected_frames)+metric('Measured',s.measured_frames)+metric('Alert frames',s.alert_frames)+metric('HIGH',s.high_frames)+metric('Max level',max);
  }
  stats.hidden=false;
}
async function refreshAutoSummary(){
  try{
    const res=await fetch('/config');
    const c=await res.json();
    if(!res.ok)return;
    document.getElementById('profileName').textContent=c.profile_name||'Default';
    document.getElementById('autoLineSummary').textContent=c.auto_line?'Tự động':'Thủ công đã lưu';
    document.getElementById('ppmSummary').textContent=Number(c.pixels_per_meter).toFixed(2);
    document.getElementById('tipSummary').textContent=Number(c.tip_height).toFixed(2)+' m';
    document.getElementById('thresholdSummary').textContent=`${c.low_level} / ${c.medium_level} / ${c.high_level}`;
  }catch(_){ }
}

form.addEventListener('submit',async e=>{
  e.preventDefault();
  if(timer)clearInterval(timer);
  resetResult();
  submitBtn.disabled=true;
  statusText.textContent='Đang tải file lên...';
  setBadge('UPLOADING','processing');
  try{
    const fd=new FormData(form);
    const mode=currentMode();
    fd.set('mode',mode);
    if(mode==='auto'){
      ['profile_name','auto_line','start','end','pixels_per_meter','tip_height','low_level','medium_level','high_level','save_as_auto'].forEach(k=>fd.delete(k));
      fd.set('mode','auto');
    }
    const res=await fetch('/process',{method:'POST',body:fd});
    const data=await res.json();
    if(!res.ok)throw new Error(data.error||'Upload thất bại.');
    if(mode==='manual'&&document.getElementById('saveAsAuto').checked)refreshAutoSummary();
    poll(data.job_id);
  }catch(err){
    submitBtn.disabled=false;statusText.textContent='Thất bại.';setBadge('ERROR','error');errorBox.textContent=err.message;
  }
});

function poll(jobId){
  timer=setInterval(async()=>{
    try{
      const res=await fetch('/status/'+jobId);
      const data=await res.json();
      if(!res.ok)throw new Error(data.error||'Không lấy được trạng thái.');
      const p=data.progress||0;
      progressBar.style.width=p+'%';
      if(data.status==='queued'){statusText.textContent='Đang chờ xử lý...';setBadge('QUEUED','processing')}
      if(data.status==='processing'){
        const extra=data.total_frames?` · ${data.frame||0}/${data.total_frames} frames`:'';
        statusText.textContent=`Đang phân tích ${p}%${extra}`;setBadge('PROCESSING','processing');
      }
      if(data.status==='error'){
        clearInterval(timer);submitBtn.disabled=false;statusText.textContent='Xử lý thất bại.';setBadge('ERROR','error');errorBox.textContent=data.error||'Lỗi không xác định.';
      }
      if(data.status==='done'){
        clearInterval(timer);submitBtn.disabled=false;progressBar.style.width='100%';statusText.textContent='Phân tích hoàn tất.';setBadge('DONE','done');previewEmpty.hidden=true;
        const url=data.result_url+'?v='+Date.now();
        if(data.media_type==='image'){resultImage.src=url;resultImage.hidden=false}else{resultVideo.src=url;resultVideo.hidden=false;resultVideo.load()}
        openResult.href=url;openResult.hidden=false;renderStats(data);refreshAutoSummary();
      }
    }catch(err){clearInterval(timer);submitBtn.disabled=false;setBadge('ERROR','error');errorBox.textContent=err.message}
  },800);
}
