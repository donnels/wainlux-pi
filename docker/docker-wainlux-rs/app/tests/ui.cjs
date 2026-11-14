const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM, VirtualConsole } = require("jsdom");

const html = fs.readFileSync(path.join(__dirname, "../src/ui.html"), "utf8");
const privateDisplay = {masked_fields:["ssid","password","qr-value","card-text","text","comment"],card_content_visible:false,markings_visible:true};
const publicDisplay = {masked_fields:[],card_content_visible:true,markings_visible:false};
const field = (id, type = "text", value = "", extra = {}) => ({id,label:id,type,default:value,editable:true,revealable:true,masked:["text","multiline"].includes(type),options:[],...extra});
const templates = [
  {id:"wifi-login",name:"WiFi login",audio_recording_enabled:true,fields:[field("ssid"),field("password"),field("title","multiline"),field("include-note","checkbox",false,{masked:false}),field("note","multiline")],elements:[{id:"note",side:"front",kind:"text",enabled_by:"include-note",bindings:{"content.text":"note"}}],display:privateDisplay},
  {id:"public-qr",name:"Public QR",audio_recording_enabled:false,fields:[field("public-value","text","",{masked:false})],elements:[],display:publicDisplay},
  {id:"application-card",name:"Application card",audio_recording_enabled:false,fields:[field("locked","multiline","",{editable:false,revealable:false}),field("position","number",10,{masked:false})],elements:[],display:privateDisplay},
  {id:"github-recovery",name:"GitHub recovery",audio_recording_enabled:false,fields:[field("recovery-payload","multiline","",{editable:true,revealable:true,masked:true,file_input:true}),field("login-url","text","https://github.com/login",{masked:true}),field("front-context-y","number",1.75,{masked:false}),field("back-context-y","number",1.75,{masked:false})],elements:[{id:"recovery-qr",side:"front",bindings:{"content.text":"recovery-payload"}},{id:"login-qr",side:"back",bindings:{"content.text":"login-url"}}],display:privateDisplay},
];
const machine = {model:"Simulator",reach_mm:[80,75],settings:[{key:"power",label:"Power",min:0,max:1000,default:50,note:"Synthetic setting"},{key:"depth",label:"Depth",min:0,max:10,default:1,note:"Synthetic setting"}]};
const mode = {mode:"simulator",real:false,real_available:false};
const layout = {reach_mm:[80,75],piece:{x:1,y:10.64,w:85.45,h:53.72},etch:{x:7.45,y:10.64,w:72.55,h:53.72}};
const svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path d="M0 0h10v1H1v8h9v1H0z" fill="black"/></svg>';
const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXi8AAAAASUVORK5CYII=";
const stage = name => ({name,goal:name,burns:[{x_mm:1,y_mm:10,w_mm:30,h_mm:30,width_px:10,height_px:10,svg,png}]});

async function open(backend) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", error => errors.push(error));
  const dom = new JSDOM(html, {
    url:"http://localhost/",runScripts:"dangerously",virtualConsole,
    beforeParse(window) {
      const media = {matches:Boolean(backend.systemDark),addEventListener:(_,listener) => { backend.themeChange = listener; }};
      window.matchMedia = () => media;
      backend.media = media;
      if (backend.standalone) Object.defineProperty(window.navigator,"standalone",{configurable:true,value:true});
      if (backend.savedTheme) window.localStorage.setItem("laser_theme", backend.savedTheme);
      window.setInterval = () => 0;
      window.structuredClone = structuredClone;
      window.TextDecoder = TextDecoder;
      window.alert = value => { throw new Error(value); };
      window.confirm = () => true;
      window.AudioContext = class {
        constructor() { this.currentTime = 0; this.destination = {}; }
        resume() { return Promise.resolve(); }
        createOscillator() { return {frequency:{value:0},connect(){},start(){backend.audioTones=(backend.audioTones||0)+1;},stop(){}}; }
        createGain() { return {gain:{setValueAtTime(){},linearRampToValueAtTime(){},exponentialRampToValueAtTime(){}},connect(){}}; }
        close() { return Promise.resolve(); }
      };
      window.fetch = async (url, options) => {
        (backend.requests ||= []).push(url);
        const body = options?.body ? (typeof options.body === "string" ? JSON.parse(options.body) : options.body) : null;
        let value;
        switch (url) {
          case "/api/steps": value = {machine,defaults:{text:"",size_mm:20,x_mm:0,y_mm:0},steps:[
            {step:"frame",title:"Frame",controls:["x_mm","y_mm","size_mm"],settings:[],materials:false},
            {step:"qr",title:"QR",controls:["text","x_mm","y_mm","size_mm"],settings:["power","depth"],materials:true},
            {step:"talk",title:"Talk",controls:[],settings:[],materials:false},
            {step:"power_sweep",title:"Power sweep",controls:["x_mm","size_mm"],settings:["power","depth"],materials:true,fixed_y_mm:0,power_values:[1000,889,778,667,556,444,333,222,111,0]},
            {step:"audio_power_sweep_low",title:"Audio compare: minimum power",controls:["x_mm"],settings:["power","depth"],materials:true,requires_real:true,requires_analyzer:true,requires_sync_marker:true,requires_recording_ack:true,fixed_y_mm:0,fixed_size_mm:19,power_values:[1]},
            {step:"audio_power_sweep_full",title:"Audio compare: full power",controls:["x_mm"],settings:["power","depth"],materials:true,requires_real:true,requires_analyzer:true,requires_sync_marker:true,requires_recording_ack:true,fixed_y_mm:0,fixed_size_mm:19,power_values:[1000]}
          ]}; break;
          case "/api/audio-analyses/availability": value = {available:backend.audioAnalyzerAvailable !== false}; break;
          case "/api/audio-analyses/analysis-9/report":
            value = {event:"audio_analysis",attempt_id:"analysis-9",has_audio:false,report_url:"/api/audio-analyses/analysis-9/report",timing:{analyzer_total_ms:10}};
            backend.logEntries.push(value); break;
          case "/api/plan":
            backend.plans ||= []; backend.plans.push(body);
            value = backend.lastPlan = body?.review_id
              ? {id:`1794304104-${backend.review?.stages?.[body.stage]?.name || "content"}`,review_id:body.review_id,stage_name:backend.review?.stages?.[body.stage]?.name || "content",goal:"Synthetic card stage"}
              : {id:`1794304104-${body.step}`,step:body.step,goal:"Synthetic short frame"};
            break;
          case "/api/run":
            backend.runs ||= []; backend.runs.push(body);
            if (body?.sync_marker) backend.logEntries = [
              {event:"sync_marker",attempt_id:body.attempt_id,recording_id:body.recording_id},
              {event:"ran",attempt:{id:body.attempt_id,mode:"real",step:backend.lastPlan?.step,review_id:backend.lastPlan?.review_id},outcome:{ok:true}},
            ];
            value = {running:true,attempt_id:body?.attempt_id || "attempt-sync",sync_marker:Boolean(body?.sync_marker),recording_id:body?.recording_id}; break;
          case "/api/stop": backend.stopCalls = (backend.stopCalls || 0) + 1; value = {sent:true}; break;
          case "/api/verdict": {
            backend.verdicts ||= []; backend.verdicts.push(body);
            if (body.status === "ok" && backend.state.outcome?.ok && backend.review) {
              backend.review.accepted_stages ||= []; backend.review.accepted_stages.push(backend.state.attempt.stage || 0);
              backend.state.accepted_stages = backend.review.accepted_stages;
            }
            backend.state = {...backend.state,attempt:null,running:false,outcome:null};
            value = {status:body.status}; break;
          }
          case "/api/audio-analyses": backend.audioUploads = (backend.audioUploads || 0) + 1; backend.audioUploadHeaders = options.headers; value = {report_url:"/api/audio-analyses/1794304104-audio_power_sweep_low/report",timing:{pi_receive_ms:11,multipart_parse_ms:2,pi_prepare_ms:3,persistence_ms:4,analysis_process_ms:50,report_read_ms:6,analyzer_http_ms:60,analyzer_total_ms:58,pi_total_ms:76,request_total_ms:72,analysis:{probe_ms:1,decode_ms:12,sync_correlation_ms:13,playback_encode_ms:14,analysis_compute_ms:40,report_write_ms:5}}}; break;
          case "/api/tests/preview":
            backend.testPreviews ||= [];
            backend.testPreviews.push(body);
            value = body.step === "talk"
              ? {goal:"Simulator answers; nothing moves.",svg:null,note:"This step has no raster pattern."}
              : {goal:`${body.step} expected pattern at ${body.params.x_mm},${body.params.y_mm} mm`,svg,note:"Synthetic pattern preview only; not a burn result."};
            break;
          case "/api/materials": value = [
            {material:{id:"bamboo-ntag215-light",name:"Card",role:"piece",size_mm:[85.45,53.72],note:"Synthetic card note"},values:{power:1000,depth:10}},
            {material:{id:"backing-first",name:"Backing",role:"backing",size_mm:[80,75],note:"Backing test: 500/10 marked QR; line sweep suggests lower rows may not show."},values:{power:500,depth:10}},
          ]; break;
          case "/api/templates": value = templates; break;
          case "/api/log": value = backend.logEntries || []; break;
          case "/api/state":
            if (options?.method === "DELETE") {
              backend.clearCalls = (backend.clearCalls || 0) + 1;
              backend.review = null;
              backend.state = {...backend.state,attempt:null,running:false,outcome:null,progress:null,review_id:null,accepted_stages:null};
              value = {cleared:true,run_log_retained:true};
            } else value = backend.state;
            break;
          case "/api/jobs/review":
            if (options?.method === "DELETE") { backend.review = null; backend.state.review_id = null; value = {discarded:true}; }
            else value = backend.review;
            break;
          case "/api/jobs/form": {
            const template = templates.find(template => template.id === body.template);
            const values = backend.review?.fields || Object.fromEntries(template.fields.map(field => [field.id,{value:field.default,provided:false}]));
            for (const [id,value] of Object.entries(body.fields)) values[id] = {value,provided:true};
            backend.lastChanges = body.fields;
            const stages = template.id === "github-recovery" ? [{...stage("front"),side:"front"},{...stage("back"),side:"back"}] : [stage("guide"),stage("content")];
            backend.review = {review_id:"review-" + (backend.revision = (backend.revision || 0) + 1),template,fields:values,job:{template:body.template,piece:body.piece,backing:body.backing,placement:body.placement,guide:body.guide,items:[],values:{}},layout,display:template.display,stages};
            backend.state.review_id = backend.review.review_id;
            value = backend.review; break;
          }
          case "/api/attempt/preview": value = {attempt_id:backend.state.attempt.id,layout,display:privateDisplay,stages:[stage(backend.activeStage)]}; break;
          case "/api/mode": backend.state.mode = {...backend.state.mode,real:body.real,mode:body.real?"real":"simulator"}; value = backend.state.mode; break;
          default: throw new Error("Unexpected request: " + url);
        }
        return {ok:true,json:async () => structuredClone(value)};
      };
    },
  });
  for (let turn = 0; turn < 100 && dom.window.document.getElementById("status").textContent === "Connecting..."; turn++) {
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.deepEqual(errors, []);
  assert(!dom.window.document.getElementById("status").textContent.startsWith("Connecting"), dom.window.document.getElementById("status").textContent + " requests=" + (backend.requests||[]).join(",") + " " + errors.map(error=>error.message).join("; "));
  return dom;
}

async function main() {
  const backend = {state:{attempt:null,running:false,outcome:null,progress:null,mode}};
  const dom = await open(backend);
  const window = dom.window, document = window.document;
  const element = id => document.getElementById(id);
  const toggle = id => document.querySelector(`button[aria-controls="${id}"]`);
  assert.equal(document.documentElement.dataset.themePreference, "system");
  assert.equal(document.documentElement.dataset.theme, "light");
  assert.equal(document.documentElement.dataset.previewPolarity, "dark");
  assert.equal(document.querySelector('link[rel="manifest"]').getAttribute("href"),"/manifest.webmanifest");
  assert.equal(element("fullscreen").textContent,"Full screen");
  let fullscreenEntries = 0, fullscreenExits = 0;
  Object.defineProperty(document,"fullscreenElement",{configurable:true,value:null});
  document.documentElement.requestFullscreen = async options => {
    assert.equal(options.navigationUI,"hide"); fullscreenEntries++;
    Object.defineProperty(document,"fullscreenElement",{configurable:true,value:document.documentElement});
    document.dispatchEvent(new window.Event("fullscreenchange"));
  };
  document.exitFullscreen = async () => {
    fullscreenExits++;
    Object.defineProperty(document,"fullscreenElement",{configurable:true,value:null});
    document.dispatchEvent(new window.Event("fullscreenchange"));
  };
  await window.toggleFullscreen();
  assert.equal(fullscreenEntries,1);
  assert.equal(element("fullscreen").textContent,"Exit full screen");
  assert.equal(element("stop").hidden,false,"STOP remains available in fullscreen");
  await window.toggleFullscreen();
  assert.equal(fullscreenExits,1);
  assert.equal(element("fullscreen").textContent,"Full screen");
  const standalonePage = await open({standalone:true,state:{attempt:null,running:false,outcome:null,progress:null,mode}});
  assert.equal(standalonePage.window.document.getElementById("fullscreen").textContent,"Full-screen app");
  assert.equal(standalonePage.window.document.getElementById("fullscreen").disabled,true);
  assert.equal(standalonePage.window.document.getElementById("stop").hidden,false);
  standalonePage.window.close();
  backend.logEntries = Array.from({length:12},(_,index)=>({event:"ran",attempt:{id:`analysis-${index}`,mode:"real",stage_name:"content"},outcome:{ok:true,elapsed_ms:1000+index*100,band_timings:[{transfer_ms:100,setup_ms:200,wait_ms:300,phase:"reported_complete"}]}}));
  backend.logEntries.push({event:"audio_analysis",attempt_id:"analysis-11",has_audio:false,report_url:"/api/audio-analyses/analysis-11/older-report",timing:{analyzer_total_ms:10}});
  backend.logEntries.push({event:"audio_analysis",attempt_id:"analysis-11",has_audio:true,report_url:"/api/audio-analyses/analysis-11/report",timing:{pi_total_ms:900,analysis_process_ms:600}});
  backend.logEntries.push({event:"audio_analysis",attempt_id:"analysis-10",has_audio:false,report_url:"/api/audio-analyses/analysis-10/report",timing:{analyzer_total_ms:10}});
  window.tab("analysis");
  await window.loadAnalysis();
  assert.equal(document.querySelectorAll("#analysis-runs button").length,10,"analysis view is capped at the latest ten runs");
  assert(document.querySelector("#analysis-runs button").textContent.includes("analysis-11"),"most recent run is first");
  assert(document.getElementById("analysis-chart").getAttribute("aria-label").includes("transfer 0.10 s"));
  assert.equal(document.getElementById("analysis-audio").hidden,false);
  assert.equal(document.getElementById("analysis-report-link").href,"http://localhost/api/audio-analyses/analysis-11/report");
  const analysisReportDetails=document.getElementById("analysis-report-details");
  assert.equal(analysisReportDetails.hidden,false);
  analysisReportDetails.open=true; analysisReportDetails.dispatchEvent(new window.Event("toggle"));
  assert.equal(document.getElementById("analysis-report-frame").src,"http://localhost/api/audio-analyses/analysis-11/report");
  document.querySelectorAll("#analysis-runs button")[1].click();
  assert.equal(document.getElementById("analysis-report-link").href,"http://localhost/api/audio-analyses/analysis-10/report");
  assert.equal(document.getElementById("analysis-generate").hidden,true,"log-only reports are usable without an upload");
  document.querySelectorAll("#analysis-runs button")[2].click();
  assert.equal(document.getElementById("analysis-generate").hidden,false);
  await window.generateAnalysis();
  assert.equal(document.getElementById("analysis-report-link").href,"http://localhost/api/audio-analyses/analysis-9/report");
  window.tab("cards");
  assert.equal(element("step").value, "frame");
  assert.equal(element("layout-controls").open,false,"numeric layout controls are secondary, not the content form");
  element("tab-cards").dispatchEvent(new window.KeyboardEvent("keydown",{key:"ArrowRight",bubbles:true}));
  assert.equal(element("tests").hidden,false);
  assert.equal(document.activeElement,element("tab-tests"));
  element("tab-tests").dispatchEvent(new window.KeyboardEvent("keydown",{key:"Home",bubbles:true}));
  assert.equal(element("cards").hidden,false);
  assert(element("test-preview-goal").textContent.includes("frame expected pattern"));
  assert.equal(element("test-pattern-image").hidden, false);
  element("step").value = "talk";
  element("step").dispatchEvent(new window.Event("change", {bubbles:true}));
  await new Promise(resolve => setTimeout(resolve, 220));
  assert.equal(element("test-pattern-image").hidden, true);
  assert.equal(element("test-pattern-empty").hidden, false);
  assert(element("test-preview-goal").textContent.includes("Simulator answers"));
  element("step").value = "qr";
  element("step").dispatchEvent(new window.Event("change", {bubbles:true}));
  element("text").value = "SYNTHETIC PREVIEW ONLY";
  element("text").dispatchEvent(new window.Event("input", {bubbles:true}));
  await new Promise(resolve => setTimeout(resolve, 220));
  assert.equal(backend.testPreviews.at(-1).params.text,"SYNTHETIC PREVIEW ONLY");
  assert.equal(element("test-pattern-image").hidden,false);
  assert(element("test-preview-note").textContent.includes("not a burn result"));
  element("x_mm").value = 5; element("y_mm").value = 8;
  element("step").value = "power_sweep";
  element("step").dispatchEvent(new window.Event("change", {bubbles:true}));
  await new Promise(resolve => setTimeout(resolve, 220));
  assert.equal(element("x_mm").disabled, false); assert.equal(element("y_mm").disabled, true);
  assert.equal(element("x_mm").value, "20"); assert.equal(element("y_mm").value, "0");
  assert.equal(backend.testPreviews.at(-1).params.x_mm, 20); assert.equal(backend.testPreviews.at(-1).params.y_mm, 0);
  assert.equal(element("power_sweep_index"), null, "the whole sweep is submitted as one attempt");
  element("test-material").value = "backing-first";
  element("test-material").dispatchEvent(new window.Event("change", {bubbles:true}));
  await new Promise(resolve => setTimeout(resolve, 220));
  assert.equal(element("test-material-note").hidden, false);
  assert(element("test-material-note").textContent.includes("lower rows may not show"));
  assert.equal(backend.testPreviews.at(-1).material, "backing-first");
  assert.equal(backend.testPreviews.at(-1).params.values.power, 1000);
  assert.equal(backend.testPreviews.at(-1).params.values.depth, 10);
  element("step").value = "qr";
  element("step").dispatchEvent(new window.Event("change", {bubbles:true}));
  await new Promise(resolve => setTimeout(resolve, 220));
  assert.equal(element("x_mm").disabled, false); assert.equal(element("y_mm").disabled, false);
  backend.state = {attempt:{goal:"synthetic trace attempt"},running:false,outcome:{ok:true,saw:"complete",elapsed_ms:112000,band_timings:[{band:1,bands:1,row_start:0,row_end:100,transfer_ms:12000,setup_ms:5000,wait_ms:90000,elapsed_ms:107000,phase:"reported_complete"}],receive_trace:{bytes_captured:4,truncated:false,duration_ms:2,events:[{at_ms:1,job_index:2,phase:"completion_wait",kind:"rx_byte",byte:"FF",quiet_ms:0}]}},progress:null,mode};
  await window.poll();
  assert.equal(element("receive-trace-details").hidden,false);
  assert.equal(element("timing-panel").hidden,false);
  assert.equal(element("time-total").textContent,"1m 52.00s");
  assert.equal(element("time-transfer").textContent,"12.00 s");
  assert.equal(element("time-setup").textContent,"5.00 s");
  assert.equal(element("time-wait").textContent,"1m 30.00s");
  assert.equal(window.msText(1964672),"32m 44.67s");
  assert.equal(window.seconds(1943207),"32m 23.2s");
  assert.equal(element("time-other").textContent,"5.00 s");
  assert.equal(element("timing-example-frame").hasAttribute("src"),false,"real report example is not downloaded by default");
  element("timing-example").open = true;
  element("timing-example").dispatchEvent(new window.Event("toggle"));
  assert.equal(element("timing-example-frame").getAttribute("src"),"/examples/audio-analysis");
  assert(element("receive-trace").textContent.includes('"job_index": 2'));
  assert(element("receive-trace").textContent.includes('"byte": "FF"'));
  backend.state = {attempt:null,running:false,outcome:null,progress:null,mode:{mode:"real",real:true,real_available:true}};
  await window.poll();
  assert.equal(element("audio-sync-cue-control").hidden,false);
  assert.equal(element("audio-sync-cue").checked,true);
  element("step").value = "frame"; element("size_mm").value = 2;
  await window.plan();
  assert.equal(element("audio-recording-ack").hidden,true,"machine tests without recording policy do not show the optional recording control");
  await window.run();
  assert.equal(backend.runs.at(-1).sync_marker,true);
  assert.equal(backend.runs.at(-1).attempt_id,backend.lastPlan.id,"start is bound to the displayed plan");
  assert.equal(backend.runs.at(-1).recording_id,"304104");
  assert.equal(backend.runs.at(-1).cue_duration_ms,2000);
  assert.equal(backend.runs.at(-1).post_cue_lead_ms,700);
  assert.equal(backend.audioTones,15);
  await window.plan();
  element("audio-sync-cue").checked = false;
  element("audio-recording-started").checked = true;
  await window.run();
  assert.equal(backend.runs.at(-1).sync_marker,true,"Recording confirmation forces a matching sync cue even when the optional cue control is off");
  assert.equal(window.sessionStorage.getItem("laser_audio_recording_attempt_id"),backend.runs.at(-1).attempt_id);
  backend.state = {attempt:{id:backend.runs.at(-1).attempt_id,step:"frame",goal:"Synthetic recorded card"},running:false,outcome:{ok:true,saw:"complete"},progress:null,mode:{mode:"real",real:true,real_available:true}};
  await window.poll();
  assert.equal(element("audio-upload-panel").hidden,false,"A recorded card run can be uploaded after completion");
  assert.equal(element("audio-phone-upload").hidden,false,"the iPhone upload QR is available after the marked card run");
  assert(element("audio-phone-upload-link").href.includes(`/audio-upload/${backend.runs.at(-1).attempt_id}`));
  assert(element("audio-phone-upload-qr").src.endsWith(`/api/audio-analyses/${backend.runs.at(-1).attempt_id}/upload-qr.svg`));
  backend.state = {attempt:null,running:false,outcome:null,progress:null,mode:{mode:"real",real:true,real_available:true}};
  await window.poll();
  await window.plan(); element("audio-sync-cue").checked = false; element("audio-recording-started").checked = false; await window.run();
  assert.equal(Boolean(backend.runs.at(-1).sync_marker),false);
  assert.equal(backend.audioTones,30,"disabled cue does not play audio when recording is not confirmed");
  element("step").value = "audio_power_sweep_low";
  element("step").dispatchEvent(new window.Event("change", {bubbles:true}));
  await new Promise(resolve => setTimeout(resolve,220));
  assert.equal(element("y_mm").value,"0");
  assert.equal(element("size_mm").value,"19");
  assert.equal(element("size_mm").disabled,true);
  assert.equal(element("set-power").value,"1");
  assert.equal(element("set-power").disabled,true);
  assert.equal(backend.testPreviews.at(-1).params.size_mm,19);
  await window.plan();
  assert.equal(backend.plans.at(-1).params.size_mm,19);
  assert.equal(element("audio-recording-ack").hidden,false);
  assert.equal(window.attemptRequires("requires_analyzer",{review_id:"synthetic-card"}),false,"card plans never inherit the selected audio test's requirements");
  const runsBeforeAudioTest = backend.runs.length;
  await window.run();
  assert.equal(backend.runs.length,runsBeforeAudioTest,"audio test waits for explicit recording confirmation");
  element("audio-recording-started").checked = true;
  await window.run();
  assert.equal(backend.runs.at(-1).sync_marker,true,"audio test always plays its recording ID cue");
  assert.equal(backend.audioTones,30);
  backend.state = {attempt:{id:"1794304104-audio_power_sweep_low",step:"audio_power_sweep_low",goal:"Synthetic audio sweep"},running:false,outcome:{ok:true,saw:"completed"},progress:null,mode:{mode:"real",real:true,real_available:true}};
  await window.poll();
  assert.equal(element("audio-upload-panel").hidden,false,"upload is shown only after a completed attempt");
  const memo = new window.File(["synthetic audio"],"voice.m4a",{type:"audio/mp4"});
  Object.defineProperty(element("audio-recording-file"),"files",{configurable:true,value:[memo]});
  element("audio-recording-file").dispatchEvent(new window.Event("change"));
  assert.equal(element("audio-upload-button").disabled,false);
  await window.uploadAudio();
  assert.equal(backend.audioUploads,1);
  assert(element("audio-upload-status").textContent.includes("Upload and analysis response:"));
  assert.equal(element("audio-timing").hidden,false);
  assert.equal([...element("audio-timing").querySelectorAll("dt")].some(term=>term.textContent==="Pi upload receive"),true);
  assert.equal([...element("audio-analysis-timing").querySelectorAll("dt")].some(term=>term.textContent==="Sync/log correlation"),true);
  element("audio-report").dispatchEvent(new window.Event("load"));
  assert.equal([...element("audio-timing").querySelectorAll("dt")].some(term=>term.textContent==="Browser report download"),true);
  assert.equal(backend.audioUploadHeaders["X-Attempt-ID"],"1794304104-audio_power_sweep_low");
  assert.equal(element("audio-report").src,"http://localhost/api/audio-analyses/1794304104-audio_power_sweep_low/report");
  backend.logEntries = Array.from({length:12},(_,index)=>({event:"ran",attempt:{id:`comparison-${index}`,mode:"real",step:"audio_power_sweep_full",goal:`Power ${index*100}, Depth 10`},outcome:{ok:true,elapsed_ms:1000+index*100,band_timings:[{transfer_ms:100,setup_ms:200,wait_ms:300,phase:"reported_complete"}]}}));
  backend.logEntries.push(
    {event:"audio_analysis",attempt_id:"comparison-11",report_url:"/api/audio-analyses/comparison-11/report",timing:{pi_total_ms:900,analysis_process_ms:600}},
    {event:"audio_analysis",attempt_id:"comparison-10",report_url:"/api/audio-analyses/comparison-10/report",timing:{pi_total_ms:950,analysis_process_ms:650}},
  );
  window.tab("analysis"); await window.loadAnalysis();
  assert.equal(element("analysis-runs").querySelectorAll("button").length,10);
  const compareBoxes = element("analysis-runs").querySelectorAll('input[type="checkbox"]');
  compareBoxes[0].checked = true; compareBoxes[0].dispatchEvent(new window.Event("change"));
  compareBoxes[1].checked = true; compareBoxes[1].dispatchEvent(new window.Event("change"));
  await window.compareAnalysisRuns();
  assert.equal(element("analysis-comparison-body").querySelectorAll("tr").length,2);
  assert.equal(element("analysis-comparison-body").querySelectorAll(".analysis-bar").length,2);
  assert.equal([...element("analysis-comparison-body").querySelectorAll("a")].length,1,"uploaded waveform report is linked for the matching run only");
  assert.equal(element("analysis-compare-waveforms").hidden,false);
  window.compareAnalysisWaveforms();
  assert.equal(element("analysis-waveforms").querySelectorAll("iframe").length,2);
  const observation = element("comment");
  assert.equal(observation.hidden,false,"inspection feedback is always visible");
  assert.equal(observation.hasAttribute("data-secret"),false,"operator feedback is not treated as a credential");
  observation.value = "Bottom marker measured 0.4 mm short.";
  await window.verdict("deviation");
  assert.equal(backend.verdicts.at(-1).comment,"Bottom marker measured 0.4 mm short.","operator can enter and submit inspection feedback");
    const bandWait = window.progressText({phase:"waiting",percent:97,band:2,bands:5,row_start:500,row_end:620,previous_assumed:true});
    assert(bandWait.includes("Band 2 of 5") && bandWait.includes("raster rows 501-620"));
    assert(bandWait.includes("97% of this band only") && bandWait.includes("not the whole card") && bandWait.includes("90 seconds"));
    const bandStart = window.progressText({phase:"starting",percent:null,band:3,bands:5,row_start:700,row_end:810,previous_assumed:true});
    assert(bandStart.toLowerCase().includes("previous band was assumed complete"));
    assert(window.bandTimingText([{band:2,bands:5,row_start:500,row_end:620,transfer_ms:12000,setup_ms:4900,wait_ms:90000,phase:"assumed_complete"}]).includes("transfer 12.0s, setup 4.9s, laser wait 90.0s; completion assumed"));
  assert.equal(document.querySelector("footer").textContent.trim(), "Copyright (c) 2026 Sean Donnellan");
  assert.equal(document.querySelector('meta[name="author"]').content, "Sean Donnellan");
  element("theme").value = "dark";
  element("theme").dispatchEvent(new window.Event("change"));
  assert.equal(document.documentElement.dataset.theme, "dark");
  assert.equal(window.localStorage.getItem("laser_theme"), "dark");
  backend.media.matches = false; backend.themeChange();
  assert.equal(document.documentElement.dataset.theme, "dark", "explicit dark ignores system changes");
  window.setTheme("system"); backend.media.matches = true; backend.themeChange();
  assert.equal(document.documentElement.dataset.theme, "dark");
  backend.media.matches = false; backend.themeChange();
  assert.equal(document.documentElement.dataset.theme, "light");
  assert.equal(element("template-fields").children.length, 0, "load type before showing fields");
  assert.equal(element("preview-card").disabled, true);
  window.renderTemplate(templates[0]); window.controls();
  assert.equal(element("field-public-value"), null, "undefined elements never appear");
  for (const [id,value] of [["ssid","Synthetic network"],["password","Synthetic password"],["title","TOP\nBOTTOM"]]) {
    element("field-" + id).value = value;
    element("field-" + id).dispatchEvent(new window.Event("input", {bubbles:true}));
  }
  for (const id of ["field-ssid","field-password","text"]) assert.equal(element(id).type, "password");
  assert.equal(element("field-title").hidden, true);
  toggle("field-ssid").click(); assert.equal(element("field-ssid").type, "text");
  toggle("field-ssid").click(); assert.equal(element("field-ssid").type, "password");
  const titleField = element("field-title");
  toggle("field-title").click(); assert.equal(element("field-title").hidden, false);
  titleField.value = "Operator-entered card title";
  titleField.dispatchEvent(new window.Event("input", {bubbles:true}));
  toggle("field-title").click(); assert.equal(element("field-title").value, "Operator-entered card title");
  assert.equal(titleField.value,"Operator-entered card title","hiding a masked editable field preserves its draft");
  assert.equal(element("field-title").hidden, true);
  const includeNote = element("field-include-note"), noteField = element("field-note");
  assert.equal(noteField.closest("label").hidden,true,"disabled optional content is hidden");
  includeNote.checked = true; includeNote.dispatchEvent(new window.Event("input", {bubbles:true}));
  assert.equal(noteField.closest("label").hidden,false,"enabling optional content exposes its field");
  toggle("field-note").click();
  noteField.value = "Operator-entered location";
  noteField.dispatchEvent(new window.Event("input", {bubbles:true}));
  includeNote.checked = false; includeNote.dispatchEvent(new window.Event("input", {bubbles:true}));
  assert.equal(noteField.closest("label").hidden,true);
  includeNote.checked = true; includeNote.dispatchEvent(new window.Event("input", {bubbles:true}));
  assert.equal(noteField.value,"Operator-entered location","reenabling optional content restores its editable value");
  assert.equal(noteField.hidden,false,"the optional text is still editable after being reenabled");
  await window.prepareCard();
  assert(element("card-inputs").parentElement.contains(element("preview-card")),"review submits from the content editor");
  assert.equal(element("laser-output").hidden, false);
  const outputContent = document.querySelector('.output-image[data-preview-layer="content"]');
  const outputGuide = document.querySelector('.output-image[data-preview-layer="guide"]');
  assert.equal(outputContent.getAttribute("src"), png);
  assert.equal(outputContent.style.display, "none");
  assert.equal(outputGuide.style.display, "");
  const content = document.querySelector('[data-preview-layer="content"]');
  const guide = document.querySelector('[data-preview-layer="guide"]');
  assert.equal(content.style.display, "none"); assert.equal(guide.style.display, "");
  element("show-card").click(); assert.equal(content.style.display, "");
  assert.equal(outputContent.style.display, "");
  window.setTheme("dark");
  assert.equal(content.style.display, "", "theme changes do not change privacy visibility");
  assert.equal(document.documentElement.dataset.previewPolarity, "dark", "dark theme does not invert burn marks");
  const originalPng = outputContent.getAttribute("src");
  const originalSvg = content.getAttribute("href");
  element("preview-polarity").value = "light";
  element("preview-polarity").dispatchEvent(new window.Event("change"));
  assert.equal(document.documentElement.dataset.previewPolarity, "light");
  assert.equal(outputContent.getAttribute("src"), originalPng, "polarity does not change raster pixels");
  assert.equal(content.getAttribute("href"), originalSvg);
  assert.equal(outputContent.style.display, "");
  assert.equal(outputGuide.style.display, "");
  window.setTheme("light");
  assert.equal(document.documentElement.dataset.previewPolarity, "light");
  window.setPreviewPolarity("dark");
  assert.equal(document.documentElement.dataset.previewPolarity, "dark");
  const rasterMarkup = decodeURIComponent(content.getAttribute("href").split(",").slice(1).join(","));
  assert(!rasterMarkup.includes('fill="white"'), "preview has no bright raster background");
  element("show-card").click(); assert.equal(content.style.display, "none");
  assert.equal(outputContent.style.display, "none");
  element("show-markings").click(); assert.equal(guide.style.display, "none");
  element("show-markings").click(); assert.equal(guide.style.display, "");
  element("template").value = "public-qr";
  element("template").dispatchEvent(new window.Event("change", {bubbles:true}));
  await new Promise(setImmediate);
  assert.equal(element("field-ssid"), null); assert.equal(element("field-password"), null);
  element("field-public-value").value = "Public sample";
  element("field-public-value").dispatchEvent(new window.Event("input", {bubbles:true}));
  await window.prepareCard();
  assert.equal(document.querySelector('[data-preview-layer="content"]').style.display, "");
  assert.equal(document.querySelector('[data-preview-layer="guide"]').style.display, "none");
  const received = await open(backend);
  assert.equal(received.window.document.getElementById("field-public-value").value, "Public sample");
  assert.equal(received.window.document.getElementById("laser-output").hidden, false);
  received.window.close();
  element("field-public-value").value = "Unsaved change";
  element("field-public-value").dispatchEvent(new window.Event("input", {bubbles:true}));
  await window.poll();
  assert.equal(element("field-public-value").value, "Unsaved change", "polling must not restore the older review over edits");
  dom.window.close();

  backend.state = {attempt:{id:"retained-guide",job:{items:[]},goal:"Guide"},running:false,outcome:{ok:true,saw:"assumed complete"},progress:99,mode};
  backend.activeStage = "guide";
  const guidePage = await open(backend);
  assert.equal(guidePage.window.document.querySelector('[data-preview-layer="guide"]').style.display, "");
  assert.equal(guidePage.window.document.getElementById("show-card").disabled, true);
  guidePage.window.close();
  backend.state.attempt.id = "retained-content"; backend.activeStage = "content";
  const contentPage = await open(backend);
  assert.equal(contentPage.window.document.querySelector('[data-preview-layer="content"]').style.display, "none");
  contentPage.window.close();
  backend.state = {attempt:null,running:false,outcome:null,progress:null,mode,review_id:"locked-review"};
  backend.review = {review_id:"locked-review",template:templates[2],fields:{locked:{provided:true,value:null},position:{provided:true,value:10}},job:{template:"application-card",piece:"bamboo-ntag215-light",backing:"backing-first",placement:"left",guide:true,items:[],values:{}},layout,display:privateDisplay,stages:[stage("guide"),stage("content")]};
  const restricted = await open(backend);
  const restrictedDoc = restricted.window.document;
  assert.equal(restrictedDoc.getElementById("field-ssid"), null);
  assert.equal(restrictedDoc.getElementById("field-locked").value, "");
  assert.equal(restrictedDoc.getElementById("field-locked").disabled, true);
  assert(restrictedDoc.getElementById("layout-fields").contains(restrictedDoc.getElementById("field-position")));
  assert.equal(restrictedDoc.getElementById("layout-controls").open,false);
  assert.equal(restrictedDoc.querySelector('button[aria-controls="field-locked"]').hidden, true);
  restrictedDoc.getElementById("show-card").click();
  assert.equal(restrictedDoc.querySelector('.output-image[data-preview-layer="content"]').style.display, "");
  await restricted.window.prepareCard();
  assert.deepEqual(backend.lastChanges, {}, "restricted source values are not copied back by the browser");
  restricted.window.close();
  backend.savedTheme = "dark";
  const savedThemePage = await open(backend);
  assert.equal(savedThemePage.window.document.documentElement.dataset.theme, "dark");
  assert.equal(savedThemePage.window.document.documentElement.dataset.previewPolarity, "dark", "saved dark theme still defaults to dark burn marks");
  assert.equal(savedThemePage.window.document.getElementById("theme").value, "dark");
  savedThemePage.window.close();
  const liveMode = {mode:"simulator",real:false,real_available:true};
  backend.state = {attempt:{id:"stale-live-attempt",review_id:"mode-review",job:{items:[]},goal:"Old attempt"},running:false,outcome:null,progress:null,mode:liveMode};
  backend.activeStage = "guide";
  backend.review = {review_id:"mode-review",template:templates[2],fields:{locked:{provided:false,value:null},position:{provided:true,value:10}},job:{template:"application-card",piece:"bamboo-ntag215-light",backing:"backing-first",placement:"left",guide:false,items:[],values:{}},layout,display:privateDisplay,stages:[stage("content")]};
  const modePage = await open(backend); const modeDoc = modePage.window.document;
  assert.equal(modeDoc.getElementById("mode").disabled,true,"active review freezes mode");
  backend.state.attempt = null;
  await modePage.window.poll();
  assert.equal(modeDoc.getElementById("mode").disabled,false,"idle API state clears stale browser freeze");
  modeDoc.getElementById("mode").value = "real";
  modeDoc.getElementById("mode").dispatchEvent(new modePage.window.Event("change"));
  for (let turn=0;turn<20 && modeDoc.getElementById("mode").disabled;turn++) await new Promise(resolve=>setTimeout(resolve,5));
  assert.equal(backend.state.mode.real,true,"user mode switch reaches the API after idle clears");
  modePage.window.close();
  const reuseBackend = {state:{attempt:null,running:false,outcome:null,progress:null,mode,review_id:"reuse-review"}};
  reuseBackend.review = {review_id:"reuse-review",template:templates[2],fields:{locked:{provided:false,value:null},position:{provided:true,value:10}},job:{template:"application-card",piece:"bamboo-ntag215-light",backing:"backing-first",placement:"left",guide:false,items:[],values:{}},layout,display:privateDisplay,stages:[stage("content")]};
  const reusePage = await open(reuseBackend);
  const reuseDoc = reusePage.window.document;
  assert.equal(reuseDoc.getElementById("burn-guide").disabled, true);
  assert.equal(reuseDoc.getElementById("placement-check").hidden, false, `reuse gate: ${reuseDoc.getElementById("status").textContent}; stages=${[...reuseDoc.querySelectorAll(".output-stage h3")].map(node=>node.textContent).join(",")}; guide=${backend.review.job.guide}`);
  assert.equal(reuseDoc.getElementById("burn-card").disabled, true);
  reuseDoc.getElementById("card-placed").checked = true;
  reuseDoc.getElementById("card-placed").dispatchEvent(new reusePage.window.Event("change"));
  assert.equal(reuseDoc.getElementById("burn-card").disabled, false, "existing guide can be reused after explicit placement confirmation");
  reusePage.window.close();
  backend.state = {attempt:null,running:false,outcome:null,progress:null,mode,review_id:"two-sides",accepted_stages:[]};
  backend.review = {review_id:"two-sides",accepted_stages:[],template:{...templates[2],elements:[{id:"front-item",side:"front",bindings:{"content.text":"locked"}},{id:"back-item",side:"back",bindings:{"content.text":"position"}}]},fields:{locked:{provided:true,value:null},position:{provided:true,value:10}},job:{template:"application-card",piece:"bamboo-ntag215-light",backing:"backing-first",placement:"left",guide:false,items:[],values:{}},layout,display:privateDisplay,stages:[{...stage("front"),side:"front"},{...stage("back"),side:"back"}]};
  const sidesPage = await open(backend); const sidesDoc = sidesPage.window.document;
  assert.equal(sidesDoc.getElementById("side-control").hidden, false);
  assert.equal(sidesDoc.querySelectorAll(".output-stage").length, 1, "front and back rasters never overlay");
  sidesPage.window.selectCardSide("back");
  sidesDoc.getElementById("card-placed").checked = true; sidesPage.window.controls();
  assert.equal(sidesDoc.getElementById("burn-card").disabled, true, "back locked until front acceptance");
  assert(sidesDoc.getElementById("placement-label").textContent.includes("flipped"));
  backend.state.accepted_stages = [0]; await sidesPage.window.poll();
  assert.equal(sidesDoc.getElementById("burn-card").disabled, false);
  assert.equal(sidesDoc.querySelectorAll(".output-stage").length, 1);
  sidesPage.window.close();
  backend.state = {attempt:null,running:false,outcome:null,progress:null,mode,review_id:"github-review"};
  backend.review = {review_id:"github-review",template:templates.find(template=>template.id==="github-recovery"),accepted_stages:[],fields:{"recovery-payload":{provided:true,value:null},"login-url":{provided:true,value:"https://github.com/login"},"front-context-y":{provided:true,value:1.75},"back-context-y":{provided:true,value:1.75}},job:{template:"github-recovery",piece:"bamboo-ntag215-light",backing:"backing-first",placement:"left",guide:false,items:[],values:{}},layout,display:privateDisplay,stages:[{...stage("front"),side:"front"},{...stage("back"),side:"back"}]};
  const githubPage = await open(backend); const githubDoc = githubPage.window.document;
  githubPage.window.selectCardSide("front");
  const recovery = githubDoc.getElementById("field-recovery-payload");
  assert(recovery && recovery.definition.editable && recovery.definition.revealable);
  assert.equal(recovery.hidden,true,"recovery payload is masked on load");
  const recoveryToggle = githubDoc.querySelector('button[aria-controls="field-recovery-payload"]');
  assert.equal(recoveryToggle.hidden,false);
  recoveryToggle.click(); assert.equal(recovery.hidden,false);
  const recoveryFile = githubDoc.getElementById("file-recovery-payload");
  assert(recoveryFile.accept.includes(".vcf"));
  const fileBytes = Buffer.from("SYNTHETIC FIVE-CHAR-DASH DEMO\n","utf8");
  Object.defineProperty(recoveryFile,"files",{configurable:true,value:[{arrayBuffer:async()=>fileBytes.buffer.slice(fileBytes.byteOffset,fileBytes.byteOffset+fileBytes.byteLength)}]});
  recoveryFile.dispatchEvent(new githubPage.window.Event("change"));
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(recovery.value,"SYNTHETIC FIVE-CHAR-DASH DEMO\n");
  await githubPage.window.prepareCard();
  assert.equal(backend.lastChanges["recovery-payload"],"SYNTHETIC FIVE-CHAR-DASH DEMO\n");
  assert.equal(githubDoc.getElementById("card-side").value,"front");
  githubPage.window.selectCardSide("back");
  assert.equal(githubDoc.getElementById("field-recovery-payload").closest("label").hidden,true,"front payload editor is hidden on the back side");
  assert.equal(githubDoc.querySelector('button[aria-controls="field-recovery-payload"]').hidden,false,"real codes can be entered and optionally revealed");
  const loginUrl = githubDoc.getElementById("field-login-url");
  loginUrl.value = "https://github.com/new-login";
  loginUrl.dispatchEvent(new githubPage.window.Event("input", {bubbles:true}));
  await githubPage.window.prepareCard();
  assert.equal(backend.lastChanges["login-url"],"https://github.com/new-login");
  assert.equal(githubDoc.getElementById("card-side").value,"back","previewing the edited login URL keeps the back selected");
  assert.equal(githubDoc.querySelector(".output-stage h3").textContent,"Back content");
  githubPage.window.close();
  const offlineBackend = {audioAnalyzerAvailable:false,state:{attempt:null,running:false,outcome:null,progress:null,mode}};
  const offlinePage = await open(offlineBackend);
  assert.equal(offlinePage.window.document.querySelector('#step option[value="audio_power_sweep_low"]'),null,"low-power audio test is hidden when analyzer is stopped");
  assert.equal(offlinePage.window.document.querySelector('#step option[value="audio_power_sweep_full"]'),null,"full-power audio test is hidden when analyzer is stopped");
  assert(offlinePage.window.document.querySelector('#step option[value="power_sweep"]'),"regular power sweep remains available offline");
  offlinePage.window.close();
  const clearBackend = {state:{attempt:null,running:false,outcome:null,progress:null,mode:{mode:"real",real:true,real_available:true},review_id:"clear-review"}};
  clearBackend.review = {review_id:"clear-review",template:templates[0],fields:{ssid:{provided:true,value:"Synthetic network"},password:{provided:true,value:"synthetic password"},title:{provided:false,value:""}},job:{template:"wifi-login",piece:"bamboo-ntag215-light",backing:"backing-first",placement:"left",guide:true,items:[],values:{}},layout,display:privateDisplay,stages:[stage("guide"),stage("content")]};
  const clearPage = await open(clearBackend); const clearDoc = clearPage.window.document;
  assert.equal(clearDoc.getElementById("clear-pi").textContent,"Clear session");
  assert.equal(clearDoc.getElementById("field-password").value,"synthetic password");
  assert.equal(clearDoc.getElementById("audio-recording-ack").hidden,false,"Wi-Fi recording opt-in is visible before clicking Burn card");
  clearDoc.getElementById("audio-recording-started").checked = true;
  const runsBeforeCardPlan = (clearBackend.runs || []).length;
  await clearPage.window.startStage("content");
  assert.equal(clearDoc.getElementById("audio-recording-ack").hidden,false,"Wi-Fi template opt-in exposes recording confirmation before a Real card run");
  assert.equal(clearDoc.getElementById("audio-recording-started").checked,true,"pre-run recording confirmation survives card planning");
  assert.equal((clearBackend.runs || []).length,runsBeforeCardPlan,"planning a card stage waits for explicit Start");
  assert.equal(clearDoc.getElementById("start-attempt").textContent,"Start content");
  await clearPage.window.clearPi();
  assert.equal(clearBackend.clearCalls,1);
  assert.equal(clearBackend.review,null);
  assert.equal(clearBackend.state.review_id,null);
  assert.equal(clearDoc.getElementById("field-password"),null,"clear removes private review fields from the browser");
  assert(clearDoc.getElementById("status").textContent.includes("run history retained"));
  clearPage.window.close();

  const guideBackend = {state:{attempt:{id:"guide-inspection",review_id:"guide-review",stage_name:"guide",stage:0,goal:"Synthetic guide"},running:false,outcome:{ok:true,saw:"assumed complete"},progress:null,mode,review_id:"guide-review",accepted_stages:[]}};
  guideBackend.review = {review_id:"guide-review",template:templates[0],fields:{ssid:{provided:true,value:"Synthetic"},password:{provided:false,value:""},title:{provided:false,value:""}},job:{template:"wifi-login",piece:"bamboo-ntag215-light",backing:"backing-first",placement:"left",guide:true,items:[],values:{}},layout,display:privateDisplay,stages:[stage("guide"),stage("content")],accepted_stages:[]};
  const inspectionPage = await open(guideBackend), inspectionDoc = inspectionPage.window.document;
  assert.equal(inspectionDoc.getElementById("judge").classList.contains("hidden"),false,"guide inspection is an explicit step");
  assert.equal(inspectionDoc.getElementById("placement-check").hidden,true,"placement waits for acceptance");
  assert.equal(inspectionDoc.getElementById("burn-card").disabled,true);
  assert.equal(inspectionDoc.querySelector('[aria-current="step"]').dataset.workflowStep,"3");
  await inspectionPage.window.verdict("ok");
  assert.equal(guideBackend.verdicts.length,1);
  assert.equal(inspectionDoc.getElementById("placement-check").hidden,false);
  assert.equal(inspectionDoc.getElementById("burn-card").disabled,true,"inspection does not imply placement");
  assert.equal((guideBackend.runs || []).length,0,"acceptance never starts the next stage");
  inspectionPage.window.close();

  const stopBackend = {state:{attempt:null,running:false,outcome:null,progress:null,mode}};
  const stopPage = await open(stopBackend);
  let release;
  const busy = stopPage.window.action(() => new Promise(resolve => { release = resolve; }));
  await stopPage.window.requestStop();
  assert.equal(stopBackend.stopCalls,1,"STOP bypasses ordinary busy work");
  release(); await busy;
  const originalFetch = stopPage.window.fetch;
  let releasePlan;
  stopPage.window.fetch = async (url, options) => {
    if (url === "/api/plan") await new Promise(resolve => { releasePlan = resolve; });
    return originalFetch(url, options);
  };
  stopBackend.review = structuredClone(reuseBackend.review);
  stopBackend.state.review_id = stopBackend.review.review_id;
  stopPage.window.loadReview(stopBackend.review);
  const planning = stopPage.window.startStage("content");
  for (let turn=0;turn<20 && !releasePlan;turn++) await new Promise(setImmediate);
  await stopPage.window.requestStop();
  releasePlan(); await planning;
  assert.equal((stopBackend.runs || []).length,0,"STOP during card planning cancels its pending automatic start");
  stopPage.window.fetch = originalFetch;
  stopBackend.state.mode = {mode:"real",real:true,real_available:true};
  await stopPage.window.poll();
  await stopPage.window.plan();
  let releaseCue;
  const AudioContext = stopPage.window.AudioContext;
  stopPage.window.AudioContext = class extends AudioContext { resume() { return new Promise(resolve => { releaseCue = resolve; }); } };
  stopPage.window.setTimeout = callback => { setImmediate(callback); return 0; };
  const cueStart = stopPage.window.run();
  for (let turn=0;turn<20 && !releaseCue;turn++) await new Promise(setImmediate);
  await stopPage.window.requestStop();
  releaseCue(); await cueStart;
  assert.equal((stopBackend.runs || []).length,0,"STOP during the audio cue prevents the subsequent test start");
  stopPage.window.close();
  console.log("UI checks passed: VSR logo, Light/Dark/System, saved preference, private fields, preview layers and refresh recovery.");
}

main().catch(error => { console.error(error); process.exitCode = 1; });